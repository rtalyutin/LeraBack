"""One backend service: admin API, VK Callback API and outgoing worker."""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone

from admin_http import Handler as AdminHandler, server
from booking_core import validate_policy
from startup import prepare_database
from vk_gateway import CallbackGateway, deliver_pending
from vk_sender import VKSender
from vk_config import integrations
from pg_store import SalonScope, connect
from constructor_store import get_policy


class Handler(AdminHandler):
    def do_POST(self):
        if self.path != "/vk/callback":
            return super().do_POST()
        if not self.server.gateways:
            return self._json(503, {"error": "vk_not_configured"})
        try:
            if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
                return self._json(415, {"error": "json_required"})
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 256_000:
                return self._json(413, {"error": "invalid_size"})
            event = json.loads(self.rfile.read(length))
            if not isinstance(event, dict) or type(event.get("group_id")) is not int:
                raise ValueError("group_id required")
            gateway = self.server.gateways.get(event["group_id"])
            if gateway is None:
                raise PermissionError("Unknown VK community")
            with connect(gateway.db_path) as db:
                current_policy = get_policy(db)
            gateway = CallbackGateway(gateway.db_path, current_policy, gateway.group_id,
                                      gateway.secret, gateway.confirmation_code)
            result = gateway.handle(event)
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
    validate_policy(policy)
    csrf_secret = os.environ["SALON_CSRF_SECRET"]
    if len(csrf_secret.encode()) < 32 or csrf_secret == "REPLACE_WITH_32_OR_MORE_RANDOM_BYTES":
        raise RuntimeError("SALON_CSRF_SECRET must contain at least 32 bytes")
    port = int(os.environ.get("PORT", "8080"))
    if not 1 <= port <= 65535:
        raise RuntimeError("PORT must be between 1 and 65535")
    try:
        bindings = integrations(os.environ)
    except (ValueError, TypeError) as exc:
        raise RuntimeError("Invalid VK integration configuration") from exc
    prepare_database(database_url, os.environ.get("SALON_ADMIN_USERNAME", "salon_admin"),
                     os.environ.pop("SALON_ADMIN_PASSWORD", None))
    print("Database initialized; administrator ready", flush=True)
    gateways = {}
    for binding in bindings:
        scope = SalonScope(database_url, binding["salon_id"])
        with connect(scope) as db:
            salon_policy = get_policy(db)
        gateways[binding["group_id"]] = CallbackGateway(scope, salon_policy, binding["group_id"],
                                                       binding["secret"], binding["confirmation_code"])
    stop = threading.Event()
    with server(database_url, policy, csrf_secret, host="0.0.0.0",
                port=port, handler_class=Handler) as httpd:
        httpd.gateways = gateways
        for binding in bindings:
            if binding.get("token"):
                sender = VKSender(binding["token"], binding.get("api_version", "5.199"),
                                  master_photo_ids=binding.get("master_photo_ids", {}))
                threading.Thread(target=outgoing_loop,
                                 args=(SalonScope(database_url, binding["salon_id"]), sender, stop), daemon=True).start()
        try:
            httpd.serve_forever()
        finally:
            stop.set()


if __name__ == "__main__":
    main()
