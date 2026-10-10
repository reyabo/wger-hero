"""Sync against the shape the current wger version actually returns.

Fixtures here mirror a live API probe of the deployed wger: a WorkoutSession id
is a **string** UUID, a WorkoutLog points back through `session` (not `workout`),
carries `repetitions` rather than `reps`, and returns numbers as strings. All
values are synthetic — no production id is copied.
"""

from datetime import date, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import Base, HeroProfile, HeroStat, StatXpEvent, SyncEvent, XpEvent
from app.sync import _normalize_session, sync_workouts

SESSION_A = "test-session-aaaa-1111"
SESSION_B = "test-session-bbbb-2222"


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _session(session_id=SESSION_A, day="2026-08-07", notes=""):
    """A WorkoutSession exactly as the live API returns it — no `workout` key."""
    return {
        "id": session_id,
        "date": day,
        "day": None,
        "impression": "2",
        "notes": notes,
        "routine": 123,
        "time_start": "10:00:00",
        "time_end": "11:00:00",
    }


def _log(log_id, session_id=SESSION_A, exercise=456, repetitions="8",
         weight="80.0", rir=None):
    """A WorkoutLog as the live API returns it — one set, keyed by session."""
    return {
        "id": log_id,
        "session": session_id,
        "routine": 123,
        "exercise": exercise,
        "repetitions": repetitions,
        "weight": weight,
        "rir": rir,
    }


def _client(sessions=None, logs=None, exercises=None):
    client = MagicMock()
    client.get_workout_sessions = AsyncMock(return_value=sessions or [])
    client.get_exercise_logs = AsyncMock(return_value=logs or [])
    client.get_exercises = AsyncMock(return_value=exercises or [])
    return client


# ---------------------------------------------------------------------------
# Normalization of the live shapes
# ---------------------------------------------------------------------------

def test_a_string_session_id_becomes_the_source_id():
    normalized = _normalize_session(_session(), [], {})
    assert normalized.source_id == f"session-{SESSION_A}"


def test_a_session_without_a_workout_key_still_normalizes():
    """The live API dropped `workout` entirely — that must not be fatal."""
    raw = _session()
    assert "workout" not in raw
    assert _normalize_session(raw, [], {}).date == date(2026, 8, 7)


def test_repetitions_as_a_string_becomes_an_integer():
    normalized = _normalize_session(_session(), [_log("l1", repetitions="12")], {})
    assert normalized.exercises[0].reps == 12


def test_the_old_reps_field_is_still_accepted():
    """Older exports wrote `reps`; it stays a fallback, not the canonical name."""
    legacy = {"id": "l1", "session": SESSION_A, "exercise": 456, "reps": 5}
    assert _normalize_session(_session(), [legacy], {}).exercises[0].reps == 5


def test_repetitions_wins_over_a_stale_reps_field():
    entry = _log("l1", repetitions="9")
    entry["reps"] = 99
    assert _normalize_session(_session(), [entry], {}).exercises[0].reps == 9


def test_weight_as_a_string_becomes_a_float():
    normalized = _normalize_session(_session(), [_log("l1", weight="82.5")], {})
    assert normalized.exercises[0].weight == pytest.approx(82.5)


def test_a_null_rir_stays_none():
    normalized = _normalize_session(_session(), [_log("l1", rir=None)], {})
    assert normalized.exercises[0].rir is None


def test_a_numeric_rir_is_kept():
    normalized = _normalize_session(_session(), [_log("l1", rir="2")], {})
    assert normalized.exercises[0].rir == pytest.approx(2.0)


@pytest.mark.parametrize("bad", ["", "viele", None, "8.5.1"])
def test_an_unreadable_repetition_count_does_not_abort_the_session(bad):
    normalized = _normalize_session(_session(), [_log("l1", repetitions=bad)], {})
    assert len(normalized.exercises) == 1
    assert normalized.exercises[0].reps is None


def test_an_unresolvable_exercise_keeps_a_stable_fallback_name():
    normalized = _normalize_session(_session(), [_log("l1", exercise=789)], {})
    assert normalized.exercises[0].name == "Exercise 789"


