"""Tests for single-user access protection, sessions and CSRF."""

import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import (
    CSRF_FIELD,
    SESSION_COOKIE,
    AuthConfigError,
    LoginRateLimiter,
    csrf_ok,
    deserialize_session,
    hash_password,
    is_public_path,
    is_state_changing,
    load_password_hash,
    new_session,
    serialize_session,
    verify_password,
)
from app.database import get_db
from app.models import Base, HeroProfile

PASSWORD = "correct horse battery staple"


# ---------------------------------------------------------------------------
# Password hashing — no plaintext anywhere
# ---------------------------------------------------------------------------

def test_argon2_hash_round_trip():
    stored = hash_password(PASSWORD)
    assert verify_password(PASSWORD, stored)


def test_wrong_password_is_rejected():
    stored = hash_password(PASSWORD)
    assert not verify_password("something else", stored)


def test_verification_never_raises_on_garbage():
    assert not verify_password(PASSWORD, "not-a-hash")
    assert not verify_password("", "")


def test_hash_does_not_contain_the_password():
    assert PASSWORD not in hash_password(PASSWORD)


def test_missing_secret_file_is_a_clear_error(tmp_path):
    class S:
        AUTH_PASSWORD_HASH_FILE = str(tmp_path / "nope")

    with pytest.raises(AuthConfigError):
        load_password_hash(S())


def test_empty_secret_file_is_rejected(tmp_path):
    path = tmp_path / "empty"
    path.write_text("   ")

    class S:
        AUTH_PASSWORD_HASH_FILE = str(path)

    with pytest.raises(AuthConfigError):
        load_password_hash(S())


# ---------------------------------------------------------------------------
# Session signing
# ---------------------------------------------------------------------------

def test_session_round_trip():
    session = new_session(authenticated=True)
    raw = serialize_session(session, "secret-key")
    loaded = deserialize_session(raw, "secret-key", 3600)
    assert loaded.authenticated is True
    assert loaded.csrf == session.csrf


def test_tampered_session_is_rejected():
    raw = serialize_session(new_session(authenticated=True), "secret-key")
    assert deserialize_session(raw + "x", "secret-key", 3600) is None


def test_session_signed_with_another_key_is_rejected():
    raw = serialize_session(new_session(authenticated=True), "key-a")
    assert deserialize_session(raw, "key-b", 3600) is None


def test_expired_session_is_rejected():
    raw = serialize_session(new_session(authenticated=True), "secret-key")
    time.sleep(2.1)   # itsdangerous compares whole seconds
    assert deserialize_session(raw, "secret-key", 1) is None


def test_absent_session_is_none():
    assert deserialize_session(None, "secret-key", 3600) is None
    assert deserialize_session("", "secret-key", 3600) is None


# ---------------------------------------------------------------------------
# CSRF helper
# ---------------------------------------------------------------------------

def test_csrf_matches_only_the_bound_token():
    session = new_session()
    assert csrf_ok(session, session.csrf)
    assert not csrf_ok(session, "other")
    assert not csrf_ok(session, None)
    assert not csrf_ok(None, session.csrf)


def test_public_paths():
    for path in ("/healthz", "/login", "/manifest.webmanifest", "/sw.js", "/offline"):
        assert is_public_path(path)
    assert is_public_path("/static/style.css")
    assert not is_public_path("/")
    assert not is_public_path("/habits")


def test_state_changing_methods():
    for method in ("POST", "put", "PATCH", "delete"):
        assert is_state_changing(method)
    for method in ("GET", "HEAD", "options"):
        assert not is_state_changing(method)


# ---------------------------------------------------------------------------
# Login rate limiting
# ---------------------------------------------------------------------------

def test_limiter_blocks_after_max_attempts():
    limiter = LoginRateLimiter(max_attempts=3, window_seconds=60)
    for _ in range(3):
        assert not limiter.is_blocked("ip", now=100)
        limiter.record_failure("ip", now=100)
    assert limiter.is_blocked("ip", now=100)


def test_limiter_window_expires():
    limiter = LoginRateLimiter(max_attempts=2, window_seconds=60)
    limiter.record_failure("ip", now=100)
    limiter.record_failure("ip", now=100)
    assert limiter.is_blocked("ip", now=100)
    assert not limiter.is_blocked("ip", now=200)


def test_limiter_reset_on_success():
    limiter = LoginRateLimiter(max_attempts=2, window_seconds=60)
    limiter.record_failure("ip", now=100)
    limiter.record_failure("ip", now=100)
    limiter.reset("ip")
    assert not limiter.is_blocked("ip", now=100)


