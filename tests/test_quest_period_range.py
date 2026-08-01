"""Tests for explicit period_start/period_end ranges on quests."""

import os
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import get_db
from app.models import Base, HeroProfile, Quest, XpEvent
from app.quests import _count_workout_variety_in_period, create_quest, parse_period_range

os.environ.setdefault("WGER_BASE_URL", "https://wger.example.com")
os.environ.setdefault("WGER_API_TOKEN", "test-token-for-quests")


# ---------------------------------------------------------------------------
# parse_period_range — pure, database-free
# ---------------------------------------------------------------------------

def test_both_empty_is_valid_and_empty():
    start, end, error = parse_period_range("", "")
    assert (start, end, error) == (None, None, None)


def test_none_is_valid_and_empty():
    assert parse_period_range(None, None) == (None, None, None)


def test_valid_range_is_normalized_to_full_days():
    start, end, error = parse_period_range("2026-08-01", "2026-10-24")
    assert error is None
    assert start == datetime(2026, 8, 1, 0, 0, 0)
    assert end.date() == datetime(2026, 10, 24).date()
    assert (end.hour, end.minute) == (23, 59)


def test_same_day_range_is_allowed():
    start, end, error = parse_period_range("2026-08-01", "2026-08-01")
    assert error is None
    assert start.date() == end.date()
    assert start.hour == 0 and end.hour == 23


def test_end_before_start_is_an_error():
    start, end, error = parse_period_range("2026-10-24", "2026-08-01")
    assert error
    assert (start, end) == (None, None)


def test_only_start_is_an_error():
    start, end, error = parse_period_range("2026-08-01", "")
    assert error
    assert (start, end) == (None, None)


def test_only_end_is_an_error():
    start, end, error = parse_period_range("", "2026-08-01")
    assert error
    assert (start, end) == (None, None)


def test_unparseable_date_is_an_error():
    start, end, error = parse_period_range("nicht-ein-datum", "2026-08-01")
    assert error
    assert (start, end) == (None, None)


def test_error_messages_are_german():
    for args in [("2026-10-24", "2026-08-01"), ("2026-08-01", ""), ("x", "y")]:
        _, _, error = parse_period_range(*args)
        assert error and error.strip()


# ---------------------------------------------------------------------------
# Counting inside an explicit window
# ---------------------------------------------------------------------------

@pytest.fixture
def db_session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _workout(db, title, when):
    db.add(
        XpEvent(
            event_type="workout_complete",
            source="wger",
            source_id=f"{title}-{when.isoformat()}",
            xp=100,
            attribute="Strength",
            title=title,
            created_at=when,
        )
    )
    db.commit()


def test_variety_quest_counts_only_inside_the_range(db_session):
    """The 12-week arc: workouts before or after the window must not count."""
    db = db_session
    start = datetime.utcnow() - timedelta(days=10)
    end = datetime.utcnow() + timedelta(days=70)

    _workout(db, "Rinne Arc – Push", datetime.utcnow())                      # inside
    _workout(db, "Rinne Arc – Pull", datetime.utcnow() - timedelta(days=2))  # inside
    _workout(db, "Rinne Arc – Beine", datetime.utcnow() - timedelta(days=40))  # before
    _workout(db, "Rinne Arc – Core", datetime.utcnow() + timedelta(days=200))  # after

    quest = create_quest(
        db,
        title="Rinne Arc",
        quest_type="workout_variety",
        period="once",
        target_value=4,
        match_text="Push,Pull,Beine,Core",
        period_start=start,
        period_end=end,
    )
    assert _count_workout_variety_in_period(db, quest) == 2


def test_create_quest_persists_the_range(db_session):
    start = datetime(2026, 8, 1, 0, 0, 0)
    end = datetime(2026, 10, 24, 23, 59, 59)
    quest = create_quest(
        db_session,
        title="Arc",
        quest_type="workout_variety",
        period="once",
        target_value=3,
        match_text="Push,Pull",
        period_start=start,
        period_end=end,
    )
    assert quest.period_start == start
    assert quest.period_end == end


# ---------------------------------------------------------------------------
# Form routes
# ---------------------------------------------------------------------------

@pytest.fixture
def client():
    import app.config as cfg
    cfg._settings = None

    from app.main import app

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine)

    def override_db():
        s = TestSession()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = override_db
    seed = TestSession()
    seed.add(HeroProfile(name="Hero", level=1, total_xp=0))
    seed.commit()
    seed.close()

    with TestClient(app) as c:
        yield c, TestSession

    app.dependency_overrides.clear()


