import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.models import Base, Habit
from app.habits import create_habit, delete_or_archive_habit
from app.quests import create_quest, delete_or_archive_quest


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


def test_delete_habit_no_completions_removes_it(db):
    habit = create_habit(db, title="Test", base_xp_reward=10)
    result = delete_or_archive_habit(db, habit)
    assert result == "deleted"
    assert db.query(Habit).filter_by(id=habit.id).first() is None


def test_delete_habit_with_completions_archives_it(db):
    from app.models import HabitCompletion
    habit = create_habit(db, title="Test2", base_xp_reward=10)
    completion = HabitCompletion(habit_id=habit.id)
    db.add(completion)
    db.commit()
    result = delete_or_archive_habit(db, habit)
    assert result == "archived"
    assert habit.active == False


def test_archived_habit_cannot_be_completed(db):
    from app.models import HabitCompletion
    habit = create_habit(db, title="Test3", base_xp_reward=10)
    completion = HabitCompletion(habit_id=habit.id)
    db.add(completion)
    db.commit()
    delete_or_archive_habit(db, habit)
    assert habit.active == False


def test_delete_quest_no_history_removes_it(db):
    from app.models import Quest
    quest = create_quest(db, title="Q1", quest_type="count", target_value=5)
    result = delete_or_archive_quest(db, quest)
    assert result == "deleted"
    assert db.query(Quest).filter_by(id=quest.id).first() is None


def test_delete_quest_with_history_archives_it(db):
    from app.models import Quest, XpEvent
    quest = create_quest(db, title="Q2", quest_type="count", target_value=5)
    event = XpEvent(source="quest", source_id=quest.slug, xp=10, event_type="quest_complete", attribute="Strength", title="Q2")
    db.add(event)
    db.commit()
    result = delete_or_archive_quest(db, quest)
    assert result == "archived"
    assert quest.active == False
