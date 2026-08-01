"""
Manual habit logic: create, edit, and complete user-defined habits.

Completing a habit is the core transparent reward loop:
  user completes habit -> global XP + stat XP awarded by visible rules ->
  auditable XpEvent / StatXpEvent / HabitCompletion rows written.

No AI, no hidden weighting, no productivity pressure — the app only records
what the user did and applies the rewards they defined.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from app.models import Habit, HabitCompletion, HabitScheduleDay, HeroProfile, XpEvent
from app.rewards import (
    CATEGORY_CHOICES,
    DURATION_CHOICES,
    EFFORT_CHOICES,
    calculate_rewards,
)
from app.stats import award_stat_xp, parse_stat_rewards, serialize_stat_rewards
from app.xp import recalc_level

logger = logging.getLogger(__name__)

# Two completions of the same habit within this window are treated as an
# accidental double-click and the second is ignored (no duplicate XP).
DOUBLE_CLICK_WINDOW_SECONDS = 2

RECURRENCE_CHOICES = ("daily", "weekly", "monthly", "flexible")

# ISO weekdays, the only accepted schedule values. Numbers rather than German
# labels, so the stored data stays language-independent and validatable.
ISO_WEEKDAYS = (1, 2, 3, 4, 5, 6, 7)
WEEKDAY_LABELS = {
    1: "Montag",
    2: "Dienstag",
    3: "Mittwoch",
    4: "Donnerstag",
    5: "Freitag",
    6: "Samstag",
    7: "Sonntag",
}
WEEKDAY_SHORT = {1: "Mo", 2: "Di", 3: "Mi", 4: "Do", 5: "Fr", 6: "Sa", 7: "So"}


class InvalidWeekdayError(ValueError):
    """A schedule value that is not an ISO weekday was submitted."""


def parse_weekdays(raw_values) -> list[int]:
    """Validate submitted weekdays into a sorted, duplicate-free ISO list.

    Raises InvalidWeekdayError on anything that is not 1…7, so a manipulated
    form is refused on the server rather than silently corrected. An empty
    selection is valid and means "not planned for a fixed day".
    """
    days: set[int] = set()
    for value in raw_values or []:
        text = str(value).strip()
        if not text:
            continue
        try:
            day = int(text)
        except ValueError:
            raise InvalidWeekdayError(f"Kein gültiger Wochentag: {text[:20]}")
        if day not in ISO_WEEKDAYS:
            raise InvalidWeekdayError(f"Kein gültiger Wochentag: {day}")
        days.add(day)
    return sorted(days)


def scheduled_weekdays(db: Session, habit: Habit) -> list[int]:
    """The ISO weekdays a habit is planned for, ascending. Empty = flexible."""
    rows = (
        db.query(HabitScheduleDay)
        .filter(HabitScheduleDay.habit_id == habit.id)
        .order_by(HabitScheduleDay.iso_weekday)
        .all()
    )
    return [row.iso_weekday for row in rows]


def set_weekdays(db: Session, habit: Habit, days: list[int]) -> list[int]:
    """Replace a habit's plan with exactly `days`, without touching history.

    Only the difference is written, so re-saving an unchanged habit leaves the
    rows alone. Completions are never read or modified here — clearing a plan
    removes the plan, never the record of what was actually done.
    """
    days = parse_weekdays(days)
    current = set(scheduled_weekdays(db, habit))
    wanted = set(days)

    for day in current - wanted:
        db.query(HabitScheduleDay).filter(
            HabitScheduleDay.habit_id == habit.id,
            HabitScheduleDay.iso_weekday == day,
        ).delete()
    for day in sorted(wanted - current):
        db.add(HabitScheduleDay(habit_id=habit.id, iso_weekday=day))

    db.commit()
    return sorted(wanted)


@dataclass
class CompletionResult:
    ok: bool
    reason: Optional[str] = None  # "inactive" | "duplicate" | None on success
    xp_awarded: int = 0
    stat_xp_awarded: int = 0
    stat_rewards: dict[str, int] = field(default_factory=dict)


def _resolve_xp_and_stats(
    category: Optional[str],
    duration_size: Optional[str],
    effort: Optional[str],
    base_xp_reward: Optional[int],
    stat_rewards: Optional[dict[str, int]],
) -> tuple[int, dict[str, int]]:
    """Auto-calculate XP/stats from category/duration/effort, or use explicit values."""
    if (
        category in CATEGORY_CHOICES
        and duration_size in DURATION_CHOICES
        and effort in EFFORT_CHOICES
        and base_xp_reward is None
    ):
        return calculate_rewards(duration_size, effort, category)
    return max(0, int(base_xp_reward or 20)), stat_rewards or {}


def create_habit(
    db: Session,
    *,
    title: str,
    description: Optional[str] = None,
    active: bool = True,
    recurrence: str = "daily",
    target_count: int = 1,
    base_xp_reward: Optional[int] = None,
    stat_rewards: Optional[dict[str, int]] = None,
    category: Optional[str] = None,
    duration_size: Optional[str] = None,
    effort: Optional[str] = None,
) -> Habit:
    """Create and persist a new habit.

    If category/duration_size/effort are all valid and base_xp_reward is not
    given, XP and stat rewards are calculated automatically. Pass base_xp_reward
    explicitly to use a fixed value (backward-compatible with old data).
    """
    xp, computed_stats = _resolve_xp_and_stats(
        category, duration_size, effort, base_xp_reward, stat_rewards
    )
    if stat_rewards is not None and base_xp_reward is not None:
        # Caller provided explicit values — respect them.
        computed_stats = stat_rewards

    habit = Habit(
        title=title.strip(),
        description=(description or "").strip() or None,
        active=active,
        recurrence=recurrence if recurrence in RECURRENCE_CHOICES else "daily",
        target_count=max(1, int(target_count)),
        base_xp_reward=xp,
        stat_rewards=serialize_stat_rewards(computed_stats),
        category=category if category in CATEGORY_CHOICES else None,
        duration_size=duration_size if duration_size in DURATION_CHOICES else None,
        effort=effort if effort in EFFORT_CHOICES else None,
    )
    db.add(habit)
    db.commit()
    db.refresh(habit)
    return habit


def update_habit(
    db: Session,
    habit: Habit,
    *,
    title: str,
    description: Optional[str],
    active: bool,
    recurrence: str,
    target_count: int,
    base_xp_reward: Optional[int] = None,
    stat_rewards: Optional[dict[str, int]] = None,
    category: Optional[str] = None,
    duration_size: Optional[str] = None,
    effort: Optional[str] = None,
) -> Habit:
    """Update an existing habit in place."""
    xp, computed_stats = _resolve_xp_and_stats(
        category, duration_size, effort, base_xp_reward, stat_rewards
    )
    if stat_rewards is not None and base_xp_reward is not None:
        computed_stats = stat_rewards

    habit.title = title.strip()
    habit.description = (description or "").strip() or None
    habit.active = active
    habit.recurrence = recurrence if recurrence in RECURRENCE_CHOICES else "daily"
    habit.target_count = max(1, int(target_count))
    habit.base_xp_reward = xp
    habit.stat_rewards = serialize_stat_rewards(computed_stats)
    habit.category = category if category in CATEGORY_CHOICES else None
    habit.duration_size = duration_size if duration_size in DURATION_CHOICES else None
    habit.effort = effort if effort in EFFORT_CHOICES else None
    habit.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(habit)
    return habit


def archive_habit(db: Session, habit: Habit) -> Habit:
    """Deactivate a habit, always keeping the row and its history.

    Unlike :func:`delete_or_archive_habit`, this never removes anything — it is
    used where the intent is explicitly "archive", also for a habit that has no
    completions yet and could technically be hard-deleted.
    """
    habit.active = False
    habit.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(habit)
    return habit


def delete_or_archive_habit(db: Session, habit: Habit) -> str:
    """
    Hard-delete a habit if it has no completions; deactivate (archive) it otherwise.
    History, XP events, and stat events are never deleted.
    Returns "deleted" or "archived".
    """
    has_completions = (
        db.query(HabitCompletion)
        .filter(HabitCompletion.habit_id == habit.id)
        .first()
        is not None
    )
    if has_completions:
        habit.active = False
        habit.updated_at = datetime.utcnow()
        db.commit()
        return "archived"
    db.delete(habit)
    db.commit()
    return "deleted"


def _get_or_create_hero(db: Session, name: str = "Hero") -> HeroProfile:
    hero = db.query(HeroProfile).first()
    if hero is None:
        hero = HeroProfile(name=name, level=1, total_xp=0)
        db.add(hero)
        db.commit()
        db.refresh(hero)
    return hero


def complete_habit(
    db: Session,
    habit: Habit,
    hero: Optional[HeroProfile] = None,
    when: Optional[datetime] = None,
) -> CompletionResult:
    """
    Record a habit completion and award global + stat XP.

    Guards:
      - inactive habits cannot be completed
      - a second completion within DOUBLE_CLICK_WINDOW_SECONDS is ignored
        (accidental double-click protection)

    Does the full award atomically and commits once.
    """
    now = when or datetime.utcnow()

    if not habit.active:
        return CompletionResult(ok=False, reason="inactive")

    # Accidental double-click protection: reject a near-instant repeat.
    last = (
        db.query(HabitCompletion)
        .filter(HabitCompletion.habit_id == habit.id)
        .order_by(HabitCompletion.completed_at.desc())
        .first()
    )
    if last is not None:
        delta = abs((now - last.completed_at).total_seconds())
        if delta < DOUBLE_CLICK_WINDOW_SECONDS:
            logger.info("Ignored duplicate completion for habit %s", habit.id)
            return CompletionResult(ok=False, reason="duplicate")

    if hero is None:
        hero = _get_or_create_hero(db)

    rewards = parse_stat_rewards(habit.stat_rewards)
    base_xp = max(0, int(habit.base_xp_reward or 0))

    # Global XP (drives level) — auditable XpEvent.
    if base_xp > 0:
        db.add(
            XpEvent(
                event_type="habit_complete",
                source="habit",
                source_id=str(habit.id),
                xp=base_xp,
                attribute="Habit",
                title=habit.title,
                description="Habit completed",
                created_at=now,
            )
        )
        hero.total_xp += base_xp

    # Stat XP (drives attributes) — kept separate from global XP.
    stat_total = award_stat_xp(
        db,
        rewards,
        source="habit",
        source_id=str(habit.id),
        title=habit.title,
        when=now,
    )

    db.add(
        HabitCompletion(
            habit_id=habit.id,
            completed_at=now,
            xp_awarded=base_xp,
            stat_xp_awarded=stat_total,
        )
    )

    hero.level = recalc_level(hero.total_xp)
    hero.updated_at = now
    db.commit()

    return CompletionResult(
        ok=True,
        xp_awarded=base_xp,
        stat_xp_awarded=stat_total,
        stat_rewards=rewards,
    )
