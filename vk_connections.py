"""Per-salon VK credentials, Callback provisioning and runtime activation.

This is a technical registry, like accounts/sessions, not constructor data.
All administrator operations receive an already authorized SalonScope.
No token, Callback secret or raw upstream error is exposed to administrators.
"""

from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import threading
from contextlib import contextmanager
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, build_opener, HTTPRedirectHandler

from cryptography.fernet import Fernet, InvalidToken
from psycopg.pq import TransactionStatus
from pg_store import SalonScope, connect

API_VERSION = "5.199"
LOCK_BASE = 810000000000000000
STATUSES = {"disconnected", "configuring", "connected", "needs_attention"}


class VKSetupError(Exception):
    def __init__(self, code, message, status=422):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


class VKTransportError(VKSetupError):
    def __init__(self):
        super().__init__("vk_unreachable", "ВК не ответил. Повторите проверку позже.", 502)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class VKAPI:
    METHODS = {"groups.getById", "groups.getCallbackConfirmationCode", "groups.getTokenPermissions",
               "groups.getCallbackServers", "groups.addCallbackServer", "groups.setSettings",
               "groups.setCallbackSettings", "groups.getCallbackSettings", "groups.deleteCallbackServer"}

    def __init__(self, token):
        self._token = token

    def call(self, method, **params):
        if method not in self.METHODS:
            raise ValueError("Unsupported VK method")
        data = urlencode({**params, "access_token": self._token, "v": API_VERSION}).encode()
        request = Request("https://api.vk.com/method/" + method, data=data, method="POST")
        try:
            with build_opener(NoRedirect()).open(request, timeout=5) as response:
                raw = response.read(128_001)
            if len(raw) > 128_000:
                raise VKTransportError()
            payload = json.loads(raw)
        except (HTTPError, URLError, TimeoutError, OSError, ValueError):
            raise VKTransportError() from None
        if not isinstance(payload, dict):
            raise VKTransportError()
        if "error" in payload:
            code = payload["error"].get("error_code") if isinstance(payload["error"], dict) else None
            if code in (5, 7, 15, 27, 28, 203):
                raise VKSetupError("vk_permissions", "Проверьте ключ этого сообщества и права: управление сообществом и сообщения.")
            if code == 6:
                raise VKSetupError("vk_rate_limit", "ВК временно ограничил запросы. Повторите позже.", 502)
            raise VKSetupError("vk_rejected", "ВК отклонил настройку. Проверьте права ключа и повторите.", 502)
        if "response" not in payload:
            raise VKTransportError()
        return payload["response"]


def community_reference(value):
    if not isinstance(value, str) or len(value) > 250:
        raise VKSetupError("invalid_community", "Укажите ссылку или ID сообщества ВК.")
    value = value.strip()
    if value.startswith(("vk.com/", "vk.ru/", "www.vk.com/", "www.vk.ru/")):
        value = "https://" + value
    if "://" in value:
        parsed = urlparse(value)
        if (parsed.scheme != "https" or parsed.hostname not in ("vk.com", "vk.ru", "www.vk.com", "www.vk.ru")
                or parsed.username or parsed.password or parsed.port or parsed.query or parsed.fragment):
            raise VKSetupError("invalid_community", "Нужна ссылка на сообщество vk.com или vk.ru.")
        value = parsed.path.strip("/")
    value = re.sub(r"^(club|public)(?=[0-9]+$)", "", value)
    if not re.fullmatch(r"[A-Za-z0-9_.]{1,100}", value):
        raise VKSetupError("invalid_community", "Укажите ссылку или ID сообщества ВК.")
    return value


def callback_origin(value, request_origin, secure=True):
    if not isinstance(value, str) or value != request_origin or len(value) > 250:
        raise VKSetupError("invalid_origin", "Откройте мастер на адресе опубликованного сайта.")
    parsed = urlparse(value)
    if (parsed.scheme not in (("https",) if secure else ("http", "https")) or not parsed.hostname
            or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment):
        raise VKSetupError("invalid_origin", "Для подключения нужен опубликованный HTTPS-сайт.")
    # Accessing port also rejects malformed authorities. No supplied URL is fetched by this process.
    try:
        parsed.port
    except ValueError:
        raise VKSetupError("invalid_origin", "Некорректный адрес сайта.") from None
    return value


