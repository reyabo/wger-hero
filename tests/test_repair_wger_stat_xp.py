"""The one-off repair that gives past wger workouts their strength stat XP.

The fixture reproduces the production fault: global workout XP exists, the
canonical strength stat does not. All ids are synthetic.
"""

from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import Base, HeroProfile, HeroStat, StatXpEvent, SyncEvent, XpEvent
from app.repair_wger_stat_xp import _main, apply_repair, plan_repair


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


def _broken(db, count=3, xp=100):
    """A database in the state the production audit found."""
    db.add(HeroProfile(name="Hero", level=5, total_xp=count * xp))
    for n in range(count):
        source_id = f"session-test-{n}"
        db.add(XpEvent(
            event_type="workout_complete",
            source="wger",
            source_id=source_id,
            xp=xp,
            attribute="Strength",
            title=f"Workout {n}",
            description="",
            created_at=datetime(2026, 8, 1 + n, 10, 0),
        ))
        db.add(SyncEvent(
            source="wger",
            source_id=source_id,
            source_hash=f"hash-{n}",
            synced_at=datetime(2026, 8, 1 + n, 10, 0),
            raw_summary="",
            xp_awarded=xp,
        ))
    db.commit()


def _strength(db) -> int:
    stat = db.query(HeroStat).filter(HeroStat.stat_key == "strength").first()
    return stat.xp if stat else 0


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------

def test_the_dry_run_writes_nothing(db):
    _broken(db, count=3)
    before = (db.query(HeroProfile).one().total_xp, db.query(XpEvent).count(),
              db.query(StatXpEvent).count(), _strength(db))

    plan_repair(db)

    assert (db.query(HeroProfile).one().total_xp, db.query(XpEvent).count(),
            db.query(StatXpEvent).count(), _strength(db)) == before


def test_the_dry_run_counts_the_candidates(db):
    _broken(db, count=3)
    plan = plan_repair(db)
    assert plan.candidates == 3
    assert plan.already_repaired == 0
    assert plan.missing == 3


def test_the_dry_run_sums_the_missing_strength_xp(db):
    _broken(db, count=3, xp=100)
    assert plan_repair(db).additional_strength_xp == 300


def test_the_dry_run_reports_no_global_xp_change(db):
    _broken(db, count=3)
    assert plan_repair(db).global_xp_change == 0


def test_the_report_states_what_it_found(db):
    _broken(db, count=2)
    text = "\n".join(plan_repair(db).report())
    assert "dry-run" in text
    assert "Kandidaten" in text and "2" in text
    assert "globale XP Änderung" in text


def test_the_amount_comes_from_the_event_not_from_a_constant(db):
    """A workout worth something other than 100 is credited with its own value."""
    _broken(db, count=2, xp=175)
    plan = plan_repair(db)
    assert plan.additional_strength_xp == 350


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------

def test_apply_adds_the_missing_strength_xp(db):
    _broken(db, count=3)
    apply_repair(db)
    assert _strength(db) == 300


def test_apply_creates_one_stat_event_per_workout(db):
    _broken(db, count=3)
    apply_repair(db)
    rows = db.query(StatXpEvent).filter(StatXpEvent.source == "wger").all()
    assert len(rows) == 3
    assert {r.stat_key for r in rows} == {"strength"}
    assert {r.source_id for r in rows} == {f"session-test-{n}" for n in range(3)}


def test_apply_keeps_the_original_timestamps(db):
    _broken(db, count=2)
    apply_repair(db)
    stamps = sorted(r.created_at for r in db.query(StatXpEvent).all())
    assert stamps == [datetime(2026, 8, 1, 10, 0), datetime(2026, 8, 2, 10, 0)]


def test_apply_never_touches_global_xp(db):
    _broken(db, count=3)
    before = db.query(HeroProfile).one().total_xp
    apply_repair(db)
    assert db.query(HeroProfile).one().total_xp == before


def test_apply_never_touches_the_xp_events(db):
    _broken(db, count=3)
    before = [(e.id, e.xp, e.attribute, e.source_id) for e in db.query(XpEvent).all()]
    apply_repair(db)
    assert [(e.id, e.xp, e.attribute, e.source_id) for e in db.query(XpEvent).all()] == before


def test_apply_never_touches_the_sync_events(db):
    _broken(db, count=3)
    before = [(e.source_id, e.source_hash, e.xp_awarded)
              for e in db.query(SyncEvent).all()]
    apply_repair(db)
    assert [(e.source_id, e.source_hash, e.xp_awarded)
            for e in db.query(SyncEvent).all()] == before


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

def test_a_second_apply_changes_nothing(db):
    _broken(db, count=3)
    apply_repair(db)
    second = apply_repair(db)

    assert second.additional_strength_xp == 0
    assert second.missing == 0
    assert second.already_repaired == 3
    assert _strength(db) == 300
    assert db.query(StatXpEvent).count() == 3


def test_a_dry_run_after_the_repair_reports_nothing_to_do(db):
    _broken(db, count=3)
    apply_repair(db)
    plan = plan_repair(db)
    assert plan.missing == 0
    assert "Nichts zu tun" in "\n".join(plan.report())


