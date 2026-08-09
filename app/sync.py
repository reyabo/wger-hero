"""Sync orchestrator: fetch wger data → normalize → award XP → update DB."""

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

import httpx
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.models import HeroProfile, HeroStat, StatXpEvent, SyncEvent, XpEvent
from app.stats import award_stat_xp
from app.wger_client import WgerClient, WgerClientError
from app.xp import calculate_xp_awards, level_from_total_xp

logger = logging.getLogger(__name__)


class NormalizedExerciseLog(BaseModel):
    name: str
    # Stable identity of the exercise this set belongs to: the wger exercise id
    # when known, otherwise the resolved name. Two sets of the same exercise
    # share it, which is what makes "how many exercises" answerable.
    exercise_id: Optional[int] = None
    sets: Optional[int] = None
    reps: Optional[int] = None
    duration_seconds: Optional[int] = None
    weight: Optional[float] = None
    rir: Optional[float] = None

    @property
    def exercise_key(self) -> str:
        return f"id:{self.exercise_id}" if self.exercise_id is not None else f"name:{self.name}"


class NormalizedWorkoutLog(BaseModel):
    source_id: str
    date: date
    title: Optional[str] = None
    routine_name: Optional[str] = None
    # One entry per performed *set* — wger returns a WorkoutLog per set, and
    # throwing that away would lose the RIR and weight of every set but one.
    exercises: list[NormalizedExerciseLog] = field(default_factory=list)
    raw_hash: str
    # Whether exercise detail was fetched at all. False means "not asked",
    # which is a different fact from "the workout had no exercises".
    logs_fetched: bool = True

    @property
    def distinct_exercise_count(self) -> int:
        """How many *different* exercises the session contained.

        Five sets of two exercises are two exercises, not five. Counted by the
        stable exercise id where there is one, falling back to the resolved
        name so an unresolvable exercise still groups with itself.
        """
        return len({e.exercise_key for e in self.exercises})

    def summary(self) -> str:
        """The one line stored on SyncEvent.raw_summary."""
        if not self.logs_fetched:
            return f"{self.date}: exercise details disabled"
        count = self.distinct_exercise_count
        word = "exercise" if count == 1 else "exercises"
        return f"{self.date}: {count} {word}"


@dataclass
class SyncResult:
    new_sessions: int = 0
    skipped_sessions: int = 0
    total_xp_awarded: int = 0
    xp_events: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _sanitize_error(e: Exception) -> str:
    """
    Convert an exception into a short, safe message that contains no private data.
    Never include exception messages directly — they may embed tokens or raw payloads.
    """
    msg = str(e)
    if isinstance(e, WgerClientError):
        if "401" in msg:
            return "401 Unauthorized: check API token"
        if "403" in msg:
            return "403 Forbidden: token may lack required permissions"
        if "404" in msg:
            return "404 Not Found: endpoint may not exist on this wger version"
        if "429" in msg:
            return "429 Too Many Requests: rate limited by wger"
        if "5" in msg[:3]:
            return "5xx Server Error: wger returned a server error"
        return "API error: unexpected HTTP status"
    if isinstance(e, (httpx.ConnectError, httpx.TimeoutException, httpx.RequestError)):
        return "Connection error: could not reach wger"
    if isinstance(e, (KeyError, TypeError, ValueError)):
        return "Unexpected response shape: missing or invalid field in wger response"
    return f"Sync error: {type(e).__name__}"


def _hash_session(data: dict) -> str:
    serialized = json.dumps(data, sort_keys=True, default=str)
    return hashlib.sha256(serialized.encode()).hexdigest()


def _safe_int(value) -> Optional[int]:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _safe_float(value) -> Optional[float]:
    try:
        return float(value) if value not in (None, "", "None") else None
    except (TypeError, ValueError):
        return None


def _log_sort_key(log: dict) -> str:
    """Stable order for hashing. Log ids are strings on the current wger."""
    return str(log.get("id") or "")


