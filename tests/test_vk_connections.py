"""No live VK calls. Optional real PostgreSQL uses a fresh disposable *_test DB."""
import json
import io
import os
import threading
import unittest
from datetime import datetime, timezone
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from unittest.mock import patch

from cryptography.fernet import Fernet
from pg_store import SalonScope, connect
from booking_core import migrate
from admin_auth import create_or_update_admin
from salon_service import create_salon, accessible_salons, salon_profile, update_salon, require_salon
from constructor_store import constructor_snapshot
from vk_connections import VKConnections, VKSetupError, VKTransportError, community_reference, callback_origin

URL = os.environ.get("VK_CONNECTION_TEST_DATABASE_URL", "")
TOKEN = "synthetic-community-key-1234567890"
ORIGIN = "https://salon.example"


class Inputs(unittest.TestCase):
    def test_credential_errors_are_distinct_from_recipient_errors(self):
        from vk_sender import VKSender, VKSendError, VKCredentialSendError
        def response(code):
            return lambda *args, **kwargs: io.BytesIO(json.dumps({"error": {"error_code": code, "error_msg": TOKEN}}).encode())
        for code in (5, 7, 27, 28):
            with self.assertRaises(VKCredentialSendError) as caught:
                VKSender(TOKEN, opener=response(code))(1, "text", [], 1)
            self.assertNotIn(TOKEN, str(caught.exception))
        with self.assertRaises(VKSendError) as caught:
            VKSender(TOKEN, opener=response(901))(1, "text", [], 1)
        self.assertNotIsInstance(caught.exception, VKCredentialSendError)
    def test_community_and_origin_are_not_arbitrary_urls(self):
        for value in ("https://vk.com/club123", "vk.ru/public123", "123"):
            self.assertEqual(community_reference(value), "123")
        self.assertEqual(community_reference("https://vk.com/salon_test"), "salon_test")
        for value in ("https://example.com/club123", "https://vk.com@evil.example/club123", "https://vk.com/club1?x=1", "club1/path"):
            with self.assertRaises(VKSetupError):
                community_reference(value)
        for value, origin in (("http://salon.example", "http://salon.example"), (ORIGIN, "https://other.example"), (ORIGIN + "/path", ORIGIN + "/path")):
            with self.assertRaises(VKSetupError):
                callback_origin(value, origin)

    def test_ciphertext_is_authenticated_and_bound_to_salon(self):
        service = VKConnections("unused", Fernet.generate_key())
        row = {"salon_id": 1, "connection_id": "a" * 32, "group_id": 123}
        row["credentials_ciphertext"] = service._seal(row, {"token": TOKEN, "secret": "private", "confirmation": "code"})
        self.assertNotIn(TOKEN, row["credentials_ciphertext"])
        self.assertEqual(service._open(row)["token"], TOKEN)
        with self.assertRaises(VKSetupError):
            service._open({**row, "salon_id": 2})
        public = service._public({**row, "status": "connected", "community_name": "Salon", "step": "ready",
            "message": None, "error_code": None, "cleanup_warning": None, "callback_origin": ORIGIN})
        self.assertNotIn(TOKEN, json.dumps(public))
        self.assertFalse({"token", "secret", "confirmation", "credentials_ciphertext"} & public.keys())

    def test_missing_key_stops_before_vk_request(self):
        factory = unittest.mock.Mock()
        service = VKConnections("unused", api_factory=factory)
        with self.assertRaises(VKSetupError) as caught:
            service.connect(SalonScope("unused", 1), {"community": "123", "token": TOKEN, "callback_origin": ORIGIN}, ORIGIN)
        self.assertEqual(caught.exception.code, "vk_unavailable")
        factory.assert_not_called()


class FakeVK:
    def __init__(self):
        self.servers, self.calls = [], []
        self.group_id = 123
        self.fail_create_after_commit = False
        self.fail_create_without_commit = False
        self.fail_delete = False
        self.messages_permission = True
        self.events = {"message_new": 1}
        self.service = None
        self.confirmations = []

    def call(self, method, **params):
        self.calls.append((method, params))
        if method == "groups.getById":
            return {"groups": [{"id": self.group_id, "name": "Тестовый салон"}]}
        if method == "groups.getCallbackConfirmationCode":
            return {"code": "synthetic-confirmation"}
        if method == "groups.getTokenPermissions":
            return {"mask": 266240, "permissions": [{"name": "messages", "setting": 4096}, {"name": "manage", "setting": 262144}] if self.messages_permission else []}
        if method == "groups.getCallbackServers":
            return {"count": len(self.servers), "items": [dict(s) for s in self.servers]}
        if method == "groups.addCallbackServer":
            if self.fail_create_without_commit:
                raise VKTransportError()
            remote = {"id": len(self.servers) + 10, "title": params["title"], "url": params["url"],
                      "secret_key": params["secret_key"], "status": "ok"}
            self.servers.append(remote)
            if self.service:
                confirmation = self.service.callback(params["url"].rsplit("/", 1)[1],
                    {"type": "confirmation", "group_id": params["group_id"], "secret": params["secret_key"]})
                self.confirmations.append(confirmation)
            if self.fail_create_after_commit:
                raise VKTransportError()
            return {"server_id": remote["id"]}
        if method == "groups.getCallbackSettings":
            return {"api_version": "5.199", "events": self.events}
        if method == "groups.deleteCallbackServer":
            if self.fail_delete:
                raise VKTransportError()
            self.servers = [s for s in self.servers if s["id"] != params["server_id"]]
        return 1


