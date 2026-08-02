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


# ---------------------------------------------------------------------------
# Revision 0003: quest completions and the stable habit link
# ---------------------------------------------------------------------------

def test_quest_completions_revision_on_a_populated_database(db_url):
    _seed_legacy_database(db_url)
    before = _snapshot(db_url)

    cfg = _alembic_config(db_url)
    command.stamp(cfg, "0001_baseline")
    command.upgrade(cfg, "head")

    engine = create_engine(db_url)
    assert "quest_completions" in _tables(db_url)
    quest_cols = {c["name"] for c in inspect(engine).get_columns("quests")}
    assert "habit_id" in quest_cols
    engine.dispose()

    assert _snapshot(db_url) == before, "existing quest and XP data must survive"


def test_dedup_key_is_unique_in_the_database(db_url):
    """The guarantee has to come from the schema, not only from Python."""
    from sqlalchemy.exc import IntegrityError

    cfg = _alembic_config(db_url)
    command.upgrade(cfg, "head")

    engine = create_engine(db_url)
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO quest_completions (quest_id, dedup_key, xp_awarded,"
            " stat_xp_awarded, completed_at, created_at)"
            " VALUES (1, 'quest:1:weekly:2026-07-27', 100, 0,"
            " '2026-07-27 10:00:00', '2026-07-27 10:00:00')"
        ))
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO quest_completions (quest_id, dedup_key, xp_awarded,"
                " stat_xp_awarded, completed_at, created_at)"
                " VALUES (1, 'quest:1:weekly:2026-07-27', 100, 0,"
                " '2026-07-27 11:00:00', '2026-07-27 11:00:00')"
            ))
        raise AssertionError("the unique index did not fire")
    except IntegrityError:
        pass
    finally:
        engine.dispose()


def test_quest_completions_downgrade_keeps_user_data(db_url):
    _seed_legacy_database(db_url)
    before = _snapshot(db_url)

    cfg = _alembic_config(db_url)
    command.stamp(cfg, "0001_baseline")
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0002_goals")

    tables = _tables(db_url)
    assert "quest_completions" not in tables
    assert "goals" in tables          # only this revision was rolled back
    assert _snapshot(db_url) == before


def test_full_downgrade_and_upgrade_round_trip(db_url):
    cfg = _alembic_config(db_url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    assert {"goals", "quest_completions", "hero_profile"} <= _tables(db_url)


# ---------------------------------------------------------------------------
# 0004 — goal pause intervals
# ---------------------------------------------------------------------------

def _seed_goal_database(url: str) -> None:
    """A database at revision 0003 with a goal and a rewarded quest."""
    cfg = _alembic_config(url)
    command.upgrade(cfg, "0003_quest_completions")

    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO hero_profile (name, level, total_xp) VALUES ('Hero', 4, 4200)"
        ))
        conn.execute(text(
            "INSERT INTO goals (slug, title, status, sort_order, created_at, updated_at)"
            " VALUES ('bestand', 'Bestandsziel', 'active', 0,"
            " '2026-01-01 00:00:00', '2026-01-01 00:00:00')"
        ))
        conn.execute(text(
            "INSERT INTO quests (slug, title, quest_type, period, target_value,"
            " current_value, xp_reward, attribute, active, repeatable, goal_id,"
            " is_milestone, sort_order)"
            " VALUES ('legacy-weekly', 'Legacy', 'manual', 'weekly', 3, 3, 100,"
            " 'Strength', 1, 1, 1, 0, 0)"
        ))
        conn.execute(text(
            "INSERT INTO quest_completions (quest_id, completed_at, xp_awarded,"
            " stat_xp_awarded, dedup_key, created_at)"
            " VALUES (1, '2026-07-15 10:00:00', 100, 0, 'quest:1:weekly:2026-07-13',"
            " '2026-07-15 10:00:00')"
        ))
    engine.dispose()


def _goal_snapshot(url: str) -> dict:
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            return {
                "goals": conn.execute(
                    text("SELECT slug, status FROM goals")).fetchall(),
                "completions": conn.execute(
                    text("SELECT quest_id, dedup_key FROM quest_completions")
                ).fetchall(),
                "hero": conn.execute(
                    text("SELECT total_xp FROM hero_profile")).scalar(),
            }
    finally:
        engine.dispose()


def test_pause_migration_runs_on_a_populated_database(db_url):
    _seed_goal_database(db_url)
    before = _goal_snapshot(db_url)

    command.upgrade(_alembic_config(db_url), "0004_goal_pause_intervals")

    assert "goal_pause_intervals" in _tables(db_url)
    assert _goal_snapshot(db_url) == before, "existing goals and history must survive"


