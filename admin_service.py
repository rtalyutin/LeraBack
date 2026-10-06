"""M3 application service used by the admin HTTP boundary and tests."""

from __future__ import annotations

import json
import re
from contextlib import closing
from datetime import date, datetime, timedelta, timezone

from booking_core import (
    BookingConflict, _candidate, _check_phone, _fingerprint, _remember, _replay, _working,
    connect, get_available_slots, parse, stamp, validate_policy,
)
from vk_gateway import stable_random_id

UTC = timezone.utc


class AdminConflict(BookingConflict):
    def __init__(self, message: str, affected_booking_ids=()):
        super().__init__(message)
        self.affected_booking_ids = list(affected_booking_ids)


def _audit(db, booking_id, event, actor, details, now):
    db.execute(
        "INSERT INTO booking_history(booking_id,event,actor,details_json,created_at) VALUES (?,?,?,?,?)",
        (booking_id, event, actor, json.dumps(details, ensure_ascii=False), stamp(now)),
    )


def _admin_audit(db, actor, action, object_type, object_id, details, now):
    db.execute(
        "INSERT INTO admin_audit_log(actor,action,object_type,object_id,details_json,created_at) "
        "VALUES (?,?,?,?,?,?)",
        (actor, action, object_type, str(object_id), json.dumps(details, ensure_ascii=False), stamp(now)),
    )


def _cancel_affected(db, booking_ids, actor, reason, now):
    """Cancel future bookings and queue VK notices in the same transaction."""
    manual_contact_ids = []
    for booking_id in booking_ids:
        row = db.execute(
            "SELECT b.*, c.vk_id FROM bookings b LEFT JOIN clients c ON c.id=b.client_id WHERE b.id=?",
            (booking_id,),
        ).fetchone()
        if not row or row["status"] != "confirmed":
            continue
        db.execute("UPDATE bookings SET status='cancelled',updated_at=? WHERE id=?",
                   (stamp(now), booking_id))
        db.execute("UPDATE message_outbox SET status='superseded' WHERE booking_id=? AND status IN ('pending','failed')",
                   (booking_id,))
        db.execute("UPDATE vk_outgoing_messages SET status='superseded' WHERE booking_id=? AND status IN ('pending','failed')",
                   (booking_id,))
        _audit(db, booking_id, "cancelled", actor, {"reason": reason}, now)
        _admin_audit(db, actor, "booking_cancelled_by_change", "booking", booking_id,
                     {"reason": reason}, now)
        if row["vk_id"] is None:
            manual_contact_ids.append(booking_id)
            continue
        db.execute(
            "INSERT INTO message_outbox(booking_id,recipient_vk_id,event_kind,status,created_at) "
            "VALUES (?,?,'cancelled','pending',?)",
            (booking_id, row["vk_id"], stamp(now)),
        )
        key = f"admin-change-cancel:{booking_id}"
        db.execute(
            "INSERT INTO vk_outgoing_messages(booking_id,recipient_vk_id,text,keyboard_json,dedupe_key,random_id,status,created_at) "
            "VALUES (?,?,?,?,?,?,'pending',?)",
            (booking_id, row["vk_id"],
             f"Запись №{booking_id} отменена из-за изменения расписания салона. Свяжитесь с салоном для новой записи.",
             "[]", key, stable_random_id(key), stamp(now)),
        )
    return manual_contact_ids


def _iso_day_bounds(local_day: date, zone):
    start = datetime(local_day.year, local_day.month, local_day.day, tzinfo=zone).astimezone(UTC)
    return stamp(start), stamp(start + timedelta(days=1))


