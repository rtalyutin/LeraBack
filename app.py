"""One backend service: admin API, VK Callback API and outgoing worker."""

from __future__ import annotations

import json
import os
import threading
import re
from urllib.parse import urlparse
from datetime import datetime, timezone

from admin_http import Handler as AdminHandler, server
from runtime_config import initial_policy, optional_csrf_secret
from startup import prepare_database
from vk_gateway import CallbackGateway, deliver_pending
from vk_sender import VKSender
from vk_config import integrations
from pg_store import SalonScope, connect
from constructor_store import get_policy
from vk_connections import VKConnections, VKSetupError, salon_lock


class Handler(AdminHandler):
    def do_POST(self):
        path = urlparse(self.path).path
        managed = re.fullmatch(r"/api/vk/callback/([a-f0-9]{32})", path)
        if path != "/vk/callback" and not managed:
            return super().do_POST()
        if not managed and not self.server.gateways:
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
            if managed:
                result = self.server.vk_service.callback(managed[1], event)
            else:
                gateway = self.server.gateways.get(event["group_id"])
                if gateway is None:
                    raise PermissionError("Unknown VK community")
                with connect(gateway.db_path.url) as guard, salon_lock(guard, gateway.db_path.salon_id):
                    if self.server.vk_service.managed(gateway.db_path.salon_id):
                        raise PermissionError("Managed VK community")
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
        except VKSetupError:
            return self._json(503, {"error": "retry"})
        except (ValueError, KeyError, TypeError, json.JSONDecodeError):
            return self._json(422, {"error": "invalid_event"})
        except Exception:
            return self._json(500, {"error": "retry"})


def outgoing_loop(database_url, sender, stop, vk_service=None):
    while not stop.is_set():
        try:
            if vk_service is None:
                deliver_pending(database_url, sender, limit=20, now=datetime.now(timezone.utc))
            else:
                with connect(database_url.url) as db, salon_lock(db, database_url.salon_id):
                    if not vk_service.managed(database_url.salon_id):
                        deliver_pending(database_url, sender, limit=1, now=datetime.now(timezone.utc))
        except Exception as exc:
            print(f"Outgoing worker error: {type(exc).__name__}", flush=True)
        stop.wait(5)


def main():
    database_url = os.environ["DATABASE_URL"]
    policy = initial_policy()
    csrf_secret = optional_csrf_secret()
    port = int(os.environ.get("PORT", "8080"))
    if not 1 <= port <= 65535:
        raise RuntimeError("PORT must be between 1 and 65535")
    try:
        bindings = integrations(os.environ)
    except (ValueError, TypeError) as exc:
        raise RuntimeError("Invalid VK integration configuration") from exc
    prepare_database(database_url, os.environ.get("SALON_ADMIN_USERNAME", "salon_admin"),
                     os.environ.pop("SALON_ADMIN_PASSWORD", None))
    vk_service = VKConnections(database_url, os.environ.get("VK_TOKEN_ENCRYPTION_KEY"))
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
        httpd.vk_service = vk_service
        if vk_service.crypto is not None:
            threading.Thread(target=vk_service.run, args=(stop, VKSender), daemon=True).start()
        for binding in bindings:
            if binding.get("token"):
                sender = VKSender(binding["token"], binding.get("api_version", "5.199"),
                                  master_photo_ids=binding.get("master_photo_ids", {}))
                threading.Thread(target=outgoing_loop,
                                 args=(SalonScope(database_url, binding["salon_id"]), sender, stop, vk_service), daemon=True).start()
        try:
            httpd.serve_forever()
        finally:
            stop.set()


if __name__ == "__main__":
    main()
