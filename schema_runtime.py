"""Read-only compatibility gate for a database already migrated to this release.

The checked-in contract contains catalog metadata only, never application rows,
owners, OIDs or sequence counters. Refresh it on a disposable migrated database
when changing the migrations. A mismatch requires initialize.py; it must never
be treated as an excuse to skip guards or ignore a permission error.
"""

from __future__ import annotations

import hashlib
import json
import ast
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent
CONTRACT = ROOT / "schema_contract.json"
SOURCES = ("schema_postgres.sql", "shared_schema.sql", "shared_schema.py",
           "booking_core.py:migrate", "vk_connections.py:upgrade_vk")

# These SELECTs intentionally use only catalog columns present in PostgreSQL 16.
# PG18 stores table NOT NULL constraints in pg_constraint too; attnotnull is
# their portable representation. Constraint-trigger metadata is checked below.
QUERIES = {
    "relations": """SELECT c.relname AS name,c.relkind AS kind,
        c.relrowsecurity AS rls,c.relforcerowsecurity AS force_rls,
        CASE WHEN c.relkind='v' THEN pg_get_viewdef(c.oid,false) END AS view
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname=? AND c.relkind IN ('r','v','p') ORDER BY c.relname""",
    "columns": """SELECT c.relname AS relation,a.attname AS name,
        format_type(a.atttypid,a.atttypmod) AS type,a.attnotnull AS required,
        a.attidentity AS identity,a.attgenerated AS generated,
        pg_get_expr(d.adbin,d.adrelid,false) AS default
        FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace
        LEFT JOIN pg_attrdef d ON d.adrelid=c.oid AND d.adnum=a.attnum
        WHERE n.nspname=? AND c.relkind IN ('r','v','p')
        AND a.attnum>0 AND NOT a.attisdropped ORDER BY c.relname,a.attnum""",
    "constraints": """SELECT c.relname AS relation,k.conname AS name,
        k.contype AS kind,k.condeferrable AS deferrable,k.condeferred AS deferred,
        k.convalidated AS validated,
        COALESCE((to_jsonb(k)->>'conenforced')::boolean,true) AS enforced,
        pg_get_constraintdef(k.oid,false) AS definition
        FROM pg_constraint k JOIN pg_class c ON c.oid=k.conrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname=? AND k.contype IN ('c','f','p','u','x')
        ORDER BY c.relname,k.conname""",
    "indexes": """SELECT c.relname AS relation,i.relname AS name,
        x.indisvalid AS valid,x.indisready AS ready,
        pg_get_indexdef(x.indexrelid,0,false) AS definition
        FROM pg_index x JOIN pg_class c ON c.oid=x.indrelid
        JOIN pg_class i ON i.oid=x.indexrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname=? ORDER BY c.relname,i.relname""",
    "functions": """SELECT p.proname AS name,
        pg_get_function_identity_arguments(p.oid) AS arguments,
        pg_get_function_result(p.oid) AS result,l.lanname AS language,
        p.prosrc AS body,p.proconfig AS config,p.provolatile AS volatility,
        p.prosecdef AS security_definer,p.proisstrict AS strict,
        p.proleakproof AS leakproof,p.proparallel AS parallel,
        pg_get_expr(p.proargdefaults,0,false) AS defaults
        FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
        JOIN pg_language l ON l.oid=p.prolang
        WHERE n.nspname=? AND p.prokind='f' ORDER BY p.proname,arguments""",
    "triggers": """SELECT c.relname AS relation,t.tgname AS name,
        t.tgtype AS type,t.tgenabled AS enabled,t.tgdeferrable AS deferrable,
        t.tginitdeferred AS deferred,p.proname AS function,
        pn.nspname AS function_schema,encode(t.tgargs,'hex') AS arguments,
        pg_get_triggerdef(t.oid,false) AS definition
        FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace
        JOIN pg_proc p ON p.oid=t.tgfoid
        JOIN pg_namespace pn ON pn.oid=p.pronamespace
        WHERE n.nspname=? AND NOT t.tgisinternal ORDER BY c.relname,t.tgname""",
    "policies": """SELECT c.relname AS relation,p.polname AS name,
        p.polcmd AS command,p.polpermissive AS permissive,
        ARRAY(SELECT CASE WHEN role_id=0 THEN 'PUBLIC' ELSE pg_get_userbyid(role_id)::text END
              FROM unnest(p.polroles) role_id ORDER BY role_id) AS roles,
        pg_get_expr(p.polqual,p.polrelid,false) AS using,
        pg_get_expr(p.polwithcheck,p.polrelid,false) AS check
        FROM pg_policy p JOIN pg_class c ON c.oid=p.polrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname=? ORDER BY c.relname,p.polname""",
    "internal_triggers": """SELECT c.relname AS relation,cc.relname AS constraint_relation,
        k.conname AS constraint_name,t.tgtype AS type,t.tgenabled AS enabled,
        t.tgdeferrable AS deferrable,t.tginitdeferred AS deferred,
        pn.nspname AS function_schema,p.proname AS function
        FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace
        JOIN pg_constraint k ON k.oid=t.tgconstraint
        JOIN pg_class cc ON cc.oid=k.conrelid
        JOIN pg_proc p ON p.oid=t.tgfoid
        JOIN pg_namespace pn ON pn.oid=p.pronamespace
        WHERE n.nspname=? AND t.tgisinternal
        ORDER BY c.relname,cc.relname,k.conname,p.proname,t.tgtype""",
    "sequences": """SELECT c.relname AS name,format_type(s.seqtypid,NULL) AS type,
        s.seqstart AS start,s.seqincrement AS increment,s.seqmax AS max,
        s.seqmin AS min,s.seqcache AS cache,s.seqcycle AS cycle
        FROM pg_sequence s JOIN pg_class c ON c.oid=s.seqrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname=? ORDER BY c.relname""",
}


