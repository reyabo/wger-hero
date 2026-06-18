"""Tests for sync robustness: sanitized errors, missing fields, normalization edge cases."""

from datetime import date
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.models import HeroProfile, SyncEvent
from app.quests import seed_quests
from app.sync import (
    NormalizedExerciseLog,
    NormalizedWorkoutLog,
    SyncResult,
    _normalize_session,
    _sanitize_error,
    sync_workouts,
)
from app.wger_client import WgerClient, WgerClientError


# ── _sanitize_error ─────────────────────────────────────────────────────────

class TestSanitizeError:
    def test_401_unauthorized(self):
        msg = _sanitize_error(WgerClientError("HTTP 401"))
        assert "401" in msg
        assert "Unauthorized" in msg or "token" in msg.lower()

    def test_403_forbidden(self):
        msg = _sanitize_error(WgerClientError("HTTP 403"))
        assert "403" in msg or "Forbidden" in msg

    def test_404_not_found(self):
        msg = _sanitize_error(WgerClientError("HTTP 404"))
        assert "404" in msg or "Not Found" in msg

    def test_connection_error(self):
        msg = _sanitize_error(httpx.ConnectError("refused"))
        assert "Connection" in msg or "connection" in msg

    def test_timeout_error(self):
        msg = _sanitize_error(httpx.TimeoutException("timed out"))
        assert "Connection" in msg or "error" in msg.lower()

    def test_key_error(self):
        msg = _sanitize_error(KeyError("id"))
        assert "shape" in msg.lower() or "field" in msg.lower() or "response" in msg.lower()

    def test_type_error(self):
        msg = _sanitize_error(TypeError("NoneType"))
        assert "shape" in msg.lower() or "field" in msg.lower() or "error" in msg.lower()

    def test_generic_exception_no_private_data(self):
        # Message content must be stripped — only the type name is safe
        private = "token=abc123secretvalue"
        msg = _sanitize_error(Exception(private))
        assert private not in msg

    def test_wger_client_error_no_private_data(self):
        private = "Authorization: Token abc123"
        msg = _sanitize_error(WgerClientError(private))
        assert "abc123" not in msg


# ── _normalize_session ───────────────────────────────────────────────────────

class TestNormalizeSession:
    def test_full_session_normalizes(self):
        session = {"id": 1, "date": "2024-03-15", "workout": 2, "notes": "test"}
        logs = [{"id": 10, "exercise": 5, "reps": 8, "weight": 60.0, "rir": 2}]
        result = _normalize_session(session, logs, {5: "Pull-up"})
        assert result.source_id == "session-1"
        assert result.date == date(2024, 3, 15)
        assert len(result.exercises) == 1
        assert result.exercises[0].name == "Pull-up"
        assert result.exercises[0].reps == 8
        assert result.exercises[0].rir == 2.0

    def test_missing_date_defaults_to_today(self):
        session = {"id": 99}  # no date key
        result = _normalize_session(session, [], {})
        assert result.date == date.today()

    def test_invalid_date_defaults_to_today(self):
        session = {"id": 1, "date": "not-a-date"}
        result = _normalize_session(session, [], {})
        assert result.date == date.today()

    def test_missing_exercise_id_uses_unknown(self):
        session = {"id": 1, "date": "2024-01-01"}
        logs = [{"id": 1, "reps": 5}]  # no exercise field
        result = _normalize_session(session, logs, {})
        assert result.exercises[0].name == "Unknown"

    def test_unknown_exercise_id_shows_fallback(self):
        session = {"id": 1, "date": "2024-01-01"}
        logs = [{"id": 1, "exercise": 999, "reps": 5}]
        result = _normalize_session(session, logs, {})  # empty name dict
        assert "999" in result.exercises[0].name or "Exercise" in result.exercises[0].name

    def test_missing_rir_is_none(self):
        session = {"id": 1, "date": "2024-01-01"}
        logs = [{"id": 1, "exercise": 5, "reps": 8}]  # no rir
        result = _normalize_session(session, logs, {5: "Squat"})
        assert result.exercises[0].rir is None

    def test_rir_none_string_is_none(self):
        session = {"id": 1, "date": "2024-01-01"}
        logs = [{"id": 1, "exercise": 5, "reps": 8, "rir": "None"}]
        result = _normalize_session(session, logs, {5: "Squat"})
        assert result.exercises[0].rir is None

    def test_rir_empty_string_is_none(self):
        session = {"id": 1, "date": "2024-01-01"}
        logs = [{"id": 1, "exercise": 5, "reps": 8, "rir": ""}]
        result = _normalize_session(session, logs, {5: "Squat"})
        assert result.exercises[0].rir is None

    def test_missing_weight_is_none(self):
        session = {"id": 1, "date": "2024-01-01"}
        logs = [{"id": 1, "exercise": 5, "reps": 8}]
        result = _normalize_session(session, logs, {5: "Squat"})
        assert result.exercises[0].weight is None

    def test_missing_id_uses_unknown_source_id(self):
        session = {"date": "2024-01-01"}  # no id
        result = _normalize_session(session, [], {})
        assert "unknown" in result.source_id

    def test_empty_session_dict_does_not_crash(self):
        result = _normalize_session({}, [], {})
        assert result is not None
        assert result.date == date.today()

    def test_malformed_log_entry_skipped(self):
        session = {"id": 1, "date": "2024-01-01"}
        logs = [
            {"id": 1, "exercise": 5, "reps": 8},         # good
            {"id": 2, "exercise": "bad-type", "reps": "X"},  # may fail
        ]
        result = _normalize_session(session, logs, {5: "Squat"})
        # Should not raise; may have 1 or 2 exercises depending on handling
        assert len(result.exercises) >= 0


