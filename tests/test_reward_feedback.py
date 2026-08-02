"""The habit completion moment: it must appear exactly once, and only for real.

The effect is server-driven — a confirmed completion leaves one short-lived
signed cookie, and the next rendered page consumes it. Everything worth testing
about "exactly once" is therefore testable without a browser.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import Base, Habit, HabitCompletion, HeroProfile, XpEvent
from app.quests import app_today

SUCCESS_MARKER = "reward-flash"


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

    # https, because the reward cookie is issued with Secure=True by default —
    # an http client would silently never send it back.
    with TestClient(app, base_url="https://testserver") as c:
        yield c, TestSession

    app.dependency_overrides.clear()


def _habit(c, title="Heute", xp="10", active=True):
    data = {"title": title, "recurrence": "daily", "target_count": "1",
            "base_xp_reward": xp, "weekdays": [str(app_today().isoweekday())]}
    if active:
        data["active"] = "on"
    c.post("/habits/new", data=data, follow_redirects=False)


def _complete(c, habit_id=1, target="/today"):
    return c.post(
        f"/habits/{habit_id}/complete", data={"next": target}, follow_redirects=False
    )


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------

def test_a_confirmed_completion_shows_the_reward(client):
    c, _ = client
    _habit(c)
    _complete(c)
    html = c.get("/today").text
    assert SUCCESS_MARKER in html
    assert "Gewohnheit abgeschlossen" in html


def test_the_reward_names_the_habit(client):
    c, _ = client
    _habit(c, "Kontrollsession")
    _complete(c)
    assert "Kontrollsession" in c.get("/today").text


def test_the_reward_is_an_aria_live_region(client):
    c, _ = client
    _habit(c)
    _complete(c)
    html = c.get("/today").text
    assert 'role="status"' in html
    assert 'aria-live="polite"' in html


def test_the_completed_card_is_marked_fresh(client):
    c, _ = client
    _habit(c)
    _complete(c)
    assert "is-fresh" in c.get("/today").text


def test_the_hero_panel_reacts_only_to_a_confirmed_success(client):
    c, _ = client
    _habit(c)
    assert "is-charged" not in c.get("/today").text
    _complete(c)
    assert "is-charged" in c.get("/today").text


def test_the_reward_works_on_the_week_view_too(client):
    c, _ = client
    _habit(c)
    _complete(c, target="/week")
    assert SUCCESS_MARKER in c.get("/week").text


def test_the_reward_works_on_the_habit_list_too(client):
    c, _ = client
    _habit(c)
    _complete(c, target="/habits")
    assert SUCCESS_MARKER in c.get("/habits").text


# ---------------------------------------------------------------------------
# Exactly once
# ---------------------------------------------------------------------------

def test_the_reward_is_consumed_by_the_first_page(client):
    c, _ = client
    _habit(c)
    _complete(c)
    assert SUCCESS_MARKER in c.get("/today").text
    assert SUCCESS_MARKER not in c.get("/today").text


def test_reloading_shows_no_reward(client):
    c, _ = client
    _habit(c)
    _complete(c)
    c.get("/today")
    for _ in range(3):
        assert SUCCESS_MARKER not in c.get("/today").text


def test_reloading_creates_no_second_completion(client):
    c, TestSession = client
    _habit(c)
    _complete(c)
    for _ in range(3):
        c.get("/today")

    db = TestSession()
    assert db.query(HabitCompletion).count() == 1
    db.close()


def test_an_already_completed_habit_is_not_marked_fresh_again(client):
    c, _ = client
    _habit(c)
    _complete(c)
    c.get("/today")
    html = c.get("/today").text
    assert "is-done" in html
    assert "is-fresh" not in html


def test_a_plain_visit_never_shows_a_reward(client):
    c, _ = client
    _habit(c)
    for path in ("/today", "/week", "/habits", "/goals", "/"):
        assert SUCCESS_MARKER not in c.get(path).text


# ---------------------------------------------------------------------------
# Nothing to celebrate
# ---------------------------------------------------------------------------

def test_a_deduplicated_double_click_shows_no_second_reward(client):
    c, TestSession = client
    _habit(c)
    _complete(c)
    c.get("/today")                # consume the first, legitimate reward

    second = _complete(c)          # within the double-click window
    assert second.status_code == 303
    assert SUCCESS_MARKER not in c.get("/today").text

    db = TestSession()
    assert db.query(HabitCompletion).count() == 1
    assert db.query(XpEvent).filter(XpEvent.source == "habit").count() == 1
    db.close()


def test_an_inactive_habit_awards_nothing_and_shows_nothing(client):
    c, TestSession = client
    _habit(c, active=False)
    _complete(c)

    assert SUCCESS_MARKER not in c.get("/today").text
    db = TestSession()
    assert db.query(HabitCompletion).count() == 0
    assert db.query(XpEvent).count() == 0
    db.close()


def test_an_unknown_habit_is_a_404_and_shows_nothing(client):
    c, _ = client
    assert c.post("/habits/999/complete", data={"next": "/today"}).status_code == 404
    assert SUCCESS_MARKER not in c.get("/today").text


def test_no_xp_no_xp_claim(client):
    """A habit worth 0 XP must not produce a "+XP" line.

    The form falls back to the default reward for an empty value, so the 0 is
    written directly — this is about what the flash claims, not about the form.
    """
    c, TestSession = client
    _habit(c, "Ohne XP")
    db = TestSession()
    habit = db.query(Habit).one()
    habit.base_xp_reward = 0
    habit.stat_rewards = None
    db.commit()
    db.close()

    _complete(c)
    html = c.get("/today").text
    assert SUCCESS_MARKER in html
    assert "+0 XP" not in html
    assert "Fortschritt gespeichert" in html


def test_an_awarded_habit_states_the_real_amount(client):
    c, _ = client
    _habit(c, "Mit XP", xp="25")
    _complete(c)
    assert "+25 XP" in c.get("/today").text


# ---------------------------------------------------------------------------
# Auth and CSRF
# ---------------------------------------------------------------------------

def test_a_csrf_rejection_shows_no_reward(secure_reward_client):
    """A rejected POST never reaches the route, so nothing is ever set."""
    from app.auth import CSRF_FIELD

    client, token = secure_reward_client
    client.post(
        "/habits/new",
        data={"title": "Heute", "active": "on", "recurrence": "daily",
              "target_count": "1", "base_xp_reward": "10",
              "weekdays": [str(app_today().isoweekday())], CSRF_FIELD: token},
        follow_redirects=False,
    )

    bad = client.post("/habits/1/complete", data={"next": "/today"})
    assert bad.status_code == 403
    page = client.get("/today")
    assert SUCCESS_MARKER not in page.text

    # Re-read the token: the rejected request may have re-issued the session.
    marker = f'name="{CSRF_FIELD}" value="'
    start = page.text.index(marker) + len(marker)
    fresh = page.text[start : page.text.index('"', start)]

    ok = client.post(
        "/habits/1/complete",
        data={"next": "/today", CSRF_FIELD: fresh},
        follow_redirects=False,
    )
    assert ok.status_code == 303
    assert SUCCESS_MARKER in client.get("/today").text


@pytest.fixture
def secure_reward_client(tmp_path, monkeypatch):
    """Auth switched on, already logged in, with a usable CSRF token."""
    from app.auth import CSRF_FIELD, hash_password

    hash_file = tmp_path / "hash"
    hash_file.write_text(hash_password("unit-test-passwort"))
    secret_file = tmp_path / "secret"
    secret_file.write_text("unit-test-session-secret")

    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("AUTH_PASSWORD_HASH_FILE", str(hash_file))
    monkeypatch.setenv("SESSION_SECRET_FILE", str(secret_file))
    monkeypatch.setenv("COOKIE_SECURE", "true")
    monkeypatch.setenv("WGER_BASE_URL", "https://wger.example.com")

    import app.config as cfg

    cfg._settings = None

    from fastapi.testclient import TestClient

    from app.database import get_db
    from app.main import _login_limiter, app

    _login_limiter._failures.clear()

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

    with TestClient(app, base_url="https://testserver") as client:
        page = client.get("/login")
        marker = f'name="{CSRF_FIELD}" value="'
        start = page.text.index(marker) + len(marker)
        token = page.text[start : page.text.index('"', start)]
        assert client.post(
            "/login",
            data={"password": "unit-test-passwort", CSRF_FIELD: token},
            follow_redirects=False,
        ).status_code == 303

        # Logging in re-issues the session, so the token from the login page is
        # already stale. Read the current one from an authenticated page.
        page = client.get("/habits/new")
        start = page.text.index(marker) + len(marker)
        yield client, page.text[start : page.text.index('"', start)]

    app.dependency_overrides.clear()
    cfg._settings = None
