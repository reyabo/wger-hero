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

from app.models import HeroProfile, SyncEvent, XpEvent
from app.wger_client import WgerClient, WgerClientError
from app.xp import calculate_xp_awards, level_from_total_xp

logger = logging.getLogger(__name__)


class NormalizedExerciseLog(BaseModel):
    name: str
    sets: Optional[int] = None
    reps: Optional[int] = None
    duration_seconds: Optional[int] = None
    weight: Optional[float] = None
    rir: Optional[float] = None


class NormalizedWorkoutLog(BaseModel):
    source_id: str
    date: date
    title: Optional[str] = None
    routine_name: Optional[str] = None
    exercises: list[NormalizedExerciseLog] = field(default_factory=list)
    raw_hash: str


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


def _normalize_session(
    session: dict,
    logs: list[dict],
    exercise_names: dict[int, str],
) -> NormalizedWorkoutLog:
    session_id = str(session.get("id") or "unknown")

    session_date_raw = session.get("date", "")
    try:
        session_date = date.fromisoformat(str(session_date_raw))
    except (ValueError, TypeError):
        session_date = date.today()

    # notes may contain private data — use only as label, never log the content
    title = session.get("notes") or None

    exercises: list[NormalizedExerciseLog] = []
    for log_entry in logs:
        try:
            ex_id = log_entry.get("exercise") or log_entry.get("exercise_id")
            ex_id_int = _safe_int(ex_id)
            ex_name = (
                exercise_names.get(ex_id_int, f"Exercise {ex_id_int}")
                if ex_id_int is not None
                else "Unknown"
            )
            exercises.append(
                NormalizedExerciseLog(
                    name=ex_name,
                    sets=None,
                    reps=_safe_int(log_entry.get("reps")),
                    weight=_safe_float(log_entry.get("weight")),
                    rir=_safe_float(log_entry.get("rir")),
                )
            )
        except Exception:
            # Malformed log entry — skip, don't crash the entire sync
            logger.warning("Skipping malformed log entry (unexpected field type)")

    raw_hash = _hash_session({"session": session, "logs": logs})

    return NormalizedWorkoutLog(
        source_id=f"session-{session_id}",
        date=session_date,
        title=title,
        routine_name=None,
        exercises=exercises,
        raw_hash=raw_hash,
    )


def _get_or_create_hero(db: Session, name: str = "Hero") -> HeroProfile:
    hero = db.query(HeroProfile).first()
    if hero is None:
        hero = HeroProfile(name=name, level=1, total_xp=0)
        db.add(hero)
        db.commit()
        db.refresh(hero)
    return hero


def _update_hero_level(db: Session, hero: HeroProfile) -> None:
    level, _, _ = level_from_total_xp(hero.total_xp)
    if level != hero.level:
        hero.level = level
    hero.updated_at = datetime.utcnow()
    db.commit()


async def sync_workouts(db: Session, client: WgerClient, hero_name: str = "Hero") -> SyncResult:
    result = SyncResult()

    hero = _get_or_create_hero(db, hero_name)

    # Fetch exercise catalog for name resolution
    exercise_names: dict[int, str] = {}
    try:
        exercises = await client.get_exercises()
        for ex in exercises:
            ex_id = _safe_int(ex.get("id"))
            name = ex.get("name") or ex.get("uuid") or f"Exercise {ex_id}"
            if ex_id is not None:
                exercise_names[ex_id] = name
    except Exception as e:
        logger.warning("Could not fetch exercise catalog: %s", type(e).__name__)

    # Fetch sessions
    try:
        sessions = await client.get_workout_sessions()
    except Exception as e:
        sanitized = _sanitize_error(e)
        result.errors.append(sanitized)
        logger.error("Sync failed fetching sessions: %s", type(e).__name__)
        return result

    # Fetch exercise logs
    try:
        all_logs = await client.get_exercise_logs()
    except Exception as e:
        logger.warning("Could not fetch exercise logs: %s — proceeding without them", type(e).__name__)
        all_logs = []

    # Group logs by workout id
    logs_by_workout: dict[int, list[dict]] = {}
    for log in all_logs:
        wid = _safe_int(log.get("workout"))
        if wid is not None:
            logs_by_workout.setdefault(wid, []).append(log)

    for session in sessions:
        try:
            session_workout_id = _safe_int(session.get("workout"))
            session_logs = logs_by_workout.get(session_workout_id, []) if session_workout_id else []

            normalized = _normalize_session(session, session_logs, exercise_names)
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
            # Data changed — re-sync, revoke old XP first
            old_xp = existing.xp_awarded
            hero.total_xp = max(0, hero.total_xp - old_xp)
            db.delete(existing)
            db.flush()

        awards = calculate_xp_awards(normalized)
        session_xp = sum(a.xp for a in awards)

        for award in awards:
            xp_event = XpEvent(
                event_type=award.event_type,
                source="wger",
                source_id=normalized.source_id,
                xp=award.xp,
                attribute=award.attribute,
                title=award.title,
                description=award.description,
                created_at=datetime.combine(normalized.date, datetime.min.time()),
            )
            db.add(xp_event)
            result.xp_events.append(f"+{award.xp} {award.attribute} — {award.title}")

        sync_event = SyncEvent(
            source="wger",
            source_id=normalized.source_id,
            source_hash=normalized.raw_hash,
            synced_at=datetime.utcnow(),
            raw_summary=f"{normalized.date}: {len(normalized.exercises)} exercises",
            xp_awarded=session_xp,
        )
        db.add(sync_event)

        hero.total_xp += session_xp
        result.total_xp_awarded += session_xp
        result.new_sessions += 1

    _update_hero_level(db, hero)
    db.commit()

    logger.info(
        "Sync complete: %d new, %d skipped, %d XP awarded",
        result.new_sessions,
        result.skipped_sessions,
        result.total_xp_awarded,
    )
    return result
