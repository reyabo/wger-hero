"""Tests for the Alembic migration setup.

The point of these tests is the one thing that must never go wrong on the live
server: an existing SQLite database has to be adoptable without losing a single
row, and every revision has to be reversible.
"""

from argparse import Namespace
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from app.models import Base

REPO_ROOT = Path(__file__).resolve().parent.parent


def _alembic_config(url: str) -> Config:
    """Alembic config pointed at a throwaway database via -x url."""
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.cmd_opts = Namespace(x=[f"url={url}"])
    return cfg


def _tables(url: str) -> set[str]:
    engine = create_engine(url)
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


@pytest.fixture
def db_url(tmp_path) -> str:
    return f"sqlite:///{tmp_path / 'test.db'}"


# ---------------------------------------------------------------------------
# Fresh database
# ---------------------------------------------------------------------------

def test_upgrade_creates_every_table(db_url):
    command.upgrade(_alembic_config(db_url), "head")
    tables = _tables(db_url)
    for name in Base.metadata.tables:
        assert name in tables, f"{name} missing after upgrade"


def test_upgrade_records_the_revision(db_url):
    command.upgrade(_alembic_config(db_url), "head")
    assert "alembic_version" in _tables(db_url)


def test_downgrade_is_reversible(db_url):
    cfg = _alembic_config(db_url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    remaining = _tables(db_url) - {"alembic_version"}
    assert remaining == set(), f"downgrade left tables behind: {remaining}"


def test_upgrade_downgrade_upgrade_round_trip(db_url):
    cfg = _alembic_config(db_url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    assert "hero_profile" in _tables(db_url)


# ---------------------------------------------------------------------------
# Existing database: adoption via stamp
# ---------------------------------------------------------------------------

def _seed_legacy_database(url: str) -> None:
    """Build a database the way the app did before Alembic, with real rows."""
    from datetime import datetime

    from sqlalchemy.orm import sessionmaker

    from app.models import Habit, HabitCompletion, HeroProfile, Quest, XpEvent

    engine = create_engine(url)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add(HeroProfile(name="Hero", level=4, total_xp=4200))
    session.add(
        XpEvent(
            event_type="workout_complete", source="wger", source_id="42",
            xp=100, attribute="Strength", title="Bestand",
        )
    )
    habit = Habit(title="Bestands-Habit", base_xp_reward=25)
    session.add(habit)
    session.flush()
    session.add(
        HabitCompletion(habit_id=habit.id, completed_at=datetime(2026, 1, 5), xp_awarded=25)
    )
    session.add(Quest(slug="legacy", title="Legacy", target_value=3, current_value=2))
    session.commit()
    session.close()
    engine.dispose()


def _snapshot(url: str) -> dict:
    from sqlalchemy.orm import sessionmaker

    from app.models import Habit, HabitCompletion, HeroProfile, Quest, XpEvent

    engine = create_engine(url)
    session = sessionmaker(bind=engine)()
    try:
        hero = session.query(HeroProfile).first()
        return {
            "hero": (hero.level, hero.total_xp),
            "xp_events": session.query(XpEvent).count(),
            "habits": session.query(Habit).count(),
            "completions": session.query(HabitCompletion).count(),
            "quest_progress": session.query(Quest).filter(
                Quest.slug == "legacy"
            ).one().current_value,
        }
    finally:
        session.close()
        engine.dispose()


def test_existing_database_can_be_stamped(db_url):
    _seed_legacy_database(db_url)
    before = _snapshot(db_url)

    command.stamp(_alembic_config(db_url), "head")

    assert "alembic_version" in _tables(db_url)
    assert _snapshot(db_url) == before, "stamping must not touch any data"


def test_stamped_database_is_already_at_head(db_url):
    """After stamping, a further upgrade must be a no-op, not a re-create."""
    _seed_legacy_database(db_url)
    before = _snapshot(db_url)

    cfg = _alembic_config(db_url)
    command.stamp(cfg, "head")
    command.upgrade(cfg, "head")      # must not raise "table already exists"

    assert _snapshot(db_url) == before


def test_baseline_upgrade_on_existing_database_is_refused(db_url):
    """Running the baseline over an existing schema must fail loudly.

    This documents why DEPLOY.md says to stamp, never upgrade, on adoption:
    silently continuing could otherwise mask a half-migrated database.
    """
    _seed_legacy_database(db_url)
    with pytest.raises(Exception):
        command.upgrade(_alembic_config(db_url), "head")


# ---------------------------------------------------------------------------
# Migrations must never run by themselves
# ---------------------------------------------------------------------------

def test_importing_the_app_does_not_migrate(db_url, monkeypatch):
    """Importing modules must not create alembic_version anywhere.

    Deliberately does NOT reload app.database: reloading rebinds get_db, which
    would break the dependency_overrides other test modules key on.
    """
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("WGER_BASE_URL", "https://wger.example.com")

    import app.database  # noqa: F401
    import app.main  # noqa: F401

    assert "alembic_version" not in _tables(db_url)


def test_no_module_calls_alembic_at_import_time():
    """No application module may trigger a migration as an import side effect."""
    import ast

    for path in (REPO_ROOT / "app").glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [
                    node.module or ""
                    if isinstance(node, ast.ImportFrom)
                    else a.name
                    for a in (node.names or [None])
                ]
                assert not any("alembic" in (n or "") for n in names), (
                    f"{path.name} imports alembic; migrations must stay an "
                    "explicit deployment step"
                )


def test_init_db_does_not_migrate(db_url, monkeypatch):
    """init_db() sets up tables for fresh installs but never stamps or upgrades."""
    import app.config as cfg
    import app.database as dbmod

    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("WGER_BASE_URL", "https://wger.example.com")
    cfg._settings = None
    dbmod._engine = None
    dbmod._SessionLocal = None

    dbmod.init_db()

    assert "hero_profile" in _tables(db_url)
    assert "alembic_version" not in _tables(db_url)

    dbmod._engine = None
    dbmod._SessionLocal = None
    cfg._settings = None


def test_added_columns_transition_is_still_documented():
    """_ADDED_COLUMNS stays until adoption is proven; it must stay documented."""
    import app.database as dbmod

    assert dbmod._ADDED_COLUMNS, "transitional migrator was removed too early"
    source = Path(dbmod.__file__).read_text()
    assert "Alembic" in source, "the transitional status must be documented"
