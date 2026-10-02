"""Run only against a new, disposable PostgreSQL database named *_test."""

import json
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from admin_auth import create_or_update_admin, login
from booking_core import BookingConflict, confirm_booking, connect
from seed_starter import seed
from pg_store import SalonScope
from vk_gateway import deliver_pending

TEST_URL = os.environ.get("TEST_DATABASE_URL", "")
POLICY = json.loads(Path(__file__).resolve().parents[1].joinpath("starter_data.json").read_text())["policy"]


@unittest.skipUnless(TEST_URL, "TEST_DATABASE_URL absent")
class PostgresIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not urlsplit(TEST_URL).path.removeprefix("/").endswith("_test"):
            raise ValueError("Use a disposable database whose name ends in _test")
        with connect(TEST_URL) as db:
            if db.execute("SELECT to_regclass('public.bookings')").fetchone()[0] is not None:
                raise ValueError("TEST_DATABASE_URL must point to a fresh empty database")
        seed(TEST_URL)

    def test_concurrent_booking_and_outbox(self):
        now = datetime.now(timezone.utc)
        day = (now.astimezone(ZoneInfo("Europe/Moscow")) + timedelta(days=1)).date()
        start = datetime(day.year, day.month, day.day, 10, tzinfo=ZoneInfo("Europe/Moscow"))
        def book(vk_id):
            return confirm_booking(SalonScope(TEST_URL, 1), POLICY, vk_id, f"+79990000{vk_id:03d}", 1, 3,
                                   start, f"concurrent-{vk_id}", now)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda vk_id: self._result(book, vk_id), (101, 102)))
        self.assertEqual(sum(type(x) is dict for x in results), 1, results)
        self.assertEqual(sum(isinstance(x, BookingConflict) for x in results), 1, results)
        booking = next(x for x in results if type(x) is dict)
        with connect(SalonScope(TEST_URL, 1)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM bookings WHERE status='confirmed'").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM message_outbox WHERE booking_id=?",
                                        (booking["id"],)).fetchone()[0], 1)
        self.assertEqual(deliver_pending(SalonScope(TEST_URL, 1), lambda *args: None), 0)  # No VK reply queued by core alone.

    @staticmethod
    def _result(fn, arg):
        try:
            return fn(arg)
        except BookingConflict as exc:
            return exc

    def test_admin_login(self):
        create_or_update_admin(TEST_URL, "salon_test", "a-long-test-password")
        token, identity = login(TEST_URL, "SALON_TEST", "a-long-test-password")
        self.assertTrue(token)
        self.assertEqual(identity.role, "admin")


if __name__ == "__main__":
    unittest.main()