def test_a_resolvable_exercise_uses_its_catalogue_name():
    normalized = _normalize_session(_session(), [_log("l1", exercise=789)], {789: "Squat"})
    assert normalized.exercises[0].name == "Squat"


# ---------------------------------------------------------------------------
# Session ↔ log linking
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_logs_are_grouped_by_session_not_by_workout(db):
    client = _client(
        sessions=[_session(SESSION_A), _session(SESSION_B, day="2026-08-08")],
        logs=[
            _log("a1", SESSION_A, exercise=1),
            _log("a2", SESSION_A, exercise=2),
            _log("b1", SESSION_B, exercise=3),
        ],
        exercises=[{"id": 1, "name": "Squat"}, {"id": 2, "name": "Bench"},
                   {"id": 3, "name": "Row"}],
    )
    await sync_workouts(db, client, fetch_exercise_logs=True)

    summaries = {e.source_id: e.raw_summary for e in db.query(SyncEvent).all()}
    assert summaries[f"session-{SESSION_A}"].endswith("2 exercises")
    assert summaries[f"session-{SESSION_B}"].endswith("1 exercise")


@pytest.mark.asyncio
async def test_a_log_without_a_session_is_ignored(db):
    client = _client(
        sessions=[_session(SESSION_A)],
        logs=[_log("a1", SESSION_A), {"id": "orphan", "exercise": 9}],
        exercises=[{"id": 456, "name": "Squat"}],
    )
    await sync_workouts(db, client, fetch_exercise_logs=True)
    assert db.query(SyncEvent).one().raw_summary.endswith("1 exercise")


@pytest.mark.asyncio
async def test_a_log_for_an_unknown_session_is_ignored(db):
    client = _client(
        sessions=[_session(SESSION_A)],
        logs=[_log("a1", SESSION_A), _log("x1", "test-session-unknown")],
        exercises=[{"id": 456, "name": "Squat"}],
    )
    await sync_workouts(db, client, fetch_exercise_logs=True)
    assert db.query(SyncEvent).one().raw_summary.endswith("1 exercise")


@pytest.mark.asyncio
async def test_a_session_without_logs_reports_none(db):
    client = _client(sessions=[_session(SESSION_A)], logs=[])
    await sync_workouts(db, client, fetch_exercise_logs=True)
    assert "0 exercises" in db.query(SyncEvent).one().raw_summary


@pytest.mark.asyncio
async def test_the_same_exercise_in_two_sessions_counts_once_each(db):
    client = _client(
        sessions=[_session(SESSION_A), _session(SESSION_B, day="2026-08-08")],
        logs=[_log("a1", SESSION_A, exercise=1), _log("b1", SESSION_B, exercise=1)],
        exercises=[{"id": 1, "name": "Squat"}],
    )
    await sync_workouts(db, client, fetch_exercise_logs=True)
    for event in db.query(SyncEvent).all():
        assert event.raw_summary.endswith("1 exercise")


# ---------------------------------------------------------------------------
# "X exercises" counts distinct exercises, not sets
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_five_sets_of_two_exercises_read_as_two_exercises(db):
    client = _client(
        sessions=[_session(SESSION_A)],
        logs=[
            _log("s1", exercise=1), _log("s2", exercise=1), _log("s3", exercise=1),
            _log("s4", exercise=2), _log("s5", exercise=2),
        ],
        exercises=[{"id": 1, "name": "Squat"}, {"id": 2, "name": "Bench"}],
    )
    await sync_workouts(db, client, fetch_exercise_logs=True)
    assert db.query(SyncEvent).one().raw_summary.endswith("2 exercises")


def test_every_set_is_still_kept_as_its_own_entry():
    """Counting distinct exercises must not throw away the individual sets."""
    logs = [_log("s1", exercise=1), _log("s2", exercise=1), _log("s3", exercise=2)]
    normalized = _normalize_session(_session(), logs, {1: "Squat", 2: "Bench"})
    assert len(normalized.exercises) == 3
    assert normalized.distinct_exercise_count == 2


