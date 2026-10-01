"""M6 server-side administrator authentication and sessions.

The code intentionally supports only the approved-for-prototype ``admin`` role.
    One active administrator is supported. Recovery is done by the developer's
    controlled local password rotation; MFA remains a release decision.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from booking_core import connect, stamp

UTC = timezone.utc
PBKDF2_ITERATIONS = 600_000
USERNAME_RE = re.compile(r"[a-zA-Z0-9_.@-]{3,64}")


class AuthenticationError(Exception):
    pass


class AuthorizationError(Exception):
    pass


@dataclass(frozen=True)
class AdminIdentity:
    user_id: int
    username: str
    role: str
    session_id: int
    session_token: str

    @property
    def actor(self) -> str:
        return f"admin:{self.user_id}"


def _password_hash(password: str, salt: bytes, iterations: int = PBKDF2_ITERATIONS) -> bytes:
    if not isinstance(password, str) or not 12 <= len(password) <= 256:
        raise ValueError("Password must contain 12–256 characters")
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations, dklen=32)


def create_or_update_admin(path, username: str, password: str, now=None, *, initial_only=False) -> int:
    """Provision an administrator locally; the password is never persisted verbatim."""
    now = now or datetime.now(UTC)
    username = username.strip()
    if not USERNAME_RE.fullmatch(username):
        raise ValueError("Username must contain 3–64 safe characters")
    salt = secrets.token_bytes(16)
    digest = _password_hash(password, salt)
    with closing(connect(path)) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            if initial_only:
                users = list(db.execute("SELECT id,active FROM admin_users"))
                if users:
                    active = [user for user in users if user["active"]]
                    if len(active) != 1:
                        raise ValueError("Initial setup cannot reactivate or replace administrator accounts")
                    db.commit()
                    return active[0]["id"]
            existing = db.execute("SELECT id FROM admin_users WHERE lower(username)=lower(?)", (username,)).fetchone()
            if existing:
                user_id = existing["id"]
                db.execute(
                    "UPDATE admin_users SET password_salt=?,password_hash=?,password_iterations=?,"
                    "role='admin',active=1,password_changed_at=? WHERE id=?",
                    (salt, digest, PBKDF2_ITERATIONS, stamp(now), user_id),
                )
                db.execute("DELETE FROM admin_sessions WHERE admin_user_id=?", (user_id,))
                action = "password_changed"
            else:
                if db.execute("SELECT 1 FROM admin_users WHERE active=1 LIMIT 1").fetchone():
                    raise ValueError("Only one active administrator is allowed")
                user_id = db.execute(
                    "INSERT INTO admin_users(username,password_salt,password_hash,password_iterations,role,active,"
                    "created_at,password_changed_at) VALUES (?,?,?,?,'admin',1,?,?)",
                    (username, salt, digest, PBKDF2_ITERATIONS, stamp(now), stamp(now)),
                ).lastrowid
                action = "admin_created"
            db.execute(
                "INSERT INTO admin_audit_log(actor,action,object_type,object_id,details_json,created_at) "
                "VALUES (?,?,?,?,?,?)",
                (f"admin:{user_id}", action, "admin_user", str(user_id), "{}", stamp(now)),
            )
            db.commit()
            return user_id
        except Exception:
            db.rollback()
            raise


def login(path, username: str, password: str, absolute_hours: int = 12, now=None) -> tuple[str, AdminIdentity]:
    now = now or datetime.now(UTC)
    username = (username or "").strip()
    with closing(connect(path)) as db:
        row = db.execute("SELECT * FROM admin_users WHERE lower(username)=lower(?) AND active=1", (username,)).fetchone()
        # Perform the same expensive KDF for an unknown user to reduce enumeration timing.
        salt = bytes(row["password_salt"]) if row else b"\0" * 16
        iterations = int(row["password_iterations"]) if row else PBKDF2_ITERATIONS
        expected = bytes(row["password_hash"]) if row else b"\0" * 32
        try:
            supplied = _password_hash(password or "", salt, iterations)
        except ValueError:
            supplied = hashlib.pbkdf2_hmac("sha256", b"invalid", salt, iterations, dklen=32)
        if row is None or not hmac.compare_digest(supplied, expected):
            raise AuthenticationError("Invalid username or password")
        if absolute_hours < 1 or absolute_hours > 168:
            raise ValueError("Session lifetime must be 1–168 hours")
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode()).digest()
        expires = now + timedelta(hours=absolute_hours)
        db.execute("BEGIN IMMEDIATE")
        try:
            db.execute("DELETE FROM admin_sessions WHERE expires_at<=?", (stamp(now),))
            session_id = db.execute(
                "INSERT INTO admin_sessions(admin_user_id,token_hash,created_at,last_seen_at,expires_at) "
                "VALUES (?,?,?,?,?)",
                (row["id"], token_hash, stamp(now), stamp(now), stamp(expires)),
            ).lastrowid
            db.execute(
                "INSERT INTO admin_audit_log(actor,action,object_type,object_id,details_json,created_at) "
                "VALUES (?,?,?,?,?,?)",
                (f"admin:{row['id']}", "login", "admin_session", str(session_id), "{}", stamp(now)),
            )
            db.commit()
        except Exception:
            db.rollback()
            raise
    return token, AdminIdentity(row["id"], row["username"], row["role"], session_id, token)


def authenticate(path, token: str, idle_minutes: int = 30, now=None) -> AdminIdentity:
    now = now or datetime.now(UTC)
    if not token or idle_minutes < 1 or idle_minutes > 1440:
        raise AuthenticationError("Authentication required")
    token_hash = hashlib.sha256(token.encode()).digest()
    with closing(connect(path)) as db:
        row = db.execute(
            "SELECT s.*,u.username,u.role,u.active FROM admin_sessions s "
            "JOIN admin_users u ON u.id=s.admin_user_id WHERE s.token_hash=?",
            (token_hash,),
        ).fetchone()
        if row is None or not row["active"]:
            raise AuthenticationError("Authentication required")
        expires = datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00"))
        last_seen = datetime.fromisoformat(row["last_seen_at"].replace("Z", "+00:00"))
        if now >= expires or now - last_seen > timedelta(minutes=idle_minutes):
            db.execute("DELETE FROM admin_sessions WHERE id=?", (row["id"],))
            raise AuthenticationError("Session expired")
        db.execute("UPDATE admin_sessions SET last_seen_at=? WHERE id=?", (stamp(now), row["id"]))
        return AdminIdentity(row["admin_user_id"], row["username"], row["role"], row["id"], token)


def require_admin(identity: AdminIdentity) -> None:
    if identity.role != "admin":
        raise AuthorizationError("Administrator role required")


def csrf_token(session_token: str, secret: bytes) -> str:
    if len(secret) < 32:
        raise ValueError("CSRF secret must contain at least 32 bytes")
    return hmac.new(secret, session_token.encode(), hashlib.sha256).hexdigest()


def verify_csrf(identity: AdminIdentity, secret: bytes, supplied: str) -> bool:
    return bool(supplied) and hmac.compare_digest(csrf_token(identity.session_token, secret), supplied)


def logout(path, identity: AdminIdentity, now=None) -> None:
    now = now or datetime.now(UTC)
    with closing(connect(path)) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            db.execute("DELETE FROM admin_sessions WHERE id=?", (identity.session_id,))
            db.execute(
                "INSERT INTO admin_audit_log(actor,action,object_type,object_id,details_json,created_at) "
                "VALUES (?,?,?,?,?,?)",
                (identity.actor, "logout", "admin_session", str(identity.session_id), "{}", stamp(now)),
            )
            db.commit()
        except Exception:
            db.rollback()
            raise
