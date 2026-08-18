"""The two FitTrackee quest sources, and the training plan built on them.

The plan is one endurance session on Tuesday and one at the weekend. That makes
the weekday decisive, so most of this file is about which day a workout lands on
— in the *local* Hero timezone, never in UTC.

The duration source is the other half: it sums seconds and converts once, so
three sessions just under thirty minutes are not quietly rounded away.
"""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import (
    Base,
    FitTrackeeWorkout,
    Goal,
    HeroProfile,
    Quest,
    QuestCompletion,
)
from app.quests import count_quest_progress, evaluate_quests

# Week of 2026-08-17: Monday the 17th … Sunday the 23rd.
MONDAY = datetime(2026, 8, 17, 10, 0)
TUESDAY = datetime(2026, 8, 18, 10, 0)
WEDNESDAY = datetime(2026, 8, 19, 10, 0)
THURSDAY = datetime(2026, 8, 20, 10, 0)
FRIDAY = datetime(2026, 8, 21, 10, 0)
SATURDAY = datetime(2026, 8, 22, 10, 0)
SUNDAY = datetime(2026, 8, 23, 10, 0)


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add(HeroProfile(name="Hero", level=1, total_xp=0))
    session.commit()
    yield session
    session.close()


@pytest.fixture(autouse=True)
def _fixed_week(monkeypatch):
    """Pin "today" to Wednesday of that week, so the window is deterministic."""
    import app.quests as quests

    monkeypatch.setattr(quests, "app_today", lambda: WEDNESDAY.date())


def add_workout(
    db, when, *, external_id=None, seconds=2400, moving=None, qualifying=True,
    eligible=True,
):
    from app.quests import app_date_of

    external_id = external_id or f"ft-{when.isoformat()}-{seconds}"
    db.add(
        FitTrackeeWorkout(
            external_id=external_id,
            workout_at=when,
            local_date=app_date_of(when),
            sport_id=5,
            sport_label="Running",
            duration_seconds=seconds,
            moving_seconds=moving,
            qualifies_for_endurance=qualifying,
            reward_eligible=eligible,
            source_hash=f"hash-{external_id}",
        )
    )
    db.commit()


def make_quest(db, **kw):
    from app.quests import serialize_allowed_weekdays

    days = kw.pop("allowed_weekdays", None)
    quest = Quest(
        slug=kw.pop("slug", "q"),
        title=kw.pop("title", "Quest"),
        quest_type=kw.pop("quest_type", "fittrackee_workout_count"),
        period=kw.pop("period", "weekly"),
        target_value=kw.pop("target_value", 1),
        repeatable=kw.pop("repeatable", True),
        xp_reward=kw.pop("xp_reward", 75),
        active=True,
        allowed_weekdays=serialize_allowed_weekdays(days) if days else None,
        **kw,
    )
    db.add(quest)
    db.commit()
    return quest


# ---------------------------------------------------------------------------
# fittrackee_workout_count — the plain counter
# ---------------------------------------------------------------------------

def test_it_counts_qualifying_workouts(db):
    quest = make_quest(db, target_value=3)
    for day in (TUESDAY, THURSDAY, SATURDAY):
        add_workout(db, day)
    assert count_quest_progress(db, quest) == 3


def test_a_non_qualifying_workout_does_not_count(db):
    quest = make_quest(db)
    add_workout(db, TUESDAY, qualifying=False)
    assert count_quest_progress(db, quest) == 0


def test_a_baseline_workout_does_not_count(db):
    """History fills no quest bar, just as it pays no XP."""
    quest = make_quest(db)
    add_workout(db, TUESDAY, eligible=False)
    assert count_quest_progress(db, quest) == 0


def test_a_workout_from_another_week_does_not_count(db):
    quest = make_quest(db)
    add_workout(db, TUESDAY - timedelta(days=7))
    assert count_quest_progress(db, quest) == 0


def test_one_external_id_counts_once(db):
    """Rule 11: no artificial multiple counting of a single activity."""
    quest = make_quest(db, target_value=3)
    add_workout(db, TUESDAY, external_id="same")
    assert count_quest_progress(db, quest) == 1
    assert db.query(FitTrackeeWorkout).count() == 1


