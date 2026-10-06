"""M6 administrator HTTP boundary with server-side sessions and CSRF checks."""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import psycopg
from collections import defaultdict, deque
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from admin_auth import (
    AuthenticationError, AuthorizationError, authenticate, csrf_token, login,
    logout, require_admin, verify_csrf,
)
from admin_service import (
    AdminConflict, cancel_admin_booking, create_block, create_manual_booking, create_service,
    availability_preview, save_resource, snapshot, update_service, update_weekly_schedule,
    update_weekly_schedule_batch,
)
from booking_core import BookingConflict, connect
from ops import database_health
from salon_service import (accessible_salons, require_salon, shared_masters, attach_master,
                           create_salon, salon_profile, update_salon)
from constructor_store import (
    constructor_snapshot, create_type, update_type, delete_type, save_parameter,
    delete_parameter, save_entity, archive_entity, get_policy,
)
from vk_connections import VKSetupError

UTC = timezone.utc


class Handler(BaseHTTPRequestHandler):
    server_version = "SalonAdmin/0.3"

    def log_message(self, fmt, *args):
        pass

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "connect-src 'self'; img-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
        )
        if self.server.secure_cookie:
            self.send_header("Strict-Transport-Security", "max-age=31536000")
        super().end_headers()

    def _json(self, status, body, headers=()):
        raw = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        for key, value in headers:
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(raw)

    def _body(self):
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise ValueError("Content-Type application/json required")
        length = int(self.headers.get("Content-Length", "0"))
        if length < 0 or length > 128_000:
            raise ValueError("Request too large")
        return json.loads(self.rfile.read(length) or b"{}")

    def _cookie_name(self):
        return "__Host-salon_session" if self.server.secure_cookie else "salon_session"

    def _cookie_header(self, token, delete=False):
        attrs = [f"{self._cookie_name()}={token}", "Path=/", "HttpOnly", "SameSite=Strict"]
        if self.server.secure_cookie:
            attrs.append("Secure")
        if delete:
            attrs.extend(["Max-Age=0", "Expires=Thu, 01 Jan 1970 00:00:00 GMT"])
        return "; ".join(attrs)

    def _session_token(self):
        parsed = SimpleCookie(self.headers.get("Cookie", ""))
        morsel = parsed.get(self._cookie_name())
        return morsel.value if morsel else ""

    def _identity(self):
        return authenticate(
            self.server.db_path,
            self._session_token(),
            idle_minutes=self.server.session_idle_minutes,
            now=self.server.clock(),
        )

    def _login_allowed(self):
        now = self.server.clock()
        key = self.client_address[0]
        attempts = self.server.login_attempts[key]
        cutoff = now - timedelta(minutes=5)
        while attempts and attempts[0] < cutoff:
            attempts.popleft()
        if len(attempts) >= 5:
            return False
        attempts.append(now)
        return True

    def _login(self):
        if not self._login_allowed():
            return self._json(429, {"error": "too_many_attempts"})
        body = self._body()
        token, identity = login(
            self.server.db_path,
            str(body.get("username", "")),
            str(body.get("password", "")),
            absolute_hours=self.server.session_absolute_hours,
            now=self.server.clock(),
        )
        self.server.login_attempts[self.client_address[0]].clear()
        return self._json(
            200,
            {"username": identity.username, "role": identity.role,
             "csrf_token": csrf_token(token, self.server.csrf_secret),
             "salons": accessible_salons(self.server.db_path, identity.user_id)},
            [("Set-Cookie", self._cookie_header(token))],
        )

    def _route(self, method):
        path = urlparse(self.path)
        try:
            if method == "GET" and path.path == "/livez":
                return self._json(200, {"status": "ok"})
            if method == "GET" and path.path == "/healthz":
                health = database_health(self.server.db_path)
                return self._json(200 if health["status"] == "ok" else 503,
                                  {"status": health["status"]})
            if method == "POST" and path.path == "/api/login":
                return self._login()

            identity = self._identity()
            require_admin(identity)

            if method == "GET" and path.path == "/api/session":
                return self._json(200, {
                    "username": identity.username,
                    "role": identity.role,
                    "csrf_token": csrf_token(identity.session_token, self.server.csrf_secret),
                    "salons": accessible_salons(self.server.db_path, identity.user_id),
                })
            if method == "GET" and path.path == "/api/salons":
                return self._json(200, {"salons": accessible_salons(self.server.db_path, identity.user_id)})
            if method == "POST" and not verify_csrf(
                    identity, self.server.csrf_secret, self.headers.get("X-CSRF-Token", "")):
                return self._json(HTTPStatus.FORBIDDEN, {"error": "csrf_failed"})
            if method == "POST" and path.path == "/api/logout":
                logout(self.server.db_path, identity, now=self.server.clock())
                return self._json(200, {"status": "logged_out"},
                                  [("Set-Cookie", self._cookie_header("", delete=True))])
            if method == "POST" and path.path == "/api/salons":
                body = self._body()
                if not isinstance(body, dict):
                    raise ValueError("JSON object required")
                return self._json(201, create_salon(self.server.db_path, identity.user_id,
                                                  body.get("name"), body.get("action_key")))
            salon_id = self.headers.get("X-Salon-Id", "")
            if not re.fullmatch(r"[1-9][0-9]{0,17}", salon_id):
                raise ValueError("X-Salon-Id is required")
            scoped = require_salon(self.server.db_path, identity.user_id, int(salon_id))
            if method == "GET" and path.path == "/api/salon":
                return self._json(200, salon_profile(scoped))
            if path.path == "/api/vk" and method == "GET":
                service = getattr(self.server, "vk_service", None)
                if service is None:
                    raise VKSetupError("vk_unavailable", "Подключение ВК пока не включено. Обратитесь к разработчику.", 503)
                return self._json(200, service.status(scoped))
            with closing(connect(scoped)) as db:
                policy = get_policy(db)
            if method == "GET" and path.path == "/api/shared-masters":
                return self._json(200, {"masters": shared_masters(scoped, identity.user_id)})
            if method == "GET" and path.path == "/api/constructor":
                with closing(connect(scoped)) as db:
                    db.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
                    result = constructor_snapshot(db)
                    db.commit()
                return self._json(200, result)
            if method == "GET" and path.path == "/api/snapshot":
                day = date.fromisoformat(parse_qs(path.query).get("date", [date.today().isoformat()])[0])
                return self._json(200, snapshot(scoped, policy, day))

            body = self._body() if method == "POST" else {}
            if not isinstance(body, dict):
                raise ValueError("JSON object required")
            if method == "POST" and path.path == "/api/salon":
                return self._json(200, update_salon(scoped, identity.user_id, body.get("name")))
            if method == "POST" and path.path in ("/api/vk/connect", "/api/vk/retry", "/api/vk/check", "/api/vk/disconnect"):
                service = getattr(self.server, "vk_service", None)
                if service is None:
                    raise VKSetupError("vk_unavailable", "Подключение ВК пока не включено. Обратитесь к разработчику.", 503)
                if path.path == "/api/vk/connect":
                    result = service.connect(scoped, body, self.headers.get("Origin", ""), self.server.secure_cookie, user_id=identity.user_id)
                elif path.path == "/api/vk/disconnect":
                    return self._json(200, service.disconnect(scoped, user_id=identity.user_id))
                else:
                    result = service.retry(scoped, check_only=path.path == "/api/vk/check", user_id=identity.user_id)
                return self._json(202, result)
            if "acknowledge" in body and type(body["acknowledge"]) is not bool:
                raise ValueError("acknowledge must be boolean")
            now = self.server.clock()
            actor = identity.actor
            if method == "POST" and path.path == "/api/availability-preview":
                return self._json(200, availability_preview(
                    scoped, policy, body["service_id"], body["master_id"], body["date"], now,
                    draft=body.get("draft"), drafts=body.get("drafts")))
            if method == "POST" and path.path == "/api/weekly-schedule/batch":
                return self._json(200, update_weekly_schedule_batch(
                    scoped, policy, body["resource_kind"], body["resource_id"], body["days"], actor, now))
            if method == "POST" and path.path == "/api/masters/attach":
                return self._json(201, attach_master(scoped, identity.user_id,
                    body["source_salon_id"], body["master_id"], body["service_ids"],
                    body.get("active", True), body.get("name")))
            constructor = re.fullmatch(r"/api/constructor/(types|parameters|entities)(?:/([1-9][0-9]*))?", path.path)
            if method == "POST" and constructor:
                kind, object_id = constructor[1], int(constructor[2]) if constructor[2] else None
                with closing(connect(scoped)) as db:
                    if kind == "types":
                        result = (create_type(db, body["code"], body["label"], actor) if object_id is None
                                  else update_type(db, object_id, body["label"], actor))
                    elif kind == "parameters":
                        result = save_parameter(db, {**body, **({"id": object_id} if object_id else {})}, actor)
                    else:
                        result = save_entity(db, {**body, **({"id": object_id} if object_id else {})}, actor)
                return self._json(201 if object_id is None else 200, result)
            if method == "POST" and path.path == "/api/bookings":
                result = create_manual_booking(
                    scoped, policy, body["phone"], int(body["service_id"]),
                    int(body["master_id"]), datetime.fromisoformat(body["start"]),
                    body.get("action_key") or secrets.token_urlsafe(16), actor, now,
                )
                return self._json(201, result)
            if method == "POST" and path.path == "/api/blocks":
                result = create_block(
                    scoped, body["resource_kind"], int(body["resource_id"]),
                    datetime.fromisoformat(body["start"]), datetime.fromisoformat(body["end"]),
                    body.get("reason", ""), actor, bool(body.get("acknowledge")), now,
                )
                return self._json(201, result)
            if method == "POST" and path.path == "/api/weekly-schedule":
                if "intervals" in body and not isinstance(body["intervals"], list):
                    raise ValueError("intervals must be a list")
                start = body.get("start_minute")
                end = body.get("end_minute")
                result = update_weekly_schedule(
                    scoped, policy, body["resource_kind"], int(body["resource_id"]),
                    int(body["weekday"]), None if start is None else int(start),
                    None if end is None else int(end), actor, bool(body.get("acknowledge")), now,
                    intervals=body.get("intervals"),
                )
                return self._json(200, result)
            if method == "POST" and path.path.startswith("/api/bookings/") and path.path.endswith("/cancel"):
                booking_id = int(path.path.split("/")[3])
                result = cancel_admin_booking(
                    scoped, booking_id, body.get("action_key") or secrets.token_urlsafe(16), actor, now
                )
                return self._json(200, result)
            if method == "POST" and path.path == "/api/services":
                return self._json(201, create_service(scoped, body["name"],
                                                      body["duration_minutes"], actor, now))
            resource = re.fullmatch(r"/api/(masters|rooms)(?:/([1-9][0-9]*))?", path.path)
            if method == "POST" and resource:
                resource_id = int(resource[2]) if resource[2] else None
                result = save_resource(
                    scoped, "master" if resource[1] == "masters" else "room", resource_id,
                    body["name"], body["service_ids"], body["active"], actor,
                    bool(body.get("acknowledge")), now,
                )
                return self._json(201 if resource_id is None else 200, result)
            if method == "POST" and re.fullmatch(r"/api/services/[1-9][0-9]*", path.path):
                service_id = int(path.path.split("/")[3])
                result = update_service(
                    scoped, service_id, body["name"], body["duration_minutes"],
                    body["active"], actor, bool(body.get("acknowledge")), now,
                )
                return self._json(200, result)
            return self._json(404, {"error": "not_found"})
        except VKSetupError as exc:
            return self._json(exc.status, {"error": exc.code, "message": exc.message})
        except AuthenticationError:
            return self._json(HTTPStatus.UNAUTHORIZED, {"error": "authentication_required"})
        except AuthorizationError:
            return self._json(HTTPStatus.FORBIDDEN, {"error": "forbidden"})
        except AdminConflict as exc:
            return self._json(409, {"error": "conflict", "message": str(exc),
                                    "affected_booking_ids": exc.affected_booking_ids})
        except BookingConflict as exc:
            return self._json(409, {"error": "conflict", "message": str(exc), "affected_booking_ids": []})
        except (psycopg.errors.UniqueViolation, psycopg.errors.ExclusionViolation):
            return self._json(409, {"error": "conflict", "message": "Объект уже существует или время занято", "affected_booking_ids": []})
        except (psycopg.errors.CheckViolation, psycopg.errors.ForeignKeyViolation, psycopg.errors.NotNullViolation):
            return self._json(422, {"error": "invalid_request", "message": "Нарушены правила данных салона"})
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            return self._json(422, {"error": "invalid_request", "message": str(exc)})
        except LookupError as exc:
            return self._json(404, {"error": "not_found", "message": str(exc)})
        except Exception:
            return self._json(500, {"error": "internal_error"})

    def do_GET(self):
        if self.path in {"/healthz", "/livez"} or self.path.startswith("/api/"):
            return self._route("GET")
        return self._json(404, {"error": "not_found"})

    def do_POST(self):
        return self._route("POST")


