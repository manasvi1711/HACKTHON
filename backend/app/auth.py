"""Local demo authentication and server-side authorization.

DEMO ONLY: synthetic users with a shared demo password and opaque bearer tokens kept
in SQLite. Production deployments should replace login with the enterprise IdP
(OIDC/SAML) and map IdP groups to roles; the authorization functions below stay.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from fastapi import Depends, Header, HTTPException, Request

from .config import settings
from .db import get_conn

ROLES = ("employee", "sales", "hr", "admin")

DEMO_USERS = [
    # id, username, display name, role
    ("u-emma", "emma", "Emma Employee", "employee"),
    ("u-eli", "eli", "Eli Employee", "employee"),
    ("u-sam", "sam", "Sam Sales", "sales"),
    ("u-hana", "hana", "Hana HR", "hr"),
    ("u-adam", "adam", "Adam Admin", "admin"),
]

UPLOAD_ROLES = {"admin", "hr", "sales"}


@dataclass(frozen=True)
class User:
    id: str
    username: str
    display_name: str
    role: str

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    def public(self) -> dict:
        return {"id": self.id, "username": self.username, "display_name": self.display_name, "role": self.role,
                "can_upload": self.role in UPLOAD_ROLES, "is_admin": self.is_admin}


# ---------------------------------------------------------------- passwords
def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
    return f"pbkdf2_sha256${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt_hex, digest_hex = stored.split("$")
    except ValueError:
        return False
    candidate = hash_password(password, bytes.fromhex(salt_hex)).split("$")[2]
    return hmac.compare_digest(candidate, digest_hex)


def seed_users() -> None:
    conn = get_conn()
    for uid, username, name, role in DEMO_USERS:
        row = conn.execute("SELECT password_hash FROM users WHERE id=?", (uid,)).fetchone()
        if row:
            # Keep demo users in sync with DEMO_PASSWORD if it was changed in .env.
            if not verify_password(settings.demo_password, row["password_hash"]):
                conn.execute("UPDATE users SET password_hash=? WHERE id=?",
                             (hash_password(settings.demo_password), uid))
                conn.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
            continue
        conn.execute("INSERT INTO users(id, username, display_name, role, password_hash) VALUES (?,?,?,?,?)",
                     (uid, username, name, role, hash_password(settings.demo_password)))


# ---------------------------------------------------------------- sessions
def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def authenticate(username: str, password: str) -> User | None:
    row = get_conn().execute("SELECT * FROM users WHERE username=? AND active=1", (username,)).fetchone()
    # Always run a hash to keep timing similar for unknown users.
    stored = row["password_hash"] if row else hash_password("timing-equaliser")
    if not verify_password(password, stored) or row is None:
        return None
    return User(row["id"], row["username"], row["display_name"], row["role"])


def create_session(user: User) -> str:
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    get_conn().execute(
        "INSERT INTO sessions(token_hash, user_id, created_at, expires_at) VALUES (?,?,?,?)",
        (_token_hash(token), user.id, now.isoformat(), (now + timedelta(minutes=settings.session_ttl_minutes)).isoformat()),
    )
    return token


def revoke_session(token: str) -> None:
    get_conn().execute("DELETE FROM sessions WHERE token_hash=?", (_token_hash(token),))


def user_from_token(token: str) -> User | None:
    row = get_conn().execute(
        "SELECT u.*, s.expires_at FROM sessions s JOIN users u ON u.id = s.user_id "
        "WHERE s.token_hash=? AND u.active=1", (_token_hash(token),)).fetchone()
    if not row:
        return None
    if datetime.fromisoformat(row["expires_at"]) < datetime.now(timezone.utc):
        revoke_session(token)
        return None
    # Role is always read from the server-side user record, never from the client.
    return User(row["id"], row["username"], row["display_name"], row["role"])


def user_from_id(user_id: str) -> User | None:
    row = get_conn().execute("SELECT * FROM users WHERE id=? AND active=1", (user_id,)).fetchone()
    return User(row["id"], row["username"], row["display_name"], row["role"]) if row else None


def bearer_token(authorization: str | None = Header(default=None)) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Authentication required")
    return authorization[7:].strip()


def current_user(request: Request, token: str = Depends(bearer_token)) -> User:
    user = user_from_token(token)
    if user is None:
        raise HTTPException(status_code=401, detail="Session invalid or expired")
    request.state.user_id = user.id
    return user


def require_admin(user: User = Depends(current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Administrator role required")
    return user


# ---------------------------------------------------------------- authorization
def allowed_roles_of(doc_row) -> list[str]:
    try:
        roles = json.loads(doc_row["allowed_roles"])
    except (TypeError, ValueError):
        return []  # malformed ACL -> deny
    return [r for r in roles if r in ROLES]


def can_read_document(user: User, doc_row) -> bool:
    """Read = search, answer, cite, preview, download. Deny by default."""
    if doc_row is None or doc_row["status"] == "deleted":
        return False
    if user.is_admin:
        return True
    searchable = doc_row["status"] == "ready" and doc_row["approval_status"] == "approved"
    if not searchable:
        return False
    return user.role in allowed_roles_of(doc_row)


def can_see_document_entry(user: User, doc_row) -> bool:
    """Whether the document appears in the user's document list (own pending uploads too)."""
    if can_read_document(user, doc_row):
        return True
    return doc_row is not None and doc_row["status"] != "deleted" and doc_row["uploader_id"] == user.id


def validate_upload_acl(user: User, roles: list[str]) -> list[str]:
    if user.role not in UPLOAD_ROLES:
        raise HTTPException(status_code=403, detail="Your role cannot publish documents")
    clean = sorted({r for r in roles if r in ROLES})
    if not clean:
        raise HTTPException(status_code=422, detail="At least one valid role must be granted access")
    if not user.is_admin and user.role not in clean:
        raise HTTPException(status_code=422, detail="You must keep access to documents you upload")
    if "admin" not in clean:
        clean.append("admin")
    return sorted(clean)


def readable_document_ids(user: User) -> list[str]:
    rows = get_conn().execute(
        "SELECT * FROM documents WHERE status='ready' AND approval_status='approved'").fetchall()
    return [r["id"] for r in rows if can_read_document(user, r)]
