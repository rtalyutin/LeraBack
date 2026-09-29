"""M6 administrator HTTP boundary with server-side sessions and CSRF checks."""

from __future__ import annotations

import argparse
import json
import os
import secrets
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
    AdminConflict, cancel_admin_booking, create_block, create_manual_booking, snapshot,
    update_service, update_weekly_schedule,
)
from booking_core import BookingConflict, connect
from ops import database_health

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
             "csrf_token": csrf_token(token, self.server.csrf_secret)},
            [("Set-Cookie", self._cookie_header(token))],
        )

    def _route(self, method):
        path = urlparse(self.path)
        try:
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
                })
            if method == "POST" and not verify_csrf(
                    identity, self.server.csrf_secret, self.headers.get("X-CSRF-Token", "")):
                return self._json(HTTPStatus.FORBIDDEN, {"error": "csrf_failed"})
            if method == "POST" and path.path == "/api/logout":
                logout(self.server.db_path, identity, now=self.server.clock())
                return self._json(200, {"status": "logged_out"},
                                  [("Set-Cookie", self._cookie_header("", delete=True))])
            if method == "GET" and path.path == "/api/snapshot":
                day = date.fromisoformat(parse_qs(path.query).get("date", [date.today().isoformat()])[0])
                return self._json(200, snapshot(self.server.db_path, self.server.policy, day))

            body = self._body() if method == "POST" else {}
            now = self.server.clock()
            actor = identity.actor
            if method == "POST" and path.path == "/api/bookings":
                result = create_manual_booking(
                    self.server.db_path, self.server.policy, body["phone"], int(body["service_id"]),
                    int(body["master_id"]), datetime.fromisoformat(body["start"]),
                    body.get("action_key") or secrets.token_urlsafe(16), actor, now,
                )
                return self._json(201, result)
            if method == "POST" and path.path == "/api/blocks":
                result = create_block(
                    self.server.db_path, body["resource_kind"], int(body["resource_id"]),
                    datetime.fromisoformat(body["start"]), datetime.fromisoformat(body["end"]),
                    body.get("reason", ""), actor, bool(body.get("acknowledge")), now,
                )
                return self._json(201, result)
            if method == "POST" and path.path == "/api/weekly-schedule":
                start = body.get("start_minute")
                end = body.get("end_minute")
                result = update_weekly_schedule(
                    self.server.db_path, self.server.policy, body["resource_kind"], int(body["resource_id"]),
                    int(body["weekday"]), None if start is None else int(start),
                    None if end is None else int(end), actor, bool(body.get("acknowledge")), now,
                )
                return self._json(200, result)
            if method == "POST" and path.path.startswith("/api/bookings/") and path.path.endswith("/cancel"):
                booking_id = int(path.path.split("/")[3])
                result = cancel_admin_booking(
                    self.server.db_path, booking_id, body.get("action_key") or secrets.token_urlsafe(16), actor, now
                )
                return self._json(200, result)
            if method == "POST" and path.path.startswith("/api/services/"):
                service_id = int(path.path.split("/")[3])
                result = update_service(
                    self.server.db_path, service_id, body["name"], int(body["duration_minutes"]),
                    bool(body["active"]), actor, bool(body.get("acknowledge")), now,
                )
                return self._json(200, result)
            return self._json(404, {"error": "not_found"})
        except AuthenticationError:
            return self._json(HTTPStatus.UNAUTHORIZED, {"error": "authentication_required"})
        except AuthorizationError:
            return self._json(HTTPStatus.FORBIDDEN, {"error": "forbidden"})
        except AdminConflict as exc:
            return self._json(409, {"error": "conflict", "message": str(exc),
                                    "affected_booking_ids": exc.affected_booking_ids})
        except BookingConflict as exc:
            return self._json(409, {"error": "conflict", "message": str(exc), "affected_booking_ids": []})
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            return self._json(422, {"error": "invalid_request", "message": str(exc)})
        except LookupError as exc:
            return self._json(404, {"error": "not_found", "message": str(exc)})
        except Exception:
            return self._json(500, {"error": "internal_error"})

    def do_GET(self):
        if self.path == "/healthz" or self.path.startswith("/api/"):
            return self._route("GET")
        return self._json(404, {"error": "not_found"})

    def do_POST(self):
        return self._route("POST")


def server(db_path, policy, csrf_secret, host="127.0.0.1", port=8765, *, secure_cookie=True,
           session_idle_minutes=30, session_absolute_hours=12, clock=None, handler_class=Handler):
    if isinstance(csrf_secret, str):
        csrf_secret = csrf_secret.encode()
    if len(csrf_secret or b"") < 32:
        raise RuntimeError("SALON_CSRF_SECRET must contain at least 32 bytes")
    if host not in {"127.0.0.1", "localhost", "0.0.0.0"}:
        raise RuntimeError("Unsupported bind address")
    with closing(connect(db_path)) as db:
        active = db.execute("SELECT count(*) FROM admin_users WHERE active=1").fetchone()[0]
    if active != 1:
        raise RuntimeError("Exactly one active administrator is required")
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
    policy = json.loads(os.environ["SALON_POLICY_JSON"])
    secure_cookie = os.environ.get("SALON_COOKIE_SECURE", "1") != "0"
    with server(
        os.environ["DATABASE_URL"], policy, os.environ.get("SALON_CSRF_SECRET", ""), args.host, args.port,
        secure_cookie=secure_cookie,
        session_idle_minutes=int(os.environ.get("SALON_SESSION_IDLE_MINUTES", "30")),
        session_absolute_hours=int(os.environ.get("SALON_SESSION_ABSOLUTE_HOURS", "12")),
    ) as httpd:
        print(f"Admin server: http://{args.host}:{args.port}", flush=True)
        httpd.serve_forever()


if __name__ == "__main__":
    main()
