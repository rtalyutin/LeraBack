"""One-shot M8 outgoing worker; schedule it only after explicit VK authorization."""

import argparse
import json
import os

from vk_gateway import deliver_pending
from vk_sender import VKSender
from vk_config import integrations
from pg_store import SalonScope, connect
from constructor_store import get_policy
from vk_connections import VKConnections, salon_lock


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--salon-id", type=int, required=True)
    parser.add_argument("--group-id", type=int, required=True)
    args = parser.parse_args()
    binding = next((b for b in integrations(os.environ) if b["salon_id"] == args.salon_id
                    and b["group_id"] == args.group_id and b.get("token")), None)
    if binding is None:
        raise ValueError("No matching configured VK binding with a token")
    scope = SalonScope(os.environ["DATABASE_URL"], args.salon_id)
    with connect(scope) as db:
        get_policy(db)
    sender = VKSender(binding["token"], binding.get("api_version", "5.199"),
                      master_photo_ids=binding.get("master_photo_ids", {}))
    service = VKConnections(scope.url)
    with connect(scope.url) as guard, salon_lock(guard, scope.salon_id):
        if service.managed(scope.salon_id):
            raise ValueError("This salon uses the connection wizard; legacy sending is disabled")
        delivered = deliver_pending(scope, sender, limit=args.limit)
    print(f"Delivered: {delivered}")


if __name__ == "__main__":
    main()
