"""One attribute, and where its XP actually came from.

The radar shows ten numbers without saying how any of them was earned. The
detail page answers that from the `stat_xp_events` ledger — the same append-only
record the consistency check verifies — so the provenance is readable rather
than inferred.
"""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import Base, HeroProfile, HeroStat, StatXpEvent
from app.stats import (
    SOURCE_LABELS,
    STAT_KEYS,
    get_stat_detail,
    summarize_sources,
    summarize_titles,
)


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add(HeroProfile(name="Hero", level=1, total_xp=0))
    session.commit()
    yield session
    session.close()


class _Event:
    """Just enough of a StatXpEvent for the two pure summarisers."""

    def __init__(self, source, title, xp, when=None):
        self.source = source
        self.title = title
        self.xp = xp
        self.created_at = when or datetime(2026, 8, 18, 12, 0)


def _award(db, stat="strength", xp=10, source="habit", title="Kniebeugen", when=None):
    db.add(StatXpEvent(
        stat_key=stat, xp=xp, source=source, title=title,
        created_at=when or datetime(2026, 8, 18, 12, 0),
    ))
    row = db.query(HeroStat).filter(HeroStat.stat_key == stat).first()
    if row is None:
        row = HeroStat(stat_key=stat, xp=0)
        db.add(row)
    row.xp += xp
    db.commit()


# ---------------------------------------------------------------------------
# The two summarisers are pure — no database, no clock
# ---------------------------------------------------------------------------

def test_sources_are_summed_per_source():
    rows = summarize_sources([
        _Event("habit", "A", 10), _Event("habit", "B", 5), _Event("quest", "C", 20),
    ])
    assert [(r.key, r.xp) for r in rows] == [("quest", 20), ("habit", 15)]


def test_sources_are_sorted_by_xp_descending():
    rows = summarize_sources([_Event("habit", "A", 1), _Event("wger", "B", 99)])
    assert rows[0].key == "wger"


def test_a_source_carries_its_share_and_count():
    rows = summarize_sources([_Event("habit", "A", 75), _Event("quest", "B", 25)])
    assert rows[0].pct == 75
    assert rows[0].count == 1


def test_an_unknown_source_keeps_its_raw_key_as_label():
    rows = summarize_sources([_Event("etwas-neues", "A", 5)])
    assert rows[0].label == "etwas-neues"


def test_known_sources_get_a_german_label():
    rows = summarize_sources([_Event("habit", "A", 5)])
    assert rows[0].label == SOURCE_LABELS["habit"]


def test_no_events_summarise_to_nothing():
    assert summarize_sources([]) == []
    assert summarize_titles([]) == []


def test_a_zero_total_does_not_divide_by_zero():
    rows = summarize_sources([_Event("habit", "A", 0)])
    assert rows[0].pct == 0


def test_titles_are_summed_and_counted():
    rows = summarize_titles([
        _Event("habit", "Kniebeugen", 10), _Event("habit", "Kniebeugen", 10),
        _Event("quest", "Dreifachschlag", 30),
    ])
    assert [(r.title, r.xp, r.count) for r in rows] == [
        ("Dreifachschlag", 30, 1), ("Kniebeugen", 20, 2),
    ]


def test_titles_are_limited():
    rows = summarize_titles([_Event("habit", f"T{n}", n) for n in range(30)], limit=5)
    assert len(rows) == 5


def test_the_same_title_from_two_sources_stays_separate():
    """Otherwise a habit and a quest of the same name would be merged."""
    rows = summarize_titles([_Event("habit", "Lauf", 10), _Event("wger", "Lauf", 40)])
    assert len(rows) == 2
    assert rows[0].source == "wger"


# ---------------------------------------------------------------------------
# The detail assembly
# ---------------------------------------------------------------------------

def test_an_unknown_stat_key_has_no_detail(db):
    assert get_stat_detail(db, "nicht-existent") is None


def test_every_known_stat_key_has_a_detail(db):
    for key in STAT_KEYS:
        assert get_stat_detail(db, key) is not None


def test_a_stat_without_events_reports_zero(db):
    detail = get_stat_detail(db, "strength")
    assert detail.progress.total_xp == 0
    assert detail.sources == []
    assert detail.events == []


def test_the_detail_reflects_the_ledger(db):
    _award(db, "strength", 40, "habit", "Kniebeugen")
    _award(db, "strength", 20, "quest", "Dreifachschlag")
    detail = get_stat_detail(db, "strength")

    assert detail.progress.total_xp == 60
    assert detail.total_xp == 60
    assert {r.key for r in detail.sources} == {"habit", "quest"}


def test_other_stats_do_not_leak_in(db):
    _award(db, "strength", 40)
    _award(db, "knowledge", 999, title="Vokabeln")
    detail = get_stat_detail(db, "strength")

    assert detail.total_xp == 40
    assert all(e.stat_key == "strength" for e in detail.events)


def test_the_event_list_is_newest_first(db):
    base = datetime(2026, 8, 1, 8, 0)
    _award(db, xp=1, title="alt", when=base)
    _award(db, xp=2, title="neu", when=base + timedelta(days=3))
    detail = get_stat_detail(db, "strength")
    assert [e.title for e in detail.events] == ["neu", "alt"]


def test_the_event_list_is_limited(db):
    for n in range(40):
        _award(db, xp=1, title=f"E{n}", when=datetime(2026, 8, 1) + timedelta(hours=n))
    detail = get_stat_detail(db, "strength", limit=10)
    assert len(detail.events) == 10


def test_the_ledger_total_can_disagree_with_the_aggregate(db):
    """A partial view must not be presented as the full total. The page shows
    the aggregate as the truth and the events only as the recent slice."""
    for n in range(5):
        _award(db, xp=10, when=datetime(2026, 8, 1) + timedelta(hours=n))
    detail = get_stat_detail(db, "strength", limit=2)
    assert detail.progress.total_xp == 50
    assert len(detail.events) == 2


def test_the_detail_names_its_neighbours(db):
    """Ten attributes, so the page offers direct navigation to the others."""
    detail = get_stat_detail(db, STAT_KEYS[0])
    assert [s.key for s in detail.all_stats] == STAT_KEYS


# ---------------------------------------------------------------------------
# The route
# ---------------------------------------------------------------------------

@pytest.fixture
def client():
    import os
    os.environ.setdefault("WGER_BASE_URL", "https://wger.example.com")
    os.environ.setdefault("WGER_API_TOKEN", "test-token-for-stat-detail")

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
    seed.add(HeroStat(stat_key="strength", xp=800))
    seed.add(StatXpEvent(stat_key="strength", xp=40, source="habit",
                         source_id="1", title="Kniebeugen"))
    seed.commit()
    seed.close()

    with TestClient(app) as c:
        yield c

    app.dependency_overrides.clear()


def test_the_route_renders(client):
    resp = client.get("/stats/strength")
    assert resp.status_code == 200
    assert "Stärke" in resp.text


def test_an_unknown_stat_is_a_404(client):
    assert client.get("/stats/nicht-existent").status_code == 404


def test_the_overview_links_to_the_detail(client):
    assert "/stats/strength" in client.get("/stats").text
