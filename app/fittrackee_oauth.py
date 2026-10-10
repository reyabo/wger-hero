"""OAuth2 for FitTrackee: PKCE, state, and a file-backed token store.

FitTrackee exposes third-party access through OAuth2 Authorization Code with
PKCE. This module owns the parts that must not be spread around: building the
authorization URL, verifying the returned ``state``, and reading and writing the
tokens.

Three rules shape everything here.

**Read-only scope.** Only ``workouts:read`` is ever requested. Hero reads
activities and writes nothing back to FitTrackee, so no write scope can be
justified — and a scope that is never requested cannot be abused.

**Tokens never enter the database.** They live in one file outside it, so a
database backup, an export or a page render can never carry a usable
credential. The static client secret lives in a *different*, read-only file, so
a token refresh can never overwrite it.

**Nothing here is logged.** No token, no refresh token, no code, no verifier and
no client secret reaches a log line, an error message or an HTTP response. The
functions raise messages that name what failed, never the value that failed.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

logger = logging.getLogger(__name__)

# The one scope this integration needs. FitTrackee documents workouts:read for
# GET /api/workouts and GET /api/sports.
REQUIRED_SCOPE = "workouts:read"

# Conventional container locations. The secret is read-only; the token store is
# the only writable path, and it is a directory of its own so a token write can
# never touch the secret.
DEFAULT_CLIENT_SECRET_FILE = Path("/run/secrets/fittrackee_client_secret")
DEFAULT_TOKEN_DIR = Path("/run/wger-hero-fittrackee")
TOKEN_FILENAME = "tokens.json"

# Refresh this long before the token actually expires, so a sync that starts
# just under the wire does not fail halfway through.
EXPIRY_MARGIN_SECONDS = 120

AUTHORIZE_PATH = "/profile/apps/authorize"
TOKEN_PATH = "/api/oauth/token"


class FitTrackeeAuthError(RuntimeError):
    """Authorization is missing, invalid or expired beyond refreshing."""


# ---------------------------------------------------------------------------
# PKCE and state
# ---------------------------------------------------------------------------

def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def generate_code_verifier() -> str:
    """A high-entropy PKCE verifier (RFC 7636 allows 43–128 characters)."""
    return _b64url(secrets.token_bytes(64))


def code_challenge_for(verifier: str) -> str:
    """The S256 challenge. Plain is never offered — S256 or nothing."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return _b64url(digest)


def generate_state() -> str:
    """CSRF protection for the redirect back from FitTrackee."""
    return _b64url(secrets.token_bytes(32))


def state_matches(expected: Optional[str], received: Optional[str]) -> bool:
    """Constant-time comparison that treats a missing value as a mismatch."""
    if not expected or not received:
        return False
    return secrets.compare_digest(str(expected), str(received))


@dataclass
class AuthorizationRequest:
    """Everything one authorization attempt needs, in one object.

    ``verifier`` and ``state`` are held by the session for the callback to check
    and are never rendered into the page.
    """

    url: str
    state: str
    verifier: str


def build_authorization_request(
    base_url: str, client_id: str, redirect_uri: str
) -> AuthorizationRequest:
    verifier = generate_code_verifier()
    state = generate_state()
    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": REQUIRED_SCOPE,
        "state": state,
        "code_challenge": code_challenge_for(verifier),
        "code_challenge_method": "S256",
    }
    url = f"{base_url.rstrip('/')}{AUTHORIZE_PATH}?{urlencode(params)}"
    return AuthorizationRequest(url=url, state=state, verifier=verifier)


# ---------------------------------------------------------------------------
# The token store
# ---------------------------------------------------------------------------

