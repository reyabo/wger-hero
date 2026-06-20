"""Tests for stat level calculation, progress, and radar helpers."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models import HeroProfile, HeroStat, StatXpEvent
from app.stats import (
    STAT_KEYS,
    StatProgressView,
    calculate_stat_level,
    generate_radar_grid_points,
    generate_radar_points,
    get_all_stat_progress,
    get_stat_summary,
    xp_for_stat_level,
)


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
# /stats route
# ---------------------------------------------------------------------------

def test_stats_route(db):
    from fastapi.testclient import TestClient
    from unittest.mock import patch, MagicMock
    from app.main import app

    settings_mock = MagicMock()
    settings_mock.HERO_NAME = "Tester"
    settings_mock.WGER_BASE_URL = "https://wger.example.com"
    settings_mock.DATABASE_URL = "sqlite:///:memory:"

    with patch("app.main.get_settings", return_value=settings_mock):
        with patch("app.main.get_db") as mock_get_db:
            mock_get_db.return_value = iter([db])
            client = TestClient(app)
            resp = client.get("/stats")
    assert resp.status_code == 200
    assert "Attribute" in resp.text
    assert "Radar" in resp.text
