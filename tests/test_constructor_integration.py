"""Constructor acceptance against a fresh disposable PostgreSQL database."""
import json
import os
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from admin_service import (AdminConflict, create_manual_booking, create_service,
                           save_resource, snapshot, update_service, update_weekly_schedule)
from booking_core import connect, get_available_slots, migrate
from pg_store import SalonScope

RAW_URL = os.environ.get("CONSTRUCTOR_TEST_DATABASE_URL", "")
URL = SalonScope(RAW_URL, 1) if RAW_URL else None
POLICY = json.loads(Path(__file__).resolve().parents[1].joinpath(".env.example").read_text().split("SALON_POLICY_JSON=", 1)[1].splitlines()[0])
UTC = timezone.utc


@unittest.skipUnless(URL, "CONSTRUCTOR_TEST_DATABASE_URL absent")
class ConstructorIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not urlsplit(RAW_URL).path.endswith("_test"):
            raise ValueError("Use a fresh disposable *_test database")
        with closing(connect(RAW_URL)) as db:
            if db.execute("SELECT to_regclass('__APP_SCHEMA__.services')").fetchone()[0] is not None:
                raise ValueError("Constructor test database must be fresh")
            migrate(db)
        cls.now = datetime.now(UTC)
        from zoneinfo import ZoneInfo
        cls.zone = ZoneInfo(POLICY["timezone"])
        cls.day = (cls.now.astimezone(cls.zone) + timedelta(days=1)).date()
        cls.start = datetime(cls.day.year, cls.day.month, cls.day.day, 10, tzinfo=cls.zone)
        cls.actor = "admin:test"

    def fixture(self, duration=90):
        token = uuid4().hex[:10]
        sid = create_service(URL, "Услуга "+token, duration, self.actor)["id"]
        mid = save_resource(URL, "master", None, "Мастер "+token, [sid], True, self.actor)["id"]
        rid = save_resource(URL, "room", None, "Кабинет "+token, [sid], True, self.actor)["id"]
        for kind, resource_id in (("master", mid), ("room", rid)):
            update_weekly_schedule(URL, POLICY, kind, resource_id, self.day.weekday(),
                                   None, None, self.actor, intervals=[
                                       {"start_minute": 600, "end_minute": 720},
                                       {"start_minute": 780, "end_minute": 1140}])
        return sid, mid, rid

    def test_01_empty_schema(self):
        state = snapshot(URL, POLICY, self.day)
        for table in ("services", "masters", "rooms", "weekly_schedule"):
            self.assertEqual(state[table], [])

    def test_constructor_links_duration_and_split_hours(self):
        sid, mid, rid = self.fixture()
        state = snapshot(URL, POLICY, self.day)
        self.assertEqual(next(m for m in state["masters"] if m["id"] == mid)["service_ids"], [sid])
        self.assertEqual(next(r for r in state["rooms"] if r["id"] == rid)["service_ids"], [sid])
        with closing(connect(URL)) as db:
            slots = get_available_slots(db, POLICY, sid, mid, self.day, self.now)
        self.assertIn(self.start.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"), slots)
        for hour in (11, 12):
            start = self.start.replace(hour=hour)
            self.assertNotIn(start.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"), slots)
        booking = create_manual_booking(URL, POLICY, "+79990000011", sid, mid, self.start,
                                        uuid4().hex, self.actor, self.now)
        self.assertEqual(booking["room_id"], rid)
        self.assertEqual((datetime.fromisoformat(booking["end_utc"].replace("Z", "+00:00")) -
                          datetime.fromisoformat(booking["start_utc"].replace("Z", "+00:00"))).total_seconds(), 5400)

    def test_resource_changes_require_confirmation_and_rollback(self):
        sid, mid, rid = self.fixture()
        booking = create_manual_booking(URL, POLICY, "+79990000012", sid, mid, self.start,
                                        uuid4().hex, self.actor, self.now)
        with self.assertRaises(AdminConflict):
            save_resource(URL, "master", mid, "Снято", [], True, self.actor, now=self.now)
        with closing(connect(URL)) as db:
            self.assertEqual(db.execute("SELECT service_id FROM master_services WHERE master_id=?", (mid,)).fetchone()[0], sid)
            self.assertEqual(db.execute("SELECT status FROM bookings WHERE id=?", (booking["id"],)).fetchone()[0], "confirmed")
        result = save_resource(URL, "master", mid, "Снято "+uuid4().hex, [], True,
                               self.actor, acknowledge=True, now=self.now)
        self.assertEqual(result["cancelled_booking_ids"], [booking["id"]])
        with closing(connect(URL)) as db:
            self.assertEqual(db.execute("SELECT status FROM bookings WHERE id=?", (booking["id"],)).fetchone()[0], "cancelled")

    def test_schedule_rollback_conflict_and_day_off(self):
        sid, mid, rid = self.fixture()
        booking = create_manual_booking(URL, POLICY, "+79990000013", sid, mid, self.start,
                                        uuid4().hex, self.actor, self.now)
        with self.assertRaises(AdminConflict):
            update_weekly_schedule(URL, POLICY, "room", rid, self.day.weekday(),
                                   None, None, self.actor, intervals=[], now=self.now)
        with closing(connect(URL)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM work_intervals WHERE room_id=?", (rid,)).fetchone()[0], 2)
        result = update_weekly_schedule(URL, POLICY, "room", rid, self.day.weekday(),
                                       None, None, self.actor, acknowledge=True,
                                       intervals=[], now=self.now)
        self.assertEqual(result["cancelled_booking_ids"], [booking["id"]])

    def test_validation_and_preserving_migration(self):
        sid, mid, rid = self.fixture(120)
        name = "Проверка Кириллицы " + uuid4().hex
        create_service(URL, name, 45, self.actor)
        with self.assertRaises(ValueError):
            create_service(URL, name.upper(), 45, self.actor)
        for duration in (0, 1441, True, "30"):
            with self.assertRaises(ValueError):
                create_service(URL, "Некорректная", duration, self.actor)
        with self.assertRaises(ValueError):
            update_weekly_schedule(URL, POLICY, "master", mid, 0, None, None, self.actor,
                                   intervals=[{"start_minute": 600, "end_minute": 720},
                                              {"start_minute": 700, "end_minute": 800}])
        with self.assertRaises(ValueError):
            save_resource(URL, "room", rid, "Ошибка", [99999999], True, self.actor)
        with closing(connect(URL)) as db:
            before = [dict(r) for r in db.execute("SELECT * FROM services ORDER BY id")]
            with closing(connect(RAW_URL)) as migration_db:
                migrate(migration_db)
                migrate(migration_db)
            after = [dict(r) for r in db.execute("SELECT * FROM services ORDER BY id")]
            self.assertEqual(before, after)
            self.assertEqual(db.execute("SELECT max(version) FROM schema_migrations").fetchone()[0], 4)
        update_service(URL, sid, "Длинный приём "+uuid4().hex, 150, True, self.actor)
