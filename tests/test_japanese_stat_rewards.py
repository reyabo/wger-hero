"""Import-service tests for deterministic rewards, stat XP and the legacy habit."""

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.japanese_import import import_save, preview_save
from app.models import Base, Habit, HabitCompletion, HeroProfile, HeroStat, JapaneseSaveImport, StatXpEvent, XpEvent
from app.rewards import calculate_stat_rewards
from app.seed_defaults import (
    DEFAULT_HABITS,
    LEGACY_JAPANESE_HABIT_TITLE,
    find_legacy_japanese_habit,
    seed_default_habits,
)

from tests.test_japanese_rewards import save_with


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


def hero(db) -> HeroProfile:
    return db.query(HeroProfile).first()


def stat_totals(db) -> dict[str, int]:
    return {s.stat_key: s.xp for s in db.query(HeroStat).all()}


def seed_baseline(db, day="2026-07-01"):
    """Consume the baseline so later imports are real sessions."""
    return import_save(db, save_with(mode="STATUS", completion="vollständig", day=day))


# ---------------------------------------------------------------------------
# 12./13./14. Stat distribution
# ---------------------------------------------------------------------------

def test_start_session_awards_28_8_4(db):
    seed_baseline(db)
    result = import_save(db, save_with(mode="START", completion="vollständig", day="2026-07-02"))
    assert result.xp_awarded == 40
    assert stat_totals(db) == {"knowledge": 28, "discipline": 8, "technique": 4}
    assert hero(db).total_xp == 40


def test_boss_session_awards_56_16_8(db):
    seed_baseline(db)
    result = import_save(db, save_with(mode="BOSS", completion="vollständig", day="2026-07-02"))
    assert result.xp_awarded == 80
    assert stat_totals(db) == {"knowledge": 56, "discipline": 16, "technique": 8}


def test_mini_session_awards_10_3_2(db):
    seed_baseline(db)
    import_save(db, save_with(mode="MINI", completion="vollständig", day="2026-07-02"))
    assert stat_totals(db) == {"knowledge": 10, "discipline": 3, "technique": 2}


@pytest.mark.parametrize("mode", ["MINI", "START", "BOSS"])
def test_stat_xp_sum_equals_global_xp(db, mode):
    seed_baseline(db)
    result = import_save(db, save_with(mode=mode, completion="vollständig", day="2026-07-02"))
    assert sum(stat_totals(db).values()) == result.xp_awarded
    assert result.created.stat_xp_awarded == result.xp_awarded


def test_no_physical_or_creative_stats_are_touched(db):
    seed_baseline(db)
    import_save(db, save_with(mode="BOSS", completion="vollständig", day="2026-07-02"))
    forbidden = {"strength", "endurance", "dexterity", "mobility",
                 "body_control", "creativity", "recovery"}
    assert forbidden.isdisjoint(stat_totals(db))


def test_stat_events_share_source_and_id(db):
    seed_baseline(db)
    result = import_save(db, save_with(mode="START", completion="vollständig", day="2026-07-02"))
    events = db.query(StatXpEvent).all()
    assert len(events) == 3
    for ev in events:
        assert ev.source == "japanese_save"
        assert ev.source_id == str(result.created.id)


# ---------------------------------------------------------------------------
# 9./10. Zero-reward sessions
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("completion", ["abgebrochen", "keine Leistung"])
def test_zero_sessions_award_nothing(db, completion):
    seed_baseline(db)
    result = import_save(db, save_with(mode="BOSS", completion=completion, day="2026-07-02"))
    assert result.xp_awarded == 0
    assert stat_totals(db) == {}
    assert db.query(StatXpEvent).count() == 0
    assert db.query(XpEvent).count() == 0


def test_status_mode_awards_nothing(db):
    seed_baseline(db)
    result = import_save(db, save_with(mode="STATUS", completion="vollständig", day="2026-07-02"))
    assert result.xp_awarded == 0
    assert stat_totals(db) == {}


def test_warning_case_stores_snapshot_without_reward(db):
    seed_baseline(db)
    result = import_save(db, save_with(mode="TURBO", completion="vollständig", day="2026-07-02"))
    assert result.created is not None
    assert result.created.reward_calculation == "warning"
    assert result.xp_awarded == 0
    assert stat_totals(db) == {}
    assert result.created.warning_text


# ---------------------------------------------------------------------------
# 11./20. Baseline, historical, duplicate
# ---------------------------------------------------------------------------

def test_first_import_is_baseline_without_any_xp(db):
    result = import_save(db, save_with(mode="BOSS", completion="vollständig"))
    assert result.xp_awarded == 0
    assert result.created.reward_calculation == "baseline"
    assert hero(db).total_xp == 0
    assert stat_totals(db) == {}


def test_baseline_credit_gives_global_xp_but_no_stat_xp(db):
    result = import_save(
        db, save_with(mode="BOSS", completion="vollständig"), accept_baseline_credit=True
    )
    assert result.xp_awarded == 433
    assert hero(db).total_xp == 433
    assert stat_totals(db) == {}          # explicitly no attribute XP
    assert db.query(StatXpEvent).count() == 0


