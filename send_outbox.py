"""One-shot M8 outgoing worker; schedule it only after explicit VK authorization."""

import argparse
import json
import os

from vk_gateway import deliver_pending
from vk_sender import VKSender


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=50)
    args = parser.parse_args()
    sender = VKSender(os.environ["VK_COMMUNITY_TOKEN"], os.environ.get("VK_API_VERSION", "5.199"),
                      master_photo_ids=json.loads(os.environ.get("VK_MASTER_PHOTO_IDS_JSON", "{}")))
    delivered = deliver_pending(os.environ["DATABASE_URL"], sender, limit=args.limit)
    print(f"Delivered: {delivered}")


if __name__ == "__main__":
    main()
