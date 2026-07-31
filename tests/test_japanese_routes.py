"""Route-level tests for the Japanese SAVE import screens."""

import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import get_db
from app.models import Base, HeroProfile, JapaneseSaveImport, XpEvent

from tests.test_japanese_saves import VALID_SAVE

os.environ.setdefault("WGER_BASE_URL", "https://wger.example.com")
os.environ.setdefault("WGER_API_TOKEN", "test-token-for-japanese")


def _save_text(*, level=2, xp=433, day="2026-07-31", session_xp=None) -> str:
    text = VALID_SAVE.replace(
        "Charakter: Lv 2 (見習い) | 433 / 1000 XP",
        f"Charakter: Lv {level} (見習い) | {xp} / 1000 XP",
    ).replace("Datum: 2026-07-31", f"Datum: {day}")
    if session_xp is not None:
        text = text.replace("=== END SAVE ===", f"Session-XP: {session_xp}\n=== END SAVE ===")
    return text


@pytest.fixture
def env():
    """TestClient + a session onto the same in-memory DB."""
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

    with TestClient(app) as client:
        yield client, TestSession

    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# GET /japanese and navigation
# ---------------------------------------------------------------------------

def test_japanese_page_renders(env):
    client, _ = env
    resp = client.get("/japanese")
    assert resp.status_code == 200
    assert "SAVE" in resp.text
    assert "raw_save" in resp.text


def test_navigation_contains_japanese(env):
    client, _ = env
    resp = client.get("/")
    assert resp.status_code == 200
    assert 'href="/japanese"' in resp.text
    assert "Japanisch" in resp.text


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------

def test_preview_stores_nothing(env):
    client, Session = env
    resp = client.post("/japanese/preview", data={"raw_save": VALID_SAVE})
    assert resp.status_code == 200
    assert "baseline" in resp.text

    db = Session()
    assert db.query(JapaneseSaveImport).count() == 0
    assert db.query(XpEvent).count() == 0
    db.close()


def test_preview_shows_delta(env):
    client, _ = env
    client.post("/japanese/import", data={"raw_save": _save_text(xp=373, day="2026-07-30")})
    resp = client.post("/japanese/preview", data={"raw_save": _save_text(xp=433)})
    assert resp.status_code == 200
    assert "+60" in resp.text


def test_preview_invalid_input_shows_german_error(env):
    client, _ = env
    resp = client.post("/japanese/preview", data={"raw_save": "kein save"})
    assert resp.status_code == 400
    assert "Startmarker" in resp.text


def test_preview_flags_duplicate(env):
    client, _ = env
    client.post("/japanese/import", data={"raw_save": VALID_SAVE})
    resp = client.post("/japanese/preview", data={"raw_save": VALID_SAVE})
    assert resp.status_code == 200
    assert "bereits importiert" in resp.text


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

def test_import_redirects_to_detail(env):
    client, Session = env
    resp = client.post(
        "/japanese/import", data={"raw_save": VALID_SAVE}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/japanese/imports/")

    db = Session()
    assert db.query(JapaneseSaveImport).count() == 1
    db.close()


def test_import_baseline_awards_no_xp(env):
    client, Session = env
    client.post("/japanese/import", data={"raw_save": VALID_SAVE})
    db = Session()
    assert db.query(HeroProfile).first().total_xp == 0
    assert db.query(XpEvent).count() == 0
    db.close()


def test_import_baseline_credit_opt_in(env):
    client, Session = env
    client.post(
        "/japanese/import",
        data={"raw_save": VALID_SAVE, "accept_baseline_credit": "1"},
    )
    db = Session()
    assert db.query(HeroProfile).first().total_xp == 433
    db.close()


def test_import_recomputes_server_side(env):
    """A forged XP field in the form must be ignored."""
    client, Session = env
    client.post("/japanese/import", data={"raw_save": _save_text(xp=373, day="2026-07-30")})
    client.post(
        "/japanese/import",
        data={"raw_save": _save_text(xp=433), "xp_delta": "99999"},
    )
    db = Session()
    assert db.query(HeroProfile).first().total_xp == 60
    db.close()


def test_import_duplicate_is_idempotent(env):
    client, Session = env
    client.post("/japanese/import", data={"raw_save": VALID_SAVE})
    resp = client.post(
        "/japanese/import", data={"raw_save": VALID_SAVE}, follow_redirects=False
    )
    assert resp.status_code == 303

    db = Session()
    assert db.query(JapaneseSaveImport).count() == 1
    assert db.query(XpEvent).count() == 0
    db.close()


def test_import_invalid_input_shows_german_error(env):
    client, Session = env
    resp = client.post("/japanese/import", data={"raw_save": "=== 状態 SAVE ===\nMüll"})
    assert resp.status_code == 400
    db = Session()
    assert db.query(JapaneseSaveImport).count() == 0
    db.close()


# ---------------------------------------------------------------------------
# Detail view
# ---------------------------------------------------------------------------

def test_detail_view_shows_snapshot(env):
    client, _ = env
    resp = client.post(
        "/japanese/import", data={"raw_save": VALID_SAVE}, follow_redirects=True
    )
    assert resp.status_code == 200
    assert "これ" in resp.text          # grammar point
    assert "180" in resp.text            # vocabulary score
    assert "Partikel-Golem" in resp.text  # daily quest
    assert "=== 状態 SAVE ===" in resp.text  # raw text


def test_detail_view_404_for_unknown_id(env):
    client, _ = env
    assert client.get("/japanese/imports/9999").status_code == 404


# ---------------------------------------------------------------------------
# Dashboard card
# ---------------------------------------------------------------------------

def test_dashboard_without_import(env):
    client, _ = env
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Noch kein SAVE importiert" in resp.text


def test_dashboard_with_import(env):
    client, _ = env
    client.post("/japanese/import", data={"raw_save": VALID_SAVE})
    resp = client.get("/")
    assert resp.status_code == 200
    assert "31.07.2026" in resp.text  # last SAVE date
    assert "N5" in resp.text           # bunpro level
    assert "これ" in resp.text         # grammar point
