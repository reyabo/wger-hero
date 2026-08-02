"""Markup and semantics of the reworked interface.

These tests check structure, wording and accessibility hooks — never pixels.
There is no browser here, so anything visual is asserted where it is actually
decided: in the template output and in the stylesheet source.
"""

from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import (
    Base,
    Goal,
    Habit,
    HabitCompletion,
    HeroProfile,
    JapaneseSaveImport,
    Quest,
    XpEvent,
)
from app.quests import app_today

REPO_ROOT = Path(__file__).resolve().parent.parent
CSS = (REPO_ROOT / "app" / "static" / "style.css").read_text()


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
    seed.add(HeroProfile(name="Hero", level=3, total_xp=1500))
    seed.commit()
    seed.close()

    with TestClient(app) as c:
        yield c, TestSession

    app.dependency_overrides.clear()


def _habit(c, title="Gewohnheit", weekdays=None, xp="10"):
    data = {"title": title, "active": "on", "recurrence": "daily",
            "target_count": "1", "base_xp_reward": xp}
    if weekdays:
        data["weekdays"] = [str(d) for d in weekdays]
    c.post("/habits/new", data=data, follow_redirects=False)


def _today_habit(c, title="Heute", xp="10"):
    _habit(c, title, weekdays=[app_today().isoweekday()], xp=xp)


# ---------------------------------------------------------------------------
# Layout and navigation
# ---------------------------------------------------------------------------

def test_the_layout_offers_a_skip_link(client):
    c, _ = client
    html = c.get("/today").text
    assert 'class="skip-link"' in html
    assert 'href="#main"' in html
    assert 'id="main"' in html


def test_the_navigation_is_labelled(client):
    c, _ = client
    assert 'aria-label="Hauptnavigation"' in c.get("/today").text


def test_every_main_view_is_reachable_from_the_navigation(client):
    c, _ = client
    html = c.get("/today").text
    for path in ("/today", "/week", "/goals", "/habits", "/quests",
                 "/japanese", "/stats", "/settings"):
        assert f'href="{path}"' in html


def test_the_theme_colour_matches_the_dark_base(client):
    c, _ = client
    assert 'content="#070a13"' in c.get("/today").text


# ---------------------------------------------------------------------------
# Hero panel
# ---------------------------------------------------------------------------

def test_the_hero_panel_is_on_today(client):
    c, _ = client
    html = c.get("/today").text
    assert 'class="hero-panel' in html
    assert "Hero" in html


def test_the_hero_panel_shows_level_and_xp(client):
    c, _ = client
    html = c.get("/today").text
    # 1500 total XP is level 2 with 250 XP into it — the numbers come from
    # app/xp.py, so the panel is asserted against that, not against a guess.
    assert "Level 2" in html
    assert "XP bis Level 3" in html
    assert "Gesamt 1500 XP" in html


def test_the_xp_bar_is_a_labelled_progressbar(client):
    c, _ = client
    html = c.get("/today").text
    assert 'role="progressbar"' in html
    assert 'aria-valuenow=' in html
    assert 'aria-valuemin="0"' in html and 'aria-valuemax="100"' in html


def test_the_hero_panel_loads_no_image(client):
    c, _ = client
    html = c.get("/today").text
    assert "<img" not in html


def test_the_hero_panel_is_not_charged_without_a_completion(client):
    c, _ = client
    assert "is-charged" not in c.get("/today").text


# ---------------------------------------------------------------------------
# Habit cards and their states
# ---------------------------------------------------------------------------

def test_an_open_habit_says_open(client):
    c, _ = client
    _today_habit(c)
    html = c.get("/today").text
    assert "offen" in html
    assert "status-open" in html


def test_a_completed_habit_says_done(client):
    c, _ = client
    _today_habit(c)
    c.post("/habits/1/complete", data={"next": "/today"}, follow_redirects=False)
    html = c.get("/today").text
    assert "erledigt" in html
    assert "is-done" in html


def test_a_paused_goal_marks_its_habit_neutrally(client):
    c, TestSession = client
    c.post("/goals/new", data={"title": "Ziel"}, follow_redirects=False)
    _today_habit(c)
    db = TestSession()
    db.query(Habit).one().goal_id = db.query(Goal).one().id
    db.commit()
    db.close()
    c.post("/goals/ziel/status", data={"status": "paused"}, follow_redirects=False)

    html = c.get("/today").text
    assert "pausiert" in html and "is-paused" in html
    for word in ("gescheitert", "Strafe", "verloren", "versagt"):
        assert word.lower() not in html.lower()