# ---------------------------------------------------------------------------
# The Tuesday quest
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "day,expected",
    [(MONDAY, 0), (TUESDAY, 1), (WEDNESDAY, 0), (SATURDAY, 0), (SUNDAY, 0)],
)
def test_only_tuesday_counts_for_the_tuesday_quest(db, day, expected):
    quest = make_quest(db, allowed_weekdays=[2])
    add_workout(db, day)
    assert count_quest_progress(db, quest) == expected


def test_two_tuesday_sessions_satisfy_the_quest_once(db):
    quest = make_quest(db, allowed_weekdays=[2], target_value=1)
    add_workout(db, TUESDAY, external_id="a")
    add_workout(db, TUESDAY.replace(hour=18), external_id="b")

    hero = db.query(HeroProfile).one()
    newly = evaluate_quests(db, hero)

    assert newly == ["Quest"]
    assert db.query(QuestCompletion).count() == 1


def test_a_late_monday_run_counts_as_tuesday(db):
    """23:30 UTC on Monday is 01:30 on Tuesday in Europe/Berlin."""
    quest = make_quest(db, allowed_weekdays=[2])
    add_workout(db, datetime(2026, 8, 17, 23, 30))
    assert count_quest_progress(db, quest) == 1


def test_a_late_tuesday_run_counts_as_wednesday(db):
    quest = make_quest(db, allowed_weekdays=[2])
    add_workout(db, datetime(2026, 8, 18, 23, 30))
    assert count_quest_progress(db, quest) == 0


# ---------------------------------------------------------------------------
# The weekend quest
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "day,expected",
    [(FRIDAY, 0), (SATURDAY, 1), (SUNDAY, 1), (MONDAY, 0)],
)
def test_only_the_weekend_counts_for_the_weekend_quest(db, day, expected):
    quest = make_quest(db, allowed_weekdays=[6, 7])
    add_workout(db, day)
    assert count_quest_progress(db, quest) == expected


def test_saturday_and_sunday_satisfy_it_once(db):
    quest = make_quest(db, allowed_weekdays=[6, 7], target_value=1)
    add_workout(db, SATURDAY, external_id="sa")
    add_workout(db, SUNDAY, external_id="so")

    newly = evaluate_quests(db, db.query(HeroProfile).one())

    assert newly == ["Quest"]
    assert db.query(QuestCompletion).count() == 1


def test_one_workout_cannot_satisfy_both_quests(db):
    """Tuesday and the weekend are disjoint, so this is structural."""
    tuesday = make_quest(db, slug="tue", title="Dienstag", allowed_weekdays=[2])
    weekend = make_quest(db, slug="we", title="Wochenende", allowed_weekdays=[6, 7])
    add_workout(db, TUESDAY)

    assert count_quest_progress(db, tuesday) == 1
    assert count_quest_progress(db, weekend) == 0


# ---------------------------------------------------------------------------
# fittrackee_duration_minutes
# ---------------------------------------------------------------------------

def test_it_sums_minutes(db):
    quest = make_quest(db, quest_type="fittrackee_duration_minutes", target_value=90)
    add_workout(db, TUESDAY, external_id="a", seconds=1800)
    add_workout(db, THURSDAY, external_id="b", seconds=1800)
    assert count_quest_progress(db, quest) == 60


def test_seconds_are_summed_before_converting(db):
    """29:50 + 30:10 + 30:00 is 90 minutes exactly. Rounding each one down
    first would give 89 and silently fail the quest."""
    quest = make_quest(db, quest_type="fittrackee_duration_minutes", target_value=90)
    for name, seconds in (("a", 1790), ("b", 1810), ("c", 1800)):
        add_workout(db, TUESDAY, external_id=name, seconds=seconds)

    assert count_quest_progress(db, quest) == 90


def test_the_ninety_minute_boundary_is_exact(db):
    quest = make_quest(db, quest_type="fittrackee_duration_minutes", target_value=90)
    add_workout(db, TUESDAY, external_id="a", seconds=5399)   # 89:59
    assert count_quest_progress(db, quest) == 89

    add_workout(db, TUESDAY, external_id="b", seconds=1)
    assert count_quest_progress(db, quest) == 90