def snapshot(path, policy, local_day: date):
    zone = validate_policy(policy)
    lower, upper = _iso_day_bounds(local_day, zone)
    with closing(connect(path)) as db:
        db.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
        catalog = {
            "services": [dict(r) for r in db.execute("SELECT * FROM services ORDER BY name")],
            "masters": [dict(r) for r in db.execute("SELECT * FROM masters ORDER BY name")],
            "rooms": [dict(r) for r in db.execute("SELECT * FROM rooms ORDER BY name")],
            "weekly_schedule": [dict(r) for r in db.execute(
                "SELECT id,master_id,room_id,weekday,start_minute,end_minute FROM work_intervals "
                "WHERE local_date IS NULL AND mode='open' ORDER BY weekday,id")],
        }
        for table in ("services", "masters", "rooms"):
            for item in catalog[table]:
                extra = db.execute(f"SELECT entity_id FROM __APP_SCHEMA__.{table} WHERE salon_id=__APP_SCHEMA__.current_salon_id() AND id=?", (item["id"],)).fetchone()
                item["entity_id"] = extra["entity_id"]
        for kind in ("master", "room"):
            links = list(db.execute(f"SELECT {kind}_id,service_id FROM {kind}_services ORDER BY service_id"))
            for resource in catalog[f"{kind}s"]:
                resource["service_ids"] = [link["service_id"] for link in links if link[f"{kind}_id"] == resource["id"]]
        bookings = [dict(r) for r in db.execute(
            "SELECT b.*,c.phone,c.vk_id,r.name AS room_name FROM bookings b "
            "LEFT JOIN clients c ON c.id=b.client_id JOIN rooms r ON r.id=b.room_id "
            "WHERE b.start_utc>=? AND b.start_utc<? ORDER BY b.start_utc,b.master_id",
            (lower, upper),
        )]
        blocks = [dict(r) for r in db.execute(
            "SELECT * FROM resource_blocks WHERE start_utc<? AND end_utc>? ORDER BY start_utc", (upper, lower)
        )]
        delivery_issues = [dict(r) for r in db.execute(
            "SELECT id,booking_id,event_kind,status,attempts,last_error,created_at FROM message_outbox "
            "WHERE status='failed' ORDER BY id DESC LIMIT 50"
        )]
        db.commit()
        return {**catalog, "timezone": policy["timezone"], "bookings": bookings,
                "blocks": blocks, "delivery_issues": delivery_issues}


def create_manual_booking(path, policy, phone, service_id, master_id, start, action_key, actor, now):
    validate_policy(policy)
    _check_phone(phone)
    if not actor or not action_key or start.tzinfo is None or now.tzinfo is None:
        raise ValueError("actor, action key and timezone-aware times are required")
    start = start.astimezone(UTC)
    fp = _fingerprint(phone, service_id, master_id, stamp(start), actor)
    with closing(connect(path)) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            replay = _replay(db, action_key, "admin_confirm", fp)
            if replay:
                db.commit()
                return dict(replay)
            room_id, end, service = _candidate(db, policy, service_id, master_id, start, now, client_phone=phone)
            client_id = db.execute(
                "INSERT INTO clients(vk_id,phone,phone_provided_at) VALUES (NULL,?,?)",
                (phone, stamp(now)),
            ).lastrowid
            booking_id = db.execute(
                "INSERT INTO bookings(client_id,service_id,master_id,room_id,start_utc,end_utc,source,status,"
                "phone_snapshot,service_name_snapshot,master_name_snapshot,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,'admin','confirmed',?,?,?,?,?)",
                (client_id, service_id, master_id, room_id, stamp(start), stamp(end), phone,
                 service["name"], service["master_name"], stamp(now), stamp(now)),
            ).lastrowid
            _audit(db, booking_id, "confirmed", actor, {"source": "admin"}, now)
            _admin_audit(db, actor, "booking_created", "booking", booking_id,
                         {"service_id": service_id, "master_id": master_id, "room_id": room_id}, now)
            _remember(db, action_key, "admin_confirm", fp, booking_id, now)
            result = dict(db.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone())
            db.commit()
            return result
        except Exception:
            db.rollback()
            raise


