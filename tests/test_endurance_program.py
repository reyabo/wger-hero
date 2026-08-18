"""The endurance goal, and the week it actually demands.

The plan is Tuesday plus one weekend day. The point of this file is that the
existing goal engine produces exactly that from the seeded data alone — the
five acceptance cases at the bottom are run against the real
`goal_progress.week_outcome`, not against a reimplementation.
"""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.endurance_program import (
    ALL_QUESTS,
    GOAL_SLUG,
    GOAL_TITLE,
    MILESTONES,
    MONTHLY_QUESTS,
    WEEKLY_QUESTS,
    apply_endurance_program,
    plan_endurance_program,
)
from app.goal_progress import week_outcome, weekly_quests_of
from app.models import (
    Base,
    FitTrackeeWorkout,
    Goal,
    HeroProfile,
    Quest,
    QuestCompletion,
)
from app.quests import evaluate_quests, parse_allowed_weekdays

MONDAY = datetime(2026, 8, 17, 10, 0)
TUESDAY = datetime(2026, 8, 18, 10, 0)
WEDNESDAY = datetime(2026, 8, 19, 10, 0)
THURSDAY = datetime(2026, 8, 20, 10, 0)
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
def _fixed_today(monkeypatch):
    """Sunday of the plan week, so every weekly window covers all its days."""
    import app.quests as quests

    monkeypatch.setattr(quests, "app_today", lambda: SUNDAY.date())


def add_workout(db, when, external_id=None, seconds=2400, qualifying=True, eligible=True):
    from app.quests import app_date_of

    external_id = external_id or f"ft-{when.isoformat()}"
    db.add(
        FitTrackeeWorkout(
            external_id=external_id,
            workout_at=when,
            local_date=app_date_of(when),
            sport_id=5,
            sport_label="Running",
            duration_seconds=seconds,
            moving_seconds=None,
            qualifies_for_endurance=qualifying,
            reward_eligible=eligible,
            source_hash=f"h-{external_id}",
        )
    )
    db.commit()


def backdate_goal(db):
    """The goal must predate the judged week, or it reads as "no history"."""
    goal = db.query(Goal).filter(Goal.slug == GOAL_SLUG).one()
    goal.created_at = datetime(2026, 1, 1)
    db.commit()
    return goal


# ---------------------------------------------------------------------------
# The shape of the programme
# ---------------------------------------------------------------------------

def test_the_plan_writes_nothing(db):
    plan = plan_endurance_program(db)
    assert plan.creates_anything
    assert db.query(Goal).count() == 0
    assert db.query(Quest).count() == 0


def test_it_creates_the_goal(db):
    apply_endurance_program(db)
    goal = db.query(Goal).filter(Goal.slug == GOAL_SLUG).one()
    assert goal.title == GOAL_TITLE
    assert goal.status == "active"


def test_it_creates_exactly_eight_quests(db):
    apply_endurance_program(db)
    assert db.query(Quest).count() == len(ALL_QUESTS) == 8


def test_the_weekly_quests_are_the_two_planned_slots(db):
    apply_endurance_program(db)
    goal = db.query(Goal).filter(Goal.slug == GOAL_SLUG).one()
    titles = sorted(q.title for q in weekly_quests_of(db, goal))
    assert titles == ["Dienstagsrunde", "Wochenendrunde"]


def test_the_monthly_quest_is_not_weekly(db):
    """Otherwise it would silently become a condition of a successful week."""
    apply_endurance_program(db)
    goal = db.query(Goal).filter(Goal.slug == GOAL_SLUG).one()
    assert "Vier Stunden Basis" not in {q.title for q in weekly_quests_of(db, goal)}


def test_the_milestones_are_not_weekly(db):
    apply_endurance_program(db)
    goal = db.query(Goal).filter(Goal.slug == GOAL_SLUG).one()
    weekly = {q.title for q in weekly_quests_of(db, goal)}
    for spec in MILESTONES:
        assert spec.title not in weekly


@pytest.mark.parametrize("spec", ALL_QUESTS, ids=lambda s: s.title)
def test_every_quest_of_the_programme_exists(db, spec):
    apply_endurance_program(db)
    quest = db.query(Quest).filter(Quest.title == spec.title).one()
    assert quest.quest_type == spec.quest_type
    assert quest.period == spec.period
    assert quest.target_value == spec.target_value
    assert quest.xp_reward == spec.xp_reward
    assert quest.is_milestone is spec.is_milestone


@pytest.mark.parametrize("spec", ALL_QUESTS, ids=lambda s: s.title)
def test_every_quest_rewards_endurance(db, spec):
    from app.stats import parse_stat_rewards

    apply_endurance_program(db)
    quest = db.query(Quest).filter(Quest.title == spec.title).one()
    assert parse_stat_rewards(quest.stat_rewards) == {"endurance": spec.stat_xp}


def test_the_tuesday_quest_is_restricted_to_tuesday(db):
    apply_endurance_program(db)
    quest = db.query(Quest).filter(Quest.title == "Dienstagsrunde").one()
    assert parse_allowed_weekdays(quest.allowed_weekdays) == [2]


def test_the_weekend_quest_is_restricted_to_saturday_and_sunday(db):
    apply_endurance_program(db)
    quest = db.query(Quest).filter(Quest.title == "Wochenendrunde").one()
    assert parse_allowed_weekdays(quest.allowed_weekdays) == [6, 7]


def test_the_monthly_target_is_four_hours(db):
    apply_endurance_program(db)
    quest = db.query(Quest).filter(Quest.title == "Vier Stunden Basis").one()
    assert quest.target_value == 240
    assert quest.period == "monthly"