def test_pause_migration_invents_no_history(db_url):
    """A goal paused before this revision gets no interval out of thin air."""
    _seed_goal_database(db_url)
    engine = create_engine(db_url)
    with engine.begin() as conn:
        conn.execute(text("UPDATE goals SET status='paused' WHERE slug='bestand'"))
    engine.dispose()

    command.upgrade(_alembic_config(db_url), "0004_goal_pause_intervals")

    engine = create_engine(db_url)
    try:
        with engine.connect() as conn:
            assert conn.execute(
                text("SELECT count(*) FROM goal_pause_intervals")).scalar() == 0
    finally:
        engine.dispose()


def test_open_pause_interval_is_unique_per_goal_in_the_database(db_url):
    """The partial unique index, not Python, is the last line of defence."""
    _seed_goal_database(db_url)
    command.upgrade(_alembic_config(db_url), "0004_goal_pause_intervals")

    engine = create_engine(db_url)
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO goal_pause_intervals (goal_id, started_at, created_at)"
                " VALUES (1, '2026-07-01 00:00:00', '2026-07-01 00:00:00')"
            ))
        with pytest.raises(Exception):
            with engine.begin() as conn:
                conn.execute(text(
                    "INSERT INTO goal_pause_intervals (goal_id, started_at, created_at)"
                    " VALUES (1, '2026-07-02 00:00:00', '2026-07-02 00:00:00')"
                ))
    finally:
        engine.dispose()


def test_closed_pause_intervals_may_repeat_per_goal(db_url):
    """Several finished breaks are normal — only the open one is unique."""
    _seed_goal_database(db_url)
    command.upgrade(_alembic_config(db_url), "0004_goal_pause_intervals")

    engine = create_engine(db_url)
    try:
        with engine.begin() as conn:
            for start, end in (
                ("2026-05-01 00:00:00", "2026-05-10 00:00:00"),
                ("2026-06-01 00:00:00", "2026-06-10 00:00:00"),
            ):
                conn.execute(text(
                    "INSERT INTO goal_pause_intervals (goal_id, started_at, ended_at,"
                    f" created_at) VALUES (1, '{start}', '{end}', '{start}')"
                ))
        with engine.connect() as conn:
            assert conn.execute(
                text("SELECT count(*) FROM goal_pause_intervals")).scalar() == 2
    finally:
        engine.dispose()


def test_pause_migration_downgrade_and_upgrade_again(db_url):
    _seed_goal_database(db_url)
    cfg = _alembic_config(db_url)
    command.upgrade(cfg, "0004_goal_pause_intervals")
    before = _goal_snapshot(db_url)

    command.downgrade(cfg, "0003_quest_completions")
    assert "goal_pause_intervals" not in _tables(db_url)
    assert _goal_snapshot(db_url) == before, "downgrade must not touch user data"

    command.upgrade(cfg, "0004_goal_pause_intervals")
    assert "goal_pause_intervals" in _tables(db_url)
    assert _goal_snapshot(db_url) == before


# ---------------------------------------------------------------------------
# 0005 — optional weekday planning for habits
# ---------------------------------------------------------------------------

def _seed_habit_database(url: str) -> None:
    """A database at revision 0004 with a habit and a real completion."""
    cfg = _alembic_config(url)
    command.upgrade(cfg, "0004_goal_pause_intervals")

    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO habits (title, active, recurrence, target_count,"
            " base_xp_reward, sort_order, created_at, updated_at)"
            " VALUES ('Bestands-Habit', 1, 'daily', 1, 25, 0,"
            " '2026-01-01 00:00:00', '2026-01-01 00:00:00')"
        ))
        conn.execute(text(
            "INSERT INTO habit_completions (habit_id, completed_at, xp_awarded,"
            " stat_xp_awarded) VALUES (1, '2026-01-05 00:00:00', 25, 0)"
        ))
    engine.dispose()


def _habit_snapshot(url: str) -> dict:
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            return {
                "habits": conn.execute(
                    text("SELECT title, active, recurrence FROM habits")).fetchall(),
                "completions": conn.execute(
                    text("SELECT habit_id, xp_awarded FROM habit_completions")
                ).fetchall(),
            }
    finally:
        engine.dispose()


def test_schedule_migration_runs_on_a_populated_database(db_url):
    _seed_habit_database(db_url)
    before = _habit_snapshot(db_url)

    command.upgrade(_alembic_config(db_url), "0005_habit_schedule_days")

    assert "habit_schedule_days" in _tables(db_url)
    assert _habit_snapshot(db_url) == before, "habits and completions must survive"