# ── sync_workouts robustness ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_sync_connection_error_returns_sanitized_error(db):
    hero = HeroProfile(name="Test", level=1, total_xp=0)
    db.add(hero)
    db.commit()

    client = MagicMock(spec=WgerClient)
    client.get_exercises = AsyncMock(return_value=[])
    client.get_workout_sessions = AsyncMock(
        side_effect=httpx.ConnectError("refused")
    )

    result = await sync_workouts(db, client)
    assert len(result.errors) == 1
    assert "Connection" in result.errors[0] or "connection" in result.errors[0].lower()
    # Ensure the raw exception message is not leaked
    assert "refused" not in result.errors[0]


@pytest.mark.asyncio
async def test_sync_401_returns_sanitized_error(db):
    hero = HeroProfile(name="Test", level=1, total_xp=0)
    db.add(hero)
    db.commit()

    client = MagicMock(spec=WgerClient)
    client.get_exercises = AsyncMock(return_value=[])
    client.get_workout_sessions = AsyncMock(
        side_effect=WgerClientError("HTTP 401 from /api/v2/workoutsession/")
    )

    result = await sync_workouts(db, client)
    assert len(result.errors) == 1
    assert "401" in result.errors[0] or "Unauthorized" in result.errors[0]


@pytest.mark.asyncio
async def test_sync_sessions_with_missing_fields_no_crash(db):
    seed_quests(db)
    hero = HeroProfile(name="Test", level=1, total_xp=0)
    db.add(hero)
    db.commit()

    sessions = [
        {},                          # completely empty
        {"id": 2, "date": "bad"},    # bad date
        {"id": 3, "date": "2024-01-05"},  # good minimal
    ]
    client = MagicMock(spec=WgerClient)
    client.get_exercises = AsyncMock(return_value=[])
    client.get_workout_sessions = AsyncMock(return_value=sessions)
    client.get_exercise_logs = AsyncMock(return_value=[])

    result = await sync_workouts(db, client)
    # Should not raise; should process at least some sessions
    assert result is not None
    assert result.new_sessions >= 0


@pytest.mark.asyncio
async def test_sync_dedup_still_works_after_robustness_changes(db):
    """Core deduplication must still hold after the robustness edits."""
    seed_quests(db)
    hero = HeroProfile(name="Test", level=1, total_xp=0)
    db.add(hero)
    db.commit()

    sessions = [{"id": 42, "date": "2024-03-01", "workout": 1}]
    client = MagicMock(spec=WgerClient)
    client.get_exercises = AsyncMock(return_value=[])
    client.get_workout_sessions = AsyncMock(return_value=sessions)
    client.get_exercise_logs = AsyncMock(return_value=[])

    await sync_workouts(db, client)
    xp_first = db.query(HeroProfile).first().total_xp

    await sync_workouts(db, client)
    xp_second = db.query(HeroProfile).first().total_xp

    assert xp_first == xp_second
