"""The session date field: current wger reports datetime_start, older ones date.

The regression these cover: after a wger update /api/v2/workoutsession/ stopped
returning `date` and reports `datetime_start` / `datetime_end` instead. The
local SYNC_FROM_DATE filter asked for `date`, found nothing, and therefore
discarded every session — leaving a sync that reported no error, no last_error
and no imported workout.

All synthetic data. There is no reachable wger instance in this environment, so
the new response shape is reproduced from the field names only.
"""

from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models import HeroProfile, SyncEvent, XpEvent
from app.quests import seed_quests
from app.sync import _normalize_session, session_date_str, sync_workouts
from app.wger_client import WgerClient, WgerClientError


def _client(sessions=None, logs=None, exercises=None):
    c = MagicMock(spec=WgerClient)
    c.get_workout_sessions = AsyncMock(return_value=sessions or [])
    c.get_exercise_logs = AsyncMock(return_value=logs or [])
    c.get_exercises = AsyncMock(return_value=exercises or [])
    return c


def _hero(db):
    hero = HeroProfile(name="Test", level=1, total_xp=0)
    db.add(hero)
    db.commit()
    return hero


# A session as current wger versions report it: no `date`, but datetime_start
# and datetime_end.
NEW_SHAPE = {
    "id": 501,
    "datetime_start": "2026-09-25T18:30:00+02:00",
    "datetime_end": "2026-09-25T19:45:00+02:00",
    "workout": 10,
    "notes": "synthetic",
}

# A session as older wger versions reported it.
OLD_SHAPE = {
    "id": 502,
    "date": "2026-09-26",
    "workout": 10,
    "notes": "synthetic",
}


# ── The single date rule (pure, no database) ─────────────────────────────────

class TestSessionDateStr:
    def test_reads_the_day_from_datetime_start(self):
        assert session_date_str(NEW_SHAPE) == "2026-09-25"

    def test_still_reads_a_plain_date(self):
        assert session_date_str(OLD_SHAPE) == "2026-09-26"

    def test_datetime_start_wins_over_date(self):
        session = {"datetime_start": "2026-09-25T18:30:00Z", "date": "2026-01-01"}
        assert session_date_str(session) == "2026-09-25"

    def test_falls_back_to_date_when_datetime_start_is_empty(self):
        session = {"datetime_start": "", "date": "2026-09-26"}
        assert session_date_str(session) == "2026-09-26"

    def test_falls_back_to_date_when_datetime_start_is_null(self):
        session = {"datetime_start": None, "date": "2026-09-26"}
        assert session_date_str(session) == "2026-09-26"

    def test_falls_back_to_date_when_datetime_start_is_unparsable(self):
        session = {"datetime_start": "not-a-datetime", "date": "2026-09-26"}
        assert session_date_str(session) == "2026-09-26"

    def test_accepts_a_date_only_datetime_start(self):
        assert session_date_str({"datetime_start": "2026-09-25"}) == "2026-09-25"

    def test_returns_none_when_no_field_carries_a_day(self):
        assert session_date_str({"id": 1, "workout": 2}) is None

    def test_returns_none_rather_than_guessing_for_garbage(self):
        session = {"datetime_start": "not-a-datetime", "date": "also-not-a-date"}
        assert session_date_str(session) is None

    def test_empty_session_is_none(self):
        assert session_date_str({}) is None


# ── Normalization ───────────────────────────────────────────────────────────

class TestNormalizationUsesTheSameRule:
    def test_new_shape_keeps_the_reported_day(self):
        result = _normalize_session(NEW_SHAPE, [], {})
        assert result.date == date(2026, 9, 25)

    def test_new_shape_is_not_stamped_with_today(self):
        """The regression: a dateless-looking session must not become 'today'."""
        result = _normalize_session(NEW_SHAPE, [], {})
        assert result.date != date.today()

    def test_old_shape_unchanged(self):
        result = _normalize_session(OLD_SHAPE, [], {})
        assert result.date == date(2026, 9, 26)

    def test_without_any_day_the_existing_fallback_still_applies(self):
        """Unchanged behaviour for a genuinely dateless session: no crash."""
        result = _normalize_session({"id": 99}, [], {})
        assert result.date == date.today()


