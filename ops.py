"""Read-only health checks for the managed PostgreSQL database."""

from __future__ import annotations

import argparse
import json
import os
from contextlib import closing

from booking_core import connect

REQUIRED_TABLES = {
    "schema_migrations", "services", "masters", "rooms", "bookings",
    "admin_users", "admin_sessions", "inbound_events", "vk_outgoing_messages",
    "accounts", "salons", "salon_memberships", "shared_masters", "shared_master_busy",
    "entity_types", "entities", "entity_parameters", "entity_parameter_values",
    "vk_connections",
    "salon_creation_requests",
}


def database_health(url):
    try:
        with closing(connect(url)) as db:
            db.execute("SELECT 1").fetchone()
            version = db.execute("SELECT max(version) FROM __APP_SCHEMA__.schema_migrations").fetchone()[0]
            if version != 5:
                return {"status": "error"}
        return {"status": "ok", "schema_version": version}
    except Exception:
        return {"status": "error"}


def verify_database(url):
    with closing(connect(url)) as db:
        tables = {r[0] for r in db.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema=?", (db.schema_name,)
        )}
        missing = REQUIRED_TABLES - tables
        if missing:
            raise ValueError(f"Missing required tables: {', '.join(sorted(missing))}")
        version = db.execute("SELECT max(version) FROM __APP_SCHEMA__.schema_migrations").fetchone()[0]
        if version != 5:
            raise ValueError("Database schema version 5 is required by this backend")
    return {"status": "ok", "schema_version": version}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("health", "verify"))
    args = parser.parse_args()
    database_url = os.environ["DATABASE_URL"]
    result = database_health(database_url) if args.command == "health" else verify_database(database_url)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
