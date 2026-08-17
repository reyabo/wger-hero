"""A habit may only be completed as often as its own period allows.

The rule is not new: `Habit.target_count` has always been documented as "how
many completions make up a full period", and /today has always shown a habit as
"erledigt" once that many were recorded. Until now nothing enforced it, so the
same habit could be completed over and over for XP. These tests pin the rule
down at the one place that awards XP.
"""

from datetime import date, datetime, time, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.habits import (
    DOUBLE_CLICK_WINDOW_SECONDS,
    completion_period_bounds,
    completions_in_period,
    complete_habit,
    create_habit,
    remaining_completions,
)
from app.models import Base, HabitCompletion, HeroProfile, StatXpEvent, XpEvent

# A Wednesday, so week and month boundaries are both a few days away.
WEDNESDAY = datetime(2026, 8, 5, 12, 0)


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


def _habit(db, recurrence="daily", target_count=1, xp=20, **kw):
    return create_habit(
        db, title=kw.pop("title", "Gewohnheit"), recurrence=recurrence,
        target_count=target_count, base_xp_reward=xp, **kw
    )


def _apart(n: int) -> datetime:
    """`n` completions far enough apart that the double-click guard is idle."""
    return WEDNESDAY + timedelta(seconds=n * (DOUBLE_CLICK_WINDOW_SECONDS + 60))


# ---------------------------------------------------------------------------
# The period a completion falls into
# ---------------------------------------------------------------------------

def test_a_daily_habit_uses_the_calendar_day():
    start, end = completion_period_bounds("daily", date(2026, 8, 5))
    assert start.date() == date(2026, 8, 5)
    assert end.date() == date(2026, 8, 5)


def test_a_weekly_habit_uses_monday_to_sunday():
    start, end = completion_period_bounds("weekly", date(2026, 8, 5))
    assert start.date() == date(2026, 8, 3)     # Monday
    assert end.date() == date(2026, 8, 9)       # Sunday


def test_a_monthly_habit_uses_the_calendar_month():
    start, end = completion_period_bounds("monthly", date(2026, 8, 5))
    assert start.date() == date(2026, 8, 1)
    assert end.date() == date(2026, 8, 31)


def test_a_monthly_period_ends_correctly_in_february():
    _, end = completion_period_bounds("monthly", date(2026, 2, 10))
    assert end.date() == date(2026, 2, 28)


def test_a_monthly_period_ends_correctly_in_december():
    start, end = completion_period_bounds("monthly", date(2026, 12, 10))
    assert start.date() == date(2026, 12, 1)
    assert end.date() == date(2026, 12, 31)


def test_a_flexible_habit_is_capped_per_day():
    """Flexible means "no fixed weekday", not "unlimited"."""
    start, end = completion_period_bounds("flexible", date(2026, 8, 5))
    assert start.date() == end.date() == date(2026, 8, 5)


def test_an_unknown_recurrence_falls_back_to_the_day():
    start, end = completion_period_bounds("irgendwas", date(2026, 8, 5))
    assert start.date() == end.date() == date(2026, 8, 5)


# ---------------------------------------------------------------------------
# The cap itself
# ---------------------------------------------------------------------------

def test_a_daily_habit_can_be_completed_once(db):
    habit = _habit(db)
    result = complete_habit(db, habit, when=_apart(0))
    assert result.ok
    assert db.query(HabitCompletion).count() == 1


def test_a_second_completion_on_the_same_day_is_refused(db):
    habit = _habit(db)
    complete_habit(db, habit, when=_apart(0))
    result = complete_habit(db, habit, when=_apart(1))

    assert not result.ok
    assert result.reason == "period_complete"
    assert result.xp_awarded == 0


def test_the_refused_completion_awards_no_xp(db):
    habit = _habit(db, xp=20)
    complete_habit(db, habit, when=_apart(0))
    before = db.query(HeroProfile).one().total_xp

    complete_habit(db, habit, when=_apart(1))

    assert db.query(HeroProfile).one().total_xp == before
    assert db.query(XpEvent).count() == 1
    assert db.query(HabitCompletion).count() == 1


def test_ten_attempts_still_award_exactly_one_reward(db):
    """The whole point: repeating the action cannot farm XP."""
    habit = _habit(db, xp=20)
    for n in range(10):
        complete_habit(db, habit, when=_apart(n))

    assert db.query(HabitCompletion).count() == 1
    assert db.query(XpEvent).count() == 1
    assert db.query(HeroProfile).one().total_xp == 20


def test_a_target_count_above_one_allows_that_many(db):
    habit = _habit(db, target_count=3, xp=20)
    for n in range(3):
        assert complete_habit(db, habit, when=_apart(n)).ok

    assert db.query(HabitCompletion).count() == 3
    assert db.query(HeroProfile).one().total_xp == 60

    fourth = complete_habit(db, habit, when=_apart(3))
    assert not fourth.ok
    assert db.query(HabitCompletion).count() == 3


