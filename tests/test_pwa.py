"""Tests for the PWA: manifest, icons, service worker and the cache boundary.

There is no browser here, so the service worker is checked as what it actually
is — a small, explicit, auditable source file. That is the point of keeping it
free of runtime caching rules: its cache boundary can be read and asserted
statically instead of being inferred from a live cache.
"""

import json
import re
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import Base, HeroProfile

REPO_ROOT = Path(__file__).resolve().parent.parent
STATIC = REPO_ROOT / "app" / "static"
SW_SOURCE = (STATIC / "sw.js").read_text()
MANIFEST = json.loads((STATIC / "manifest.webmanifest").read_text())

# Paths that must never end up in Cache Storage.
DYNAMIC_PATHS = [
    "/", "/login", "/logout", "/today", "/week", "/goals", "/habits",
    "/quests", "/japanese", "/settings", "/stats", "/achievements",
]


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
        yield c

    app.dependency_overrides.clear()


def _static_assets() -> list[str]:
    """The allowlist as the worker actually declares it."""
    block = re.search(r"const STATIC_ASSETS = \[(.*?)\];", SW_SOURCE, re.S).group(1)
    return re.findall(r"'([^']+)'", block)


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------

def test_the_manifest_is_reachable(client):
    resp = client.get("/manifest.webmanifest")
    assert resp.status_code == 200
    assert "manifest" in resp.headers["content-type"]


def test_the_manifest_is_valid_json(client):
    assert client.get("/manifest.webmanifest").json()["name"] == "wger-hero"


@pytest.mark.parametrize("field", [
    "name", "short_name", "description", "start_url", "scope", "display",
    "background_color", "theme_color", "icons",
])
def test_the_manifest_carries_every_required_field(field):
    assert MANIFEST.get(field)


def test_the_manifest_starts_on_the_day_view():
    assert MANIFEST["start_url"] == "/today"
    assert MANIFEST["scope"] == "/"


def test_the_manifest_is_installable_as_a_standalone_app():
    assert MANIFEST["display"] == "standalone"


def test_the_manifest_declares_a_maskable_icon():
    purposes = {icon.get("purpose") for icon in MANIFEST["icons"]}
    assert "any" in purposes and "maskable" in purposes


def test_every_icon_exists_locally():
    for icon in MANIFEST["icons"]:
        src = icon["src"]
        assert src.startswith("/static/")
        assert (REPO_ROOT / "app" / src.lstrip("/")).is_file()


def test_the_manifest_references_no_external_host():
    blob = json.dumps(MANIFEST)
    assert "http://" not in blob and "https://" not in blob


def test_the_manifest_contains_no_configuration_values():
    blob = json.dumps(MANIFEST).lower()
    for word in ("token", "secret", "password", "hash", "database_url", "wger_base_url"):
        assert word not in blob


def test_the_icons_are_reachable(client):
    for icon in MANIFEST["icons"]:
        assert client.get(icon["src"]).status_code == 200


def test_the_icons_reference_nothing_external():
    """No icon may fetch anything — the SVG namespace URI is not a fetch."""
    for icon in MANIFEST["icons"]:
        text = (REPO_ROOT / "app" / icon["src"].lstrip("/")).read_text()
        body = text.replace('xmlns="http://www.w3.org/2000/svg"', "")
        assert "http://" not in body
        assert "https://" not in body
        for tag in ("<image", "<use", "url(", "@import", "<script"):
            assert tag not in text


# ---------------------------------------------------------------------------
# Service worker
# ---------------------------------------------------------------------------

def test_the_service_worker_is_reachable(client):
    resp = client.get("/sw.js")
    assert resp.status_code == 200
    assert "javascript" in resp.headers["content-type"]


def test_the_service_worker_may_control_the_whole_app(client):
    assert client.get("/sw.js").headers["Service-Worker-Allowed"] == "/"


def test_the_service_worker_itself_is_not_pinned(client):
    assert "no-cache" in client.get("/sw.js").headers["Cache-Control"]


def test_the_static_asset_list_is_explicit():
    assets = _static_assets()
    assert assets
    assert all(a.startswith("/") for a in assets)
    assert "/static/style.css" in assets
    assert "/offline" in assets
    assert "/manifest.webmanifest" in assets


def test_every_cached_asset_exists(client):
    for path in _static_assets():
        assert client.get(path).status_code == 200, path


