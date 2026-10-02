"""Deployment defaults; only connection and bootstrap credentials are required."""

import json
import os
from pathlib import Path

from booking_core import validate_policy


def initial_policy():
    """Use the migration's agreed defaults; existing salon settings stay in the DB."""
    supplied = os.environ.get("SALON_POLICY_JSON")
    if supplied is None:
        policy = json.loads(Path(__file__).with_name("starter_data.json").read_text())["policy"]
    else:
        policy = json.loads(supplied)
    validate_policy(policy)
    return policy


def optional_csrf_secret():
    """Retain an explicit legacy key; otherwise use each session's random key."""
    supplied = os.environ.get("SALON_CSRF_SECRET")
    if supplied is None:
        return None
    if len(supplied.encode()) < 32 or supplied == "REPLACE_WITH_32_OR_MORE_RANDOM_BYTES":
        raise RuntimeError("SALON_CSRF_SECRET must contain at least 32 bytes when supplied")
    return supplied.encode()
