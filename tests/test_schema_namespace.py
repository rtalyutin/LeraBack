"""Namespace safety units and sentinel checks in a fresh disposable *_test DB.

NAMESPACE_TEST_DATABASE_URL enables native PostgreSQL integration. The test
owner prepares the lera schema; application migrations never CREATE SCHEMA.
No Timeweb or existing tg-mcp database is contacted by this suite.
"""
import json
import os
import unittest
from pathlib import Path
from urllib.parse import urlsplit
from unittest.mock import patch

from database_namespace import relation_name, render_sql, schema_name

URL = os.environ.get("NAMESPACE_TEST_DATABASE_URL", "")


class NamespaceUnits(unittest.TestCase):
    def test_default_and_rejected_identifiers(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(schema_name(), "public")
        self.assertEqual(schema_name("lera"), "lera")
        for bad in ("", "Lera", "public,lera", "lera;DROP SCHEMA public", "lera\"", "pg_catalog", "pg_temp", "pg_custom", "information_schema", "a" * 64, "я", 1):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                schema_name(bad)

    def test_namespace_rendering_preserves_public_role_and_regclass(self):
        self.assertEqual(render_sql("REVOKE ALL ON __APP_SCHEMA__.entities FROM PUBLIC", "lera"),
                         'REVOKE ALL ON "lera".entities FROM PUBLIC')
        self.assertEqual(render_sql("SELECT to_regclass('__APP_SCHEMA__.schema_migrations')", "lera"),
                         'SELECT to_regclass(\'"lera".schema_migrations\')')
        self.assertEqual(render_sql("SET search_path=pg_catalog,__APP_SCHEMA__", "lera"),
                         'SET search_path=pg_catalog,"lera"')
        class Scope:
            schema_name = "lera"
        with patch.dict(os.environ, {"DATABASE_SCHEMA": "other"}):
            self.assertEqual(relation_name(Scope(), "services"), '"lera"."services"')


class _Result:
    def __init__(self, rows=()):
        self.rows = list(rows)
    def fetchone(self):
        return self.rows[0] if self.rows else None
    def __iter__(self):
        return iter(self.rows)


class _Connection:
    def __init__(self, schema_exists=True):
        self.schema_exists = schema_exists
        self.queries = []
        self.closed = False
    def execute(self, query, params=None):
        if hasattr(query, "as_string"):
            query = query.as_string()
        self.queries.append((query, params))
        if "pg_namespace" in query:
            return _Result([(1,)] if self.schema_exists else [])
        if "information_schema.columns" in query:
            return _Result([("id",), ("name",)])
        return _Result()
    def close(self):
        self.closed = True


class AdapterNamespaceUnits(unittest.TestCase):
    def test_future_selected_version_fails_before_any_guard_ddl(self):
        from booking_core import migrate
        class MigrationDatabase:
            scope = None
            def __init__(self):
                self.queries, self.rolled_back = [], False
            def execute(self, query, params=()):
                self.queries.append(query)
                if "to_regclass" in query:
                    return _Result([("lera.schema_migrations",)])
                if "max(version)" in query:
                    return _Result([(9999,)])
                return _Result()
            def rollback(self):
                self.rolled_back = True
            def commit(self):
                raise AssertionError("Future version must not commit")
        db = MigrationDatabase()
        with patch("shared_schema.upgrade_shared") as upgrade:
            with self.assertRaisesRegex(RuntimeError, "newer than supported"):
                migrate(db)
            upgrade.assert_not_called()
        self.assertTrue(db.rolled_back)
        self.assertEqual(len(db.queries), 3)
        self.assertTrue(all(query == "BEGIN IMMEDIATE" or query.startswith("SELECT ") for query in db.queries))

    def test_bad_schema_fails_before_connect_and_missing_schema_before_ddl(self):
        from pg_store import Database
        with patch.dict(os.environ, {"DATABASE_SCHEMA": "lera; DROP SCHEMA public"}), patch("pg_store.psycopg.connect") as connect:
            with self.assertRaises(ValueError):
                Database("postgresql://local/test")
            connect.assert_not_called()
        connection = _Connection(schema_exists=False)
        with patch.dict(os.environ, {"DATABASE_SCHEMA": "missing"}), patch("pg_store.psycopg.connect", return_value=connection):
            with self.assertRaises(RuntimeError):
                Database("postgresql://local/test")
        self.assertTrue(connection.closed)
        self.assertEqual(len(connection.queries), 1)

    def test_captured_schema_views_upsert_and_literal_parameters(self):
        from pg_store import Database, SalonScope
        connection = _Connection()
        with patch.dict(os.environ, {"DATABASE_SCHEMA": "lera"}), patch("pg_store.psycopg.connect", return_value=connection):
            db = Database(SalonScope("postgresql://local/test", 1))
        views = [q for q, _ in connection.queries if q.startswith("CREATE TEMP VIEW")]
        self.assertTrue(views)
        self.assertTrue(all('FROM "lera".' in q and '"lera".current_salon_id()' in q for q in views))
        columns = [p for q, p in connection.queries if "information_schema.columns" in q]
        self.assertTrue(all(p[0] == "lera" for p in columns))
        with patch.dict(os.environ, {"DATABASE_SCHEMA": "public"}):
            db.execute("INSERT INTO vk_dialogs(vk_id,state,draft_json,expires_at) VALUES(?,?,?,?) ON CONFLICT(salon_id,vk_id) DO NOTHING",
                       (1, "s", "{}", "later"))
            self.assertTrue(connection.queries[-1][0].startswith('INSERT INTO "lera".vk_dialogs'))
            literal = "__APP_SCHEMA__.public.accounts"
            db.execute("SELECT ? FROM __APP_SCHEMA__.salons", (literal,))
            self.assertEqual(connection.queries[-1], ('SELECT %s FROM "lera".salons', (literal,)))
            db.execute("INSERT INTO __APP_SCHEMA__.masters(name) VALUES(?)", ("Master",))
            self.assertEqual(connection.queries[-1][0], 'INSERT INTO "lera".masters(name) VALUES(%s) RETURNING id')
        db.close()


@unittest.skipUnless(URL, "NAMESPACE_TEST_DATABASE_URL absent; native PostgreSQL not executed")
class NamespacePostgres(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg
        from pg_store import connect
        from booking_core import migrate
        if not urlsplit(URL).path.removeprefix("/").endswith("_test"):
            raise ValueError("Use a fresh disposable *_test database")
        cls.env = patch.dict(os.environ, {"DATABASE_SCHEMA": "lera"})
        cls.env.start()
        cls.addClassCleanup(cls.env.stop)
        # Explicit fixture preparation only. These objects simulate unrelated
        # tg-mcp data and functions; Lera itself must leave them untouched.
        with psycopg.connect(URL, autocommit=True, cursor_factory=psycopg.ClientCursor) as raw:
            flags = raw.execute("SELECT rolsuper,rolbypassrls FROM pg_catalog.pg_roles WHERE rolname=current_user").fetchone()
            if flags != (False, False):
                raise ValueError("Namespace RLS integration requires NOSUPERUSER NOBYPASSRLS")
            if raw.execute("SELECT to_regnamespace('lera') IS NOT NULL OR to_regclass('public.schema_migrations') IS NOT NULL").fetchone()[0]:
                raise ValueError("Namespace test database must be fresh; no existing objects are removed")
            raw.execute("CREATE SCHEMA lera")
            raw.execute("CREATE TABLE public.schema_migrations(version integer PRIMARY KEY,applied_at text NOT NULL)")
            raw.execute("INSERT INTO public.schema_migrations VALUES(9999,'tg-mcp-sentinel')")
            raw.execute("CREATE TABLE public.services(id bigint PRIMARY KEY,name text NOT NULL,duration_minutes integer NOT NULL)")
            raw.execute("INSERT INTO public.services VALUES(31337,'tg-mcp-sentinel',13)")
            raw.execute("CREATE FUNCTION public.current_salon_id() RETURNS bigint LANGUAGE sql AS 'SELECT 999999::bigint'")
            raw.execute("CREATE FUNCTION public.protect_booking_overlap() RETURNS trigger LANGUAGE plpgsql AS $$BEGIN RAISE EXCEPTION 'tg-mcp guard'; END$$")
            cls.sentinel = cls._sentinel(raw)
        with connect(URL) as db:
            migrate(db)

    @staticmethod
    def _sentinel(raw):
        return {
            "versions": raw.execute("SELECT * FROM public.schema_migrations ORDER BY version").fetchall(),
            "services": raw.execute("SELECT * FROM public.services ORDER BY id").fetchall(),
            "functions": raw.execute("SELECT p.oid,p.proname,pg_get_functiondef(p.oid) FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='public' AND p.proname IN ('current_salon_id','protect_booking_overlap') ORDER BY p.proname").fetchall(),
            "relations": raw.execute("SELECT c.oid,c.relname,c.relkind FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' ORDER BY c.relname").fetchall(),
            "extensions": raw.execute("SELECT e.oid,e.extname,n.nspname,e.extversion FROM pg_catalog.pg_extension e JOIN pg_catalog.pg_namespace n ON n.oid=e.extnamespace ORDER BY e.extname").fetchall(),
        }

    def test_fresh_and_repeat_migration_ignore_foreign_public(self):
        import psycopg
        from booking_core import migrate
        from pg_store import connect
        from ops import database_health, verify_database
        with connect(URL) as db:
            self.assertEqual(db.execute("SELECT max(version) FROM __APP_SCHEMA__.schema_migrations").fetchone()[0], 4)
            migrate(db)
            self.assertEqual(db.execute("SELECT current_schema()").fetchone()[0], "lera")
            wrong_fks = db.execute("""SELECT c.conname FROM pg_catalog.pg_constraint c
                JOIN pg_catalog.pg_class child ON child.oid=c.conrelid JOIN pg_catalog.pg_namespace cn ON cn.oid=child.relnamespace
                JOIN pg_catalog.pg_class parent ON parent.oid=c.confrelid JOIN pg_catalog.pg_namespace pn ON pn.oid=parent.relnamespace
                WHERE c.contype='f' AND cn.nspname=? AND pn.nspname<>?""", ("lera", "lera")).fetchall()
            self.assertEqual(wrong_fks, [])
            paths = db.execute("SELECT p.proconfig FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname=?", ("lera",)).fetchall()
            self.assertTrue(paths)
            self.assertTrue(all("public" not in str(p[0]) for p in paths))
        self.assertEqual(database_health(URL), {"status": "ok", "schema_version": 4})
        self.assertEqual(verify_database(URL), {"status": "ok", "schema_version": 4})
        with psycopg.connect(URL, autocommit=True, cursor_factory=psycopg.ClientCursor) as raw:
            self.assertEqual(self.sentinel, self._sentinel(raw))

    def test_scoped_legacy_commands_constructor_and_missing_schema(self):
        from pg_store import SalonScope, connect
        from admin_service import create_service
        from constructor_store import constructor_snapshot, get_policy
        result = create_service(SalonScope(URL, 1), "Schema service", 30, "admin:test")
        with connect(SalonScope(URL, 1)) as db:
            self.assertEqual(db.execute("SELECT name FROM services WHERE id=?", (result["id"],)).fetchone()[0], "Schema service")
            self.assertTrue(get_policy(db))
            self.assertTrue(constructor_snapshot(db)["entities"])
            db.execute("INSERT INTO vk_dialogs(vk_id,state,draft_json,expires_at) VALUES(1,'a','{}','later') ON CONFLICT(salon_id,vk_id) DO UPDATE SET state=excluded.state")
            db.execute("INSERT INTO vk_dialogs(vk_id,state,draft_json,expires_at) VALUES(1,'b','{}','later') ON CONFLICT(salon_id,vk_id) DO UPDATE SET state=excluded.state")
            self.assertEqual(db.execute("SELECT state FROM vk_dialogs WHERE vk_id=1").fetchone()[0], "b")
            self._exercise_ignored_insert_entities(db)
        with patch.dict(os.environ, {"DATABASE_SCHEMA": "does_not_exist"}):
            with self.assertRaises(RuntimeError):
                connect(URL)

    def _exercise_ignored_insert_entities(self, db):
        """Real adapter paths, including conflicts on keys other than generated ID."""
        import psycopg
        from constructor_store import constructor_snapshot
        for _ in range(2):
            db.execute("INSERT INTO inbound_events(event_id,status,received_at) VALUES('namespace-dedupe','received','2050-01-01') ON CONFLICT(salon_id,event_id) DO NOTHING")
        statement = """INSERT INTO vk_outgoing_messages(inbound_event_id,recipient_vk_id,text,dedupe_key,random_id,status,created_at)
            VALUES('namespace-dedupe',8123,?,?,?,'pending','2050-01-01') ON CONFLICT DO NOTHING"""
        db.execute(statement, ("original", "namespace-reply", 8123))
        original = dict(db.execute("SELECT * FROM __APP_SCHEMA__.vk_outgoing_messages WHERE dedupe_key='namespace-reply'").fetchone())
        # Exercise dedupe_key, random_id and both simultaneously. Each rejected
        # candidate receives a new identity before PostgreSQL resolves conflict.
        for key, random_id in (("namespace-reply", 8124), ("namespace-other", 8123), ("namespace-reply", 8123)):
            db.execute(statement, ("ignored", key, random_id))
        self.assertEqual(dict(db.execute("SELECT * FROM __APP_SCHEMA__.vk_outgoing_messages WHERE dedupe_key='namespace-reply'").fetchone()), original)
        for code, table in (("vk_dialog", "vk_dialogs"), ("inbound_event", "inbound_events"), ("vk_outgoing_message", "vk_outgoing_messages")):
            entities = db.execute("""SELECT e.id FROM __APP_SCHEMA__.entities e JOIN __APP_SCHEMA__.entity_types t
                ON t.salon_id=e.salon_id AND t.id=e.entity_type_id WHERE t.code=?""", (code,)).fetchall()
            projections = db.execute(f"SELECT entity_id FROM __APP_SCHEMA__.{table}").fetchall()
            self.assertEqual(sorted(r[0] for r in entities), sorted(r[0] for r in projections))
        # A plain duplicate remains an error and rolls back its EAV candidate.
        before = constructor_snapshot(db)
        with self.assertRaises(psycopg.errors.UniqueViolation):
            db.execute("INSERT INTO __APP_SCHEMA__.vk_outgoing_messages(inbound_event_id,recipient_vk_id,text,dedupe_key,random_id,status,created_at) VALUES('namespace-dedupe',8123,'invalid','namespace-reply',8123,'pending','2050-01-01')")
        self.assertEqual(constructor_snapshot(db), before)
        with self.assertRaises(psycopg.errors.CheckViolation):
            db.execute("UPDATE __APP_SCHEMA__.vk_outgoing_messages SET entity_id=entity_id+100000 WHERE id=?", (original["id"],))
        self.assertEqual(constructor_snapshot(db), before)
        json.dumps(before, allow_nan=False)

    def test_existing_v4_startup_refresh_repairs_old_trigger_and_preserves_state(self):
        import psycopg
        from booking_core import migrate
        from constructor_store import get_policy
        from pg_store import connect

        def preserved(db):
            queries = {
                "accounts": "SELECT * FROM __APP_SCHEMA__.accounts ORDER BY id",
                "sessions": "SELECT * FROM __APP_SCHEMA__.admin_sessions ORDER BY id",
                "memberships": "SELECT * FROM __APP_SCHEMA__.salon_memberships ORDER BY salon_id,user_id",
                "entities": "SELECT * FROM __APP_SCHEMA__.entities ORDER BY id",
                "values": "SELECT * FROM __APP_SCHEMA__.entity_parameter_values ORDER BY entity_id,parameter_id",
                "dialogs": "SELECT * FROM __APP_SCHEMA__.vk_dialogs ORDER BY vk_id",
                "versions": "SELECT * FROM __APP_SCHEMA__.schema_migrations ORDER BY version",
            }
            return {name: [dict(row) for row in db.execute(query)] for name, query in queries.items()}

        with connect(URL) as db:
            db.execute("SELECT set_config('app.salon_id','1',false)")
            account = db.execute("""INSERT INTO __APP_SCHEMA__.accounts(username,password_salt,password_hash,password_iterations,created_at,password_changed_at)
                VALUES('namespace_refresh_admin',?,?,100000,'2050-01-01','2050-01-01') RETURNING id""", (b"synthetic-salt", b"synthetic-hash")).fetchone()[0]
            db.execute("INSERT INTO __APP_SCHEMA__.salon_memberships(salon_id,user_id) VALUES(1,?)", (account,))
            db.execute("INSERT INTO __APP_SCHEMA__.admin_sessions(admin_user_id,token_hash,created_at,last_seen_at,expires_at) VALUES(?,?,'2050-01-01','2050-01-01','2050-01-02')", (account, b"synthetic-token-hash"))
            command = """INSERT INTO __APP_SCHEMA__.vk_dialogs(vk_id,state,draft_json,expires_at) VALUES(8991,?,'{}','2050-01-01')
                ON CONFLICT(salon_id,vk_id) DO UPDATE SET state=excluded.state"""
            db.execute(command, ("old-state",))
            original_entity = db.execute("SELECT entity_id FROM __APP_SCHEMA__.vk_dialogs WHERE vk_id=8991").fetchone()[0]
            # Restore the exact shipped pre-repair function, rather than invent
            # a surrogate failure. Startup must repair an already-versioned DB.
            db.execute(Path(__file__).with_name("fixtures").joinpath("projection_entity_before_v4.sql").read_text())
            db.execute("DROP TRIGGER canonical_ignored_cleanup ON __APP_SCHEMA__.vk_dialogs")
            with self.assertRaises(psycopg.errors.UniqueViolation):
                db.execute(command, ("broken-state",))
            before, policy = preserved(db), get_policy(db)
            try:
                migrate(db)
                self.assertEqual(preserved(db), before)
                self.assertEqual(get_policy(db), policy)
                definition = db.execute("SELECT pg_get_functiondef('__APP_SCHEMA__.projection_entity_before()'::regprocedure)").fetchone()[0]
                self.assertIn("ON CONFLICT(salon_id,entity_type_id,projection_key)", definition)
                db.execute(command, ("repaired-state",))
                self.assertEqual(db.execute("SELECT entity_id,state FROM __APP_SCHEMA__.vk_dialogs WHERE vk_id=8991").fetchone()[0], original_entity)
                self.assertEqual(db.execute("SELECT state FROM __APP_SCHEMA__.vk_dialogs WHERE vk_id=8991").fetchone()[0], "repaired-state")
                after = preserved(db)
                migrate(db)
                self.assertEqual(preserved(db), after)
                self.assertEqual(get_policy(db), policy)
            finally:
                migrate(db)

    def test_future_selected_schema_rejected_without_guard_refresh(self):
        from booking_core import migrate
        from pg_store import connect
        from ops import database_health, verify_database
        with connect(URL) as db:
            # The unrelated public version9999 remains legal. Only the selected
            # application's future version must prevent old-code guard refresh.
            db.execute("INSERT INTO __APP_SCHEMA__.schema_migrations VALUES(9999,'future-fixture')")
            definition = db.execute("SELECT pg_get_functiondef('__APP_SCHEMA__.projection_entity_before()'::regprocedure)").fetchone()[0]
            try:
                with self.assertRaisesRegex(RuntimeError, "newer than supported"):
                    migrate(db)
                self.assertEqual(db.execute("SELECT max(version) FROM __APP_SCHEMA__.schema_migrations").fetchone()[0], 9999)
                self.assertEqual(db.execute("SELECT pg_get_functiondef('__APP_SCHEMA__.projection_entity_before()'::regprocedure)").fetchone()[0], definition)
                self.assertEqual(database_health(URL), {"status": "error"})
                with self.assertRaisesRegex(ValueError, "version 4"):
                    verify_database(URL)
            finally:
                db.execute("DELETE FROM __APP_SCHEMA__.schema_migrations WHERE version=9999")


if __name__ == "__main__":
    unittest.main()
