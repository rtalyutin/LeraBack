"""M3 application service used by the admin HTTP boundary and tests."""

from __future__ import annotations

import json
from contextlib import closing
from datetime import date, datetime, timedelta, timezone

from booking_core import (
    BookingConflict, _candidate, _check_phone, _fingerprint, _remember, _replay, _working,
    connect, parse, stamp, validate_policy,
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
        catalog = {
            "services": [dict(r) for r in db.execute("SELECT * FROM services ORDER BY name")],
            "masters": [dict(r) for r in db.execute("SELECT * FROM masters ORDER BY name")],
            "rooms": [dict(r) for r in db.execute("SELECT * FROM rooms ORDER BY name")],
            "weekly_schedule": [dict(r) for r in db.execute(
                "SELECT id,master_id,room_id,weekday,start_minute,end_minute FROM work_intervals "
                "WHERE local_date IS NULL AND mode='open' ORDER BY weekday,id")],
        }
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


def update_service(path, service_id, name, duration_minutes, active, actor, acknowledge=False, now=None):
    now = now or datetime.now(UTC)
    if not name or not 30 <= duration_minutes <= 60:
        raise ValueError("Service name and 30–60 minute duration required")
    with closing(connect(path)) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            old = db.execute("SELECT * FROM services WHERE id=?", (service_id,)).fetchone()
            if old is None:
                raise LookupError("Service not found")
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
                           start_minute, end_minute, actor, acknowledge=False, now=None):
    """Replace one resource's weekly opening hours; None/None marks the day closed."""
    now = now or datetime.now(UTC)
    zone = validate_policy(policy)
    if resource_kind not in {"master", "room"} or type(weekday) is not int or not 0 <= weekday <= 6:
        raise ValueError("Invalid resource kind or weekday")
    if (start_minute is None) != (end_minute is None):
        raise ValueError("Both times must be provided or both omitted")
    if start_minute is not None and (type(start_minute) is not int or type(end_minute) is not int or
                                     not 0 <= start_minute < end_minute <= 1440):
        raise ValueError("Invalid work interval")
    column = "master_id" if resource_kind == "master" else "room_id"
    table = "masters" if resource_kind == "master" else "rooms"
    with closing(connect(path)) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            if not db.execute(f"SELECT 1 FROM {table} WHERE id=?", (resource_id,)).fetchone():
                raise LookupError("Resource not found")
            db.execute(f"DELETE FROM work_intervals WHERE {column}=? AND weekday=? AND local_date IS NULL AND mode='open'",
                       (resource_id, weekday))
            if start_minute is not None:
                db.execute(
                    f"INSERT INTO work_intervals({column},weekday,mode,start_minute,end_minute) "
                    "VALUES (?,?,'open',?,?)", (resource_id, weekday, start_minute, end_minute),
                )
            upcoming = db.execute(
                f"SELECT id,start_utc,end_utc FROM bookings WHERE {column}=? AND status='confirmed' AND start_utc>=?",
                (resource_id, stamp(now)),
            ).fetchall()
            affected = [r["id"] for r in upcoming
                        if parse(r["start_utc"]).astimezone(zone).weekday() == weekday
                        and not _working(db, resource_kind, resource_id, parse(r["start_utc"]),
                                         parse(r["end_utc"]), zone)]
            if affected and not acknowledge:
                raise AdminConflict("Schedule change affects future confirmed bookings", affected)
            manual_contact = _cancel_affected(db, affected, actor, "weekly_schedule_changed", now)
            _admin_audit(db, actor, "weekly_schedule_updated", resource_kind, resource_id,
                         {"weekday": weekday, "start_minute": start_minute, "end_minute": end_minute,
                          "cancelled_booking_ids": affected}, now)
            db.commit()
            return {"resource_kind": resource_kind, "resource_id": resource_id, "weekday": weekday,
                    "cancelled_booking_ids": affected, "manual_contact_booking_ids": manual_contact}
        except Exception:
            db.rollback()
            raise
