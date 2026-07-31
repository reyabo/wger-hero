"""
Database-facing import service for Japanese coach SAVE snapshots.

The parsing / delta rules live in ``app/japanese_saves.py`` and stay
database-free. This module is the thin layer that reads the previous snapshot,
writes the new one, and books the derived global XP — all in one transaction.

Never deletes or rewrites an existing record: a correction is a new import, and
the ledger (``XpEvent``) is only ever appended to.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

from sqlalchemy.orm import Session

from app.japanese_saves import (
    CALC_DUPLICATE,
    CLASSIFICATION_DUPLICATE,
    JAPANESE_STAT_CATEGORY,
    DeltaResult,
    JapaneseSave,
    PreviousState,
    calculate_delta,
    parse_save,
)
from app.models import HeroProfile, JapaneseSaveImport, XpEvent
from app.rewards import calculate_stat_rewards
from app.stats import award_stat_xp, serialize_stat_rewards
from app.xp import recalc_level

logger = logging.getLogger(__name__)

XP_EVENT_TYPE = "japanese_session"
XP_SOURCE = "japanese_save"
XP_ATTRIBUTE = "Japanese"


@dataclass
class PreviewResult:
    """Everything the preview screen needs — computed, nothing persisted."""

    save: JapaneseSave
    delta: DeltaResult
    previous: Optional[JapaneseSaveImport]
    is_duplicate: bool
    duplicate_of: Optional[JapaneseSaveImport]
    hero_total_xp_before: int
    hero_total_xp_after: int
    hero_level_before: int
    hero_level_after: int

    @property
    def classification(self) -> str:
        return CLASSIFICATION_DUPLICATE if self.is_duplicate else self.delta.classification

    @property
    def reward_calculation(self) -> str:
        return CALC_DUPLICATE if self.is_duplicate else self.delta.reward_calculation

    @property
    def xp_delta(self) -> int:
        return 0 if self.is_duplicate else self.delta.xp_delta

    @property
    def stat_rewards(self) -> dict[str, int]:
        """Attribute split for the preview — the same rule the import applies."""
        if self.is_duplicate or not self.delta.awards_stat_xp:
            return {}
        return calculate_stat_rewards(JAPANESE_STAT_CATEGORY, self.xp_delta)

    @property
    def warning(self) -> Optional[str]:
        if self.is_duplicate:
            return "Dieser SAVE wurde bereits importiert. Es wird nichts gespeichert."
        return self.delta.warning


@dataclass
class ImportResult:
    created: Optional[JapaneseSaveImport]
    is_duplicate: bool
    duplicate_of: Optional[JapaneseSaveImport]
    xp_awarded: int


def get_latest_import(db: Session) -> Optional[JapaneseSaveImport]:
    """Newest snapshot by save date, ties broken by insertion order."""
    return (
        db.query(JapaneseSaveImport)
        .order_by(
            JapaneseSaveImport.save_date.desc(),
            JapaneseSaveImport.id.desc(),
        )
        .first()
    )


def get_recent_imports(db: Session, limit: int = 10) -> list[JapaneseSaveImport]:
    return (
        db.query(JapaneseSaveImport)
        .order_by(
            JapaneseSaveImport.save_date.desc(),
            JapaneseSaveImport.id.desc(),
        )
        .limit(limit)
        .all()
    )


def _find_duplicate(db: Session, digest: str) -> Optional[JapaneseSaveImport]:
    return (
        db.query(JapaneseSaveImport)
        .filter(JapaneseSaveImport.normalized_hash == digest)
        .first()
    )


def _previous_state(row: Optional[JapaneseSaveImport]) -> Optional[PreviousState]:
    if row is None:
        return None
    save_date = row.save_date
    if isinstance(save_date, datetime):
        save_date = save_date.date()
    return PreviousState(
        character_level=row.source_character_level,
        level_xp=row.source_level_xp,
        level_xp_cap=row.source_level_xp_cap,
        save_date=save_date,
    )


def _xp_description(save: JapaneseSave, delta: DeltaResult) -> str:
    """Auditable one-liner explaining where this session's XP came from."""
    if save.has_session_fields:
        return (
            f"Sitzungsmodus {save.session_mode}, Abschluss {save.session_completion}"
        )
    return (
        f"Legacy-Berechnung, Quell-Level {save.character_level}: "
        f"{save.level_xp} / {save.level_xp_cap} XP"
    )


def _ensure_hero(db: Session, name: str = "Hero") -> HeroProfile:
    hero = db.query(HeroProfile).first()
    if hero is None:
        hero = HeroProfile(name=name, level=1, total_xp=0)
        db.add(hero)
        db.flush()
    return hero


