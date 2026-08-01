"""Tests for mapping stored history onto week outcomes."""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.goals import STATUS_ACTIVE, STATUS_PAUSED, create_goal, set_status
from app.goal_progress import (
    goal_momentum,
    goal_streak,
    goal_week_summary,
    week_outcome,
    weekly_quests_of,
)
from app.models import Base, Quest, QuestCompletion
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


def test_paused_goal_marks_weeks_as_paused(db):
    goal = _goal(db)
    _weekly_quest(db, goal, "a")
    set_status(db, goal, STATUS_PAUSED)
    outcome = week_outcome(db, goal, week_start(app_today()) - timedelta(weeks=1))
    assert outcome.paused
    assert not outcome.scorable


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


def test_paused_goal_momentum_is_none_not_zero(db):
    goal = _goal(db)
    _weekly_quest(db, goal, "a")
    set_status(db, goal, STATUS_PAUSED)
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