# ── The reported failure: the local SYNC_FROM_DATE filter ───────────────────

@pytest.mark.asyncio
async def test_new_shape_is_imported_with_a_cutoff_set(db):
    """The actual bug: with SYNC_FROM_DATE set, nothing was imported at all."""
    seed_quests(db)
    _hero(db)

    client = _client(sessions=[NEW_SHAPE])
    result = await sync_workouts(db, client, sync_from_date=date(2026, 7, 18))

    assert result.new_sessions == 1
    assert result.errors == []
    assert db.query(SyncEvent).count() == 1


@pytest.mark.asyncio
async def test_new_shape_before_the_cutoff_is_still_filtered(db):
    seed_quests(db)
    _hero(db)

    early = dict(NEW_SHAPE, id=600, datetime_start="2026-04-02T07:00:00+02:00")
    client = _client(sessions=[early])
    result = await sync_workouts(db, client, sync_from_date=date(2026, 7, 18))

    assert result.new_sessions == 0
    assert db.query(SyncEvent).count() == 0


@pytest.mark.asyncio
async def test_both_shapes_are_filtered_by_the_same_cutoff(db):
    seed_quests(db)
    _hero(db)

    sessions = [
        dict(NEW_SHAPE, id=1, datetime_start="2026-09-25T18:30:00+02:00"),
        dict(OLD_SHAPE, id=2, date="2026-09-26"),
        dict(NEW_SHAPE, id=3, datetime_start="2026-04-02T07:00:00+02:00"),
        dict(OLD_SHAPE, id=4, date="2026-04-03"),
    ]
    client = _client(sessions=sessions)
    result = await sync_workouts(db, client, sync_from_date=date(2026, 7, 18))

    assert result.new_sessions == 2


@pytest.mark.asyncio
async def test_the_cutoff_day_itself_is_included(db):
    seed_quests(db)
    _hero(db)

    session = dict(NEW_SHAPE, datetime_start="2026-07-18T06:00:00+02:00")
    client = _client(sessions=[session])
    result = await sync_workouts(db, client, sync_from_date=date(2026, 7, 18))

    assert result.new_sessions == 1


@pytest.mark.asyncio
async def test_a_dateless_session_is_reported_instead_of_dropped_silently(db):
    """Why this went unnoticed for weeks: the filter discarded without a trace."""
    seed_quests(db)
    _hero(db)

    client = _client(sessions=[{"id": 700, "workout": 10}])
    result = await sync_workouts(db, client, sync_from_date=date(2026, 7, 18))

    assert result.new_sessions == 0
    assert result.errors, "a session dropped for lack of a date must be visible"
    assert "datetime_start" in result.errors[0]
    assert db.query(XpEvent).count() == 0


@pytest.mark.asyncio
async def test_a_dateless_session_does_not_hide_the_importable_ones(db):
    seed_quests(db)
    _hero(db)

    client = _client(sessions=[{"id": 700}, NEW_SHAPE])
    result = await sync_workouts(db, client, sync_from_date=date(2026, 7, 18))

    assert result.new_sessions == 1
    assert len(result.errors) == 1


@pytest.mark.asyncio
async def test_new_shape_dedupes_on_a_second_sync(db):
    """An unchanged session must not be re-awarded under the new shape either."""
    seed_quests(db)
    _hero(db)

    client = _client(sessions=[NEW_SHAPE])
    await sync_workouts(db, client, sync_from_date=date(2026, 7, 18))
    first = db.query(HeroProfile).first().total_xp

    result = await sync_workouts(db, client, sync_from_date=date(2026, 7, 18))

    assert result.skipped_sessions == 1
    assert db.query(HeroProfile).first().total_xp == first
    assert db.query(SyncEvent).count() == 1


