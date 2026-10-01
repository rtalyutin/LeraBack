"""Startup behavior. SQLite exercises auth units; it does not verify the PG adapter.

STARTUP_TEST_DATABASE_URL enables the separate fresh PostgreSQL acceptance test.
"""

import json
import os
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import date
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit
from urllib.request import urlopen

import admin_auth
import admin_http
import app
import startup
from admin_service import create_service, snapshot
from booking_core import connect

AUTH_SCHEMA = """
CREATE TABLE IF NOT EXISTS admin_users (
 id INTEGER PRIMARY KEY, username TEXT UNIQUE, password_salt BLOB,
 password_hash BLOB, password_iterations INTEGER, role TEXT, active INTEGER,
 created_at TEXT, password_changed_at TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_admin ON admin_users(active) WHERE active=1;
CREATE TABLE IF NOT EXISTS admin_sessions (
 id INTEGER PRIMARY KEY, admin_user_id INTEGER, token_hash BLOB,
 created_at TEXT, last_seen_at TEXT, expires_at TEXT);
CREATE TABLE IF NOT EXISTS admin_audit_log (
 id INTEGER PRIMARY KEY, actor TEXT, action TEXT, object_type TEXT,
 object_id TEXT, details_json TEXT, created_at TEXT);
"""
PASSWORD = "Synthetic-startup-password-only"
POLICY = json.loads(Path(__file__).resolve().parents[1].joinpath("starter_data.json").read_text())["policy"]


def auth_unit_connect(path):
    db = sqlite3.connect(path, isolation_level=None, timeout=10)
    db.row_factory = sqlite3.Row
    return db


class StartupAuthUnits(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "auth.sqlite")
        self.addCleanup(self.tmp.cleanup)
        self.patches = [patch.object(startup, "connect", auth_unit_connect),
                        patch.object(admin_auth, "connect", auth_unit_connect),
                        patch.object(startup, "migrate", lambda db: db.executescript(AUTH_SCHEMA))]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def read_user(self):
        with closing(auth_unit_connect(self.path)) as db:
            return dict(db.execute("SELECT * FROM admin_users").fetchone())

    def test_first_launch_creates_hashed_administrator(self):
        user_id = startup.prepare_database(self.path, "salon_admin", PASSWORD)
        token, identity = admin_auth.login(self.path, "salon_admin", PASSWORD)
        self.assertEqual(user_id, identity.user_id)
        self.assertTrue(token)
        self.assertNotEqual(self.read_user()["password_hash"], PASSWORD.encode())

    def test_restart_and_initial_only_preserve_password_and_session(self):
        user_id = startup.prepare_database(self.path, "salon_admin", PASSWORD)
        token, _ = admin_auth.login(self.path, "salon_admin", PASSWORD)
        before = self.read_user()
        self.assertEqual(startup.prepare_database(self.path, "other", "short"), user_id)
        self.assertEqual(admin_auth.create_or_update_admin(
            self.path, "another", "Another-synthetic-password", initial_only=True), user_id)
        self.assertEqual(before, self.read_user())
        self.assertEqual(admin_auth.authenticate(self.path, token).user_id, user_id)
        admin_auth.login(self.path, "salon_admin", PASSWORD)

    def test_missing_invalid_or_example_password_creates_no_account(self):
        for password in (None, "short", "REPLACE_WITH_YOUR_12_TO_256_CHARACTER_PASSWORD"):
            with self.assertRaises((RuntimeError, ValueError)):
                startup.prepare_database(self.path, "salon_admin", password)
            with closing(auth_unit_connect(self.path)) as db:
                self.assertEqual(db.execute("SELECT count(*) FROM admin_users").fetchone()[0], 0)

    def test_disabled_administrator_needs_explicit_recovery(self):
        startup.prepare_database(self.path, "salon_admin", PASSWORD)
        with closing(auth_unit_connect(self.path)) as db:
            db.execute("UPDATE admin_users SET active=0")
        with self.assertRaises(RuntimeError):
            startup.prepare_database(self.path, "new_admin", PASSWORD)
        with self.assertRaises(ValueError):
            admin_auth.create_or_update_admin(self.path, "new_admin", PASSWORD, initial_only=True)
        self.assertEqual(self.read_user()["active"], 0)

    def test_competing_bootstraps_keep_one_winners_credentials(self):
        # Create the auth-unit schema first; PG migration has its own integration suite.
        with closing(auth_unit_connect(self.path)) as db:
            db.executescript(AUTH_SCHEMA)
        contenders = [("salon_admin", "First-synthetic-password"),
                      ("salon_admin", "Second-synthetic-password")]
        barrier = threading.Barrier(2)
        def provision(values):
            barrier.wait(timeout=30)
            return admin_auth.create_or_update_admin(self.path, *values, initial_only=True)
        with ThreadPoolExecutor(max_workers=2) as pool:
            ids = list(pool.map(provision, contenders))
        self.assertEqual(ids[0], ids[1])
        successful_logins = []
        for values in contenders:
            try:
                successful_logins.append(admin_auth.login(self.path, *values))
            except admin_auth.AuthenticationError:
                pass
        self.assertEqual(len(successful_logins), 1)
        self.assertEqual(successful_logins[0][1].user_id, ids[0])
        with closing(auth_unit_connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM admin_users").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM admin_audit_log WHERE action='admin_created'").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM admin_audit_log WHERE action='password_changed'").fetchone()[0], 0)


