"""Tests for the database-facing Japanese SAVE import service."""

from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.japanese_import import import_save, preview_save
from app.japanese_saves import SaveParseError
from app.models import Base, HeroProfile, JapaneseSaveImport, XpEvent
from app.xp import recalc_level

from tests.test_japanese_saves import VALID_SAVE


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


def _save_text(*, level=2, xp=433, day="2026-07-31", session_xp=None) -> str:
    text = VALID_SAVE
    text = text.replace(
        "Charakter: Lv 2 (見習い) | 433 / 1000 XP",
        f"Charakter: Lv {level} (見習い) | {xp} / 1000 XP",
    )
    text = text.replace("Datum: 2026-07-31", f"Datum: {day}")
    if session_xp is not None:
        text = text.replace("=== END SAVE ===", f"Session-XP: {session_xp}\n=== END SAVE ===")
    return text


def _hero(db) -> HeroProfile:
    return db.query(HeroProfile).first()


# ---------------------------------------------------------------------------
# Baseline
# ---------------------------------------------------------------------------

def test_first_import_is_baseline_zero_xp(db):
    result = import_save(db, VALID_SAVE)
    assert result.created is not None
    assert result.xp_awarded == 0
    assert result.created.classification == "baseline"
    assert _hero(db).total_xp == 0
    assert db.query(XpEvent).count() == 0


def test_baseline_with_starting_credit(db):
    result = import_save(db, VALID_SAVE, accept_baseline_credit=True)
    assert result.xp_awarded == 433
    assert _hero(db).total_xp == 433
    assert db.query(XpEvent).count() == 1


def test_baseline_snapshot_stores_all_fields(db):
    import_save(db, VALID_SAVE)
    row = db.query(JapaneseSaveImport).one()
    assert row.save_date == date(2026, 7, 31)
    assert row.streak == 4
    assert row.wanikani_level == 1
    assert row.bunpro_level == "N5"
    assert row.bunpro_points == 5
    assert row.source_character_level == 2
    assert row.source_level_xp == 433
    assert row.source_level_xp_cap == 1000
    assert row.vocabulary_score == 180
    assert row.grammar_score == 250
    assert row.speaking_score == 215
    assert row.current_grammar_point == "これ"
    assert row.raw_save == VALID_SAVE
    assert len(row.normalized_hash) == 64


# ---------------------------------------------------------------------------
# Progress deltas
# ---------------------------------------------------------------------------

def test_same_level_progress_awards_delta(db):
    import_save(db, _save_text(xp=373, day="2026-07-30"))
    result = import_save(db, _save_text(xp=433, day="2026-07-31"))
    assert result.xp_awarded == 60
    assert _hero(db).total_xp == 60
    assert result.created.classification == "progress"


def test_level_up_awards_remaining_plus_new(db):
    import_save(db, _save_text(level=2, xp=990, day="2026-07-30"))
    result = import_save(db, _save_text(level=3, xp=20, day="2026-07-31"))
    assert result.xp_awarded == 30
    assert _hero(db).total_xp == 30


def test_level_jump_greater_than_one_awards_nothing(db):
    import_save(db, _save_text(level=2, xp=990, day="2026-07-30"))
    result = import_save(db, _save_text(level=5, xp=20, day="2026-07-31"))
    assert result.xp_awarded == 0
    assert result.created.classification == "warning"
    assert result.created.warning_text
    assert _hero(db).total_xp == 0


def test_negative_delta_never_deducts(db):
    import_save(db, _save_text(xp=800, day="2026-07-30"))
    result = import_save(db, _save_text(xp=433, day="2026-07-31"))
    assert result.xp_awarded == 0
    assert _hero(db).total_xp == 0
    assert result.created.classification == "warning"


def test_explicit_session_xp_is_used(db):
    import_save(db, _save_text(xp=373, day="2026-07-30"))
    result = import_save(db, _save_text(xp=433, day="2026-07-31", session_xp=42))
    assert result.xp_awarded == 42
    assert result.created.reported_session_xp == 42


