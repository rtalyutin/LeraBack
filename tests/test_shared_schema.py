"""Validation plus real migration/RLS/concurrency tests on an empty *_test DB.

Run: SHARED_SCHEMA_TEST_DATABASE_URL=postgresql://.../shared_test \
     python -m unittest discover -s tests -p test_shared_schema.py -v
The PostgreSQL role must own the test tables, be NOSUPERUSER/NOBYPASSRLS and
have schema CREATE privileges. No server is installed or started by these tests.
Absent URL means the PG tests are explicitly skipped, not a runtime PASS.
"""
import json
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal
from pathlib import Path
from threading import Barrier
from urllib.parse import urlsplit
from uuid import uuid4
from unittest.mock import patch

from constructor_store import (ConstructorError, _code, _id, _typed_value,
    archive_entity, constructor_snapshot, create_type, delete_parameter,
    delete_type, get_policy, save_entity, save_parameter, update_type)
from shared_schema import MODELS, SALON_TABLES, provision_salon, upgrade_shared

URL = os.environ.get("SHARED_SCHEMA_TEST_DATABASE_URL", "")
ROOT = Path(__file__).resolve().parents[1]
POLICY = json.loads((ROOT / "starter_data.json").read_text())["policy"]


class InputValidation(unittest.TestCase):
    def typed(self, dtype, value):
        return _typed_value(None, 1, {"code": "example", "data_type": dtype}, value)

    def test_bool_does_not_become_integer_or_reference_id(self):
        for value in (True, False, 0, -1, "1", 1.0, 9223372036854775807):
            with self.assertRaises(ConstructorError):
                _id(value)
        with self.assertRaises(ConstructorError):
            self.typed("integer", True)
        self.assertEqual(self.typed("integer", -9223372036854775808), -9223372036854775808)

    def test_nonfinite_numeric_and_implicit_coercion_rejected(self):
        for value in (float("nan"), float("inf"), Decimal("NaN"), Decimal("Infinity"), True, "1.5"):
            with self.assertRaises(ConstructorError):
                self.typed("number", value)
        self.assertEqual(self.typed("number", 1.25), Decimal("1.25"))
        for dtype, value in (("boolean", 1), ("string", 12), ("integer", 1.2)):
            with self.assertRaises(ConstructorError):
                self.typed(dtype, value)

    def test_iso_date_and_codes(self):
        self.assertEqual(self.typed("date", "2024-02-29"), date(2024, 2, 29))
        for value in ("2023-02-29", "20240229", "2024-W09-4", "2024-02-29T00:00:00", None):
            with self.assertRaises(ConstructorError):
                self.typed("date", value)
        for value in ("UPPER", "bad-name", "x';DROP TABLE", "я", "_bad", "x" * 65):
            with self.assertRaises(ConstructorError):
                _code(value)
        self.assertEqual(_code("custom_12"), "custom_12")


class _Row(dict):
    def __getitem__(self, key):
        return tuple(self.values())[key] if isinstance(key, int) else super().__getitem__(key)


def _factory(cursor):
    names = [column.name for column in cursor.description or ()]
    return lambda values: _Row(zip(names, values))


class _Database:
    """Use the real PG driver without relying on the compatibility view adapter."""
    def __init__(self, url, salon_id=None):
        import psycopg
        self.connection = psycopg.connect(url, autocommit=True, row_factory=_factory)
        if salon_id is not None:
            self.execute("SELECT set_config('app.salon_id',?,false)", (str(salon_id),))

    def execute(self, sql, params=()):
        if sql == "BEGIN IMMEDIATE":
            self.connection.execute("BEGIN")
            return self.connection.execute("SELECT pg_advisory_xact_lock(706547229101)")
        return self.connection.execute(sql.replace("?", "%s"), params or None)

    def commit(self):
        self.connection.commit()

    def rollback(self):
        self.connection.rollback()

    def close(self):
        self.connection.close()