def server(db_path, policy, csrf_secret=None, host="127.0.0.1", port=8765, *, secure_cookie=True,
           session_idle_minutes=30, session_absolute_hours=12, clock=None, handler_class=Handler):
    if isinstance(csrf_secret, str):
        csrf_secret = csrf_secret.encode()
    if csrf_secret is not None and len(csrf_secret) < 32:
        raise RuntimeError("SALON_CSRF_SECRET must contain at least 32 bytes")
    if host not in {"127.0.0.1", "localhost", "0.0.0.0"}:
        raise RuntimeError("Unsupported bind address")
    with closing(connect(db_path)) as db:
        active = db.execute("SELECT count(*) FROM admin_users WHERE active=1").fetchone()[0]
    if active < 1:
        raise RuntimeError("At least one active administrator is required")
    httpd = ThreadingHTTPServer((host, port), handler_class)
    httpd.db_path = db_path
    httpd.policy = policy
    httpd.csrf_secret = csrf_secret
    httpd.secure_cookie = bool(secure_cookie)
    httpd.session_idle_minutes = int(session_idle_minutes)
    httpd.session_absolute_hours = int(session_absolute_hours)
    httpd.clock = clock or (lambda: datetime.now(UTC))
    httpd.login_attempts = defaultdict(deque)
    return httpd


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    from runtime_config import initial_policy, optional_csrf_secret
    policy = initial_policy()
    secure_cookie = os.environ.get("SALON_COOKIE_SECURE", "1") != "0"
    with server(
        os.environ["DATABASE_URL"], policy, optional_csrf_secret(), args.host, args.port,
        secure_cookie=secure_cookie,
        session_idle_minutes=int(os.environ.get("SALON_SESSION_IDLE_MINUTES", "30")),
        session_absolute_hours=int(os.environ.get("SALON_SESSION_ABSOLUTE_HOURS", "12")),
    ) as httpd:
        print(f"Admin server: http://{args.host}:{args.port}", flush=True)
        httpd.serve_forever()


if __name__ == "__main__":
    main()