def test_historical_import_awards_nothing(db):
    seed_baseline(db, day="2026-07-10")
    result = import_save(db, save_with(mode="BOSS", completion="vollständig", day="2026-07-01"))
    assert result.created.reward_calculation == "historical"
    assert result.xp_awarded == 0
    assert stat_totals(db) == {}


def test_duplicate_creates_no_further_events(db):
    seed_baseline(db)
    text_ = save_with(mode="START", completion="vollständig", day="2026-07-02")
    import_save(db, text_)
    before = (hero(db).total_xp, dict(stat_totals(db)), db.query(StatXpEvent).count())

    result = import_save(db, text_)
    assert result.is_duplicate
    assert result.created is None
    assert db.query(JapaneseSaveImport).count() == 2
    assert (hero(db).total_xp, dict(stat_totals(db)), db.query(StatXpEvent).count()) == before


# ---------------------------------------------------------------------------
# 16./18. Reported Session-XP and legacy fallback
# ---------------------------------------------------------------------------

def test_reported_session_xp_is_stored_but_not_used(db):
    seed_baseline(db)
    result = import_save(
        db, save_with(mode="START", completion="vollständig", session_xp=60, day="2026-07-02")
    )
    assert result.created.reported_session_xp == 60
    assert result.xp_awarded == 40
    assert stat_totals(db) == {"knowledge": 28, "discipline": 8, "technique": 4}


def test_legacy_save_awards_global_xp_but_no_stat_xp(db):
    """Backward compatibility only: the amount still comes from the coach's
    progress bar, so it must not reach the radar."""
    import_save(db, save_with(xp=373, day="2026-07-01"))          # baseline
    result = import_save(db, save_with(xp=433, day="2026-07-02"))  # legacy delta = 60
    assert result.created.reward_calculation == "legacy_level_delta"
    assert result.xp_awarded == 60
    assert hero(db).total_xp == 60                 # global XP still granted
    assert stat_totals(db) == {}                   # but no attribute XP
    assert db.query(StatXpEvent).count() == 0
    assert result.created.stat_xp_awarded == 0


def test_legacy_save_with_session_xp_also_awards_no_stat_xp(db):
    import_save(db, save_with(xp=373, day="2026-07-01"))
    result = import_save(db, save_with(xp=433, day="2026-07-02", session_xp=42))
    assert result.created.reward_calculation == "legacy_level_delta"
    assert result.xp_awarded == 42
    assert hero(db).total_xp == 42
    assert stat_totals(db) == {}


def test_legacy_level_up_awards_no_stat_xp(db):
    import_save(db, save_with(level=2, xp=990, day="2026-07-01"))
    result = import_save(db, save_with(level=3, xp=20, day="2026-07-02"))
    assert result.xp_awarded == 30
    assert stat_totals(db) == {}


def test_only_deterministic_sessions_move_the_radar(db):
    """A legacy and a deterministic session of equal global XP differ on stats."""
    # legacy: +40 global via the bar, no stats
    import_save(db, save_with(xp=100, day="2026-07-01"))
    import_save(db, save_with(xp=140, day="2026-07-02"))
    assert hero(db).total_xp == 40
    assert stat_totals(db) == {}

    # deterministic START: also +40 global, but this one splits
    import_save(db, save_with(mode="START", completion="vollständig", day="2026-07-03"))
    assert hero(db).total_xp == 80
    assert stat_totals(db) == calculate_stat_rewards("knowledge_learning", 40)


def test_competence_deltas_do_not_change_stored_reward(db):
    seed_baseline(db)
    text_ = save_with(mode="START", completion="vollständig", day="2026-07-02").replace(
        "語彙 180 | 文法 250 | 読解 0 | 聴解 0 | 会話 215",
        "語彙 999 | 文法 999 | 読解 999 | 聴解 999 | 会話 999",
    )
    result = import_save(db, text_)
    assert result.xp_awarded == 40
    assert result.created.vocabulary_score == 999   # snapshot still records them


# ---------------------------------------------------------------------------
# 17. Preview surfaces the mismatch
# ---------------------------------------------------------------------------

def test_preview_reports_mismatch(db):
    seed_baseline(db)
    result = preview_save(
        db, save_with(mode="START", completion="vollständig", session_xp=60, day="2026-07-02")
    )
    assert result.xp_delta == 40
    assert result.delta.reported_mismatch
    assert "60" in result.warning and "40" in result.warning
    assert result.stat_rewards == {"knowledge": 28, "discipline": 8, "technique": 4}


def test_preview_persists_nothing(db):
    seed_baseline(db)
    before = db.query(JapaneseSaveImport).count()
    preview_save(db, save_with(mode="BOSS", completion="vollständig", day="2026-07-02"))
    assert db.query(JapaneseSaveImport).count() == before
    assert db.query(StatXpEvent).count() == 0