@dataclass
class TokenSet:
    access_token: str
    refresh_token: Optional[str] = None
    expires_at: Optional[float] = None
    scope: str = REQUIRED_SCOPE

    def is_expired(self, now: Optional[float] = None) -> bool:
        """Treat "about to expire" as expired — see EXPIRY_MARGIN_SECONDS."""
        if self.expires_at is None:
            return False
        return (now if now is not None else time.time()) >= (
            self.expires_at - EXPIRY_MARGIN_SECONDS
        )

    def to_payload(self) -> dict:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
            "scope": self.scope,
        }

    @classmethod
    def from_payload(cls, payload: dict) -> "TokenSet":
        access = payload.get("access_token")
        if not access:
            raise FitTrackeeAuthError("Token store contains no access token")
        return cls(
            access_token=str(access),
            refresh_token=payload.get("refresh_token") or None,
            expires_at=payload.get("expires_at"),
            scope=str(payload.get("scope") or REQUIRED_SCOPE),
        )

    @classmethod
    def from_response(cls, data: dict, now: Optional[float] = None) -> "TokenSet":
        """Build from a token endpoint response, converting expires_in."""
        access = data.get("access_token")
        if not access:
            raise FitTrackeeAuthError("Token response contained no access token")
        expires_in = data.get("expires_in")
        expires_at = None
        if expires_in is not None:
            try:
                expires_at = (now if now is not None else time.time()) + float(expires_in)
            except (TypeError, ValueError):
                expires_at = None
        return cls(
            access_token=str(access),
            refresh_token=data.get("refresh_token") or None,
            expires_at=expires_at,
            scope=str(data.get("scope") or REQUIRED_SCOPE),
        )


class TokenStore:
    """Tokens on disk, written atomically.

    A half-written token file is worse than no token file: it costs the refresh
    token and forces a re-authorization. So a write goes to a temporary file in
    the same directory, is flushed and fsynced, and only then replaces the real
    one — os.replace is atomic within a filesystem.
    """

    def __init__(self, directory: Optional[Path] = None) -> None:
        self._dir = Path(directory) if directory else DEFAULT_TOKEN_DIR
        self._path = self._dir / TOKEN_FILENAME

    @property
    def path(self) -> Path:
        return self._path

    def exists(self) -> bool:
        return self._path.is_file()

    def load(self) -> Optional[TokenSet]:
        """The stored tokens, or None when there are none.

        A corrupt file is reported as "no authorization" rather than raising
        into a request: the user's fix is the same either way — reconnect.
        """
        if not self._path.is_file():
            return None
        try:
            payload = json.loads(self._path.read_text())
        except (OSError, ValueError) as exc:
            logger.error(
                "FitTrackee token store unreadable (%s) — reconnect required",
                type(exc).__name__,
            )
            return None
        try:
            return TokenSet.from_payload(payload)
        except FitTrackeeAuthError:
            logger.error("FitTrackee token store has no access token")
            return None

    def save(self, tokens: TokenSet) -> None:
        self._dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        # A pre-existing directory keeps its mode from mkdir, so set it too.
        try:
            os.chmod(self._dir, 0o700)
        except OSError:  # a read-only bind mount would fail here; not fatal
            logger.warning("Could not tighten the token directory mode")

        fd, tmp_name = tempfile.mkstemp(dir=str(self._dir), prefix=".tokens-")
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(tokens.to_payload(), handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, self._path)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
        logger.info("FitTrackee tokens stored")   # deliberately says nothing more

    def clear(self) -> None:
        """Drop the stored tokens — used when a refresh token is rejected."""
        self._path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Client credentials
# ---------------------------------------------------------------------------

def load_client_secret(settings) -> str:
    """The OAuth client secret from its read-only file. Never from env."""
    explicit = getattr(settings, "FITTRACKEE_CLIENT_SECRET_FILE", None)
    path = Path(explicit) if explicit else DEFAULT_CLIENT_SECRET_FILE
    try:
        value = path.read_text().strip()
    except OSError as exc:
        raise FitTrackeeAuthError(
            f"Cannot read the FitTrackee client secret from {path}: {exc.strerror}"
        ) from exc
    if not value:
        raise FitTrackeeAuthError(f"FitTrackee client secret file {path} is empty")
    return value


def token_store_for(settings) -> TokenStore:
    directory = getattr(settings, "FITTRACKEE_TOKEN_DIR", None)
    return TokenStore(Path(directory) if directory else None)


def is_configured(settings) -> bool:
    """Whether a connection could even be attempted. Reads no secret value."""
    return bool(
        getattr(settings, "FITTRACKEE_BASE_URL", None)
        and getattr(settings, "FITTRACKEE_CLIENT_ID", None)
    )