def cancel_admin_booking(path, booking_id, action_key, actor, now):
    fp = _fingerprint(booking_id, actor)
    with closing(connect(path)) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            replay = _replay(db, action_key, "admin_cancel", fp)
            if replay:
                db.commit()
                return dict(replay)
            row = db.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()
            if row is None:
                raise LookupError("Booking not found")
            if row["status"] != "confirmed":
                raise AdminConflict("Booking is no longer active", [booking_id])
            manual_contact = _cancel_affected(db, [booking_id], actor, "admin_cancel", now)
            _admin_audit(db, actor, "booking_cancelled", "booking", booking_id, {}, now)
            _remember(db, action_key, "admin_cancel", fp, booking_id, now)
            result = dict(db.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone())
            result["manual_contact_booking_ids"] = manual_contact
            db.commit()
            return result
        except Exception:
            db.rollback()
            raise


def create_block(path, resource_kind, resource_id, start, end, reason, actor, acknowledge=False, now=None):
    now = now or datetime.now(UTC)
    if resource_kind not in {"master", "room"} or start.tzinfo is None or end.tzinfo is None or start >= end:
        raise ValueError("Invalid block")
    column = "master_id" if resource_kind == "master" else "room_id"
    with closing(connect(path)) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            affected = [r[0] for r in db.execute(
                f"SELECT id FROM bookings WHERE {column}=? AND status='confirmed' AND start_utc<? AND end_utc>?",
                (resource_id, stamp(end), stamp(start)),
            )]
            if affected and not acknowledge:
                raise AdminConflict("Block intersects confirmed bookings", affected)
            if affected and any(parse(db.execute("SELECT start_utc FROM bookings WHERE id=?", (bid,)).fetchone()[0]) < now
                                for bid in affected):
                raise AdminConflict("Cannot cancel a visit that has already started", affected)
            block_id = db.execute(
                f"INSERT INTO resource_blocks({column},start_utc,end_utc,reason,created_by) VALUES (?,?,?,?,?)",
                (resource_id, stamp(start), stamp(end), reason, actor),
            ).lastrowid
            manual_contact = _cancel_affected(db, affected, actor, "resource_block", now) if affected else []
            _admin_audit(db, actor, "resource_block_created", "resource_block", block_id,
                         {"resource_kind": resource_kind, "resource_id": resource_id,
                          "affected_booking_ids": affected}, now)
            db.commit()
            return {"id": block_id, "cancelled_booking_ids": affected,
                    "manual_contact_booking_ids": manual_contact}
        except Exception:
            db.rollback()
            raise


def _catalog_name(name):
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 160:
        raise ValueError("Название должно содержать от 1 до 160 символов")
    return name.strip()


def _unique_name(db, table, name, object_id=None):
    # Python casefold also works for Cyrillic on PostgreSQL clusters with locale C.
    if any(row["id"] != object_id and row["name"].casefold() == name.casefold()
           for row in db.execute(f"SELECT id,name FROM {table}")):
        raise ValueError("Такое название уже существует")


def create_service(path, name, duration_minutes, actor, now=None):
    now = now or datetime.now(UTC)
    name = _catalog_name(name)
    if type(duration_minutes) is not int or not 1 <= duration_minutes <= 1440:
        raise ValueError("Длительность услуги: от 1 до 1440 минут")
    with closing(connect(path)) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            _unique_name(db, "services", name)
            service_id = db.execute("INSERT INTO services(name,duration_minutes) VALUES (?,?)",
                                    (name, duration_minutes)).lastrowid
            _admin_audit(db, actor, "service_created", "service", service_id,
                         {"name": name, "duration_minutes": duration_minutes}, now)
            db.commit()
            return {"id": service_id}
        except Exception:
            db.rollback()
            raise