def _normalize_session(
    session: dict,
    logs: list[dict],
    exercise_names: dict[int, str],
    logs_fetched: bool = True,
) -> NormalizedWorkoutLog:
    session_id = str(session.get("id") or "unknown")

    session_date_raw = session.get("date", "")
    try:
        session_date = date.fromisoformat(str(session_date_raw))
    except (ValueError, TypeError):
        session_date = date.today()

    # notes may contain private data — use only as label, never log the content
    title = session.get("notes") or None

    # Hash a *stably ordered* copy: the API may paginate or reorder logs, and
    # the same set of sets must always produce the same hash. Adding, removing
    # or changing a set still changes it.
    ordered_logs = sorted(logs, key=_log_sort_key)

    exercises: list[NormalizedExerciseLog] = []
    for log_entry in ordered_logs:
        try:
            ex_id = log_entry.get("exercise") or log_entry.get("exercise_id")
            ex_id_int = _safe_int(ex_id)
            ex_name = (
                exercise_names.get(ex_id_int, f"Exercise {ex_id_int}")
                if ex_id_int is not None
                else "Unknown"
            )
            # `repetitions` is what the current wger sends, as a string.
            # `reps` stays a fallback for older exports only.
            reps_raw = log_entry.get("repetitions")
            if reps_raw is None:
                reps_raw = log_entry.get("reps")
            exercises.append(
                NormalizedExerciseLog(
                    name=ex_name,
                    exercise_id=ex_id_int,
                    sets=None,
                    reps=_safe_int(reps_raw),
                    weight=_safe_float(log_entry.get("weight")),
                    rir=_safe_float(log_entry.get("rir")),
                )
            )
        except Exception:
            # Malformed log entry — skip, don't crash the entire sync. The
            # entry itself is never logged: it may carry private detail.
            logger.warning("Skipping malformed log entry (unexpected field type)")

    raw_hash = _hash_session({"session": session, "logs": ordered_logs})

    return NormalizedWorkoutLog(
        source_id=f"session-{session_id}",
        date=session_date,
        title=title,
        routine_name=None,
        exercises=exercises,
        raw_hash=raw_hash,
        logs_fetched=logs_fetched,
    )


# Only this award feeds a canonical stat. It is the one the reward rules
# already label "Strength", and app/stats.py already knows "strength".
WORKOUT_COMPLETE_EVENT = "workout_complete"
STRENGTH_ATTRIBUTE = "Strength"
STRENGTH_STAT_KEY = "strength"


def _strength_rewards(awards) -> dict[str, int]:
    """Mirror the workout-completion award onto the canonical strength stat.

    Nothing is invented: only an award that is already both
    ``workout_complete`` and ``Strength`` is mirrored, one to one. Conditioning
    and RIR bonuses stay global XP, because no canonical mapping exists for
    them in this project.
    """
    total = sum(
        a.xp
        for a in awards
        if a.event_type == WORKOUT_COMPLETE_EVENT and a.attribute == STRENGTH_ATTRIBUTE
    )
    return {STRENGTH_STAT_KEY: total} if total > 0 else {}


def _revoke_session_awards(
    db: Session,
    hero: HeroProfile,
    source_id: str,
    existing: SyncEvent,
    result: "SyncResult",
) -> None:
    """Take back everything a previous sync awarded for one session.

    The audit rows are the source of truth, not ``SyncEvent.xp_awarded``: there
    may be several awards per session, and a stored total that disagrees with
    them would otherwise be subtracted blindly. When the two disagree the
    difference is reported instead of guessed at, and the auditable rows win.
    """
    xp_rows = (
        db.query(XpEvent)
        .filter(XpEvent.source == "wger", XpEvent.source_id == source_id)
        .all()
    )
    stat_rows = (
        db.query(StatXpEvent)
        .filter(StatXpEvent.source == "wger", StatXpEvent.source_id == source_id)
        .all()
    )

    audited_xp = sum(row.xp for row in xp_rows)
    if xp_rows and existing.xp_awarded != audited_xp:
        message = (
            f"Re-sync of {source_id}: stored total {existing.xp_awarded} XP does "
            f"not match {audited_xp} XP in the audit rows; the audit rows were used"
        )
        logger.warning("%s", message)
        result.errors.append(message)

    # With no audit rows at all — a database from before those were written —
    # the stored total is the only thing left to go on.
    hero.total_xp = max(0, hero.total_xp - (audited_xp if xp_rows else existing.xp_awarded))

    for row in stat_rows:
        stat = db.query(HeroStat).filter(HeroStat.stat_key == row.stat_key).first()
        if stat is not None:
            stat.xp = max(0, stat.xp - row.xp)
        db.delete(row)

    for row in xp_rows:
        db.delete(row)
    db.flush()


def _get_or_create_hero(db: Session, name: str = "Hero") -> HeroProfile:
    hero = db.query(HeroProfile).first()
    if hero is None:
        hero = HeroProfile(name=name, level=1, total_xp=0)
        db.add(hero)
        db.commit()
        db.refresh(hero)
    return hero


def _update_hero_level(db: Session, hero: HeroProfile) -> None:
    """Recalculate the level. Deliberately does **not** commit.

    The caller owns the transaction, so a failure after this point cannot leave
    a hero whose level was written while its awards were rolled back.
    """
    level, _, _ = level_from_total_xp(hero.total_xp)
    if level != hero.level:
        hero.level = level
    hero.updated_at = datetime.utcnow()


