"""Validated application namespace; SQL templates never infer a public fallback.

DATABASE_SCHEMA is captured by each connection. SQL/PLpgSQL templates use the
explicit __APP_SCHEMA__ token, including qualified regclass string literals.
It denotes a namespace only; PostgreSQL's PUBLIC role is never rewritten.
The selected schema must already exist. This module performs no database DDL.
"""
from __future__ import annotations

import os
import re

SCHEMA_TOKEN = "__APP_SCHEMA__"
IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def schema_name(value=None):
    name = os.environ.get("DATABASE_SCHEMA", "public") if value is None else value
    if not isinstance(name, str) or not IDENTIFIER.fullmatch(name):
        raise ValueError("DATABASE_SCHEMA must be a lowercase SQL identifier of at most 63 characters")
    if name.startswith("pg_") or name == "information_schema":
        raise ValueError("DATABASE_SCHEMA cannot use a PostgreSQL system namespace")
    return name


def quote_identifier(value):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ValueError("Invalid SQL identifier")
    return '"' + value + '"'


def render_sql(template, namespace=None):
    """Render module-owned templates, never parameter values or arbitrary names."""
    return template.replace(SCHEMA_TOKEN, quote_identifier(schema_name(namespace)))


def relation_name(db, table):
    """Qualified regclass/sequence input for catalog queries using bind values."""
    return quote_identifier(schema_name(getattr(db, "schema_name", None))) + "." + quote_identifier(table)
