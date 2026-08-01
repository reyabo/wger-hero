"""Tests for mapping stored history onto week outcomes."""

from datetime import date, datetime, time, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.goals import (
    STATUS_ACTIVE,
    STATUS_ARCHIVED,
    STATUS_COMPLETED,
    STATUS_PAUSED,
    create_goal,
    open_pause_interval,
    pause_intervals_of,
    set_status,
)
from app.goal_progress import (
    goal_momentum,
    goal_streak,
    goal_week_summary,
    week_outcome,
    weekly_quests_of,
)
from app.models import Base, GoalPauseInterval, Quest, QuestCompletion
from app.momentum import week_start
from app.quests import app_today


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


def _goal(db, **kw):
    goal = create_goal(db, title=kw.pop("title", "Ziel"), **kw)
    # created far enough back that history weeks count as "has data"
    goal.created_at = datetime.utcnow() - timedelta(weeks=20)
    db.commit()
    return goal


def _weekly_quest(db, goal, slug="q", target=3):
    quest = Quest(slug=slug, title=slug, goal_id=goal.id, period="weekly",
                  quest_type="workout_count", target_value=target, repeatable=True)
    db.add(quest)
    db.commit()
    return quest


def _interval(db, goal, start_day, end_day=None):
    """Record a break by calendar days, as if it had happened back then."""
    interval = GoalPauseInterval(
        goal_id=goal.id,
        started_at=datetime.combine(start_day, time(12, 0)),
        ended_at=None if end_day is None else datetime.combine(end_day, time(12, 0)),
    )
    db.add(interval)
    db.commit()
    return interval


def _backdate_pause(db, goal, *, weeks: int, ended_weeks: int = None):
    """Move the open interval of a goal back in time, optionally closing it."""
    interval = open_pause_interval(db, goal)
    interval.started_at = datetime.utcnow() - timedelta(weeks=weeks)
    if ended_weeks is not None:
        interval.ended_at = datetime.utcnow() - timedelta(weeks=ended_weeks)
    db.commit()
    return interval


def _rewarded(db, quest, monday, key_suffix=""):
    db.add(QuestCompletion(
        quest_id=quest.id,
        completed_at=datetime.combine(monday + timedelta(days=2), datetime.min.time()),
        dedup_key=f"quest:{quest.id}:weekly:{monday.isoformat()}{key_suffix}",
        xp_awarded=100,
    ))
    db.commit()


def test_goal_without_weekly_quests_has_no_data(db):
    goal = _goal(db)
    outcome = week_outcome(db, goal, week_start(app_today()))
    assert not outcome.has_data
    assert not outcome.scorable


def test_weekly_quests_are_found(db):
    goal = _goal(db)
    _weekly_quest(db, goal, "a")
    db.add(Quest(slug="milestone", title="M", goal_id=goal.id, is_milestone=True,
                 period="once"))
    db.add(Quest(slug="other", title="O", period="weekly"))   # no goal
    db.commit()
    assert [q.slug for q in weekly_quests_of(db, goal)] == ["a"]


def test_satisfied_week_when_every_quest_was_rewarded(db):
    goal = _goal(db)
    q1 = _weekly_quest(db, goal, "a")
    q2 = _weekly_quest(db, goal, "b")
    monday = week_start(app_today()) - timedelta(weeks=1)
    _rewarded(db, q1, monday)
    _rewarded(db, q2, monday)

    outcome = week_outcome(db, goal, monday)
    assert outcome.target == 2
    assert outcome.achieved == 2
    assert outcome.satisfied


def test_partially_rewarded_week_is_not_satisfied(db):
    goal = _goal(db)
    q1 = _weekly_quest(db, goal, "a")
    _weekly_quest(db, goal, "b")
    monday = week_start(app_today()) - timedelta(weeks=1)
    _rewarded(db, q1, monday)

    outcome = week_outcome(db, goal, monday)
    assert outcome.achieved == 1 and outcome.target == 2
    assert not outcome.satisfied
    assert outcome.fulfilment == 50


def test_weeks_before_the_goal_existed_have_no_data(db):
    goal = _goal(db)
    _weekly_quest(db, goal, "a")
    goal.created_at = datetime.utcnow()
    db.commit()

    old = week_start(app_today()) - timedelta(weeks=5)
    assert not week_outcome(db, goal, old).has_data