def test_moving_time_is_preferred(db):
    quest = make_quest(db, quest_type="fittrackee_duration_minutes")
    add_workout(db, TUESDAY, seconds=3600, moving=1800)
    assert count_quest_progress(db, quest) == 30


def test_duration_is_used_when_moving_is_missing(db):
    quest = make_quest(db, quest_type="fittrackee_duration_minutes")
    add_workout(db, TUESDAY, seconds=3600, moving=None)
    assert count_quest_progress(db, quest) == 60


def test_non_qualifying_minutes_are_not_summed(db):
    quest = make_quest(db, quest_type="fittrackee_duration_minutes")
    add_workout(db, TUESDAY, seconds=3600, qualifying=False)
    assert count_quest_progress(db, quest) == 0


def test_baseline_minutes_are_not_summed(db):
    quest = make_quest(db, quest_type="fittrackee_duration_minutes")
    add_workout(db, TUESDAY, seconds=3600, eligible=False)
    assert count_quest_progress(db, quest) == 0


# ---------------------------------------------------------------------------
# Periods
# ---------------------------------------------------------------------------

def test_a_daily_quest_sees_only_today(db):
    quest = make_quest(db, period="daily")
    add_workout(db, WEDNESDAY)
    assert count_quest_progress(db, quest) == 1

    other = make_quest(db, slug="q2", period="daily")
    db.query(FitTrackeeWorkout).delete()
    db.commit()
    add_workout(db, TUESDAY)
    assert count_quest_progress(db, other) == 0


def test_a_monthly_quest_spans_the_calendar_month(db):
    quest = make_quest(db, period="monthly", target_value=5)
    add_workout(db, datetime(2026, 8, 3, 9, 0), external_id="early")
    add_workout(db, WEDNESDAY, external_id="mid")
    assert count_quest_progress(db, quest) == 2


def test_a_monthly_quest_ignores_the_previous_month(db):
    quest = make_quest(db, period="monthly")
    add_workout(db, datetime(2026, 7, 31, 9, 0))
    assert count_quest_progress(db, quest) == 0


def test_a_once_quest_counts_everything(db):
    quest = make_quest(db, period="once", target_value=10, repeatable=False)
    add_workout(db, datetime(2025, 1, 1, 9, 0), external_id="ancient")
    add_workout(db, WEDNESDAY, external_id="now")
    assert count_quest_progress(db, quest) == 2


def test_a_once_quest_still_ignores_the_baseline(db):
    quest = make_quest(db, period="once", target_value=10, repeatable=False)
    add_workout(db, datetime(2025, 1, 1, 9, 0), eligible=False)
    assert count_quest_progress(db, quest) == 0


def test_a_new_year_boundary_does_not_leak(db):
    """A December workout must not count towards January."""
    import app.quests as quests

    quests_today = datetime(2027, 1, 6, 12, 0).date()
    quest = make_quest(db, period="monthly")
    add_workout(db, datetime(2026, 12, 30, 9, 0))

    original = quests.app_today
    quests.app_today = lambda: quests_today
    try:
        assert count_quest_progress(db, quest) == 0
    finally:
        quests.app_today = original


# ---------------------------------------------------------------------------
# Rewarding
# ---------------------------------------------------------------------------

def test_a_satisfied_quest_is_rewarded_once(db):
    quest = make_quest(db, allowed_weekdays=[2], target_value=1, xp_reward=75)
    add_workout(db, TUESDAY)
    hero = db.query(HeroProfile).one()

    first = evaluate_quests(db, hero)
    second = evaluate_quests(db, hero)

    assert first == ["Quest"]
    assert second == []
    assert db.query(QuestCompletion).count() == 1


def test_an_unsatisfied_quest_is_not_rewarded(db):
    make_quest(db, allowed_weekdays=[2], target_value=1)
    add_workout(db, THURSDAY)
    assert evaluate_quests(db, db.query(HeroProfile).one()) == []
