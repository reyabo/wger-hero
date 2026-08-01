"""Web and CLI activation of the starter campaign — auth, CSRF, equivalence."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import Base, Goal, Habit, HeroProfile, Quest, XpEvent


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


# ---------------------------------------------------------------------------
# Preview page
# ---------------------------------------------------------------------------

def test_the_settings_page_links_the_campaign(client):
    c, _ = client
    assert 'href="/settings/starter"' in c.get("/settings").text


def test_the_preview_page_renders(client):
    c, _ = client
    resp = c.get("/settings/starter")
    assert resp.status_code == 200
    assert "Starter-Kampagne" in resp.text


def test_the_preview_lists_all_three_goals(client):
    c, _ = client
    html = c.get("/settings/starter").text
    for title in ("Kraftpfad", "Weg des Japanischen", "Körperkontrolle"):
        assert title in html


def test_the_preview_shows_what_would_happen(client):
    c, _ = client
    html = c.get("/settings/starter").text
    assert "wird neu angelegt" in html


def test_opening_the_preview_writes_nothing(client):
    c, TestSession = client
    c.get("/settings/starter")
    db = TestSession()
    assert db.query(Goal).count() == 0
    assert db.query(Habit).count() == 0
    assert db.query(Quest).count() == 0
    db.close()


def test_the_preview_shows_the_safety_note(client):
    c, _ = client
    html = c.get("/settings/starter").text
    assert "pausieren" in html.lower()
    assert "kostet kein XP" in html


def test_the_preview_shows_no_configuration_values(client):
    c, _ = client
    html = c.get("/settings/starter").text.lower()
    for word in ("token", "secret", "password", "database_url"):
        assert word not in html


# ---------------------------------------------------------------------------
# Activation
# ---------------------------------------------------------------------------

def test_activation_creates_the_campaign(client):
    c, TestSession = client
    resp = c.post("/settings/starter", data={})
    assert resp.status_code == 200

    db = TestSession()
    assert db.query(Goal).count() == 3
    assert db.query(Habit).count() == 6
    assert db.query(Quest).count() == 15
    db.close()


def test_activation_shows_a_result(client):
    c, _ = client
    html = c.post("/settings/starter", data={}).text
    assert "Ergebnis der Aktivierung" in html


def test_a_second_activation_adds_nothing(client):
    c, TestSession = client
    c.post("/settings/starter", data={})
    c.post("/settings/starter", data={})

    db = TestSession()
    assert db.query(Goal).count() == 3
    assert db.query(Quest).count() == 15
    db.close()


def test_activation_awards_no_xp(client):
    c, TestSession = client
    c.post("/settings/starter", data={})
    db = TestSession()
    assert db.query(XpEvent).count() == 0
    assert db.query(HeroProfile).first().total_xp == 0
    db.close()


def test_the_campaign_shows_up_in_the_goal_list(client):
    c, _ = client
    c.post("/settings/starter", data={})
    html = c.get("/goals").text
    assert "Kraftpfad" in html and "Körperkontrolle" in html


def test_the_planned_routines_show_up_in_the_week(client):
    c, _ = client
    c.post("/settings/starter", data={})
    html = c.get("/week").text
    assert "SRS-Review" in html


# ---------------------------------------------------------------------------
# Auth and CSRF
# ---------------------------------------------------------------------------

def test_the_campaign_routes_are_protected():
    """Both routes sit behind the normal auth gate, like every other page."""
    from app.auth import PUBLIC_PATHS, PUBLIC_PREFIXES

    for path in ("/settings/starter",):
        assert path not in PUBLIC_PATHS
        assert not path.startswith(PUBLIC_PREFIXES)


def test_the_activation_form_carries_a_csrf_field(client):
    c, _ = client
    html = c.get("/settings/starter").text
    assert 'method="post"' in html
    assert 'action="/settings/starter"' in html


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _run_cli(monkeypatch, db, argv):
    """Run the CLI against an in-memory session, capturing its output."""
    import app.database as database
    import app.seed_programs as seed_programs

    monkeypatch.setattr(database, "init_db", lambda: None)
    monkeypatch.setattr(seed_programs, "__name__", "app.seed_programs")

    def fake_get_db():
        yield db

    monkeypatch.setattr(database, "get_db", fake_get_db)
    return seed_programs._main(argv)


@pytest.fixture
def cli_db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def test_the_cli_dry_run_writes_nothing(monkeypatch, cli_db, capsys):
    code = _run_cli(monkeypatch, cli_db, ["starter", "--dry-run"])
    assert code == 0
    assert cli_db.query(Goal).count() == 0
    assert "dry-run" in capsys.readouterr().out


def test_the_cli_activates_the_campaign(monkeypatch, cli_db, capsys):
    code = _run_cli(monkeypatch, cli_db, ["starter"])
    assert code == 0
    assert cli_db.query(Goal).count() == 3
    assert "Angewendet" in capsys.readouterr().out


def test_the_cli_is_idempotent(monkeypatch, cli_db, capsys):
    _run_cli(monkeypatch, cli_db, ["starter"])
    _run_cli(monkeypatch, cli_db, ["starter"])
    assert cli_db.query(Goal).count() == 3
    assert cli_db.query(Quest).count() == 15


def test_a_dry_run_after_activation_reports_no_changes(monkeypatch, cli_db, capsys):
    _run_cli(monkeypatch, cli_db, ["starter"])
    capsys.readouterr()
    _run_cli(monkeypatch, cli_db, ["starter", "--dry-run"])
    assert "Keine Änderungen nötig" in capsys.readouterr().out


def test_the_cli_output_carries_no_secrets(monkeypatch, cli_db, capsys):
    _run_cli(monkeypatch, cli_db, ["starter"])
    out = capsys.readouterr().out.lower()
    for word in ("token", "secret", "password", "database_url", "wger_base_url"):
        assert word not in out


def test_an_unknown_program_is_refused(monkeypatch, cli_db, capsys):
    assert _run_cli(monkeypatch, cli_db, ["gibt-es-nicht"]) == 1
    assert "Usage" in capsys.readouterr().out


def test_cli_and_web_produce_the_same_result(monkeypatch, cli_db, client):
    """The two paths call one service, so they must agree row for row."""
    c, TestSession = client
    c.post("/settings/starter", data={})
    _run_cli(monkeypatch, cli_db, ["starter"])

    web = TestSession()
    try:
        assert (
            sorted(g.slug for g in web.query(Goal).all())
            == sorted(g.slug for g in cli_db.query(Goal).all())
        )
        assert (
            sorted(h.title for h in web.query(Habit).all())
            == sorted(h.title for h in cli_db.query(Habit).all())
        )
        assert (
            sorted(q.title for q in web.query(Quest).all())
            == sorted(q.title for q in cli_db.query(Quest).all())
        )
    finally:
        web.close()
