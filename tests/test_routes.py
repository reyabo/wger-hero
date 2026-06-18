"""Route-level smoke tests using FastAPI TestClient."""

import os

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
