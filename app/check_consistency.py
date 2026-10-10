"""
Read-only consistency check for the append-only ledgers.

    python -m app.check_consistency

wger-hero keeps every reward twice: once as an auditable ledger row and once as
an aggregate the interface reads. XpEvent feeds HeroProfile.total_xp,
StatXpEvent feeds HeroStat.xp. Those pairs can only be wrong if something wrote
one without the other — which is exactly the bug that once left 63 wger workouts
with global XP and no strength at all, and which was found by a hand-written
audit rather than by the app.

This command answers that question in one run. It **only reads**: no INSERT, no
UPDATE, no DELETE, no commit, and nothing is repaired. When it finds something,
it says what and leaves the fixing to a deliberate, separate command.

Two severities, deliberately distinguished:

  FEHLER    an invariant that is true by construction. If it is violated, data
            was written by something that did not keep both sides in step.
  HINWEIS   something that *can* legitimately differ — historical data written
            before a rule existed, or an aggregate that is briefly stale. Worth
            seeing, not worth alarming about.

Checks the database enforces itself are deliberately absent: the unique
dedup_key on QuestCompletion, the unique normalized_hash on JapaneseSaveImport
and the partial unique index on the open pause interval cannot be violated
while the file is intact, and re-checking them would only suggest they might be.

Never prints: an API token, a password hash, a session secret, or the content of
a workout note, a SAVE block or any other free text the user wrote.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy.orm import Session

from app.models import (
    Goal,
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

logger = logging.getLogger(__name__)

ERROR = "FEHLER"
NOTE = "HINWEIS"


@dataclass
class Finding:
    severity: str
    check: str
    message: str

    def line(self) -> str:
        return f"  [{self.severity}] {self.check}: {self.message}"


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)
    checks_run: int = 0

    def error(self, check: str, message: str) -> None:
        self.findings.append(Finding(ERROR, check, message))

    def note(self, check: str, message: str) -> None:
        self.findings.append(Finding(NOTE, check, message))

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == ERROR]

    @property
    def notes(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == NOTE]

    @property
    def ok(self) -> bool:
        return not self.errors

    def render(self) -> list[str]:
        sep = "=" * 52
        lines = [sep, "  wger-hero — Konsistenzprüfung (nur lesend)", sep, ""]
        lines.append(f"Durchgeführte Prüfungen : {self.checks_run}")
        lines.append(f"Fehler                  : {len(self.errors)}")
        lines.append(f"Hinweise                : {len(self.notes)}")
        lines.append("")

        if self.errors:
            lines.append("Fehler — hier stimmen Ledger und Summe nicht überein:")
            lines.extend(f.line() for f in self.errors)
            lines.append("")
        if self.notes:
            lines.append("Hinweise — erklärbar, aber sehenswert:")
            lines.extend(f.line() for f in self.notes)
            lines.append("")
        if self.ok and not self.notes:
            lines.append("Alles stimmt überein. Keine Auffälligkeiten.")
            lines.append("")
        elif self.ok:
            lines.append("Keine Fehler. Die Hinweise oben sind kein Defekt.")
            lines.append("")

        lines.append("Es wurde nichts verändert.")
        return lines


# ---------------------------------------------------------------------------
# Invariants that are true by construction — a violation is a real defect
# ---------------------------------------------------------------------------

def check_global_xp(db: Session, report: Report) -> None:
    """Every path that raises total_xp also writes an XpEvent, and vice versa."""
    report.checks_run += 1
    hero = db.query(HeroProfile).first()
    if hero is None:
        return
    ledger = sum(e.xp for e in db.query(XpEvent).all())
    if ledger != hero.total_xp:
        report.error(
            "Globale XP",
            f"Summe der XpEvents ist {ledger}, HeroProfile.total_xp ist "
            f"{hero.total_xp} (Differenz {hero.total_xp - ledger})",
        )


def check_stat_xp(db: Session, report: Report) -> None:
    """award_stat_xp() is the only writer and always moves both sides together."""
    report.checks_run += 1
    ledger: dict[str, int] = {}
    for event in db.query(StatXpEvent).all():
        ledger[event.stat_key] = ledger.get(event.stat_key, 0) + event.xp

    totals = {stat.stat_key: stat.xp for stat in db.query(HeroStat).all()}

    for key in sorted(set(ledger) | set(totals)):
        summed = ledger.get(key, 0)
        stored = totals.get(key, 0)
        if summed != stored:
            report.error(
                "Attribut-XP",
                f"„{key}“: Summe der StatXpEvents ist {summed}, HeroStat.xp ist "
                f"{stored} (Differenz {stored - summed})",
            )


def check_japanese_imports(db: Session, report: Report) -> None:
    """Each import's recorded XP must match the XpEvent it wrote.

    A baseline import legitimately awards 0 stat XP, so only the global side is
    compared — flagging the deliberate baseline case would be noise.
    """
    report.checks_run += 1
    for record in db.query(JapaneseSaveImport).all():
        events = (
            db.query(XpEvent)
            .filter(XpEvent.source == "japanese", XpEvent.source_id == str(record.id))
            .all()
        )
        awarded = sum(e.xp for e in events)
        if awarded != (record.xp_awarded or 0):
            report.error(
                "Japanisch-Import",
                f"Import {record.id} ({record.save_date}): gespeichert "
                f"{record.xp_awarded} XP, im Ledger {awarded} XP",
            )


def check_weekday_range(db: Session, report: Report) -> None:
    """1..7 is enforced in Python only — the database cannot express it."""
    report.checks_run += 1
    for row in db.query(HabitScheduleDay).all():
        if row.iso_weekday not in range(1, 8):
            report.error(
                "Wochenplanung",
                f"Gewohnheit {row.habit_id} hat den ungültigen Wochentag "
                f"{row.iso_weekday} (erlaubt: 1–7)",
            )


def check_orphans(db: Session, report: Report) -> None:
    """Ledger rows pointing at something that no longer exists."""
    report.checks_run += 1
    habit_ids = {h.id for h in db.query(Habit).all()}
    quest_ids = {q.id for q in db.query(Quest).all()}

    orphan_completions = [
        c.id for c in db.query(HabitCompletion).all() if c.habit_id not in habit_ids
    ]
    if orphan_completions:
        report.error(
            "Verwaiste Abschlüsse",
            f"{len(orphan_completions)} HabitCompletion(s) ohne zugehörige "
            f"Gewohnheit",
        )

    orphan_quests = [
        c.id for c in db.query(QuestCompletion).all() if c.quest_id not in quest_ids
    ]
    if orphan_quests:
        report.error(
            "Verwaiste Quest-Abschlüsse",
            f"{len(orphan_quests)} QuestCompletion(s) ohne zugehörige Quest",
        )

    orphan_plans = [
        s.id for s in db.query(HabitScheduleDay).all() if s.habit_id not in habit_ids
    ]
    if orphan_plans:
        report.error(
            "Verwaiste Wochenplanung",
            f"{len(orphan_plans)} Planungseintrag/-einträge ohne Gewohnheit",
        )


# ---------------------------------------------------------------------------
# Advisory — legitimate data can differ here
# ---------------------------------------------------------------------------

def check_hero_level(db: Session, report: Report) -> None:
    """Unlocking an achievement adds XP without recalculating the level.

    It catches up at the next habit, quest or sync award, so a stale level is
    expected rather than broken.
    """
    report.checks_run += 1
    from app.xp import recalc_level

    hero = db.query(HeroProfile).first()
    if hero is None:
        return
    expected = recalc_level(hero.total_xp)
    if expected != hero.level:
        report.note(
            "Heldenlevel",
            f"gespeichert Level {hero.level}, aus {hero.total_xp} XP berechnet "
            f"Level {expected} — gleicht sich bei der nächsten Belohnung an",
        )


def check_sync_totals(db: Session, report: Report) -> None:
    """SyncEvent.xp_awarded is a convenience total, the audit rows are truth.

    sync.py already treats a disagreement as an expected legacy condition and
    resolves it in favour of the ledger, so this reports rather than accuses.
    """
    report.checks_run += 1
    mismatched = 0
    for event in db.query(SyncEvent).filter(SyncEvent.source == "wger").all():
        awarded = sum(
            e.xp
            for e in db.query(XpEvent)
            .filter(XpEvent.source == "wger", XpEvent.source_id == event.source_id)
            .all()
        )
        if awarded != (event.xp_awarded or 0):
            mismatched += 1
    if mismatched:
        report.note(
            "wger-Sync",
            f"{mismatched} SyncEvent(s) mit einer gespeicherten Summe, die von "
            f"den Auditzeilen abweicht — beim nächsten Re-Sync gewinnen die "
            f"Auditzeilen",
        )


def check_completion_allowance(db: Session, report: Report) -> None:
    """Completions beyond the period allowance, from before that rule existed."""
    report.checks_run += 1
    from app.habits import completion_period_bounds
    from app.quests import app_date_of

    over = 0
    for habit in db.query(Habit).all():
        target = max(1, int(habit.target_count or 1))
        buckets: dict[str, int] = {}
        for completion in (
            db.query(HabitCompletion)
            .filter(HabitCompletion.habit_id == habit.id)
            .all()
        ):
            day = app_date_of(completion.completed_at)
            start, _ = completion_period_bounds(habit.recurrence, day)
            key = start.date().isoformat()
            buckets[key] = buckets.get(key, 0) + 1
        if any(count > target for count in buckets.values()):
            over += 1
    if over:
        report.note(
            "Abschlussgrenze",
            f"{over} Gewohnheit(en) mit mehr Abschlüssen in einer Periode als "
            f"das Ziel erlaubt — historische Daten von vor dieser Regel; "
            f"nichts wird rückwirkend entfernt",
        )


def check_pause_history(db: Session, report: Report) -> None:
    """A paused goal whose break predates the interval table has no open row."""
    report.checks_run += 1
    from app.goals import STATUS_PAUSED
    from app.models import GoalPauseInterval

    missing = 0
    for goal in db.query(Goal).filter(Goal.status == STATUS_PAUSED).all():
        open_interval = (
            db.query(GoalPauseInterval)
            .filter(
                GoalPauseInterval.goal_id == goal.id,
                GoalPauseInterval.ended_at.is_(None),
            )
            .first()
        )
        if open_interval is None:
            missing += 1
    if missing:
        report.note(
            "Pausenhistorie",
            f"{missing} pausiertes/pausierte Ziel(e) ohne offenes Intervall — "
            f"eine Pause von vor Revision 0004; es wird keine Historie erfunden",
        )


def check_fittrackee_ledger(db: Session, report: Report) -> None:
    """Each FitTrackee activity carries at most one reward, in both ledgers.

    The sync revokes from the audit rows before it re-awards, so a second row
    for the same source id means a reward was written twice — the exact bug the
    reconciliation exists to prevent. True by construction, therefore an error.
    """
    from app.fittrackee_sync import SOURCE, source_id_for
    from app.models import FitTrackeeWorkout

    report.checks_run += 1

    for model, label in ((XpEvent, "XP"), (StatXpEvent, "Attribut-XP")):
        counts: dict[str, int] = {}
        rows = (
            db.query(model)
            .filter(model.source == SOURCE, model.source_id.isnot(None))
            .all()
        )
        for row in rows:
            counts[row.source_id] = counts.get(row.source_id, 0) + 1
        for source_id, count in sorted(counts.items()):
            if count > 1:
                report.error(
                    "FitTrackee",
                    f"{label}: {count} Belohnungszeilen für dieselbe Quelle "
                    f"„{source_id}“ — erwartet ist höchstens eine",
                )

    # A workout the ledger pays for must be one the rules say qualifies.
    awarded = {
        row.source_id
        for row in db.query(XpEvent).filter(XpEvent.source == SOURCE).all()
        if row.source_id
    }
    for workout in db.query(FitTrackeeWorkout).all():
        source_id = source_id_for(workout.external_id)
        has_reward = source_id in awarded
        should = bool(workout.qualifies_for_endurance and workout.reward_eligible)
        if has_reward and not should:
            report.error(
                "FitTrackee",
                f"Workout {workout.external_id} ist belohnt, erfüllt aber die "
                f"Bedingungen nicht (qualifiziert={workout.qualifies_for_endurance}, "
                f"reward-fähig={workout.reward_eligible})",
            )
        elif should and not has_reward:
            # Legitimate for a database whose sync ran before this check
            # existed, or where a reward was revoked by hand.
            report.note(
                "FitTrackee",
                f"Workout {workout.external_id} qualifiziert und ist reward-fähig, "
                f"hat aber keine Belohnungszeile",
            )


def check_fittrackee_orphans(db: Session, report: Report) -> None:
    """No FitTrackee ledger row without the workout it belongs to."""
    from app.fittrackee_sync import SOURCE, source_id_for
    from app.models import FitTrackeeWorkout

    report.checks_run += 1

    known = {source_id_for(external_id) for (external_id,) in db.query(FitTrackeeWorkout.external_id).all()}

    for model, label in ((XpEvent, "XpEvent"), (StatXpEvent, "StatXpEvent")):
        orphans = [
            row.source_id
            for row in db.query(model).filter(model.source == SOURCE).all()
            if row.source_id and row.source_id not in known
        ]
        for source_id in sorted(set(orphans)):
            report.error(
                "FitTrackee",
                f"{label} für „{source_id}“, aber kein passendes FitTrackee-Workout",
            )


def check_fittrackee_negatives(db: Session, report: Report) -> None:
    """No negative durations, and no negative aggregate anywhere."""
    from app.models import FitTrackeeWorkout

    report.checks_run += 1

    for workout in db.query(FitTrackeeWorkout).all():
        if (workout.duration_seconds or 0) < 0:
            report.error(
                "FitTrackee",
                f"Workout {workout.external_id} hat eine negative Dauer",
            )
        if workout.moving_seconds is not None and workout.moving_seconds < 0:
            report.error(
                "FitTrackee",
                f"Workout {workout.external_id} hat eine negative Bewegungszeit",
            )

    for stat in db.query(HeroStat).all():
        if stat.xp < 0:
            report.error(
                "Attribut-XP", f"„{stat.stat_key}“ ist negativ: {stat.xp}"
            )


CHECKS = (
    check_global_xp,
    check_stat_xp,
    check_japanese_imports,
    check_weekday_range,
    check_orphans,
    check_hero_level,
    check_sync_totals,
    check_completion_allowance,
    check_pause_history,
    check_fittrackee_ledger,
    check_fittrackee_orphans,
    check_fittrackee_negatives,
)


def check_consistency(db: Session) -> Report:
    """Run every check. Reads only — never writes, never commits."""
    report = Report()
    for check in CHECKS:
        check(db, report)
    return report


def _main(argv: list[str]) -> int:
    import os

    os.environ.setdefault("WGER_BASE_URL", "https://wger.example.com")
    from app.database import get_db, init_db

    if any(arg not in ("--quiet",) for arg in argv):
        print("Usage: python -m app.check_consistency [--quiet]")
        return 1
    quiet = "--quiet" in argv

    init_db()
    db_gen = get_db()
    db = next(db_gen)
    try:
        report = check_consistency(db)
        if not quiet or not report.ok:
            print("\n".join(report.render()))
        # Non-zero only for real defects: a cron job should stay quiet about
        # the advisory notes, which are expected on an older database.
        return 0 if report.ok else 2
    finally:
        try:
            next(db_gen)
        except StopIteration:
            pass


if __name__ == "__main__":
    import sys

    sys.exit(_main(sys.argv[1:]))
