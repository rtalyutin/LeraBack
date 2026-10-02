"""Developer-only provisioning. No public salon/account registration endpoint."""
import argparse
import json
import os
from pathlib import Path

from pg_store import connect
from salon_service import grant_membership
from shared_schema import provision_salon


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create")
    create.add_argument("name")
    create.add_argument("--user-id", type=int, required=True)
    for command in ("grant", "revoke"):
        sub = commands.add_parser(command)
        sub.add_argument("salon_id", type=int)
        sub.add_argument("user_id", type=int)
    args = parser.parse_args()
    with connect(os.environ["DATABASE_URL"]) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            if args.command == "create":
                policy = json.loads(Path(__file__).with_name("starter_data.json").read_text())["policy"]
                salon_id = provision_salon(db, args.name, policy)
                grant_membership(db, args.user_id, salon_id)
            else:
                salon_id = args.salon_id
                grant_membership(db, args.user_id, salon_id, args.command == "grant")
            db.commit()
        except Exception:
            db.rollback()
            raise
    print(f"Salon #{salon_id}: {args.command} complete")


if __name__ == "__main__":
    main()