def preview_save(
    db: Session,
    raw: str,
    *,
    accept_baseline_credit: bool = False,
) -> PreviewResult:
    """Parse and evaluate a SAVE without writing anything.

    Raises ``SaveParseError`` (from japanese_saves) on invalid input.
    """
    save = parse_save(raw)
    digest = save.normalized_hash()

    duplicate = _find_duplicate(db, digest)
    previous = get_latest_import(db)
    delta = calculate_delta(
        save,
        _previous_state(previous),
        accept_baseline_credit=accept_baseline_credit,
    )

    hero = db.query(HeroProfile).first()
    total_before = hero.total_xp if hero else 0
    level_before = hero.level if hero else 1

    awarded = 0 if duplicate is not None else delta.xp_delta
    total_after = total_before + awarded

    return PreviewResult(
        save=save,
        delta=delta,
        previous=previous,
        is_duplicate=duplicate is not None,
        duplicate_of=duplicate,
        hero_total_xp_before=total_before,
        hero_total_xp_after=total_after,
        hero_level_before=level_before,
        hero_level_after=recalc_level(total_after),
    )


def import_save(
    db: Session,
    raw: str,
    *,
    accept_baseline_credit: bool = False,
    hero_name: str = "Hero",
) -> ImportResult:
    """Parse, evaluate and persist a SAVE snapshot in a single transaction.

    The raw text is re-parsed and the delta re-derived server-side; no value
    computed in the browser is trusted. Commits exactly once at the end and
    rolls back the whole unit (snapshot + XP event + hero update) on error.

    Raises ``SaveParseError`` on invalid input.
    """
    save = parse_save(raw)
    digest = save.normalized_hash()

    duplicate = _find_duplicate(db, digest)
    if duplicate is not None:
        # Idempotent: no new row, no XP, nothing to commit.
        return ImportResult(
            created=None, is_duplicate=True, duplicate_of=duplicate, xp_awarded=0
        )

    previous = get_latest_import(db)
    delta = calculate_delta(
        save,
        _previous_state(previous),
        accept_baseline_credit=accept_baseline_credit,
    )
    awarded = max(0, delta.xp_delta)

    try:
        record = JapaneseSaveImport(
            save_date=save.save_date,
            streak=save.streak,
            wanikani_level=save.wanikani_level,
            bunpro_level=save.bunpro_level,
            bunpro_points=save.bunpro_points,
            source_character_level=save.character_level,
            source_character_rank=save.character_rank,
            source_level_xp=save.level_xp,
            source_level_xp_cap=save.level_xp_cap,
            reported_session_xp=save.session_xp,
            vocabulary_score=save.vocabulary,
            grammar_score=save.grammar,
            reading_score=save.reading,
            listening_score=save.listening,
            speaking_score=save.speaking,
            current_grammar_point=save.grammar_point,
            debuffs_text=save.debuffs,
            new_vocabulary_text=save.new_vocabulary,
            daily_quest_text=save.daily_quest,
            raw_save=save.raw,
            normalized_hash=digest,
            classification=delta.classification,
            xp_awarded=awarded,
            warning_text=delta.warning,
            created_at=datetime.utcnow(),
            session_mode=save.session_mode,
            session_completion=save.session_completion,
            reward_calculation=delta.reward_calculation,
        )
        db.add(record)
        db.flush()  # assign record.id for the XpEvent source_id

        stat_rewards: dict[str, int] = {}
        stat_total = 0

        if awarded > 0:
            now = datetime.utcnow()
            hero = _ensure_hero(db, hero_name)
            db.add(
                XpEvent(
                    event_type=XP_EVENT_TYPE,
                    source=XP_SOURCE,
                    source_id=str(record.id),
                    xp=awarded,
                    attribute=XP_ATTRIBUTE,
                    title=f"Japanisch-Session {save.save_date.isoformat()}",
                    description=_xp_description(save, delta),
                    created_at=now,
                )
            )
            hero.total_xp += awarded
            hero.level = recalc_level(hero.total_xp)
            hero.updated_at = now

            # Attribute XP uses the project's canonical learning split, so the
            # sum always equals the global XP of this session. A baseline
            # credit is global-only and never reaches this branch.
            if delta.awards_stat_xp:
                stat_rewards = calculate_stat_rewards(JAPANESE_STAT_CATEGORY, awarded)
                stat_total = award_stat_xp(
                    db,
                    stat_rewards,
                    source=XP_SOURCE,
                    source_id=str(record.id),
                    title=f"Japanisch-Session {save.save_date.isoformat()}",
                    when=now,
                )

        record.stat_xp_awarded = stat_total
        record.stat_rewards = serialize_stat_rewards(stat_rewards)

        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Japanese SAVE import failed; rolled back")
        raise

    db.refresh(record)
    return ImportResult(
        created=record, is_duplicate=False, duplicate_of=None, xp_awarded=awarded
    )
