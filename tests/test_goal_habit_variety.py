"""The goal-scoped quest source: how many different habits of a goal were done.

"All five routines this week" is a question about which of the five happened,
not about how many sessions there were — so completing one routine five times
must not satisfy a quest meant to span five.
"""

from datetime import datetime, time, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.goals import create_goal
from app.habits import create_habit
from app.models import Base, HabitCompletion, HeroProfile, Quest
from app.quests import (
    QUEST_TYPE_CHOICES,
    _current_week_bounds,
    app_today,
    count_quest_progress,
    create_quest,
    evaluate_quests,
    update_quest,
)


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


def _this_week(offset_days=0):
    monday, _ = _current_week_bounds()
    return datetime.combine(monday + timedelta(days=offset_days), time(9, 0))


def _habit(db, title, goal=None):
    habit = create_habit(db, title=title, base_xp_reward=10)
    if goal is not None:
        habit.goal_id = goal.id
        db.commit()
    return habit


def _done(db, habit, when=None, times=1):
    for _ in range(times):
        db.add(HabitCompletion(habit_id=habit.id,
                               completed_at=when or _this_week(),
                               xp_awarded=10, stat_xp_awarded=0))
    db.commit()


def _quest(db, goal=None, target=5, period="weekly"):
    return create_quest(
        db, title="Der Fünfer-Rhythmus", quest_type="goal_habit_variety",
        period=period, target_value=target, repeatable=True,
        goal_id=goal.id if goal else None,
    )


# ---------------------------------------------------------------------------
# The source exists and is selectable
# ---------------------------------------------------------------------------

def test_the_source_is_a_known_quest_type():
    assert "goal_habit_variety" in QUEST_TYPE_CHOICES


def test_the_existing_sources_are_untouched():
    for kept in ("manual", "habit_count", "workout_count", "workout_variety",
                 "japanese_session_count"):
        assert kept in QUEST_TYPE_CHOICES


# ---------------------------------------------------------------------------
# Counting: distinct habits, not completions
# ---------------------------------------------------------------------------

def test_it_counts_different_habits(db):
    goal = create_goal(db, title="Körperkontrolle")
    for title in ("Kraft", "Atem", "Kontrolle"):
        _done(db, _habit(db, title, goal))

    assert count_quest_progress(db, _quest(db, goal)) == 3


def test_repeating_one_habit_does_not_inflate_the_count(db):
    """Five sessions of one routine are one routine, not five."""
    goal = create_goal(db, title="Körperkontrolle")
    habit = _habit(db, "Kraft", goal)
    _done(db, habit, times=5)

    assert count_quest_progress(db, _quest(db, goal)) == 1


def test_the_five_routines_of_a_week_count_as_five(db):
    goal = create_goal(db, title="Körperkontrolle")
    for day, title in enumerate(
        ["Kontrolltraining – Kraft", "Atem & Wahrnehmung", "Kontrollsession",
         "Lösen & Entspannen", "Lange Kontrollsession"]
    ):
        _done(db, _habit(db, title, goal), when=_this_week(day))

    assert count_quest_progress(db, _quest(db, goal)) == 5


def test_a_habit_of_another_goal_is_not_counted(db):
    goal = create_goal(db, title="Körperkontrolle")
    other = create_goal(db, title="Kraftpfad")
    _done(db, _habit(db, "Kraft", goal))
    _done(db, _habit(db, "Bankdrücken", other))

    assert count_quest_progress(db, _quest(db, goal)) == 1


def test_a_habit_without_a_goal_is_not_counted(db):
    goal = create_goal(db, title="Körperkontrolle")
    _done(db, _habit(db, "Kraft", goal))
    _done(db, _habit(db, "Freifliegend"))

    assert count_quest_progress(db, _quest(db, goal)) == 1


def test_a_completion_outside_the_period_is_not_counted(db):
    goal = create_goal(db, title="Körperkontrolle")
    _done(db, _habit(db, "Kraft", goal), when=_this_week() - timedelta(days=14))

    assert count_quest_progress(db, _quest(db, goal)) == 0


def test_nothing_done_counts_zero(db):
    goal = create_goal(db, title="Körperkontrolle")
    _habit(db, "Kraft", goal)
    assert count_quest_progress(db, _quest(db, goal)) == 0


# ---------------------------------------------------------------------------
# A quest without a goal
# ---------------------------------------------------------------------------

def test_without_a_goal_it_counts_nothing(db):
    """Never "every habit" — a half-configured quest must not complete itself."""
    goal = create_goal(db, title="Körperkontrolle")
    for title in ("A", "B", "C", "D", "E", "F"):
        _done(db, _habit(db, title, goal))

    assert count_quest_progress(db, _quest(db, goal=None)) == 0


def test_a_goalless_quest_never_completes_itself(db):
    goal = create_goal(db, title="Körperkontrolle")
    for title in ("A", "B", "C", "D", "E"):
        _done(db, _habit(db, title, goal))

    quest = _quest(db, goal=None, target=1)
    evaluate_quests(db, db.query(HeroProfile).one())
    db.refresh(quest)

    assert quest.completed_at is None
    assert db.query(HeroProfile).one().total_xp == 0


