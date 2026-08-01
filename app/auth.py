"""
Single-user access protection: password check, signed session, CSRF, rate limit.

Deliberately small. There is exactly one user, so there is no user table, no
registration, no password change and no role model — the password hash lives in
a read-only mounted secret file and the session is a signed cookie.

Everything here is pure enough to unit-test without a database or a request:
the only I/O is reading the two secret files.
"""

from __future__ import annotations

import hmac
import logging
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

logger = logging.getLogger(__name__)

SESSION_COOKIE = "hero_session"
CSRF_FIELD = "csrf_token"

# Conventional Docker secret locations, used when no explicit path is set.
_DEFAULT_HASH_FILE = Path("/run/secrets/hero_password_hash")
_DEFAULT_SECRET_FILE = Path("/run/secrets/hero_session_secret")

# Paths that must stay reachable without a session.
PUBLIC_PATHS = frozenset({
    "/healthz",
    "/login",
    "/manifest.webmanifest",
    "/sw.js",
    "/offline",
})
PUBLIC_PREFIXES = ("/static/",)

_hasher = PasswordHasher()


class AuthConfigError(RuntimeError):
    """Raised when auth is enabled but its secrets are missing or unusable."""


# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------

def _read_secret(explicit: Optional[str], fallback: Path, what: str) -> str:
    path = Path(explicit) if explicit else fallback
    try:
        value = path.read_text().strip()
    except OSError as exc:
        raise AuthConfigError(f"Cannot read {what} from {path}: {exc.strerror}") from exc
    if not value:
        raise AuthConfigError(f"{what} file {path} is empty")
    return value


def load_password_hash(settings) -> str:
    """Argon2 hash of the single user's password. Never logged, never rendered."""
    return _read_secret(
        getattr(settings, "AUTH_PASSWORD_HASH_FILE", None),
        _DEFAULT_HASH_FILE,
        "password hash",
    )


def load_session_secret(settings) -> str:
    """Key used to sign session cookies. Never logged, never rendered."""
    return _read_secret(
        getattr(settings, "SESSION_SECRET_FILE", None),
        _DEFAULT_SECRET_FILE,
        "session secret",
    )


def hash_password(password: str) -> str:
    """Only used by the documented CLI recipe, never by the running app."""
    return _hasher.hash(password)


def verify_password(password: str, stored_hash: str) -> bool:
    """Constant-time-ish Argon2 verification. Never raises on a wrong password."""
    try:
        return _hasher.verify(stored_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------

@dataclass
class SessionData:
    """What the signed cookie carries. Deliberately tiny — no personal data."""

    csrf: str
    authenticated: bool = False

    def to_payload(self) -> dict:
        return {"c": self.csrf, "a": self.authenticated}

    @classmethod
    def from_payload(cls, payload: dict) -> "SessionData":
        return cls(csrf=str(payload.get("c", "")), authenticated=bool(payload.get("a")))


def new_session(authenticated: bool = False) -> SessionData:
    return SessionData(csrf=secrets.token_urlsafe(32), authenticated=authenticated)


def _serializer(secret: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(secret, salt="wger-hero-session")


def serialize_session(session: SessionData, secret: str) -> str:
    return _serializer(secret).dumps(session.to_payload())


def deserialize_session(
    raw: Optional[str], secret: str, max_age_seconds: int
) -> Optional[SessionData]:
    """Return the session, or None if absent, expired or tampered with."""
    if not raw:
        return None
    try:
        payload = _serializer(secret).loads(raw, max_age=max_age_seconds)
    except SignatureExpired:
        logger.info("Session expired")
        return None
    except BadSignature:
        # Do not log the value — it is attacker-controlled.
        logger.warning("Rejected a session cookie with an invalid signature")
        return None
    if not isinstance(payload, dict):
        return None
    return SessionData.from_payload(payload)


# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------

def csrf_ok(session: Optional[SessionData], submitted: Optional[str]) -> bool:
    """Compare the submitted token against the one bound to the session."""
    if session is None or not session.csrf or not submitted:
        return False
    return hmac.compare_digest(session.csrf, submitted)


def is_public_path(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)


def is_state_changing(method: str) -> bool:
    return method.upper() in {"POST", "PUT", "PATCH", "DELETE"}


# ---------------------------------------------------------------------------
# Login rate limiting (in-process, no extra service)
# ---------------------------------------------------------------------------

@dataclass
class LoginRateLimiter:
    """Sliding-window limiter for failed logins.

    In-process on purpose: this is a single-user app on one container, so a
    dict is enough and adding Redis for it would be absurd. State is lost on
    restart, which is acceptable — an attacker cannot trigger a restart.
    """

    max_attempts: int = 5
    window_seconds: int = 300
    _failures: dict[str, list[float]] = field(default_factory=dict)

    def _prune(self, key: str, now: float) -> list[float]:
        recent = [t for t in self._failures.get(key, []) if now - t < self.window_seconds]
        if recent:
            self._failures[key] = recent
        else:
            self._failures.pop(key, None)
        return recent

    def is_blocked(self, key: str, now: Optional[float] = None) -> bool:
        now = time.monotonic() if now is None else now
        return len(self._prune(key, now)) >= self.max_attempts

    def record_failure(self, key: str, now: Optional[float] = None) -> None:
        now = time.monotonic() if now is None else now
        self._prune(key, now)
        self._failures.setdefault(key, []).append(now)

    def reset(self, key: str) -> None:
        self._failures.pop(key, None)

    def retry_after(self, key: str, now: Optional[float] = None) -> int:
        """Seconds until the oldest failure leaves the window (0 if unblocked)."""
        now = time.monotonic() if now is None else now
        recent = self._prune(key, now)
        if len(recent) < self.max_attempts:
            return 0
        return max(0, int(self.window_seconds - (now - min(recent))) + 1)