class StartupHttpUnits(unittest.TestCase):
    def test_liveness_stays_available_when_database_is_unavailable(self):
        # Real HTTP and a real refused psycopg connection, not a mocked health response.
        with ThreadingHTTPServer(("127.0.0.1", 0), admin_http.Handler) as httpd:
            httpd.secure_cookie = False
            httpd.db_path = "postgresql://synthetic@127.0.0.1:1/unavailable_test?connect_timeout=1"
            worker = threading.Thread(target=httpd.serve_forever)
            worker.start()
            self.addCleanup(httpd.shutdown)
            base = "http://127.0.0.1:" + str(httpd.server_port)
            try:
                with urlopen(base + "/livez", timeout=3) as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(json.load(response), {"status": "ok"})
                from urllib.error import HTTPError
                with self.assertRaises(HTTPError) as error:
                    urlopen(base + "/healthz", timeout=3)
                self.assertEqual(error.exception.code, 503)
            finally:
                httpd.shutdown()
                worker.join()

    def test_bad_config_does_not_initialize_database(self):
        base = {"DATABASE_URL": "postgresql://synthetic/unused_test",
                "SALON_POLICY_JSON": json.dumps(POLICY), "SALON_CSRF_SECRET": "x"*40}
        for change in ({"SALON_CSRF_SECRET": "short"},
                       {"SALON_CSRF_SECRET": "REPLACE_WITH_32_OR_MORE_RANDOM_BYTES"},
                       {"PORT": "0"}, {"VK_GROUP_ID": "123"}):
            with patch.dict(os.environ, {**base, **change}, clear=True), patch.object(app, "prepare_database") as prepare:
                with self.assertRaises(RuntimeError):
                    app.main()
                prepare.assert_not_called()


URL = os.environ.get("STARTUP_TEST_DATABASE_URL", "")


@unittest.skipUnless(URL, "STARTUP_TEST_DATABASE_URL absent: PG startup acceptance not executed")
class StartupPostgresIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not urlsplit(URL).path.endswith("_test"):
            raise ValueError("Use a fresh disposable *_test database")
        with closing(connect(URL)) as db:
            if db.execute("SELECT to_regclass('public.admin_users')").fetchone()[0] is not None:
                raise ValueError("Startup test database must be fresh")

    def test_first_launch_restart_concurrency_and_disabled_account(self):
        for password in (None, "short"):
            with self.assertRaises((RuntimeError, ValueError)):
                startup.prepare_database(URL, "salon_admin", password)
            with closing(connect(URL)) as db:
                self.assertEqual(db.execute("SELECT count(*) FROM admin_users").fetchone()[0], 0)
        contenders = [("salon_admin", "First-synthetic-password"),
                      ("salon_admin", "Second-synthetic-password")]
        barrier = threading.Barrier(2)
        real_provision = admin_auth.create_or_update_admin
        def synchronized_provision(*args, **kwargs):
            # Both launches have read an empty table before either may provision.
            # Only scheduling is controlled; the PG transaction and auth stay real.
            self.assertTrue(kwargs.get("initial_only"))
            barrier.wait(timeout=30)
            return real_provision(*args, **kwargs)
        with patch.object(startup, "create_or_update_admin", synchronized_provision):
            with ThreadPoolExecutor(max_workers=2) as pool:
                ids = list(pool.map(lambda values: startup.prepare_database(URL, *values), contenders))
        self.assertEqual(ids[0], ids[1])
        first = ids[0]
        with closing(connect(URL)) as db:
            before = dict(db.execute("SELECT * FROM admin_users").fetchone())
        successful_logins = []
        for values in contenders:
            try:
                successful_logins.append(admin_auth.login(URL, *values))
            except admin_auth.AuthenticationError:
                pass
        self.assertEqual(len(successful_logins), 1)
        token, identity = successful_logins[0]
        self.assertEqual(identity.user_id, first)
        service = create_service(URL, "Synthetic startup service", 45, "admin:test")
        state = snapshot(URL, POLICY, date.today())
        self.assertEqual(startup.prepare_database(URL, "ignored_admin", "short"), first)
        self.assertEqual(startup.prepare_database(URL), first)
        self.assertEqual(admin_auth.authenticate(URL, token).user_id, identity.user_id)
        self.assertEqual(snapshot(URL, POLICY, date.today())["services"], state["services"])
        self.assertEqual(state["services"][0]["id"], service["id"])
        with closing(connect(URL)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM admin_users").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM masters").fetchone()[0], 0)
            self.assertEqual(dict(db.execute("SELECT * FROM admin_users").fetchone()), before)
            self.assertEqual(db.execute("SELECT count(*) FROM admin_audit_log WHERE action='admin_created'").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM admin_audit_log WHERE action='password_changed'").fetchone()[0], 0)
            db.execute("UPDATE admin_users SET active=0")
        with self.assertRaises(RuntimeError):
            startup.prepare_database(URL, "replacement", PASSWORD)
        with closing(connect(URL)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM admin_users WHERE active=1").fetchone()[0], 0)
