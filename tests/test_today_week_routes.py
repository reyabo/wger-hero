"""Route tests for /today and /week, including auth, CSRF and safe returns."""

from datetime import datetime, time, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import Base, HabitCompletion, HeroProfile, Quest
from app.quests import app_today


@pytest.fixture
def client():
    import os

    os.environ.setdefault("WGER_BASE_URL", "https://wger.example.com")
    import app.config as cfg

    cfg._settings = None

    from fastapi.testclient import TestClient

    from app.database import get_db
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


def _make_habit(c, title="Gewohnheit", weekdays=None):
    data = {"title": title, "active": "on", "recurrence": "daily", "target_count": "1",
            "base_xp_reward": "10"}
    if weekdays:
        data["weekdays"] = [str(d) for d in weekdays]
    c.post("/habits/new", data=data, follow_redirects=False)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def test_today_page_renders(client):
    c, _ = client
    resp = c.get("/today")
    assert resp.status_code == 200
    assert "Heute" in resp.text


def test_week_page_renders(client):
    c, _ = client
    resp = c.get("/week")
    assert resp.status_code == 200
    assert "Woche" in resp.text


def test_navigation_offers_both_views(client):
    c, _ = client
    html = c.get("/").text
    assert 'href="/today"' in html
    assert 'href="/week"' in html


def test_today_shows_the_current_local_day(client):
    c, _ = client
    assert app_today().strftime("%d.%m.%Y") in c.get("/today").text


def test_today_links_to_the_week_view(client):
    c, _ = client
    assert 'href="/week"' in c.get("/today").text


def test_week_shows_all_seven_weekdays(client):
    c, _ = client
    html = c.get("/week").text
    for label in ("Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag",
                  "Samstag", "Sonntag"):
        assert label in html


def test_empty_state_is_stated_plainly(client):
    c, _ = client
    html = c.get("/today").text
    assert "nichts fest eingeplant" in html.lower()
    for word in ("gescheitert", "Strafe", "verloren", "faul"):
        assert word.lower() not in html.lower()


# ---------------------------------------------------------------------------
# Weekday planning through the form
# ---------------------------------------------------------------------------

def test_a_habit_planned_for_today_appears_on_today(client):
    c, _ = client
    _make_habit(c, "Heute geplant", weekdays=[app_today().isoweekday()])
    assert "Heute geplant" in c.get("/today").text


def test_a_habit_planned_for_another_day_does_not_appear_today(client):
    c, _ = client
    other = (app_today().isoweekday() % 7) + 1
    _make_habit(c, "Anderer Tag", weekdays=[other])
    html = c.get("/today").text
    section = html.split("Jederzeit möglich")[0]
    assert "Anderer Tag" not in section


def test_an_unplanned_habit_stays_available(client):
    c, _ = client
    _make_habit(c, "Flexibel")
    assert "Flexibel" in c.get("/today").text


def test_the_form_prefills_the_stored_plan(client):
    c, _ = client
    _make_habit(c, "Mo Mi", weekdays=[1, 3])
    html = c.get("/habits/1/edit").text
    assert 'value="1"' in html and "checked" in html


