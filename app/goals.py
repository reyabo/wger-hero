"""
Goals: long-running personal aims that habits and quests can belong to.

A goal is never deleted through the UI — it is archived. Pausing is a
first-class state, not an absence of activity: a paused goal keeps its XP, its
history and its place in the list, and nothing about it is presented as a
failure or a debt.

The status rules are pure functions so they can be unit-tested without a
database, like app/xp.py and app/rewards.py.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Goal, GoalPauseInterval, Habit, Quest

STATUS_ACTIVE = "active"
STATUS_PAUSED = "paused"
STATUS_COMPLETED = "completed"
STATUS_ARCHIVED = "archived"

GOAL_STATUSES = (STATUS_ACTIVE, STATUS_PAUSED, STATUS_COMPLETED, STATUS_ARCHIVED)

STATUS_LABELS = {
    STATUS_ACTIVE: "Aktiv",
    STATUS_PAUSED: "Pausiert",
    STATUS_COMPLETED: "Abgeschlossen",
    STATUS_ARCHIVED: "Archiviert",
}

# Which statuses a goal may move to. Archiving is always allowed; nothing ever
# leads to deletion.
ALLOWED_TRANSITIONS: dict[str, tuple[str, ...]] = {
    STATUS_ACTIVE: (STATUS_PAUSED, STATUS_COMPLETED, STATUS_ARCHIVED),
    STATUS_PAUSED: (STATUS_ACTIVE, STATUS_COMPLETED, STATUS_ARCHIVED),
    STATUS_COMPLETED: (STATUS_ACTIVE, STATUS_ARCHIVED),
    STATUS_ARCHIVED: (STATUS_ACTIVE,),
}


def can_transition(current: str, target: str) -> bool:
    if current == target:
        return True  # idempotent, never an error
    return target in ALLOWED_TRANSITIONS.get(current, ())


def counts_towards_progress(status: str) -> bool:
    """Whether a goal's weeks should be scored at all.

    Paused, completed and archived goals are not scored, so a break never
    lowers momentum or breaks a streak — it simply is not counted.
    """
    return status == STATUS_ACTIVE


def slugify(title: str, existing: Optional[set[str]] = None) -> str:
    """Stable, readable slug. Appends -2, -3 … only on a real collision."""
    base = re.sub(r"[^a-z0-9]+", "-", title.strip().lower()).strip("-") or "goal"
    if existing is None or base not in existing:
        return base
    n = 2
    while f"{base}-{n}" in existing:
        n += 1
    return f"{base}-{n}"


# ---------------------------------------------------------------------------
# Database-facing helpers
# ---------------------------------------------------------------------------

def _unique_slug(db: Session, title: str) -> str:
    existing = {g.slug for g in db.query(Goal).all()}
    return slugify(title, existing)


def create_goal(
    db: Session,
    *,
    title: str,
    description: Optional[str] = None,
    short_label: Optional[str] = None,
    status: str = STATUS_ACTIVE,
    sort_order: int = 0,
    slug: Optional[str] = None,
) -> Goal:
    goal = Goal(
        slug=slug or _unique_slug(db, title),
        title=title.strip(),
        description=(description or "").strip() or None,
        short_label=(short_label or "").strip() or None,
        status=status if status in GOAL_STATUSES else STATUS_ACTIVE,
        sort_order=int(sort_order),
    )
    db.add(goal)
    db.commit()
    db.refresh(goal)
    return goal


def update_goal(
    db: Session,
    goal: Goal,
    *,
    title: str,
    description: Optional[str],
    short_label: Optional[str],
    sort_order: int = 0,
) -> Goal:
    """Edit the descriptive fields. Status changes go through set_status()."""
    goal.title = title.strip()
    goal.description = (description or "").strip() or None
    goal.short_label = (short_label or "").strip() or None
    goal.sort_order = int(sort_order)
    goal.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(goal)
    return goal


def open_pause_interval(db: Session, goal: Goal) -> Optional[GoalPauseInterval]:
    """The still-running break of a goal, if it is paused right now."""
    return (
        db.query(GoalPauseInterval)
        .filter(GoalPauseInterval.goal_id == goal.id, GoalPauseInterval.ended_at.is_(None))
        .first()
    )


def pause_intervals_of(db: Session, goal: Goal) -> list[GoalPauseInterval]:
    """Every recorded break of a goal, oldest first."""
    return (
        db.query(GoalPauseInterval)
        .filter(GoalPauseInterval.goal_id == goal.id)
        .order_by(GoalPauseInterval.started_at, GoalPauseInterval.id)
        .all()
    )


def _record_pause_change(db: Session, goal: Goal, target: str, *, when: datetime) -> None:
    """Keep the pause history in step with a status change.

    Entering `paused` opens exactly one interval; leaving it closes the open
    one. Leaving means *any* other status: a completed or archived goal is no
    longer on a break, so an interval must never stay open behind it. Both
    directions are idempotent, so pressing pause twice records one break.
    """
    if target == STATUS_PAUSED:
        if open_pause_interval(db, goal) is None:
            db.add(GoalPauseInterval(goal_id=goal.id, started_at=when, created_at=when))
        return

    running = open_pause_interval(db, goal)
    if running is not None:
        running.ended_at = when


def set_status(db: Session, goal: Goal, target: str) -> bool:
    """Move a goal to another status. Returns False if the move is not allowed.

    Never touches habits, quests, XP or history — a status is a label on the
    goal, not a cascade. The one thing it does record is the break itself, so
    that a paused week stays neutral even after the goal is resumed.
    """
    if target not in GOAL_STATUSES or not can_transition(goal.status, target):
        return False
    now = datetime.utcnow()
    _record_pause_change(db, goal, target, when=now)
    goal.status = target
    goal.updated_at = now
    try:
        db.commit()
    except IntegrityError:
        # The partial unique index refused a second open interval — another
        # request opened one first. The goal is paused either way.
        db.rollback()
        return goal.status == target
    return True


def list_goals(db: Session, *, include_archived: bool = False) -> list[Goal]:
    query = db.query(Goal)
    if not include_archived:
        query = query.filter(Goal.status != STATUS_ARCHIVED)
    return query.order_by(Goal.sort_order, Goal.id).all()


def get_by_slug(db: Session, slug: str) -> Optional[Goal]:
    return db.query(Goal).filter(Goal.slug == slug).first()


def habits_for_goal(db: Session, goal: Goal, *, active_only: bool = False) -> list[Habit]:
    query = db.query(Habit).filter(Habit.goal_id == goal.id)
    if active_only:
        query = query.filter(Habit.active == True)  # noqa: E712
    return query.order_by(Habit.sort_order, Habit.id).all()


def quests_for_goal(db: Session, goal: Goal) -> list[Quest]:
    """Recurring quests of a goal, milestones excluded."""
    return (
        db.query(Quest)
        .filter(Quest.goal_id == goal.id, Quest.is_milestone == False)  # noqa: E712
        .order_by(Quest.sort_order, Quest.id)
        .all()
    )


def milestones_for_goal(db: Session, goal: Goal) -> list[Quest]:
    return (
        db.query(Quest)
        .filter(Quest.goal_id == goal.id, Quest.is_milestone == True)  # noqa: E712
        .order_by(Quest.sort_order, Quest.id)
        .all()
    )


def next_milestone(db: Session, goal: Goal) -> Optional[Quest]:
    """The first milestone that is not finished yet."""
    for quest in milestones_for_goal(db, goal):
        if quest.completed_at is None:
            return quest
    return None


def goal_progress(db: Session, goal: Goal) -> dict:
    """Transparent, countable progress: finished milestones out of all of them."""
    milestones = milestones_for_goal(db, goal)
    done = [m for m in milestones if m.completed_at is not None]
    total = len(milestones)
    return {
        "milestones_total": total,
        "milestones_done": len(done),
        "percent": int(len(done) / total * 100) if total else 0,
        "next": next_milestone(db, goal),
        "habits": len(habits_for_goal(db, goal, active_only=True)),
        "quests": len(quests_for_goal(db, goal)),
        "counts_towards_progress": counts_towards_progress(goal.status),
    }
