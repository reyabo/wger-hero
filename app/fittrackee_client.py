"""FitTrackee API client — read-only, and narrow on purpose.

Verified against the FitTrackee 1.3.x API documentation. Three endpoints are
used and no others:

  GET  /api/sports            the instance's sport catalogue
  GET  /api/workouts          the activity collection, paginated
  POST /api/oauth/token       access token refresh

No cookies, no scraping, no frontend endpoint, no undocumented path, and
nothing that writes: this client cannot modify a FitTrackee record because it
never issues anything but GET against the data API.

The response fields taken from a workout are exactly those the reward and quest
rules need. GPS tracks, coordinates, bounds, map ids, titles, notes and
descriptions are dropped at this boundary — they are never returned to the
caller, so no later layer can accidentally store or log them.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime
from typing import Any, Optional

import httpx

from app.fittrackee_oauth import (
    TOKEN_PATH,
    FitTrackeeAuthError,
    TokenSet,
    TokenStore,
)

logger = logging.getLogger(__name__)

SPORTS_PATH = "/api/sports"
WORKOUTS_PATH = "/api/workouts"

# FitTrackee caps the page size; asking for more just gets the cap back.
MAX_PER_PAGE = 100
# A runaway pagination loop would hammer the instance. Far above any real
# history, low enough to end a broken "next page" forever.
MAX_PAGES = 500

# The only workout fields this integration reads. Everything else in the
# response — including every GPS and free-text field — is discarded here.
WORKOUT_FIELDS = (
    "id",
    "sport_id",
    "workout_date",
    "duration",
    "moving",
    "distance",
    "ave_speed",
    "max_speed",
    "ave_hr",
    "max_hr",
    "modification_date",
)


class FitTrackeeClientError(RuntimeError):
    """A sanitized transport or protocol failure. Never carries a token."""


def _sanitize(exc: Exception) -> str:
    """A message safe to store and show.

    Raw exception text from httpx can embed the full request URL, and an
    authorization header or a token in a query string would travel with it. Only
    the exception class and, for HTTP errors, the status code survive.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return f"FitTrackee antwortete mit HTTP {exc.response.status_code}"
    if isinstance(exc, httpx.TimeoutException):
        return "Zeitüberschreitung bei der Verbindung zu FitTrackee"
    if isinstance(exc, httpx.RequestError):
        return "FitTrackee war nicht erreichbar"
    return f"Unerwarteter Fehler beim FitTrackee-Abruf ({type(exc).__name__})"


# ---------------------------------------------------------------------------
# Field parsing
# ---------------------------------------------------------------------------

_DURATION_RE = re.compile(r"^(?:(\d+):)?(\d{1,2}):(\d{2})(?:\.\d+)?$")


def parse_duration(raw: object) -> Optional[int]:
    """FitTrackee durations as seconds. ``"0:42:10"`` → 2530.

    Accepts H:MM:SS and MM:SS, and a plain number of seconds. Anything else is
    None rather than a guess — a workout with an unreadable duration must not
    silently qualify as a ten-minute session.
    """
    if raw is None:
        return None
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return int(raw) if raw >= 0 else None
    text = str(raw).strip()
    if not text:
        return None
    match = _DURATION_RE.match(text)
    if match:
        hours, minutes, seconds = match.groups()
        return int(hours or 0) * 3600 + int(minutes) * 60 + int(seconds)
    try:  # a bare number of seconds
        value = float(text)
    except ValueError:
        return None
    return int(value) if value >= 0 else None