def test_schedule_migration_invents_no_plan(db_url):
    """Existing habits stay unplanned — no weekday is made up for them."""
    _seed_habit_database(db_url)
    command.upgrade(_alembic_config(db_url), "0005_habit_schedule_days")

    engine = create_engine(db_url)
    try:
        with engine.connect() as conn:
            assert conn.execute(
                text("SELECT count(*) FROM habit_schedule_days")).scalar() == 0
    finally:
        engine.dispose()


def test_several_weekdays_per_habit_are_allowed(db_url):
    _seed_habit_database(db_url)
    command.upgrade(_alembic_config(db_url), "0005_habit_schedule_days")

    engine = create_engine(db_url)
    try:
        with engine.begin() as conn:
            for day in (1, 3, 5):
                conn.execute(text(
                    "INSERT INTO habit_schedule_days (habit_id, iso_weekday)"
                    f" VALUES (1, {day})"
                ))
        with engine.connect() as conn:
            assert conn.execute(
                text("SELECT count(*) FROM habit_schedule_days")).scalar() == 3
    finally:
        engine.dispose()


def test_the_same_weekday_cannot_be_stored_twice(db_url):
    """The unique constraint, not Python, is the last line of defence."""
    _seed_habit_database(db_url)
    command.upgrade(_alembic_config(db_url), "0005_habit_schedule_days")

    engine = create_engine(db_url)
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO habit_schedule_days (habit_id, iso_weekday) VALUES (1, 2)"
            ))
        with pytest.raises(Exception):
            with engine.begin() as conn:
                conn.execute(text(
                    "INSERT INTO habit_schedule_days (habit_id, iso_weekday)"
                    " VALUES (1, 2)"
                ))
    finally:
        engine.dispose()


def test_schedule_migration_downgrade_and_upgrade_again(db_url):
    _seed_habit_database(db_url)
    cfg = _alembic_config(db_url)
    command.upgrade(cfg, "0005_habit_schedule_days")
    before = _habit_snapshot(db_url)

    command.downgrade(cfg, "0004_goal_pause_intervals")
    assert "habit_schedule_days" not in _tables(db_url)
    assert _habit_snapshot(db_url) == before, "downgrade must not touch user data"

    command.upgrade(cfg, "0005_habit_schedule_days")
    assert "habit_schedule_days" in _tables(db_url)
    assert _habit_snapshot(db_url) == before


def test_downgrade_keeps_habit_completions(db_url):
    """Dropping the plan table must never cascade into recorded history."""
    _seed_habit_database(db_url)
    cfg = _alembic_config(db_url)
    command.upgrade(cfg, "0005_habit_schedule_days")

    engine = create_engine(db_url)
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO habit_schedule_days (habit_id, iso_weekday) VALUES (1, 4)"
        ))
    engine.dispose()

    command.downgrade(cfg, "0004_goal_pause_intervals")

    engine = create_engine(db_url)
    try:
        with engine.connect() as conn:
            assert conn.execute(
                text("SELECT count(*) FROM habit_completions")).scalar() == 1
            assert conn.execute(text("SELECT count(*) FROM habits")).scalar() == 1
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# 0006 — optional learning metrics on a Japanese SAVE import
# ---------------------------------------------------------------------------

def _seed_save_import_database(url: str) -> None:
    """A database at revision 0005 with one imported SAVE."""
    cfg = _alembic_config(url)
    command.upgrade(cfg, "0005_habit_schedule_days")

    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO japanese_save_imports (save_date, streak, wanikani_level,"
            " bunpro_level, bunpro_points, source_character_level,"
            " source_level_xp, source_level_xp_cap, vocabulary_score,"
            " grammar_score, reading_score, listening_score, speaking_score,"
            " raw_save, normalized_hash, created_at, xp_awarded,"
            " stat_xp_awarded, classification)"
            " VALUES ('2026-07-31', 4, 1, 'N5', 5, 2, 433, 1000, 180, 250, 0, 0,"
            " 215, 'roh', 'hash-1', '2026-07-31 10:00:00', 0, 0, 'baseline')"
        ))
    engine.dispose()


def _save_snapshot(url: str) -> list:
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            return conn.execute(text(
                "SELECT save_date, wanikani_level, bunpro_level, bunpro_points,"
                " normalized_hash, xp_awarded FROM japanese_save_imports"
            )).fetchall()
    finally:
        engine.dispose()


