"""Local M1/M2 prototype. No VK or HTTP entry point is exposed here."""

from __future__ import annotations

import hashlib
import json
import re
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from pg_store import connect

UTC = timezone.utc
SCHEMA = Path(__file__).with_name("schema_postgres.sql")
REQUIRED_POLICY = {
    "timezone", "booking_horizon_days", "slot_step_minutes", "min_notice_minutes",
    "same_day_allowed", "buffer_minutes", "room_capacity",
    "allow_parallel_client_bookings", "cancel_cutoff_minutes",
    "reschedule_cutoff_minutes",
}


class BookingConflict(Exception):
    pass


class BookingPermissionError(Exception):
    pass


def migrate(db) -> None:
    """Upgrade atomically; never replay the legacy schema over shared tables."""
    from shared_schema import upgrade_shared
    from vk_connections import upgrade_vk
    if db.scope:
        raise ValueError("Migrations require an unscoped developer connection")
    try:
        db.execute("BEGIN IMMEDIATE")
        exists = db.execute("SELECT to_regclass('__APP_SCHEMA__.schema_migrations')").fetchone()[0]
        version = db.execute("SELECT max(version) FROM __APP_SCHEMA__.schema_migrations").fetchone()[0] if exists else 0
        if (version or 0) > 5:
            raise RuntimeError("Selected database schema is newer than supported version 5")
        if (version or 0) < 4:
            db.execute(SCHEMA.read_text(encoding="utf-8"))
            db.execute("ALTER TABLE __APP_SCHEMA__.services DROP CONSTRAINT IF EXISTS services_duration_minutes_check")
            db.execute("ALTER TABLE __APP_SCHEMA__.services ADD CONSTRAINT services_duration_minutes_check CHECK (duration_minutes BETWEEN 1 AND 1440)")
            for number in (1, 2, 3):
                db.execute("INSERT INTO __APP_SCHEMA__.schema_migrations(version,applied_at) VALUES (?, to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"')) ON CONFLICT DO NOTHING", (number,))
        # Version 4 has repeatable function/trigger/policy repairs. Refresh it
        # under the same transaction/lock without replaying legacy DDL/backfill.
        upgrade_shared(db)
        upgrade_vk(db)
        db.commit()
    except Exception:
        db.rollback()
        raise


def validate_policy(policy: dict) -> ZoneInfo:
    missing = REQUIRED_POLICY - policy.keys()
    if missing or any(policy[k] is None for k in REQUIRED_POLICY if k in policy):
        raise ValueError(f"Unapproved policy: {', '.join(sorted(missing | {k for k in REQUIRED_POLICY if policy.get(k) is None}))}")
    if policy["room_capacity"] != 1:
        raise ValueError("This core supports one simultaneous booking per room only")
    for key in ("booking_horizon_days", "min_notice_minutes", "buffer_minutes", "cancel_cutoff_minutes", "reschedule_cutoff_minutes"):
        if type(policy[key]) is not int or policy[key] < 0:
            raise ValueError(f"Invalid {key}")
    if type(policy["slot_step_minutes"]) is not int or not 1 <= policy["slot_step_minutes"] <= 60:
        raise ValueError("Invalid slot_step_minutes")
    for key in ("same_day_allowed", "allow_parallel_client_bookings"):
        if type(policy[key]) is not bool:
            raise ValueError(f"Invalid {key}")
    return ZoneInfo(policy["timezone"])


def stamp(moment: datetime) -> str:
    if moment.tzinfo is None:
        raise ValueError("Timezone-aware datetime required")
    return moment.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse(stored: str) -> datetime:
    return datetime.fromisoformat(stored.replace("Z", "+00:00"))


def _fingerprint(*values: object) -> str:
    payload = json.dumps(values, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def _replay(db, action_key: str, kind: str, fingerprint: str):
    row = db.execute("SELECT * FROM action_results WHERE action_key=?", (action_key,)).fetchone()
    if row is None:
        return None
    if row["action_kind"] != kind or row["fingerprint"] != fingerprint:
        raise BookingConflict("Action key reused for different operation")
    return db.execute("SELECT * FROM bookings WHERE id=?", (row["booking_id"],)).fetchone()


def _remember(db, key, kind, fingerprint, booking_id, now):
    db.execute(
        "INSERT INTO action_results(action_key,action_kind,fingerprint,booking_id,created_at) VALUES (?,?,?,?,?)",
        (key, kind, fingerprint, booking_id, stamp(now)),
    )


def _intervals(db, kind, resource_id, local_day, weekly_overlay=None):
    col = "master_id" if kind == "master" else "room_id"
    args = (resource_id, local_day.isoformat(), local_day.weekday())
    rows = db.execute(
        f"SELECT mode,start_minute,end_minute,local_date FROM work_intervals "
        f"WHERE {col}=? AND (local_date=? OR weekday=?)", args
    ).fetchall()
    dated_opens = [(r["start_minute"], r["end_minute"]) for r in rows if r["mode"] == "open" and r["local_date"] is not None]
    weekly_opens = [(r["start_minute"], r["end_minute"]) for r in rows if r["mode"] == "open" and r["local_date"] is None]
    # A validated constructor draft replaces only the selected weekly openings.
    # Dated exceptions and saved closed intervals retain their normal precedence.
    key = (kind, resource_id, local_day.weekday())
    if weekly_overlay is not None and key in weekly_overlay:
        weekly_opens = weekly_overlay[key]
    closed = [(r["start_minute"], r["end_minute"]) for r in rows if r["mode"] == "closed"]
    return dated_opens if dated_opens else weekly_opens, closed


def _working(db, kind, resource_id, start, end, zone, weekly_overlay=None):
    first, last = start.astimezone(zone), end.astimezone(zone)
    ends_at_midnight = (
        last.date() == first.date() + timedelta(days=1)
        and last.hour == last.minute == last.second == last.microsecond == 0
    )
    if first.date() != last.date() and not ends_at_midnight:
        return False  # Overnight shifts require a separately agreed rule.
    a = first.hour * 60 + first.minute
    b = 1440 if ends_at_midnight else last.hour * 60 + last.minute
    if last.second or last.microsecond:
        b += 1
    opens, closed = _intervals(db, kind, resource_id, first.date(), weekly_overlay)
    return any(lo <= a and b <= hi for lo, hi in opens) and not any(lo < b and hi > a for lo, hi in closed)


def _unblocked(db, kind, resource_id, start, end):
    col = "master_id" if kind == "master" else "room_id"
    return db.execute(
        f"SELECT 1 FROM resource_blocks WHERE {col}=? AND start_utc<? AND end_utc>? LIMIT 1",
        (resource_id, stamp(end), stamp(start)),
    ).fetchone() is None


def _unbooked(db, kind, resource_id, start, end, buffer_minutes, except_id=None):
    col = "master_id" if kind == "master" else "room_id"
    pad = timedelta(minutes=buffer_minutes)
    if kind == "master":
        return not db.execute(
            "SELECT __APP_SCHEMA__.shared_master_conflict(?,?,?,?)",
            (resource_id, stamp(start - pad), stamp(end + pad), except_id),
        ).fetchone()[0]
    return db.execute(
        f"SELECT 1 FROM bookings WHERE {col}=? AND status='confirmed' "
        "AND id IS DISTINCT FROM ? AND start_utc<? AND end_utc>? LIMIT 1",
        (resource_id, except_id, stamp(end + pad), stamp(start - pad)),
    ).fetchone() is None


def _candidate(db, policy, service_id, master_id, start, now, except_id=None, client_id=None, client_phone=None,
               *, weekly_overlay=None):
    zone = validate_policy(policy)
    now = now.astimezone(UTC)
    local_start = start.astimezone(zone)
    if (local_start.hour * 60 + local_start.minute) % policy["slot_step_minutes"] != 0:
        raise BookingConflict("Start is off the configured slot grid")
    if local_start.second or local_start.microsecond:
        raise BookingConflict("Start must be on a whole minute")
    today = now.astimezone(zone).date()
    days = (local_start.date() - today).days
    if days < 0 or days > policy["booking_horizon_days"] or (days == 0 and not policy["same_day_allowed"]):
        raise BookingConflict("Date outside booking window")
    if start < now + timedelta(minutes=policy["min_notice_minutes"]):
        raise BookingConflict("Insufficient notice")
    row = db.execute(
        "SELECT s.name, s.duration_minutes, m.name AS master_name FROM services s "
        "JOIN master_services ms ON ms.service_id=s.id "
        "JOIN masters m ON m.id=ms.master_id "
        "WHERE s.id=? AND m.id=? AND s.active=1 AND m.active=1",
        (service_id, master_id),
    ).fetchone()
    if row is None:
        raise BookingConflict("Service or master unavailable")
    end = start + timedelta(minutes=row["duration_minutes"])
    if not _working(db, "master", master_id, start, end, zone, weekly_overlay) or not _unblocked(db, "master", master_id, start, end):
        raise BookingConflict("Master outside schedule or blocked")
    if not _unbooked(db, "master", master_id, start, end, policy["buffer_minutes"], except_id):
        raise BookingConflict("Master occupied")
    if not policy["allow_parallel_client_bookings"] and (client_id is not None or client_phone is not None):
        overlap = db.execute(
            "SELECT 1 FROM bookings WHERE (client_id=? OR phone_snapshot=?) "
            "AND id IS DISTINCT FROM ? AND status='confirmed' "
            "AND start_utc<? AND end_utc>? LIMIT 1",
            (client_id, client_phone, except_id, stamp(end), stamp(start)),
        ).fetchone()
        if overlap:
            raise BookingConflict("Client has an overlapping booking")
    for room in db.execute(
        "SELECT r.id FROM rooms r JOIN room_services rs ON rs.room_id=r.id "
        "WHERE rs.service_id=? AND r.active=1 ORDER BY r.id", (service_id,)
    ):
        room_id = room["id"]
        if _working(db, "room", room_id, start, end, zone, weekly_overlay) and _unblocked(db, "room", room_id, start, end) and _unbooked(db, "room", room_id, start, end, policy["buffer_minutes"], except_id):
            return room_id, end, row
    raise BookingConflict("No suitable room")


def _available_slots(db, policy, service_id, master_id, local_day: date, now: datetime,
                     *, weekly_overlay=None, except_id=None):
    zone = validate_policy(policy)
    if now.tzinfo is None:
        raise ValueError("Timezone-aware now required")
    step = policy["slot_step_minutes"]
    for minute in range(0, 1440, step):
        wall = datetime(local_day.year, local_day.month, local_day.day, minute // 60, minute % 60)
        for fold in (0, 1):
            candidate = wall.replace(tzinfo=zone, fold=fold)
            if candidate.astimezone(UTC).astimezone(zone).replace(tzinfo=None) != wall:
                continue
            if fold == 1 and candidate.utcoffset() == wall.replace(tzinfo=zone, fold=0).utcoffset():
                continue
            start = candidate.astimezone(UTC)
            try:
                _candidate(db, policy, service_id, master_id, start, now,
                           weekly_overlay=weekly_overlay, except_id=except_id)
            except BookingConflict:
                continue
            yield stamp(start)


def get_available_slots(db, policy, service_id, master_id, local_day: date, now: datetime,
                        *, weekly_overlay=None, except_id=None):
    return list(_available_slots(db, policy, service_id, master_id, local_day, now,
                                 weekly_overlay=weekly_overlay, except_id=except_id))


def get_available_days(db, policy, service_id, master_id, now: datetime, *, except_id=None):
    """Return salon-local dates with at least one bookable appointment.

    Stop after the first free slot on each day; final booking still checks
    availability atomically. Reuse the exact slot rules, including DST.
    """
    zone = validate_policy(policy)
    if now.tzinfo is None:
        raise ValueError("Timezone-aware now required")
    today = now.astimezone(zone).date()
    first = 0 if policy["same_day_allowed"] else 1
    result = []
    for offset in range(first, policy["booking_horizon_days"] + 1):
        day = today + timedelta(days=offset)
        if next(_available_slots(db, policy, service_id, master_id, day, now,
                                 except_id=except_id), None) is not None:
            result.append(day.isoformat())
    return result


def _check_phone(phone: str):
    if not re.fullmatch(r"\+?[0-9]{10,15}", phone):
        raise ValueError("Phone must contain 10–15 digits, optional leading +")


def _owned(db, booking_id: int, vk_id: int):
    row = db.execute(
        "SELECT b.* FROM bookings b JOIN clients c ON c.id=b.client_id "
        "WHERE b.id=? AND c.vk_id=?", (booking_id, vk_id)
    ).fetchone()
    if row is None:
        raise BookingPermissionError("Booking does not belong to this VK user")
    return row


def confirm_booking(path, policy, vk_id: int, phone: str, service_id: int, master_id: int,
                    start: datetime, action_key: str, now: datetime):
    validate_policy(policy)
    _check_phone(phone)
    if not action_key or vk_id <= 0:
        raise ValueError("VK ID and action key required")
    if start.tzinfo is None or now.tzinfo is None:
        raise ValueError("Timezone-aware start and now required")
    start = start.astimezone(UTC)
    fp = _fingerprint(vk_id, phone, service_id, master_id, stamp(start))
    with closing(connect(path)) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            replay = _replay(db, action_key, "confirm", fp)
            if replay:
                db.commit()
                return dict(replay)
            client = db.execute("SELECT id FROM clients WHERE vk_id=?", (vk_id,)).fetchone()
            room_id, end, service = _candidate(db, policy, service_id, master_id, start, now,
                                               client_id=client["id"] if client else None,
                                               client_phone=phone)
            if client:
                client_id = client["id"]
                db.execute("UPDATE clients SET phone=?,phone_provided_at=? WHERE id=?", (phone, stamp(now), client_id))
            else:
                client_id = db.execute(
                    "INSERT INTO clients(vk_id,phone,phone_provided_at) VALUES (?,?,?)",
                    (vk_id, phone, stamp(now))
                ).lastrowid
            booking_id = db.execute(
                "INSERT INTO bookings(client_id,service_id,master_id,room_id,start_utc,end_utc,source,status,"
                "phone_snapshot,service_name_snapshot,master_name_snapshot,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,'vk','confirmed',?,?,?,?,?)",
                (client_id, service_id, master_id, room_id, stamp(start), stamp(end), phone,
                 service["name"], service["master_name"], stamp(now), stamp(now)),
            ).lastrowid
            db.execute(
                "INSERT INTO booking_history(booking_id,event,actor,details_json,created_at) VALUES (?,?,?,?,?)",
                (booking_id, "confirmed", f"vk:{vk_id}", "{}", stamp(now)),
            )
            db.execute(
                "INSERT INTO message_outbox(booking_id,recipient_vk_id,event_kind,status,created_at) "
                "VALUES (?,?, 'confirmed','pending',?)", (booking_id, vk_id, stamp(now)),
            )
            _remember(db, action_key, "confirm", fp, booking_id, now)
            result = dict(db.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone())
            db.commit()
            return result
        except Exception:
            db.rollback()
            raise


def cancel_booking(path, policy, vk_id: int, booking_id: int, action_key: str, now: datetime):
    validate_policy(policy)
    if now.tzinfo is None or not action_key:
        raise ValueError("Timezone-aware now and action key required")
    fp = _fingerprint(vk_id, booking_id)
    with closing(connect(path)) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            replay = _replay(db, action_key, "cancel", fp)
            if replay:
                db.commit()
                return dict(replay)
            old = _owned(db, booking_id, vk_id)
            if old["status"] != "confirmed":
                raise BookingConflict("Booking is no longer active")
            if parse(old["start_utc"]) < now.astimezone(UTC) + timedelta(minutes=policy["cancel_cutoff_minutes"]):
                raise BookingConflict("Cancellation cutoff reached")
            db.execute("UPDATE bookings SET status='cancelled',updated_at=? WHERE id=?", (stamp(now), booking_id))
            db.execute("UPDATE message_outbox SET status='superseded' WHERE booking_id=? "
                       "AND event_kind IN ('confirmed','rescheduled') AND status IN ('pending','failed')", (booking_id,))
            db.execute("UPDATE vk_outgoing_messages SET status='superseded' WHERE booking_id=? "
                       "AND event_kind IN ('confirm','reschedule_confirm') AND status IN ('pending','failed')", (booking_id,))
            db.execute("INSERT INTO booking_history(booking_id,event,actor,details_json,created_at) VALUES (?,?,?,?,?)",
                       (booking_id, "cancelled", f"vk:{vk_id}", "{}", stamp(now)))
            db.execute("INSERT INTO message_outbox(booking_id,recipient_vk_id,event_kind,status,created_at) "
                       "VALUES (?,?, 'cancelled','pending',?)", (booking_id, vk_id, stamp(now)))
            _remember(db, action_key, "cancel", fp, booking_id, now)
            result = dict(db.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone())
            db.commit()
            return result
        except Exception:
            db.rollback()
            raise


def reschedule_booking(path, policy, vk_id: int, booking_id: int, new_start: datetime,
                       action_key: str, now: datetime):
    validate_policy(policy)
    if new_start.tzinfo is None or now.tzinfo is None or not action_key:
        raise ValueError("Timezone-aware new_start, now and action key required")
    new_start = new_start.astimezone(UTC)
    fp = _fingerprint(vk_id, booking_id, stamp(new_start))
    with closing(connect(path)) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            replay = _replay(db, action_key, "reschedule", fp)
            if replay:
                db.commit()
                return dict(replay)
            old = _owned(db, booking_id, vk_id)
            if old["status"] != "confirmed":
                raise BookingConflict("Booking is no longer active")
            if parse(old["start_utc"]) < now.astimezone(UTC) + timedelta(minutes=policy["reschedule_cutoff_minutes"]):
                raise BookingConflict("Reschedule cutoff reached")
            room_id, end, service = _candidate(
                db, policy, old["service_id"], old["master_id"], new_start, now,
                except_id=booking_id, client_id=old["client_id"],
                client_phone=old["phone_snapshot"])
            db.execute("UPDATE bookings SET status='rescheduled',updated_at=? WHERE id=?", (stamp(now), booking_id))
            db.execute("UPDATE message_outbox SET status='superseded' WHERE booking_id=? "
                       "AND event_kind IN ('confirmed','rescheduled') AND status IN ('pending','failed')", (booking_id,))
            db.execute("UPDATE vk_outgoing_messages SET status='superseded' WHERE booking_id=? "
                       "AND event_kind IN ('confirm','reschedule_confirm') AND status IN ('pending','failed')", (booking_id,))
            new_id = db.execute(
                "INSERT INTO bookings(client_id,service_id,master_id,room_id,start_utc,end_utc,source,status,"
                "phone_snapshot,service_name_snapshot,master_name_snapshot,prior_booking_id,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,'vk','confirmed',?,?,?,?,?,?)",
                (old["client_id"], old["service_id"], old["master_id"], room_id, stamp(new_start),
                 stamp(end), old["phone_snapshot"], service["name"], service["master_name"],
                 booking_id, stamp(now), stamp(now)),
            ).lastrowid
            db.execute("INSERT INTO booking_history(booking_id,event,actor,details_json,created_at) VALUES (?,?,?,?,?)",
                       (booking_id, "rescheduled", f"vk:{vk_id}", json.dumps({"new_booking_id": new_id}), stamp(now)))
            db.execute("INSERT INTO booking_history(booking_id,event,actor,details_json,created_at) VALUES (?,?,?,?,?)",
                       (new_id, "confirmed", f"vk:{vk_id}", json.dumps({"prior_booking_id": booking_id}), stamp(now)))
            db.execute("INSERT INTO message_outbox(booking_id,recipient_vk_id,event_kind,status,created_at) "
                       "VALUES (?,?, 'rescheduled','pending',?)", (new_id, vk_id, stamp(now)))
            _remember(db, action_key, "reschedule", fp, new_id, now)
            result = dict(db.execute("SELECT * FROM bookings WHERE id=?", (new_id,)).fetchone())
            db.commit()
            return result
        except Exception:
            db.rollback()
            raise