def test_the_next_day_is_a_new_period(db):
    habit = _habit(db)
    complete_habit(db, habit, when=WEDNESDAY)
    result = complete_habit(db, habit, when=WEDNESDAY + timedelta(days=1))

    assert result.ok
    assert db.query(HabitCompletion).count() == 2
    assert db.query(HeroProfile).one().total_xp == 40


def test_a_weekly_habit_is_not_reset_by_the_next_day(db):
    habit = _habit(db, recurrence="weekly")
    assert complete_habit(db, habit, when=WEDNESDAY).ok
    assert not complete_habit(db, habit, when=WEDNESDAY + timedelta(days=1)).ok


def test_a_weekly_habit_resets_on_monday(db):
    habit = _habit(db, recurrence="weekly")
    complete_habit(db, habit, when=WEDNESDAY)              # Wed 2026-08-05
    next_monday = datetime(2026, 8, 10, 9, 0)
    assert complete_habit(db, habit, when=next_monday).ok


def test_a_monthly_habit_resets_on_the_first(db):
    habit = _habit(db, recurrence="monthly")
    complete_habit(db, habit, when=WEDNESDAY)
    assert not complete_habit(db, habit, when=datetime(2026, 8, 31, 23, 0)).ok
    assert complete_habit(db, habit, when=datetime(2026, 9, 1, 0, 30)).ok


def test_a_flexible_habit_is_capped_per_day_too(db):
    habit = _habit(db, recurrence="flexible")
    assert complete_habit(db, habit, when=_apart(0)).ok
    assert not complete_habit(db, habit, when=_apart(1)).ok
    assert complete_habit(db, habit, when=WEDNESDAY + timedelta(days=1)).ok


def test_two_habits_do_not_share_a_limit(db):
    a = _habit(db, title="A")
    b = _habit(db, title="B")
    assert complete_habit(db, a, when=_apart(0)).ok
    assert complete_habit(db, b, when=_apart(1)).ok
    assert db.query(HabitCompletion).count() == 2


def test_the_double_click_guard_still_applies(db):
    """The instant repeat keeps its own reason — it is a different problem."""
    habit = _habit(db, target_count=5)
    complete_habit(db, habit, when=WEDNESDAY)
    result = complete_habit(db, habit, when=WEDNESDAY + timedelta(seconds=1))
    assert not result.ok
    assert result.reason == "duplicate"


def test_an_inactive_habit_is_still_refused_first(db):
    habit = _habit(db)
    habit.active = False
    db.commit()
    assert complete_habit(db, habit, when=_apart(0)).reason == "inactive"


def test_no_stat_xp_leaks_through_a_refused_completion(db):
    habit = _habit(db, xp=20, stat_rewards={"strength": 5})
    complete_habit(db, habit, when=_apart(0))
    before = db.query(StatXpEvent).count()

    complete_habit(db, habit, when=_apart(1))

    assert db.query(StatXpEvent).count() == before


# ---------------------------------------------------------------------------
# Reporting the remaining allowance
# ---------------------------------------------------------------------------

def test_completions_in_period_counts_only_this_period(db):
    habit = _habit(db, recurrence="daily", target_count=3)
    complete_habit(db, habit, when=_apart(0))
    complete_habit(db, habit, when=_apart(1))
    complete_habit(db, habit, when=WEDNESDAY + timedelta(days=1))

    assert completions_in_period(db, habit, WEDNESDAY.date()) == 2


def test_remaining_completions_counts_down(db):
    habit = _habit(db, target_count=3)
    assert remaining_completions(db, habit, WEDNESDAY.date()) == 3
    complete_habit(db, habit, when=_apart(0))
    assert remaining_completions(db, habit, WEDNESDAY.date()) == 2


def test_remaining_completions_never_goes_below_zero(db):
    habit = _habit(db, target_count=1)
    complete_habit(db, habit, when=_apart(0))
    # A completion recorded before the rule existed, on the same day.
    db.add(HabitCompletion(habit_id=habit.id, completed_at=_apart(1), xp_awarded=20))
    db.commit()
    assert remaining_completions(db, habit, WEDNESDAY.date()) == 0


def test_existing_history_is_never_deleted_by_the_rule(db):
    """Old over-completions stay in the ledger — the rule only applies going
    forward."""
    habit = _habit(db, target_count=1)
    for n in range(3):
        db.add(HabitCompletion(habit_id=habit.id, completed_at=_apart(n), xp_awarded=20))
    db.commit()

    complete_habit(db, habit, when=_apart(5))

    assert db.query(HabitCompletion).count() == 3
