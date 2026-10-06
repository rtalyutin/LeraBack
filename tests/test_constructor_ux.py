"""Constructor schedule/preview contracts; integration requires a disposable DB."""
import json
import os
import threading
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from http.cookiejar import CookieJar
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPCookieProcessor, Request, build_opener
from uuid import uuid4
from zoneinfo import ZoneInfo

from admin_auth import create_or_update_admin
from admin_http import server
from admin_service import (AdminConflict, availability_preview, create_block,
    create_manual_booking, create_service, normalize_weekly_draft, save_resource,
    update_weekly_schedule_batch)
from booking_core import connect, get_available_slots, migrate, stamp
from pg_store import SalonScope
from salon_service import grant_membership
from shared_schema import provision_salon

RAW_URL = os.environ.get("CONSTRUCTOR_UX_TEST_DATABASE_URL", "")
POLICY = json.loads(Path(__file__).resolve().parents[1].joinpath("starter_data.json").read_text())["policy"]
UTC = timezone.utc
PASSWORD = "Only-synthetic-ux-test-password"


class ConstructorInputValidation(unittest.TestCase):
    def test_invalid_drafts_fail_before_database_access(self):
        valid = {"resource_kind": "master", "resource_id": 1,
                 "days": [{"weekday": 0, "intervals": [{"start_minute": 540, "end_minute": 720}]}]}
        invalid = [
            {"resource_kind": "client"}, {"resource_kind": []}, {"resource_id": True},
            {"resource_id": "1"}, {"resource_id": 0}, {"resource_id": 2**63},
            {"days": []}, {"days": {}}, {"days": [None]},
            {"days": [{"weekday": True, "intervals": []}]},
            {"days": [{"weekday": 7, "intervals": []}]},
            {"days": [{"weekday": 0, "intervals": []}]*2},
            {"days": [{"weekday": 0}]},
            {"days": [{"weekday": 0, "intervals": [None]}]},
            {"days": [{"weekday": 0, "intervals": [{"start_minute": True, "end_minute": 720}]}]},
            {"days": [{"weekday": 0, "intervals": [{"start_minute": 600, "end_minute": 600}]}]},
            {"days": [{"weekday": 0, "intervals": [{"start_minute": 0, "end_minute": 1441}]}]},
            {"days": [{"weekday": 0, "intervals": [{"start_minute": 540, "end_minute": 720},
                                                      {"start_minute": 700, "end_minute": 800}]}]},
        ]
        with patch("admin_service.connect") as db:
            for changed in invalid:
                data = {**valid, **changed}
                with self.subTest(changed=changed), self.assertRaises(ValueError):
                    update_weekly_schedule_batch("unused", POLICY, **data, actor="admin:test")
            db.assert_not_called()

    def test_preview_rejects_coercion_dates_and_bad_drafts(self):
        now = datetime(2026, 1, 1, tzinfo=UTC)
        with patch("admin_service.connect") as db:
            for service, master, day, draft in [
                (True, 1, "2026-01-02", None), (1, "1", "2026-01-02", None),
                (1, 1, "20260102", None), (1, 1, "2026-W01-5", None),
                (1, 1, "2026-02-30", None), (1, 1, None, None),
                (1, 1, "2026-01-02", []), (1, 1, "2026-01-02", {}),
            ]:
                with self.subTest(service=service, master=master, day=day, draft=draft), self.assertRaises(ValueError):
                    availability_preview("unused", POLICY, service, master, day, now, draft)
            db.assert_not_called()

    def test_preview_rejects_duplicate_and_oversized_multi_drafts(self):
        now = datetime(2026, 1, 1, tzinfo=UTC)
        draft = {"resource_kind": "master", "resource_id": 1,
                 "days": [{"weekday": 0, "intervals": []}]}
        with patch("admin_service.connect") as db:
            for options in ({"draft": draft, "drafts": []}, {"drafts": {}},
                            {"drafts": [draft, draft]}, {"drafts": [draft]*101}):
                with self.subTest(options=options), self.assertRaises(ValueError):
                    availability_preview("unused", POLICY, 1, 1, "2026-01-02", now, **options)
            db.assert_not_called()


