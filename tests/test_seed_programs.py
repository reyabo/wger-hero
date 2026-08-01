"""Tests for the opt-in training programs."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import Base, Habit, Quest
from app.rewards import CATEGORY_CHOICES, DURATION_CHOICES, EFFORT_CHOICES
from app.seed_programs import PROGRAMS, seed_program


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


def test_program_seeds_habits_and_quests(db):
    program = PROGRAMS["control"]
    habits, quests = seed_program(db, "control")
    assert habits == len(program.habits)
    assert quests == len(program.quests)
    assert db.query(Habit).count() == len(program.habits)
    assert db.query(Quest).count() == len(program.quests)


def test_seeding_is_idempotent(db):
    seed_program(db, "control")
    habits, quests = seed_program(db, "control")
    assert (habits, quests) == (0, 0)
    assert db.query(Habit).count() == len(PROGRAMS["control"].habits)
    assert db.query(Quest).count() == len(PROGRAMS["control"].quests)


def test_unknown_program_raises(db):
    with pytest.raises(KeyError):
        seed_program(db, "does-not-exist")


def test_not_seeded_on_startup(db):
    """The program must never be applied automatically."""
    from app.seed_defaults import DEFAULT_HABITS

    titles = {h["title"] for h in DEFAULT_HABITS}
    for item in PROGRAMS["control"].habits:
        assert item.title not in titles


def test_habits_use_valid_reward_fields(db):
    """Every habit must produce an auto-calculated reward, not a fallback."""
    seed_program(db, "control")
    for habit in db.query(Habit).all():
        assert habit.category in CATEGORY_CHOICES
        assert habit.duration_size in DURATION_CHOICES
        assert habit.effort in EFFORT_CHOICES
        assert habit.base_xp_reward > 0
        assert habit.stat_rewards          # stat split was derived


def test_quests_are_finite_and_not_repeatable(db):
    """Stage transitions end once reached — they are milestones, not routines."""
    seed_program(db, "control")
    for quest in db.query(Quest).all():
        assert quest.period == "once"
        assert quest.quest_type == "manual"
        assert quest.repeatable is False
        assert quest.target_value >= 1


def test_stage_targets_match_their_criteria(db):
    seed_program(db, "control")
    targets = {q.title: q.target_value for q in db.query(Quest).all()}
    assert targets["Stufe 3 – Präzision"] == 3
    assert targets["Stufe 4 – Erste Serie"] == 2


def test_existing_titles_are_left_alone(db):
    """A habit the user already edited must not be duplicated or overwritten."""
    first = PROGRAMS["control"].habits[0]
    db.add(Habit(title=first.title, description="eigene Notiz",
                 active=False, base_xp_reward=99))
    db.commit()

    habits, _ = seed_program(db, "control")
    assert habits == len(PROGRAMS["control"].habits) - 1

    kept = db.query(Habit).filter(Habit.title == first.title).one()
    assert kept.description == "eigene Notiz"
    assert kept.base_xp_reward == 99
    assert kept.active is False