# ---------------------------------------------------------------------------
# Strength stat XP
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_new_workout_awards_global_and_strength_xp(db):
    client = _client(sessions=[_session(SESSION_A)])
    await sync_workouts(db, client, fetch_exercise_logs=False)

    hero = db.query(HeroProfile).one()
    assert hero.total_xp == 100
    assert db.query(HeroStat).filter(HeroStat.stat_key == "strength").one().xp == 100

    assert db.query(XpEvent).filter(XpEvent.event_type == "workout_complete").count() == 1
    stat_events = db.query(StatXpEvent).filter(StatXpEvent.source == "wger").all()
    assert len(stat_events) == 1
    assert stat_events[0].stat_key == "strength"
    assert stat_events[0].xp == 100
    assert stat_events[0].source_id == f"session-{SESSION_A}"


@pytest.mark.asyncio
async def test_an_unchanged_second_sync_awards_nothing_twice(db):
    """Invariant 1 and 2: neither global XP nor strength XP may be paid twice."""
    client = _client(sessions=[_session(SESSION_A)])
    await sync_workouts(db, client, fetch_exercise_logs=False)
    await sync_workouts(db, client, fetch_exercise_logs=False)

    assert db.query(HeroProfile).one().total_xp == 100
    assert db.query(HeroStat).filter(HeroStat.stat_key == "strength").one().xp == 100
    assert db.query(XpEvent).count() == 1
    assert db.query(StatXpEvent).count() == 1


@pytest.mark.asyncio
async def test_several_workouts_each_get_their_own_strength_award(db):
    client = _client(sessions=[
        _session(SESSION_A, day="2026-08-05"),
        _session(SESSION_B, day="2026-08-07"),
    ])
    await sync_workouts(db, client, fetch_exercise_logs=False)

    assert db.query(HeroStat).filter(HeroStat.stat_key == "strength").one().xp == 200
    assert db.query(StatXpEvent).filter(StatXpEvent.source == "wger").count() == 2


@pytest.mark.asyncio
async def test_conditioning_and_rir_stay_global_only(db):
    """No stat mapping is invented for the two bonus awards."""
    client = _client(
        sessions=[_session(SESSION_A)],
        logs=[_log("s1", exercise=1, rir="2")],
        exercises=[{"id": 1, "name": "Burpee"}],
    )
    await sync_workouts(db, client, fetch_exercise_logs=True)

    types = {e.event_type for e in db.query(XpEvent).all()}
    assert types == {"workout_complete", "conditioning_bonus", "rir_logged"}
    assert db.query(HeroProfile).one().total_xp == 135

    stat_events = db.query(StatXpEvent).all()
    assert len(stat_events) == 1
    assert stat_events[0].stat_key == "strength"
    assert stat_events[0].xp == 100


# ---------------------------------------------------------------------------
# Re-sync reconciliation (invariant 3)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_changed_session_leaves_no_stale_audit_rows(db):
    # First sync: no logs at all.
    await sync_workouts(db, _client(sessions=[_session(SESSION_A)]),
                        fetch_exercise_logs=False)
    assert db.query(XpEvent).count() == 1
    assert db.query(StatXpEvent).count() == 1

    # Same session, now with logs — a different hash, so it must be re-synced.
    changed = _client(
        sessions=[_session(SESSION_A)],
        logs=[_log("s1", exercise=1, rir="2")],
        exercises=[{"id": 1, "name": "Burpee"}],
    )
    await sync_workouts(db, changed, fetch_exercise_logs=True)

    # Exactly the current award set, nothing left over.
    assert db.query(XpEvent).count() == 3
    assert db.query(StatXpEvent).count() == 1
    assert db.query(SyncEvent).count() == 1
    assert db.query(HeroProfile).one().total_xp == 135
    assert db.query(HeroStat).filter(HeroStat.stat_key == "strength").one().xp == 100


