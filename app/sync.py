"""Sync orchestrator: fetch wger data → normalize → award XP → update DB."""

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.models import HeroProfile, SyncEvent, XpEvent
from app.wger_client import WgerClient
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


def _hash_session(data: dict) -> str:
    serialized = json.dumps(data, sort_keys=True, default=str)
    return hashlib.sha256(serialized.encode()).hexdigest()


def _normalize_session(
    session: dict,
    logs: list[dict],
    exercise_names: dict[int, str],
) -> NormalizedWorkoutLog:
    session_id = str(session.get("id", ""))
    session_date_raw = session.get("date", "")
    try:
        session_date = date.fromisoformat(session_date_raw)
    except (ValueError, TypeError):
        session_date = date.today()

    title = session.get("notes") or None
    workout_id = session.get("workout")

    exercises: list[NormalizedExerciseLog] = []
    for log_entry in logs:
        ex_id = log_entry.get("exercise") or log_entry.get("exercise_id")
        ex_name = exercise_names.get(ex_id, f"Exercise {ex_id}") if ex_id else "Unknown"
        rir_raw = log_entry.get("rir")
        exercises.append(
            NormalizedExerciseLog(
                name=ex_name,
                sets=None,
                reps=log_entry.get("reps"),
                weight=log_entry.get("weight"),
                rir=float(rir_raw) if rir_raw not in (None, "", "None") else None,
            )
        )

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
            ex_id = ex.get("id")
            # Name may be in translations list or direct field
            name = ex.get("name") or ex.get("uuid") or f"Exercise {ex_id}"
            if ex_id:
                exercise_names[int(ex_id)] = name
    except Exception as e:
        logger.warning("Could not fetch exercise catalog: %s", type(e).__name__)

    # Fetch sessions and logs
    try:
        sessions = await client.get_workout_sessions()
    except Exception as e:
        result.errors.append(f"Failed to fetch sessions: {e}")
        logger.error("Sync failed fetching sessions: %s", e)
        return result

    try:
        all_logs = await client.get_exercise_logs()
    except Exception as e:
        logger.warning("Could not fetch exercise logs: %s — proceeding without them", e)
        all_logs = []

    # Group logs by workout/session id
    logs_by_workout: dict[int, list[dict]] = {}
    for log in all_logs:
        wid = log.get("workout")
        if wid is not None:
            logs_by_workout.setdefault(int(wid), []).append(log)

    for session in sessions:
        session_id = session.get("id")
        workout_id = session.get("workout")
        session_logs = logs_by_workout.get(workout_id, []) if workout_id else []

        normalized = _normalize_session(session, session_logs, exercise_names)

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
