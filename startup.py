"""Repeatable first-run preparation; bootstrap never rotates existing credentials."""

from contextlib import closing

from admin_auth import create_or_update_admin
from booking_core import connect, migrate


def prepare_database(database_url, username="salon_admin", password=None):
    with closing(connect(database_url)) as db:
        migrate(db)
        users = list(db.execute("SELECT id,active FROM admin_users"))
    if users:
        active = [user for user in users if user["active"]]
        if len(active) != 1:
            raise RuntimeError("Exactly one active administrator is required; use developer recovery")
        return active[0]["id"]
    if not password:
        raise RuntimeError("First launch requires SALON_ADMIN_PASSWORD (12–256 characters)")
    if password == "REPLACE_WITH_YOUR_12_TO_256_CHARACTER_PASSWORD":
        raise RuntimeError("Replace the SALON_ADMIN_PASSWORD example before first launch")
    # Recheck inside the provisioning transaction in case two instances start together.
    return create_or_update_admin(database_url, username, password, initial_only=True)