def test_limiter_is_per_key():
    limiter = LoginRateLimiter(max_attempts=1, window_seconds=60)
    limiter.record_failure("a", now=100)
    assert limiter.is_blocked("a", now=100)
    assert not limiter.is_blocked("b", now=100)


# ---------------------------------------------------------------------------
# Routes with auth ENABLED
# ---------------------------------------------------------------------------

@pytest.fixture
def secure_client(tmp_path, monkeypatch):
    """TestClient with auth switched on and real secret files."""
    hash_file = tmp_path / "hash"
    hash_file.write_text(hash_password(PASSWORD))
    secret_file = tmp_path / "secret"
    secret_file.write_text("unit-test-session-secret")

    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("AUTH_PASSWORD_HASH_FILE", str(hash_file))
    monkeypatch.setenv("SESSION_SECRET_FILE", str(secret_file))
    monkeypatch.setenv("COOKIE_SECURE", "true")
    monkeypatch.setenv("WGER_BASE_URL", "https://wger.example.com")

    import app.config as cfg
    cfg._settings = None

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

    # base_url must be https: the session cookie is issued with Secure=True, and
    # an http client would silently never send it back.
    with TestClient(app, base_url="https://testserver") as client:
        yield client

    app.dependency_overrides.clear()
    cfg._settings = None


def _login(client) -> None:
    page = client.get("/login")
    token = _token_from(page.text)
    resp = client.post(
        "/login", data={"password": PASSWORD, CSRF_FIELD: token}, follow_redirects=False
    )
    assert resp.status_code == 303, resp.text


def _token_from(html: str) -> str:
    marker = f'name="{CSRF_FIELD}" value="'
    start = html.index(marker) + len(marker)
    return html[start : html.index('"', start)]


def test_healthz_is_public(secure_client):
    assert secure_client.get("/healthz").status_code == 200


def test_login_page_is_public(secure_client):
    assert secure_client.get("/login").status_code == 200