def test_the_milestones_count_units_not_weeks(db):
    """"Fünf Wochen" is a label. The condition is ten workouts."""
    apply_endurance_program(db)
    quest = db.query(Quest).filter(Quest.title == "Fünf Wochen Ausdauer").one()
    assert quest.quest_type == "fittrackee_workout_count"
    assert quest.target_value == 10
    assert quest.period == "once"
    assert quest.repeatable is False


# ---------------------------------------------------------------------------
# Idempotence
# ---------------------------------------------------------------------------

def test_applying_twice_creates_no_duplicates(db):
    apply_endurance_program(db)
    apply_endurance_program(db)

    assert db.query(Goal).filter(Goal.slug == GOAL_SLUG).count() == 1
    assert db.query(Quest).count() == len(ALL_QUESTS)


def test_applying_five_times_still_creates_no_duplicates(db):
    for _ in range(5):
        apply_endurance_program(db)
    assert db.query(Quest).count() == len(ALL_QUESTS)


def test_a_user_edited_quest_is_never_reset(db):
    apply_endurance_program(db)
    quest = db.query(Quest).filter(Quest.title == "Vier Stunden Basis").one()
    quest.target_value = 120
    quest.xp_reward = 999
    db.commit()

    apply_endurance_program(db)

    again = db.query(Quest).filter(Quest.title == "Vier Stunden Basis").one()
    assert again.target_value == 120
    assert again.xp_reward == 999


def test_an_edited_weekday_set_is_never_reset(db):
    apply_endurance_program(db)
    quest = db.query(Quest).filter(Quest.title == "Dienstagsrunde").one()
    quest.allowed_weekdays = "3"        # the user moved it to Wednesday
    db.commit()

    apply_endurance_program(db)

    assert db.query(Quest).filter(Quest.title == "Dienstagsrunde").one().allowed_weekdays == "3"


def test_xp_history_is_never_touched(db):
    from app.models import XpEvent

    apply_endurance_program(db)
    db.add(XpEvent(event_type="test", source="fittrackee", xp=40, attribute="Endurance",
                   title="alt"))
    db.commit()

    apply_endurance_program(db)

    assert db.query(XpEvent).count() == 1


def test_a_second_plan_reports_nothing_to_create(db):
    apply_endurance_program(db)
    plan = plan_endurance_program(db)
    assert not plan.creates_anything


# ---------------------------------------------------------------------------
# The acceptance cases — run against the real goal engine
# ---------------------------------------------------------------------------

def _play(db, *days):
    apply_endurance_program(db)
    goal = backdate_goal(db)
    for n, day in enumerate(days):
        add_workout(db, day, external_id=f"w{n}")
    evaluate_quests(db, db.query(HeroProfile).one())
    return week_outcome(db, goal, MONDAY.date())


def test_case_a_tuesday_and_saturday_is_a_successful_week(db):
    outcome = _play(db, TUESDAY, SATURDAY)
    assert outcome.achieved == outcome.target == 2


def test_case_b_tuesday_and_sunday_is_a_successful_week(db):
    outcome = _play(db, TUESDAY, SUNDAY)
    assert outcome.achieved == outcome.target == 2


def test_case_c_saturday_and_sunday_is_not(db):
    """The weekend quest is satisfied; Tuesday is not."""
    outcome = _play(db, SATURDAY, SUNDAY)
    assert outcome.achieved == 1
    assert outcome.target == 2


def test_case_d_tuesday_and_thursday_is_not(db):
    outcome = _play(db, TUESDAY, THURSDAY)
    assert outcome.achieved == 1
    assert outcome.target == 2


def test_case_e_no_workouts_satisfies_nothing(db):
    outcome = _play(db)
    assert outcome.achieved == 0
    assert outcome.target == 2


def test_tuesday_saturday_and_sunday_is_still_just_successful(db):
    outcome = _play(db, TUESDAY, SATURDAY, SUNDAY)
    assert outcome.achieved == outcome.target == 2


def test_monday_and_saturday_is_not_successful(db):
    outcome = _play(db, MONDAY, SATURDAY)
    assert outcome.achieved == 1


def test_two_tuesday_sessions_satisfy_only_the_tuesday_slot(db):
    outcome = _play(db, TUESDAY, TUESDAY.replace(hour=18))
    assert outcome.achieved == 1


def test_a_thursday_ride_still_earns_its_workout_xp(db):
    """Rule 10: extra movement is recognised even off-plan — it simply does
    not fill either weekly slot."""
    outcome = _play(db, THURSDAY)
    assert outcome.achieved == 0

    quest = db.query(Quest).filter(Quest.title == "Der erste Schritt").one()
    assert quest.completed_at is not None      # the once-milestone did count


def test_the_weekly_quests_are_rewarded_exactly_once(db):
    _play(db, TUESDAY, SATURDAY)
    evaluate_quests(db, db.query(HeroProfile).one())

    for title in ("Dienstagsrunde", "Wochenendrunde"):
        quest = db.query(Quest).filter(Quest.title == title).one()
        assert (
            db.query(QuestCompletion)
            .filter(QuestCompletion.quest_id == quest.id)
            .count()
            == 1
        )


def test_baseline_workouts_never_satisfy_the_week(db):
    apply_endurance_program(db)
    goal = backdate_goal(db)
    add_workout(db, TUESDAY, external_id="hist-1", eligible=False)
    add_workout(db, SATURDAY, external_id="hist-2", eligible=False)
    evaluate_quests(db, db.query(HeroProfile).one())

    assert week_outcome(db, goal, MONDAY.date()).achieved == 0