def test_pausing_marks_the_running_week_only(db):
    """A pause starting today neutralises this week, not the ones before it."""
    goal = _goal(db)
    _weekly_quest(db, goal, "a")
    set_status(db, goal, STATUS_PAUSED)

    this_week = week_outcome(db, goal, week_start(app_today()))
    assert this_week.paused
    assert not this_week.scorable
    assert not week_outcome(db, goal, week_start(app_today()) - timedelta(weeks=1)).paused


def test_momentum_of_a_perfect_month(db):
    goal = _goal(db)
    quest = _weekly_quest(db, goal, "a")
    for n in range(1, 5):
        _rewarded(db, quest, week_start(app_today()) - timedelta(weeks=n))
    assert goal_momentum(db, goal).value == 100


def test_momentum_survives_one_missed_week(db):
    goal = _goal(db)
    quest = _weekly_quest(db, goal, "a")
    for n in (2, 3, 4):
        _rewarded(db, quest, week_start(app_today()) - timedelta(weeks=n))
    result = goal_momentum(db, goal)
    assert result.value == 60          # last week missing = 40 % lost
    assert result.value > 0


def test_momentum_is_none_without_history(db):
    goal = _goal(db)
    _weekly_quest(db, goal, "a")
    goal.created_at = datetime.utcnow()
    db.commit()
    assert goal_momentum(db, goal).value is None


def test_momentum_over_a_fully_paused_month_is_none_not_zero(db):
    goal = _goal(db)
    _weekly_quest(db, goal, "a")
    set_status(db, goal, STATUS_PAUSED)
    _backdate_pause(db, goal, weeks=6)
    assert goal_momentum(db, goal).value is None


def test_streak_counts_completed_weeks(db):
    goal = _goal(db)
    quest = _weekly_quest(db, goal, "a")
    for n in (1, 2, 3):
        _rewarded(db, quest, week_start(app_today()) - timedelta(weeks=n))
    assert goal_streak(db, goal).current == 3


def test_unfinished_current_week_does_not_break_the_streak(db):
    goal = _goal(db)
    quest = _weekly_quest(db, goal, "a")
    for n in (1, 2):
        _rewarded(db, quest, week_start(app_today()) - timedelta(weeks=n))
    # nothing rewarded this week yet
    assert goal_streak(db, goal).current == 2


def test_current_week_extends_the_streak_once_satisfied(db):
    goal = _goal(db)
    quest = _weekly_quest(db, goal, "a")
    for n in (1, 2):
        _rewarded(db, quest, week_start(app_today()) - timedelta(weeks=n))
    _rewarded(db, quest, week_start(app_today()))
    assert goal_streak(db, goal).current == 3


def test_best_streak_is_kept_after_a_break(db):
    goal = _goal(db)
    quest = _weekly_quest(db, goal, "a")
    for n in (2, 3, 4):
        _rewarded(db, quest, week_start(app_today()) - timedelta(weeks=n))
    result = goal_streak(db, goal)
    assert result.current == 0
    assert result.best == 3


def test_summary_has_everything_a_card_needs(db):
    goal = _goal(db)
    quest = _weekly_quest(db, goal, "a")
    _rewarded(db, quest, week_start(app_today()) - timedelta(weeks=1))

    summary = goal_week_summary(db, goal)
    assert set(summary) == {"this_week", "momentum", "streak", "scored", "weekly_quests"}
    assert summary["scored"] is True
    assert summary["weekly_quests"] == 1


def test_paused_goal_is_not_scored_in_the_summary(db):
    goal = _goal(db)
    _weekly_quest(db, goal, "a")
    set_status(db, goal, STATUS_PAUSED)
    assert goal_week_summary(db, goal)["scored"] is False


# ---------------------------------------------------------------------------
# Pause intervals — bookkeeping
# ---------------------------------------------------------------------------

def test_pausing_opens_exactly_one_interval(db):
    goal = _goal(db)
    set_status(db, goal, STATUS_PAUSED)
    intervals = pause_intervals_of(db, goal)
    assert len(intervals) == 1
    assert intervals[0].ended_at is None


def test_pausing_twice_records_one_break(db):
    goal = _goal(db)
    set_status(db, goal, STATUS_PAUSED)
    set_status(db, goal, STATUS_PAUSED)
    assert len(pause_intervals_of(db, goal)) == 1


