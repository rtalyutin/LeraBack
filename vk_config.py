"""Private environment bindings: one VK community belongs to one salon."""
import json


def integrations(environ):
    legacy = ("VK_GROUP_ID", "VK_CALLBACK_SECRET", "VK_CONFIRMATION_CODE", "VK_COMMUNITY_TOKEN")
    encoded = environ.get("VK_INTEGRATIONS_JSON")
    if encoded and any(environ.get(key) for key in legacy):
        raise ValueError("Use either VK_INTEGRATIONS_JSON or legacy VK variables")
    if encoded:
        rows = json.loads(encoded)
        if not isinstance(rows, list):
            raise ValueError("VK_INTEGRATIONS_JSON must be an array")
    elif any(environ.get(key) for key in legacy):
        rows = [{"salon_id": int(environ.get("VK_SALON_ID", "1")),
                 "group_id": int(environ.get("VK_GROUP_ID", "0")),
                 "secret": environ.get("VK_CALLBACK_SECRET"),
                 "confirmation_code": environ.get("VK_CONFIRMATION_CODE"),
                 "token": environ.get("VK_COMMUNITY_TOKEN"),
                 "api_version": environ.get("VK_API_VERSION", "5.199"),
                 "master_photo_ids": json.loads(environ.get("VK_MASTER_PHOTO_IDS_JSON", "{}"))}]
    else:
        rows = []
    groups, salons = set(), set()
    for row in rows:
        if not isinstance(row, dict) or any(type(row.get(key)) is not int or row[key] <= 0 for key in ("salon_id", "group_id")):
            raise ValueError("VK binding requires positive salon_id and group_id")
        if any(not isinstance(row.get(key), str) or not row[key] for key in ("secret", "confirmation_code")):
            raise ValueError("Incomplete VK callback configuration")
        if row["group_id"] in groups:
            raise ValueError("VK group must have exactly one salon binding")
        if row["salon_id"] in salons:
            # Dialog/event/outgoing namespaces are salon-local. Multiple communities
            # in one salon would require community ownership on those records too.
            raise ValueError("Each salon supports one VK community binding")
        if not isinstance(row.get("master_photo_ids", {}), dict):
            raise ValueError("master_photo_ids must be an object")
        if row.get("token") is not None and not isinstance(row["token"], str):
            raise ValueError("Invalid VK token")
        groups.add(row["group_id"])
        salons.add(row["salon_id"])
    return rows