@pytest.mark.asyncio
async def test_re_syncing_the_changed_state_changes_nothing_further(db):
    changed = _client(
        sessions=[_session(SESSION_A)],
        logs=[_log("s1", exercise=1, rir="2")],
        exercises=[{"id": 1, "name": "Burpee"}],
    )
    await sync_workouts(db, _client(sessions=[_session(SESSION_A)]),
                        fetch_exercise_logs=False)
    await sync_workouts(db, changed, fetch_exercise_logs=True)
    before = (
        db.query(HeroProfile).one().total_xp,
        db.query(XpEvent).count(),
        db.query(StatXpEvent).count(),
        db.query(HeroStat).filter(HeroStat.stat_key == "strength").one().xp,
    )

    result = await sync_workouts(db, changed, fetch_exercise_logs=True)

    assert result.new_sessions == 0
    assert result.skipped_sessions == 1
    assert (
        db.query(HeroProfile).one().total_xp,
        db.query(XpEvent).count(),
        db.query(StatXpEvent).count(),
        db.query(HeroStat).filter(HeroStat.stat_key == "strength").one().xp,
    ) == before


@pytest.mark.asyncio
async def test_a_re_sync_never_produces_negative_stat_xp(db):
    await sync_workouts(db, _client(sessions=[_session(SESSION_A)]),
                        fetch_exercise_logs=False)
    # Someone removed the strength total by hand; a re-sync must not go below 0.
    db.query(HeroStat).filter(HeroStat.stat_key == "strength").one().xp = 0
    db.commit()

    await sync_workouts(db, _client(
        sessions=[_session(SESSION_A)],
        logs=[_log("s1", exercise=1)],
        exercises=[{"id": 1, "name": "Squat"}],
    ), fetch_exercise_logs=True)

    assert db.query(HeroStat).filter(HeroStat.stat_key == "strength").one().xp >= 0


# ---------------------------------------------------------------------------
# Hash determinism
# ---------------------------------------------------------------------------

def test_the_hash_does_not_depend_on_log_order():
    logs = [_log("s1", exercise=1), _log("s2", exercise=2), _log("s3", exercise=3)]
    forward = _normalize_session(_session(), logs, {}).raw_hash
    backward = _normalize_session(_session(), list(reversed(logs)), {}).raw_hash
    assert forward == backward


def test_adding_a_set_changes_the_hash():
    base = [_log("s1", exercise=1)]
    more = base + [_log("s2", exercise=1)]
    assert _normalize_session(_session(), base, {}).raw_hash != \
        _normalize_session(_session(), more, {}).raw_hash


def test_changing_a_set_changes_the_hash():
    before = [_log("s1", exercise=1, weight="80.0")]
    after = [_log("s1", exercise=1, weight="85.0")]
    assert _normalize_session(_session(), before, {}).raw_hash != \
        _normalize_session(_session(), after, {}).raw_hash


def test_removing_a_set_changes_the_hash():
    both = [_log("s1", exercise=1), _log("s2", exercise=1)]
    one = [_log("s1", exercise=1)]
    assert _normalize_session(_session(), both, {}).raw_hash != \
        _normalize_session(_session(), one, {}).raw_hash


def test_the_same_state_always_hashes_the_same():
    logs = [_log("s1", exercise=1), _log("s2", exercise=2)]
    assert _normalize_session(_session(), logs, {}).raw_hash == \
        _normalize_session(_session(), logs, {}).raw_hash


# ---------------------------------------------------------------------------
# Exercise logs switched off
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_log_request_is_made_when_the_feature_is_off(db):
    client = _client(sessions=[_session(SESSION_A)])
    await sync_workouts(db, client, fetch_exercise_logs=False)

    client.get_exercise_logs.assert_not_called()
    client.get_exercises.assert_not_called()


@pytest.mark.asyncio
async def test_a_disabled_log_fetch_says_so_rather_than_claiming_zero(db):
    """"0 exercises" would read as "the workout was empty" — it was not asked."""
    client = _client(sessions=[_session(SESSION_A)])
    await sync_workouts(db, client, fetch_exercise_logs=False)

    summary = db.query(SyncEvent).one().raw_summary
    assert "0 exercises" not in summary
    assert "exercise details disabled" in summary


