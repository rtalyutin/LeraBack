"""Single-practitioner setup using existing typed salon metadata and commands.

No schema/permissions changes. Callers use trusted SalonScope; all mutations
take the same advisory transaction lock as bookings and the full constructor.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone

from admin_service import (AdminConflict, _admin_audit, _catalog_name, _unique_name,
                           _weekly_overlay, normalize_weekly_draft)
from booking_core import _working, connect, parse, stamp, validate_policy
from constructor_store import _scope

MODE_CODE = "constructor_mode"
UTC = timezone.utc


def _settings(db):
    sid = _scope(db)
    row = db.execute("""SELECT t.id AS type_id,e.id AS entity_id FROM __APP_SCHEMA__.entity_types t
        JOIN __APP_SCHEMA__.entities e ON e.salon_id=t.salon_id AND e.entity_type_id=t.id
        WHERE t.salon_id=? AND t.code='salon_settings' AND t.core AND NOT e.archived""", (sid,)).fetchone()
    if row is None:
        raise ValueError("Настройки выбранного салона не найдены")
    return sid, row


def _mode_parameter(db, sid, settings):
    row = db.execute("""SELECT p.*,v.value_string FROM __APP_SCHEMA__.entity_parameters p
        LEFT JOIN __APP_SCHEMA__.entity_parameter_values v ON v.salon_id=p.salon_id
          AND v.parameter_id=p.id AND v.entity_id=?
        WHERE p.salon_id=? AND p.entity_type_id=? AND p.code=?""",
        (settings["entity_id"], sid, settings["type_id"], MODE_CODE)).fetchone()
    if row is not None and (row["data_type"] != "string" or row["core"] or row["required"]
                            or row["value_string"] not in (None, "team", "solo")):
        raise AdminConflict("Служебное поле режима конструктора имеет несовместимый формат")
    return row


def get_mode(db):
    sid, settings = _settings(db)
    parameter = _mode_parameter(db, sid, settings)
    return (parameter["value_string"] or "team") if parameter is not None else "team"


def _set_mode(db, mode):
    sid, settings = _settings(db)
    parameter = _mode_parameter(db, sid, settings)
    if parameter is None:
        pid = db.execute("""INSERT INTO __APP_SCHEMA__.entity_parameters
            (salon_id,entity_type_id,code,label,data_type,required)
            VALUES(?,?,?,'Режим конструктора','string',false) RETURNING id""",
            (sid, settings["type_id"], MODE_CODE)).fetchone()[0]
    else:
        pid = parameter["id"]
    db.execute("""INSERT INTO __APP_SCHEMA__.entity_parameter_values
        (salon_id,entity_id,parameter_id,entity_type_id,value_string) VALUES(?,?,?,?,?)
        ON CONFLICT(salon_id,entity_id,parameter_id) DO UPDATE SET value_string=excluded.value_string""",
        (sid, settings["entity_id"], pid, settings["type_id"], mode))


def _active(db, kind):
    return [dict(row) for row in db.execute(f"SELECT id,name FROM {kind}s WHERE active=1 ORDER BY id")]


def _weekly_days(db, kind, resource_id):
    days = [{"weekday": i, "intervals": []} for i in range(7)]
    if resource_id is None:
        return days
    for row in db.execute(f"SELECT weekday,start_minute,end_minute FROM work_intervals "
                          f"WHERE {kind}_id=? AND local_date IS NULL AND mode='open' "
                          "ORDER BY weekday,start_minute,end_minute", (resource_id,)):
        intervals = days[row["weekday"]]["intervals"]
        a, b = row["start_minute"], row["end_minute"]
        if intervals and a <= intervals[-1]["end_minute"]:
            intervals[-1]["end_minute"] = max(intervals[-1]["end_minute"], b)
        else:
            intervals.append({"start_minute": a, "end_minute": b})
    return days


def setup_snapshot(db):
    mode = get_mode(db)
    masters, rooms = _active(db, "master"), _active(db, "room")
    master = masters[0] if len(masters) == 1 else None
    room = rooms[0] if len(rooms) == 1 else None
    services = {row[0] for row in db.execute("SELECT id FROM services WHERE active=1")}
    missing = {}
    for kind, resource in (("master", master), ("room", room)):
        linked = {row[0] for row in db.execute(f"SELECT service_id FROM {kind}_services WHERE {kind}_id=?",
                                             (resource["id"],))} if resource else set()
        # A resource yet to be created is assigned automatically, not reconciled.
        missing[kind + "_missing_ids"] = sorted(services - linked) if resource else []
    md = _weekly_days(db, "master", master["id"] if master else None)
    rd = _weekly_days(db, "room", room["id"] if room else None)
    exceptions = {}
    for kind, resource in (("master", master), ("room", room)):
        exceptions[kind] = [dict(row) for row in db.execute(
            f"SELECT weekday,local_date,mode,start_minute,end_minute FROM work_intervals "
            f"WHERE {kind}_id=? AND (local_date IS NOT NULL OR mode='closed') ORDER BY id",
            (resource["id"],))] if resource else []
    return {"mode": mode, "eligible": len(masters) <= 1 and len(rooms) <= 1,
            "master_id": master["id"] if master else None, "room_id": room["id"] if room else None,
            "master_name": master["name"] if master else "",
            "differences": {"services": any(missing.values()),
                            "schedule": master is not None and room is not None and md != rd},
            "service_differences": missing, "master_days": md, "room_days": rd,
            "schedule_exceptions": exceptions}


def get_setup(path):
    with closing(connect(path)) as db:
        db.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
        result = setup_snapshot(db)
        db.commit()
        return result


def _sole(db):
    masters, rooms = _active(db, "master"), _active(db, "room")
    if len(masters) != 1 or len(rooms) != 1:
        raise AdminConflict("Состав салона изменился. Перейдите в полный конструктор или обновите настройки.")
    return masters[0]["id"], rooms[0]["id"]


def _expected_ids(payload):
    if payload is None:
        return None
    if not isinstance(payload, dict):
        raise ValueError("Ожидаемые ресурсы должны быть объектом")
    keys = ("expected_master_id", "expected_room_id")
    present = [key in payload for key in keys]
    if any(present) and not all(present):
        raise ValueError("Передайте оба expected_master_id и expected_room_id")
    if not all(present):
        return None
    values = tuple(payload[key] for key in keys)
    if any(value is not None and (type(value) is not int or not 0 < value < 9223372036854775807)
           for value in values):
        raise ValueError("Ожидаемый ресурс должен быть положительным целым ID или null")
    return values


def _check_expected(state, expected):
    if expected is not None and expected != (state["master_id"], state["room_id"]):
        raise AdminConflict("Состав салона изменился после загрузки. Обновите настройки и повторите выбор.")


def guard_resource(db, kind, resource_id, active, service_ids=None):
    """Keep solo invariants on existing resource commands, inside their transaction."""
    if get_mode(db) != "solo":
        return service_ids
    mid, rid = _sole(db)
    if resource_id != (mid if kind == "master" else rid) or not active:
        raise AdminConflict("Для добавления или отключения ресурса перейдите в полный конструктор.")
    if service_ids is not None:
        required = {row[0] for row in db.execute("SELECT id FROM services WHERE active=1")}
        if not required.issubset(service_ids):
            raise AdminConflict("В упрощённом режиме все активные услуги доступны единственному мастеру и кабинету.")
    return service_ids


def guard_individual_schedule(db):
    if get_mode(db) == "solo":
        _sole(db)
        raise AdminConflict("В упрощённом режиме изменяйте общий график мастера и кабинета.")


def associate_service(db, service_id):
    if get_mode(db) != "solo":
        return
    mid, rid = _sole(db)
    for kind, resource_id in (("master", mid), ("room", rid)):
        if not db.execute(f"SELECT 1 FROM {kind}_services WHERE {kind}_id=? AND service_id=?",
                          (resource_id, service_id)).fetchone():
            db.execute(f"INSERT INTO {kind}_services({kind}_id,service_id) VALUES (?,?)", (resource_id, service_id))


def _preserve_bookings(db, policy, mid, rid, days, now):
    zone = validate_policy(policy)
    overlay = {**_weekly_overlay("master", mid, days), **_weekly_overlay("room", rid, days)}
    affected = []
    for row in db.execute("SELECT id,master_id,room_id,start_utc,end_utc FROM bookings "
                          "WHERE (master_id=? OR room_id=?) AND status='confirmed' AND end_utc>? ORDER BY id",
                          (mid, rid, stamp(now))):
        start, end = parse(row["start_utc"]), parse(row["end_utc"])
        if ((row["master_id"] == mid and not _working(db, "master", mid, start, end, zone, overlay))
                or (row["room_id"] == rid and not _working(db, "room", rid, start, end, zone, overlay))):
            affected.append(row["id"])
    if affected:
        raise AdminConflict("Общий график затрагивает действующие записи. Сначала перенесите записи.", affected)


def _write_days(db, mid, rid, days):
    for kind, resource_id in (("master", mid), ("room", rid)):
        db.execute(f"DELETE FROM work_intervals WHERE {kind}_id=? AND local_date IS NULL AND mode='open'",
                   (resource_id,))
        db.executemany(f"INSERT INTO work_intervals({kind}_id,weekday,mode,start_minute,end_minute) "
                       "VALUES (?,?,'open',?,?)",
                       [(resource_id, day["weekday"], item["start_minute"], item["end_minute"])
                        for day in days for item in day["intervals"]])


def save_setup(path, policy, payload, actor, now=None):
    now = now or datetime.now(UTC)
    if not isinstance(payload, dict) or payload.get("mode") not in ("team", "solo"):
        raise ValueError("Выберите режим solo или team")
    if not actor or now.tzinfo is None:
        raise ValueError("actor and timezone-aware now required")
    if "reconcile_services" in payload and type(payload["reconcile_services"]) is not bool:
        raise ValueError("reconcile_services должен быть boolean")
    if "hours_source" in payload and payload["hours_source"] not in ("master", "room"):
        raise ValueError("Выберите график мастера или кабинета")
    mode = payload["mode"]
    expected = _expected_ids(payload)
    name = _catalog_name(payload.get("master_name")) if mode == "solo" else None
    with closing(connect(path)) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            old = setup_snapshot(db)
            _check_expected(old, expected)
            if mode == "solo":
                if not old["eligible"]:
                    raise AdminConflict("Упрощённый режим доступен салонам с одним активным мастером и кабинетом.")
                if old["differences"]["services"] and not payload.get("reconcile_services"):
                    raise AdminConflict("Подтвердите назначение всех активных услуг единственному мастеру и кабинету.")
                if old["differences"]["schedule"] and not payload.get("hours_source"):
                    raise AdminConflict("Графики различаются. Выберите график мастера или кабинета для обоих ресурсов.")
                mid, rid = old["master_id"], old["room_id"]
                _unique_name(db, "masters", name, mid)
                if mid is None:
                    mid = db.execute("INSERT INTO masters(name,active) VALUES (?,1)", (name,)).lastrowid
                else:
                    # Updating the local profile preserves its shared master identity.
                    db.execute("UPDATE masters SET name=? WHERE id=?", (name, mid))
                if rid is None:
                    used = {row["name"].casefold() for row in db.execute("SELECT name FROM rooms")}
                    room_name, suffix = "Кабинет", 1
                    while room_name.casefold() in used:
                        suffix += 1
                        room_name = f"Кабинет {suffix}"
                    rid = db.execute("INSERT INTO rooms(name,active) VALUES (?,1)", (room_name,)).lastrowid
                for sid in (row[0] for row in db.execute("SELECT id FROM services WHERE active=1").fetchall()):
                    for kind, resource_id in (("master", mid), ("room", rid)):
                        if not db.execute(f"SELECT 1 FROM {kind}_services WHERE {kind}_id=? AND service_id=?",
                                          (resource_id, sid)).fetchone():
                            db.execute(f"INSERT INTO {kind}_services({kind}_id,service_id) VALUES (?,?)", (resource_id, sid))
                source = payload.get("hours_source") or ("master" if old["master_id"] is not None else "room")
                days = old[source + "_days"]
                # Do not rewrite identical schedules during retries/renames.
                if old["master_days"] != days or old["room_days"] != days:
                    _preserve_bookings(db, policy, mid, rid, days, now)
                    _write_days(db, mid, rid, days)
            _set_mode(db, mode)
            _admin_audit(db, actor, "constructor_mode_saved", "salon", _scope(db),
                         {"mode": mode, "reconcile_services": payload.get("reconcile_services", False),
                          "hours_source": payload.get("hours_source")}, now)
            result = setup_snapshot(db)
            db.commit()
            return result
        except Exception:
            db.rollback()
            raise


def save_schedule(path, policy, days, actor, now=None, *, expectations=None):
    now = now or datetime.now(UTC)
    if not actor or now.tzinfo is None:
        raise ValueError("actor and timezone-aware now required")
    days = normalize_weekly_draft("master", 1, days)
    if len(days) != 7:
        raise ValueError("Общий график должен содержать все семь дней недели")
    expected = _expected_ids(expectations)
    with closing(connect(path)) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            if get_mode(db) != "solo":
                raise AdminConflict("Сначала включите упрощённый конструктор.")
            mid, rid = _sole(db)
            _check_expected({"master_id": mid, "room_id": rid}, expected)
            _preserve_bookings(db, policy, mid, rid, days, now)
            _write_days(db, mid, rid, days)
            _admin_audit(db, actor, "solo_schedule_saved", "salon", _scope(db), {"days": days}, now)
            result = setup_snapshot(db)
            db.commit()
            return result
        except Exception:
            db.rollback()
            raise
