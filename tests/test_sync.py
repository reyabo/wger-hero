"""Test sync deduplication and XP awarding."""

import hashlib
import json
from datetime import date, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.achievements import seed_achievements
from app.models import HeroProfile, SyncEvent, XpEvent
from app.quests import seed_quests
from app.sync import NormalizedWorkoutLog, sync_workouts
from app.wger_client import WgerClient


def _make_mock_client(sessions=None, logs=None, exercises=None):
    client = MagicMock(spec=WgerClient)
    client.get_workout_sessions = AsyncMock(return_value=sessions or [])
    client.get_exercise_logs = AsyncMock(return_value=logs or [])
    client.get_exercises = AsyncMock(return_value=exercises or [])
    return client


SESSION_1 = {
    "id": 42,
    "date": "2024-03-01",
    "workout": 10,
    "notes": "Tag 1 – Beine",
    "impression": "3",
}


@pytest.mark.asyncio
async def test_sync_awards_xp(db):
    seed_quests(db)
    seed_achievements(db)
    hero = HeroProfile(name="Test Hero", level=1, total_xp=0)
    db.add(hero)
    db.commit()

    client = _make_mock_client(sessions=[SESSION_1])
    result = await sync_workouts(db, client, hero_name="Test Hero")

    assert result.new_sessions == 1
    assert result.total_xp_awarded >= 100

    hero = db.query(HeroProfile).first()
    assert hero.total_xp >= 100

    xp_events = db.query(XpEvent).all()
    assert any(ev.event_type == "workout_complete" for ev in xp_events)


@pytest.mark.asyncio
async def test_sync_deduplication(db):
    """Syncing the same session twice must not award XP a second time."""
    seed_quests(db)
    seed_achievements(db)
    hero = HeroProfile(name="Test Hero", level=1, total_xp=0)
    db.add(hero)
    db.commit()

    client = _make_mock_client(sessions=[SESSION_1])

    await sync_workouts(db, client, hero_name="Test Hero")
    hero_after_first = db.query(HeroProfile).first()
    xp_after_first = hero_after_first.total_xp

    await sync_workouts(db, client, hero_name="Test Hero")
    hero_after_second = db.query(HeroProfile).first()
    xp_after_second = hero_after_second.total_xp

    assert xp_after_first == xp_after_second, (
        f"XP changed on second sync: {xp_after_first} → {xp_after_second}"
    )

    sync_events = db.query(SyncEvent).all()
    assert len(sync_events) == 1


@pytest.mark.asyncio
async def test_sync_multiple_sessions(db):
    seed_quests(db)
    seed_achievements(db)
    hero = HeroProfile(name="Test Hero", level=1, total_xp=0)
    db.add(hero)
    db.commit()

    sessions = [
        {"id": 1, "date": "2024-03-01", "workout": 1, "notes": "Session A"},
        {"id": 2, "date": "2024-03-03", "workout": 2, "notes": "Session B"},
        {"id": 3, "date": "2024-03-05", "workout": 3, "notes": "Session C"},
    ]
    client = _make_mock_client(sessions=sessions)
    result = await sync_workouts(db, client, hero_name="Test Hero")

    assert result.new_sessions == 3
    assert result.total_xp_awarded >= 300


@pytest.mark.asyncio
async def test_sync_empty_response(db):
    seed_quests(db)
    hero = HeroProfile(name="Test Hero", level=1, total_xp=0)
    db.add(hero)
    db.commit()

    client = _make_mock_client(sessions=[])
    result = await sync_workouts(db, client)

    assert result.new_sessions == 0
    assert result.total_xp_awarded == 0


@pytest.mark.asyncio
async def test_sync_error_recorded(db):
    from app.wger_client import WgerClientError

    hero = HeroProfile(name="Test Hero", level=1, total_xp=0)
    db.add(hero)
    db.commit()

    client = MagicMock(spec=WgerClient)
    client.get_exercises = AsyncMock(return_value=[])
    client.get_workout_sessions = AsyncMock(side_effect=WgerClientError("Connection refused"))

    result = await sync_workouts(db, client)
    assert len(result.errors) > 0
    assert result.new_sessions == 0
