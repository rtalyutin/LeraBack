"""Provision or rotate the administrator without putting a password in argv."""

import argparse
import getpass
import os

from admin_auth import create_or_update_admin


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("username")
    args = parser.parse_args()
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise SystemExit("DATABASE_URL is required")
    password = os.environ.pop("SALON_ADMIN_PASSWORD", None) or getpass.getpass("New password: ")
    user_id = create_or_update_admin(database_url, args.username, password)
    print(f"Administrator #{user_id} provisioned; existing sessions revoked")


if __name__ == "__main__":
    main()
