"""
wger API client.

Endpoint notes (verify against live instance):
  - /api/v2/workoutsession/ — list sessions with date, workout, notes
  - /api/v2/log/            — individual exercise logs per session
  - /api/v2/workout/        — workout definitions
  - /api/v2/routine/        — routine definitions (newer wger versions)
  - /api/v2/exercise/       — exercise catalog

Token format: Authorization: Token <value>
"""

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

SESSIONS_PATH = "/api/v2/workoutsession/"

# Current wger versions expose datetime_start / datetime_end on a session and
# no longer expose `date`, so both the ordering hint and the lower-bound filter
# have to name datetime_start.
SESSION_ORDERING = "-datetime_start"
SESSION_SINCE_PARAM = "datetime_start__gte"

# The current wger exposes performed sets here. /api/v2/log/ is gone (404).
LOG_ENDPOINT = "/api/v2/workoutlog/"


class WgerClientError(Exception):
    """A failed wger request.

    `status_code` carries the HTTP status when there was one, so a caller can
    react to a specific status without parsing the message. The message format
    is unchanged, since existing callers still match on it.
    """

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class WgerClient:
    def __init__(self, base_url: str, token: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Token {token}"}

    async def _get(self, path: str, params: dict | None = None) -> dict[str, Any]:
        url = f"{self._base_url}{path}"
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(url, headers=self._headers, params=params)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as e:
            logger.error("wger API error: %s %s -> %s", e.request.method, path, e.response.status_code)
            raise WgerClientError(
                f"HTTP {e.response.status_code} from {path}",
                status_code=e.response.status_code,
            ) from e
        except httpx.RequestError as e:
            logger.error("wger request error on %s: %s", path, type(e).__name__)
            raise WgerClientError(f"Request failed for {path}") from e

    async def _get_all(self, path: str, params: dict | None = None) -> list[dict]:
        """Fetch all pages from a paginated endpoint."""
        results: list[dict] = []
        base_params = dict(params or {})
        base_params.setdefault("format", "json")
        base_params.setdefault("limit", 100)
        base_params["offset"] = 0

        while True:
            data = await self._get(path, base_params)
            page_results = data.get("results", [])
            results.extend(page_results)
            if data.get("next") is None:
                break
            base_params["offset"] += len(page_results)
            if not page_results:
                break

        return results

    async def get_workout_sessions(self, since: "date | None" = None) -> list[dict]:
        """
        Fetch workout sessions, newest first where the server honours it.
        Endpoint: /api/v2/workoutsession/
        Fields expected: id, datetime_start, datetime_end, workout, notes,
        impression — current versions no longer report a plain `date`.

        `since`: if given, only sessions on or after this day are requested.

        Which filter and ordering parameters this wger version accepts has not
        been verified against a live instance. The server may ignore them — it
        demonstrably ignores the ordering hint and answers oldest first — or
        reject them. A rejected parameter (HTTP 400) is therefore retried once
        without ordering and filter: fetching everything and narrowing it down
        locally is slower but correct, whereas a failed request would look like
        "no new workouts" again. sync.py enforces `since` locally regardless.
        """
        params: dict = {"ordering": SESSION_ORDERING}
        if since is not None:
            params[SESSION_SINCE_PARAM] = since.isoformat()
        try:
            return await self._get_all(SESSIONS_PATH, params)
        except WgerClientError as e:
            if e.status_code != 400:
                raise
            logger.warning(
                "wger rejected the session ordering/filter parameters — "
                "retrying unfiltered and narrowing down locally"
            )
            return await self._get_all(SESSIONS_PATH)

    async def get_exercise_logs(self, session_id: str | None = None) -> list[dict]:
        """
        Fetch WorkoutLog rows — one per performed *set*, not per exercise.

        Endpoint: ``/api/v2/workoutlog/``. A live probe of the deployed wger
        confirms the fields ``id`` (str), ``session`` (str), ``routine`` (int),
        ``exercise`` (int), ``repetitions`` (str), ``weight`` (str) and ``rir``
        (nullable). The older ``/api/v2/log/`` answers 404 on this version and
        is no longer used; ``workout`` and ``reps`` are gone from the payload.

        The name is kept for backward compatibility with the sync orchestrator.

        Filtering by ``session_id`` passes the session UUID through untouched —
        it is a string and must never be coerced to an integer.
        """
        params: dict = {}
        if session_id is not None:
            params["session"] = str(session_id)
        try:
            return await self._get_all(LOG_ENDPOINT, params)
        except WgerClientError as e:
            if "404" in str(e):
                # Not an expected success case on a supported wger version, so
                # this is a warning, not an info line. The sync may still carry
                # on without exercise detail — see sync_workouts().
                logger.warning(
                    "%s returned 404 — this wger version does not expose "
                    "workout logs where expected; continuing without exercise "
                    "detail",
                    LOG_ENDPOINT,
                )
                return []
            raise

    async def get_workouts(self) -> list[dict]:
        """
        Fetch workout definitions.
        Endpoint: /api/v2/workout/
        Fields expected: id, description, creation_date
        """
        return await self._get_all("/api/v2/workout/")

    async def get_routines(self) -> list[dict]:
        """
        Fetch routine definitions (newer wger versions).
        Endpoint: /api/v2/routine/
        May not exist on older wger — returns [] on 404.
        """
        try:
            return await self._get_all("/api/v2/routine/")
        except WgerClientError:
            logger.warning("Routines endpoint unavailable — skipping")
            return []

    async def get_exercises(self, language: int = 2) -> list[dict]:
        """
        Fetch exercise catalog.
        Endpoint: /api/v2/exercise/
        language=2 is English.
        """
        return await self._get_all("/api/v2/exercise/", {"language": language, "format": "json"})

    async def get_exercise_info(self, exercise_id: int) -> dict:
        """
        Fetch full exercise info including translations.
        Endpoint: /api/v2/exerciseinfo/{id}/
        """
        return await self._get(f"/api/v2/exerciseinfo/{exercise_id}/")