def test_metric_migration_runs_on_a_populated_database(db_url):
    _seed_save_import_database(db_url)
    before = _save_snapshot(db_url)

    command.upgrade(_alembic_config(db_url), "0006_optional_learning_metrics")

    assert _save_snapshot(db_url) == before, "existing imports must be untouched"


def test_after_the_migration_a_metric_may_be_null(db_url):
    _seed_save_import_database(db_url)
    command.upgrade(_alembic_config(db_url), "0006_optional_learning_metrics")

    engine = create_engine(db_url)
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO japanese_save_imports (save_date, streak,"
                " wanikani_level, bunpro_level, bunpro_points,"
                " source_character_level, source_level_xp, source_level_xp_cap,"
                " vocabulary_score, grammar_score, reading_score,"
                " listening_score, speaking_score, raw_save, normalized_hash, created_at,"
                " xp_awarded, stat_xp_awarded, classification)"
                " VALUES ('2026-08-01', 5, NULL, NULL, NULL, 2, 433, 1000, 180,"
                " 250, 0, 0, 215, 'roh', 'hash-2', '2026-08-01 10:00:00', 0, 0, 'progress')"
            ))
        with engine.connect() as conn:
            row = conn.execute(text(
                "SELECT wanikani_level, bunpro_points FROM japanese_save_imports"
                " WHERE normalized_hash = 'hash-2'"
            )).one()
            assert row == (None, None)
    finally:
        engine.dispose()


def test_before_the_migration_a_null_metric_is_refused(db_url):
    """This is why the revision exists: NOT NULL could not record "not stated"."""
    _seed_save_import_database(db_url)

    engine = create_engine(db_url)
    try:
        with pytest.raises(Exception):
            with engine.begin() as conn:
                conn.execute(text(
                    "INSERT INTO japanese_save_imports (save_date, streak,"
                    " wanikani_level, bunpro_level, bunpro_points,"
                    " source_character_level, source_level_xp,"
                    " source_level_xp_cap, vocabulary_score, grammar_score,"
                    " reading_score, listening_score, speaking_score, raw_save,"
                    " normalized_hash, created_at, xp_awarded, stat_xp_awarded,"
                    " classification)"
                    " VALUES ('2026-08-01', 5, NULL, NULL, NULL, 2, 433, 1000,"
                    " 180, 250, 0, 0, 215, 'roh', 'hash-3', '2026-08-01 10:00:00', 0, 0, 'progress')"
                ))
    finally:
        engine.dispose()


def test_metric_migration_downgrade_and_upgrade_again(db_url):
    _seed_save_import_database(db_url)
    cfg = _alembic_config(db_url)
    command.upgrade(cfg, "0006_optional_learning_metrics")
    before = _save_snapshot(db_url)

    command.downgrade(cfg, "0005_habit_schedule_days")
    assert _save_snapshot(db_url) == before, "counted values must survive a downgrade"

    command.upgrade(cfg, "0006_optional_learning_metrics")
    assert _save_snapshot(db_url) == before


def test_downgrade_keeps_every_counted_value(db_url):
    """Only "not stated" becomes 0 — a counted number is never rewritten."""
    _seed_save_import_database(db_url)
    cfg = _alembic_config(db_url)
    command.upgrade(cfg, "0006_optional_learning_metrics")

    engine = create_engine(db_url)
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO japanese_save_imports (save_date, streak, wanikani_level,"
            " bunpro_level, bunpro_points, source_character_level, source_level_xp,"
            " source_level_xp_cap, vocabulary_score, grammar_score, reading_score,"
            " listening_score, speaking_score, raw_save, normalized_hash, created_at,"
            " xp_awarded, stat_xp_awarded, classification)"
            " VALUES ('2026-08-01', 5, NULL, NULL, NULL, 2, 433, 1000, 180, 250,"
            " 0, 0, 215, 'roh', 'hash-4', '2026-08-01 10:00:00', 0, 0, 'progress')"
        ))
    engine.dispose()

    command.downgrade(cfg, "0005_habit_schedule_days")

    engine = create_engine(db_url)
    try:
        with engine.connect() as conn:
            kept = conn.execute(text(
                "SELECT wanikani_level, bunpro_points FROM japanese_save_imports"
                " WHERE normalized_hash = 'hash-1'"
            )).one()
            filled = conn.execute(text(
                "SELECT wanikani_level, bunpro_points FROM japanese_save_imports"
                " WHERE normalized_hash = 'hash-4'"
            )).one()
            assert kept == (1, 5)
            assert filled == (0, 0)
    finally:
        engine.dispose()