@pytest.mark.parametrize("path", DYNAMIC_PATHS)
def test_no_dynamic_route_is_precached(path):
    assert path not in _static_assets()


def test_the_worker_never_caches_a_post():
    assert "request.method !== 'GET'" in SW_SOURCE


def test_the_worker_has_no_catch_all_caching_rule():
    """A blanket "cache every GET" would defeat the whole boundary."""
    assert "cache.put(" not in SW_SOURCE
    assert "cache.add(" not in SW_SOURCE          # only addAll on the fixed list
    assert SW_SOURCE.count("caches.open") == 1    # install only


def test_the_worker_only_stores_the_fixed_list():
    """`addAll` is the single write into Cache Storage, on the allowlist."""
    assert SW_SOURCE.count("addAll(") == 1
    assert "addAll(STATIC_ASSETS)" in SW_SOURCE


def test_the_worker_lists_the_protected_paths_it_must_not_touch():
    for path in ("/login", "/today", "/week", "/settings", "/japanese"):
        assert f"'{path}'" in SW_SOURCE


def test_the_cache_is_versioned_and_old_versions_are_removed():
    assert "CACHE_VERSION" in SW_SOURCE
    assert "caches.delete" in SW_SOURCE
    assert "name !== CACHE_VERSION" in SW_SOURCE


def test_the_worker_ignores_other_origins():
    assert "url.origin !== self.location.origin" in SW_SOURCE


def test_the_worker_falls_back_to_the_offline_page():
    assert "caches.match('/offline')" in SW_SOURCE


def test_the_worker_references_no_external_url():
    assert "http://" not in SW_SOURCE
    assert "https://" not in SW_SOURCE


def test_the_worker_contains_no_configuration_values():
    lowered = SW_SOURCE.lower()
    for word in ("token", "secret", "password", "csrf"):
        # "csrf" may only appear in the comment explaining what is not cached
        assert word not in lowered or word == "csrf"


# ---------------------------------------------------------------------------
# Offline page
# ---------------------------------------------------------------------------

def test_the_offline_page_is_reachable(client):
    resp = client.get("/offline")
    assert resp.status_code == 200
    assert "Keine Verbindung" in resp.text


def test_the_offline_page_is_public(client):
    from app.auth import PUBLIC_PATHS

    assert "/offline" in PUBLIC_PATHS
    assert "/manifest.webmanifest" in PUBLIC_PATHS
    assert "/sw.js" in PUBLIC_PATHS


def test_the_offline_page_shows_no_user_data(client):
    """It is cached, so it must never carry goals, habits or quests."""
    html = client.get("/offline").text
    for word in ("Kraftpfad", "Gewohnheit", "Quest", "XP", "Level", "Streak"):
        assert word not in html


def test_the_offline_page_offers_no_form(client):
    html = client.get("/offline").text
    assert "<form" not in html
    assert "<input" not in html


def test_the_offline_page_uses_no_external_resource(client):
    html = client.get("/offline").text
    assert "http://" not in html
    assert "https://" not in html


def test_the_offline_page_offers_a_retry(client):
    assert 'href="/today"' in client.get("/offline").text


# ---------------------------------------------------------------------------
# Wiring and headers
# ---------------------------------------------------------------------------

def test_the_base_template_links_the_manifest(client):
    html = client.get("/").text
    assert 'rel="manifest" href="/manifest.webmanifest"' in html
    assert 'name="theme-color"' in html
    assert 'rel="apple-touch-icon"' in html


def test_the_registration_is_defensive(client):
    html = client.get("/").text
    assert '"serviceWorker" in navigator' in html
    assert ".catch(" in html
    assert "console." not in html


def test_the_registration_uses_no_external_library(client):
    html = client.get("/").text
    assert "<script src=" not in html


def test_protected_pages_stay_uncacheable(secure_pages_client=None):
    """The no-store header on authenticated pages is what the SW relies on."""
    import inspect

    import app.main as main

    source = inspect.getsource(main)
    assert 'response.headers["Cache-Control"] = "no-store, private"' in source


def test_public_pwa_files_carry_no_user_data(client):
    for path in ("/manifest.webmanifest", "/sw.js", "/offline"):
        body = client.get(path).text
        assert "Hero" not in body or path == "/offline" and "wger-hero" in body