@pytest.mark.asyncio
async def test_strength_xp_is_awarded_even_without_logs(db):
    client = _client(sessions=[_session(SESSION_A)])
    await sync_workouts(db, client, fetch_exercise_logs=False)
    assert db.query(HeroStat).filter(HeroStat.stat_key == "strength").one().xp == 100


# ---------------------------------------------------------------------------
# Nothing else moved
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_workout_quests_still_count_synced_sessions(db):
    from app.models import Quest
    from app.quests import count_quest_progress

    quest = Quest(slug="wq", title="Dreifachschlag", period="weekly",
                  quest_type="workout_count", target_value=3, current_value=0,
                  active=True, repeatable=True)
    db.add(quest)
    db.commit()

    today = date.today().isoformat()
    await sync_workouts(db, _client(sessions=[
        _session(SESSION_A, day=today), _session(SESSION_B, day=today),
    ]), fetch_exercise_logs=False)

    assert count_quest_progress(db, quest) == 2


@pytest.mark.asyncio
async def test_the_hero_level_follows_the_awarded_xp(db):
    from app.xp import recalc_level

    await sync_workouts(db, _client(sessions=[_session(SESSION_A)]),
                        fetch_exercise_logs=False)
    hero = db.query(HeroProfile).one()
    assert hero.level == recalc_level(hero.total_xp)


@pytest.mark.asyncio
async def test_the_global_and_stat_ledgers_agree_after_a_re_sync(db):
    """The two invariants stated together, on the audit rows themselves."""
    await sync_workouts(db, _client(sessions=[_session(SESSION_A)]),
                        fetch_exercise_logs=False)
    await sync_workouts(db, _client(
        sessions=[_session(SESSION_A)],
        logs=[_log("s1", exercise=1, rir="2")],
        exercises=[{"id": 1, "name": "Burpee"}],
    ), fetch_exercise_logs=True)

    wger_xp = sum(e.xp for e in db.query(XpEvent).filter(XpEvent.source == "wger"))
    assert db.query(HeroProfile).one().total_xp == wger_xp

    wger_strength = sum(
        e.xp for e in db.query(StatXpEvent).filter(
            StatXpEvent.source == "wger", StatXpEvent.stat_key == "strength")
    )
    assert db.query(HeroStat).filter(HeroStat.stat_key == "strength").one().xp == wger_strength


@pytest.mark.asyncio
async def test_a_mismatched_stored_total_is_reported_not_guessed(db):
    """SyncEvent.xp_awarded is not trusted over the audit rows."""
    await sync_workouts(db, _client(sessions=[_session(SESSION_A)]),
                        fetch_exercise_logs=False)
    db.query(SyncEvent).one().xp_awarded = 999      # inconsistent by hand
    db.commit()

    result = await sync_workouts(db, _client(
        sessions=[_session(SESSION_A)],
        logs=[_log("s1", exercise=1)],
        exercises=[{"id": 1, "name": "Squat"}],
    ), fetch_exercise_logs=True)

    assert any("does not match" in e for e in result.errors)
    # The audit rows won: 100 taken back, 100 awarded again.
    assert db.query(HeroProfile).one().total_xp == 100


@pytest.mark.asyncio
async def test_a_failing_session_does_not_leave_a_half_written_hero(db):
    """A crash mid-sync must not commit a level without its awards."""
    from unittest.mock import patch

    client = _client(sessions=[_session(SESSION_A)])
    with patch("app.sync.calculate_xp_awards", side_effect=RuntimeError("boom")):
        with pytest.raises(RuntimeError):
            await sync_workouts(db, client, fetch_exercise_logs=False)

    db.rollback()
    assert db.query(XpEvent).count() == 0
    assert db.query(StatXpEvent).count() == 0
    assert db.query(SyncEvent).count() == 0
