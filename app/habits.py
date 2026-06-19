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

from app.models import Habit, HabitCompletion, HeroProfile, XpEvent
from app.stats import award_stat_xp, parse_stat_rewards, serialize_stat_rewards
from app.xp import recalc_level

logger = logging.getLogger(__name__)

# Two completions of the same habit within this window are treated as an
# accidental double-click and the second is ignored (no duplicate XP).
DOUBLE_CLICK_WINDOW_SECONDS = 2

RECURRENCE_CHOICES = ("daily", "weekly", "monthly", "flexible")


@dataclass
class CompletionResult:
    ok: bool
    reason: Optional[str] = None  # "inactive" | "duplicate" | None on success
    xp_awarded: int = 0
    stat_xp_awarded: int = 0
    stat_rewards: dict[str, int] = field(default_factory=dict)


def create_habit(
    db: Session,
    *,
    title: str,
    description: Optional[str] = None,
    active: bool = True,
    recurrence: str = "daily",
    target_count: int = 1,
    base_xp_reward: int = 20,
    stat_rewards: Optional[dict[str, int]] = None,
) -> Habit:
    """Create and persist a new habit. Stat rewards are stored as JSON."""
    habit = Habit(
        title=title.strip(),
        description=(description or "").strip() or None,
        active=active,
        recurrence=recurrence if recurrence in RECURRENCE_CHOICES else "daily",
        target_count=max(1, int(target_count)),
        base_xp_reward=max(0, int(base_xp_reward)),
        stat_rewards=serialize_stat_rewards(stat_rewards or {}),
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
    base_xp_reward: int,
    stat_rewards: Optional[dict[str, int]],
) -> Habit:
    """Update an existing habit in place."""
    habit.title = title.strip()
    habit.description = (description or "").strip() or None
    habit.active = active
    habit.recurrence = recurrence if recurrence in RECURRENCE_CHOICES else "daily"
    habit.target_count = max(1, int(target_count))
    habit.base_xp_reward = max(0, int(base_xp_reward))
    habit.stat_rewards = serialize_stat_rewards(stat_rewards or {})
    habit.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(habit)
    return habit


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