def source_hashes():
    result = {}
    for name in SOURCES:
        filename, _, function = name.partition(":")
        content = ROOT.joinpath(filename).read_text(encoding="utf-8")
        if function:
            node = next(node for node in ast.parse(content).body
                        if isinstance(node, ast.FunctionDef) and node.name == function)
            content = ast.get_source_segment(content, node)
        result[name] = hashlib.sha256(content.encode()).hexdigest()
    return result


def _normalize(value, namespace):
    if isinstance(value, str):
        if "__APP_SCHEMA__" in value:
            # A raw function referring to this nonexistent namespace must not
            # collide with the normalized fingerprint of a valid reference.
            raise RuntimeError("Database schema contract mismatch: reserved namespace marker")
        # Replace qualified namespace identifiers only. Never strip whitespace
        # or replace a bare word inside PL/pgSQL literals or identifiers.
        name = re.escape(namespace)
        value = re.sub(r'(?<![\w"])(?:"' + name + r'"|' + name + r')\.',
                       "__APP_SCHEMA__.", value)
        if value in (f"search_path=pg_catalog, {namespace}",
                     f'search_path=pg_catalog, "{namespace}"'):
            value = "search_path=pg_catalog, __APP_SCHEMA__"
        return value
    if isinstance(value, list):
        return [_normalize(item, namespace) for item in value]
    if isinstance(value, dict):
        return {key: ("__APP_SCHEMA__" if key == "function_schema" and item == namespace
                      else _normalize(item, namespace)) for key, item in value.items()}
    return value


def catalog_contract(db):
    return {section: [_normalize(dict(row), db.schema_name)
                      for row in db.execute(query, (db.schema_name,))]
            for section, query in QUERIES.items()}


def catalog_fingerprints(db):
    return {section: hashlib.sha256(json.dumps(rows, ensure_ascii=False, sort_keys=True,
                                               separators=(",", ":")).encode()).hexdigest()
            for section, rows in catalog_contract(db).items()}