def save_resource(path, resource_kind, resource_id, name, service_ids, active, actor,
                  acknowledge=False, now=None):
    """Create/edit a master or room, including its service eligibility."""
    now = now or datetime.now(UTC)
    name = _catalog_name(name)
    if resource_kind not in {"master", "room"} or type(active) is not bool:
        raise ValueError("Invalid resource or active flag")
    if resource_id is not None and (type(resource_id) is not int or resource_id <= 0):
        raise ValueError("Invalid resource ID")
    if not isinstance(service_ids, list) or any(type(x) is not int or x <= 0 for x in service_ids):
        raise ValueError("Услуги должны быть списком идентификаторов")
    service_ids = sorted(set(service_ids))
    table, column = f"{resource_kind}s", f"{resource_kind}_id"
    with closing(connect(path)) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            _unique_name(db, table, name, resource_id)
            for service_id in service_ids:
                if not db.execute("SELECT 1 FROM services WHERE id=?", (service_id,)).fetchone():
                    raise ValueError("Неизвестная услуга")
            affected = []
            if resource_id is None:
                resource_id = db.execute(f"INSERT INTO {table}(name,active) VALUES (?,?)",
                                         (name, int(active))).lastrowid
                action = "resource_created"
            else:
                old = db.execute(f"SELECT * FROM {table} WHERE id=?", (resource_id,)).fetchone()
                if old is None:
                    raise LookupError("Ресурс не найден")
                removed = {r[0] for r in db.execute(f"SELECT service_id FROM {resource_kind}_services WHERE {column}=?",
                                                   (resource_id,))} - set(service_ids)
                rows = db.execute(f"SELECT id,service_id,start_utc FROM bookings WHERE {column}=? "
                                  "AND status='confirmed' AND end_utc>?", (resource_id, stamp(now))).fetchall()
                affected_rows = [r for r in rows if (old["active"] and not active) or r["service_id"] in removed]
                if any(parse(r["start_utc"]) <= now for r in affected_rows):
                    raise AdminConflict("Нельзя изменить доступность ресурса во время приёма",
                                        [r["id"] for r in affected_rows])
                affected = [r["id"] for r in affected_rows]
                if affected and not acknowledge:
                    raise AdminConflict("Изменение затрагивает действующие записи", affected)
                db.execute(f"UPDATE {table} SET name=?,active=? WHERE id=?", (name, int(active), resource_id))
                action = "resource_updated"
            db.execute(f"DELETE FROM {resource_kind}_services WHERE {column}=?", (resource_id,))
            db.executemany(f"INSERT INTO {resource_kind}_services({column},service_id) VALUES (?,?)",
                           [(resource_id, sid) for sid in service_ids])
            manual = _cancel_affected(db, affected, actor, "resource_changed", now)
            _admin_audit(db, actor, action, resource_kind, resource_id,
                         {"name": name, "service_ids": service_ids, "active": active,
                          "cancelled_booking_ids": affected}, now)
            db.commit()
            return {"id": resource_id, "cancelled_booking_ids": affected, "manual_contact_booking_ids": manual}
        except Exception:
            db.rollback()
            raise


def update_service(path, service_id, name, duration_minutes, active, actor, acknowledge=False, now=None):
    now = now or datetime.now(UTC)
    name = _catalog_name(name)
    if type(duration_minutes) is not int or not 1 <= duration_minutes <= 1440 or type(active) is not bool:
        raise ValueError("Длительность услуги: от 1 до 1440 минут; active должен быть boolean")
    with closing(connect(path)) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            old = db.execute("SELECT * FROM services WHERE id=?", (service_id,)).fetchone()
            if old is None:
                raise LookupError("Service not found")
            _unique_name(db, "services", name, service_id)
            affected = [r[0] for r in db.execute(
                "SELECT id FROM bookings WHERE service_id=? AND status='confirmed' AND start_utc>=?",
                (service_id, stamp(now)),
            )]
            changed_availability = (old["active"] != int(bool(active)) or old["duration_minutes"] != duration_minutes)
            if affected and changed_availability and not acknowledge:
                raise AdminConflict("Service change affects future confirmed bookings", affected)
            db.execute("UPDATE services SET name=?,duration_minutes=?,active=? WHERE id=?",
                       (name, duration_minutes, int(bool(active)), service_id))
            manual_contact = _cancel_affected(db, affected, actor, "service_changed", now) if changed_availability else []
            _admin_audit(db, actor, "service_updated", "service", service_id,
                         {"duration_minutes": duration_minutes, "active": bool(active),
                          "affected_booking_ids": affected if changed_availability else []}, now)
            db.commit()
            return {"id": service_id,
                    "cancelled_booking_ids": affected if changed_availability else [],
                    "manual_contact_booking_ids": manual_contact}
        except Exception:
            db.rollback()
            raise