@unittest.skipUnless(URL, "VK_CONNECTION_TEST_DATABASE_URL absent; PostgreSQL not executed")
class ConnectionsPostgres(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not urlsplit(URL).path.removeprefix("/").endswith("_test"):
            raise ValueError("Use a fresh disposable *_test database")
        with connect(URL) as db:
            if db.execute("SELECT to_regclass('__APP_SCHEMA__.bookings')").fetchone()[0] is not None:
                raise ValueError("Fresh empty schema required; no data is erased")
            migrate(db)
        cls.user_id = create_or_update_admin(URL, "test_admin", "synthetic-password-12345")
        cls.no_salon_user = create_or_update_admin(URL, "new_admin", "synthetic-password-54321")

    def setUp(self):
        created = create_salon(URL, self.user_id, "Тест", os.urandom(16).hex())
        self.scope = SalonScope(URL, created["id"])
        self.fake = FakeVK()
        self.service = VKConnections(URL, Fernet.generate_key(), lambda token: self.fake)
        self.fake.service = self.service

    def tearDown(self):
        self.service.disconnect(self.scope)

    def attach(self):
        return self.service.connect(self.scope, {"community": "123", "token": TOKEN, "callback_origin": ORIGIN}, ORIGIN)

    def test_onboarding_idempotency_rename_and_canonical_storage(self):
        self.no_salon_user = create_or_update_admin(URL, "onboard_admin", "synthetic-onboarding-12345")
        self.assertEqual(accessible_salons(URL, self.no_salon_user), [])
        key = os.urandom(16).hex()
        first = create_salon(URL, self.no_salon_user, "Мой салон", key)
        second = create_salon(URL, self.no_salon_user, "Мой салон", key)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(second["salons"]), 1)
        with self.assertRaises(ValueError):
            create_salon(URL, self.no_salon_user, "Другой салон", key)
        scope = require_salon(URL, self.no_salon_user, first["id"])
        self.assertEqual(update_salon(scope, self.no_salon_user, "Новое имя")["name"], "Новое имя")
        with connect(scope) as db:
            snapshot = constructor_snapshot(db)
            settings_type = next(t["id"] for t in snapshot["types"] if t["code"] == "salon_settings")
            self.assertEqual(next(e for e in snapshot["entities"] if e["entity_type_id"] == settings_type)["values"]["name"], "Новое имя")
            with self.assertRaises(Exception):
                db.execute("UPDATE __APP_SCHEMA__.salons SET name='bypass' WHERE id=?", (scope.salon_id,))
            with self.assertRaises(Exception):
                db.execute("UPDATE __APP_SCHEMA__.entity_parameter_values SET value_string='bypass' "
                           "WHERE salon_id=? AND entity_id=? AND value_string='Новое имя'", (scope.salon_id,
                           next(e["id"] for e in snapshot["entities"] if e["entity_type_id"] == settings_type)))
        self.assertEqual(salon_profile(self.scope)["name"], "Тест")
        from admin_auth import AuthorizationError
        with self.assertRaises(AuthorizationError):
            update_salon(self.scope, self.no_salon_user, "Чужой")
        with self.assertRaises(ValueError):
            update_salon(scope, self.no_salon_user, " ")

    def test_setup_confirmation_duplicate_prevention_and_readback(self):
        result = self.attach()
        self.assertEqual(result["status"], "configuring")
        self.service.advance(self.scope.salon_id)
        self.assertEqual(self.service.status(self.scope)["status"], "connected")
        self.assertEqual(self.fake.confirmations, ["synthetic-confirmation"])
        self.service.retry(self.scope)
        self.service.advance(self.scope.salon_id)
        self.service.retry(self.scope, check_only=True)
        self.service.advance(self.scope.salon_id)
        self.assertEqual(sum(m == "groups.addCallbackServer" for m, _ in self.fake.calls), 1)
        self.assertFalse(any(m == "messages.send" for m, _ in self.fake.calls))
        with connect(URL) as db:
            ciphertext = db.execute("SELECT credentials_ciphertext FROM vk_connections WHERE salon_id=?", (self.scope.salon_id,)).fetchone()[0]
        self.assertNotIn(TOKEN, ciphertext)
        self.assertNotIn(TOKEN, json.dumps(self.service.status(self.scope)))
        self.fake.messages_permission = False
        before = self.service.status(self.scope)
        with self.assertRaises(VKSetupError):
            self.attach()
        self.assertEqual(self.service.status(self.scope), before)

    def test_callback_deduplication_delivery_and_failure_status(self):
        self.attach()
        self.service.advance(self.scope.salon_id)
        connection_id = self.service.status(self.scope)["callback_url"].rsplit("/", 1)[1]
        event = {"type": "message_new", "group_id": 123, "secret": self.fake.servers[0]["secret_key"],
                 "event_id": "same-event", "object": {"message": {"from_id": 899, "peer_id": 899, "text": "начать"}}}
        self.assertEqual(self.service.callback(connection_id, event), "ok")
        self.assertEqual(self.service.callback(connection_id, event), "ok")
        sent = []
        self.service.advance(self.scope.salon_id, lambda token: lambda *args: sent.append(args))
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], 899)
        with connect(self.scope) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM vk_outgoing_messages").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT event_id FROM inbound_events").fetchone()[0], "group:123:same-event")
        self.service.callback(connection_id, {**event, "event_id": "fail-event"})
        def failing_sender(*args):
            from vk_sender import VKSendError
            raise VKSendError("synthetic recipient refusal")
        self.service.advance(self.scope.salon_id, lambda token: failing_sender)
        self.assertEqual(self.service.status(self.scope)["status"], "connected")
        self.service.callback(connection_id, {**event, "event_id": "fresh-event", "object": {"message": {"from_id": 900, "peer_id": 900, "text": "начать"}}})
        self.service.advance(self.scope.salon_id, lambda token: lambda *args: sent.append(args))
        self.assertEqual(sent[-1][0], 900)
        def bad_key_sender(*args):
            from vk_sender import VKCredentialSendError
            raise VKCredentialSendError("synthetic token rejection")
        self.service.advance(self.scope.salon_id, lambda token: bad_key_sender)
        self.assertEqual(self.service.status(self.scope)["error_code"], "vk_send_failed")
        self.service.retry(self.scope)
        self.service.advance(self.scope.salon_id)
        self.service.advance(self.scope.salon_id, lambda token: lambda *args: sent.append(args))
        self.assertEqual(self.service.status(self.scope)["status"], "connected")

    def test_unknown_create_readback_adopts_and_unknown_absence_does_not_recreate(self):
        self.fake.fail_create_after_commit = True
        self.attach()
        self.service.advance(self.scope.salon_id)
        self.assertEqual(self.service.status(self.scope)["status"], "needs_attention")
        self.fake.fail_create_after_commit = False
        self.service.retry(self.scope)
        self.service.advance(self.scope.salon_id)
        self.assertEqual(self.service.status(self.scope)["status"], "connected")
        self.assertEqual(len(self.fake.servers), 1)
        self.service.disconnect(self.scope)
        self.fake.fail_create_without_commit = True
        self.attach()
        self.service.advance(self.scope.salon_id)
        self.fake.fail_create_without_commit = False
        self.service.retry(self.scope)
        self.service.advance(self.scope.salon_id)
        self.assertEqual(self.service.status(self.scope)["error_code"], "callback_uncertain")
        self.assertEqual(sum(m == "groups.addCallbackServer" for m, _ in self.fake.calls), 2)

    def test_permissions_malformed_events_and_no_other_server_changes(self):
        self.fake.messages_permission = False
        with self.assertRaises(VKSetupError):
            self.attach()
        self.assertEqual(self.service.status(self.scope)["status"], "disconnected")
        self.fake.messages_permission = True
        foreign = {"id": 77, "title": "OtherBot", "url": "https://other.example/callback", "secret_key": "other", "status": "ok"}
        self.fake.servers.append(foreign)
        self.fake.events = None
        self.attach()
        self.service.advance(self.scope.salon_id)
        self.assertEqual(self.service.status(self.scope)["status"], "needs_attention")
        self.service.disconnect(self.scope)
        self.assertEqual(self.fake.servers, [foreign])

    def test_one_community_cannot_be_claimed_by_second_salon(self):
        self.attach()
        other = create_salon(URL, self.user_id, "Другой", os.urandom(16).hex())
        other_scope = SalonScope(URL, other["id"])
        with self.assertRaises(VKSetupError) as caught:
            self.service.connect(other_scope, {"community": "123", "token": TOKEN, "callback_origin": ORIGIN}, ORIGIN)
        self.assertEqual(caught.exception.code, "community_in_use")
        self.assertEqual(self.service.status(other_scope)["status"], "disconnected")

    def test_disconnect_disables_callback_and_old_delivery_even_if_vk_fails(self):
        self.attach()
        self.service.advance(self.scope.salon_id)
        connection_id = self.service.status(self.scope)["callback_url"].rsplit("/", 1)[1]
        secret = self.fake.servers[0]["secret_key"]
        with connect(self.scope) as db:
            db.execute("INSERT INTO vk_outgoing_messages(recipient_vk_id,text,dedupe_key,random_id,status,created_at) VALUES(123,'old','old',1,'pending',?)",
                       (datetime.now(timezone.utc).isoformat(),))
        self.fake.fail_delete = True
        result = self.service.disconnect(self.scope)
        self.assertEqual(result["status"], "disconnected")
        self.assertTrue(result["cleanup_warning"])
        self.assertTrue(result["callback_url"].endswith(connection_id))
        self.assertTrue(self.service.managed(self.scope.salon_id))
        with self.assertRaises(PermissionError):
            self.service.callback(connection_id, {"type": "confirmation", "secret": secret, "group_id": 123})
        with connect(self.scope) as db:
            self.assertEqual(db.execute("SELECT status FROM vk_outgoing_messages WHERE dedupe_key='old'").fetchone()[0], "superseded")
        with connect(URL) as db:
            self.assertIsNone(db.execute("SELECT credentials_ciphertext FROM vk_connections WHERE salon_id=?", (self.scope.salon_id,)).fetchone()[0])

    def test_v4_to_v5_repeat_migration_preserves_accounts_and_connections(self):
        self.attach()
        before = self.service.status(self.scope)
        with connect(URL) as db:
            db.execute("DELETE FROM schema_migrations WHERE version=5")
            migrate(db)
            migrate(db)
            self.assertEqual(db.execute("SELECT max(version) FROM schema_migrations").fetchone()[0], 5)
        self.assertEqual(self.service.status(self.scope), before)

    def test_http_creation_scope_csrf_and_callback_source(self):
        from admin_http import server
        from app import Handler
        from runtime_config import initial_policy
        with server(URL, initial_policy(), None, host="127.0.0.1", port=0,
                    secure_cookie=False, handler_class=Handler) as httpd:
            httpd.gateways = {}
            httpd.vk_service = self.service
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{httpd.server_port}"
            def request(path, body=None, headers=None):
                req = Request(base + path, json.dumps(body).encode() if body is not None else None,
                              {"Content-Type": "application/json", **(headers or {})})
                try:
                    response = urlopen(req, timeout=10)
                except HTTPError as exc:
                    response = exc
                return response.status, response.headers, response.read().decode()
            try:
                status, headers, raw = request("/api/login", {"username": "new_admin", "password": "synthetic-password-54321"})
                self.assertEqual(status, 200)
                identity = json.loads(raw)
                auth = {"Cookie": headers["Set-Cookie"].split(";", 1)[0], "X-CSRF-Token": identity["csrf_token"]}
                self.assertEqual(request("/api/salons", {"name": "HTTP", "action_key": os.urandom(16).hex()}, {"Cookie": auth["Cookie"]})[0], 403)
                status, _, raw = request("/api/salons", {"name": "HTTP", "action_key": os.urandom(16).hex(), "user_id": self.user_id}, auth)
                self.assertEqual(status, 201)
                created = json.loads(raw)
                scoped = {**auth, "X-Salon-Id": str(created["id"])}
                self.assertEqual(request("/api/salon", {"name": "HTTP новое имя"}, scoped)[0], 200)
                self.assertEqual(request("/api/vk", headers={**auth, "X-Salon-Id": str(self.scope.salon_id)})[0], 403)
                self.assertEqual(request("/api/vk/connect", {"community": "123", "token": TOKEN, "callback_origin": ORIGIN}, scoped)[0], 422)
                scoped["Origin"] = ORIGIN
                self.assertEqual(request("/api/vk/connect", {"community": "123", "token": TOKEN, "callback_origin": ORIGIN}, scoped)[0], 202)
                result = self.service.status(SalonScope(URL, created["id"]))
                callback = result["callback_url"].replace(ORIGIN, "")
                self.assertEqual(request(callback, {"type": "confirmation", "group_id": 999, "secret": "bad"})[0], 403)
                self.service.disconnect(SalonScope(URL, created["id"]))
            finally:
                httpd.shutdown()
                thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
