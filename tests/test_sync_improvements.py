"""Tests for the four sync improvements from live testing."""

from datetime import date
from unittest.mock import AsyncMock, MagicMock, call

import pytest

from app.models import HeroProfile
from app.quests import seed_quests
from app.sync import sync_workouts
from app.wger_client import WgerClient, WgerClientError


def _client(sessions=None, logs=None, exercises=None, log_exc=None):
    c = MagicMock(spec=WgerClient)
    c.get_workout_sessions = AsyncMock(return_value=sessions or [])
    c.get_exercises = AsyncMock(return_value=exercises or [])
    if log_exc is not None:
        c.get_exercise_logs = AsyncMock(side_effect=log_exc)
    else:
        c.get_exercise_logs = AsyncMock(return_value=logs or [])
    return c


SESSIONS = [{"id": 1, "date": "2024-03-01", "workout": 1}]


# ── 1. /api/v2/log/ 404 treated as expected, not an error ────────────────────

@pytest.mark.asyncio
async def test_log_404_returns_empty_not_error(db):
    """WgerClient.get_exercise_logs must return [] silently on 404."""
    from app.wger_client import WgerClient as RealClient

    # Patch _get_all to raise 404 for /log/ path
    async def fake_get_all(path, params=None):
        if "/log/" in path:
            raise WgerClientError("HTTP 404 from /api/v2/log/")
        return []

    client = RealClient.__new__(RealClient)
    client._base_url = "https://wger.example.com"
    client._headers = {}
    client._get_all = fake_get_all

    result = await client.get_exercise_logs()
    assert result == []  # 404 → silent empty list, no exception


@pytest.mark.asyncio
async def test_log_404_produces_no_sync_error(db):
    """A 404 on /log/ must not appear in SyncResult.errors."""
    seed_quests(db)
    hero = HeroProfile(name="Test", level=1, total_xp=0)
    db.add(hero)
    db.commit()

    # get_exercise_logs returns [] (already absorbed 404 inside the client)
    client = _client(sessions=SESSIONS, logs=[])
    result = await sync_workouts(db, client)
    assert result.errors == []
    assert result.new_sessions == 1


# ── 2. Exercise catalog skipped when logs unavailable ────────────────────────

@pytest.mark.asyncio
async def test_exercise_catalog_not_fetched_when_logs_empty(db):
    """If log fetch returns [], the exercise catalog should not be fetched."""
    seed_quests(db)
    hero = HeroProfile(name="Test", level=1, total_xp=0)
    db.add(hero)
    db.commit()

    client = _client(sessions=SESSIONS, logs=[])
    await sync_workouts(db, client, fetch_exercise_logs=True)

    client.get_exercises.assert_not_called()


@pytest.mark.asyncio
async def test_exercise_catalog_fetched_when_logs_present(db):
    """If logs exist, the exercise catalog should be fetched for name resolution."""
    seed_quests(db)
    hero = HeroProfile(name="Test", level=1, total_xp=0)
    db.add(hero)
    db.commit()

    logs = [{"id": 1, "exercise": 5, "reps": 8, "weight": 60, "rir": None, "workout": 1}]
    client = _client(sessions=SESSIONS, logs=logs, exercises=[{"id": 5, "name": "Squat"}])
    await sync_workouts(db, client, fetch_exercise_logs=True)

    client.get_exercises.assert_called_once()


# ── 3. WGER_FETCH_EXERCISE_LOGS=false skips log+catalog fetch entirely ───────

@pytest.mark.asyncio
async def test_fetch_exercise_logs_false_skips_log_and_catalog(db):
    """When fetch_exercise_logs=False, neither log nor catalog endpoint is called."""
    seed_quests(db)
    hero = HeroProfile(name="Test", level=1, total_xp=0)
    db.add(hero)
    db.commit()

    client = _client(sessions=SESSIONS)
    result = await sync_workouts(db, client, fetch_exercise_logs=False)

    client.get_exercise_logs.assert_not_called()
    client.get_exercises.assert_not_called()
    assert result.new_sessions == 1
    assert result.errors == []


@pytest.mark.asyncio
async def test_fetch_exercise_logs_false_still_awards_xp(db):
    """Disabling log fetching must not prevent base XP from being awarded."""
    seed_quests(db)
    hero = HeroProfile(name="Test", level=1, total_xp=0)
    db.add(hero)
    db.commit()

    client = _client(sessions=SESSIONS)
    result = await sync_workouts(db, client, fetch_exercise_logs=False)

    hero = db.query(HeroProfile).first()
    assert hero.total_xp >= 100


# ── 4. SYNC_FROM_DATE passed to client ───────────────────────────────────────

@pytest.mark.asyncio
async def test_sync_from_date_passed_to_client(db):
    """sync_from_date must be forwarded to WgerClient.get_workout_sessions."""
    seed_quests(db)
    hero = HeroProfile(name="Test", level=1, total_xp=0)
    db.add(hero)
    db.commit()

    cutoff = date(2024, 6, 1)
    client = _client(sessions=[])
    await sync_workouts(db, client, sync_from_date=cutoff)

    client.get_workout_sessions.assert_called_once_with(since=cutoff)


@pytest.mark.asyncio
async def test_sync_from_date_none_passes_none(db):
    """When sync_from_date is None the client receives since=None."""
    seed_quests(db)
    hero = HeroProfile(name="Test", level=1, total_xp=0)
    db.add(hero)
    db.commit()

    client = _client(sessions=[])
    await sync_workouts(db, client, sync_from_date=None)

    client.get_workout_sessions.assert_called_once_with(since=None)


def test_sync_from_date_setting_parses_iso_date(monkeypatch, tmp_path):
    """SYNC_FROM_DATE env var parses correctly as a date object."""
    monkeypatch.setenv("WGER_BASE_URL", "https://wger.example.com")
    monkeypatch.setenv("WGER_API_TOKEN", "tok")
    monkeypatch.setenv("SYNC_FROM_DATE", "2024-06-01")

    import app.config as cfg
    cfg._settings = None
    s = cfg.Settings()
    assert s.SYNC_FROM_DATE == date(2024, 6, 1)
    cfg._settings = None  # reset for other tests


def test_wger_fetch_exercise_logs_setting_false(monkeypatch):
    """WGER_FETCH_EXERCISE_LOGS=false parses as bool False."""
    monkeypatch.setenv("WGER_BASE_URL", "https://wger.example.com")
    monkeypatch.setenv("WGER_API_TOKEN", "tok")
    monkeypatch.setenv("WGER_FETCH_EXERCISE_LOGS", "false")

    import app.config as cfg
    cfg._settings = None
    s = cfg.Settings()
    assert s.WGER_FETCH_EXERCISE_LOGS is False
    cfg._settings = None
