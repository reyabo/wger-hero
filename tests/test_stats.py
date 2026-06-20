"""Tests for stat level calculation, progress, and radar helpers."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models import HeroProfile, HeroStat, StatXpEvent
from app.stats import (
    RADAR_MIN_RATIO,
    STAT_KEYS,
    StatProgressView,
    build_radar,
    calculate_stat_level,
    generate_radar_grid_points,
    generate_radar_points,
    get_all_stat_progress,
    get_stat_summary,
    xp_for_stat_level,
)


def _progress(level=1):
    """Helper: 10 StatProgressView rows at a given level."""
    return [
        StatProgressView(key=k, name=k.title(), abbr=k[:3].upper(), level=level,
                         total_xp=0, xp_in_level=0, xp_for_next=150, pct=0)
        for k in STAT_KEYS
    ]


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()
    Base.metadata.drop_all(engine)


# ---------------------------------------------------------------------------
# xp_for_stat_level
# ---------------------------------------------------------------------------

def test_xp_for_level_1():
    assert xp_for_stat_level(1) == 150  # 100 + 1*50

def test_xp_for_level_5():
    assert xp_for_stat_level(5) == 350  # 100 + 5*50

def test_xp_for_level_0():
    assert xp_for_stat_level(0) == 100  # 100 + 0*50


# ---------------------------------------------------------------------------
# calculate_stat_level
# ---------------------------------------------------------------------------

def test_zero_xp_is_level_1():
    info = calculate_stat_level(0)
    assert info.level == 1
    assert info.xp_in_level == 0
    assert info.pct == 0

def test_negative_xp_treated_as_zero():
    info = calculate_stat_level(-50)
    assert info.level == 1
    assert info.total_xp == 0

def test_exactly_at_level_boundary():
    # Level 1 needs 150 XP. 150 XP should advance to level 2.
    info = calculate_stat_level(150)
    assert info.level == 2
    assert info.xp_in_level == 0

def test_mid_level_progress():
    # Level 1 needs 150 XP. 75 XP = halfway through level 1.
    info = calculate_stat_level(75)
    assert info.level == 1
    assert info.xp_in_level == 75
    assert info.pct == 50

def test_high_xp_level():
    # Accumulate enough for several levels
    total = sum(xp_for_stat_level(lv) for lv in range(1, 6))  # levels 1-5 complete
    info = calculate_stat_level(total)
    assert info.level == 6
    assert info.xp_in_level == 0


# ---------------------------------------------------------------------------
# get_all_stat_progress
# ---------------------------------------------------------------------------

def test_all_10_stats_returned(db):
    progress = get_all_stat_progress(db)
    assert len(progress) == 10
    keys = [p.key for p in progress]
    assert keys == STAT_KEYS

def test_zero_xp_stats_are_level_1(db):
    progress = get_all_stat_progress(db)
    for p in progress:
        assert p.level == 1
        assert p.total_xp == 0

def test_stat_with_xp_shows_progress(db):
    stat = HeroStat(stat_key="strength", xp=200)
    db.add(stat)
    db.commit()
    progress = get_all_stat_progress(db)
    strength = next(p for p in progress if p.key == "strength")
    assert strength.total_xp == 200
    assert strength.level >= 1


# ---------------------------------------------------------------------------
# get_stat_summary
# ---------------------------------------------------------------------------

def test_summary_empty_db(db):
    summary = get_stat_summary(db)
    assert summary["week_xp"] == 0
    assert summary["month_xp"] == 0
    assert summary["strongest_key"] is None

def test_summary_strongest_weakest(db):
    db.add(HeroStat(stat_key="strength", xp=500))
    db.add(HeroStat(stat_key="endurance", xp=100))
    db.commit()
    summary = get_stat_summary(db)
    assert summary["strongest_key"] == "strength"
    assert summary["weakest_key"] == "endurance"


# ---------------------------------------------------------------------------
# radar helpers
# ---------------------------------------------------------------------------

def test_radar_points_count():
    progress = [
        StatProgressView(key=k, name=k, abbr=k[:3].upper(), level=1,
                         total_xp=0, xp_in_level=0, xp_for_next=150, pct=0)
        for k in STAT_KEYS
    ]
    points = generate_radar_points(progress)
    assert len(points) == 10

def test_radar_points_empty():
    assert generate_radar_points([]) == []

def test_radar_grid_points_count():
    pts = generate_radar_grid_points(level=3, n=10)
    assert len(pts) == 10

def test_radar_grid_at_center_when_level_zero():
    pts = generate_radar_grid_points(level=0, n=6)
    for x, y in pts:
        assert x == pytest.approx(200.0)
        assert y == pytest.approx(200.0)


# ---------------------------------------------------------------------------
# build_radar (ready-to-render structure)
# ---------------------------------------------------------------------------

def test_build_radar_empty():
    radar = build_radar([])
    assert radar["rings"] == []
    assert radar["points"] == []
    assert radar["polygon_points"] == ""


def test_build_radar_structure():
    radar = build_radar(_progress(level=3))
    # at least 5 grid rings, 10 axes / labels / points
    assert radar["rings_count"] >= 5
    assert len(radar["rings"]) == radar["rings_count"]
    assert len(radar["axes"]) == 10
    assert len(radar["labels"]) == 10
    assert len(radar["points"]) == 10
    # every ring has a non-empty points string
    for ring in radar["rings"]:
        assert ring["points"].strip()
    # data polygon is non-empty
    assert radar["polygon_points"].strip()


def test_build_radar_labels_have_text():
    radar = build_radar(_progress())
    for lbl in radar["labels"]:
        assert lbl["text"]  # abbreviation
        assert "x" in lbl and "y" in lbl


def test_build_radar_zero_data_polygon_visible():
    """Fresh / all-level-1 character must still produce a visible polygon."""
    radar = build_radar(_progress(level=1))
    assert radar["polygon_points"].strip() != ""
    # vertices must be off-centre (minimum radius applied), not collapsed to 200,200
    coords = [tuple(map(float, p.split(","))) for p in radar["polygon_points"].split()]
    cx = cy = 200.0
    distances = [((x - cx) ** 2 + (y - cy) ** 2) ** 0.5 for x, y in coords]
    assert min(distances) > 0  # nothing collapsed onto the centre
    # the minimum radius should be honoured (150 * RADAR_MIN_RATIO)
    assert max(distances) >= 150 * RADAR_MIN_RATIO - 1


def test_build_radar_points_carry_metadata():
    radar = build_radar(_progress(level=2))
    pt = radar["points"][0]
    assert {"cx", "cy", "label", "value"} <= set(pt.keys())
    assert pt["value"] == 2


# ---------------------------------------------------------------------------
# /stats route
# ---------------------------------------------------------------------------

@pytest.fixture
def client():
    """TestClient wired to a fresh in-memory DB via dependency override."""
    import os
    os.environ.setdefault("WGER_BASE_URL", "https://wger.example.com")
    os.environ.setdefault("WGER_API_TOKEN", "test-token-for-stats")

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
    # Seed varied stat data so the radar + gain list render with real values.
    seed = TestSession()
    seed.add(HeroStat(stat_key="strength", xp=800))
    seed.add(HeroStat(stat_key="endurance", xp=350))
    seed.add(StatXpEvent(
        stat_key="strength", xp=40, source="habit",
        source_id="1", title="Workout erledigt",
    ))
    seed.commit()
    seed.close()

    with TestClient(app) as c:
        yield c

    app.dependency_overrides.clear()


def test_stats_route(client):
    resp = client.get("/stats")
    assert resp.status_code == 200
    assert "Attribute" in resp.text
    assert "Radar" in resp.text


def test_stats_route_renders_data(client):
    resp = client.get("/stats")
    # radar data polygon, a stat abbreviation, and the seeded gain must appear
    assert 'class="radar-polygon"' in resp.text
    assert "STR" in resp.text
    assert "Workout erledigt" in resp.text


def test_stats_radar_has_visible_elements(client):
    resp = client.get("/stats")
    html = resp.text
    assert "<svg" in html
    # grid rings present (at least 5)
    assert html.count('class="radar-ring"') >= 5
    # data polygon present
    assert 'class="radar-polygon"' in html
    # at least 10 stat point markers
    assert html.count('class="radar-point"') >= 10
    # axis labels present
    assert html.count('class="radar-label"') >= 10


def test_stats_radar_uses_explicit_attributes(client):
    """SVG must carry explicit stroke/fill so it renders even without CSS."""
    resp = client.get("/stats")
    html = resp.text
    # polygon fill + stroke set inline
    assert 'stroke="rgba(125, 249, 255, 0.95)"' in html
    # point markers use an explicit gold fill
    assert 'fill="rgba(245, 158, 11, 1)"' in html


def test_stats_in_nav(client):
    resp = client.get("/stats")
    assert "/stats" in resp.text
    assert "Attribute" in resp.text


# ---------------------------------------------------------------------------
# Reward preview contrast (habit + quest forms) and CSS coverage
# ---------------------------------------------------------------------------

def test_habit_form_reward_preview_themed(client):
    resp = client.get("/habits/new")
    assert resp.status_code == 200
    # themed class, not the old white "card" box
    assert 'class="reward-preview"' in resp.text
    assert "#f8f8f2" not in resp.text


def test_quest_form_reward_preview_themed(client):
    resp = client.get("/quests/new")
    assert resp.status_code == 200
    assert 'class="reward-preview"' in resp.text
    assert "#f8f8f2" not in resp.text


def test_css_styles_radar_and_reward_preview():
    import pathlib
    css = pathlib.Path("app/static/style.css").read_text()
    for selector in (".radar-ring", ".radar-axis", ".radar-polygon",
                     ".radar-point", ".radar-label"):
        assert selector in css, f"missing CSS for {selector}"
    assert ".reward-preview" in css
    assert ".reward-preview-xp" in css
