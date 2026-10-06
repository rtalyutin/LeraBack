"""Startup mode rules plus disposable PostgreSQL contract acceptance.

RUNTIME_SCHEMA_TEST_DATABASE_URL must point to a fresh, isolated schema owned by
a NOSUPERUSER NOBYPASSRLS test role. Production URLs must never be supplied.
"""
from contextlib import closing
import os
import unittest
from unittest.mock import MagicMock, patch

import startup
from pg_store import connect
from schema_runtime import _normalize


class StartupModeUnits(unittest.TestCase):
    def run_start(self, mode, version=5, users=None, verification_error=None):
        db = MagicMock()
        db.execute.return_value.fetchone.return_value = None
        db.execute.return_value.__iter__.return_value = iter(
            [{"id": 7, "active": 1}] if users is None else users)
        with patch.object(startup, "connect", return_value=db), \
                patch.object(startup, "schema_version", return_value=version), \
                patch.object(startup, "verify_runtime_schema", side_effect=verification_error) as verify, \
                patch.object(startup, "migrate") as migrate, \
                patch.object(startup, "create_or_update_admin") as provision:
            try:
                result = startup.prepare_database("synthetic", mode=mode)
            except RuntimeError as error:
                result = error
        return result, verify, migrate, provision

    def test_ready_auto_preserves_admin_without_migration(self):
        result, verify, migrate, provision = self.run_start("auto")
        self.assertEqual(result, 7)
        verify.assert_called_once()
        migrate.assert_not_called()
        provision.assert_not_called()

    def test_first_auto_still_migrates_then_verifies(self):
        result, verify, migrate, _ = self.run_start("auto", version=0)
        self.assertEqual(result, 7)
        migrate.assert_called_once()
        verify.assert_called_once()

    def test_verify_never_falls_back_to_migration(self):
        error = RuntimeError("incompatible schema")
        result, _, migrate, provision = self.run_start("verify", version=4, verification_error=error)
        self.assertIs(result, error)
        migrate.assert_not_called()
        provision.assert_not_called()

    def test_future_auto_never_migrates(self):
        error = RuntimeError("future schema")
        result, _, migrate, _ = self.run_start("auto", version=9999, verification_error=error)
        self.assertIs(result, error)
        migrate.assert_not_called()

    def test_verify_requires_existing_active_admin(self):
        for users in ([], [{"id": 7, "active": 0}]):
            with self.subTest(users=users):
                result, _, migrate, provision = self.run_start("verify", users=users)
                self.assertIsInstance(result, RuntimeError)
                migrate.assert_not_called()
                provision.assert_not_called()

    def test_explicit_migration_retains_repair_path(self):
        result, verify, migrate, _ = self.run_start("migrate")
        self.assertEqual(result, 7)
        migrate.assert_called_once()
        verify.assert_not_called()

    def test_invalid_mode_rejected_before_connection(self):
        with patch.object(startup, "connect") as connection:
            with self.assertRaisesRegex(RuntimeError, "DATABASE_STARTUP_MODE"):
                startup.prepare_database("synthetic", mode="ignore_errors")
            connection.assert_not_called()

    def test_unversioned_existing_schema_is_not_migrated_automatically(self):
        db = MagicMock()
        db.execute.return_value.fetchone.return_value = (1,)
        with patch.object(startup, "connect", return_value=db), \
                patch.object(startup, "schema_version", return_value=0), \
                patch.object(startup, "migrate") as migrate, \
                patch.object(startup, "verify_runtime_schema") as verify:
            with self.assertRaisesRegex(RuntimeError, "Unversioned existing schema"):
                startup.prepare_database("synthetic", mode="auto")
            migrate.assert_not_called()
            verify.assert_not_called()


