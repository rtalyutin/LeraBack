"""Initialize or upgrade the schema without loading any salon catalog."""

import os
from contextlib import closing

from booking_core import connect, migrate


if __name__ == "__main__":
    with closing(connect(os.environ["DATABASE_URL"])) as db:
        migrate(db)
    print("Schema ready. Configure services, masters, rooms and hours in the admin constructor.")