def upgrade_vk(db):
    db.execute("""CREATE TABLE IF NOT EXISTS __APP_SCHEMA__.salon_creation_requests (
        user_id bigint NOT NULL REFERENCES __APP_SCHEMA__.accounts(id),
        action_key text NOT NULL, salon_id bigint NOT NULL REFERENCES __APP_SCHEMA__.salons(id),
        requested_name text NOT NULL, PRIMARY KEY(user_id,action_key)
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS __APP_SCHEMA__.vk_connections (
        salon_id bigint PRIMARY KEY REFERENCES __APP_SCHEMA__.salons(id) ON DELETE CASCADE,
        connection_id text NOT NULL UNIQUE CHECK (connection_id ~ '^[a-f0-9]{32}$'),
        group_id bigint UNIQUE CHECK (group_id > 0),
        community_name text, callback_origin text,
        credentials_ciphertext text, server_id bigint,
        status text NOT NULL DEFAULT 'disconnected'
            CHECK (status IN ('disconnected','configuring','connected','needs_attention')),
        step text NOT NULL DEFAULT 'disconnected',
        error_code text, message text, cleanup_warning text,
        generation bigint NOT NULL DEFAULT 0,
        creation_ambiguous boolean NOT NULL DEFAULT false,
        check_only boolean NOT NULL DEFAULT false,
        updated_at timestamptz NOT NULL DEFAULT now(), last_checked_at timestamptz
    )""")
    db.execute("INSERT INTO __APP_SCHEMA__.schema_migrations(version,applied_at) "
               "VALUES (5, to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"')) ON CONFLICT DO NOTHING")


@contextmanager
def salon_lock(db, salon_id):
    key = LOCK_BASE + salon_id
    if not db.execute("SELECT pg_try_advisory_lock(?)", (key,)).fetchone()[0]:
        raise VKSetupError("vk_busy", "Настройка ещё выполняется. Дождитесь результата и повторите.", 409)
    try:
        yield
    finally:
        # A SQL constraint error may have aborted the local transaction. Roll
        # it back before unlock so cleanup does not mask the original conflict.
        if db.connection.info.transaction_status in (TransactionStatus.INTRANS, TransactionStatus.INERROR):
            db.rollback()
        db.execute("SELECT pg_advisory_unlock(?)", (key,))


class VKConnections:
    def __init__(self, database_url, key=None, api_factory=VKAPI):
        self.database_url = database_url
        self.api_factory = api_factory
        self.crypto = None
        if key:
            try:
                self.crypto = Fernet(key.encode() if isinstance(key, str) else key)
            except (ValueError, TypeError):
                raise RuntimeError("VK_TOKEN_ENCRYPTION_KEY must be a Fernet key") from None

    def _require_key(self):
        if self.crypto is None:
            raise VKSetupError("vk_unavailable", "Подключение ВК пока не включено. Обратитесь к разработчику.", 503)

    def _row(self, db, salon_id):
        return db.execute("SELECT * FROM __APP_SCHEMA__.vk_connections WHERE salon_id=?", (salon_id,)).fetchone()

    @staticmethod
    def _authorize(db, salon_id, user_id):
        if user_id is None:  # Internal trusted service callers; HTTP always passes identity.user_id.
            return
        if not db.execute("SELECT 1 FROM salon_memberships m JOIN accounts a ON a.id=m.user_id "
                          "JOIN salons s ON s.id=m.salon_id WHERE m.user_id=? AND m.salon_id=? "
                          "AND m.active=1 AND a.active=1 AND a.role='admin' AND s.active=1 FOR SHARE OF m,a,s",
                          (user_id, salon_id)).fetchone():
            from admin_auth import AuthorizationError
            raise AuthorizationError("Salon access denied")

    def _seal(self, row, credentials):
        self._require_key()
        return self.crypto.encrypt(json.dumps({"salon_id": row["salon_id"],
            "connection_id": row["connection_id"], "group_id": row["group_id"], **credentials}).encode()).decode()

    def _open(self, row):
        self._require_key()
        try:
            payload = json.loads(self.crypto.decrypt(row["credentials_ciphertext"].encode()))
            if any(payload.get(key) != row[key] for key in ("salon_id", "connection_id", "group_id")):
                raise ValueError()
            return payload
        except (InvalidToken, ValueError, KeyError, AttributeError, TypeError):
            raise VKSetupError("vk_credentials_unreadable", "Не удалось прочитать ключ. Отключите сообщество и подключите заново.", 503) from None

    def _public(self, row):
        result = {"available": self.crypto is not None, "status": "disconnected", "community_id": None,
                  "community_name": None, "community_url": None, "bot_url": None, "callback_url": None,
                  "step": "disconnected", "message": None, "error_code": None, "cleanup_warning": None}
        if row:
            result.update({key: row[key] for key in ("status", "community_name", "step", "message", "error_code", "cleanup_warning")})
            result["community_id"] = row["group_id"]
            if row["group_id"]:
                result["community_url"] = f"https://vk.com/club{row['group_id']}"
                result["bot_url"] = f"https://vk.me/club{row['group_id']}"
            if row["callback_origin"] and (row["group_id"] or row["cleanup_warning"]):
                result["callback_url"] = self._url(row)
        if self.crypto is None:
            result["message"] = "Подключение ВК пока не включено. Обратитесь к разработчику."
        return result

    def status(self, scope):
        with connect(self.database_url) as db:
            return self._public(self._row(db, scope.salon_id))

    def managed(self, salon_id):
        with connect(self.database_url) as db:
            return self._row(db, salon_id) is not None

    def connect(self, scope, body, request_origin, secure=True, user_id=None):
        self._require_key()
        reference = community_reference(body.get("community"))
        origin = callback_origin(body.get("callback_origin"), request_origin, secure)
        token = body.get("token")
        if not isinstance(token, str) or not 20 <= len(token.strip()) <= 4096 or re.search(r"\s", token.strip()):
            raise VKSetupError("invalid_token", "Вставьте ключ доступа сообщества.")
        api = self.api_factory(token.strip())
        # Validate before replacing a working connection; confirmation proves this group can be managed.
        info = api.call("groups.getById", group_id=reference)
        groups = info.get("groups", []) if isinstance(info, dict) else info
        if not isinstance(groups, list) or not groups or not isinstance(groups[0], dict):
            raise VKSetupError("invalid_community", "Сообщество не найдено.")
        group_id = groups[0].get("id")
        if type(group_id) is not int or group_id <= 0:
            raise VKSetupError("invalid_community", "Сообщество не найдено.")
        confirmation = api.call("groups.getCallbackConfirmationCode", group_id=group_id)
        if not isinstance(confirmation, dict) or not isinstance(confirmation.get("code"), str) or not confirmation["code"]:
            raise VKTransportError()
        # Reject user/service tokens. Group token is required for the eventual OAuth exchange too.
        permissions = api.call("groups.getTokenPermissions")
        granted = {p.get("name") for p in permissions.get("permissions", [])
                   if isinstance(p, dict) and type(p.get("setting")) is int and p["setting"] > 0} if (
                       isinstance(permissions, dict) and isinstance(permissions.get("permissions"), list)) else set()
        if not {"messages", "manage"} <= granted:
            raise VKSetupError("vk_permissions", "Создайте ключ сообщества с правами на сообщения и управление сообществом.")
        with connect(self.database_url) as db, salon_lock(db, scope.salon_id):
            db.execute("BEGIN IMMEDIATE")
            self._authorize(db, scope.salon_id, user_id)
            row = self._row(db, scope.salon_id)
            if row and row["group_id"] and (row["group_id"] != group_id or row["callback_origin"] != origin):
                raise VKSetupError("disconnect_first", "Сначала отключите текущее сообщество, затем подключите новое.", 409)
            if db.execute("SELECT salon_id FROM __APP_SCHEMA__.vk_connections WHERE group_id=? AND salon_id<>?",
                          (group_id, scope.salon_id)).fetchone():
                raise VKSetupError("community_in_use", "Это сообщество уже подключено к другому салону.", 409)
            new_binding = not row or not row["group_id"]
            if not row:
                db.execute("INSERT INTO __APP_SCHEMA__.vk_connections(salon_id,connection_id) VALUES (?,?)",
                           (scope.salon_id, secrets.token_hex(16)))
                row = self._row(db, scope.salon_id)
            row["group_id"] = group_id
            old = self._open(row) if row["credentials_ciphertext"] else {}
            credentials = {"token": token.strip(), "secret": old.get("secret") or secrets.token_urlsafe(32),
                           "confirmation": confirmation["code"]}
            encrypted = self._seal(row, credentials)
            if new_binding:
                self._reset_delivery(db, scope.salon_id)
            db.execute("UPDATE __APP_SCHEMA__.vk_connections SET group_id=?,community_name=?,callback_origin=?,"
                       "credentials_ciphertext=?,status='configuring',step='callback',error_code=NULL,message=NULL,"
                       "cleanup_warning=NULL,generation=generation+1,check_only=false,updated_at=now() WHERE salon_id=?",
                       (group_id, str(groups[0].get("name", "Сообщество ВК"))[:250], origin, encrypted, scope.salon_id))
            result = self._public(self._row(db, scope.salon_id))
            db.commit()
            return result

    @staticmethod
    def _reset_delivery(db, salon_id):
        db.execute("SELECT set_config('app.salon_id',?,true)", (str(salon_id),))
        for table in ("vk_outgoing_messages", "message_outbox"):
            db.execute(f"UPDATE __APP_SCHEMA__.{table} SET status='superseded' "
                       "WHERE salon_id=? AND status IN ('pending','failed')", (salon_id,))
        db.execute("DELETE FROM __APP_SCHEMA__.vk_dialogs WHERE salon_id=?", (salon_id,))

    def retry(self, scope, check_only=False, user_id=None):
        self._require_key()
        with connect(self.database_url) as db, salon_lock(db, scope.salon_id):
            db.execute("BEGIN IMMEDIATE")
            self._authorize(db, scope.salon_id, user_id)
            row = self._row(db, scope.salon_id)
            if not row or not row["group_id"] or not row["credentials_ciphertext"]:
                raise VKSetupError("vk_disconnected", "Сначала подключите сообщество.", 409)
            self._open(row)
            db.execute("UPDATE __APP_SCHEMA__.vk_connections SET status='configuring',step=?,check_only=?,"
                       "generation=generation+1,error_code=NULL,message=NULL,updated_at=now() WHERE salon_id=?",
                       ("checking" if check_only else "callback", check_only, scope.salon_id))
            result = self._public(self._row(db, scope.salon_id))
            db.commit()
            return result

    @staticmethod
    def _url(row):
        return row["callback_origin"] + "/api/vk/callback/" + row["connection_id"]

    @staticmethod
    def _title(row):
        return "Lera-" + row["connection_id"][:8]

    def _servers(self, api, row):
        result = api.call("groups.getCallbackServers", group_id=row["group_id"])
        if not isinstance(result, dict) or not isinstance(result.get("items"), list):
            raise VKTransportError()
        if any(not isinstance(s, dict) or type(s.get("id")) is not int
               or any(not isinstance(s.get(k), str) for k in ("title", "url", "secret_key", "status"))
               for s in result["items"]):
            raise VKTransportError()
        return result["items"]

    def _owned(self, server, row, credentials):
        return (isinstance(server, dict) and server.get("url") == self._url(row)
                and server.get("title") == self._title(row) and server.get("secret_key") == credentials["secret"])

    def _configure(self, db, row, api, credentials):
        servers = self._servers(api, row)
        owned = [s for s in servers if self._owned(s, row, credentials)]
        if any(s.get("url") == self._url(row) and not self._owned(s, row, credentials) for s in servers):
            raise VKSetupError("callback_conflict", "В ВК уже есть сервер с этим адресом и другим секретом. Удалите старый сервер по адресу из мастера и повторите настройку.")
        if len(owned) > 1:
            raise VKSetupError("callback_duplicates", "В ВК найдено несколько подключений этого бота. Обратитесь к разработчику.")
        remote = owned[0] if owned else None
        if remote:
            server_id = remote["id"]
            db.execute("UPDATE __APP_SCHEMA__.vk_connections SET server_id=?,creation_ambiguous=false WHERE salon_id=?",
                       (server_id, row["salon_id"]))
        elif row["check_only"]:
            raise VKSetupError("callback_missing", "Подключение не найдено в ВК. Нажмите «Повторить настройку».")
        elif row["creation_ambiguous"]:
            raise VKSetupError("callback_uncertain", "ВК не подтвердил создание сервера. Повторите проверку позже; если он не появится, обратитесь к разработчику.")
        else:
            # Persist uncertainty BEFORE request. A crash/timeout must never cause a blind second create.
            db.execute("UPDATE __APP_SCHEMA__.vk_connections SET creation_ambiguous=true,step='callback' WHERE salon_id=?",
                       (row["salon_id"],))
            try:
                created = api.call("groups.addCallbackServer", group_id=row["group_id"], url=self._url(row),
                                   title=self._title(row), secret_key=credentials["secret"])
            except VKTransportError:
                raise VKSetupError("callback_uncertain", "ВК не подтвердил создание сервера. Нажмите «Повторить настройку»: сначала проверим результат.", 502) from None
            except VKSetupError:
                # VK explicitly rejected this request, so a future create is safe.
                db.execute("UPDATE __APP_SCHEMA__.vk_connections SET creation_ambiguous=false WHERE salon_id=?", (row["salon_id"],))
                raise
            if not isinstance(created, dict) or type(created.get("server_id")) is not int:
                raise VKTransportError()
            server_id = created["server_id"]
            db.execute("UPDATE __APP_SCHEMA__.vk_connections SET server_id=?,creation_ambiguous=false WHERE salon_id=?",
                       (server_id, row["salon_id"]))
        if not row["check_only"]:
            db.execute("UPDATE __APP_SCHEMA__.vk_connections SET step='messages' WHERE salon_id=?", (row["salon_id"],))
            api.call("groups.setSettings", group_id=row["group_id"], messages=1, bots_capabilities=1, bots_start_button=1)
            api.call("groups.setCallbackSettings", group_id=row["group_id"], server_id=server_id,
                     api_version=API_VERSION, message_new=1)
        db.execute("UPDATE __APP_SCHEMA__.vk_connections SET step='checking' WHERE salon_id=?", (row["salon_id"],))
        servers = self._servers(api, row)
        remote = next((s for s in servers if s.get("id") == server_id and self._owned(s, row, credentials)), None)
        settings = api.call("groups.getCallbackSettings", group_id=row["group_id"], server_id=server_id)
        if not remote or remote.get("status") != "ok":
            raise VKSetupError("callback_not_confirmed", "ВК пока не подтвердил адрес бота. Подождите немного и повторите проверку.")
        if not isinstance(settings, dict) or not isinstance(settings.get("events"), dict):
            raise VKTransportError()
        if not settings["events"].get("message_new"):
            raise VKSetupError("events_disabled", "Получение сообщений выключено. Нажмите «Повторить настройку».")
        db.execute("SELECT set_config('app.salon_id',?,false)", (str(row["salon_id"]),))
        db.execute("UPDATE __APP_SCHEMA__.vk_outgoing_messages SET last_error=NULL "
                   "WHERE salon_id=? AND status='failed' AND last_error='VKCredentialSendError'", (row["salon_id"],))
        db.execute("UPDATE __APP_SCHEMA__.vk_connections SET status='connected',step='ready',error_code=NULL,"
                   "message='Бот подключён. Клиенты могут записываться через сообщения сообщества.',"
                   "last_checked_at=now(),updated_at=now() WHERE salon_id=?", (row["salon_id"],))

    def advance(self, salon_id, sender_factory=None):
        """One worker iteration; lock prevents old-generation sends after local disconnect."""
        with connect(self.database_url) as db, salon_lock(db, salon_id):
            row = self._row(db, salon_id)
            if not row or row["status"] == "disconnected":
                return
            try:
                credentials = self._open(row)
                if row["status"] == "configuring":
                    self._configure(db, row, self.api_factory(credentials["token"]), credentials)
                    return
                if row["status"] == "connected" and sender_factory:
                    from vk_gateway import deliver_pending
                    db.execute("SELECT set_config('app.salon_id',?,false)", (str(salon_id),))
                    deliver_pending(SalonScope(self.database_url, salon_id), sender_factory(credentials["token"]), limit=1)
                    if db.execute("SELECT 1 FROM __APP_SCHEMA__.vk_outgoing_messages "
                                  "WHERE salon_id=? AND status='failed' AND last_error='VKCredentialSendError' LIMIT 1", (salon_id,)).fetchone():
                        raise VKSetupError("vk_send_failed", "ВК не принял ответ клиенту. Проверьте доступность ВК и права ключа, затем повторите настройку.", 502)
            except VKSetupError as exc:
                db.execute("UPDATE __APP_SCHEMA__.vk_connections SET status='needs_attention',error_code=?,message=?,"
                           "updated_at=now() WHERE salon_id=?", (exc.code, exc.message, salon_id))
            except Exception:
                if row["status"] == "configuring":
                    db.execute("UPDATE __APP_SCHEMA__.vk_connections SET status='needs_attention',error_code='vk_setup_failed',"
                               "message='Не удалось завершить настройку. Повторите позже или обратитесь к разработчику.',"
                               "updated_at=now() WHERE salon_id=?", (salon_id,))
                raise

    def callback(self, connection_id, event):
        from constructor_store import get_policy
        from vk_gateway import CallbackGateway
        with connect(self.database_url) as db:
            row = db.execute("SELECT * FROM __APP_SCHEMA__.vk_connections WHERE connection_id=?", (connection_id,)).fetchone()
            if not row or not row["group_id"] or row["status"] == "disconnected":
                raise PermissionError()
            credentials = self._open(row)
            if type(event.get("group_id")) is not int or event["group_id"] != row["group_id"]:
                raise PermissionError()
            if not isinstance(event.get("secret"), str) or not hmac.compare_digest(event["secret"], credentials["secret"]):
                raise PermissionError()
            # addCallbackServer may synchronously confirm while setup owns the salon lock.
            if event.get("type") == "confirmation":
                return credentials["confirmation"]
            with salon_lock(db, row["salon_id"]):
                current = self._row(db, row["salon_id"])
                if current["status"] != "connected" or current["generation"] != row["generation"]:
                    raise VKSetupError("vk_not_ready", "retry", 503)
                scope = SalonScope(self.database_url, row["salon_id"])
                with connect(scope) as scoped:
                    policy = get_policy(scoped)
                if event.get("event_id"):
                    event = {**event, "event_id": f"group:{row['group_id']}:{event['event_id']}"}
                return CallbackGateway(scope, policy, row["group_id"], credentials["secret"], credentials["confirmation"]).handle(event)

    def disconnect(self, scope, user_id=None):
        with connect(self.database_url) as db, salon_lock(db, scope.salon_id):
            db.execute("BEGIN IMMEDIATE")
            self._authorize(db, scope.salon_id, user_id)
            row = self._row(db, scope.salon_id)
            if not row or row["status"] == "disconnected":
                return self._public(row)
            try:
                credentials = self._open(row)
            except VKSetupError:
                credentials = None
            # Disable first, even if VK is unavailable; retain no credential after disconnect.
            db.execute("UPDATE __APP_SCHEMA__.vk_connections SET status='disconnected',step='disconnected',"
                       "group_id=NULL,credentials_ciphertext=NULL,server_id=NULL,generation=generation+1,"
                       "error_code=NULL,message=NULL,cleanup_warning=NULL,creation_ambiguous=false,updated_at=now() WHERE salon_id=?",
                       (scope.salon_id,))
            self._reset_delivery(db, scope.salon_id)
            db.commit()
            warning = None
            try:
                if credentials:
                    api = self.api_factory(credentials["token"])
                    for remote in self._servers(api, row):
                        if self._owned(remote, row, credentials):
                            api.call("groups.deleteCallbackServer", group_id=row["group_id"], server_id=remote["id"])
                else:
                    warning = "Бот отключён. Не удалось удалить его сервер в ВК; удалите его в настройках сообщества."
            except VKSetupError:
                warning = "Бот отключён. ВК не подтвердил удаление сервера; проверьте настройки Callback API сообщества."
            db.execute("UPDATE __APP_SCHEMA__.vk_connections SET cleanup_warning=? WHERE salon_id=?", (warning, scope.salon_id))
            return self._public(self._row(db, scope.salon_id))

    def run(self, stop, sender_factory):
        workers = {}

        def worker(salon_id):
            while not stop.is_set():
                try:
                    self.advance(salon_id, sender_factory)
                except VKSetupError:  # Busy salon: the next iteration will read its current state.
                    pass
                except Exception as exc:
                    # Do not print exceptions/reprs: upstream libraries can include request credentials.
                    print(f"VK worker error: {type(exc).__name__}", flush=True)
                stop.wait(3)

        while not stop.is_set():
            try:
                with connect(self.database_url) as db:
                    rows = db.execute("SELECT salon_id FROM __APP_SCHEMA__.vk_connections WHERE status IN ('configuring','connected')").fetchall()
                for row in rows:
                    salon_id = row["salon_id"]
                    if salon_id not in workers or not workers[salon_id].is_alive():
                        workers[salon_id] = threading.Thread(target=worker, args=(salon_id,), daemon=True)
                        workers[salon_id].start()
            except Exception as exc:
                print(f"VK registry error: {type(exc).__name__}", flush=True)
            stop.wait(3)
