"""
Diagnostic CLI for wger-hero.

Usage:
    python -m app.diagnostics wger

Prints a sanitized structural summary of the wger API:
  - Which endpoints are reachable
  - HTTP status codes
  - Number of results
  - Field names found in the first result (not values)
  - Whether normalization succeeds

Never prints:
  - The API token or any part of it
  - Workout notes or descriptions
  - Raw API response values
  - Personal data of any kind
"""

import asyncio
import sys
from typing import Optional

import httpx

CANDIDATE_ENDPOINTS = [
    "/api/v2/workoutsession/",
    "/api/v2/log/",
    "/api/v2/workout/",
    "/api/v2/routine/",
    "/api/v2/exercise/",
]

# Endpoints current wger versions no longer serve. The sync already copes with
# their absence — get_exercise_logs() returns [] on 404 and the exercise
# catalog is only used to resolve names — so a 404 here is a fact about the
# instance, not a defect. Reporting it as a problem sends the reader looking
# for a fault that is not there.
OPTIONAL_ENDPOINTS = frozenset({
    "/api/v2/log/",
    "/api/v2/workout/",
})

# Field names a session may carry its day in, newest wger first. Kept in sync
# with app.sync.session_date_str, which is where the rule actually lives.
SESSION_DATE_FIELDS = ("datetime_start", "date")


def _field_names(data: dict) -> list[str]:
    """Return sorted field names from the first result item — never field values."""
    results = data.get("results", [])
    if results and isinstance(results[0], dict):
        return sorted(results[0].keys())
    if isinstance(data, dict):
        return sorted(data.keys())
    return []


def _result_count(data: dict) -> int:
    results = data.get("results")
    if isinstance(results, list):
        return len(results)
    if isinstance(data, list):
        return len(data)
    return 0


async def probe_endpoint(
    base_url: str,
    token: str,
    path: str,
) -> tuple[Optional[int], Optional[int], Optional[list[str]], Optional[str]]:
    """
    Probe a single endpoint.
    Returns (http_status, result_count, field_names, error_message).
    Never logs the token or response values.
    """
    url = f"{base_url.rstrip('/')}{path}"
    headers = {"Authorization": f"Token {token}"}
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, headers=headers, params={"format": "json", "limit": 5})
        status = resp.status_code
        if status == 200:
            try:
                data = resp.json()
                count = _result_count(data)
                fields = _field_names(data)
                return status, count, fields, None
            except Exception:
                return status, None, None, "Could not parse JSON response"
        return status, None, None, _http_status_hint(status)
    except httpx.ConnectError:
        return None, None, None, "Connection refused or DNS failure"
    except httpx.TimeoutException:
        return None, None, None, "Request timed out"
    except httpx.RequestError:
        return None, None, None, "Network error"


def _print_session_date_field(fields: list[str]) -> None:
    """Name the field the sync will read a session's day from.

    This is the field that broke the import once, so it is worth stating
    explicitly rather than leaving the reader to spot it in the field list.
    Only field names are printed — never a value.
    """
    present = [f for f in SESSION_DATE_FIELDS if f in fields]
    if present:
        print(f"    Date field recognized : {present[0]}")
    else:
        print(
            "    Date field recognized : NONE — no date field the sync "
            f"understands (looked for: {', '.join(SESSION_DATE_FIELDS)})"
        )


def _http_status_hint(status: int) -> str:
    hints = {
        401: "Unauthorized — check API token",
        403: "Forbidden — token may lack permissions",
        404: "Not Found — endpoint may not exist on this wger version",
        429: "Too Many Requests — rate limited",
    }
    if status >= 500:
        return f"Server Error ({status})"
    return hints.get(status, f"Unexpected status {status}")


