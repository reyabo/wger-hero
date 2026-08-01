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
    """Build a database as it looked BEFORE the current revisions, with real rows.

    Deliberately does not use Base.metadata.create_all(): the models now carry
    columns and tables that later revisions add, so create_all() would produce a
    database that is already migrated. Running the baseline revision and then
    dropping alembic's bookkeeping reproduces a genuine pre-Alembic install.
    Rows are inserted with raw SQL for the same reason — the ORM would try to
    write columns that do not exist yet.
    """
    cfg = _alembic_config(url)
    command.upgrade(cfg, "0001_baseline")

    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE alembic_version"))
        conn.execute(text(
            "INSERT INTO hero_profile (name, level, total_xp) VALUES ('Hero', 4, 4200)"
        ))
        conn.execute(text(
            "INSERT INTO xp_events (event_type, source, source_id, xp, attribute, title)"
            " VALUES ('workout_complete', 'wger', '42', 100, 'Strength', 'Bestand')"
        ))
        conn.execute(text(
            "INSERT INTO habits (title, active, recurrence, target_count,"
            " base_xp_reward, created_at, updated_at)"
            " VALUES ('Bestands-Habit', 1, 'daily', 1, 25,"
            " '2026-01-01 00:00:00', '2026-01-01 00:00:00')"
        ))
        conn.execute(text(
            "INSERT INTO habit_completions (habit_id, completed_at, xp_awarded,"
            " stat_xp_awarded) VALUES (1, '2026-01-05 00:00:00', 25, 0)"
        ))
        conn.execute(text(
            "INSERT INTO quests (slug, title, quest_type, period, target_value,"
            " current_value, xp_reward, attribute, active, repeatable)"
            " VALUES ('legacy', 'Legacy', 'manual', 'weekly', 3, 2, 100, 'Strength', 1, 0)"
        ))
    engine.dispose()


def _snapshot(url: str) -> dict:
    """Read user data with raw SQL so it works before and after a migration."""
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            hero = conn.execute(text("SELECT level, total_xp FROM hero_profile")).first()
            return {
                "hero": (hero[0], hero[1]),
                "xp_events": conn.execute(
                    text("SELECT count(*) FROM xp_events")).scalar(),
                "habits": conn.execute(text("SELECT count(*) FROM habits")).scalar(),
                "completions": conn.execute(
                    text("SELECT count(*) FROM habit_completions")).scalar(),
                "quest_progress": conn.execute(
                    text("SELECT current_value FROM quests WHERE slug='legacy'")
                ).scalar(),
            }
    finally:
        engine.dispose()


def test_existing_database_can_be_stamped(db_url):
    _seed_legacy_database(db_url)
    before = _snapshot(db_url)

    command.stamp(_alembic_config(db_url), "head")

    assert "alembic_version" in _tables(db_url)
    assert _snapshot(db_url) == before, "stamping must not touch any data"


def test_stamped_database_upgrades_cleanly(db_url):
    """The documented adoption path: stamp the baseline, then upgrade.

    Stamping must not re-create what is already there, and the upgrade that
    follows must apply the later revisions without touching user data.
    """
    _seed_legacy_database(db_url)
    before = _snapshot(db_url)

    cfg = _alembic_config(db_url)
    command.stamp(cfg, "0001_baseline")
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


# ---------------------------------------------------------------------------
# Revision 0002: goals — additive on a populated database
# ---------------------------------------------------------------------------

def test_goals_revision_upgrades_a_populated_database(db_url):
    """The riskiest case: new NOT NULL columns on tables that already have rows."""
    _seed_legacy_database(db_url)
    before = _snapshot(db_url)

    cfg = _alembic_config(db_url)
    command.stamp(cfg, "0001_baseline")
    command.upgrade(cfg, "head")

    tables = _tables(db_url)
    assert "goals" in tables
    columns = {
        t: {c["name"] for c in inspect(create_engine(db_url)).get_columns(t)}
        for t in ("habits", "quests")
    }
    assert {"goal_id", "sort_order"} <= columns["habits"]
    assert {"goal_id", "is_milestone", "sort_order"} <= columns["quests"]
    assert _snapshot(db_url) == before, "existing rows must survive untouched"


def test_existing_rows_get_defaults_not_nulls(db_url):
    from sqlalchemy.orm import sessionmaker

    from app.models import Habit, Quest

    _seed_legacy_database(db_url)
    cfg = _alembic_config(db_url)
    command.stamp(cfg, "0001_baseline")
    command.upgrade(cfg, "head")

    engine = create_engine(db_url)
    session = sessionmaker(bind=engine)()
    try:
        habit = session.query(Habit).first()
        quest = session.query(Quest).first()
        assert habit.goal_id is None        # optional link stays empty
        assert habit.sort_order == 0        # server_default filled it in
        assert quest.is_milestone in (False, 0)
        assert quest.sort_order == 0
    finally:
        session.close()
        engine.dispose()


def test_goals_revision_downgrade_removes_only_what_it_added(db_url):
    _seed_legacy_database(db_url)
    before = _snapshot(db_url)

    cfg = _alembic_config(db_url)
    command.stamp(cfg, "0001_baseline")
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0001_baseline")

    tables = _tables(db_url)
    assert "goals" not in tables
    assert {"habits", "quests", "hero_profile"} <= tables
    assert _snapshot(db_url) == before, "downgrade must not lose user data"