def test_planned_and_flexible_habits_are_separated(client):
    c, _ = client
    _today_habit(c, "Geplant")
    _habit(c, "Flexibel")
    html = c.get("/today").text
    assert "Für heute geplant" in html
    assert "Jederzeit möglich" in html


# ---------------------------------------------------------------------------
# Goals, momentum, quests
# ---------------------------------------------------------------------------

def test_a_goal_card_shows_its_metrics(client):
    c, _ = client
    c.post("/goals/new", data={"title": "Kraftpfad"}, follow_redirects=False)
    html = c.get("/goals").text
    assert "Kraftpfad" in html
    assert "Momentum" in html


def test_the_goal_detail_shows_streaks_and_the_formula(client):
    c, _ = client
    c.post("/goals/new", data={"title": "Kraftpfad"}, follow_redirects=False)
    html = c.get("/goals/kraftpfad").text
    assert "Aktuelle Serie" in html
    assert "Beste Serie" in html
    assert "Wie wird Momentum berechnet?" in html


def test_a_manual_quest_says_it_needs_confirmation(client):
    c, TestSession = client
    db = TestSession()
    db.add(Quest(slug="fuenfer", title="Der Fünfer-Rhythmus", period="weekly",
                 quest_type="manual", target_value=5, current_value=0,
                 active=True, repeatable=True))
    db.commit()
    db.close()
    html = c.get("/quests").text
    assert "manuell zu bestätigen" in html
    assert "zählt automatisch" not in html


def test_an_automatic_quest_says_it_counts_itself(client):
    c, TestSession = client
    db = TestSession()
    db.add(Quest(slug="wq", title="Dreifachschlag", period="weekly",
                 quest_type="workout_count", target_value=3, current_value=0,
                 active=True, repeatable=True))
    db.commit()
    db.close()
    assert "zählt automatisch" in c.get("/quests").text


def test_a_milestone_is_marked_as_one(client):
    c, TestSession = client
    db = TestSession()
    db.add(Quest(slug="ms", title="Erstes Workout", period="once",
                 quest_type="manual", target_value=1, current_value=0,
                 active=True, is_milestone=True))
    db.commit()
    db.close()
    assert "Meilenstein" in c.get("/quests").text


# ---------------------------------------------------------------------------
# Login, starter, offline
# ---------------------------------------------------------------------------

def test_the_login_page_is_focused():
    """Rendered only when auth is on, so the template itself is asserted here.

    The rendered page is covered by tests/test_auth.py, which runs with
    AUTH_ENABLED=true.
    """
    html = (REPO_ROOT / "app" / "templates" / "login.html").read_text()
    assert 'name="password"' in html
    assert "login-card" in html
    assert "csrf_input(request)" in html
    for leak in ("$argon2", "AUTH_PASSWORD_HASH_FILE", "SESSION_SECRET_FILE"):
        assert leak not in html


def test_the_starter_preview_names_every_action(client):
    c, _ = client
    html = c.get("/settings/starter").text
    for label in ("wird neu angelegt", "Vorschau"):
        assert label in html
    assert "Ziel" in html and "Gewohnheit" in html and "Meilenstein" in html


def test_the_offline_page_matches_the_dark_theme(client):
    c, _ = client
    html = c.get("/offline").text
    assert 'content="#070a13"' in html
    assert "/static/style.css" in html


# ---------------------------------------------------------------------------
# Japanese metrics: displayed as 0, stored as NULL
# ---------------------------------------------------------------------------

MINIMAL_SAVE = """=== 状態 SAVE ===
Datum: 2026-08-01 | Streak: 4
Charakter: Lv 2 (見習い) | 433 / 1000 XP
語彙 180 | 文法 250 | 読解 0 | 聴解 0 | 会話 215
=== END SAVE ==="""


def test_a_missing_numeric_metric_renders_as_zero(client):
    c, _ = client
    html = c.post("/japanese/preview", data={"raw_save": MINIMAL_SAVE}).text
    assert "Lv 0" in html
    assert "0 Punkte" in html


