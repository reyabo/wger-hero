"""The FitTrackee settings page: CSRF, secrecy, and the order of operations.

The page is the only place a user drives this integration from, so it is also
the place where a secret would leak if one were going to. Several tests here do
nothing but assert that a token cannot appear in a rendered page.
"""

import os
import re

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import (
    Base,
    FitTrackeeConnection,
    FitTrackeeSport,
    FitTrackeeWorkout,
    Goal,
    HeroProfile,
    Quest,
)

MUTATING_ROUTES = [
    "/settings/fittrackee/connect",
    "/settings/fittrackee/sports",
    "/settings/fittrackee/sports/save",
    "/settings/fittrackee/baseline",
    "/settings/fittrackee/sync",
    "/settings/fittrackee/goal",
]


@pytest.fixture
def session_factory():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    seed = factory()
    seed.add(HeroProfile(name="Hero", level=1, total_xp=0))
    seed.commit()
    seed.close()
    return factory


@pytest.fixture
def client(session_factory, tmp_path):
    os.environ.setdefault("WGER_BASE_URL", "https://wger.example.com")
    os.environ.setdefault("WGER_API_TOKEN", "test-token-for-fittrackee")

    import app.config as cfg

    cfg._settings = None

    from fastapi.testclient import TestClient

    from app.database import get_db
    from app.main import app

    def override_db():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# The page renders in every state
# ---------------------------------------------------------------------------

def test_the_page_renders_when_nothing_is_configured(client):
    resp = client.get("/settings/fittrackee")
    assert resp.status_code == 200
    assert "FitTrackee" in resp.text


def test_it_says_it_is_not_configured(client):
    page = client.get("/settings/fittrackee").text
    assert "nicht verbunden" in page


def test_it_shows_the_setup_order(client):
    """The order matters: no reward before the baseline."""
    page = client.get("/settings/fittrackee").text
    assert "Baseline erstellen" in page
    assert page.index("Sportarten") < page.index("ab jetzt normale Synchronisation")


def test_the_settings_page_links_to_it(client):
    assert "/settings/fittrackee" in client.get("/settings").text


def test_it_states_the_ten_minute_rule(client):
    assert "10 Minuten" in client.get("/settings/fittrackee").text


def test_it_states_the_flat_award(client):
    page = client.get("/settings/fittrackee").text
    assert "40 globale XP" in page and "40 Ausdauer-XP" in page


def test_it_says_heart_rate_is_not_scored(client):
    page = client.get("/settings/fittrackee").text
    assert "nie zur XP-Berechnung" in page


def test_it_says_synchronisation_is_manual(client):
    assert "keinen Hintergrundjob" in client.get("/settings/fittrackee").text


# ---------------------------------------------------------------------------
# Nothing secret is ever rendered
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "secret", ["access_token", "refresh_token", "client_secret", "code_verifier"]
)
def test_no_secret_field_name_is_rendered(client, secret):
    assert secret not in client.get("/settings/fittrackee").text


def test_a_stored_token_never_reaches_the_page(client, tmp_path, monkeypatch):
    from app.config import get_settings
    from app.fittrackee_oauth import TokenSet, TokenStore

    settings = get_settings()
    monkeypatch.setattr(settings, "FITTRACKEE_TOKEN_DIR", str(tmp_path / "oauth"))
    TokenStore(tmp_path / "oauth").save(
        TokenSet("super-secret-access", "super-secret-refresh")
    )

    page = client.get("/settings/fittrackee").text
    assert "super-secret-access" not in page
    assert "super-secret-refresh" not in page


def test_the_page_says_it_hides_secrets(client):
    assert "grundsätzlich nicht angezeigt" in client.get("/settings/fittrackee").text


# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("route", MUTATING_ROUTES)
def test_every_mutating_form_emits_a_csrf_field(route):
    """Checked in the template, not the rendered page.

    The test suite runs with AUTH_ENABLED=false, where csrf_input() correctly
    renders nothing — so a rendered page could not tell a protected form from
    an unprotected one. The template is where the guarantee actually lives.
    """
    from pathlib import Path

    template = (
        Path(__file__).resolve().parents[1]
        / "app" / "templates" / "fittrackee.html"
    ).read_text()

    forms = re.findall(
        r"<form[^>]*action=\"([^\"]+)\"[^>]*>(.*?)</form>", template, re.S
    )
    bodies = [body for action, body in forms if action == route]
    assert bodies, f"no form posts to {route}"
    for body in bodies:
        assert "csrf_input(request)" in body


