"""
app/auth.py — Local user store + session tokens for the assistant web app.

Two concerns live here:

  * **Users** — a flat ``users.json`` file, ``{username: {salt, hash, role,
    created}}``. Passwords are stretched with PBKDF2-HMAC-SHA256 at 200_000
    iterations. Only the salt + derived hash are stored; the plaintext is
    never persisted or logged.

  * **Sessions** — opaque bearer tokens. We hand the browser a random
    ``secrets.token_urlsafe`` value and store only its SHA-256 hex in the
    ``sessions`` table with an absolute expiry (now + SESSION_TTL_HOURS).
    Looking a token up re-hashes it; an attacker with DB read access still
    cannot reconstruct a usable cookie.

HONESTY NOTES
  - ``verify_password`` compares with ``hmac.compare_digest`` (constant
    time) and returns a plain bool — no partial credit, no fabricated
    "maybe" state.
  - Session lookup purges expired rows so a stale token never validates and
    the table self-cleans.
  - This is a LOCAL internal tool; there is no password-reset flow yet. Users
    are provisioned via ``server.py --create-user``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from datetime import datetime, timezone
from typing import Optional

from app.config import SETTINGS
from app import db

logger = logging.getLogger(__name__)

_PBKDF2_ITERS = 200_000
_PBKDF2_ALGO = "sha256"
_SALT_BYTES = 16
_TOKEN_BYTES = 32
_VALID_ROLES = ("staff", "manager", "admin")


# ---------------------------------------------------------------------------
# users.json helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_users() -> dict:
    """Return the users map, or ``{}`` if the store is absent/corrupt."""
    path = SETTINGS.USERS_PATH
    if not path.is_file():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        logger.error("users.json unreadable (%s); treating as empty", exc)
        return {}
    return data if isinstance(data, dict) else {}


def _save_users(users: dict) -> None:
    """Atomically write the users map with owner-only permissions."""
    path = SETTINGS.USERS_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(users, fh, indent=2, sort_keys=True)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover - best-effort on odd filesystems
        pass


def _hash_password(password: str, salt: bytes) -> str:
    derived = hashlib.pbkdf2_hmac(
        _PBKDF2_ALGO, password.encode("utf-8"), salt, _PBKDF2_ITERS
    )
    return derived.hex()


# ---------------------------------------------------------------------------
# Public: users
# ---------------------------------------------------------------------------

def create_user(username: str, password: str, role: str = "staff") -> dict:
    """Create (or overwrite) a user record and persist it.

    Returns the stored record MINUS the hash/salt (safe to surface). Raises
    ``ValueError`` on empty username/password or unknown role.
    """
    username = (username or "").strip()
    if not username:
        raise ValueError("username required")
    if not password:
        raise ValueError("password required")
    if role not in _VALID_ROLES:
        raise ValueError(
            f"role must be one of {_VALID_ROLES}, got {role!r}"
        )

    salt = secrets.token_bytes(_SALT_BYTES)
    users = _load_users()
    existed = username in users
    users[username] = {
        "salt": salt.hex(),
        "hash": _hash_password(password, salt),
        "role": role,
        "created": _now_iso(),
    }
    _save_users(users)
    logger.info(
        "%s user %r (role=%s)",
        "updated" if existed else "created",
        username,
        role,
    )
    return {"username": username, "role": role,
            "created": users[username]["created"]}


def delete_user(username: str) -> bool:
    """Remove *username* from users.json AND purge all their session rows.

    This is the documented offboarding path: it revokes access immediately
    (no waiting out the session TTL). Returns True if a user record was
    removed, False if the username was unknown. Session rows are always purged
    (idempotent) so a stale token can never validate afterward.
    """
    username = (username or "").strip()
    if not username:
        raise ValueError("username required")

    users = _load_users()
    existed = username in users
    if existed:
        del users[username]
        _save_users(users)

    conn = db.get_conn()
    with conn:
        conn.execute("DELETE FROM sessions WHERE username = ?", (username,))

    logger.info(
        "%s user %r and purged their sessions",
        "deleted" if existed else "no-such-user; purged sessions for",
        username,
    )
    return existed


def verify_password(username: str, password: str) -> bool:
    """Constant-time check of *password* for *username*. Bool only."""
    username = (username or "").strip()
    users = _load_users()
    record = users.get(username)
    if not record:
        # Still run a dummy derivation to blunt username-timing oracles.
        _hash_password(password or "", secrets.token_bytes(_SALT_BYTES))
        return False
    try:
        salt = bytes.fromhex(record["salt"])
        expected = record["hash"]
    except (KeyError, ValueError):
        logger.error("corrupt user record for %r", username)
        return False
    candidate = _hash_password(password or "", salt)
    return hmac.compare_digest(candidate, expected)


def get_user_role(username: str) -> Optional[str]:
    """Return the stored role for *username*, or ``None`` if unknown."""
    record = _load_users().get((username or "").strip())
    return record.get("role") if record else None


# ---------------------------------------------------------------------------
# Public: sessions
# ---------------------------------------------------------------------------

def _hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def create_session(username: str) -> str:
    """Mint a session for *username* and return the RAW token (once).

    Only the SHA-256 of the token is stored, alongside an absolute epoch
    expiry ``now + SESSION_TTL_HOURS``.
    """
    raw_token = secrets.token_urlsafe(_TOKEN_BYTES)
    expires_ts = time.time() + SETTINGS.SESSION_TTL_HOURS * 3600
    conn = db.get_conn()
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO sessions "
            "(token_hash, username, expires_ts) VALUES (?, ?, ?)",
            (_hash_token(raw_token), username, expires_ts),
        )
    return raw_token


def get_session_user(raw_token: Optional[str]) -> Optional[str]:
    """Resolve *raw_token* to a username, or ``None`` if invalid/expired/revoked.

    Three ways a token fails to resolve, each self-cleaning the row:
      * no matching session row,
      * the session has passed its absolute expiry, OR
      * the user no longer exists in ``users.json`` (offboarding: deleting the
        user must immediately revoke live sessions, not wait out the 12h TTL).
    """
    if not raw_token:
        return None
    token_hash = _hash_token(raw_token)
    conn = db.get_conn()
    row = conn.execute(
        "SELECT username, expires_ts FROM sessions WHERE token_hash = ?",
        (token_hash,),
    ).fetchone()
    if row is None:
        return None
    if float(row["expires_ts"]) < time.time():
        with conn:
            conn.execute(
                "DELETE FROM sessions WHERE token_hash = ?", (token_hash,)
            )
        return None
    username = row["username"]
    # Offboarding enforcement: a session for a user who has been removed from
    # users.json is no longer valid. Purge it so a terminated employee's cookie
    # stops working the moment their account is deleted.
    if (username or "").strip() not in _load_users():
        with conn:
            conn.execute(
                "DELETE FROM sessions WHERE token_hash = ?", (token_hash,)
            )
        return None
    return username


def destroy_session(raw_token: Optional[str]) -> None:
    """Invalidate a single session token (idempotent)."""
    if not raw_token:
        return
    conn = db.get_conn()
    with conn:
        conn.execute(
            "DELETE FROM sessions WHERE token_hash = ?",
            (_hash_token(raw_token),),
        )
