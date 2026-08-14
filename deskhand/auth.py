"""Passwords and session tokens.

Small on purpose. The only decisions here worth stating: passwords are bcrypt
hashed, and the session token the client holds is never stored — only its
SHA-256 digest is, so a dump of `sessions` cannot be replayed as a login.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

import bcrypt

SESSION_TTL = timedelta(days=7)


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), password_hash.encode())
    except ValueError:
        # A malformed hash in the row is a failed login, not a 500.
        return False


def new_session_token() -> tuple[str, str]:
    """Return (token_for_the_client, digest_for_the_database)."""
    token = secrets.token_urlsafe(32)
    return token, hash_token(token)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def session_expiry(now: datetime | None = None) -> datetime:
    return (now or datetime.now(UTC)) + SESSION_TTL
