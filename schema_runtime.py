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
from database_namespace import quote_identifier

ROOT = Path(__file__).resolve().parent
CONTRACT = ROOT / "schema_contract.json"
SOURCES = ("schema_postgres.sql", "shared_schema.sql", "shared_schema.py",
           "booking_core.py:migrate", "vk_connections.py:upgrade_vk")

# Actual runtime SQL, including SECURITY INVOKER projection triggers. Migration
# history is read-only. Bootstrap templates and the global busy registry are
# accessed by SECURITY DEFINER functions, never directly by the runtime role.
READ = ("SELECT",)
APPEND = ("SELECT", "INSERT")  # INSERT ... RETURNING id needs SELECT too.
WRITE = ("SELECT", "INSERT", "UPDATE")
CRUD = ("SELECT", "INSERT", "UPDATE", "DELETE")
RUNTIME_TABLE_PRIVILEGES = {
    "schema_migrations": READ,
    "services": WRITE, "masters": WRITE, "rooms": WRITE,
    "master_services": ("SELECT", "INSERT", "DELETE"),
    "room_services": ("SELECT", "INSERT", "DELETE"),
    "work_intervals": ("SELECT", "INSERT", "DELETE"),
    "resource_blocks": APPEND, "clients": WRITE, "bookings": WRITE,
    "booking_history": APPEND, "vk_dialogs": CRUD, "inbound_events": WRITE,
    "action_results": APPEND, "message_outbox": WRITE,
    "vk_outgoing_messages": WRITE, "admin_users": WRITE,
    # Account creation uses the owner-security admin_users view. Direct accounts
    # SELECT/FOR UPDATE calls need SELECT/UPDATE, not INSERT on the base table.
    "accounts": ("SELECT", "UPDATE"),
    "admin_sessions": CRUD, "admin_audit_log": APPEND,
    "account_audit_log": APPEND, "salons": WRITE, "salon_memberships": WRITE,
    "shared_masters": APPEND, "entity_types": CRUD, "entities": CRUD,
    "entity_parameters": CRUD, "entity_parameter_values": CRUD,
    "salon_creation_requests": APPEND, "vk_connections": WRITE,
}
RUNTIME_FUNCTIONS = ("current_salon_id()", "shared_master_conflict(bigint,text,text,bigint)")

# Effective authority matters even though owner names are deliberately excluded
# from structural fingerprints. Include nested INVOKER triggers/row locks.
DEFINER_TABLE_PRIVILEGES = {
    "check_required_entity_values": {
        "entities": READ, "entity_parameters": READ, "entity_parameter_values": READ},
    "initialize_salon_metadata": {
        "core_entity_templates": READ, "entity_types": WRITE,
        "entity_parameters": WRITE, "entities": WRITE,
        "entity_parameter_values": APPEND, "salons": ("SELECT", "UPDATE")},
    "shared_master_conflict": {
        "masters": READ, "bookings": READ, "shared_master_busy": READ},
    "protect_booking_overlap": {
        "masters": READ, "bookings": READ, "shared_master_busy": READ},
    "sync_shared_master_busy": {"masters": READ, "shared_master_busy": CRUD},
}

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


def runtime_privilege_issues(db):
    """Inspect required operations only; return static names, never row data."""
    issues = []
    if not db.execute("SELECT has_database_privilege(current_user,current_database(),'TEMP')").fetchone()[0]:
        issues.append("database TEMPORARY")
    required = [{"relation": table, "privilege": privilege}
                for table, privileges in sorted(RUNTIME_TABLE_PRIVILEGES.items())
                for privilege in privileges]
    missing = db.execute("""SELECT r.relation,r.privilege FROM jsonb_to_recordset(?::jsonb)
        AS r(relation text,privilege text)
        LEFT JOIN pg_namespace n ON n.nspname=?
        LEFT JOIN pg_class c ON c.relnamespace=n.oid AND c.relname=r.relation
        AND c.relkind IN ('r','v','p')
        WHERE c.oid IS NULL OR NOT has_table_privilege(current_user,c.oid,r.privilege)
        ORDER BY r.relation,r.privilege""", (json.dumps(required), db.schema_name))
    for row in missing:
        # Only known requirements can reach the log, even on catalog drift.
        if row["privilege"] in RUNTIME_TABLE_PRIVILEGES.get(row["relation"], ()):
            issues.append(f'{row["relation"]} {row["privilege"]}')
    # Identity defaults allocate internally without a sequence ACL check.
    # Only serial/default nextval callers need USAGE or UPDATE; runtime inserts
    # retrieve IDs through RETURNING, never currval/lastval.
    insert_tables = [table for table, privileges in RUNTIME_TABLE_PRIVILEGES.items()
                     if "INSERT" in privileges]
    sequences = db.execute("""SELECT DISTINCT t.relname AS relation FROM pg_class s
        JOIN pg_namespace n ON n.oid=s.relnamespace
        JOIN pg_depend d ON d.classid='pg_class'::regclass AND d.objid=s.oid
        AND d.refclassid='pg_class'::regclass AND d.deptype IN ('a','i')
        JOIN pg_class t ON t.oid=d.refobjid AND t.relnamespace=n.oid
        JOIN pg_attribute a ON a.attrelid=t.oid AND a.attnum=d.refobjsubid
        WHERE n.nspname=? AND s.relkind='S' AND t.relname=ANY(?::text[])
        AND a.attidentity=''
        AND NOT has_sequence_privilege(current_user,s.oid,'USAGE,UPDATE')
        ORDER BY t.relname""", (db.schema_name, insert_tables))
    for row in sequences:
        if row["relation"] in insert_tables:
            issues.append(f'{row["relation"]} sequence USAGE or UPDATE')
    for signature in RUNTIME_FUNCTIONS:
        qualified = quote_identifier(db.schema_name) + "." + signature
        allowed = db.execute("""SELECT COALESCE(has_function_privilege(
            current_user,to_regprocedure(?),'EXECUTE'),false)""", (qualified,)).fetchone()[0]
        if not allowed:
            issues.append(f"{signature} EXECUTE")
    return issues