async def sync_workouts(
    db: Session,
    client: WgerClient,
    hero_name: str = "Hero",
    fetch_exercise_logs: bool = True,
    sync_from_date: Optional[date] = None,
) -> SyncResult:
    result = SyncResult()

    hero = _get_or_create_hero(db, hero_name)

    # Fetch sessions first — if this fails there is nothing else to do
    try:
        sessions = await client.get_workout_sessions(since=sync_from_date)
    except Exception as e:
        sanitized = _sanitize_error(e)
        result.errors.append(sanitized)
        logger.error("Sync failed fetching sessions: %s", type(e).__name__)
        return result

    # Enforce SYNC_FROM_DATE locally in case wger API ignores date__gte
    if sync_from_date is not None:
        before = len(sessions)
        sessions = [
            s for s in sessions
            if s.get("date") and s["date"] >= sync_from_date.isoformat()
        ]
        filtered = before - len(sessions)
        if filtered:
            logger.info("Local date filter removed %d session(s) before %s", filtered, sync_from_date)

    # Only fetch logs and exercise catalog when the feature is enabled
    # and when there are sessions to enrich.
    exercise_names: dict[int, str] = {}
    all_logs: list[dict] = []

    if fetch_exercise_logs and sessions:
        try:
            all_logs = await client.get_exercise_logs()
        except Exception as e:
            # 404 is already handled inside get_exercise_logs() and returns [].
            # Any other error: proceed without logs rather than failing the sync.
            logger.warning("Could not fetch exercise logs: %s — proceeding without them", type(e).__name__)

        if all_logs:
            try:
                exercises = await client.get_exercises()
                for ex in exercises:
                    ex_id = _safe_int(ex.get("id"))
                    name = ex.get("name") or ex.get("uuid") or f"Exercise {ex_id}"
                    if ex_id is not None:
                        exercise_names[ex_id] = name
            except Exception as e:
                logger.warning("Could not fetch exercise catalog: %s", type(e).__name__)

    # Group logs by session id. The current wger links a WorkoutLog to its
    # session through `session`; the old `workout` key no longer exists on
    # either side. Ids are strings (UUIDs) and stay strings — coercing them to
    # int would silently drop every log.
    logs_by_session: dict[str, list[dict]] = {}
    for log in all_logs:
        raw_session = log.get("session")
        if raw_session in (None, ""):
            continue        # a log without a session cannot be attributed
        logs_by_session.setdefault(str(raw_session), []).append(log)

    for session in sessions:
        try:
            session_id = str(session.get("id") or "")
            session_logs = logs_by_session.get(session_id, []) if session_id else []

            normalized = _normalize_session(
                session, session_logs, exercise_names, logs_fetched=fetch_exercise_logs
            )
        except Exception as e:
            sanitized = _sanitize_error(e)
            result.errors.append(sanitized)
            logger.warning("Could not normalize session: %s", type(e).__name__)
            continue

        # Deduplication check
        existing = (
            db.query(SyncEvent)
            .filter(
                SyncEvent.source == "wger",
                SyncEvent.source_id == normalized.source_id,
            )
            .first()
        )
        if existing:
            if existing.source_hash == normalized.raw_hash:
                result.skipped_sessions += 1
                continue
            # The session changed. Take back exactly what was awarded for it —
            # read from the audit rows themselves rather than trusting a single
            # stored total — and delete those rows, so no orphaned audit entry
            # survives into the new award set.
            _revoke_session_awards(db, hero, normalized.source_id, existing, result)
            db.delete(existing)
            db.flush()

        awards = calculate_xp_awards(normalized)
        session_xp = sum(a.xp for a in awards)
        awarded_at = datetime.combine(normalized.date, datetime.min.time())

        for award in awards:
            xp_event = XpEvent(
                event_type=award.event_type,
                source="wger",
                source_id=normalized.source_id,
                xp=award.xp,
                attribute=award.attribute,
                title=award.title,
                description=award.description,
                created_at=awarded_at,
            )
            db.add(xp_event)
            result.xp_events.append(f"+{award.xp} {award.attribute} — {award.title}")

        # The completion award is the one that also feeds the canonical
        # strength stat. Conditioning and RIR stay global-only: this project
        # has no canonical mapping for them, and inventing one here would
        # quietly change the balance.
        stat_rewards = _strength_rewards(awards)
        if stat_rewards:
            award_stat_xp(
                db,
                stat_rewards,
                source="wger",
                source_id=normalized.source_id,
                title=normalized.title or "Workout",
                when=awarded_at,
            )

        sync_event = SyncEvent(
            source="wger",
            source_id=normalized.source_id,
            source_hash=normalized.raw_hash,
            synced_at=datetime.utcnow(),
            raw_summary=normalized.summary(),
            xp_awarded=session_xp,
        )
        db.add(sync_event)

        hero.total_xp += session_xp
        result.total_xp_awarded += session_xp
        result.new_sessions += 1

    # One commit for the whole sync: awards, stat awards, hero totals, level
    # and sync events either all land or none do.
    _update_hero_level(db, hero)
    db.commit()

    logger.info(
        "Sync complete: %d new, %d skipped, %d XP awarded",
        result.new_sessions,
        result.skipped_sessions,
        result.total_xp_awarded,
    )
    return result
