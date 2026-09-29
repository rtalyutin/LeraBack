"""Load the approved synthetic catalog into an empty PostgreSQL database."""

import json
from pathlib import Path

from booking_core import connect, migrate

STARTER = Path(__file__).with_name("starter_data.json")


def seed(path):
    data = json.loads(STARTER.read_text(encoding="utf-8"))
    if data["status"] != "approved_synthetic_only":
        raise ValueError("Expected synthetic starter data")
    with connect(path) as db:
        if db.execute("SELECT to_regclass('public.services')").fetchone()[0] is not None:
            raise ValueError("Starter data requires a fresh empty database")
        migrate(db)
        db.execute("BEGIN IMMEDIATE")
        try:
            if any(db.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
                   for table in ("services", "masters", "rooms", "bookings")):
                raise ValueError("Starter data may be loaded into an empty database only")
            db.executemany("INSERT INTO services(id,name,duration_minutes) VALUES (?,?,?)",
                           [(s["id"], s["name"], s["duration_minutes"]) for s in data["services"]])
            db.executemany("INSERT INTO masters(id,name) VALUES (?,?)",
                           [(m["id"], m["name"]) for m in data["masters"]])
            db.executemany("INSERT INTO rooms(id,name) VALUES (?,?)",
                           [(r["id"], r["name"]) for r in data["rooms"]])
            for table in ("services", "masters", "rooms"):
                db.execute(f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                           f"(SELECT max(id) FROM {table}), true)")
            db.executemany("INSERT INTO master_services VALUES (?,?)",
                           [(m["id"], sid) for m in data["masters"] for sid in m["service_ids"]])
            db.executemany("INSERT INTO room_services VALUES (?,?)",
                           [(r["id"], sid) for r in data["rooms"] for sid in r["service_ids"]])
            schedule = data["schedule"]
            for weekday in schedule["weekdays"]:
                for kind, resources in (("master", data["masters"]), ("room", data["rooms"])):
                    for resource in resources:
                        db.execute(
                            f"INSERT INTO work_intervals({kind}_id,weekday,mode,start_minute,end_minute) VALUES (?,?,'open',?,?)",
                            (resource["id"], weekday, schedule["start_minute"], schedule["end_minute"]),
                        )
            db.commit()
        except Exception:
            db.rollback()
            raise


if __name__ == "__main__":
    import os
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise SystemExit("DATABASE_URL is required")
    seed(database_url)
    print("Synthetic starter data loaded")
