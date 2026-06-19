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


class WgerClientError(Exception):
    pass


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
            raise WgerClientError(f"HTTP {e.response.status_code} from {path}") from e
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
        Fetch workout sessions ordered by date descending.
        Endpoint: /api/v2/workoutsession/
        Fields expected: id, date, workout, notes, impression

        `since`: if given, only sessions on or after this date are fetched
        (uses the wger date__gte filter parameter).
        """
        from datetime import date as _date
        params: dict = {"ordering": "-date"}
        if since is not None:
            params["date__gte"] = since.isoformat()
        return await self._get_all("/api/v2/workoutsession/", params)

    async def get_exercise_logs(self, workout_id: int | None = None) -> list[dict]:
        """
        Fetch exercise logs (sets/reps/weight/rir per session).
        Endpoint: /api/v2/log/
        Fields expected: id, exercise, reps, weight, rir, date, workout

        Returns [] on 404 — endpoint does not exist on all wger versions.
        """
        params: dict = {}
        if workout_id is not None:
            params["workout"] = workout_id
        try:
            return await self._get_all("/api/v2/log/", params)
        except WgerClientError as e:
            if "404" in str(e):
                logger.info("Exercise log endpoint not available on this wger version — skipping")
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
