"""
One-off repair: give existing wger workouts the strength stat XP they never got.

Until the sync was fixed, a completed wger workout awarded 100 global XP with
``attribute="Strength"`` but never touched the canonical ``strength`` stat. The
global XP is correct and must stay exactly as it is — this repair only adds the
missing stat side.

    python -m app.repair_wger_stat_xp --dry-run
    python -m app.repair_wger_stat_xp --apply

The source of truth is the existing audit rows: every ``XpEvent`` with
``source="wger"``, ``event_type="workout_complete"`` and ``attribute="Strength"``
is a candidate, and the amount credited is that row's own ``xp``. No count and
no amount is hard-coded, so the repair works on any database.

It is idempotent: a session that already has a matching ``StatXpEvent`` is
skipped, and a second run reports nothing left to do. Where the existing stat
rows are ambiguous — several of them, or one with a different amount — the
session is reported as a conflict and left completely untouched rather than
topped up on a guess.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy.orm import Session

from app.models import HeroStat, StatXpEvent, XpEvent
from app.sync import STRENGTH_ATTRIBUTE, STRENGTH_STAT_KEY, WORKOUT_COMPLETE_EVENT

logger = logging.getLogger(__name__)


@dataclass
class RepairPlan:
    """What the repair would do, or did."""

    candidates: int = 0          # workout completions found
    already_repaired: int = 0    # already carry the matching stat award
    missing: int = 0             # would be, or were, repaired
    additional_strength_xp: int = 0
    conflicts: list[str] = field(default_factory=list)
    applied: bool = False

    # Global XP is never touched by this repair; kept explicit so the report
    # can state it rather than leaving it to trust.
    global_xp_change: int = 0

    def report(self) -> list[str]:
        mode = "Angewendet" if self.applied else "Vorschau (dry-run, es wird nichts geschrieben)"
        lines = [
            f"Reparatur fehlender Stärke-Stat-XP — {mode}",
            "",
            f"  Kandidaten (wger workout_complete):  {self.candidates}",
            f"  bereits repariert:                   {self.already_repaired}",
            f"  zu ergänzen:                         {self.missing}",
            f"  zusätzliche strength XP:             {self.additional_strength_xp}",
            f"  globale XP Änderung:                 {self.global_xp_change}",
        ]
        if self.conflicts:
            lines.append("")
            lines.append(f"  Konflikte ({len(self.conflicts)}) — unverändert gelassen:")
            lines.extend(f"    {line}" for line in self.conflicts)
        if not self.missing and not self.conflicts:
            lines.append("")
            lines.append("  Nichts zu tun — alle Workouts haben ihre Stärke-XP.")
        return lines


def _workout_completions(db: Session) -> list[XpEvent]:
    return (
        db.query(XpEvent)
        .filter(
            XpEvent.source == "wger",
            XpEvent.event_type == WORKOUT_COMPLETE_EVENT,
            XpEvent.attribute == STRENGTH_ATTRIBUTE,
        )
        .order_by(XpEvent.id)
        .all()
    )


def _existing_stat_rows(db: Session, source_id: str) -> list[StatXpEvent]:
    return (
        db.query(StatXpEvent)
        .filter(
            StatXpEvent.source == "wger",
            StatXpEvent.source_id == source_id,
            StatXpEvent.stat_key == STRENGTH_STAT_KEY,
        )
        .all()
    )


def _walk(db: Session, apply: bool) -> RepairPlan:
    plan = RepairPlan(applied=apply)

    for event in _workout_completions(db):
        plan.candidates += 1

        if not event.source_id:
            plan.conflicts.append(
                f"XpEvent {event.id}: ohne source_id — nicht zuordenbar"
            )
            continue

        existing = _existing_stat_rows(db, event.source_id)

        if len(existing) > 1:
            plan.conflicts.append(
                f"{event.source_id}: {len(existing)} Stärke-Einträge vorhanden"
            )
            continue

        if len(existing) == 1:
            if existing[0].xp == event.xp:
                plan.already_repaired += 1
            else:
                plan.conflicts.append(
                    f"{event.source_id}: vorhandener Eintrag mit {existing[0].xp} XP "
                    f"statt {event.xp} XP"
                )
            continue

        # Nothing there yet — this is the case the repair exists for.
        plan.missing += 1
        plan.additional_strength_xp += event.xp

        if not apply:
            continue

        stat = (
            db.query(HeroStat)
            .filter(HeroStat.stat_key == STRENGTH_STAT_KEY)
            .first()
        )
        if stat is None:
            stat = HeroStat(stat_key=STRENGTH_STAT_KEY, xp=0)
            db.add(stat)
            db.flush()
        stat.xp += event.xp
        if event.created_at is not None:
            stat.updated_at = event.created_at

        db.add(
            StatXpEvent(
                stat_key=STRENGTH_STAT_KEY,
                xp=event.xp,
                source="wger",
                source_id=event.source_id,
                title=event.title,
                # Keep the original timestamp so the stat history lines up with
                # the workout it belongs to, not with the day of the repair.
                created_at=event.created_at,
            )
        )

    return plan


def plan_repair(db: Session) -> RepairPlan:
    """What the repair would change. Writes nothing."""
    return _walk(db, apply=False)


def apply_repair(db: Session) -> RepairPlan:
    """Add the missing strength stat XP, all or nothing.

    Global XP, XpEvent rows and SyncEvent rows are never touched.
    """
    try:
        plan = _walk(db, apply=True)
        db.commit()
        return plan
    except Exception:
        db.rollback()
        logger.exception("Strength repair failed and was rolled back")
        raise


def _main(argv: list[str]) -> int:
    import os

    os.environ.setdefault("WGER_BASE_URL", "https://wger.example.com")
    from app.database import get_db, init_db

    if "--apply" in argv:
        apply = True
    elif "--dry-run" in argv:
        apply = False
    else:
        print("Usage: python -m app.repair_wger_stat_xp {--dry-run|--apply}")
        return 1

    init_db()
    db_gen = get_db()
    db = next(db_gen)
    try:
        plan = apply_repair(db) if apply else plan_repair(db)
        print("\n".join(plan.report()))
        return 0
    finally:
        try:
            next(db_gen)
        except StopIteration:
            pass


if __name__ == "__main__":
    import sys

    sys.exit(_main(sys.argv[1:]))