@unittest.skipUnless(RAW_URL, "CONSTRUCTOR_UX_TEST_DATABASE_URL absent")
class ConstructorUXIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not urlsplit(RAW_URL).path.endswith("_test"):
            raise ValueError("Use a fresh disposable *_test database")
        with closing(connect(RAW_URL)) as db:
            if db.execute("SELECT to_regclass('__APP_SCHEMA__.services')").fetchone()[0]:
                raise ValueError("Constructor UX test database must be fresh")
            migrate(db)
        cls.scope = SalonScope(RAW_URL, 1)
        cls.zone = ZoneInfo(POLICY["timezone"])
        cls.now = datetime.now(UTC)
        cls.day = (cls.now.astimezone(cls.zone) + timedelta(days=1)).date()
        cls.start = datetime(cls.day.year, cls.day.month, cls.day.day, 10, tzinfo=cls.zone)
        cls.actor = "admin:ux-test"
        cls.admin = create_or_update_admin(RAW_URL, "ux_test", PASSWORD)
        with closing(connect(RAW_URL)) as db:
            db.execute("BEGIN IMMEDIATE")
            cls.foreign_salon = provision_salon(db, "Чужой салон", POLICY)
            grant_membership(db, cls.admin, 1)
            db.commit()

    def fixture(self, scope=None):
        scope = scope or self.scope
        name = uuid4().hex
        sid = create_service(scope, "Услуга "+name, 60, self.actor)["id"]
        mid = save_resource(scope, "master", None, "Мастер "+name, [sid], True, self.actor)["id"]
        rid = save_resource(scope, "room", None, "Кабинет "+name, [sid], True, self.actor)["id"]
        for kind, resource in (("master", mid), ("room", rid)):
            update_weekly_schedule_batch(scope, POLICY, kind, resource,
                [{"weekday": self.day.weekday(), "intervals": [
                    {"start_minute": 540, "end_minute": 780}, {"start_minute": 840, "end_minute": 1080}]}], self.actor)
        return sid, mid, rid

    def stored(self):
        tables = ("work_intervals", "bookings", "clients", "booking_history", "message_outbox",
                  "vk_outgoing_messages", "admin_audit_log", "action_results")
        with closing(connect(self.scope)) as db:
            return {table: [dict(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY 1")]
                    for table in tables}

    def preview(self, sid, mid, draft=None, policy=None, day=None):
        return availability_preview(self.scope, policy or POLICY, sid, mid,
                                    (day or self.day).isoformat(), self.now, draft)

    def test_batch_saves_all_selected_days_and_keeps_unselected_days(self):
        _, mid, _ = self.fixture()
        other = (self.day.weekday()+1) % 7
        result = update_weekly_schedule_batch(self.scope, POLICY, "master", mid, [
            {"weekday": other, "intervals": []},
            {"weekday": self.day.weekday(), "intervals": [{"start_minute": 600, "end_minute": 660},
                {"start_minute": 660, "end_minute": 720}]}], self.actor)
        self.assertEqual(result["cancelled_booking_ids"], [])
        with closing(connect(self.scope)) as db:
            rows = [dict(r) for r in db.execute("SELECT weekday,start_minute,end_minute FROM work_intervals WHERE master_id=? ORDER BY weekday,start_minute", (mid,))]
        self.assertEqual(rows, [{"weekday": self.day.weekday(), "start_minute": 600, "end_minute": 720}])
        second = (other+1) % 7
        update_weekly_schedule_batch(self.scope, POLICY, "master", mid,
            [{"weekday": second, "intervals": [{"start_minute": 540, "end_minute": 600}]}], self.actor)
        with closing(connect(self.scope)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM work_intervals WHERE master_id=?", (mid,)).fetchone()[0], 2)

    def test_batch_conflict_is_atomic_and_never_cancels(self):
        sid, mid, rid = self.fixture()
        booking = create_manual_booking(self.scope, POLICY, "+79990102030", sid, mid, self.start,
                                        uuid4().hex, self.actor, self.now)
        before = self.stored()
        for kind, resource in (("master", mid), ("room", rid)):
            with self.subTest(kind=kind), self.assertRaises(AdminConflict) as caught:
                update_weekly_schedule_batch(self.scope, POLICY, kind, resource, [
                    {"weekday": (self.day.weekday()+1)%7, "intervals": [{"start_minute": 60, "end_minute": 90}]},
                    {"weekday": self.day.weekday(), "intervals": []}], self.actor, self.now)
            self.assertEqual(caught.exception.affected_booking_ids, [booking["id"]])
            self.assertEqual(self.stored(), before)

    def test_batch_database_failure_after_first_day_rolls_everything_back(self):
        _, mid, _ = self.fixture()
        update_weekly_schedule_batch(self.scope, POLICY, "master", mid, [
            {"weekday": day, "intervals": [{"start_minute": 540, "end_minute": 780}]} for day in (0, 1)], self.actor)
        before = self.stored()
        with closing(connect(self.scope)) as db:
            db.execute("""CREATE FUNCTION __APP_SCHEMA__.ux_test_reject_second_day() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN RAISE EXCEPTION 'synthetic failure after first selected day'; END $$""")
            db.execute("""CREATE TRIGGER ux_test_reject_second_day BEFORE DELETE ON __APP_SCHEMA__.work_intervals
                FOR EACH ROW WHEN (OLD.weekday=1) EXECUTE FUNCTION __APP_SCHEMA__.ux_test_reject_second_day()""")
        try:
            with self.assertRaises(Exception) as caught:
                update_weekly_schedule_batch(self.scope, POLICY, "master", mid, [
                    {"weekday": 0, "intervals": [{"start_minute": 600, "end_minute": 660}]},
                    {"weekday": 1, "intervals": [{"start_minute": 700, "end_minute": 760}]}], self.actor)
            self.assertIn("synthetic failure after first selected day", str(caught.exception))
            self.assertEqual(self.stored(), before)
        finally:
            with closing(connect(self.scope)) as db:
                db.execute("DROP TRIGGER ux_test_reject_second_day ON __APP_SCHEMA__.work_intervals")
                db.execute("DROP FUNCTION __APP_SCHEMA__.ux_test_reject_second_day()")

    def test_batch_cannot_invalidate_a_visit_already_in_progress(self):
        sid, mid, _ = self.fixture()
        booking = create_manual_booking(self.scope, POLICY, "+79990102033", sid, mid,
                                        self.start, uuid4().hex, self.actor, self.now)
        before = self.stored()
        with self.assertRaises(AdminConflict) as caught:
            update_weekly_schedule_batch(self.scope, POLICY, "master", mid,
                [{"weekday": self.day.weekday(), "intervals": []}], self.actor,
                self.start+timedelta(minutes=30))
        self.assertEqual(caught.exception.affected_booking_ids, [booking["id"]])
        self.assertEqual(self.stored(), before)

    def test_saved_preview_uses_booking_engine_and_does_not_write(self):
        sid, mid, _ = self.fixture()
        before = self.stored()
        result = self.preview(sid, mid)
        with closing(connect(self.scope)) as db:
            expected = get_available_slots(db, POLICY, sid, mid, self.day, self.now)
        self.assertEqual(result["slots"], expected)
        self.assertIn(stamp(self.start), result["slots"])
        self.assertNotIn(stamp(self.start.replace(hour=13)), result["slots"])
        self.assertFalse(result["draft_applied"])
        self.assertEqual(result["timezone"], POLICY["timezone"])
        self.assertEqual(self.stored(), before)

    def test_preview_overlay_and_occupied_windows_do_not_write(self):
        sid, mid, rid = self.fixture()
        create_manual_booking(self.scope, POLICY, "+79990102031", sid, mid, self.start,
                              uuid4().hex, self.actor, self.now)
        create_block(self.scope, "room", rid, self.start.replace(hour=11), self.start.replace(hour=12),
                     "Тестовый перерыв", self.actor, now=self.now)
        before = self.stored()
        draft = {"resource_kind": "master", "resource_id": mid, "days": [{"weekday": self.day.weekday(),
                 "intervals": [{"start_minute": 600, "end_minute": 780}]}]}
        result = self.preview(sid, mid, draft)
        self.assertTrue(result["draft_applied"])
        self.assertNotIn(stamp(self.start), result["slots"])
        self.assertNotIn(stamp(self.start.replace(hour=11)), result["slots"])
        self.assertIn(stamp(self.start.replace(hour=12)), result["slots"])
        self.assertNotIn(stamp(self.start.replace(hour=14)), result["slots"])
        draft["resource_kind"], draft["resource_id"] = "room", rid
        draft["days"][0]["intervals"] = []
        result = self.preview(sid, mid, draft)
        self.assertEqual(result["slots"], [])
        self.assertTrue(result["empty_message"])
        self.assertEqual(self.stored(), before)

    def test_preview_preserves_dated_overrides_and_closed_intervals(self):
        sid, mid, _ = self.fixture()
        with closing(connect(self.scope)) as db:
            db.execute("INSERT INTO work_intervals(master_id,local_date,mode,start_minute,end_minute) VALUES (?,?,'open',840,1020)", (mid, self.day.isoformat()))
            db.execute("INSERT INTO work_intervals(master_id,weekday,mode,start_minute,end_minute) VALUES (?,?,'closed',900,960)", (mid, self.day.weekday()))
        before = self.stored()
        draft = {"resource_kind": "master", "resource_id": mid, "days": [{"weekday": self.day.weekday(), "intervals": []}]}
        result = self.preview(sid, mid, draft)
        self.assertIn(stamp(self.start.replace(hour=14)), result["slots"])
        self.assertNotIn(stamp(self.start.replace(hour=15)), result["slots"])
        self.assertIn(stamp(self.start.replace(hour=16)), result["slots"])
        self.assertEqual(self.stored(), before)

    def test_preview_applies_master_and_room_drafts_together(self):
        sid, mid, rid = self.fixture()
        before = self.stored()
        drafts = [
            {"resource_kind": "master", "resource_id": mid, "days": [{"weekday": self.day.weekday(),
                "intervals": [{"start_minute": 600, "end_minute": 780}]}]},
            {"resource_kind": "room", "resource_id": rid, "days": [{"weekday": self.day.weekday(),
                "intervals": [{"start_minute": 720, "end_minute": 900}]}]},
        ]
        result = availability_preview(self.scope, POLICY, sid, mid, self.day.isoformat(), self.now, drafts=drafts)
        self.assertEqual(result["slots"], [stamp(self.start.replace(hour=12))])
        self.assertTrue(result["draft_applied"])
        saved = availability_preview(self.scope, POLICY, sid, mid, self.day.isoformat(), self.now, drafts=[])
        self.assertFalse(saved["draft_applied"])
        self.assertIn(stamp(self.start), saved["slots"])
        self.assertEqual(self.stored(), before)

    def test_preview_checks_service_links_and_active_flags(self):
        sid, mid, rid = self.fixture()
        sid2 = create_service(self.scope, "Несвязанная "+uuid4().hex, 30, self.actor)["id"]
        self.assertEqual(self.preview(sid2, mid)["slots"], [])
        for table, object_id in (("services", sid), ("masters", mid), ("rooms", rid)):
            with closing(connect(self.scope)) as db:
                db.execute(f"UPDATE {table} SET active=0 WHERE id=?", (object_id,))
            self.assertEqual(self.preview(sid, mid)["slots"], [], table)
            with closing(connect(self.scope)) as db:
                db.execute(f"UPDATE {table} SET active=1 WHERE id=?", (object_id,))

    def test_preview_horizon_notice_and_timezone(self):
        sid, mid, _ = self.fixture()
        result = self.preview(sid, mid, day=self.day+timedelta(days=POLICY["booking_horizon_days"]+1))
        self.assertEqual(result["slots"], [])
        self.assertIn("пределами", result["empty_message"])
        policy = {**POLICY, "min_notice_minutes": 7*1440}
        self.assertEqual(self.preview(sid, mid, policy=policy)["slots"], [])
        self.assertEqual(self.preview(sid, mid)["date"], self.day.isoformat())

    def test_foreign_resources_are_not_visible(self):
        s2, m2, r2 = self.fixture(SalonScope(RAW_URL, self.foreign_salon))
        sid, mid, _ = self.fixture()
        before = self.stored()
        with self.assertRaises(LookupError):
            update_weekly_schedule_batch(self.scope, POLICY, "room", r2, [{"weekday": 0, "intervals": []}], self.actor)
        with self.assertRaises(LookupError):
            self.preview(s2, m2)
        with self.assertRaises(LookupError):
            self.preview(sid, mid, {"resource_kind": "master", "resource_id": m2,
                                  "days": [{"weekday": 0, "intervals": []}]})
        self.assertEqual(self.stored(), before)

    def test_http_session_csrf_scope_validation_and_impact(self):
        sid, mid, _ = self.fixture()
        booking = create_manual_booking(self.scope, POLICY, "+79990102032", sid, mid,
                                        self.start, uuid4().hex, self.actor, self.now)
        before = self.stored()
        with server(RAW_URL, POLICY, port=0, secure_cookie=False, clock=lambda: self.now) as httpd:
            worker = threading.Thread(target=httpd.serve_forever, daemon=True)
            worker.start()
            base = "http://127.0.0.1:"+str(httpd.server_port)
            browser = build_opener(HTTPCookieProcessor(CookieJar()))
            def call(path, body, scope=1, csrf=None):
                headers = {"Content-Type": "application/json"}
                if scope is not None: headers["X-Salon-Id"] = str(scope)
                if csrf is not None: headers["X-CSRF-Token"] = csrf
                try:
                    with browser.open(Request(base+path, data=json.dumps(body).encode(), headers=headers), timeout=20) as response:
                        return response.status, json.load(response)
                except HTTPError as error:
                    return error.code, json.load(error)
            preview = {"service_id": sid, "master_id": mid, "date": self.day.isoformat()}
            batch = {"resource_kind": "master", "resource_id": mid,
                     "days": [{"weekday": self.day.weekday(), "intervals": []}], "acknowledge": True}
            try:
                self.assertEqual(call("/api/availability-preview", preview)[0], 401)
                status, session = call("/api/login", {"username": "ux_test", "password": PASSWORD})
                self.assertEqual(status, 200)
                csrf = session["csrf_token"]
                for path, body in (("/api/availability-preview", preview), ("/api/weekly-schedule/batch", batch)):
                    self.assertEqual(call(path, body)[0], 403)
                    self.assertEqual(call(path, body, csrf=csrf, scope=None)[0], 422)
                    self.assertEqual(call(path, body, csrf=csrf, scope=self.foreign_salon)[0], 403)
                status, impact = call("/api/weekly-schedule/batch", batch, csrf=csrf)
                self.assertEqual(status, 409)
                self.assertEqual(impact["affected_booking_ids"], [booking["id"]])
                self.assertEqual(call("/api/availability-preview", {**preview, "service_id": True}, csrf=csrf)[0], 422)
                self.assertEqual(call("/api/availability-preview", preview, csrf=csrf)[0], 200)
                self.assertEqual(self.stored(), before)
            finally:
                httpd.shutdown()
                worker.join(timeout=10)


if __name__ == "__main__":
    unittest.main()