class NamespaceFingerprintUnits(unittest.TestCase):
    def test_reserved_raw_marker_cannot_impersonate_a_valid_reference(self):
        with self.assertRaisesRegex(RuntimeError, "reserved namespace marker"):
            _normalize("SELECT * FROM __APP_SCHEMA__.entities", "lera")

    def test_normalization_preserves_literals_spacing_and_other_identifiers(self):
        source = 'SELECT "lera".entities, lera.entities, lera_extra.entities, \'lera\', \'two  spaces\';'
        normalized = _normalize(source, "lera")
        self.assertEqual(normalized, "SELECT __APP_SCHEMA__.entities, __APP_SCHEMA__.entities, lera_extra.entities, 'lera', 'two  spaces';")


URL = os.environ.get("RUNTIME_SCHEMA_TEST_DATABASE_URL", "")


@unittest.skipUnless(URL, "RUNTIME_SCHEMA_TEST_DATABASE_URL absent; native PostgreSQL not executed")
class RuntimeSchemaAcceptance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg
        with psycopg.connect(URL, autocommit=True) as raw:
            if raw.execute("SELECT to_regnamespace('runtime_acceptance')").fetchone()[0]:
                raise ValueError("Runtime acceptance schema already exists; select a fresh disposable database")
            raw.execute("CREATE SCHEMA runtime_acceptance AUTHORIZATION CURRENT_USER")
        cls.environment = patch.dict(os.environ, {"DATABASE_SCHEMA": "runtime_acceptance"})
        cls.environment.start()
        cls.addClassCleanup(cls.environment.stop)
        with closing(connect(URL)) as db:
            if db.execute("SELECT to_regclass('__APP_SCHEMA__.schema_migrations')").fetchone()[0]:
                raise ValueError("RUNTIME_SCHEMA_TEST_DATABASE_URL must select a fresh isolated schema")
        cls.admin = startup.prepare_database(URL, password="Synthetic-runtime-test-password", mode="auto")

    def test_ready_auto_needs_no_schema_create_and_preserves_accounts(self):
        with closing(connect(URL)) as db:
            before = list(db.execute("SELECT * FROM admin_users ORDER BY id"))
            db.execute("REVOKE CREATE ON SCHEMA __APP_SCHEMA__ FROM CURRENT_USER")
            self.assertFalse(db.execute("SELECT has_schema_privilege(current_user,?,'CREATE')", (db.schema_name,)).fetchone()[0])
        try:
            self.assertEqual(startup.prepare_database(URL, mode="auto"), self.admin)
            with closing(connect(URL)) as db:
                self.assertEqual(list(db.execute("SELECT * FROM admin_users ORDER BY id")), before)
        finally:
            with closing(connect(URL)) as db:
                db.execute("GRANT CREATE ON SCHEMA __APP_SCHEMA__ TO CURRENT_USER")

    def test_changed_guard_is_rejected_and_explicit_migration_repairs_it(self):
        with closing(connect(URL)) as db:
            db.execute("ALTER TABLE __APP_SCHEMA__.bookings DISABLE ROW LEVEL SECURITY")
        try:
            with self.assertRaisesRegex(RuntimeError, "contract mismatch"):
                startup.prepare_database(URL, mode="verify")
        finally:
            startup.prepare_database(URL, mode="migrate")
        self.assertEqual(startup.prepare_database(URL, mode="verify"), self.admin)

    def test_future_schema_rejected_without_writes(self):
        with closing(connect(URL)) as db:
            db.execute("INSERT INTO __APP_SCHEMA__.schema_migrations VALUES(9999,'synthetic-future')")
        try:
            with self.assertRaisesRegex(RuntimeError, "schema version"):
                startup.prepare_database(URL, mode="auto")
            with closing(connect(URL)) as db:
                self.assertEqual(db.execute("SELECT max(version) FROM __APP_SCHEMA__.schema_migrations").fetchone()[0], 9999)
        finally:
            with closing(connect(URL)) as db:
                db.execute("DELETE FROM __APP_SCHEMA__.schema_migrations WHERE version=9999")


if __name__ == "__main__":
    unittest.main()