def update_weekly_schedule(path, policy, resource_kind, resource_id, weekday,
                           start_minute, end_minute, actor, acknowledge=False, now=None, *, intervals=None):
    """Replace a day's opening intervals; an empty list marks the day closed.

    The legacy start_minute/end_minute request remains supported.
    """
    now = now or datetime.now(UTC)
    zone = validate_policy(policy)
    if resource_kind not in {"master", "room"} or type(weekday) is not int or not 0 <= weekday <= 6:
        raise ValueError("Invalid resource kind or weekday")
    if intervals is None:
        if (start_minute is None) != (end_minute is None):
            raise ValueError("Both times must be provided or both omitted")
        intervals = [] if start_minute is None else [{"start_minute": start_minute, "end_minute": end_minute}]
    if not isinstance(intervals, list):
        raise ValueError("Рабочие интервалы должны быть списком")
    normalized = []
    for interval in intervals:
        if not isinstance(interval, dict):
            raise ValueError("Invalid work interval")
        a, b = interval.get("start_minute"), interval.get("end_minute")
        if type(a) is not int or type(b) is not int or not 0 <= a < b <= 1440:
            raise ValueError("Начало должно быть раньше конца в пределах одного дня")
        normalized.append((a, b))
    merged = []
    for a, b in sorted(normalized):
        if merged and a < merged[-1][1]:
            raise ValueError("Рабочие интервалы не должны пересекаться")
        if merged and a == merged[-1][1]:
            merged[-1] = (merged[-1][0], b)
        else:
            merged.append((a, b))
    column = "master_id" if resource_kind == "master" else "room_id"
    table = "masters" if resource_kind == "master" else "rooms"
    with closing(connect(path)) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            if not db.execute(f"SELECT 1 FROM {table} WHERE id=?", (resource_id,)).fetchone():
                raise LookupError("Resource not found")
            db.execute(f"DELETE FROM work_intervals WHERE {column}=? AND weekday=? AND local_date IS NULL AND mode='open'",
                       (resource_id, weekday))
            for a, b in merged:
                db.execute(
                    f"INSERT INTO work_intervals({column},weekday,mode,start_minute,end_minute) "
                    "VALUES (?,?,'open',?,?)", (resource_id, weekday, a, b),
                )
            upcoming = db.execute(
                f"SELECT id,start_utc,end_utc FROM bookings WHERE {column}=? AND status='confirmed' AND end_utc>?",
                (resource_id, stamp(now)),
            ).fetchall()
            affected = [r["id"] for r in upcoming
                        if parse(r["start_utc"]).astimezone(zone).weekday() == weekday
                        and not _working(db, resource_kind, resource_id, parse(r["start_utc"]),
                                         parse(r["end_utc"]), zone)]
            if any(r["id"] in affected and parse(r["start_utc"]) <= now for r in upcoming):
                raise AdminConflict("Нельзя изменить график во время приёма", affected)
            if affected and not acknowledge:
                raise AdminConflict("Schedule change affects future confirmed bookings", affected)
            manual_contact = _cancel_affected(db, affected, actor, "weekly_schedule_changed", now)
            _admin_audit(db, actor, "weekly_schedule_updated", resource_kind, resource_id,
                         {"weekday": weekday, "intervals": merged,
                          "cancelled_booking_ids": affected}, now)
            db.commit()
            return {"resource_kind": resource_kind, "resource_id": resource_id, "weekday": weekday,
                    "cancelled_booking_ids": affected, "manual_contact_booking_ids": manual_contact}
        except Exception:
            db.rollback()
            raise


def _positive_id(value):
    if type(value) is not int or not 0 < value < 9223372036854775807:
        raise ValueError("Идентификатор должен быть положительным целым числом")
    return value