@unittest.skipUnless(URL, "SHARED_SCHEMA_TEST_DATABASE_URL absent; PostgreSQL not executed")
class SharedPostgres(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not urlsplit(URL).path.removeprefix("/").endswith("_test"):
            raise ValueError("Use a fresh disposable PostgreSQL database named *_test")
        db = _Database(URL)
        try:
            role = db.execute("SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user").fetchone()
            if role["rolsuper"] or role["rolbypassrls"]:
                raise ValueError("RLS tests require NOSUPERUSER NOBYPASSRLS; superuser results are not evidence")
            if db.execute("SELECT to_regclass('public.bookings')").fetchone()[0] is not None:
                raise ValueError("SHARED_SCHEMA_TEST_DATABASE_URL must be fresh; no data is erased")
            db.execute((ROOT / "schema_postgres.sql").read_text())
            db.execute("INSERT INTO public.schema_migrations VALUES(1,'legacy'),(2,'legacy'),(3,'legacy')")
            # Realistic legacy IDs, confirmed booking, history and live session.
            db.execute("INSERT INTO public.services(id,name,duration_minutes) VALUES(77,'Legacy service',30)")
            db.execute("INSERT INTO public.masters(id,name) VALUES(88,'Legacy master')")
            db.execute("INSERT INTO public.rooms(id,name) VALUES(99,'Legacy room')")
            db.execute("INSERT INTO public.clients(id,vk_id,phone,phone_provided_at) VALUES(66,12345,'+79990000066','2025-01-01T00:00:00Z')")
            db.execute("""INSERT INTO public.bookings(id,client_id,service_id,master_id,room_id,start_utc,end_utc,
                source,status,phone_snapshot,service_name_snapshot,master_name_snapshot,created_at,updated_at)
                VALUES(55,66,77,88,99,'2050-01-01T10:00:00Z','2050-01-01T10:30:00Z','admin','confirmed',
                 '+79990000066','Legacy service','Legacy master','2025-01-01T00:00:00Z','2025-01-01T00:00:00Z')""")
            db.execute("INSERT INTO public.booking_history VALUES(44,55,'confirmed','legacy','{}','2025-01-01T00:00:00Z')")
            db.execute("""INSERT INTO public.admin_users(id,username,password_salt,password_hash,password_iterations,created_at,password_changed_at)
                VALUES(33,'legacy_admin',?,?,100000,'2025-01-01T00:00:00Z','2025-01-01T00:00:00Z')""", (b"s" * 16, b"h" * 32))
            db.execute("""INSERT INTO public.admin_sessions(id,admin_user_id,token_hash,created_at,last_seen_at,expires_at)
                VALUES(22,33,?,'2025-01-01T00:00:00Z','2025-01-01T00:00:00Z','2050-01-01T00:00:00Z')""", (b"t" * 32,))
            db.execute("INSERT INTO public.admin_audit_log VALUES(11,'admin:33','login','admin_session','22','{}','2025-01-01T00:00:00Z')")
            cls.before = {t: [dict(r) for r in db.execute(f"SELECT * FROM public.{t} ORDER BY id")]
                          for t in ("services", "masters", "rooms", "clients", "bookings", "booking_history", "admin_sessions")}
            db.execute("BEGIN IMMEDIATE")
            cls.policy = {**POLICY, "slot_step_minutes": 15}
            with patch.dict(os.environ, {"SALON_POLICY_JSON": json.dumps(cls.policy)}):
                upgrade_shared(db)
            db.commit()
            db.execute("BEGIN IMMEDIATE")
            cls.salon2 = provision_salon(db, "Second salon", {**POLICY, "timezone": "UTC"})
            db.execute("INSERT INTO public.salon_memberships(salon_id,user_id) VALUES(?,33)", (cls.salon2,))
            db.commit()
        finally:
            db.close()

    def setUp(self):
        self.db = _Database(URL, 1)

    def tearDown(self):
        self.db.rollback()
        self.db.close()

    def fixture(self, db=None, shared=None):
        db = db or self.db
        token = uuid4().hex
        service = db.execute("INSERT INTO public.services(name,duration_minutes) VALUES(?,30) RETURNING id,entity_id", (token,)).fetchone()
        master = db.execute("INSERT INTO public.masters(name,shared_master_id) VALUES(?,?) RETURNING id,entity_id,shared_master_id", (token, shared)).fetchone()
        room = db.execute("INSERT INTO public.rooms(name) VALUES(?) RETURNING id,entity_id", (token,)).fetchone()
        return service, master, room

    @staticmethod
    def booking(db, fixture, phone, start="2050-02-01T10:00:00Z", end="2050-02-01T10:30:00Z"):
        service, master, room = fixture
        return db.execute("""INSERT INTO public.bookings(service_id,master_id,room_id,start_utc,end_utc,source,status,
            phone_snapshot,service_name_snapshot,master_name_snapshot,created_at,updated_at)
            VALUES(?,?,?,?,?,'admin','confirmed',?,?,?,'2025-01-01T00:00:00Z','2025-01-01T00:00:00Z') RETURNING id""",
            (service["id"], master["id"], room["id"], start, end, phone, "service", "master")).fetchone()[0]

    def test_01_migration_preserves_all_ids_values_sessions_and_repeats(self):
        for table, rows in self.before.items():
            keys = tuple(rows[0])
            # Project precisely the old columns; technical columns are additions.
            after = [dict(r) for r in self.db.execute(f"SELECT {','.join(keys)} FROM public.{table} WHERE id=?", (rows[0]["id"],))]
            self.assertEqual(rows, after, table)
        self.assertEqual(self.db.execute("SELECT user_id FROM public.salon_memberships WHERE salon_id=1").fetchone()[0], 33)
        self.assertEqual(self.db.execute("SELECT id FROM public.account_audit_log WHERE action='login'").fetchone()[0], 11)
        before = constructor_snapshot(self.db)
        self.db.execute("BEGIN IMMEDIATE")
        upgrade_shared(self.db)
        self.db.commit()
        self.assertEqual(before, constructor_snapshot(self.db))
        self.assertEqual(self.db.execute("SELECT max(version) FROM public.schema_migrations").fetchone()[0], 4)
        self.assertIsNone(self.db.execute("SELECT to_regclass('public.one_active_admin')").fetchone()[0])
        for table in SALON_TABLES:
            flags = self.db.execute("SELECT relrowsecurity,relforcerowsecurity FROM pg_class WHERE oid=to_regclass(?)", ("public." + table,)).fetchone()
            self.assertEqual(tuple(flags.values()), (True, True), table)

    def test_02_salon_isolation_missing_context_and_shared_accounts(self):
        second = _Database(URL, self.salon2)
        try:
            a = self.fixture()
            b = self.fixture(second)
            self.assertIsNone(second.execute("SELECT id FROM public.services WHERE id=?", (a[0]["id"],)).fetchone())
            self.assertIsNone(self.db.execute("SELECT id FROM public.services WHERE id=?", (b[0]["id"],)).fetchone())
            self.assertEqual(get_policy(self.db), self.policy)
            self.assertEqual(get_policy(second)["timezone"], "UTC")
            self.assertEqual(second.execute("SELECT count(*) FROM public.salon_memberships WHERE user_id=33").fetchone()[0], 2)
            second.execute("SELECT set_config('app.salon_id','',false)")
            self.assertEqual(second.execute("SELECT count(*) FROM public.services").fetchone()[0], 0)
            with self.assertRaises(Exception):
                second.execute("INSERT INTO public.services(name,duration_minutes) VALUES('no-context',30)")
        finally:
            second.close()

    def test_03_composite_fk_and_constructor_cross_salon_reference(self):
        second = _Database(URL, self.salon2)
        try:
            a, b = self.fixture(), self.fixture(second)
            with self.assertRaises(Exception):
                self.db.execute("INSERT INTO public.master_services(master_id,service_id) VALUES(?,?)", (a[1]["id"], b[0]["id"]))
            self.assertEqual(self.db.execute("SELECT count(*) FROM public.master_services WHERE master_id=?", (a[1]["id"],)).fetchone()[0], 0)
            t = create_type(self.db, "custom_" + uuid4().hex, "Custom")
            p = save_parameter(self.db, {"entity_type_id": t["id"], "code": "ref", "label": "Ref", "data_type": "reference",
                "reference_type_id": self.db.execute("SELECT id FROM public.entity_types WHERE salon_id=1 AND code='service'").fetchone()[0]})
            with self.assertRaises(ConstructorError):
                save_entity(self.db, {"entity_type_id": t["id"], "values": {p["code"]: b[0]["entity_id"]}})
            self.assertFalse(any(e["entity_type_id"] == t["id"] for e in constructor_snapshot(self.db)["entities"]))
        finally:
            second.close()

    def test_04_typed_crud_required_validation_and_metadata_versions(self):
        t = create_type(self.db, "custom_" + uuid4().hex, "Custom")
        p = save_parameter(self.db, {"entity_type_id": t["id"], "code": "count", "label": "Count", "data_type": "integer", "required": True})
        with self.assertRaises(ConstructorError):
            save_entity(self.db, {"entity_type_id": t["id"], "values": {}})
        e = save_entity(self.db, {"entity_type_id": t["id"], "values": {"count": 3}})
        self.assertEqual(e["values"], {"count": 3})
        updated = save_entity(self.db, {"id": e["id"], "values": {"count": 4}})
        self.assertGreater(updated["version"], e["version"])
        with self.assertRaises(Exception):
            save_parameter(self.db, {"id": p["id"], "data_type": "string"})
        self.assertEqual(next(x for x in constructor_snapshot(self.db)["types"] if x["id"] == t["id"])["parameters"][0]["data_type"], "integer")
        with self.assertRaises(ConstructorError):
            delete_parameter(self.db, p["id"])
        self.assertTrue(archive_entity(self.db, e["id"])["archived"])
        empty = create_type(self.db, "empty_" + uuid4().hex, "Empty")
        self.assertGreater(update_type(self.db, empty["id"], "Renamed")["version"], empty["version"])
        self.assertTrue(delete_type(self.db, empty["id"])["deleted"])

    def test_05_core_extras_and_projection_commands_are_atomic(self):
        a = self.fixture()
        core_type = self.db.execute("SELECT id FROM public.entity_types WHERE salon_id=1 AND code='service'").fetchone()[0]
        with self.assertRaises(ConstructorError):
            save_parameter(self.db, {"entity_type_id": core_type, "code": "required_extra", "label": "Required extra", "data_type": "string", "required": True})
        save_parameter(self.db, {"entity_type_id": core_type, "code": "description_" + uuid4().hex[:8], "label": "Description", "data_type": "string"})
        with self.assertRaises(ConstructorError):
            save_entity(self.db, {"id": a[0]["entity_id"], "values": {"duration_minutes": 60}})
        with self.assertRaises(ConstructorError):
            save_entity(self.db, {"entity_type_id": core_type, "values": {}})
        with self.assertRaises(ConstructorError):
            archive_entity(self.db, a[0]["entity_id"])
        with self.assertRaises(Exception):
            self.db.execute("""UPDATE public.entity_parameter_values SET value_integer=60
                WHERE salon_id=1 AND entity_id=? AND parameter_id=(SELECT id FROM public.entity_parameters
                  WHERE salon_id=1 AND entity_type_id=? AND code='duration_minutes')""", (a[0]["entity_id"], core_type))
        self.db.execute("UPDATE public.services SET duration_minutes=45 WHERE id=?", (a[0]["id"],))
        entity = next(e for e in constructor_snapshot(self.db)["entities"] if e["id"] == a[0]["entity_id"])
        self.assertEqual(entity["values"]["duration_minutes"], 45)
        settings = self.db.execute("SELECT id FROM public.entity_types WHERE salon_id=1 AND code='salon_settings'").fetchone()[0]
        settings_entity = self.db.execute("SELECT id FROM public.entities WHERE salon_id=1 AND entity_type_id=?", (settings,)).fetchone()[0]
        code = "number_" + uuid4().hex[:8]
        save_parameter(self.db, {"entity_type_id": settings, "code": code, "label": "Number", "data_type": "number"})
        save_entity(self.db, {"id": settings_entity, "values": {code: 1.5}})
        self.assertEqual(get_policy(self.db), self.policy)

    def test_06_phone_is_local_shared_master_is_global_and_busy_releases(self):
        second = _Database(URL, self.salon2)
        try:
            a = self.fixture()
            b = self.fixture(second)
            first_id = self.booking(self.db, a, "+79990000100")
            self.booking(second, b, "+79990000100")  # same phone, separate salon/person
            with self.assertRaises(Exception):
                self.booking(self.db, self.fixture(), "+79990000100")  # same-salon collision
            attached = self.fixture(second, a[1]["shared_master_id"])
            self.assertTrue(second.execute("SELECT public.shared_master_conflict(?,?,?,NULL)",
                (attached[1]["id"], "2050-02-01T10:00:00Z", "2050-02-01T10:30:00Z")).fetchone()[0])
            with self.assertRaises(Exception):
                self.booking(second, attached, "+79990000200")
            self.booking(second, attached, "+79990000201", "2050-02-01T10:30:00Z", "2050-02-01T11:00:00Z")
            self.db.execute("UPDATE public.bookings SET status='cancelled' WHERE id=?", (first_id,))
            self.assertFalse(second.execute("SELECT public.shared_master_conflict(?,?,?,NULL)",
                (attached[1]["id"], "2050-02-01T10:00:00Z", "2050-02-01T10:30:00Z")).fetchone()[0])
            self.booking(second, attached, "+79990000200")
            with self.assertRaises(Exception):
                second.execute("SELECT public.shared_master_conflict(?,?,?,NULL)",
                    (a[1]["id"], "2050-02-01T10:00:00Z", "2050-02-01T10:30:00Z"))
        finally:
            second.close()

    def test_07_concurrent_direct_writers_cannot_double_book_global_person(self):
        second = _Database(URL, self.salon2)
        try:
            a = self.fixture()
            b = self.fixture(second, a[1]["shared_master_id"])
        finally:
            second.close()
        barrier = Barrier(2)
        def attempt(sid, fixture, phone):
            db = _Database(URL, sid)
            try:
                # Deliberately bypass the service and BEGIN IMMEDIATE; database
                # triggers/exclusion must enforce the invariant themselves.
                barrier.wait(timeout=10)
                return self.booking(db, fixture, phone, "2050-03-01T10:00:00Z", "2050-03-01T10:30:00Z")
            except Exception as exc:
                return exc
            finally:
                db.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(attempt, 1, a, "+79990000301"), pool.submit(attempt, self.salon2, b, "+79990000302")]
            results = [f.result(timeout=30) for f in futures]
        self.assertEqual(sum(type(x) is int for x in results), 1, results)
        self.assertEqual(sum(getattr(x, "sqlstate", None) in ("23505", "23P01") for x in results), 1, results)

    def test_08_direct_wrong_type_and_required_omission_rejected(self):
        t = create_type(self.db, "required_" + uuid4().hex, "Required")
        p = save_parameter(self.db, {"entity_type_id": t["id"], "code": "value", "label": "Value", "data_type": "integer", "required": True})
        self.db.execute("BEGIN")
        eid = self.db.execute("INSERT INTO public.entities(salon_id,entity_type_id) VALUES(1,?) RETURNING id", (t["id"],)).fetchone()[0]
        with self.assertRaises(Exception):
            self.db.execute("""INSERT INTO public.entity_parameter_values(salon_id,entity_id,parameter_id,entity_type_id,value_string)
                VALUES(1,?,?,?,'wrong')""", (eid, p["id"], t["id"]))
        self.db.rollback()

        self.db.execute("BEGIN")
        self.db.execute("INSERT INTO public.entities(salon_id,entity_type_id) VALUES(1,?)", (t["id"],))
        with self.assertRaises(Exception):
            self.db.commit()
        self.db.rollback()

    def test_09_repeatable_read_cannot_bypass_local_overlap_snapshot(self):
        fixture = self.fixture()
        self.db.execute("BEGIN ISOLATION LEVEL REPEATABLE READ")
        try:
            with self.assertRaises(Exception) as caught:
                self.booking(self.db, fixture, "+79990000999", "2050-04-01T10:00:00Z", "2050-04-01T10:30:00Z")
            self.assertEqual(getattr(caught.exception, "sqlstate", None), "0A000")
        finally:
            self.db.rollback()

    def test_10_direct_nonfinite_numeric_cannot_break_json_snapshot(self):
        t = create_type(self.db, "numeric_" + uuid4().hex, "Numeric")
        p = save_parameter(self.db, {"entity_type_id": t["id"], "code": "value", "label": "Value", "data_type": "number"})
        existing = save_entity(self.db, {"entity_type_id": t["id"], "values": {"value": 1.25}})
        empty = save_entity(self.db, {"entity_type_id": t["id"], "values": {}})
        for nonfinite in ("NaN", "Infinity", "-Infinity"):
            with self.assertRaises(Exception) as caught:
                self.db.execute("""UPDATE public.entity_parameter_values SET value_number=?::numeric
                    WHERE salon_id=1 AND entity_id=? AND parameter_id=?""", (nonfinite, existing["id"], p["id"]))
            self.assertEqual(getattr(caught.exception, "sqlstate", None), "23514")
            with self.assertRaises(Exception) as caught:
                self.db.execute("""INSERT INTO public.entity_parameter_values
                    (salon_id,entity_id,parameter_id,entity_type_id,value_number)
                    VALUES(1,?,?,?,?::numeric)""", (empty["id"], p["id"], t["id"], nonfinite))
            self.assertEqual(getattr(caught.exception, "sqlstate", None), "23514")
        snapshot = constructor_snapshot(self.db)
        self.assertEqual(next(e for e in snapshot["entities"] if e["id"] == existing["id"])["values"], {"value": 1.25})
        self.assertEqual(next(e for e in snapshot["entities"] if e["id"] == empty["id"])["values"], {})
        json.dumps(snapshot, allow_nan=False)


if __name__ == "__main__":
    unittest.main()