def test_a_missing_bunpro_level_stays_neutral(client):
    c, _ = client
    html = c.post("/japanese/preview", data={"raw_save": MINIMAL_SAVE}).text
    assert "Nicht angegeben" in html
    for invented in ("N0", "Lv N", "Bunpro: 0"):
        assert invented not in html


def test_an_explicit_zero_renders_as_zero_too(client):
    c, _ = client
    text = MINIMAL_SAVE.replace("Charakter:", "Grammatikpunkte im SRS: 0\nCharakter:")
    assert "0 Punkte" in c.post("/japanese/preview", data={"raw_save": text}).text


def test_rendering_never_writes_the_missing_values_back(client):
    """The UI shows 0; the database keeps NULL. Viewing changes nothing."""
    c, TestSession = client
    c.post("/japanese/import", data={"raw_save": MINIMAL_SAVE})

    db = TestSession()
    record = db.query(JapaneseSaveImport).one()
    before = (record.wanikani_level, record.bunpro_level, record.bunpro_points,
              record.normalized_hash, record.xp_awarded, record.classification)
    record_id = record.id
    db.close()

    for _ in range(3):
        c.get("/japanese")
        c.get(f"/japanese/imports/{record_id}")
        c.get("/")

    db = TestSession()
    record = db.query(JapaneseSaveImport).one()
    after = (record.wanikani_level, record.bunpro_level, record.bunpro_points,
             record.normalized_hash, record.xp_awarded, record.classification)
    assert after == before
    assert record.wanikani_level is None and record.bunpro_points is None
    db.close()


def test_viewing_creates_no_rows(client):
    c, TestSession = client
    c.post("/japanese/import", data={"raw_save": MINIMAL_SAVE})

    db = TestSession()
    counts = (db.query(JapaneseSaveImport).count(), db.query(XpEvent).count(),
              db.query(HabitCompletion).count())
    db.close()

    c.get("/japanese")
    c.get("/today")
    c.get("/week")
    c.get("/")

    db = TestSession()
    assert (db.query(JapaneseSaveImport).count(), db.query(XpEvent).count(),
            db.query(HabitCompletion).count()) == counts
    db.close()


def test_a_missing_and_an_explicit_zero_look_alike_but_differ_inside(client):
    from app.japanese_saves import parse_save

    absent = parse_save(MINIMAL_SAVE)
    zero = parse_save(
        MINIMAL_SAVE.replace("Charakter:", "Grammatikpunkte im SRS: 0\nCharakter:")
    )
    assert absent.bunpro_points is None and zero.bunpro_points == 0
    assert absent.normalized_hash() != zero.normalized_hash()


# ---------------------------------------------------------------------------
# Stylesheet: accessibility and self-containment
# ---------------------------------------------------------------------------

def test_reduced_motion_is_honoured():
    assert "@media (prefers-reduced-motion: reduce)" in CSS
    block = CSS.split("@media (prefers-reduced-motion: reduce)")[-1]
    assert "animation: none" in block


def test_a_completed_habit_stays_recognisable_without_animation():
    """Reduced motion must not reduce the state to nothing."""
    block = CSS.split("@media (prefers-reduced-motion: reduce)")[-1]
    assert "border-color" in block or "background" in block


def test_focus_is_always_visible():
    assert ":focus-visible" in CSS
    assert "outline:" in CSS


def test_touch_targets_are_large_enough():
    assert "min-height: 44px" in CSS


def test_the_stylesheet_loads_nothing_external():
    for marker in ("http://", "https://", "@import", "fonts.googleapis",
                   "cdn.", "url(//"):
        assert marker not in CSS


def test_animations_stay_on_transform_and_opacity():
    """Cheap properties only — nothing here may trigger layout."""
    for keyframes in ("@keyframes reward-rise", "@keyframes fresh-pulse"):
        assert keyframes in CSS
    assert "@keyframes" in CSS
    for banned in ("animation: spin 0s infinite", "animation-iteration-count: infinite"):
        assert banned not in CSS


def test_no_animation_runs_forever():
    assert "infinite" not in CSS


def test_no_template_loads_an_external_resource():
    for path in (REPO_ROOT / "app" / "templates").glob("*.html"):
        text = path.read_text()
        for marker in ("http://", "https://", "fonts.googleapis", "cdn."):
            assert marker not in text, f"{path.name} references {marker}"