def test_resuming_closes_the_open_interval(db):
    goal = _goal(db)
    set_status(db, goal, STATUS_PAUSED)
    set_status(db, goal, STATUS_ACTIVE)
    intervals = pause_intervals_of(db, goal)
    assert len(intervals) == 1
    assert intervals[0].ended_at is not None
    assert open_pause_interval(db, goal) is None


def test_resuming_twice_is_idempotent(db):
    goal = _goal(db)
    set_status(db, goal, STATUS_PAUSED)
    set_status(db, goal, STATUS_ACTIVE)
    first_end = pause_intervals_of(db, goal)[0].ended_at
    set_status(db, goal, STATUS_ACTIVE)
    assert len(pause_intervals_of(db, goal)) == 1
    assert pause_intervals_of(db, goal)[0].ended_at == first_end


def test_several_separate_breaks_are_recorded(db):
    goal = _goal(db)
    for _ in range(3):
        set_status(db, goal, STATUS_PAUSED)
        set_status(db, goal, STATUS_ACTIVE)
    intervals = pause_intervals_of(db, goal)
    assert len(intervals) == 3
    assert all(iv.ended_at is not None for iv in intervals)


def test_at_most_one_open_interval_per_goal(db):
    goal = _goal(db)
    for _ in range(2):
        set_status(db, goal, STATUS_PAUSED)
        set_status(db, goal, STATUS_ACTIVE)
    set_status(db, goal, STATUS_PAUSED)
    open_now = [iv for iv in pause_intervals_of(db, goal) if iv.ended_at is None]
    assert len(open_now) == 1


def test_completing_a_paused_goal_closes_the_break(db):
    """No status other than `paused` may leave an interval running."""
    goal = _goal(db)
    set_status(db, goal, STATUS_PAUSED)
    set_status(db, goal, STATUS_COMPLETED)
    assert open_pause_interval(db, goal) is None


def test_archiving_a_paused_goal_closes_the_break(db):
    goal = _goal(db)
    set_status(db, goal, STATUS_PAUSED)
    set_status(db, goal, STATUS_ARCHIVED)
    assert open_pause_interval(db, goal) is None


def test_intervals_of_two_goals_do_not_interfere(db):
    a, b = _goal(db, title="A"), _goal(db, title="B")
    set_status(db, a, STATUS_PAUSED)
    set_status(db, b, STATUS_PAUSED)
    set_status(db, a, STATUS_ACTIVE)
    assert open_pause_interval(db, a) is None
    assert open_pause_interval(db, b) is not None


# ---------------------------------------------------------------------------
# Pause intervals — which weeks they neutralise
# ---------------------------------------------------------------------------

def test_a_pause_starting_midweek_neutralises_the_whole_week(db):
    goal = _goal(db)
    _weekly_quest(db, goal, "a")
    monday = week_start(app_today()) - timedelta(weeks=2)
    _interval(db, goal, monday + timedelta(days=3), monday + timedelta(days=5))
    assert week_outcome(db, goal, monday).paused


def test_a_pause_ending_midweek_neutralises_the_whole_week(db):
    goal = _goal(db)
    _weekly_quest(db, goal, "a")
    monday = week_start(app_today()) - timedelta(weeks=2)
    _interval(db, goal, monday - timedelta(days=10), monday + timedelta(days=2))
    assert week_outcome(db, goal, monday).paused


def test_a_pause_spanning_several_weeks_neutralises_all_of_them(db):
    goal = _goal(db)
    _weekly_quest(db, goal, "a")
    third = week_start(app_today()) - timedelta(weeks=3)
    _interval(db, goal, third + timedelta(days=2), third + timedelta(days=16))
    for n in (1, 2, 3):
        monday = week_start(app_today()) - timedelta(weeks=n)
        assert week_outcome(db, goal, monday).paused, f"week -{n}"


def test_a_pause_across_the_turn_of_the_year_neutralises_both_weeks(db):
    goal = _goal(db)
    _weekly_quest(db, goal, "a")
    _interval(db, goal, date(2026, 12, 30), date(2027, 1, 2))
    assert week_outcome(db, goal, date(2026, 12, 28)).paused
    assert week_outcome(db, goal, date(2027, 1, 4)) is not None
    assert not week_outcome(db, goal, date(2026, 12, 21)).paused


