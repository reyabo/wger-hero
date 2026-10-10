"""The read-only consistency check over the append-only ledgers.

Each test breaks exactly one invariant and asserts that the checker notices it
with the right severity — and, just as importantly, that a healthy database
produces no findings at all.
"""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.check_consistency import ERROR, NOTE, _main, check_consistency
from app.models import (
    Base,
    Goal,
    GoalPauseInterval,
    Habit,
    HabitCompletion,
    HabitScheduleDay,
    HeroProfile,
    HeroStat,
    JapaneseSaveImport,
    Quest,
    QuestCompletion,
    StatXpEvent,
    SyncEvent,
    XpEvent,
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
    yield session
    session.close()


def _healthy(db):
    """A small but internally consistent database."""
    db.add(HeroProfile(name="Hero", level=1, total_xp=120))
    db.add(XpEvent(event_type="habit_complete", source="habit", source_id="1",
                   xp=120, attribute="Habit", title="Lesen", description="",
                   created_at=datetime(2026, 8, 1)))
    db.add(HeroStat(stat_key="knowledge", xp=30))
    db.add(StatXpEvent(stat_key="knowledge", xp=30, source="habit", source_id="1",
                       title="Lesen", created_at=datetime(2026, 8, 1)))
    habit = Habit(title="Lesen", active=True, recurrence="daily", target_count=1,
                  base_xp_reward=120)
    db.add(habit)
    db.commit()
    db.add(HabitCompletion(habit_id=habit.id, completed_at=datetime(2026, 8, 1),
                           xp_awarded=120, stat_xp_awarded=30))
    db.add(HabitScheduleDay(habit_id=habit.id, iso_weekday=3))
    db.commit()
    return habit


def _messages(report, severity=None):
    return " ".join(
        f.message for f in report.findings
        if severity is None or f.severity == severity
    )


# ---------------------------------------------------------------------------
# A healthy database
# ---------------------------------------------------------------------------

def test_a_healthy_database_has_no_findings(db):
    _healthy(db)
    report = check_consistency(db)
    assert report.findings == []
    assert report.ok


def test_an_empty_database_has_no_findings(db):
    report = check_consistency(db)
    assert report.findings == []
    assert report.ok


def test_every_check_actually_runs(db):
    _healthy(db)
    from app.check_consistency import CHECKS

    assert check_consistency(db).checks_run == len(CHECKS)


def test_the_report_says_nothing_changed(db):
    _healthy(db)
    assert "Es wurde nichts verändert." in check_consistency(db).render()


# ---------------------------------------------------------------------------
# Errors — invariants that are true by construction
# ---------------------------------------------------------------------------

def test_global_xp_out_of_step_is_an_error(db):
    _healthy(db)
    db.query(HeroProfile).one().total_xp = 500
    db.commit()

    report = check_consistency(db)
    assert not report.ok
    assert "Globale XP" in " ".join(f.check for f in report.errors)
    assert "500" in _messages(report, ERROR)


def test_a_missing_xp_event_is_an_error(db):
    """Exactly the shape of the wger strength bug: aggregate without ledger."""
    _healthy(db)
    db.query(XpEvent).delete()
    db.commit()
    assert not check_consistency(db).ok


def test_stat_xp_out_of_step_is_an_error(db):
    _healthy(db)
    db.query(HeroStat).filter(HeroStat.stat_key == "knowledge").one().xp = 999
    db.commit()

    report = check_consistency(db)
    assert not report.ok
    assert "knowledge" in _messages(report, ERROR)


def test_a_stat_event_without_a_total_is_an_error(db):
    """The production fault: StatXpEvents exist, HeroStat never got them."""
    db.add(HeroProfile(name="Hero", level=1, total_xp=0))
    db.add(StatXpEvent(stat_key="strength", xp=100, source="wger",
                       source_id="session-x", title="Workout",
                       created_at=datetime(2026, 8, 1)))
    db.commit()

    report = check_consistency(db)
    assert not report.ok
    assert "strength" in _messages(report, ERROR)


def test_a_stat_total_without_events_is_an_error(db):
    db.add(HeroProfile(name="Hero", level=1, total_xp=0))
    db.add(HeroStat(stat_key="strength", xp=6300))
    db.commit()
    assert not check_consistency(db).ok


def test_each_stat_is_reported_separately(db):
    db.add(HeroProfile(name="Hero", level=1, total_xp=0))
    db.add(HeroStat(stat_key="strength", xp=10))
    db.add(HeroStat(stat_key="knowledge", xp=20))
    db.commit()

    report = check_consistency(db)
    assert len(report.errors) == 2


def test_a_japanese_import_that_disagrees_with_its_event_is_an_error(db):
    db.add(HeroProfile(name="Hero", level=1, total_xp=40))
    record = JapaneseSaveImport(
        save_date=datetime(2026, 8, 1).date(), streak=1, source_character_level=2,
        source_level_xp=0, source_level_xp_cap=1000, vocabulary_score=0,
        grammar_score=0, reading_score=0, listening_score=0, speaking_score=0,
        raw_save="x", normalized_hash="h1", classification="progress",
        xp_awarded=40, stat_xp_awarded=0, created_at=datetime(2026, 8, 1),
    )
    db.add(record)
    db.commit()
    db.add(XpEvent(event_type="japanese_session", source="japanese",
                   source_id=str(record.id), xp=15, attribute="Japanese",
                   title="Session", description="", created_at=datetime(2026, 8, 1)))
    db.commit()

    report = check_consistency(db)
    assert any("Japanisch" in f.check for f in report.errors)


def test_a_baseline_import_without_stat_xp_is_not_flagged(db):
    """A baseline import deliberately awards no stat XP — that is correct."""
    db.add(HeroProfile(name="Hero", level=1, total_xp=0))
    record = JapaneseSaveImport(
        save_date=datetime(2026, 8, 1).date(), streak=1, source_character_level=2,
        source_level_xp=0, source_level_xp_cap=1000, vocabulary_score=0,
        grammar_score=0, reading_score=0, listening_score=0, speaking_score=0,
        raw_save="x", normalized_hash="h1", classification="baseline",
        xp_awarded=0, stat_xp_awarded=0, created_at=datetime(2026, 8, 1),
    )
    db.add(record)
    db.commit()

    assert check_consistency(db).ok


def test_an_invalid_weekday_is_an_error(db):
    habit = _healthy(db)
    db.add(HabitScheduleDay(habit_id=habit.id, iso_weekday=9))
    db.commit()

    report = check_consistency(db)
    assert any("Wochenplanung" in f.check for f in report.errors)
    assert "9" in _messages(report, ERROR)


def test_an_orphan_completion_is_an_error(db):
    _healthy(db)
    db.add(HabitCompletion(habit_id=9999, completed_at=datetime(2026, 8, 1),
                           xp_awarded=0, stat_xp_awarded=0))
    db.commit()

    report = check_consistency(db)
    assert any("Verwaiste Abschlüsse" in f.check for f in report.errors)


def test_an_orphan_quest_completion_is_an_error(db):
    _healthy(db)
    db.add(QuestCompletion(quest_id=9999, completed_at=datetime(2026, 8, 1),
                           dedup_key="quest:9999:weekly:2026-08-01", xp_awarded=0))
    db.commit()

    report = check_consistency(db)
    assert any("Quest-Abschlüsse" in f.check for f in report.errors)


# ---------------------------------------------------------------------------
# Advisory notes — legitimate data can look like this
# ---------------------------------------------------------------------------

def test_a_stale_level_is_only_a_note(db):
    """Unlocking an achievement adds XP without recalculating the level."""
    _healthy(db)
    db.query(HeroProfile).one().level = 7
    db.commit()

    report = check_consistency(db)
    assert report.ok                       # not an error
    assert any("Heldenlevel" in f.check for f in report.notes)


def test_a_sync_total_that_disagrees_is_only_a_note(db):
    db.add(HeroProfile(name="Hero", level=1, total_xp=100))
    db.add(XpEvent(event_type="workout_complete", source="wger",
                   source_id="session-x", xp=100, attribute="Strength",
                   title="Workout", description="", created_at=datetime(2026, 8, 1)))
    db.add(SyncEvent(source="wger", source_id="session-x", source_hash="h",
                     synced_at=datetime(2026, 8, 1), raw_summary="", xp_awarded=999))
    db.commit()

    report = check_consistency(db)
    assert report.ok
    assert any("wger-Sync" in f.check for f in report.notes)


def test_historical_over_completion_is_only_a_note(db):
    """Completions from before the period rule must not read as a defect."""
    habit = _healthy(db)
    for _ in range(3):
        db.add(HabitCompletion(habit_id=habit.id,
                               completed_at=datetime(2026, 8, 1, 10, 0),
                               xp_awarded=0, stat_xp_awarded=0))
    db.commit()

    report = check_consistency(db)
    assert report.ok
    assert any("Abschlussgrenze" in f.check for f in report.notes)


def test_a_completion_within_the_allowance_is_not_flagged(db):
    habit = _healthy(db)
    habit.target_count = 5
    db.commit()
    assert not any("Abschlussgrenze" in f.check for f in check_consistency(db).notes)


def test_a_paused_goal_without_an_interval_is_only_a_note(db):
    _healthy(db)
    db.add(Goal(slug="ziel", title="Ziel", status="paused", sort_order=0,
                created_at=datetime(2026, 8, 1), updated_at=datetime(2026, 8, 1)))
    db.commit()

    report = check_consistency(db)
    assert report.ok
    assert any("Pausenhistorie" in f.check for f in report.notes)


def test_a_paused_goal_with_an_interval_is_not_flagged(db):
    _healthy(db)
    goal = Goal(slug="ziel", title="Ziel", status="paused", sort_order=0,
                created_at=datetime(2026, 8, 1), updated_at=datetime(2026, 8, 1))
    db.add(goal)
    db.commit()
    db.add(GoalPauseInterval(goal_id=goal.id, started_at=datetime(2026, 8, 2),
                             created_at=datetime(2026, 8, 2)))
    db.commit()

    assert not any("Pausenhistorie" in f.check for f in check_consistency(db).notes)


# ---------------------------------------------------------------------------
# It must never write
# ---------------------------------------------------------------------------

def test_the_check_writes_nothing(db):
    habit = _healthy(db)
    before = (
        db.query(HeroProfile).one().total_xp,
        db.query(XpEvent).count(),
        db.query(StatXpEvent).count(),
        db.query(HabitCompletion).count(),
        db.query(HeroStat).filter(HeroStat.stat_key == "knowledge").one().xp,
    )

    for _ in range(3):
        check_consistency(db)

    assert (
        db.query(HeroProfile).one().total_xp,
        db.query(XpEvent).count(),
        db.query(StatXpEvent).count(),
        db.query(HabitCompletion).count(),
        db.query(HeroStat).filter(HeroStat.stat_key == "knowledge").one().xp,
    ) == before


def test_the_check_repairs_nothing(db):
    """Finding a problem must not tempt it into fixing one."""
    _healthy(db)
    db.query(HeroProfile).one().total_xp = 500
    db.commit()

    check_consistency(db)

    assert db.query(HeroProfile).one().total_xp == 500


def test_the_module_never_commits_or_deletes():
    import ast
    from pathlib import Path

    source = Path(__file__).resolve().parent.parent / "app" / "check_consistency.py"
    tree = ast.parse(source.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in ("commit", "add", "delete", "flush", "merge"), \
                f"check_consistency must not call {node.func.attr}()"


def test_database_enforced_uniqueness_is_not_re_checked():
    """Re-checking what the schema guarantees would suggest it might fail.

    The unique dedup_key, the unique normalized_hash and the partial unique
    index on the open pause interval cannot be violated while the file is
    intact, so there is deliberately no check for them.
    """
    from app.check_consistency import CHECKS, __doc__ as module_doc

    names = {c.__name__ for c in CHECKS}
    assert "check_dedup_keys" not in names
    assert "check_normalized_hash" not in names
    assert "check_open_pause_intervals" not in names
    # The omission is explained rather than silent.
    assert "dedup_key" in module_doc and "normalized_hash" in module_doc


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _run_cli(monkeypatch, db, argv):
    import app.check_consistency as module
    import app.database as database

    monkeypatch.setattr(database, "init_db", lambda: None)

    def fake_get_db():
        yield db

    monkeypatch.setattr(database, "get_db", fake_get_db)
    return module._main(argv)


def test_the_cli_exits_zero_when_everything_agrees(monkeypatch, db, capsys):
    _healthy(db)
    assert _run_cli(monkeypatch, db, []) == 0
    assert "Konsistenzprüfung" in capsys.readouterr().out


def test_the_cli_exits_non_zero_on_a_real_defect(monkeypatch, db, capsys):
    _healthy(db)
    db.query(HeroProfile).one().total_xp = 500
    db.commit()

    assert _run_cli(monkeypatch, db, []) == 2
    assert "FEHLER" in capsys.readouterr().out


def test_the_cli_stays_zero_for_advisory_notes_only(monkeypatch, db, capsys):
    """A cron job must not page anyone over expected historical data."""
    _healthy(db)
    db.query(HeroProfile).one().level = 7
    db.commit()

    assert _run_cli(monkeypatch, db, []) == 0


def test_quiet_prints_nothing_when_healthy(monkeypatch, db, capsys):
    _healthy(db)
    assert _run_cli(monkeypatch, db, ["--quiet"]) == 0
    assert capsys.readouterr().out == ""


def test_quiet_still_prints_a_defect(monkeypatch, db, capsys):
    _healthy(db)
    db.query(HeroProfile).one().total_xp = 500
    db.commit()

    assert _run_cli(monkeypatch, db, ["--quiet"]) == 2
    assert "FEHLER" in capsys.readouterr().out


def test_an_unknown_argument_is_refused(monkeypatch, db, capsys):
    assert _run_cli(monkeypatch, db, ["--repair"]) == 1
    assert "Usage" in capsys.readouterr().out


def test_the_output_carries_no_secrets(monkeypatch, db, capsys):
    _healthy(db)
    _run_cli(monkeypatch, db, [])
    out = capsys.readouterr().out.lower()
    for word in ("token", "secret", "password", "database_url", "wger_base_url"):
        assert word not in out