def normalize_weekly_draft(resource_kind, resource_id, days):
    """Validate all selected days before either previewing or persisting them."""
    if not isinstance(resource_kind, str) or resource_kind not in {"master", "room"}:
        raise ValueError("Выберите мастера или кабинет")
    _positive_id(resource_id)
    if not isinstance(days, list) or not 1 <= len(days) <= 7:
        raise ValueError("Выберите от одного до семи дней недели")
    normalized = []
    seen = set()
    for day in days:
        if not isinstance(day, dict):
            raise ValueError("День недели должен быть объектом")
        weekday = day.get("weekday")
        if type(weekday) is not int or not 0 <= weekday <= 6:
            raise ValueError("День недели должен быть числом от 0 до 6")
        if weekday in seen:
            raise ValueError("Один день недели нельзя передать дважды")
        seen.add(weekday)
        intervals = day.get("intervals")
        if not isinstance(intervals, list):
            raise ValueError("Рабочие интервалы должны быть списком")
        opening = []
        for interval in intervals:
            if not isinstance(interval, dict):
                raise ValueError("Рабочий интервал должен быть объектом")
            a, b = interval.get("start_minute"), interval.get("end_minute")
            if type(a) is not int or type(b) is not int or not 0 <= a < b <= 1440:
                raise ValueError("Начало должно быть раньше конца в пределах одного дня")
            opening.append((a, b))
        merged = []
        for a, b in sorted(opening):
            if merged and a < merged[-1][1]:
                raise ValueError("Рабочие интервалы не должны пересекаться")
            if merged and a == merged[-1][1]:
                merged[-1] = (merged[-1][0], b)
            else:
                merged.append((a, b))
        normalized.append({"weekday": weekday, "intervals": [
            {"start_minute": a, "end_minute": b} for a, b in merged]})
    return sorted(normalized, key=lambda day: day["weekday"])


def _weekly_overlay(resource_kind, resource_id, days):
    return {(resource_kind, resource_id, day["weekday"]): [
        (interval["start_minute"], interval["end_minute"]) for interval in day["intervals"]]
        for day in days}


def _require_resource(db, resource_kind, resource_id):
    # The connection's scoped view checks both existence and salon ownership.
    if not db.execute(f"SELECT 1 FROM {resource_kind}s WHERE id=?", (resource_id,)).fetchone():
        raise LookupError("Мастер или кабинет не найден в выбранном салоне")


def update_weekly_schedule_batch(path, policy, resource_kind, resource_id, days, actor, now=None):
    """Atomically replace selected days, preserving every confirmed booking.

    Unlike the legacy single-day endpoint, this command never cancels bookings.
    An impact conflict is always returned before any schedule writes.
    """
    now = now or datetime.now(UTC)
    zone = validate_policy(policy)
    if not actor or now.tzinfo is None:
        raise ValueError("actor and timezone-aware now required")
    days = normalize_weekly_draft(resource_kind, resource_id, days)
    overlay = _weekly_overlay(resource_kind, resource_id, days)
    column = f"{resource_kind}_id"
    selected = {day["weekday"] for day in days}
    with closing(connect(path)) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            _require_resource(db, resource_kind, resource_id)
            upcoming = db.execute(
                f"SELECT id,start_utc,end_utc FROM bookings WHERE {column}=? "
                "AND status='confirmed' AND end_utc>? ORDER BY id",
                (resource_id, stamp(now)),
            ).fetchall()
            affected = [row["id"] for row in upcoming
                        if parse(row["start_utc"]).astimezone(zone).weekday() in selected
                        and not _working(db, resource_kind, resource_id, parse(row["start_utc"]),
                                         parse(row["end_utc"]), zone, overlay)]
            if affected:
                raise AdminConflict("Новый график затрагивает действующие записи. Перенесите записи перед изменением графика.", affected)
            for day in days:
                db.execute(f"DELETE FROM work_intervals WHERE {column}=? AND weekday=? "
                           "AND local_date IS NULL AND mode='open'", (resource_id, day["weekday"]))
                db.executemany(
                    f"INSERT INTO work_intervals({column},weekday,mode,start_minute,end_minute) "
                    "VALUES (?,?,'open',?,?)",
                    [(resource_id, day["weekday"], item["start_minute"], item["end_minute"])
                     for item in day["intervals"]])
            _admin_audit(db, actor, "weekly_schedule_batch_updated", resource_kind, resource_id,
                         {"days": days}, now)
            db.commit()
            return {"resource_kind": resource_kind, "resource_id": resource_id, "days": days,
                    "cancelled_booking_ids": [], "manual_contact_booking_ids": []}
        except Exception:
            db.rollback()
            raise