def owner_privilege_issues(db):
    """Check existing definer/view authority without granting runtime access."""
    issues = []
    required = [{"function": function, "relation": table, "privilege": privilege}
                for function, tables in DEFINER_TABLE_PRIVILEGES.items()
                for table, privileges in tables.items() for privilege in privileges]
    missing = db.execute("""SELECT DISTINCT r.function,r.relation,r.privilege
        FROM jsonb_to_recordset(?::jsonb) AS r(function text,relation text,privilege text)
        LEFT JOIN pg_namespace n ON n.nspname=?
        LEFT JOIN pg_proc p ON p.pronamespace=n.oid AND p.proname=r.function
        AND p.prokind='f' AND p.prosecdef
        LEFT JOIN pg_class c ON c.relnamespace=n.oid AND c.relname=r.relation
        AND c.relkind IN ('r','p')
        WHERE p.oid IS NULL OR c.oid IS NULL
        OR NOT has_table_privilege(p.proowner,c.oid,r.privilege)
        ORDER BY r.function,r.relation,r.privilege""", (json.dumps(required), db.schema_name))
    for row in missing:
        if row["privilege"] in DEFINER_TABLE_PRIVILEGES.get(row["function"], {}).get(row["relation"], ()):
            issues.append(f'{row["function"]} owner {row["relation"]} {row["privilege"]}')
    owners = db.execute("""SELECT DISTINCT p.proname AS function,
        has_schema_privilege(p.proowner,n.oid,'USAGE') AS schema_usage,
        (p.proname IN ('shared_master_conflict','protect_booking_overlap')
          OR NOT (r.rolsuper OR r.rolbypassrls)) AS needs_scope_function,
        COALESCE(has_function_privilege(p.proowner,to_regprocedure(?),'EXECUTE'),false) AS scope_execute
        FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
        JOIN pg_roles r ON r.oid=p.proowner
        WHERE n.nspname=? AND p.proname=ANY(?::text[])
        AND p.prokind='f' AND p.prosecdef ORDER BY p.proname""",
        (quote_identifier(db.schema_name) + ".current_salon_id()", db.schema_name,
         list(DEFINER_TABLE_PRIVILEGES)))
    for row in owners:
        if row["function"] not in DEFINER_TABLE_PRIVILEGES:
            continue
        if not row["schema_usage"]:
            issues.append(f'{row["function"]} owner schema USAGE')
        if row["needs_scope_function"] and not row["scope_execute"]:
            issues.append(f'{row["function"]} owner current_salon_id() EXECUTE')
    missing_view = db.execute("""SELECT privilege FROM pg_class v
        JOIN pg_namespace n ON n.oid=v.relnamespace
        JOIN pg_class t ON t.relnamespace=n.oid AND t.relname='accounts'
        CROSS JOIN (VALUES ('SELECT'),('INSERT'),('UPDATE')) required(privilege)
        WHERE n.nspname=? AND v.relname='admin_users' AND v.relkind='v'
        AND NOT has_table_privilege(v.relowner,t.oid,privilege)
        ORDER BY privilege""", (db.schema_name,))
    for row in missing_view:
        if row["privilege"] in WRITE:
            issues.append(f'admin_users view owner accounts {row["privilege"]}')
    invoker_view = db.execute("""SELECT 1 FROM pg_class v
        JOIN pg_namespace n ON n.oid=v.relnamespace
        WHERE n.nspname=? AND v.relname='admin_users' AND v.relkind='v'
        AND COALESCE((SELECT option_value::boolean FROM pg_options_to_table(v.reloptions)
                      WHERE option_name='security_invoker'),false)""", (db.schema_name,)).fetchone()
    if invoker_view:
        issues.append("admin_users view requires owner security (security_invoker=false)")
    return issues


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
        privilege_issues = runtime_privilege_issues(db) + owner_privilege_issues(db)
        actual = catalog_fingerprints(db)
        unenforced_not_null = db.execute("""SELECT 1 FROM pg_constraint k
            JOIN pg_class c ON c.oid=k.conrelid
            JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname=? AND k.contype='n'
            AND COALESCE((to_jsonb(k)->>'conenforced')::boolean,true)=false LIMIT 1""",
            (db.schema_name,)).fetchone()
        if set(expected["catalog"]) != set(QUERIES):
            raise RuntimeError("Release schema contract is incomplete")
        mismatches = [section for section, digest in expected["catalog"].items()
                      if actual[section] != digest]
        if unenforced_not_null:
            mismatches.append("NOT NULL enforcement")
        errors = []
        if privilege_issues:
            errors.append("Database access check failed: " + "; ".join(privilege_issues))
        if mismatches:
            errors.append("Database schema contract mismatch: " + ", ".join(mismatches)
                          + "; run initialize.py with the migration role")
        if errors:
            # One failure reports every observed category; no function bodies,
            # database URLs, credentials or application rows are included.
            raise RuntimeError(" | ".join(errors))
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