def test_older_date_awards_zero(db):
    import_save(db, _save_text(xp=373, day="2026-07-31"))
    result = import_save(db, _save_text(xp=999, day="2026-07-01"))
    assert result.xp_awarded == 0
    assert result.created.classification == "historical"
    assert _hero(db).total_xp == 0


def test_same_date_multiple_imports_allowed(db):
    import_save(db, _save_text(xp=373, day="2026-07-31"))
    result = import_save(db, _save_text(xp=433, day="2026-07-31"))
    assert result.created is not None
    assert result.xp_awarded == 60
    assert db.query(JapaneseSaveImport).count() == 2


# ---------------------------------------------------------------------------
# Duplicates
# ---------------------------------------------------------------------------

def test_duplicate_creates_nothing(db):
    import_save(db, VALID_SAVE, accept_baseline_credit=True)
    before_xp = _hero(db).total_xp
    result = import_save(db, VALID_SAVE)
    assert result.is_duplicate
    assert result.created is None
    assert result.xp_awarded == 0
    assert db.query(JapaneseSaveImport).count() == 1
    assert _hero(db).total_xp == before_xp


def test_duplicate_detected_despite_formatting(db):
    import_save(db, VALID_SAVE)
    reformatted = VALID_SAVE.replace("\n", "\r\n").replace("Lv 1", "Lv. 1")
    result = import_save(db, reformatted)
    assert result.is_duplicate
    assert db.query(JapaneseSaveImport).count() == 1


# ---------------------------------------------------------------------------
# Hero level / transaction
# ---------------------------------------------------------------------------

def test_hero_level_recalculated(db):
    hero = _hero(db)
    hero.total_xp = 1200
    hero.level = recalc_level(1200)
    db.commit()

    import_save(db, _save_text(xp=373, day="2026-07-30"))
    import_save(db, _save_text(xp=433, day="2026-07-31"))

    hero = _hero(db)
    assert hero.total_xp == 1260
    assert hero.level == recalc_level(1260)


def test_xp_event_uses_agreed_identifiers(db):
    import_save(db, _save_text(xp=373, day="2026-07-30"))
    import_save(db, _save_text(xp=433, day="2026-07-31"))
    event = db.query(XpEvent).one()
    row = (
        db.query(JapaneseSaveImport)
        .order_by(JapaneseSaveImport.id.desc())
        .first()
    )
    assert event.event_type == "japanese_session"
    assert event.source == "japanese_save"
    assert event.source_id == str(row.id)
    assert event.attribute == "Japanese"
    assert event.xp == 60


def test_import_and_xp_event_roll_back_together(db, monkeypatch):
    import app.japanese_import as mod

    import_save(db, _save_text(xp=373, day="2026-07-30"))
    xp_before = _hero(db).total_xp
    rows_before = db.query(JapaneseSaveImport).count()

    def boom(*args, **kwargs):
        raise RuntimeError("commit failed")

    monkeypatch.setattr(db, "commit", boom)
    with pytest.raises(RuntimeError):
        import_save(db, _save_text(xp=433, day="2026-07-31"))

    monkeypatch.undo()
    assert db.query(JapaneseSaveImport).count() == rows_before
    assert db.query(XpEvent).count() == 0
    assert _hero(db).total_xp == xp_before


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------

def test_preview_persists_nothing(db):
    result = preview_save(db, VALID_SAVE)
    assert result.classification == "baseline"
    assert db.query(JapaneseSaveImport).count() == 0
    assert db.query(XpEvent).count() == 0


def test_preview_reports_delta_and_totals(db):
    import_save(db, _save_text(xp=373, day="2026-07-30"))
    result = preview_save(db, _save_text(xp=433, day="2026-07-31"))
    assert result.xp_delta == 60
    assert result.hero_total_xp_before == 0
    assert result.hero_total_xp_after == 60
    assert result.previous is not None


def test_preview_flags_duplicate(db):
    import_save(db, VALID_SAVE)
    result = preview_save(db, VALID_SAVE)
    assert result.is_duplicate
    assert result.classification == "duplicate"
    assert result.xp_delta == 0


def test_preview_rejects_invalid_input(db):
    with pytest.raises(SaveParseError):
        preview_save(db, "kein save")