# ---------------------------------------------------------------------------
# 21. Rollback covers snapshot, global XP and stat XP
# ---------------------------------------------------------------------------

def test_rollback_covers_snapshot_global_and_stat_xp(db, monkeypatch):
    seed_baseline(db)
    rows = db.query(JapaneseSaveImport).count()
    xp_before = hero(db).total_xp

    def boom(*a, **kw):
        raise RuntimeError("commit failed")

    monkeypatch.setattr(db, "commit", boom)
    with pytest.raises(RuntimeError):
        import_save(db, save_with(mode="BOSS", completion="vollständig", day="2026-07-02"))
    monkeypatch.undo()

    assert db.query(JapaneseSaveImport).count() == rows
    assert db.query(XpEvent).count() == 0
    assert db.query(StatXpEvent).count() == 0
    assert db.query(HeroStat).count() == 0
    assert hero(db).total_xp == xp_before


# ---------------------------------------------------------------------------
# 23./24. Legacy "Japanisch lernen" habit
# ---------------------------------------------------------------------------

def test_japanese_habit_is_no_longer_seeded(db):
    assert all(h["title"] != LEGACY_JAPANESE_HABIT_TITLE for h in DEFAULT_HABITS)
    seed_default_habits(db)
    titles = {h.title for h in db.query(Habit).all()}
    assert LEGACY_JAPANESE_HABIT_TITLE not in titles
    assert "30 Minuten lesen" in titles          # other defaults still seeded


def test_finder_detects_active_legacy_habit(db):
    db.add(Habit(title=LEGACY_JAPANESE_HABIT_TITLE, active=True, base_xp_reward=50))
    db.commit()
    assert find_legacy_japanese_habit(db) is not None


def test_finder_ignores_archived_habit(db):
    db.add(Habit(title=LEGACY_JAPANESE_HABIT_TITLE, active=False, base_xp_reward=50))
    db.commit()
    assert find_legacy_japanese_habit(db) is None


def test_finder_ignores_similarly_named_user_habits(db):
    for title in ["Japanisch lernen (eigenes)", "japanisch lernen", "Japanisch"]:
        db.add(Habit(title=title, active=True, base_xp_reward=20))
    db.commit()
    assert find_legacy_japanese_habit(db) is None


def test_archiving_keeps_history_and_other_habits(db):
    from app.habits import archive_habit

    legacy = Habit(title=LEGACY_JAPANESE_HABIT_TITLE, active=True, base_xp_reward=50)
    other = Habit(title="Mobility 10 Minuten", active=True, base_xp_reward=25)
    db.add_all([legacy, other])
    db.commit()
    db.add(HabitCompletion(habit_id=legacy.id, xp_awarded=50))
    db.commit()

    archive_habit(db, legacy)
    db.refresh(legacy); db.refresh(other)
    assert legacy.active is False
    assert other.active is True           # untouched
    assert db.query(HabitCompletion).count() == 1


def test_archive_never_deletes_even_without_history(db):
    """The archive action must keep the row, unlike delete_or_archive_habit()."""
    from app.habits import archive_habit

    habit = Habit(title=LEGACY_JAPANESE_HABIT_TITLE, active=True, base_xp_reward=50)
    db.add(habit); db.commit()
    archive_habit(db, habit)
    assert db.query(Habit).filter(Habit.title == LEGACY_JAPANESE_HABIT_TITLE).count() == 1
    assert habit.active is False


# ---------------------------------------------------------------------------
# 22. New columns are added additively to an existing table
# ---------------------------------------------------------------------------

def test_new_columns_are_added_to_existing_table(tmp_path, monkeypatch):
    """Simulate a database created by PR #8, then migrate it."""
    import app.config as cfg
    import app.database as dbmod

    db_file = tmp_path / "legacy.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file}")
    monkeypatch.setenv("WGER_BASE_URL", "https://wger.example.com")
    cfg._settings = None
    dbmod._engine = None
    dbmod._SessionLocal = None

    new_cols = [c[0] for c in dbmod._ADDED_COLUMNS["japanese_save_imports"]]

    # Build the table WITHOUT the new columns, as PR #8 shipped it.
    engine = dbmod._get_engine()
    table = Base.metadata.tables["japanese_save_imports"]
    ddl = [c for c in table.columns if c.name not in new_cols]
    cols_sql = ", ".join(f"{c.name} {c.type.compile(engine.dialect)}" for c in ddl)
    with engine.begin() as conn:
        conn.execute(text(f"CREATE TABLE japanese_save_imports ({cols_sql})"))
    present = {c["name"] for c in inspect(engine).get_columns("japanese_save_imports")}
    assert not (set(new_cols) & present), "precondition: new columns absent"

    dbmod.init_db()

    present = {c["name"] for c in inspect(engine).get_columns("japanese_save_imports")}
    for col in new_cols:
        assert col in present, f"{col} was not added additively"

    dbmod._engine = None
    dbmod._SessionLocal = None
    cfg._settings = None
