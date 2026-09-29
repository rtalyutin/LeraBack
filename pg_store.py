"""Small DB adapter for the existing booking core on PostgreSQL.

All mutations that use BEGIN IMMEDIATE take one transaction-scoped advisory
lock. This preserves the prototype's serialized read/check/write behavior
while database triggers protect bookings written by other consumers.
"""

from __future__ import annotations

import psycopg


LOCK_ID = 706547229101


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
    def __init__(self, cursor, database):
        self.cursor = cursor
        self.database = database

    def fetchone(self):
        return self.cursor.fetchone()

    def fetchall(self):
        return self.cursor.fetchall()

    def __iter__(self):
        return iter(self.cursor)

    @property
    def lastrowid(self):
        return self.database.connection.execute("SELECT lastval()").fetchone()[0]


class Database:
    def __init__(self, url):
        if not str(url).startswith(("postgresql://", "postgres://")):
            raise ValueError("DATABASE_URL must point to PostgreSQL")
        self.connection = psycopg.connect(url, autocommit=True, row_factory=row_factory)

    def execute(self, sql, params=()):
        if sql == "BEGIN IMMEDIATE":
            self.connection.execute("BEGIN")
            return Result(self.connection.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_ID,)), self)
        sql = sql.replace("?", "%s")
        return Result(self.connection.execute(sql, params if params else None), self)

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