def _form(**over):
    data = {
        "title": "Rinne Arc",
        "quest_type": "workout_variety",
        "period": "once",
        "target_value": "4",
        "match_text": "Push,Pull,Beine,Core",
        "xp_reward": "500",
        "active": "on",
    }
    data.update(over)
    return data


def test_form_offers_the_date_fields(client):
    c, _ = client
    resp = c.get("/quests/new")
    assert resp.status_code == 200
    assert 'name="period_start"' in resp.text
    assert 'name="period_end"' in resp.text


def test_create_with_valid_range(client):
    c, Session = client
    resp = c.post(
        "/quests/new",
        data=_form(period_start="2026-08-01", period_end="2026-10-24"),
        follow_redirects=False,
    )
    assert resp.status_code == 303

    db = Session()
    quest = db.query(Quest).filter(Quest.title == "Rinne Arc").one()
    assert quest.period_start == datetime(2026, 8, 1, 0, 0, 0)
    assert quest.period_end.date() == datetime(2026, 10, 24).date()
    assert quest.period_end.hour == 23
    db.close()


def test_create_without_dates_still_works(client):
    c, Session = client
    resp = c.post("/quests/new", data=_form(period="weekly"), follow_redirects=False)
    assert resp.status_code == 303
    db = Session()
    assert db.query(Quest).filter(Quest.title == "Rinne Arc").count() == 1
    db.close()


def test_end_before_start_is_rejected_and_saves_nothing(client):
    c, Session = client
    resp = c.post(
        "/quests/new",
        data=_form(period_start="2026-10-24", period_end="2026-08-01"),
    )
    assert resp.status_code == 400
    db = Session()
    assert db.query(Quest).filter(Quest.title == "Rinne Arc").count() == 0
    db.close()


def test_partial_range_is_rejected(client):
    c, Session = client
    resp = c.post("/quests/new", data=_form(period_start="2026-08-01"))
    assert resp.status_code == 400
    db = Session()
    assert db.query(Quest).filter(Quest.title == "Rinne Arc").count() == 0
    db.close()


def test_rejected_form_keeps_the_entered_values(client):
    c, _ = client
    resp = c.post(
        "/quests/new",
        data=_form(period_start="2026-10-24", period_end="2026-08-01"),
    )
    assert "Rinne Arc" in resp.text          # title preserved
    assert "2026-10-24" in resp.text          # entered dates preserved
    assert "Push,Pull,Beine,Core" in resp.text


def test_edit_can_set_a_range(client):
    c, Session = client
    c.post("/quests/new", data=_form(period="weekly"), follow_redirects=False)
    db = Session()
    quest_id = db.query(Quest).filter(Quest.title == "Rinne Arc").one().id
    db.close()

    resp = c.post(
        f"/quests/{quest_id}/edit",
        data=_form(period_start="2026-08-01", period_end="2026-10-24"),
        follow_redirects=False,
    )
    assert resp.status_code == 303

    db = Session()
    quest = db.get(Quest, quest_id)
    assert quest.period_start == datetime(2026, 8, 1, 0, 0, 0)
    db.close()


def test_edit_with_invalid_range_changes_nothing(client):
    c, Session = client
    c.post(
        "/quests/new",
        data=_form(period_start="2026-08-01", period_end="2026-10-24"),
        follow_redirects=False,
    )
    db = Session()
    quest = db.query(Quest).filter(Quest.title == "Rinne Arc").one()
    quest_id, before = quest.id, quest.period_start
    db.close()

    resp = c.post(
        f"/quests/{quest_id}/edit",
        data=_form(period_start="2026-10-24", period_end="2026-08-01"),
    )
    assert resp.status_code == 400

    db = Session()
    assert db.get(Quest, quest_id).period_start == before
    db.close()


def test_switching_away_from_once_clears_the_range(client):
    """A stale explicit window would otherwise override the derived one."""
    c, Session = client
    c.post(
        "/quests/new",
        data=_form(period_start="2026-08-01", period_end="2026-10-24"),
        follow_redirects=False,
    )
    db = Session()
    quest_id = db.query(Quest).filter(Quest.title == "Rinne Arc").one().id
    db.close()

    c.post(f"/quests/{quest_id}/edit", data=_form(period="weekly"), follow_redirects=False)

    db = Session()
    quest = db.get(Quest, quest_id)
    assert quest.period_start is None
    assert quest.period_end is None
    db.close()