def schema_version(db):
    exists = db.execute("SELECT to_regclass('__APP_SCHEMA__.schema_migrations')").fetchone()[0]
    return db.execute("SELECT max(version) FROM __APP_SCHEMA__.schema_migrations").fetchone()[0] if exists else 0


def verify_runtime_schema(db):
    """Check one stable catalog snapshot, with no DDL and no application writes."""
    expected = json.loads(CONTRACT.read_text(encoding="utf-8"))
    if expected.get("format") != 1 or expected.get("sources") != source_hashes():
        raise RuntimeError("Release schema contract is stale; regenerate it on a disposable database")
    try:
        db.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
        if schema_version(db) != expected["schema_version"]:
            raise RuntimeError("Database schema version 5 is required; run initialize.py with the migration role")
        role = db.execute("SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user").fetchone()
        if not role or role["rolsuper"] or role["rolbypassrls"]:
            raise RuntimeError("Runtime database role must be NOSUPERUSER NOBYPASSRLS")
        if not db.execute("SELECT has_database_privilege(current_user,current_database(),'TEMP')").fetchone()[0]:
            raise RuntimeError("Runtime database role needs TEMPORARY for salon-scoped views")
        missing = db.execute("""SELECT 1 FROM pg_class c
            JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname=? AND (
              (c.relkind IN ('r','v') AND (
                NOT has_table_privilege(current_user,c.oid,'SELECT') OR
                NOT has_table_privilege(current_user,c.oid,'INSERT') OR
                NOT has_table_privilege(current_user,c.oid,'UPDATE') OR
                NOT has_table_privilege(current_user,c.oid,'DELETE'))) OR
              (c.relkind='S' AND NOT has_sequence_privilege(current_user,c.oid,'USAGE')))
            LIMIT 1""", (db.schema_name,)).fetchone()
        if missing:
            raise RuntimeError("Runtime database role needs table DML and sequence USAGE in the application schema")
        missing_function = db.execute("""SELECT 1 FROM pg_proc p
            JOIN pg_namespace n ON n.oid=p.pronamespace
            WHERE n.nspname=? AND p.prokind='f'
            AND NOT has_function_privilege(current_user,p.oid,'EXECUTE') LIMIT 1""",
            (db.schema_name,)).fetchone()
        if missing_function:
            raise RuntimeError("Runtime database role needs EXECUTE on application functions")
        actual = catalog_fingerprints(db)
        unenforced_not_null = db.execute("""SELECT 1 FROM pg_constraint k
            JOIN pg_class c ON c.oid=k.conrelid
            JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname=? AND k.contype='n'
            AND COALESCE((to_jsonb(k)->>'conenforced')::boolean,true)=false LIMIT 1""",
            (db.schema_name,)).fetchone()
        if unenforced_not_null:
            raise RuntimeError("Database schema contract mismatch: NOT NULL enforcement")
        if set(expected["catalog"]) != set(QUERIES):
            raise RuntimeError("Release schema contract is incomplete")
        for section, digest in expected["catalog"].items():
            if actual[section] != digest:
                # Do not print function bodies, role identifiers or database URLs.
                raise RuntimeError(f"Database schema contract mismatch: {section}; run initialize.py with the migration role")
        db.commit()
    except Exception:
        db.rollback()
        raise


if __name__ == "__main__":
    # Developer-only, read-only capture. The target must already be a freshly
    # migrated disposable database, not production or a schema needing repairs.
    import argparse
    import os
    from contextlib import closing
    from pg_store import connect
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("record",))
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    with closing(connect(os.environ["DATABASE_URL"])) as db:
        if schema_version(db) != 5:
            raise RuntimeError("Record requires a migrated version-5 disposable database")
        payload = {"format": 1,"schema_version": 5,"sources": source_hashes(),"catalog": catalog_fingerprints(db)}
    Path(args.output).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
