"""Tests for goals: status rules, CRUD, links, and non-destructive archiving."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.goals import (
    GOAL_STATUSES,
    STATUS_ACTIVE,
    STATUS_ARCHIVED,
    STATUS_COMPLETED,
    STATUS_PAUSED,
    can_transition,
    counts_towards_progress,
    create_goal,
    get_by_slug,
    goal_progress,
    habits_for_goal,
    list_goals,
    milestones_for_goal,
    next_milestone,
    quests_for_goal,
    set_status,
    slugify,
    update_goal,
)
from app.models import Base, Habit, HabitCompletion, Quest, XpEvent


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


# ---------------------------------------------------------------------------
# Pure status rules
# ---------------------------------------------------------------------------

def test_pause_and_resume_are_allowed():
    assert can_transition(STATUS_ACTIVE, STATUS_PAUSED)
    assert can_transition(STATUS_PAUSED, STATUS_ACTIVE)


def test_archiving_is_always_reachable():
    for status in (STATUS_ACTIVE, STATUS_PAUSED, STATUS_COMPLETED):
        assert can_transition(status, STATUS_ARCHIVED)


def test_archived_can_be_reactivated():
    assert can_transition(STATUS_ARCHIVED, STATUS_ACTIVE)


def test_same_status_is_idempotent_not_an_error():
    for status in GOAL_STATUSES:
        assert can_transition(status, status)


def test_archived_cannot_jump_straight_to_completed():
    assert not can_transition(STATUS_ARCHIVED, STATUS_COMPLETED)


def test_only_active_goals_are_scored():
    """A pause must never be counted as a failed period."""
    assert counts_towards_progress(STATUS_ACTIVE)
    for status in (STATUS_PAUSED, STATUS_COMPLETED, STATUS_ARCHIVED):
        assert not counts_towards_progress(status)


# ---------------------------------------------------------------------------
# Slugs
# ---------------------------------------------------------------------------

def test_slug_is_readable():
    assert slugify("Weg des Japanischen") == "weg-des-japanischen"


def test_slug_only_suffixes_on_collision():
    existing = {"kraftpfad"}
    assert slugify("Kraftpfad", existing) == "kraftpfad-2"
    assert slugify("Kraftpfad", existing | {"kraftpfad-2"}) == "kraftpfad-3"


def test_slug_survives_a_title_without_letters():
    assert slugify("···") == "goal"


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def test_create_and_fetch(db):
    goal = create_goal(db, title="Kraftpfad", short_label="Training",
                       description="Drei Einheiten pro Woche.")
    assert goal.slug == "kraftpfad"
    assert goal.status == STATUS_ACTIVE
    assert get_by_slug(db, "kraftpfad").id == goal.id


def test_update_keeps_status_and_slug(db):
    goal = create_goal(db, title="Kraftpfad")
    set_status(db, goal, STATUS_PAUSED)
    update_goal(db, goal, title="Kraftpfad neu", description="anders",
                short_label="Kraft", sort_order=3)
    db.refresh(goal)
    assert goal.title == "Kraftpfad neu"
    assert goal.slug == "kraftpfad"        # stable identity
    assert goal.status == STATUS_PAUSED    # editing text must not resume it
    assert goal.sort_order == 3


def test_list_hides_archived_by_default(db):
    create_goal(db, title="Sichtbar")
    archived = create_goal(db, title="Weg")
    set_status(db, archived, STATUS_ARCHIVED)

    assert [g.title for g in list_goals(db)] == ["Sichtbar"]
    assert len(list_goals(db, include_archived=True)) == 2


def test_list_is_ordered_by_sort_order(db):
    create_goal(db, title="B", sort_order=2)
    create_goal(db, title="A", sort_order=1)
    assert [g.title for g in list_goals(db)] == ["A", "B"]


# ---------------------------------------------------------------------------
# Status changes never destroy anything
# ---------------------------------------------------------------------------

def test_pausing_keeps_habits_quests_and_history(db):
    goal = create_goal(db, title="Körperkontrolle")
    habit = Habit(title="Kraft & Lösen", goal_id=goal.id, base_xp_reward=25)
    db.add(habit)
    db.add(Quest(slug="q", title="Rhythmus", goal_id=goal.id, target_value=5))
    db.commit()
    db.add(HabitCompletion(habit_id=habit.id, xp_awarded=25))
    db.add(XpEvent(event_type="habit_complete", source="habit", source_id=str(habit.id),
                   xp=25, attribute="Technique", title="Kraft & Lösen"))
    db.commit()

    assert set_status(db, goal, STATUS_PAUSED)

    db.refresh(goal)
    assert goal.status == STATUS_PAUSED
    assert db.query(Habit).count() == 1
    assert db.query(Quest).count() == 1
    assert db.query(HabitCompletion).count() == 1
    assert db.query(XpEvent).count() == 1
    assert db.query(Habit).one().active is True   # habits are not deactivated


def test_archiving_deletes_nothing(db):
    goal = create_goal(db, title="Altes Ziel")
    habit = Habit(title="H", goal_id=goal.id)
    db.add(habit)
    db.commit()
    db.add(HabitCompletion(habit_id=habit.id, xp_awarded=10))
    db.commit()

    set_status(db, goal, STATUS_ARCHIVED)

    from app.models import Goal
    assert db.query(Goal).count() == 1        # the goal row survives
    assert db.query(Habit).count() == 1
    assert db.query(HabitCompletion).count() == 1


def test_resume_restores_scoring(db):
    goal = create_goal(db, title="Ziel")
    set_status(db, goal, STATUS_PAUSED)
    assert not goal_progress(db, goal)["counts_towards_progress"]
    set_status(db, goal, STATUS_ACTIVE)
    assert goal_progress(db, goal)["counts_towards_progress"]


def test_invalid_transition_is_refused_without_changing_state(db):
    goal = create_goal(db, title="Ziel")
    set_status(db, goal, STATUS_ARCHIVED)
    assert not set_status(db, goal, STATUS_COMPLETED)
    db.refresh(goal)
    assert goal.status == STATUS_ARCHIVED


def test_unknown_status_is_refused(db):
    goal = create_goal(db, title="Ziel")
    assert not set_status(db, goal, "geloescht")
    db.refresh(goal)
    assert goal.status == STATUS_ACTIVE


# ---------------------------------------------------------------------------
# Links between goals, habits, quests and milestones
# ---------------------------------------------------------------------------

def test_habits_and_quests_link_to_a_goal(db):
    goal = create_goal(db, title="Ziel")
    db.add(Habit(title="Zugehörig", goal_id=goal.id))
    db.add(Habit(title="Ohne Ziel"))
    db.add(Quest(slug="a", title="Wochenquest", goal_id=goal.id))
    db.add(Quest(slug="b", title="Fremd"))
    db.commit()

    assert [h.title for h in habits_for_goal(db, goal)] == ["Zugehörig"]
    assert [q.title for q in quests_for_goal(db, goal)] == ["Wochenquest"]


def test_habits_and_quests_without_a_goal_keep_working(db):
    """Existing rows have goal_id NULL and must be unaffected."""
    db.add(Habit(title="Bestand", base_xp_reward=20))
    db.add(Quest(slug="bestand", title="Bestandsquest", target_value=3))
    db.commit()
    assert db.query(Habit).one().goal_id is None
    assert db.query(Quest).one().goal_id is None
    assert db.query(Quest).one().is_milestone is False


def test_milestones_are_separated_from_recurring_quests(db):
    goal = create_goal(db, title="Ziel")
    db.add(Quest(slug="weekly", title="Wöchentlich", goal_id=goal.id, repeatable=True))
    db.add(Quest(slug="m1", title="Erster Zyklus", goal_id=goal.id,
                 is_milestone=True, period="once", sort_order=1))
    db.commit()

    assert [q.title for q in quests_for_goal(db, goal)] == ["Wöchentlich"]
    assert [q.title for q in milestones_for_goal(db, goal)] == ["Erster Zyklus"]


def test_next_milestone_is_the_first_unfinished(db):
    from datetime import datetime

    goal = create_goal(db, title="Ziel")
    db.add(Quest(slug="m1", title="Eins", goal_id=goal.id, is_milestone=True,
                 sort_order=1, completed_at=datetime(2026, 1, 1)))
    db.add(Quest(slug="m2", title="Zwei", goal_id=goal.id, is_milestone=True, sort_order=2))
    db.add(Quest(slug="m3", title="Drei", goal_id=goal.id, is_milestone=True, sort_order=3))
    db.commit()

    assert next_milestone(db, goal).title == "Zwei"


def test_next_milestone_is_none_when_all_done(db):
    from datetime import datetime

    goal = create_goal(db, title="Ziel")
    db.add(Quest(slug="m1", title="Eins", goal_id=goal.id, is_milestone=True,
                 completed_at=datetime(2026, 1, 1)))
    db.commit()
    assert next_milestone(db, goal) is None


def test_progress_counts_milestones(db):
    from datetime import datetime

    goal = create_goal(db, title="Ziel")
    db.add(Quest(slug="m1", title="Eins", goal_id=goal.id, is_milestone=True,
                 completed_at=datetime(2026, 1, 1)))
    db.add(Quest(slug="m2", title="Zwei", goal_id=goal.id, is_milestone=True))
    db.add(Habit(title="H", goal_id=goal.id, active=True))
    db.commit()

    progress = goal_progress(db, goal)
    assert progress["milestones_total"] == 2
    assert progress["milestones_done"] == 1
    assert progress["percent"] == 50
    assert progress["next"].title == "Zwei"
    assert progress["habits"] == 1


def test_progress_without_milestones_is_zero_not_an_error(db):
    goal = create_goal(db, title="Ziel")
    progress = goal_progress(db, goal)
    assert progress["milestones_total"] == 0
    assert progress["percent"] == 0
    assert progress["next"] is None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@pytest.fixture
def client():
    import os

    os.environ.setdefault("WGER_BASE_URL", "https://wger.example.com")
    import app.config as cfg
    cfg._settings = None

    from fastapi.testclient import TestClient

    from app.database import get_db
    from app.main import app
    from app.models import HeroProfile

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


def test_goals_page_renders(client):
    c, _ = client
    resp = c.get("/goals")
    assert resp.status_code == 200
    assert "Ziele" in resp.text


def test_goals_in_navigation(client):
    c, _ = client
    assert 'href="/goals"' in c.get("/").text


def test_create_goal_via_form(client):
    c, Session = client
    resp = c.post(
        "/goals/new",
        data={"title": "Kraftpfad", "short_label": "Training",
              "description": "Drei Einheiten pro Woche.", "sort_order": "1"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/goals/kraftpfad"

    from app.models import Goal
    db = Session()
    goal = db.query(Goal).one()
    assert goal.title == "Kraftpfad"
    assert goal.status == STATUS_ACTIVE
    db.close()


def test_goal_detail_renders(client):
    c, _ = client
    c.post("/goals/new", data={"title": "Kraftpfad"}, follow_redirects=False)
    resp = c.get("/goals/kraftpfad")
    assert resp.status_code == 200
    assert "Kraftpfad" in resp.text


def test_unknown_goal_is_404(client):
    c, _ = client
    assert c.get("/goals/gibt-es-nicht").status_code == 404


def test_pause_and_resume_via_route(client):
    c, Session = client
    c.post("/goals/new", data={"title": "Ziel"}, follow_redirects=False)

    from app.models import Goal

    c.post("/goals/ziel/status", data={"status": "paused"}, follow_redirects=False)
    db = Session()
    assert db.query(Goal).one().status == STATUS_PAUSED
    db.close()

    c.post("/goals/ziel/status", data={"status": "active"}, follow_redirects=False)
    db = Session()
    assert db.query(Goal).one().status == STATUS_ACTIVE
    db.close()


def test_paused_goal_shows_a_neutral_notice(client):
    c, _ = client
    c.post("/goals/new", data={"title": "Ziel"}, follow_redirects=False)
    c.post("/goals/ziel/status", data={"status": "paused"}, follow_redirects=False)
    html = c.get("/goals/ziel").text
    assert "pausiert" in html.lower()
    # no blaming language anywhere
    for word in ("gescheitert", "Strafe", "verloren", "faul", "zerstört"):
        assert word.lower() not in html.lower()


def test_archived_goal_is_hidden_but_not_deleted(client):
    c, Session = client
    c.post("/goals/new", data={"title": "Altes Ziel"}, follow_redirects=False)
    c.post("/goals/altes-ziel/status", data={"status": "archived"}, follow_redirects=False)

    assert "Altes Ziel" not in c.get("/goals").text
    assert "Altes Ziel" in c.get("/goals?archived=1").text

    from app.models import Goal
    db = Session()
    assert db.query(Goal).count() == 1     # still there
    db.close()


def test_invalid_status_change_is_rejected(client):
    c, _ = client
    c.post("/goals/new", data={"title": "Ziel"}, follow_redirects=False)
    resp = c.post("/goals/ziel/status", data={"status": "geloescht"})
    assert resp.status_code == 400


def test_edit_goal_via_form(client):
    c, Session = client
    c.post("/goals/new", data={"title": "Alt"}, follow_redirects=False)
    resp = c.post(
        "/goals/alt/edit",
        data={"title": "Neu", "description": "geändert", "short_label": "N",
              "sort_order": "5"},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    from app.models import Goal
    db = Session()
    goal = db.query(Goal).one()
    assert goal.title == "Neu"
    assert goal.slug == "alt"      # identity is stable
    db.close()


def test_there_is_no_delete_route(client):
    """Goals must not be removable through the UI at all."""
    c, _ = client
    c.post("/goals/new", data={"title": "Ziel"}, follow_redirects=False)
    assert c.post("/goals/ziel/delete", follow_redirects=False).status_code == 404


# ---------------------------------------------------------------------------
# Pause history through the routes
# ---------------------------------------------------------------------------

def test_pause_route_records_an_interval(client):
    c, Session = client
    c.post("/goals/new", data={"title": "Ziel"}, follow_redirects=False)
    c.post("/goals/ziel/status", data={"status": "paused"}, follow_redirects=False)

    from app.models import GoalPauseInterval

    db = Session()
    interval = db.query(GoalPauseInterval).one()
    assert interval.ended_at is None
    db.close()


def test_resume_route_closes_the_interval(client):
    c, Session = client
    c.post("/goals/new", data={"title": "Ziel"}, follow_redirects=False)
    c.post("/goals/ziel/status", data={"status": "paused"}, follow_redirects=False)
    c.post("/goals/ziel/status", data={"status": "active"}, follow_redirects=False)

    from app.models import GoalPauseInterval

    db = Session()
    interval = db.query(GoalPauseInterval).one()
    assert interval.ended_at is not None
    db.close()


def test_no_route_deletes_a_pause_interval(client):
    """Pause history is append-only — there must be no way to remove it."""
    c, Session = client
    c.post("/goals/new", data={"title": "Ziel"}, follow_redirects=False)
    c.post("/goals/ziel/status", data={"status": "paused"}, follow_redirects=False)

    from app.main import app

    paths = {getattr(r, "path", "") for r in app.routes}
    assert not any("pause" in p for p in paths)


def test_goal_detail_lists_recorded_pauses(client):
    c, _ = client
    c.post("/goals/new", data={"title": "Ziel"}, follow_redirects=False)
    c.post("/goals/ziel/status", data={"status": "paused"}, follow_redirects=False)
    html = c.get("/goals/ziel").text
    assert "Erfasste Pausen" in html
    assert "läuft" in html


def test_momentum_explanation_names_the_pause_source(client):
    c, _ = client
    c.post("/goals/new", data={"title": "Ziel"}, follow_redirects=False)
    html = c.get("/goals/ziel").text
    assert "Pausenzeiträume" in html
    assert "nicht rückwirkend" in html