@pytest.mark.asyncio
async def test_xp_event_is_dated_from_datetime_start(db):
    """Quest counting reads XpEvent.created_at — it must be the session's day."""
    seed_quests(db)
    _hero(db)

    client = _client(sessions=[NEW_SHAPE])
    await sync_workouts(db, client, sync_from_date=date(2026, 7, 18))

    event = db.query(XpEvent).filter(XpEvent.event_type == "workout_complete").first()
    assert event is not None
    assert event.created_at.date() == date(2026, 9, 25)


# ── Client: ordering and filter parameter ───────────────────────────────────

def _real_client(capture, fail_status=None):
    """A WgerClient whose transport is replaced by a recording stub."""
    client = WgerClient.__new__(WgerClient)
    client._base_url = "https://wger.example.com"
    client._headers = {}

    async def fake_get_all(path, params=None):
        capture.append((path, dict(params or {})))
        if fail_status is not None and len(capture) == 1:
            raise WgerClientError(f"HTTP {fail_status} from {path}", status_code=fail_status)
        return [NEW_SHAPE]

    client._get_all = fake_get_all
    return client


@pytest.mark.asyncio
async def test_sessions_are_ordered_by_datetime_start():
    capture: list = []
    await _real_client(capture).get_workout_sessions()

    assert capture[0][1]["ordering"] == "-datetime_start"


@pytest.mark.asyncio
async def test_since_is_sent_as_a_datetime_start_filter():
    capture: list = []
    await _real_client(capture).get_workout_sessions(since=date(2026, 7, 18))

    assert capture[0][1]["datetime_start__gte"] == "2026-07-18"
    assert "date__gte" not in capture[0][1]


@pytest.mark.asyncio
async def test_a_rejected_filter_falls_back_to_an_unfiltered_fetch():
    """Which filter params this wger version accepts is unverified.

    A 400 must therefore not break the sync: the request is retried without
    ordering and filter, and the local cutoff filter does the rest.
    """
    capture: list = []
    client = _real_client(capture, fail_status=400)

    sessions = await client.get_workout_sessions(since=date(2026, 7, 18))

    assert sessions == [NEW_SHAPE]
    assert len(capture) == 2
    assert "datetime_start__gte" not in capture[1][1]
    assert "ordering" not in capture[1][1]


@pytest.mark.asyncio
async def test_an_unauthorized_response_is_not_retried():
    """Only a rejected parameter is worth retrying — 401 must surface."""
    capture: list = []
    client = _real_client(capture, fail_status=401)

    with pytest.raises(WgerClientError):
        await client.get_workout_sessions(since=date(2026, 7, 18))

    assert len(capture) == 1


@pytest.mark.asyncio
async def test_client_error_carries_the_status_code():
    err = WgerClientError("HTTP 400 from /x", status_code=400)
    assert err.status_code == 400
    # The message format is unchanged, so existing "404 in str(e)" checks hold.
    assert "400" in str(err)


@pytest.mark.asyncio
async def test_client_error_without_a_status_code_still_works():
    err = WgerClientError("Request failed for /x")
    assert err.status_code is None


# ── Ordering is no longer relied upon ───────────────────────────────────────

@pytest.mark.asyncio
async def test_ascending_order_is_handled(db):
    """wger now ignores the ordering hint and returns oldest first.

    Nothing in the sync may depend on the order it receives.
    """
    seed_quests(db)
    _hero(db)

    oldest = date(2026, 8, 1)
    sessions = [
        dict(NEW_SHAPE, id=i, datetime_start=f"{oldest + timedelta(days=i * 7)}T18:00:00+02:00")
        for i in range(4)
    ]
    client = _client(sessions=sessions)
    result = await sync_workouts(db, client, sync_from_date=date(2026, 7, 18))

    assert result.new_sessions == 4
    assert db.query(SyncEvent).count() == 4