def parse_workout_date(raw: object) -> Optional[datetime]:
    """FitTrackee's RFC-1123 date to a naive UTC datetime.

    ``"Mon, 17 Aug 2026 06:30:00 GMT"`` → ``datetime(2026, 8, 17, 6, 30)``.
    ISO 8601 is accepted too, because the API has used it in places.
    """
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw.replace(tzinfo=None) if raw.tzinfo is None else _to_naive_utc(raw)
    text = str(raw).strip()
    if not text:
        return None
    for fmt in ("%a, %d %b %Y %H:%M:%S %Z", "%a, %d %b %Y %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _to_naive_utc(parsed)


def _to_naive_utc(moment: datetime) -> datetime:
    from datetime import timezone

    if moment.tzinfo is None:
        return moment
    return moment.astimezone(timezone.utc).replace(tzinfo=None)


def parse_number(raw: object) -> Optional[float]:
    if raw is None or isinstance(raw, bool):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def parse_int(raw: object) -> Optional[int]:
    value = parse_number(raw)
    return int(value) if value is not None else None


def project_workout(raw: dict) -> dict:
    """Keep only the documented fields this integration uses.

    The projection is the privacy boundary: it happens before anything is
    normalised, hashed, stored or logged, so a GPS track or a workout note has
    no path into Hero at all.
    """
    return {key: raw.get(key) for key in WORKOUT_FIELDS}


# ---------------------------------------------------------------------------
# The client
# ---------------------------------------------------------------------------

class FitTrackeeClient:
    """Read-only access to one FitTrackee instance.

    Holds the token set in memory for the life of one operation and writes a
    refreshed one straight back to the store, so a refresh survives even if the
    caller crashes immediately afterwards.
    """

    def __init__(
        self,
        base_url: str,
        tokens: TokenSet,
        *,
        client_id: str,
        client_secret: str,
        store: Optional[TokenStore] = None,
        timeout: float = 30.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._tokens = tokens
        self._client_id = client_id
        self._client_secret = client_secret
        self._store = store
        self._timeout = timeout

    @property
    def tokens(self) -> TokenSet:
        return self._tokens

    # -- transport ---------------------------------------------------------

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._tokens.access_token}"}

    async def _request_json(self, path: str, params: Optional[dict]) -> dict[str, Any]:
        url = f"{self._base_url}{path}"
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.get(url, headers=self._auth_headers(), params=params)
        response.raise_for_status()
        return response.json()

    async def _get(self, path: str, params: Optional[dict] = None) -> dict[str, Any]:
        """One GET, with at most one refresh-and-retry on a 401.

        Exactly one retry. A refresh that yields a token the server still
        rejects means the authorization is gone, and looping would turn that
        into a request storm against the user's own instance.
        """
        try:
            return await self._request_json(path, params)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 401:
                logger.error(
                    "FitTrackee %s -> HTTP %s", path, exc.response.status_code
                )
                raise FitTrackeeClientError(_sanitize(exc)) from exc
        except httpx.RequestError as exc:
            logger.error("FitTrackee request error on %s: %s", path, type(exc).__name__)
            raise FitTrackeeClientError(_sanitize(exc)) from exc
        except ValueError as exc:  # malformed JSON
            logger.error("FitTrackee returned invalid JSON on %s", path)
            raise FitTrackeeClientError(_sanitize(exc)) from exc

        # 401: refresh once, then try exactly once more.
        logger.info("FitTrackee returned 401 — refreshing the access token once")
        await self.refresh_access_token()
        try:
            return await self._request_json(path, params)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 401:
                raise FitTrackeeAuthError(
                    "FitTrackee lehnt die Verbindung ab. Bitte neu autorisieren."
                ) from exc
            raise FitTrackeeClientError(_sanitize(exc)) from exc
        except (httpx.RequestError, ValueError) as exc:
            raise FitTrackeeClientError(_sanitize(exc)) from exc

    # -- token refresh -----------------------------------------------------

    async def refresh_access_token(self) -> TokenSet:
        """Exchange the refresh token for a new access token.

        On rejection the stored tokens are cleared: keeping a refresh token the
        server has already refused only produces a retry loop on every later
        call. The caller marks the connection as needing re-authorization.
        """
        if not self._tokens.refresh_token:
            raise FitTrackeeAuthError(
                "Kein Refresh-Token vorhanden. Bitte neu mit FitTrackee verbinden."
            )
        data = {
            "grant_type": "refresh_token",
            "refresh_token": self._tokens.refresh_token,
            "client_id": self._client_id,
            "client_secret": self._client_secret,
        }
        url = f"{self._base_url}{TOKEN_PATH}"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(url, data=data)
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (400, 401):
                if self._store is not None:
                    self._store.clear()
                raise FitTrackeeAuthError(
                    "Die FitTrackee-Autorisierung ist abgelaufen. Bitte neu verbinden."
                ) from exc
            raise FitTrackeeClientError(_sanitize(exc)) from exc
        except (httpx.RequestError, ValueError) as exc:
            raise FitTrackeeClientError(_sanitize(exc)) from exc

        self._tokens = TokenSet.from_response(payload)
        if self._store is not None:
            self._store.save(self._tokens)
        return self._tokens

    async def ensure_fresh_token(self) -> None:
        """Refresh proactively when the token is at or near its expiry."""
        if self._tokens.is_expired():
            await self.refresh_access_token()

    # -- endpoints ---------------------------------------------------------

    async def get_sports(self) -> list[dict]:
        """The instance's sports as ``{sport_id, label, is_active}``.

        Ids are per-instance. Nothing is inferred from a name here — the caller
        stores them inert and the user decides which count as endurance.
        """
        payload = await self._get(SPORTS_PATH)
        raw = (payload.get("data") or {}).get("sports")
        if raw is None:
            raw = payload.get("sports") or []
        sports = []
        for entry in raw:
            sport_id = parse_int(entry.get("id"))
            if sport_id is None:
                continue
            sports.append(
                {
                    "sport_id": sport_id,
                    "label": str(entry.get("label") or f"Sport {sport_id}"),
                    "is_active": bool(entry.get("is_active", True)),
                }
            )
        return sports

    async def get_workouts(
        self, from_date: Optional[date] = None, to_date: Optional[date] = None
    ) -> list[dict]:
        """Every workout in the window, following pagination to the end.

        The first page is never assumed to be the whole collection. Paging stops
        when a page comes back empty or shorter than the requested size, and a
        page that repeats ids already seen ends it too — a server that ignores
        the page parameter would otherwise loop forever.
        """
        params: dict[str, Any] = {"per_page": MAX_PER_PAGE, "order": "asc"}
        if from_date is not None:
            params["from"] = from_date.isoformat()
        if to_date is not None:
            params["to"] = to_date.isoformat()

        collected: list[dict] = []
        seen: set[str] = set()

        for page in range(1, MAX_PAGES + 1):
            payload = await self._get(WORKOUTS_PATH, {**params, "page": page})
            raw = (payload.get("data") or {}).get("workouts")
            if raw is None:
                raw = payload.get("workouts") or []
            if not raw:
                break

            fresh = 0
            for entry in raw:
                identifier = entry.get("id")
                if identifier is None:
                    continue
                key = str(identifier)
                if key in seen:
                    continue
                seen.add(key)
                collected.append(project_workout(entry))
                fresh += 1

            if fresh == 0:
                logger.warning("FitTrackee page %d repeated known ids — stopping", page)
                break
            if len(raw) < MAX_PER_PAGE:
                break
        else:
            logger.warning("FitTrackee pagination hit the %d page cap", MAX_PAGES)

        return collected


async def exchange_code_for_tokens(
    base_url: str,
    *,
    code: str,
    code_verifier: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    timeout: float = 30.0,
) -> TokenSet:
    """Trade an authorization code plus PKCE verifier for tokens."""
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "client_secret": client_secret,
        "code_verifier": code_verifier,
    }
    url = f"{base_url.rstrip('/')}{TOKEN_PATH}"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(url, data=data)
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPStatusError as exc:
        logger.error("FitTrackee token exchange failed: HTTP %s", exc.response.status_code)
        raise FitTrackeeAuthError(
            "FitTrackee hat den Autorisierungscode abgelehnt."
        ) from exc
    except (httpx.RequestError, ValueError) as exc:
        logger.error("FitTrackee token exchange error: %s", type(exc).__name__)
        raise FitTrackeeClientError(_sanitize(exc)) from exc
    return TokenSet.from_response(payload)
