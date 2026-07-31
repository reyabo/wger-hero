import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.models import Base
from app.seed_defaults import seed_default_habits, DEFAULT_HABITS


@pytest.fixture
def fresh_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    yield db
    db.close()

def test_seed_default_habits_on_empty_db(fresh_db):
    count = seed_default_habits(fresh_db)
    assert count == len(DEFAULT_HABITS)

def test_seed_default_habits_idempotent(fresh_db):
    seed_default_habits(fresh_db)
    count2 = seed_default_habits(fresh_db)
    assert count2 == 0

def test_seed_default_habits_fresh_db(fresh_db):
    from app.models import Habit
    from app.seed_defaults import DEFAULT_HABITS, LEGACY_JAPANESE_HABIT_TITLE

    count = seed_default_habits(fresh_db)
    assert count == len(DEFAULT_HABITS)
    assert fresh_db.query(Habit).count() == len(DEFAULT_HABITS)
    # "Japanisch lernen" is deliberately no longer seeded: the SAVE import is
    # the single reward channel for Japanese sessions.
    titles = {h.title for h in fresh_db.query(Habit).all()}
    assert LEGACY_JAPANESE_HABIT_TITLE not in titles