def test_protected_page_redirects_to_login(secure_client):
    resp = secure_client.get("/", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_protected_pages_all_redirect(secure_client):
    for path in ("/habits", "/quests", "/stats", "/japanese", "/settings",
                 "/achievements", "/today", "/week"):
        resp = secure_client.get(path, follow_redirects=False)
        assert resp.status_code == 303, f"{path} was reachable without a session"


def test_successful_login_grants_access(secure_client):
    _login(secure_client)
    assert secure_client.get("/", follow_redirects=False).status_code == 200


def test_wrong_password_is_generic_and_denies(secure_client):
    page = secure_client.get("/login")
    resp = secure_client.post(
        "/login",
        data={"password": "wrong", CSRF_FIELD: _token_from(page.text)},
    )
    assert resp.status_code == 401
    assert "fehlgeschlagen" in resp.text
    # must not hint at the cause, and must never echo the attempt
    assert "wrong" not in resp.text
    assert secure_client.get("/", follow_redirects=False).status_code == 303


def test_logout_ends_the_session(secure_client):
    _login(secure_client)
    page = secure_client.get("/")
    resp = secure_client.post(
        "/logout", data={CSRF_FIELD: _token_from(page.text)}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert secure_client.get("/", follow_redirects=False).status_code == 303


def test_logout_is_not_available_via_get(secure_client):
    _login(secure_client)
    assert secure_client.get("/logout", follow_redirects=False).status_code == 405


def test_tampered_cookie_denies_access(secure_client):
    _login(secure_client)
    secure_client.cookies.set(SESSION_COOKIE, "forged-value")
    assert secure_client.get("/", follow_redirects=False).status_code == 303


def test_cookie_flags(secure_client):
    page = secure_client.get("/login")
    header = page.headers.get("set-cookie", "")
    assert "HttpOnly" in header
    assert "SameSite=lax" in header.replace("samesite", "SameSite")
    assert "Secure" in header


def test_authenticated_pages_are_not_cacheable(secure_client):
    _login(secure_client)
    assert "no-store" in secure_client.get("/").headers.get("cache-control", "")


def test_healthz_is_cacheable_enough(secure_client):
    assert "no-store" not in secure_client.get("/healthz").headers.get("cache-control", "")


# ---------------------------------------------------------------------------
# CSRF on state-changing routes
# ---------------------------------------------------------------------------

POST_ROUTES = [
    "/sync",
    "/quests/new",
    "/quests/1/edit",
    "/quests/1/complete",
    "/quests/1/delete",
    "/habits/new",
    "/habits/1/edit",
    "/habits/1/complete",
    "/habits/1/delete",
    "/japanese/preview",
    "/japanese/import",
    "/japanese/archive-habit",
    "/logout",
]


@pytest.mark.parametrize("path", POST_ROUTES)
def test_post_without_csrf_is_rejected(secure_client, path):
    _login(secure_client)
    resp = secure_client.post(path, data={}, follow_redirects=False)
    assert resp.status_code == 403, f"{path} accepted a request without a CSRF token"


@pytest.mark.parametrize("path", POST_ROUTES)
def test_post_with_wrong_csrf_is_rejected(secure_client, path):
    _login(secure_client)
    resp = secure_client.post(
        path, data={CSRF_FIELD: "not-the-token"}, follow_redirects=False
    )
    assert resp.status_code == 403


def test_post_without_session_is_denied_not_redirected(secure_client):
    """A write attempt must fail loudly, not silently bounce to a login page."""
    resp = secure_client.post("/sync", data={}, follow_redirects=False)
    assert resp.status_code == 403


def test_valid_csrf_passes_the_gate(secure_client):
    _login(secure_client)
    page = secure_client.get("/")   # the dashboard always renders the sync form
    resp = secure_client.post(
        "/habits/new",
        data={"title": "Testgewohnheit", "recurrence": "daily",
              "target_count": "1", CSRF_FIELD: _token_from(page.text)},
        follow_redirects=False,
    )
    assert resp.status_code == 303


def test_forms_carry_a_csrf_field(secure_client):
    _login(secure_client)
    for path in ("/", "/habits", "/quests", "/japanese"):
        html = secure_client.get(path).text
        if 'method="POST"' in html:
            assert f'name="{CSRF_FIELD}"' in html, f"{path} has a form without CSRF"


# ---------------------------------------------------------------------------
# Auth DISABLED (local test mode)
# ---------------------------------------------------------------------------

def test_disabled_auth_leaves_everything_open(client_no_auth):
    assert client_no_auth.get("/").status_code == 200
    # A write without any CSRF token must go through when auth is off.
    resp = client_no_auth.post(
        "/habits/new",
        data={"title": "Ohne CSRF", "recurrence": "daily", "target_count": "1"},
        follow_redirects=False,
    )
    assert resp.status_code == 303


def test_disabled_auth_renders_no_csrf_field(client_no_auth):
    assert f'name="{CSRF_FIELD}"' not in client_no_auth.get("/habits").text


@pytest.fixture
def client_no_auth(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    monkeypatch.setenv("WGER_BASE_URL", "https://wger.example.com")
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
        yield client

    app.dependency_overrides.clear()
    cfg._settings = None


def test_today_and_week_are_reachable_after_login(secure_client):
    _login(secure_client)
    for path in ("/today", "/week"):
        assert secure_client.get(path).status_code == 200


def test_completing_from_today_without_csrf_is_rejected(secure_client):
    """The day view's quick action goes through the same CSRF gate as any POST."""
    _login(secure_client)
    page = secure_client.get("/habits/new")
    token = _token_from(page.text)
    secure_client.post(
        "/habits/new",
        data={"title": "Heute", "active": "on", "recurrence": "daily",
              "target_count": "1", "base_xp_reward": "10", CSRF_FIELD: token},
        follow_redirects=False,
    )

    resp = secure_client.post(
        "/habits/1/complete", data={"next": "/today"}, follow_redirects=False
    )
    assert resp.status_code == 403

    resp = secure_client.post(
        "/habits/1/complete",
        data={"next": "/today", CSRF_FIELD: token},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/today"


def test_the_starter_campaign_is_protected(secure_client):
    for path in ("/settings/starter",):
        resp = secure_client.get(path, follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/login"


def test_activating_the_campaign_needs_authentication(secure_client):
    resp = secure_client.post("/settings/starter", data={}, follow_redirects=False)
    assert resp.status_code == 403


def test_activating_the_campaign_needs_a_csrf_token(secure_client):
    _login(secure_client)
    page = secure_client.get("/settings/starter")
    assert page.status_code == 200

    assert secure_client.post("/settings/starter", data={}).status_code == 403

    token = _token_from(page.text)
    ok = secure_client.post("/settings/starter", data={CSRF_FIELD: token})
    assert ok.status_code == 200
    assert "Ergebnis der Aktivierung" in ok.text


def test_the_pwa_files_stay_public(secure_client):
    for path in ("/manifest.webmanifest", "/sw.js", "/offline"):
        assert secure_client.get(path).status_code == 200
