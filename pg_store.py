"""Small DB adapter for the existing booking core on PostgreSQL.

All mutations that use BEGIN IMMEDIATE take one transaction-scoped advisory
lock. This preserves the prototype's serialized read/check/write behavior
while database triggers protect bookings written by other consumers.
"""

from __future__ import annotations

import psycopg
import re
from dataclasses import dataclass, field
from psycopg import sql as pg_sql


LOCK_ID = 706547229101


@dataclass(frozen=True)
class SalonScope:
    """Trusted server-side scope. Never construct it from an unchecked request."""
    url: str = field(repr=False)
    salon_id: int

    def __post_init__(self):
        if type(self.salon_id) is not int or self.salon_id <= 0:
            raise ValueError("Positive salon ID required")


SALON_TABLES = {
    "services", "masters", "rooms", "master_services", "room_services",
    "work_intervals", "resource_blocks", "clients", "bookings", "booking_history",
    "vk_dialogs", "inbound_events", "action_results", "message_outbox",
    "vk_outgoing_messages", "admin_audit_log",
}
GENERATED_ID_TABLES = SALON_TABLES - {"master_services", "room_services", "vk_dialogs", "inbound_events", "action_results"}
GENERATED_ID_TABLES |= {"admin_users", "accounts", "admin_sessions", "account_audit_log", "salons", "shared_masters"}


class Row(dict):
    def __getitem__(self, key):
        if isinstance(key, int):
            return tuple(self.values())[key]
        return super().__getitem__(key)


def row_factory(cursor):
    names = [column.name for column in cursor.description or ()]

    def make_row(values):
        return Row(zip(names, values))

    return make_row


class Result:
    def __init__(self, cursor, database, returning_id=False):
        self.cursor = cursor
        self.database = database
        self.returning_id = returning_id

    def fetchone(self):
        return self.cursor.fetchone()

    def fetchall(self):
        return self.cursor.fetchall()

    def __iter__(self):
        return iter(self.cursor)

    @property
    def lastrowid(self):
        if self.returning_id:
            row = self.cursor.fetchone()
            if row is None:
                raise RuntimeError("INSERT returned no object ID")
            return row[0]
        return self.database.connection.execute("SELECT lastval()").fetchone()[0]


class Database:
    def __init__(self, url):
        self.scope = url if isinstance(url, SalonScope) else None
        url = self.scope.url if self.scope else url
        if not str(url).startswith(("postgresql://", "postgres://")):
            raise ValueError("DATABASE_URL must point to PostgreSQL")
        self.connection = psycopg.connect(url, autocommit=True, row_factory=row_factory, cursor_factory=psycopg.ClientCursor)
        # The shared schema lives in public even when the DB user owns another schema.
        # PostgreSQL still searches pg_catalog and the session's temp namespace implicitly.
        self.connection.execute("SET search_path=public")
        if self.scope:
            try:
                self.connection.execute("SELECT set_config('app.salon_id', %s, false)", (str(self.scope.salon_id),))
                self._scoped_views()
            except Exception:
                self.connection.close()
                raise

    def _scoped_views(self):
        # Explicit predicates also protect callers accidentally using an owner role.
        # Views preserve the legacy SQL shape while all storage remains shared.
        for table in sorted(SALON_TABLES):
            columns = [row[0] for row in self.connection.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name=%s "
                "AND column_name NOT IN ('salon_id','entity_id','shared_master_id') "
                "ORDER BY ordinal_position", (table,))]
            if not columns:
                raise RuntimeError("Shared salon schema is not initialized")
            statement = pg_sql.SQL(
                "CREATE TEMP VIEW {} WITH (security_barrier=true) AS SELECT {} "
                "FROM public.{} WHERE salon_id = public.current_salon_id() WITH CASCADED CHECK OPTION"
            ).format(pg_sql.Identifier(table), pg_sql.SQL(',').join(map(pg_sql.Identifier, columns)), pg_sql.Identifier(table))
            self.connection.execute(statement)

    def execute(self, sql, params=()):
        if sql == "BEGIN IMMEDIATE":
            self.connection.execute("BEGIN")
            return Result(self.connection.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_ID,)), self)
        match = re.match(r"\s*INSERT\s+INTO\s+(?:public\.)?(\w+)", sql, re.I)
        # PostgreSQL UPSERT requires a table with its own unique index, not a view.
        if match and self.scope and match[1] in SALON_TABLES and re.search(r"\bON\s+CONFLICT\b", sql, re.I):
            sql = re.sub(r"(\bINSERT\s+INTO\s+)(?:public\.)?" + re.escape(match[1]) + r"\b",
                         r"\1public." + match[1], sql, count=1, flags=re.I)
        returning_id = bool(match and match[1] in GENERATED_ID_TABLES and not re.search(r"\bRETURNING\b", sql, re.I))
        if returning_id:
            sql = sql.rstrip().rstrip(';') + " RETURNING id"
        if params:
            sql = sql.replace("?", "%s")
        return Result(self.connection.execute(sql, params if params else None), self, returning_id)

    def executemany(self, sql, params):
        sql = sql.replace("?", "%s")
        with self.connection.cursor() as cursor:
            cursor.executemany(sql, params)

    def commit(self):
        self.connection.commit()

    def rollback(self):
        self.connection.rollback()

    def close(self):
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        if exc_type:
            self.rollback()
        self.close()


def connect(url):
    return Database(url)