async def run_diagnostics(base_url: str, token: str) -> None:
    """Run all diagnostics and print sanitized results to stdout."""
    sep = "=" * 52
    print(sep)
    print("  wger-hero — API Diagnostics")
    print(sep)

    # Never print the token itself
    print(f"Base URL : {base_url}")
    print(f"Token    : {'present (not shown)' if token else 'MISSING'}")
    print()

    sessions_data: Optional[dict] = None

    for path in CANDIDATE_ENDPOINTS:
        status, count, fields, error = await probe_endpoint(base_url, token, path)

        if status == 200 and count is not None:
            print(f"  {path}")
            print(f"    → 200 OK, {count} result(s)")
            if fields:
                print(f"    Fields : {', '.join(fields)}")
            if path == "/api/v2/workoutsession/" and count > 0:
                # Store for normalization test — only structural info, not values
                sessions_data = {"result_count": count, "fields": fields}
                _print_session_date_field(fields or [])
        elif status is not None:
            if status == 404 and path in OPTIONAL_ENDPOINTS:
                print(f"  {path}")
                print("    → 404 — expected: not served by this wger version")
            else:
                hint = error or _http_status_hint(status)
                print(f"  {path}")
                print(f"    → {status} — {hint}")
        else:
            print(f"  {path}")
            print(f"    → UNREACHABLE — {error}")
        print()

    # Normalization smoke-test using synthetic minimal data (no real data printed)
    print("Normalization smoke-test:")
    _run_normalization_test()


def _run_normalization_test() -> None:
    """
    Test normalization with a synthetic session dict.
    Prints only structural result (counts and field names), never real data.
    """
    from app.sync import NormalizedWorkoutLog, _normalize_session

    # Current wger shape: datetime_start / datetime_end, no plain date.
    synthetic_session = {
        "id": 1,
        "datetime_start": "2026-09-25T18:30:00+02:00",
        "datetime_end": "2026-09-25T19:45:00+02:00",
        "workout": 1,
        "notes": "[redacted for test]",
    }
    synthetic_logs = [
        {"id": 1, "exercise": 10, "reps": 8, "weight": 60, "rir": 2, "workout": 1},
        {"id": 2, "exercise": 20, "reps": None, "weight": None, "rir": None, "workout": 1},
    ]
    try:
        result = _normalize_session(synthetic_session, synthetic_logs, {10: "Exercise A", 20: "Exercise B"})
        print(f"  Synthetic session (datetime_start) normalized OK")
        print(f"  source_id : {result.source_id}")
        print(f"  date      : {result.date}")
        print(f"  exercises : {len(result.exercises)}")
        print(f"  fields    : {', '.join(NormalizedWorkoutLog.model_fields.keys())}")
    except Exception as e:
        print(f"  Normalization FAILED: {type(e).__name__}")

    # Older wger shape, still supported.
    legacy = {"id": 2, "date": "2024-01-15", "workout": 1}
    try:
        result_legacy = _normalize_session(legacy, [], {})
        print(f"  Legacy session (date) normalized OK → date={result_legacy.date}")
    except Exception as e:
        print(f"  Legacy session normalization FAILED: {type(e).__name__}")

    # Test with missing optional fields
    minimal = {"id": 99}
    try:
        result2 = _normalize_session(minimal, [], {})
        print(f"  Minimal session (missing date/workout) normalized OK → date={result2.date}")
    except Exception as e:
        print(f"  Minimal session normalization FAILED: {type(e).__name__}")


async def _main(command: str) -> None:
    if command != "wger":
        print(f"Unknown command: {command!r}. Available: wger", file=sys.stderr)
        sys.exit(1)

    from app.config import get_settings

    settings = get_settings()
    try:
        token = settings.get_token()
    except RuntimeError as e:
        print(f"Token error: {e}", file=sys.stderr)
        print("Set WGER_API_TOKEN, WGER_API_TOKEN_FILE, or mount /run/secrets/wger_api_token", file=sys.stderr)
        sys.exit(2)

    await run_diagnostics(base_url=settings.WGER_BASE_URL, token=token)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    asyncio.run(_main(cmd))
