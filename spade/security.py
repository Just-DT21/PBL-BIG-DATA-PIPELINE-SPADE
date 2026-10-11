"""Authentication, session handling and request-level authorization.

Design notes
------------
* Passwords are hashed with ``hashlib.scrypt`` (memory-hard, in the standard
  library) with a per-user random salt. Plaintext is never stored or logged.
* Sessions are opaque 256-bit tokens delivered in an HTTP-only cookie. Only the
  SHA-256 of a token is persisted, so a database leak does not yield live
  sessions.
* Every file/job/artifact lookup is scoped by ``user_id`` - authorization is
  applied in SQL, not in the template layer.
* Mutating requests carrying a browser ``Origin`` must match the request host.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

from fastapi import Cookie, Depends, HTTPException, Request, Response, status

from . import config, db

_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[A-Za-z]{2,24}$")
_MIN_PASSWORD = 8
_MAX_PASSWORD = 256
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_KEYLEN = 32


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------


def hash_password(password: str, *, salt: str | None = None) -> tuple[str, str, int]:
    """Return (hex_hash, hex_salt, iterations log2)."""
    if not isinstance(password, str):
        raise ValueError("password must be text")
    salt_bytes = bytes.fromhex(salt) if salt else secrets.token_bytes(16)
    derived = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt_bytes,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_KEYLEN,
        maxmem=128 * _SCRYPT_N * _SCRYPT_R * 2,
    )
    return derived.hex(), salt_bytes.hex(), _SCRYPT_N.bit_length() - 1


def verify_password(password: str, stored_hash: str, stored_salt: str) -> bool:
    try:
        candidate, _salt, _iters = hash_password(password, salt=stored_salt)
    except (ValueError, MemoryError):
        return False
    return hmac.compare_digest(candidate, stored_hash)


def validate_email(email: str) -> str:
    email = (email or "").strip().lower()
    if not _EMAIL_RE.match(email):
        raise ValueError("Enter a valid email address.")
    return email


def validate_password(password: str) -> str:
    if not isinstance(password, str) or len(password) < _MIN_PASSWORD:
        raise ValueError(f"Password must be at least {_MIN_PASSWORD} characters.")
    if len(password) > _MAX_PASSWORD:
        raise ValueError("Password is too long.")
    return password


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(conn: sqlite3.Connection, user_id: str) -> str:
    token = secrets.token_urlsafe(32)
    now = db.now()
    conn.execute(
        "INSERT INTO sessions (token_hash, user_id, created_at, expires_at, last_seen) "
        "VALUES (?,?,?,?,?)",
        (_token_hash(token), user_id, now, now + config.MAX_SESSION_SECONDS, now),
    )
    return token


def destroy_session(conn: sqlite3.Connection, token: str) -> None:
    conn.execute("DELETE FROM sessions WHERE token_hash=?", (_token_hash(token),))


def purge_expired_sessions(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM sessions WHERE expires_at < ?", (db.now(),))


def set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=config.SESSION_COOKIE,
        value=token,
        max_age=config.MAX_SESSION_SECONDS,
        httponly=True,
        samesite="lax",
        secure=False,  # local/private deployment over http; set True behind TLS
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(config.SESSION_COOKIE, path="/")


# ---------------------------------------------------------------------------
# Login throttling (per process, per email+origin)
# ---------------------------------------------------------------------------

_attempts: dict[str, list[float]] = {}
_attempts_lock = threading.Lock()


def _throttle_key(request: Request, email: str) -> str:
    host = request.client.host if request.client else "unknown"
    return f"{host}|{email}"


def check_throttle(request: Request, email: str) -> None:
    key = _throttle_key(request, email)
    cutoff = time.time() - config.LOGIN_WINDOW_SECONDS
    with _attempts_lock:
        recent = [t for t in _attempts.get(key, []) if t > cutoff]
        _attempts[key] = recent
        if len(recent) >= config.LOGIN_MAX_ATTEMPTS:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many failed sign-in attempts. Try again later.",
            )


def record_failure(request: Request, email: str) -> None:
    key = _throttle_key(request, email)
    with _attempts_lock:
        _attempts.setdefault(key, []).append(time.time())


def clear_failures(request: Request, email: str) -> None:
    with _attempts_lock:
        _attempts.pop(_throttle_key(request, email), None)


# ---------------------------------------------------------------------------
# Same-origin guard for state-changing requests
# ---------------------------------------------------------------------------


def enforce_same_origin(request: Request) -> None:
    """Reject cross-site state-changing requests.

    Sign-in cookies are SameSite=Lax, which already blocks cross-site POSTs.
    This is defence in depth: if an Origin header is present it must match the
    host that served the request.
    """
    origin = request.headers.get("origin")
    if not origin or origin == "null":
        return
    parsed = urlsplit(origin)
    if not parsed.netloc:
        raise HTTPException(status_code=403, detail="Cross-origin request rejected.")
    request_host = request.headers.get("host", "")
    if parsed.netloc.lower() != request_host.lower():
        raise HTTPException(status_code=403, detail="Cross-origin request rejected.")


# ---------------------------------------------------------------------------
# Current user dependency
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CurrentUser:
    id: str
    email: str


def get_current_user(
    request: Request,
    spade_session: str | None = Cookie(default=None, alias=config.SESSION_COOKIE),
) -> CurrentUser:
    if not spade_session:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sign in required.")
    with db.session() as conn:
        row = conn.execute(
            "SELECT s.token_hash, s.expires_at, u.id AS uid, u.email, u.is_active "
            "FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token_hash=?",
            (_token_hash(spade_session),),
        ).fetchone()
        if row is None or row["expires_at"] < db.now() or not row["is_active"]:
            if row is not None:
                conn.execute("DELETE FROM sessions WHERE token_hash=?", (row["token_hash"],))
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session expired.")
        now = db.now()
        conn.execute("UPDATE sessions SET last_seen=? WHERE token_hash=?", (now, row["token_hash"]))
    return CurrentUser(id=row["uid"], email=row["email"])


CurrentUserDep = Depends(get_current_user)


def optional_user(
    request: Request,
    spade_session: str | None = Cookie(default=None, alias=config.SESSION_COOKIE),
) -> CurrentUser | None:
    try:
        return get_current_user(request, spade_session)
    except HTTPException:
        return None
