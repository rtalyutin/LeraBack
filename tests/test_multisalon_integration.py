"""Business/API isolation against a fresh disposable *_test PostgreSQL DB."""
import json
import os
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPCookieProcessor, Request, build_opener
from uuid import uuid4
from zoneinfo import ZoneInfo

import psycopg
from admin_auth import create_or_update_admin
from admin_http import server
from admin_service import create_manual_booking, create_service, save_resource, update_weekly_schedule
from booking_core import BookingConflict, migrate
from constructor_store import get_policy
from pg_store import SalonScope, connect
from salon_service import attach_master, grant_membership, require_salon
from shared_schema import provision_salon
from vk_gateway import CallbackGateway

URL = os.environ.get("MULTISALON_TEST_DATABASE_URL", "")
POLICY = json.loads(Path(__file__).resolve().parents[1].joinpath("starter_data.json").read_text())["policy"]
PASSWORD = "Only-synthetic-multisalon-password"


@unittest.skipUnless(URL, "MULTISALON_TEST_DATABASE_URL absent")
class MultisalonIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not urlsplit(URL).path.endswith("_test"):
            raise ValueError("Use a fresh disposable *_test database")
        with connect(URL) as db:
            if db.execute("SELECT to_regclass('public.salons')").fetchone()[0]:
                raise ValueError("Multisalon test database must be fresh")
            migrate(db)
        cls.owner = create_or_update_admin(URL, "both_salons", PASSWORD)
        cls.single = create_or_update_admin(URL, "one_salon", PASSWORD)
        with connect(URL) as db:
            db.execute("BEGIN IMMEDIATE")
            cls.salon2 = provision_salon(db, "Другой салон", POLICY)
            grant_membership(db, cls.owner, cls.salon2)
            grant_membership(db, cls.single, 1)
            db.commit()
        cls.one, cls.two = SalonScope(URL, 1), SalonScope(URL, cls.salon2)
        cls.now = datetime.now(timezone.utc)
        day = (cls.now.astimezone(ZoneInfo(POLICY["timezone"])) + timedelta(days=1)).date()
        cls.start = datetime(day.year, day.month, day.day, 10, tzinfo=ZoneInfo(POLICY["timezone"]))

    def fixture(self, scope, name=None):
        name = name or uuid4().hex
        sid = create_service(scope, "Услуга "+name, 30, "admin:test")["id"]
        mid = save_resource(scope, "master", None, "Мастер "+name, [sid], True, "admin:test")["id"]
        rid = save_resource(scope, "room", None, "Кабинет "+name, [sid], True, "admin:test")["id"]
        for kind, resource in (("master", mid), ("room", rid)):
            update_weekly_schedule(scope, POLICY, kind, resource, self.start.weekday(), 540, 1080, "admin:test")
        return sid, mid, rid

    def test_equal_catalog_names_and_client_conflicts_are_salon_local(self):
        name = uuid4().hex
        s1, m1, _ = self.fixture(self.one, name)
        s2, m2, _ = self.fixture(self.two, name)
        # The same phone and action key may represent independent visits in different salons.
        key = uuid4().hex
        phone = "+79990101010"
        first = create_manual_booking(self.one, POLICY, phone, s1, m1, self.start, key, "admin:test", self.now)
        second = create_manual_booking(self.two, POLICY, phone, s2, m2, self.start, key, "admin:test", self.now)
        self.assertNotEqual(first["id"], second["id"])
        s3, m3, _ = self.fixture(self.one)
        with self.assertRaises(BookingConflict):
            create_manual_booking(self.one, POLICY, phone, s3, m3, self.start, uuid4().hex, "admin:test", self.now)
        with connect(self.two) as db:
            self.assertIsNone(db.execute("SELECT id FROM bookings WHERE id=?", (first["id"],)).fetchone())
            self.assertEqual(get_policy(db)["timezone"], "Europe/Moscow")

    def test_shared_master_is_busy_across_salons_and_adjacent_slot_is_free(self):
        s1, m1, _ = self.fixture(self.one)
        s2, _, _ = self.fixture(self.two)
        attached = attach_master(self.two, self.owner, 1, m1, [s2], name="Общий "+uuid4().hex)["id"]
        update_weekly_schedule(self.two, POLICY, "master", attached, self.start.weekday(), 540, 1080, "admin:test")
        create_manual_booking(self.one, POLICY, "+79990202020", s1, m1, self.start, uuid4().hex, "admin:test", self.now)
        with self.assertRaises(BookingConflict):
            create_manual_booking(self.two, POLICY, "+79990303030", s2, attached, self.start, uuid4().hex, "admin:test", self.now)
        result = create_manual_booking(self.two, POLICY, "+79990303030", s2, attached,
                                       self.start+timedelta(minutes=30), uuid4().hex, "admin:test", self.now)
        self.assertEqual(result["master_id"], attached)

    def test_http_membership_missing_scope_cross_ids_and_csrf(self):
        s2, m2, _ = self.fixture(self.two)
        with server(URL, POLICY, "x"*40, port=0, secure_cookie=False) as httpd:
            worker = threading.Thread(target=httpd.serve_forever, daemon=True)
            worker.start()
            base = "http://127.0.0.1:"+str(httpd.server_port)
            browser = build_opener(HTTPCookieProcessor(CookieJar()))
            def call(path, body=None, scope=None, csrf=None):
                headers = {"Content-Type": "application/json"}
                if scope is not None: headers["X-Salon-Id"] = str(scope)
                if csrf is not None: headers["X-CSRF-Token"] = csrf
                request = Request(base+path, data=None if body is None else json.dumps(body).encode(), headers=headers)
                try:
                    with browser.open(request, timeout=20) as response:
                        return response.status, json.load(response)
                except HTTPError as error:
                    return error.code, json.load(error)
            try:
                status, session = call("/api/login", {"username": "one_salon", "password": PASSWORD})
                self.assertEqual(status, 200)
                self.assertEqual([s["id"] for s in session["salons"]], [1])
                self.assertEqual(call("/api/snapshot")[0], 422)
                self.assertEqual(call("/api/constructor", scope=self.salon2)[0], 403)
                self.assertEqual(call("/api/masters/attach", {"source_salon_id": self.salon2,
                    "master_id": m2, "service_ids": []}, scope=1, csrf=session["csrf_token"])[0], 403)
                self.assertEqual(call("/api/services", {"name": "CSRF", "duration_minutes": 30}, scope=1)[0], 403)
                self.assertEqual(call("/api/services/"+str(s2), {"name": "foreign", "duration_minutes": 30,
                    "active": True}, scope=1, csrf=session["csrf_token"])[0], 404)
                status, payload = call("/api/constructor", scope=1)
                self.assertEqual(status, 200)
                self.assertTrue(payload["types"])
                with connect(URL) as db:
                    grant_membership(db, self.single, 1, False)
                self.assertEqual(call("/api/snapshot", scope=1)[0], 403)
            finally:
                with connect(URL) as db:
                    grant_membership(db, self.single, 1)
                httpd.shutdown()
                worker.join(timeout=10)

    def test_vk_state_and_event_ids_are_namespaced(self):
        one = CallbackGateway(self.one, POLICY, 1001, "synthetic1", "confirm1")
        two = CallbackGateway(self.two, POLICY, 1002, "synthetic2", "confirm2")
        one._save(9001, "service", {"id": "first"})
        two._save(9001, "master", {"id": "second"})
        self.assertEqual(one._load(9001), ("service", {"id": "first"}))
        self.assertEqual(two._load(9001), ("master", {"id": "second"}))
        self.assertEqual(one.handle({"type": "confirmation", "group_id": 1001, "secret": "synthetic1"}), "confirm1")
        with self.assertRaises(PermissionError):
            one.handle({"type": "confirmation", "group_id": 1002, "secret": "synthetic1"})


if __name__ == "__main__":
    unittest.main()
