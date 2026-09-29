"""One backend service: admin API, VK Callback API and outgoing worker."""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone

from admin_http import Handler as AdminHandler, server
from vk_gateway import CallbackGateway, deliver_pending
from vk_sender import VKSender


class Handler(AdminHandler):
    def do_POST(self):
        if self.path != "/vk/callback":
            return super().do_POST()
        if self.server.gateway is None:
            return self._json(503, {"error": "vk_not_configured"})
        try:
            if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
                return self._json(415, {"error": "json_required"})
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 256_000:
                return self._json(413, {"error": "invalid_size"})
            event = json.loads(self.rfile.read(length))
            result = self.server.gateway.handle(event)
            raw = result.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        except PermissionError:
            return self._json(403, {"error": "forbidden"})
        except (ValueError, KeyError, TypeError, json.JSONDecodeError):
            return self._json(422, {"error": "invalid_event"})
        except Exception:
            return self._json(500, {"error": "retry"})


def outgoing_loop(database_url, sender, stop):
    while not stop.is_set():
        try:
            deliver_pending(database_url, sender, limit=20, now=datetime.now(timezone.utc))
        except Exception as exc:
            print(f"Outgoing worker error: {type(exc).__name__}", flush=True)
        stop.wait(5)


def main():
    database_url = os.environ["DATABASE_URL"]
    policy = json.loads(os.environ["SALON_POLICY_JSON"])
    vk_keys = ("VK_GROUP_ID", "VK_CALLBACK_SECRET", "VK_CONFIRMATION_CODE")
    if any(os.environ.get(key) for key in vk_keys) and not all(os.environ.get(key) for key in vk_keys):
        raise RuntimeError("Incomplete VK callback configuration")
    gateway = (CallbackGateway(database_url, policy, int(os.environ["VK_GROUP_ID"]),
                               os.environ["VK_CALLBACK_SECRET"], os.environ["VK_CONFIRMATION_CODE"])
               if all(os.environ.get(key) for key in vk_keys) else None)
    stop = threading.Event()
    with server(database_url, policy, os.environ["SALON_CSRF_SECRET"], host="0.0.0.0",
                port=int(os.environ.get("PORT", "8080")), handler_class=Handler) as httpd:
        httpd.gateway = gateway
        if gateway and os.environ.get("VK_COMMUNITY_TOKEN"):
            sender = VKSender(os.environ["VK_COMMUNITY_TOKEN"], os.environ.get("VK_API_VERSION", "5.199"),
                              master_photo_ids=json.loads(os.environ.get("VK_MASTER_PHOTO_IDS_JSON", "{}")))
            threading.Thread(target=outgoing_loop, args=(database_url, sender, stop), daemon=True).start()
        try:
            httpd.serve_forever()
        finally:
            stop.set()


if __name__ == "__main__":
    main()
