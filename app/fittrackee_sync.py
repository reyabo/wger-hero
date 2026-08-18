"""Turn FitTrackee activities into endurance progression.

The shape mirrors the wger sync deliberately — external source, stable source
id, deterministic hash, one transaction — but the two never mix. wger is the
strength source and FitTrackee the endurance source; they have separate source
ids, separate quest types and separate stats.

Four rules carry the whole module.

**Baseline.** The first import is history, not achievement. Everything it finds
is stored with ``reward_eligible = False`` and pays nothing, and that flag is
never flipped afterwards — not by an edit, not by enabling a sport, not by a
restart. Otherwise connecting an account with two years of running in it would
hand out thousands of XP for training that happened before Hero existed.

**Qualification.** A workout counts as endurance only when the user has enabled
its sport *and* it lasted at least ten minutes. Both are Hero gamification
rules, not statements about training.

**A flat award.** Every qualifying unit is worth the same 40 XP. Per-kilometre
or per-minute XP would let one long activity outscore a month of consistency,
and would make sports incomparable. Heart rate is stored when present and never
scored — without individual baselines any zone or VO2max estimate would be
invention.

**Idempotence.** The same sync run twice awards nothing the second time. A
changed workout has its previous award taken back from the audit rows and
rewritten exactly once, in the same transaction.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

from sqlalchemy.orm import Session

from app.fittrackee_client import (
    FitTrackeeClient,
    FitTrackeeClientError,
    parse_duration,
    parse_int,
    parse_number,
    parse_workout_date,
)
from app.fittrackee_oauth import (
    FitTrackeeAuthError,
    is_configured,
    load_client_secret,
    token_store_for,
)
from app.models import (
    FitTrackeeConnection,
    FitTrackeeSport,
    FitTrackeeWorkout,
    HeroProfile,
    HeroStat,
    StatXpEvent,
    XpEvent,
)
from app.quests import app_date_of
from app.stats import award_stat_xp

logger = logging.getLogger(__name__)

SOURCE = "fittrackee"
ENDURANCE_STAT_KEY = "endurance"
EVENT_TYPE = "endurance_workout"

# A qualifying endurance unit, in seconds. Central and tested: every counter and
# every preview asks this one constant.
MIN_ENDURANCE_SECONDS = 600

# The flat award for one qualifying unit.
WORKOUT_XP = 40
ENDURANCE_STAT_XP = 40


def source_id_for(external_id: str) -> str:
    """Stable ledger id for one FitTrackee activity."""
    return f"fittrackee-workout-{external_id}"


# ---------------------------------------------------------------------------
# Normalisation and hashing
# ---------------------------------------------------------------------------

@dataclass
class NormalizedWorkout:
    external_id: str
    workout_at: datetime
    local_date: date
    sport_id: int
    duration_seconds: int
    moving_seconds: Optional[int]
    distance_km: Optional[float]
    ave_speed: Optional[float]
    max_speed: Optional[float]
    ave_hr: Optional[int]
    max_hr: Optional[int]
    remote_modified_at: Optional[datetime]
    source_hash: str

    @property
    def effective_seconds(self) -> int:
        """Moving time when FitTrackee reports it, otherwise total duration.

        Moving time is the better measure of an endurance session — a stop at a
        traffic light is not training — but it is optional in the API.
        """
        if self.moving_seconds is not None:
            return self.moving_seconds
        return self.duration_seconds


class NormalizationError(ValueError):
    """A workout that cannot be interpreted. Skipped, never guessed at."""


def _round(value: Optional[float], digits: int = 3) -> Optional[float]:
    """Round before hashing, so float noise cannot fake a change."""
    return None if value is None else round(float(value), digits)


def normalize_workout(raw: dict) -> NormalizedWorkout:
    """One API record to the fields Hero keeps, plus its hash.

    Only the projected fields arrive here — no GPS, no notes — so nothing
    private can enter the hash or the database through this path.
    """
    external_id = raw.get("id")
    if external_id in (None, ""):
        raise NormalizationError("workout without an id")
    external_id = str(external_id)

    workout_at = parse_workout_date(raw.get("workout_date"))
    if workout_at is None:
        raise NormalizationError(f"workout {external_id} has no readable date")

    sport_id = parse_int(raw.get("sport_id"))
    if sport_id is None:
        raise NormalizationError(f"workout {external_id} has no sport id")

    duration = parse_duration(raw.get("duration"))
    if duration is None:
        raise NormalizationError(f"workout {external_id} has no readable duration")

    moving = parse_duration(raw.get("moving"))
    distance = _round(parse_number(raw.get("distance")))
    ave_speed = _round(parse_number(raw.get("ave_speed")))
    max_speed = _round(parse_number(raw.get("max_speed")))
    ave_hr = parse_int(raw.get("ave_hr"))
    max_hr = parse_int(raw.get("max_hr"))
    modified = parse_workout_date(raw.get("modification_date"))

    # A deterministic, order-independent digest over exactly the fields whose
    # change should re-open a reward decision. Titles, notes and tracks are not
    # among them — and are not available here in the first place.
    payload = json.dumps(
        {
            "external_id": external_id,
            "workout_at": workout_at.isoformat(),
            "sport_id": sport_id,
            "duration_seconds": duration,
            "moving_seconds": moving,
            "distance": distance,
            "ave_speed": ave_speed,
            "max_speed": max_speed,
            "ave_hr": ave_hr,
            "max_hr": max_hr,
            "remote_modified_at": modified.isoformat() if modified else None,
        },
        sort_keys=True,
        separators=(",", ":"),
    )

    return NormalizedWorkout(
        external_id=external_id,
        workout_at=workout_at,
        local_date=app_date_of(workout_at),
        sport_id=sport_id,
        duration_seconds=duration,
        moving_seconds=moving,
        distance_km=distance,
        ave_speed=ave_speed,
        max_speed=max_speed,
        ave_hr=ave_hr,
        max_hr=max_hr,
        remote_modified_at=modified,
        source_hash=hashlib.sha256(payload.encode("utf-8")).hexdigest(),
    )


# ---------------------------------------------------------------------------
# Qualification
# ---------------------------------------------------------------------------

def endurance_sport_ids(db: Session) -> set[int]:
    """Sport ids the user explicitly marked as endurance."""
    rows = (
        db.query(FitTrackeeSport.sport_id)
        .filter(FitTrackeeSport.counts_for_endurance == True)  # noqa: E712
        .all()
    )
    return {row[0] for row in rows}


def qualifies(
    sport_id: int, effective_seconds: int, enabled_sports: set[int]
) -> bool:
    """Both conditions, in one place so no counter can drift from another."""
    return sport_id in enabled_sports and effective_seconds >= MIN_ENDURANCE_SECONDS


# ---------------------------------------------------------------------------
# Ledger writes
# ---------------------------------------------------------------------------

def _award(db: Session, hero: HeroProfile, workout: FitTrackeeWorkout) -> int:
    """Book the flat award for one qualifying, reward-eligible workout."""
    source_id = source_id_for(workout.external_id)
    when = workout.workout_at
    label = workout.sport_label or f"Sport {workout.sport_id}"
    minutes = round((workout.moving_seconds or workout.duration_seconds) / 60)
    title = f"{label} — {minutes} min"

    db.add(
        XpEvent(
            event_type=EVENT_TYPE,
            source=SOURCE,
            source_id=source_id,
            xp=WORKOUT_XP,
            attribute="Endurance",
            title=title,
            description="Qualifizierende Ausdauereinheit aus FitTrackee.",
            created_at=when,
        )
    )
    award_stat_xp(
        db,
        {ENDURANCE_STAT_KEY: ENDURANCE_STAT_XP},
        source=SOURCE,
        source_id=source_id,
        title=title,
        when=when,
    )
    hero.total_xp += WORKOUT_XP
    return WORKOUT_XP


def _revoke(db: Session, hero: HeroProfile, external_id: str) -> int:
    """Take back everything previously awarded for one activity.

    The audit rows are the authority — there is no stored per-workout total to
    trust, and reading them back is what makes a re-award exact rather than
    approximate. Returns the global XP removed.
    """
    source_id = source_id_for(external_id)

    xp_rows = (
        db.query(XpEvent)
        .filter(XpEvent.source == SOURCE, XpEvent.source_id == source_id)
        .all()
    )
    stat_rows = (
        db.query(StatXpEvent)
        .filter(StatXpEvent.source == SOURCE, StatXpEvent.source_id == source_id)
        .all()
    )

    removed = sum(row.xp for row in xp_rows)
    hero.total_xp = max(0, hero.total_xp - removed)

    for row in stat_rows:
        stat = db.query(HeroStat).filter(HeroStat.stat_key == row.stat_key).first()
        if stat is not None:
            stat.xp = max(0, stat.xp - row.xp)
        db.delete(row)
    for row in xp_rows:
        db.delete(row)

    db.flush()
    return removed


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------

@dataclass
class SyncOutcome:
    """What one run did, or would do in a dry run."""

    baseline: bool = False
    dry_run: bool = False
    fetched: int = 0
    new: int = 0
    changed: int = 0
    unchanged: int = 0
    qualifying: int = 0
    not_qualifying: int = 0
    skipped: int = 0
    xp_awarded: int = 0
    stat_xp_awarded: int = 0
    xp_revoked: int = 0
    quests_completed: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def as_lines(self) -> list[str]:
        mode = "Baseline" if self.baseline else "Synchronisation"
        if self.dry_run:
            mode += " (Probelauf, nichts geschrieben)"
        lines = [
            mode,
            f"  Workouts abgerufen:      {self.fetched}",
            f"  neu:                     {self.new}",
            f"  geändert:                {self.changed}",
            f"  unverändert:             {self.unchanged}",
            f"  qualifiziert:            {self.qualifying}",
            f"  nicht qualifiziert:      {self.not_qualifying}",
            f"  übersprungen (fehlerhaft): {self.skipped}",
            f"  globale XP:              {self.xp_awarded}",
            f"  Ausdauer-XP:             {self.stat_xp_awarded}",
        ]
        if self.xp_revoked:
            lines.append(f"  zurückgenommene XP:      {self.xp_revoked}")
        if self.quests_completed:
            lines.append(f"  Quests belohnt:          {', '.join(self.quests_completed)}")
        for error in self.errors:
            lines.append(f"  Hinweis: {error}")
        return lines


def get_connection(db: Session) -> FitTrackeeConnection:
    """The single connection row, created empty on first use."""
    row = db.query(FitTrackeeConnection).first()
    if row is None:
        row = FitTrackeeConnection()
        db.add(row)
        db.flush()
    return row


def has_baseline(db: Session) -> bool:
    return get_connection(db).baseline_at is not None


def _hero(db: Session, name: str = "Hero") -> HeroProfile:
    hero = db.query(HeroProfile).first()
    if hero is None:
        hero = HeroProfile(name=name, level=1, total_xp=0)
        db.add(hero)
        db.flush()
    return hero


def store_sports(db: Session, sports: list[dict]) -> int:
    """Record the instance's sports without deciding anything about them.

    A sport Hero has not seen before arrives with counts_for_endurance = False.
    That is the whole point: the user enables what counts, so a FitTrackee
    release adding a sport needs no code change and grants no XP by surprise.
    """
    now = datetime.utcnow()
    seen = 0
    for entry in sports:
        sport_id = entry["sport_id"]
        row = (
            db.query(FitTrackeeSport)
            .filter(FitTrackeeSport.sport_id == sport_id)
            .first()
        )
        if row is None:
            row = FitTrackeeSport(
                sport_id=sport_id,
                label=entry["label"],
                is_active=entry.get("is_active", True),
                counts_for_endurance=False,
            )
            db.add(row)
        else:
            # The label and availability follow FitTrackee; the user's endurance
            # decision is never overwritten by a refresh.
            row.label = entry["label"]
            row.is_active = entry.get("is_active", True)
        row.last_seen_at = now
        seen += 1
    connection = get_connection(db)
    connection.sports_synced_at = now
    return seen


def apply_workouts(
    db: Session,
    raw_workouts: list[dict],
    *,
    baseline: bool,
    dry_run: bool = False,
    hero_name: str = "Hero",
) -> SyncOutcome:
    """Store a fetched batch and settle its rewards. Caller owns the commit.

    Runs entirely in the caller's transaction: the ledger, the workout rows and
    the hero total either all land or none do.
    """
    outcome = SyncOutcome(baseline=baseline, dry_run=dry_run, fetched=len(raw_workouts))
    hero = _hero(db, hero_name)
    enabled = endurance_sport_ids(db)
    labels = {
        row.sport_id: row.label for row in db.query(FitTrackeeSport).all()
    }

    for raw in raw_workouts:
        try:
            normalized = normalize_workout(raw)
        except NormalizationError as exc:
            outcome.skipped += 1
            outcome.errors.append(str(exc))
            logger.warning("Skipping a FitTrackee workout: %s", exc)
            continue

        existing = (
            db.query(FitTrackeeWorkout)
            .filter(FitTrackeeWorkout.external_id == normalized.external_id)
            .first()
        )

        is_qualifying = qualifies(
            normalized.sport_id, normalized.effective_seconds, enabled
        )

        if existing is not None and existing.source_hash == normalized.source_hash:
            outcome.unchanged += 1
            continue

        if existing is None:
            outcome.new += 1
            # The baseline decides eligibility once, at import. A workout that
            # arrives during the baseline is history and stays history.
            reward_eligible = not baseline
        else:
            outcome.changed += 1
            reward_eligible = existing.reward_eligible
            if not dry_run:
                outcome.xp_revoked += _revoke(db, hero, normalized.external_id)

        if is_qualifying:
            outcome.qualifying += 1
        else:
            outcome.not_qualifying += 1

        will_award = is_qualifying and reward_eligible
        if will_award:
            outcome.xp_awarded += WORKOUT_XP
            outcome.stat_xp_awarded += ENDURANCE_STAT_XP

        if dry_run:
            continue

        if existing is None:
            existing = FitTrackeeWorkout(external_id=normalized.external_id)
            db.add(existing)

        existing.workout_at = normalized.workout_at
        existing.local_date = normalized.local_date
        existing.sport_id = normalized.sport_id
        existing.sport_label = labels.get(normalized.sport_id)
        existing.duration_seconds = normalized.duration_seconds
        existing.moving_seconds = normalized.moving_seconds
        existing.distance_km = normalized.distance_km
        existing.ave_speed = normalized.ave_speed
        existing.max_speed = normalized.max_speed
        existing.ave_hr = normalized.ave_hr
        existing.max_hr = normalized.max_hr
        existing.remote_modified_at = normalized.remote_modified_at
        existing.qualifies_for_endurance = is_qualifying
        existing.reward_eligible = reward_eligible
        existing.source_hash = normalized.source_hash
        db.flush()

        if will_award:
            _award(db, hero, existing)

    if not dry_run:
        from app.xp import level_from_total_xp

        level, _, _ = level_from_total_xp(hero.total_xp)
        hero.level = level
        hero.updated_at = datetime.utcnow()

    return outcome


async def run_sync(
    db: Session,
    client: FitTrackeeClient,
    *,
    baseline: bool = False,
    dry_run: bool = False,
    from_date: Optional[date] = None,
    hero_name: str = "Hero",
    evaluate: bool = True,
) -> SyncOutcome:
    """Fetch, then settle. Nothing is written until the fetch fully succeeded.

    A partial fetch must never become a partial sync: an interrupted page walk
    would look exactly like "these workouts do not exist", and on a later run
    the missing ones would be awarded a second time.
    """
    connection = get_connection(db)

    try:
        await client.ensure_fresh_token()
        raw_workouts = await client.get_workouts(from_date=from_date)
    except FitTrackeeAuthError as exc:
        connection.needs_reauth = True
        connection.last_error = str(exc)
        db.commit()
        raise
    except FitTrackeeClientError as exc:
        connection.last_error = str(exc)
        db.commit()
        raise

    try:
        outcome = apply_workouts(
            db, raw_workouts, baseline=baseline, dry_run=dry_run, hero_name=hero_name
        )
        if dry_run:
            db.rollback()
            return outcome

        now = datetime.utcnow()
        connection.last_sync_at = now
        connection.last_error = None
        connection.needs_reauth = False
        if baseline and connection.baseline_at is None:
            connection.baseline_at = now
            connection.baseline_workouts = outcome.new
        db.commit()
    except Exception:
        db.rollback()
        raise

    if evaluate and not baseline:
        from app.quests import evaluate_quests

        hero = _hero(db, hero_name)
        outcome.quests_completed = evaluate_quests(db, hero)

    return outcome


# ---------------------------------------------------------------------------
# Building a client from settings
# ---------------------------------------------------------------------------

def build_client(settings) -> FitTrackeeClient:
    """A ready client, or a clear error explaining what is missing."""
    if not is_configured(settings):
        raise FitTrackeeAuthError(
            "FitTrackee ist nicht konfiguriert. FITTRACKEE_BASE_URL und "
            "FITTRACKEE_CLIENT_ID setzen."
        )
    store = token_store_for(settings)
    tokens = store.load()
    if tokens is None:
        raise FitTrackeeAuthError(
            "Keine FitTrackee-Autorisierung vorhanden. Bitte zuerst verbinden."
        )
    return FitTrackeeClient(
        settings.FITTRACKEE_BASE_URL,
        tokens,
        client_id=settings.FITTRACKEE_CLIENT_ID,
        client_secret=load_client_secret(settings),
        store=store,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _status_lines(db: Session, settings) -> list[str]:
    connection = get_connection(db)
    sports = db.query(FitTrackeeSport).all()
    enabled = [s for s in sports if s.counts_for_endurance]
    total = db.query(FitTrackeeWorkout).count()
    eligible = (
        db.query(FitTrackeeWorkout)
        .filter(FitTrackeeWorkout.reward_eligible == True)  # noqa: E712
        .count()
    )
    store = token_store_for(settings)
    return [
        "FitTrackee-Status",
        f"  Basis-URL konfiguriert:  {'ja' if settings.FITTRACKEE_BASE_URL else 'nein'}",
        f"  OAuth-Client:            {'ja' if settings.FITTRACKEE_CLIENT_ID else 'nein'}",
        f"  Autorisierung vorhanden: {'ja' if store.exists() else 'nein'}",
        f"  Neu verbinden nötig:     {'ja' if connection.needs_reauth else 'nein'}",
        f"  Baseline:                {connection.baseline_at or 'noch nicht erstellt'}",
        f"  Letzter Sync:            {connection.last_sync_at or 'nie'}",
        f"  Sportarten bekannt:      {len(sports)}",
        f"  davon als Ausdauer aktiv:{len(enabled)}",
        f"  Workouts gespeichert:    {total}",
        f"  davon reward-fähig:      {eligible}",
        f"  Letzter Fehler:          {connection.last_error or '—'}",
    ]


def _main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.fittrackee_sync",
        description="Read-only FitTrackee endurance sync.",
    )
    parser.add_argument(
        "command", choices=("status", "probe", "baseline", "sync"),
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Nur berichten, nichts schreiben."
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    from app.config import get_settings
    from app.database import get_db, init_db

    settings = get_settings()
    init_db()
    db_gen = get_db()
    db = next(db_gen)
    try:
        if args.command == "status":
            for line in _status_lines(db, settings):
                print(line)
            return 0

        # The state guards run first, before any credential is read or any
        # request is made: "you have no baseline yet" is the more useful answer
        # than "you are not authorized", and it costs nothing to find out.
        baseline = args.command == "baseline"
        if args.command in ("baseline", "sync"):
            if baseline and has_baseline(db) and not args.dry_run:
                print("Es existiert bereits eine Baseline. Nichts zu tun.")
                return 0
            if not baseline and not has_baseline(db):
                print(
                    "FEHLER: Es gibt noch keine Baseline. Zuerst "
                    "'baseline' ausführen, sonst würde die gesamte Historie XP geben."
                )
                return 2

        try:
            client = build_client(settings)
        except FitTrackeeAuthError as exc:
            print(f"FEHLER: {exc}")
            return 2

        if args.command == "probe":
            sports = asyncio.run(client.get_sports())
            print(f"API erreichbar. {len(sports)} Sportarten gemeldet.")
            for sport in sports:
                print(f"  {sport['sport_id']:>4}  {sport['label']}")
            return 0

        outcome = asyncio.run(
            run_sync(
                db,
                client,
                baseline=baseline,
                dry_run=args.dry_run,
                from_date=settings.FITTRACKEE_SYNC_FROM_DATE,
                hero_name=settings.HERO_NAME,
            )
        )
        for line in outcome.as_lines():
            print(line)
        return 0
    except (FitTrackeeAuthError, FitTrackeeClientError) as exc:
        print(f"FEHLER: {exc}")
        return 2
    finally:
        db_gen.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