def test_a_partially_repaired_database_is_completed_not_doubled(db):
    _broken(db, count=3)
    # One session was already repaired by hand.
    db.add(StatXpEvent(
        stat_key="strength", xp=100, source="wger",
        source_id="session-test-1", title="Workout 1",
        created_at=datetime(2026, 8, 2, 10, 0),
    ))
    db.add(HeroStat(stat_key="strength", xp=100))
    db.commit()

    plan = apply_repair(db)

    assert plan.already_repaired == 1
    assert plan.missing == 2
    assert _strength(db) == 300
    assert db.query(StatXpEvent).count() == 3


# ---------------------------------------------------------------------------
# Conflicts
# ---------------------------------------------------------------------------

def test_two_stat_rows_for_one_session_are_a_conflict(db):
    _broken(db, count=2)
    for _ in range(2):
        db.add(StatXpEvent(
            stat_key="strength", xp=100, source="wger",
            source_id="session-test-0", title="Workout 0",
            created_at=datetime(2026, 8, 1, 10, 0),
        ))
    db.commit()

    plan = apply_repair(db)

    assert any("session-test-0" in c for c in plan.conflicts)
    assert plan.missing == 1                     # only the untouched one
    assert _strength(db) == 100                  # nothing credited for the conflict


def test_a_wrong_amount_is_a_conflict_not_a_top_up(db):
    _broken(db, count=1, xp=100)
    db.add(StatXpEvent(
        stat_key="strength", xp=40, source="wger",
        source_id="session-test-0", title="Workout 0",
        created_at=datetime(2026, 8, 1, 10, 0),
    ))
    db.add(HeroStat(stat_key="strength", xp=40))
    db.commit()

    plan = apply_repair(db)

    assert plan.conflicts
    assert plan.missing == 0
    assert _strength(db) == 40                   # untouched


def test_an_event_without_a_source_id_is_a_conflict(db):
    db.add(HeroProfile(name="Hero", level=1, total_xp=100))
    db.add(XpEvent(
        event_type="workout_complete", source="wger", source_id=None, xp=100,
        attribute="Strength", title="Workout", description="",
        created_at=datetime(2026, 8, 1, 10, 0),
    ))
    db.commit()

    plan = apply_repair(db)

    assert plan.conflicts
    assert db.query(StatXpEvent).count() == 0


def test_conflicts_are_reported_in_plain_text(db):
    _broken(db, count=1)
    db.add(StatXpEvent(
        stat_key="strength", xp=40, source="wger", source_id="session-test-0",
        title="x", created_at=datetime(2026, 8, 1, 10, 0),
    ))
    db.commit()
    text = "\n".join(plan_repair(db).report())
    assert "Konflikte" in text
    assert "session-test-0" in text


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------

def test_only_wger_workout_completions_are_candidates(db):
    db.add(HeroProfile(name="Hero", level=1, total_xp=300))
    db.add(XpEvent(event_type="workout_complete", source="wger",
                   source_id="session-test-0", xp=100, attribute="Strength",
                   title="w", description="", created_at=datetime(2026, 8, 1)))
    # A habit completion, and a conditioning bonus — neither is in scope.
    db.add(XpEvent(event_type="habit_complete", source="habit", source_id="7",
                   xp=100, attribute="Habit", title="h", description="",
                   created_at=datetime(2026, 8, 1)))
    db.add(XpEvent(event_type="conditioning_bonus", source="wger",
                   source_id="session-test-0", xp=25, attribute="Conditioning",
                   title="c", description="", created_at=datetime(2026, 8, 1)))
    db.commit()

    plan = apply_repair(db)

    assert plan.candidates == 1
    assert _strength(db) == 100
    assert db.query(StatXpEvent).count() == 1


def test_an_empty_database_is_a_no_op(db):
    plan = apply_repair(db)
    assert plan.candidates == 0
    assert plan.missing == 0
    assert db.query(StatXpEvent).count() == 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _run_cli(monkeypatch, db, argv):
    import app.database as database
    import app.repair_wger_stat_xp as module

    monkeypatch.setattr(database, "init_db", lambda: None)

    def fake_get_db():
        yield db

    monkeypatch.setattr(database, "get_db", fake_get_db)
    return module._main(argv)


def test_the_cli_dry_run_writes_nothing(monkeypatch, db, capsys):
    _broken(db, count=3)
    assert _run_cli(monkeypatch, db, ["--dry-run"]) == 0
    assert db.query(StatXpEvent).count() == 0
    assert "dry-run" in capsys.readouterr().out


def test_the_cli_applies_the_repair(monkeypatch, db, capsys):
    _broken(db, count=3)
    assert _run_cli(monkeypatch, db, ["--apply"]) == 0
    assert _strength(db) == 300
    assert "Angewendet" in capsys.readouterr().out


def test_the_cli_needs_an_explicit_mode(monkeypatch, db, capsys):
    _broken(db, count=1)
    assert _run_cli(monkeypatch, db, []) == 1
    assert db.query(StatXpEvent).count() == 0
    assert "Usage" in capsys.readouterr().out


def test_the_cli_prints_no_secrets(monkeypatch, db, capsys):
    _broken(db, count=2)
    _run_cli(monkeypatch, db, ["--apply"])
    out = capsys.readouterr().out.lower()
    for word in ("token", "secret", "password", "database_url", "wger_base_url"):
        assert word not in out