def availability_preview(path, policy, service_id, master_id, local_date, now, draft=None, *, drafts=None):
    """Read-only real booking availability with optional in-memory drafts."""
    zone = validate_policy(policy)
    _positive_id(service_id)
    _positive_id(master_id)
    if not isinstance(local_date, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", local_date):
        raise ValueError("Дата должна быть в формате YYYY-MM-DD")
    local_day = date.fromisoformat(local_date)
    if now.tzinfo is None:
        raise ValueError("Timezone-aware now required")
    if draft is not None and drafts is not None:
        raise ValueError("Передайте draft или drafts, но не оба поля")
    if drafts is not None and (not isinstance(drafts, list) or len(drafts) > 100):
        raise ValueError("Черновики должны быть списком не более чем из 100 ресурсов")
    requested_drafts = drafts if drafts is not None else ([] if draft is None else [draft])
    overlay = {}
    resources = set()
    for item in requested_drafts:
        if not isinstance(item, dict):
            raise ValueError("Черновик расписания должен быть объектом")
        kind, resource_id = item.get("resource_kind"), item.get("resource_id")
        days = normalize_weekly_draft(kind, resource_id, item.get("days"))
        candidate = _weekly_overlay(kind, resource_id, days)
        if overlay.keys() & candidate.keys():
            raise ValueError("Черновик одного ресурса и дня нельзя передать дважды")
        overlay.update(candidate)
        resources.add((kind, resource_id))
    with closing(connect(path)) as db:
        db.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
        try:
            for table, object_id in (("services", service_id), ("masters", master_id)):
                if not db.execute(f"SELECT 1 FROM {table} WHERE id=?", (object_id,)).fetchone():
                    raise LookupError("Услуга или мастер не найдены в выбранном салоне")
            for kind, resource_id in resources:
                _require_resource(db, kind, resource_id)
            slots = get_available_slots(db, policy, service_id, master_id, local_day, now,
                                        weekly_overlay=overlay)
            db.commit()
        except Exception:
            db.rollback()
            raise
    constraints = [
        "Учтены графики мастера и подходящих кабинетов, перерывы, блокировки и действующие записи.",
        "Услуга и мастер должны быть активны; эта услуга должна быть доступна у мастера и хотя бы в одном активном кабинете.",
        f"Горизонт записи: {policy['booking_horizon_days']} дней; минимальное время до записи: {policy['min_notice_minutes']} мин.",
        f"Шаг времени: {policy['slot_step_minutes']} мин; интервал между записями: {policy['buffer_minutes']} мин.",
    ]
    if not policy["same_day_allowed"]:
        constraints.append("Запись на текущий день отключена.")
    days_ahead = (local_day - now.astimezone(zone).date()).days
    empty_message = None
    if not slots:
        if days_ahead < 0 or days_ahead > policy["booking_horizon_days"]:
            empty_message = "Выбранная дата за пределами доступного периода записи."
        elif days_ahead == 0 and not policy["same_day_allowed"]:
            empty_message = "Запись на текущий день отключена правилами салона."
        else:
            empty_message = "На выбранную дату доступных окон нет с учётом графиков, связей услуг, блокировок, записей и правил салона."
    return {"slots": slots, "timezone": policy["timezone"], "date": local_date,
            "draft_applied": bool(overlay), "constraints": constraints, "empty_message": empty_message}
