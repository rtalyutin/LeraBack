"""M5 Callback API adapter and stateful salon dialog without real VK calls."""

from __future__ import annotations

import hashlib
import hmac
import json
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

from booking_core import (
    BookingConflict, _check_phone, cancel_booking, confirm_booking, connect,
    get_available_days, get_available_slots, reschedule_booking, stamp, validate_policy,
)

UTC = timezone.utc
CALENDAR_PAGE_SIZE = 8
WEEKDAYS = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")


def button(label, command, value=None, draft=None):
    payload = {"cmd": command}
    if value is not None:
        payload["value"] = value
    if draft is not None:
        payload["draft"] = draft
    return {"label": label, "payload": payload}


def stable_random_id(key):
    return int.from_bytes(hashlib.sha256(key.encode()).digest()[:4], "big") & 0x7FFFFFFF or 1


class CallbackGateway:
    def __init__(self, db_path, policy, group_id, secret, confirmation_code):
        self.db_path = db_path
        self.policy = policy
        self.zone = validate_policy(policy)
        self.group_id = int(group_id)
        self.secret = str(secret)
        self.confirmation_code = confirmation_code

    def _load(self, vk_id):
        with closing(connect(self.db_path)) as db:
            row = db.execute("SELECT * FROM vk_dialogs WHERE vk_id=?", (vk_id,)).fetchone()
        if not row or datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00")) < datetime.now(UTC):
            return "home", {}
        return row["state"], json.loads(row["draft_json"])

    def _save(self, vk_id, state, draft):
        expires = stamp(datetime.now(UTC) + timedelta(hours=2))
        with closing(connect(self.db_path)) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT INTO vk_dialogs(vk_id,state,draft_json,expires_at) VALUES (?,?,?,?) "
                "ON CONFLICT(salon_id,vk_id) DO UPDATE SET state=excluded.state,draft_json=excluded.draft_json,expires_at=excluded.expires_at",
                (vk_id, state, json.dumps(draft, ensure_ascii=False), expires),
            )
            db.commit()

    def _rows(self, sql, args=()):
        with closing(connect(self.db_path)) as db:
            return [dict(r) for r in db.execute(sql, args)]

    @staticmethod
    def _home():
        return "Что хотите сделать?", [button("Записаться", "book"), button("Мои записи", "my")], None

    @staticmethod
    def _page(value, total):
        # Treat button payloads as untrusted; never use negative slices or bools.
        page = value if type(value) is int else 0
        return max(0, min(page, max(0, (total - 1) // CALENDAR_PAGE_SIZE)))

    @staticmethod
    def _calendar_choices(items, page, command, page_command, draft_id):
        choices = []
        start = page * CALENDAR_PAGE_SIZE
        for index, (label, value) in enumerate(items[start:start + CALENDAR_PAGE_SIZE]):
            choices.append(dict(button(label, command, value, draft_id), row=index // 2))
        nav_row = (len(choices) + 1) // 2
        if page:
            choices.append(dict(button("Раньше", page_command, page - 1, draft_id), row=nav_row))
        if start + CALENDAR_PAGE_SIZE < len(items):
            choices.append(dict(button("Позже", page_command, page + 1, draft_id), row=nav_row))
        return choices, nav_row + 1

    def _show_days(self, vk_id, draft, now, *, reschedule=False, page=0, note=None):
        with closing(connect(self.db_path)) as db:
            days = get_available_days(db, self.policy, draft["service_id"], draft["master_id"], now,
                                      except_id=draft.get("booking_id") if reschedule else None)
        page = self._page(page, len(days))
        for key in ("local_date", "start_utc", "offered_slots", "time_page"):
            draft.pop(key, None)
        # A new view invalidates buttons for an earlier selection, including
        # confirmation. Keep this ID stable from choosing a slot through its
        # atomic confirmation/replay; rotate only when rebuilding the calendar.
        draft["id"] = uuid4().hex
        draft["date_page"] = page
        draft["offered_days"] = days[page * CALENDAR_PAGE_SIZE:(page + 1) * CALENDAR_PAGE_SIZE]
        self._save(vk_id, "reschedule_date" if reschedule else "date", draft)
        if not days:
            text = "В доступном периоде записи свободных дней нет."
            choices = [button("Проверить снова", "days", 0, draft["id"]), button("В меню", "home")]
        else:
            items = []
            for value in days:
                day = date.fromisoformat(value)
                items.append((f"{WEEKDAYS[day.weekday()]}, {day:%d.%m}", value))
            choices, row = self._calendar_choices(items, page, "day", "days", draft["id"])
            choices.append(dict(button("В меню", "home"), row=row))
            text = "Выберите свободный день"
            if len(days) > CALENDAR_PAGE_SIZE:
                text += f" · страница {page + 1}/{(len(days) - 1) // CALENDAR_PAGE_SIZE + 1}"
        return (note + "\n" if note else "") + text, choices, None

    def _show_times(self, vk_id, draft, local_day, now, *, reschedule=False, page=0, note=None):
        with closing(connect(self.db_path)) as db:
            slots = get_available_slots(db, self.policy, draft["service_id"], draft["master_id"], local_day, now,
                                       except_id=draft.get("booking_id") if reschedule else None)
        if not slots:
            return self._show_days(vk_id, draft, now, reschedule=reschedule,
                                   page=draft.get("date_page", 0),
                                   note=note or "На выбранный день свободного времени уже нет.")
        page = self._page(page, len(slots))
        draft.pop("start_utc", None)
        draft["id"] = uuid4().hex
        draft.update(local_date=local_day.isoformat(), time_page=page,
                     offered_slots=slots[page * CALENDAR_PAGE_SIZE:(page + 1) * CALENDAR_PAGE_SIZE])
        self._save(vk_id, "reschedule_time" if reschedule else "time", draft)
        items = [(datetime.fromisoformat(slot.replace("Z", "+00:00")).astimezone(self.zone).strftime("%H:%M"), slot)
                 for slot in slots]
        choices, row = self._calendar_choices(items, page, "time", "times", draft["id"])
        choices.append(dict(button("Другой день", "days", draft.get("date_page", 0), draft["id"]), row=row))
        text = f"Выберите время на {local_day:%d.%m.%Y}"
        if len(slots) > CALENDAR_PAGE_SIZE:
            text += f" · страница {page + 1}/{(len(slots) - 1) // CALENDAR_PAGE_SIZE + 1}"
        return (note + "\n" if note else "") + text, choices, None

    def _process(self, vk_id, text, payload, now):
        state, draft = self._load(vk_id)
        command = payload.get("cmd") if isinstance(payload, dict) else None
        value = payload.get("value") if isinstance(payload, dict) else None
        payload_draft = payload.get("draft") if isinstance(payload, dict) else None
        normalized = text.strip().lower()
        if normalized in {"начать", "старт", "меню"}:
            self._save(vk_id, "home", {})
            return self._home()
        if command and command not in {"book", "my", "home"} and payload_draft != draft.get("id"):
            return "Эта кнопка устарела. Откройте актуальное меню.", [button("В меню", "home")], None
        if command == "home":
            self._save(vk_id, "home", {})
            return self._home()
        if command == "book" or normalized == "записаться":
            draft = {"id": uuid4().hex}
            self._save(vk_id, "service", draft)
            rows = self._rows("SELECT id,name FROM services WHERE active=1 ORDER BY name")
            if not rows:
                self._save(vk_id, "home", {})
                return "Онлайн-запись пока настраивается. Попробуйте позже.", [button("В меню", "home")], None
            return "Выберите услугу", [button(r["name"], "service", r["id"], draft["id"]) for r in rows], None
        if command == "service" and state == "service":
            rows = self._rows("SELECT id,name FROM services WHERE id=? AND active=1", (int(value),))
            if not rows:
                raise BookingConflict("Услуга больше недоступна")
            draft.update(service_id=int(value), service_name=rows[0]["name"])
            self._save(vk_id, "master", draft)
            masters = self._rows(
                "SELECT m.id,m.name FROM masters m JOIN master_services ms ON ms.master_id=m.id "
                "WHERE ms.service_id=? AND m.active=1 ORDER BY m.name", (int(value),)
            )
            if not masters:
                self._save(vk_id, "home", {})
                return "Для этой услуги пока нет доступных мастеров. Попробуйте позже.", [button("В меню", "home")], None
            choices = [dict(button(r["name"], "master", r["id"], draft["id"]),
                            avatar_master_id=r["id"]) for r in masters]
            return "Выберите мастера", choices, None
        if command == "master" and state == "master":
            rows = self._rows(
                "SELECT m.name FROM masters m JOIN master_services ms ON ms.master_id=m.id "
                "WHERE m.id=? AND ms.service_id=? AND m.active=1", (int(value), draft["service_id"])
            )
            if not rows:
                raise BookingConflict("Мастер больше недоступен")
            draft.update(master_id=int(value), master_name=rows[0]["name"])
            return self._show_days(vk_id, draft, now)
        calendar_states = {"date", "time", "phone", "confirm", "reschedule_date", "reschedule_time", "reschedule_confirm"}
        reschedule = state.startswith("reschedule")
        if command == "days" and state in calendar_states:
            return self._show_days(vk_id, draft, now, reschedule=reschedule, page=value)
        if command == "times" and state in {"time", "reschedule_time"}:
            if "local_date" not in draft:
                return self._show_days(vk_id, draft, now, reschedule=reschedule)
            return self._show_times(vk_id, draft, date.fromisoformat(draft["local_date"]), now,
                                    reschedule=reschedule, page=value)
        if state in {"date", "reschedule_date"} and (command == "day" or not command):
            try:
                if command == "day":
                    if not isinstance(value, str) or value not in draft.get("offered_days", []):
                        raise ValueError("Date button not offered")
                    local_day = date.fromisoformat(value)
                else:
                    local_day = datetime.strptime(text.strip(), "%d.%m.%Y").date()
            except (TypeError, ValueError):
                return self._show_days(vk_id, draft, now, reschedule=reschedule,
                                       page=draft.get("date_page", 0), note="Выберите день актуальной кнопкой.")
            return self._show_times(vk_id, draft, local_day, now, reschedule=reschedule)
        if command == "time" and state in {"time", "reschedule_time"}:
            if "local_date" not in draft:
                return self._show_days(vk_id, draft, now, reschedule=reschedule,
                                       note="Выберите свободный день.")
            local_day = date.fromisoformat(draft["local_date"])
            with closing(connect(self.db_path)) as db:
                slots = get_available_slots(db, self.policy, draft["service_id"], draft["master_id"], local_day, now,
                                           except_id=draft.get("booking_id") if reschedule else None)
            if not isinstance(value, str) or value not in draft.get("offered_slots", []) or value not in slots:
                return self._show_times(vk_id, draft, local_day, now, reschedule=reschedule,
                                        page=draft.get("time_page", 0), note="Это время уже недоступно. Выберите другое.")
            draft["start_utc"] = str(value)
            if state == "reschedule_time":
                self._save(vk_id, "reschedule_confirm", draft)
                return "Перенести запись на выбранное время?", [button("Да, перенести", "reschedule_confirm", draft=draft["id"])], None
            self._save(vk_id, "phone", draft)
            return "Введите телефон: 10–15 цифр", [], None
        if state == "phone" and not command:
            _check_phone(text.strip())
            draft["phone"] = text.strip()
            self._save(vk_id, "confirm", draft)
            local = datetime.fromisoformat(draft["start_utc"].replace("Z", "+00:00")).astimezone(self.zone)
            summary = f"Проверьте: {draft['service_name']}, {draft['master_name']}, {local:%d.%m %H:%M}, {draft['phone']}"
            return summary, [button("Подтвердить", "confirm", draft=draft["id"])], None
        if command == "confirm" and state == "confirm":
            try:
                booking = confirm_booking(
                    self.db_path, self.policy, vk_id, draft["phone"], draft["service_id"], draft["master_id"],
                    datetime.fromisoformat(draft["start_utc"].replace("Z", "+00:00")), f"vk-confirm:{draft['id']}", now,
                )
            except BookingConflict:
                local_day = datetime.fromisoformat(draft["start_utc"].replace("Z", "+00:00")).astimezone(self.zone).date()
                return self._show_times(vk_id, draft, local_day, now,
                                        note="Не удалось записаться на это время. Выберите другое окно.")
            self._save(vk_id, "home", {})
            return "Запись подтверждена и сохранена в календаре.", [button("Мои записи", "my")], booking["id"]
        if command == "my" or normalized == "мои записи":
            rows = self._rows(
                "SELECT b.id,b.start_utc,b.service_name_snapshot,b.master_name_snapshot FROM bookings b "
                "JOIN clients c ON c.id=b.client_id WHERE c.vk_id=? AND b.status='confirmed' ORDER BY b.start_utc", (vk_id,)
            )
            if not rows:
                return "Активных записей нет.", [button("Записаться", "book")], None
            draft = {"id": uuid4().hex}
            self._save(vk_id, "my", draft)
            lines, choices = [], []
            for row in rows:
                local = datetime.fromisoformat(row["start_utc"].replace("Z", "+00:00")).astimezone(self.zone)
                lines.append(f"№{row['id']} · {local:%d.%m %H:%M} · {row['service_name_snapshot']} · {row['master_name_snapshot']}")
                choices.extend([button(f"Отменить №{row['id']}", "cancel", row["id"], draft["id"]),
                                button(f"Перенести №{row['id']}", "reschedule", row["id"], draft["id"])])
            return "\n".join(lines), choices, None
        if command == "cancel" and state == "my":
            draft["booking_id"] = int(value)
            self._save(vk_id, "cancel_confirm", draft)
            return f"Точно отменить запись №{value}?", [button("Да, отменить", "cancel_confirm", draft=draft["id"])], None
        if command == "cancel_confirm" and state == "cancel_confirm":
            booking = cancel_booking(self.db_path, self.policy, vk_id, draft["booking_id"], f"vk-cancel:{draft['id']}", now)
            self._save(vk_id, "home", {})
            return "Запись отменена.", [button("В меню", "home")], booking["id"]
        if command == "reschedule" and state == "my":
            rows = self._rows(
                "SELECT b.id,b.service_id,b.master_id,b.service_name_snapshot,b.master_name_snapshot FROM bookings b "
                "JOIN clients c ON c.id=b.client_id WHERE b.id=? AND c.vk_id=? AND b.status='confirmed'", (int(value), vk_id)
            )
            if not rows:
                raise BookingConflict("Запись недоступна")
            draft.update(booking_id=int(value), service_id=rows[0]["service_id"], master_id=rows[0]["master_id"],
                         service_name=rows[0]["service_name_snapshot"], master_name=rows[0]["master_name_snapshot"])
            return self._show_days(vk_id, draft, now, reschedule=True)
        if command == "reschedule_confirm" and state == "reschedule_confirm":
            try:
                booking = reschedule_booking(
                    self.db_path, self.policy, vk_id, draft["booking_id"],
                    datetime.fromisoformat(draft["start_utc"].replace("Z", "+00:00")), f"vk-reschedule:{draft['id']}", now,
                )
            except BookingConflict:
                local_day = datetime.fromisoformat(draft["start_utc"].replace("Z", "+00:00")).astimezone(self.zone).date()
                return self._show_times(vk_id, draft, local_day, now, reschedule=True,
                                        note="Не удалось перенести запись на это время. Выберите другое окно.")
            self._save(vk_id, "home", {})
            return "Запись перенесена. Старое время освобождено.", [button("Мои записи", "my")], booking["id"]
        return self._home()

    def handle(self, event, now=None):
        now = now or datetime.now(UTC)
        supplied_secret = str(event.get("secret") or "")
        if event.get("group_id") != self.group_id or not hmac.compare_digest(supplied_secret, self.secret):
            raise PermissionError("Invalid VK callback source")
        if event.get("type") == "confirmation":
            return self.confirmation_code
        event_id = str(event.get("event_id") or "")
        if not event_id:
            raise ValueError("event_id required")
        with closing(connect(self.db_path)) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status FROM inbound_events WHERE event_id=?", (event_id,)).fetchone()
            if row and row["status"] == "processed":
                db.commit()
                return "ok"
            if not row:
                db.execute("INSERT INTO inbound_events(event_id,status,received_at) VALUES (?,'received',?)", (event_id, stamp(now)))
            db.commit()
        if event.get("type") != "message_new":
            with closing(connect(self.db_path)) as db:
                db.execute("UPDATE inbound_events SET status='processed',processed_at=? WHERE event_id=?", (stamp(now), event_id))
            return "ok"
        message = event.get("object", {}).get("message", {})
        vk_id = int(message.get("from_id", 0))
        if vk_id <= 0:
            raise ValueError("positive from_id required")
        raw_payload = message.get("payload") or {}
        if isinstance(raw_payload, str):
            try:
                raw_payload = json.loads(raw_payload)
            except json.JSONDecodeError:
                raw_payload = {}
        try:
            text, keyboard, booking_id = self._process(vk_id, message.get("text", ""), raw_payload, now)
        except Exception as exc:
            with closing(connect(self.db_path)) as db:
                db.execute("UPDATE inbound_events SET status='failed',result_json=?,processed_at=? WHERE event_id=?",
                           (json.dumps({"error": type(exc).__name__}), stamp(now), event_id))
            raise
        dedupe = f"event:{event_id}:reply"
        with closing(connect(self.db_path)) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT INTO vk_outgoing_messages(inbound_event_id,booking_id,event_kind,recipient_vk_id,text,keyboard_json,dedupe_key,random_id,status,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,'pending',?) ON CONFLICT DO NOTHING",
                (event_id, booking_id, raw_payload.get("cmd") if isinstance(raw_payload, dict) else None,
                 vk_id, text, json.dumps(keyboard, ensure_ascii=False), dedupe, stable_random_id(dedupe), stamp(now)),
            )
            db.execute("UPDATE inbound_events SET status='processed',result_json=?,processed_at=? WHERE event_id=?",
                       (json.dumps({"reply": dedupe}), stamp(now), event_id))
            db.commit()
        return "ok"


def deliver_pending(db_path, sender, limit=50, now=None):
    """Send one message at a time under the booking mutation lock.

    A cancellation waits for an in-flight confirmation and then queues its own
    notice. A queued confirmation for an already cancelled booking is dropped.
    Keep random_id stable across retries after an unknown VK outcome.
    """
    now = now or datetime.now(UTC)
    delivered = 0
    after_id = 0
    for _ in range(max(0, limit)):
        with closing(connect(db_path)) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute(
                    "SELECT * FROM vk_outgoing_messages WHERE status IN ('pending','failed') "
                    "AND id>? ORDER BY CASE WHEN status='pending' THEN 0 ELSE 1 END,attempts,id "
                    "LIMIT 1 FOR UPDATE SKIP LOCKED", (after_id,)
                ).fetchone()
                if row is None:
                    db.commit()
                    break
                after_id = row["id"]
                outbox_kind = {"confirm": "confirmed", "cancel_confirm": "cancelled",
                               "reschedule_confirm": "rescheduled"}.get(row["event_kind"])
                if row["dedupe_key"].startswith("admin-change-cancel:"):
                    outbox_kind = "cancelled"
                if row["booking_id"]:
                    booking = db.execute("SELECT status FROM bookings WHERE id=?", (row["booking_id"],)).fetchone()
                    if booking is None or (booking["status"] != "confirmed" and row["event_kind"] in {"confirm", "reschedule_confirm"}):
                        db.execute("UPDATE vk_outgoing_messages SET status='superseded' WHERE id=?", (row["id"],))
                        db.commit()
                        continue
                try:
                    sender(row["recipient_vk_id"], row["text"], json.loads(row["keyboard_json"]), row["random_id"])
                except Exception as exc:
                    db.execute("UPDATE vk_outgoing_messages SET status='failed',attempts=attempts+1,last_error=? WHERE id=?",
                               (type(exc).__name__, row["id"]))
                    if row["booking_id"] and outbox_kind:
                        db.execute("UPDATE message_outbox SET status='failed',attempts=attempts+1,last_error=? "
                                   "WHERE booking_id=? AND event_kind=? AND status IN ('pending','failed')",
                                   (type(exc).__name__, row["booking_id"], outbox_kind))
                else:
                    db.execute("UPDATE vk_outgoing_messages SET status='sent',attempts=attempts+1,last_error=NULL,sent_at=? WHERE id=?",
                               (stamp(now), row["id"]))
                    if row["booking_id"] and outbox_kind:
                        db.execute("UPDATE message_outbox SET status='sent',attempts=attempts+1,last_error=NULL,sent_at=? "
                                   "WHERE booking_id=? AND event_kind=? AND status IN ('pending','failed')",
                                   (stamp(now), row["booking_id"], outbox_kind))
                    delivered += 1
                db.commit()
            except Exception:
                db.rollback()
                raise
    return delivered
