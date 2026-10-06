"""Repeatable first-run preparation; bootstrap never rotates existing credentials."""

from contextlib import closing

from admin_auth import create_or_update_admin
from booking_core import connect, migrate
from schema_runtime import schema_version, verify_runtime_schema


def prepare_database(database_url, username="salon_admin", password=None, *, mode="migrate"):
    if mode not in {"auto", "verify", "migrate"}:
        raise RuntimeError("DATABASE_STARTUP_MODE must be auto, verify or migrate")
    with closing(connect(database_url)) as db:
        if mode == "migrate":
            migrate(db)
        else:
            version = schema_version(db) or 0
            if mode == "auto" and version < 5:
                if version == 0 and db.execute("""SELECT 1 FROM pg_class c
                    JOIN pg_namespace n ON n.oid=c.relnamespace
                    WHERE n.nspname=? AND c.relkind IN ('r','v','p') LIMIT 1""",
                    (db.schema_name,)).fetchone():
                    raise RuntimeError("Unversioned existing schema requires developer migration; run initialize.py")
                migrate(db)
            verify_runtime_schema(db)
        users = list(db.execute("SELECT id,active FROM admin_users"))
    if users:
        active = [user for user in users if user["active"]]
        if not active:
            raise RuntimeError("An active administrator is required; use developer recovery")
        return active[0]["id"]
    if mode == "verify":
        raise RuntimeError("Verify startup requires an existing active administrator; use developer provisioning")
    if not password:
        raise RuntimeError("First launch requires SALON_ADMIN_PASSWORD (12–256 characters)")
    if password == "REPLACE_WITH_YOUR_12_TO_256_CHARACTER_PASSWORD":
        raise RuntimeError("Replace the SALON_ADMIN_PASSWORD example before first launch")
    # Recheck inside the provisioning transaction in case two instances start together.
    return create_or_update_admin(database_url, username, password, initial_only=True)