# ---------------------------------------------------------------------------
# It drives the normal quest machinery
# ---------------------------------------------------------------------------

def test_it_completes_and_pays_at_the_target(db):
    """A repeatable quest re-arms after paying, so the payment is the evidence."""
    from app.models import QuestCompletion

    goal = create_goal(db, title="Körperkontrolle")
    quest = _quest(db, goal, target=3)
    for title in ("A", "B", "C"):
        _done(db, _habit(db, title, goal))

    newly = evaluate_quests(db, db.query(HeroProfile).one())

    assert "Der Fünfer-Rhythmus" in newly
    assert db.query(QuestCompletion).filter(
        QuestCompletion.quest_id == quest.id).count() == 1
    assert db.query(HeroProfile).one().total_xp > 0


def test_a_one_off_quest_of_this_type_closes_when_reached(db):
    goal = create_goal(db, title="Körperkontrolle")
    quest = create_quest(
        db, title="Einmal alle drei", quest_type="goal_habit_variety",
        period="once", target_value=3, repeatable=False, goal_id=goal.id,
    )
    for title in ("A", "B", "C"):
        _done(db, _habit(db, title, goal))

    evaluate_quests(db, db.query(HeroProfile).one())
    db.refresh(quest)

    assert quest.current_value == 3
    assert quest.completed_at is not None


def test_it_does_not_complete_below_the_target(db):
    goal = create_goal(db, title="Körperkontrolle")
    quest = _quest(db, goal, target=5)
    for title in ("A", "B"):
        _done(db, _habit(db, title, goal))

    evaluate_quests(db, db.query(HeroProfile).one())
    db.refresh(quest)

    assert quest.current_value == 2
    assert quest.completed_at is None


def test_a_repeated_evaluation_pays_once(db):
    goal = create_goal(db, title="Körperkontrolle")
    _quest(db, goal, target=1)
    _done(db, _habit(db, "A", goal))

    hero = db.query(HeroProfile).one()
    evaluate_quests(db, hero)
    after_first = db.query(HeroProfile).one().total_xp
    evaluate_quests(db, hero)

    assert db.query(HeroProfile).one().total_xp == after_first


def test_a_monthly_period_uses_the_month(db):
    goal = create_goal(db, title="Körperkontrolle")
    _done(db, _habit(db, "A", goal),
          when=datetime.combine(app_today().replace(day=1), time(9, 0)))

    assert count_quest_progress(db, _quest(db, goal, period="monthly")) == 1


def test_a_once_period_has_no_window(db):
    """A "once" quest gets (None, None) — the date filters must be skipped."""
    goal = create_goal(db, title="Körperkontrolle")
    _done(db, _habit(db, "A", goal), when=_this_week() - timedelta(days=400))

    assert count_quest_progress(db, _quest(db, goal, period="once")) == 1


# ---------------------------------------------------------------------------
# The goal link must not be lost by accident
# ---------------------------------------------------------------------------

def test_updating_other_fields_keeps_the_goal(db):
    """Every other field is assigned unconditionally; this one must not be."""
    goal = create_goal(db, title="Körperkontrolle")
    quest = _quest(db, goal)

    update_quest(
        db, quest, title="Neuer Titel", description=None,
        quest_type=quest.quest_type, period=quest.period,
        target_value=quest.target_value, match_text=None,
        repeatable=True, active=True,
    )

    assert quest.goal_id == goal.id
    assert quest.title == "Neuer Titel"


def test_the_goal_can_still_be_cleared_explicitly(db):
    goal = create_goal(db, title="Körperkontrolle")
    quest = _quest(db, goal)

    update_quest(
        db, quest, title=quest.title, description=None,
        quest_type=quest.quest_type, period=quest.period,
        target_value=quest.target_value, match_text=None,
        repeatable=True, active=True, goal_id=None,
    )

    assert quest.goal_id is None


def test_the_goal_can_be_changed(db):
    goal = create_goal(db, title="Körperkontrolle")
    other = create_goal(db, title="Kraftpfad")
    quest = _quest(db, goal)

    update_quest(
        db, quest, title=quest.title, description=None,
        quest_type=quest.quest_type, period=quest.period,
        target_value=quest.target_value, match_text=None,
        repeatable=True, active=True, goal_id=other.id,
    )

    assert quest.goal_id == other.id


# ---------------------------------------------------------------------------
# workout_variety is a different question and stays as it is
# ---------------------------------------------------------------------------

def test_workout_variety_still_counts_workout_titles(db):
    """Different data, different scoping — the new source does not replace it."""
    from app.models import XpEvent

    quest = create_quest(
        db, title="HOME HERO", quest_type="workout_variety", period="weekly",
        target_value=2, match_text="Beine,Push",
    )
    for title in ("Tag 1 – Beine", "Tag 2 – Push"):
        db.add(XpEvent(event_type="workout_complete", source="wger",
                       source_id=title, xp=100, attribute="Strength",
                       title=title, description="", created_at=_this_week()))
    db.commit()

    assert count_quest_progress(db, quest) == 2