@pytest.mark.parametrize("start,end,monday", [
    # DST starts in Europe/Berlin on 2026-03-29 (a Sunday)
    (date(2026, 3, 29), date(2026, 3, 29), date(2026, 3, 23)),
    # DST ends on 2026-10-25 (a Sunday)
    (date(2026, 10, 25), date(2026, 10, 25), date(2026, 10, 19)),
])
def test_a_pause_on_a_dst_day_neutralises_its_week(db, start, end, monday):
    goal = _goal(db)
    _weekly_quest(db, goal, "a")
    _interval(db, goal, start, end)
    assert week_outcome(db, goal, monday).paused
    assert not week_outcome(db, goal, monday + timedelta(weeks=1)).paused


def test_a_single_day_pause_neutralises_only_its_own_week(db):
    goal = _goal(db)
    _weekly_quest(db, goal, "a")
    monday = week_start(app_today()) - timedelta(weeks=2)
    _interval(db, goal, monday + timedelta(days=2), monday + timedelta(days=2))
    assert week_outcome(db, goal, monday).paused
    assert not week_outcome(db, goal, monday - timedelta(weeks=1)).paused
    assert not week_outcome(db, goal, monday + timedelta(weeks=1)).paused


def test_a_resumed_goal_keeps_its_earlier_paused_week_neutral(db):
    """The case the old approximation got wrong in one direction."""
    goal = _goal(db)
    _weekly_quest(db, goal, "a")
    monday = week_start(app_today()) - timedelta(weeks=2)
    _interval(db, goal, monday + timedelta(days=1), monday + timedelta(days=4))
    assert goal.status == STATUS_ACTIVE

    outcome = week_outcome(db, goal, monday)
    assert outcome.paused
    assert not outcome.scorable


def test_a_missed_week_stays_scored_after_a_later_pause(db):
    """And the case it got wrong in the other direction."""
    goal = _goal(db)
    _weekly_quest(db, goal, "a")
    missed = week_start(app_today()) - timedelta(weeks=3)
    set_status(db, goal, STATUS_PAUSED)          # paused only now

    outcome = week_outcome(db, goal, missed)
    assert not outcome.paused
    assert outcome.scorable
    assert outcome.fulfilment == 0


def test_a_currently_paused_goal_does_not_neutralise_its_whole_history(db):
    goal = _goal(db)
    quest = _weekly_quest(db, goal, "a")
    for n in (2, 3, 4):
        _rewarded(db, quest, week_start(app_today()) - timedelta(weeks=n))
    set_status(db, goal, STATUS_PAUSED)

    result = goal_momentum(db, goal)
    assert result.value == 60            # last week genuinely missed, 40 % lost
    assert result.counted_weeks == 4


def test_momentum_renormalises_after_a_recorded_pause(db):
    goal = _goal(db)
    quest = _weekly_quest(db, goal, "a")
    for n in (1, 3, 4):
        _rewarded(db, quest, week_start(app_today()) - timedelta(weeks=n))
    second = week_start(app_today()) - timedelta(weeks=2)
    _interval(db, goal, second + timedelta(days=1), second + timedelta(days=3))

    result = goal_momentum(db, goal)
    assert result.value == 100           # the 30 % week is removed, not failed
    assert result.counted_weeks == 3


def test_streak_skips_a_recorded_pause_week(db):
    """Documented rule: a neutral week is skipped, it does not split a streak."""
    goal = _goal(db)
    quest = _weekly_quest(db, goal, "a")
    for n in (1, 3, 4):
        _rewarded(db, quest, week_start(app_today()) - timedelta(weeks=n))
    second = week_start(app_today()) - timedelta(weeks=2)
    _interval(db, goal, second + timedelta(days=1), second + timedelta(days=3))

    assert goal_streak(db, goal).current == 3


def test_a_break_before_the_table_existed_is_not_invented(db):
    """A goal paused before this feature has no interval and no neutral weeks."""
    goal = _goal(db)
    _weekly_quest(db, goal, "a")
    goal.status = STATUS_PAUSED          # as an old database would look
    db.commit()

    monday = week_start(app_today()) - timedelta(weeks=2)
    assert not week_outcome(db, goal, monday).paused
    assert pause_intervals_of(db, goal) == []