def test_an_invalid_weekday_is_refused_by_the_server(client):
    c, TestSession = client
    from app.models import HabitScheduleDay

    resp = c.post(
        "/habits/new",
        data={"title": "Manipuliert", "active": "on", "recurrence": "daily",
              "target_count": "1", "base_xp_reward": "10", "weekdays": ["9"]},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    db = TestSession()
    assert db.query(HabitScheduleDay).count() == 0
    db.close()


def test_editing_other_fields_keeps_the_plan(client):
    c, TestSession = client
    from app.models import HabitScheduleDay

    _make_habit(c, "Alt", weekdays=[2, 5])
    c.post(
        "/habits/1/edit",
        data={"title": "Neu", "active": "on", "recurrence": "daily",
              "target_count": "1", "base_xp_reward": "10", "weekdays": ["2", "5"]},
        follow_redirects=False,
    )
    db = TestSession()
    assert db.query(HabitScheduleDay).count() == 2
    db.close()


def test_submitting_no_weekday_clears_the_plan(client):
    c, TestSession = client
    from app.models import HabitScheduleDay

    _make_habit(c, "Geplant", weekdays=[2])
    c.post(
        "/habits/1/edit",
        data={"title": "Geplant", "active": "on", "recurrence": "daily",
              "target_count": "1", "base_xp_reward": "10"},
        follow_redirects=False,
    )
    db = TestSession()
    assert db.query(HabitScheduleDay).count() == 0
    db.close()


# ---------------------------------------------------------------------------
# Completing from the views
# ---------------------------------------------------------------------------

def test_completing_from_today_returns_to_today(client):
    c, _ = client
    _make_habit(c, "Heute", weekdays=[app_today().isoweekday()])
    resp = c.post(
        "/habits/1/complete", data={"next": "/today"}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/today"


def test_completing_from_the_week_returns_to_that_week(client):
    c, _ = client
    _make_habit(c, "Heute", weekdays=[app_today().isoweekday()])
    target = "/week?date=2026-08-03"
    resp = c.post(
        "/habits/1/complete", data={"next": target}, follow_redirects=False
    )
    assert resp.headers["location"] == target


def test_completion_awards_xp_once_through_the_existing_service(client):
    c, TestSession = client
    from app.models import XpEvent

    _make_habit(c, "Heute", weekdays=[app_today().isoweekday()])
    c.post("/habits/1/complete", data={"next": "/today"}, follow_redirects=False)
    db = TestSession()
    assert db.query(XpEvent).filter(XpEvent.source == "habit").count() == 1
    assert db.query(HabitCompletion).count() == 1
    db.close()


def test_a_completed_habit_reads_as_done_on_today(client):
    c, _ = client
    _make_habit(c, "Heute", weekdays=[app_today().isoweekday()])
    c.post("/habits/1/complete", data={"next": "/today"}, follow_redirects=False)
    assert "erledigt" in c.get("/today").text


@pytest.mark.parametrize("evil", [
    "https://evil.example/x",
    "//evil.example/x",
    "http://evil.example",
    r"/\evil.example",
    "/settings/../../evil",
])
def test_an_external_return_target_is_refused(client, evil):
    c, _ = client
    _make_habit(c, "Heute", weekdays=[app_today().isoweekday()])
    resp = c.post(
        "/habits/1/complete", data={"next": evil}, follow_redirects=False
    )
    assert resp.headers["location"] == "/habits"


def test_safe_next_accepts_only_internal_paths():
    from app.main import safe_next

    assert safe_next("/today", "/habits") == "/today"
    assert safe_next("/week?date=2026-08-03", "/habits") == "/week?date=2026-08-03"
    assert safe_next(None, "/habits") == "/habits"
    assert safe_next("", "/habits") == "/habits"
    assert safe_next("https://evil.example", "/habits") == "/habits"
    assert safe_next("//evil.example", "/habits") == "/habits"


# ---------------------------------------------------------------------------
# Reference date
# ---------------------------------------------------------------------------

def test_an_explicit_date_shows_that_week(client):
    c, _ = client
    html = c.get("/week?date=2026-08-05").text     # a Wednesday
    assert "03.08.2026" in html and "09.08.2026" in html


def test_an_invalid_date_falls_back_with_a_message(client):
    c, _ = client
    resp = c.get("/week?date=quatsch")
    assert resp.status_code == 200
    assert "JJJJ-MM-TT" in resp.text


def test_previous_and_next_week_are_linked(client):
    c, _ = client
    html = c.get("/week?date=2026-08-05").text
    assert "/week?date=2026-07-27" in html
    assert "/week?date=2026-08-10" in html


# ---------------------------------------------------------------------------
# Quests
# ---------------------------------------------------------------------------

def test_a_weekly_quest_shows_progress_and_target(client):
    c, TestSession = client
    db = TestSession()
    db.add(Quest(slug="wq", title="Wochenquest", period="weekly",
                 quest_type="workout_count", target_value=3, current_value=0,
                 active=True, repeatable=True))
    db.commit()
    db.close()
    html = c.get("/week").text
    assert "Wochenquest" in html
    assert "0 / 3" in html


def test_viewing_the_week_never_completes_a_quest(client):
    c, TestSession = client
    db = TestSession()
    db.add(Quest(slug="wq", title="Wochenquest", period="weekly",
                 quest_type="manual", target_value=1, current_value=1,
                 active=True, repeatable=True))
    db.commit()
    db.close()

    c.get("/week")
    c.get("/today")

    from app.models import QuestCompletion

    db = TestSession()
    assert db.query(QuestCompletion).count() == 0
    assert db.query(Quest).one().completed_at is None
    db.close()


# ---------------------------------------------------------------------------
# Pauses
# ---------------------------------------------------------------------------

def test_a_paused_goal_is_marked_neutral_not_failed(client):
    c, TestSession = client
    c.post("/goals/new", data={"title": "Ziel"}, follow_redirects=False)
    _make_habit(c, "Geplant", weekdays=[app_today().isoweekday()])

    from app.models import Goal, Habit

    db = TestSession()
    goal = db.query(Goal).one()
    db.query(Habit).one().goal_id = goal.id
    db.commit()
    db.close()

    c.post("/goals/ziel/status", data={"status": "paused"}, follow_redirects=False)
    html = c.get("/today").text
    assert "pausiert" in html
    for word in ("gescheitert", "Strafe", "verloren"):
        assert word.lower() not in html.lower()


# ---------------------------------------------------------------------------
# Structure and mobile friendliness
# ---------------------------------------------------------------------------

def test_the_week_uses_day_cards_not_a_seven_column_table(client):
    c, _ = client
    html = c.get("/week").text
    assert "week-grid" in html
    assert "<table" not in html


def test_both_views_use_semantic_headings(client):
    c, _ = client
    for path in ("/today", "/week"):
        html = c.get(path).text
        assert "<h1>" in html
        assert "<h2" in html


def test_status_is_stated_in_words_not_only_by_colour(client):
    c, _ = client
    _make_habit(c, "Heute", weekdays=[app_today().isoweekday()])
    html = c.get("/today").text
    assert "offen" in html
