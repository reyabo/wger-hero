"""Route-level smoke tests using FastAPI TestClient."""

import os
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import get_db
from app.models import Base

# Ensure env vars are set before the app module is imported
os.environ.setdefault("WGER_BASE_URL", "https://wger.example.com")
os.environ.setdefault("WGER_API_TOKEN", "test-token-for-routes")


@pytest.fixture(scope="module")
def client():
    import app.config as cfg
    cfg._settings = None  # reset singleton so env vars take effect

    from app.main import app

    # StaticPool keeps a single connection shared across threads,
    # which is required for in-memory SQLite with TestClient.
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine)

    def override_db():
        db = TestSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_db

    with TestClient(app, raise_server_exceptions=True) as c:
        yield c

    app.dependency_overrides.clear()


def test_healthz(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_dashboard_returns_200(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "wger-hero" in resp.text


def test_quests_returns_200(client):
    resp = client.get("/quests")
    assert resp.status_code == 200
    assert "Quest" in resp.text


def test_achievements_returns_200(client):
    resp = client.get("/achievements")
    assert resp.status_code == 200
    assert "Achievement" in resp.text


def test_settings_returns_200(client):
    resp = client.get("/settings")
    assert resp.status_code == 200
    assert "WGER_BASE_URL" in resp.text


def test_settings_does_not_expose_token(client):
    resp = client.get("/settings")
    assert "test-token-for-routes" not in resp.text


def test_habits_page_renders(client):
    resp = client.get("/habits")
    assert resp.status_code == 200
    assert "Habits" in resp.text


def test_habit_new_form_renders(client):
    resp = client.get("/habits/new")
    assert resp.status_code == 200
    assert "New Habit" in resp.text


def test_quest_new_form_renders(client):
    resp = client.get("/quests/new")
    assert resp.status_code == 200
    assert "New Quest" in resp.text


def test_create_habit_via_form(client):
    resp = client.post(
        "/habits/new",
        data={
            "title": "Read for 30 minutes",
            "description": "",
            "active": "on",
            "recurrence": "daily",
            "target_count": "1",
            "base_xp_reward": "20",
            "stat_knowledge": "10",
        },
    )
    assert resp.status_code == 200  # followed redirect to /habits
    assert "Read for 30 minutes" in resp.text


def test_complete_habit_via_form(client):
    # Create a uniquely named habit, then complete it via its form action.
    client.post(
        "/habits/new",
        data={
            "title": "Mobility routine",
            "active": "on",
            "recurrence": "daily",
            "target_count": "1",
            "base_xp_reward": "15",
        },
    )
    page = client.get("/habits").text
    ids = re.findall(r"/habits/(\d+)/complete", page)
    assert ids, "expected at least one completable habit"
    habit_id = ids[-1]

    resp = client.post(f"/habits/{habit_id}/complete")
    assert resp.status_code == 200  # followed redirect to /habits
    # Completion count is now reflected on the page.
    assert "completed" in resp.text.lower()


def test_create_manual_quest_via_form(client):
    resp = client.post(
        "/quests/new",
        data={
            "title": "Finish theater block",
            "quest_type": "manual",
            "period": "once",
            "target_value": "1",
            "xp_reward": "150",
            "active": "on",
            "stat_creativity": "20",
        },
    )
    assert resp.status_code == 200
    assert "Finish theater block" in resp.text


def test_habit_edit_form_renders(client):
    page = client.get("/habits").text
    ids = re.findall(r"/habits/(\d+)/edit", page)
    assert ids
    resp = client.get(f"/habits/{ids[0]}/edit")
    assert resp.status_code == 200
    assert "Edit Habit" in resp.text


def test_missing_habit_returns_404(client):
    resp = client.get("/habits/999999/edit")
    assert resp.status_code == 404