def test_every_form_on_the_page_posts():
    """A GET form would bypass the CSRF gate entirely."""
    from pathlib import Path

    template = (
        Path(__file__).resolve().parents[1]
        / "app" / "templates" / "fittrackee.html"
    ).read_text()
    for tag in re.findall(r"<form[^>]*>", template):
        assert 'method="POST"' in tag, tag


@pytest.mark.parametrize("route", MUTATING_ROUTES)
def test_no_mutating_route_answers_get(client, route):
    assert client.get(route).status_code == 405


# ---------------------------------------------------------------------------
# The order of operations is enforced, not just documented
# ---------------------------------------------------------------------------

def test_a_sync_before_the_baseline_is_refused(client):
    resp = client.post("/settings/fittrackee/sync")
    assert resp.status_code == 200
    assert "Zuerst die Baseline" in resp.text


def test_a_second_baseline_is_refused(client, session_factory):
    from datetime import datetime

    db = session_factory()
    db.add(FitTrackeeConnection(baseline_at=datetime(2026, 8, 18, 12, 0)))
    db.commit()
    db.close()

    resp = client.post("/settings/fittrackee/baseline")
    assert "bereits eine Baseline" in resp.text


def test_connecting_without_configuration_explains_itself(client):
    resp = client.post("/settings/fittrackee/connect", follow_redirects=False)
    assert resp.status_code == 200
    assert "nicht konfiguriert" in resp.text


# ---------------------------------------------------------------------------
# The sport selection
# ---------------------------------------------------------------------------

def test_saving_the_sport_selection_enables_only_what_was_ticked(client, session_factory):
    db = session_factory()
    db.add(FitTrackeeSport(sport_id=1, label="Cycling"))
    db.add(FitTrackeeSport(sport_id=5, label="Running"))
    db.commit()
    db.close()

    client.post("/settings/fittrackee/sports/save", data={"endurance_sports": ["5"]})

    db = session_factory()
    enabled = {
        s.sport_id for s in db.query(FitTrackeeSport).all() if s.counts_for_endurance
    }
    db.close()
    assert enabled == {5}


def test_unticking_everything_disables_everything(client, session_factory):
    db = session_factory()
    db.add(FitTrackeeSport(sport_id=5, label="Running", counts_for_endurance=True))
    db.commit()
    db.close()

    client.post("/settings/fittrackee/sports/save", data={})

    db = session_factory()
    assert not db.query(FitTrackeeSport).one().counts_for_endurance
    db.close()


def test_a_junk_sport_id_is_ignored(client, session_factory):
    db = session_factory()
    db.add(FitTrackeeSport(sport_id=5, label="Running"))
    db.commit()
    db.close()

    resp = client.post(
        "/settings/fittrackee/sports/save", data={"endurance_sports": ["nope"]}
    )
    assert resp.status_code == 200


def test_the_sport_list_is_shown(client, session_factory):
    db = session_factory()
    db.add(FitTrackeeSport(sport_id=5, label="Trail Running"))
    db.commit()
    db.close()

    assert "Trail Running" in client.get("/settings/fittrackee").text


# ---------------------------------------------------------------------------
# The goal programme
# ---------------------------------------------------------------------------

def test_creating_the_goal_is_idempotent_through_the_route(client, session_factory):
    from app.endurance_program import ALL_QUESTS, GOAL_SLUG

    client.post("/settings/fittrackee/goal")
    client.post("/settings/fittrackee/goal")

    db = session_factory()
    assert db.query(Goal).filter(Goal.slug == GOAL_SLUG).count() == 1
    assert db.query(Quest).count() == len(ALL_QUESTS)
    db.close()


def test_the_second_creation_says_so(client):
    client.post("/settings/fittrackee/goal")
    resp = client.post("/settings/fittrackee/goal")
    assert "bereits vollständig vorhanden" in resp.text


def test_creating_the_goal_awards_no_xp(client, session_factory):
    from app.models import XpEvent

    client.post("/settings/fittrackee/goal")

    db = session_factory()
    assert db.query(XpEvent).count() == 0
    assert db.query(HeroProfile).one().total_xp == 0
    db.close()
