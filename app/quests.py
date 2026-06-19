"""Quest seeding, creation, and progress evaluation."""

import logging
import re
from datetime import date, datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from app.models import Habit, HabitCompletion, HeroProfile, Quest, SyncEvent, XpEvent
from app.rewards import (
    CATEGORY_CHOICES,
    DURATION_CHOICES,
    EFFORT_CHOICES,
    calculate_rewards,
)
from app.stats import award_stat_xp, parse_stat_rewards, serialize_stat_rewards
from app.xp import recalc_level

logger = logging.getLogger(__name__)

HOME_HERO_DAYS = {"tag 1", "tag 2", "tag 3", "beine", "push", "pull"}

QUEST_TYPE_CHOICES = ("manual", "habit_count", "workout_count")
PERIOD_CHOICES = ("daily", "weekly", "monthly", "once")

DEFAULT_QUESTS = [
    {
        "slug": "week-warrior",
        "title": "Week Warrior",
        "description": "Complete 3 workouts in the current week.",
        "quest_type": "weekly",
        "target_value": 3,
        "xp_reward": 200,
        "attribute": "Strength",
    },
    {
        "slug": "home-hero-full-week",
        "title": "HOME HERO × SUPERMOVER 3 – Full Week",
        "description": "Complete Tag 1 – Beine, Tag 2 – Push, and Tag 3 – Pull in one week.",
        "quest_type": "weekly",
        "target_value": 3,
        "xp_reward": 200,
        "attribute": "Strength",
    },
]


def _current_week_bounds() -> tuple[date, date]:
    today = date.today()
    start = today - timedelta(days=today.weekday())  # Monday
    end = start + timedelta(days=6)  # Sunday
    return start, end


def seed_quests(db: Session) -> None:
    week_start, week_end = _current_week_bounds()
    for template in DEFAULT_QUESTS:
        existing = db.query(Quest).filter(Quest.slug == template["slug"]).first()
        if existing is None:
            quest = Quest(
                **template,
                active=True,
                current_value=0,
                period_start=datetime.combine(week_start, datetime.min.time()),
                period_end=datetime.combine(week_end, datetime.max.time()),
            )
            db.add(quest)
    db.commit()


def _count_workouts_this_week(db: Session) -> int:
    start, end = _current_week_bounds()
    return (
        db.query(SyncEvent)
        .filter(
            SyncEvent.source == "wger",
            SyncEvent.synced_at >= datetime.combine(start, datetime.min.time()),
            SyncEvent.synced_at <= datetime.combine(end, datetime.max.time()),
        )
        .count()
    )


def _count_home_hero_days_this_week(db: Session) -> int:
    """Count distinct HOME HERO day types completed this week (rough detection)."""
    start, end = _current_week_bounds()
    xp_events = (
        db.query(XpEvent)
        .filter(
            XpEvent.source == "wger",
            XpEvent.event_type == "workout_complete",
            XpEvent.created_at >= datetime.combine(start, datetime.min.time()),
            XpEvent.created_at <= datetime.combine(end, datetime.max.time()),
        )
        .all()
    )
    detected_days: set[str] = set()
    for ev in xp_events:
        title_lower = (ev.title or "").lower()
        for day_kw in HOME_HERO_DAYS:
            if day_kw in title_lower:
                detected_days.add(day_kw)
    return len(detected_days)


def _period_end_from(start_dt: datetime, period: Optional[str]) -> Optional[datetime]:
    """End-of-window datetime for a period starting at `start_dt` (None = open)."""
    period = (period or "weekly").lower()
    start = start_dt.date()
    if period == "daily":
        end = start
    elif period == "monthly":
        nxt = (
            start.replace(year=start.year + 1, month=1, day=1)
            if start.month == 12
            else start.replace(month=start.month + 1, day=1)
        )
        end = nxt - timedelta(days=1)
    elif period in ("once", "flexible"):
        return None
    else:  # weekly
        wk_start = start - timedelta(days=start.weekday())
        end = wk_start + timedelta(days=6)
    return datetime.combine(end, datetime.max.time())


def _period_window(quest: Quest) -> tuple[Optional[datetime], Optional[datetime]]:
    """
    Resolve a quest's counting window.

    An explicit period_start/period_end wins (used after a repeatable re-arm);
    otherwise the current window is derived from `period` so recurring quests
    always reflect "this day/week/month".
    """
    if quest.period_start and quest.period_end:
        return quest.period_start, quest.period_end

    period = (quest.period or "weekly").lower()
    if period == "once":
        return None, None
    today = date.today()
    if period == "daily":
        start = today
    elif period == "monthly":
        start = today.replace(day=1)
    else:  # weekly (default)
        start = today - timedelta(days=today.weekday())
    start_dt = datetime.combine(start, datetime.min.time())
    return start_dt, _period_end_from(start_dt, period)


def _count_workouts_in_period(db: Session, quest: Quest) -> int:
    start, end = _period_window(quest)
    q = db.query(SyncEvent).filter(SyncEvent.source == "wger")
    if start is not None:
        q = q.filter(SyncEvent.synced_at >= start)
    if end is not None:
        q = q.filter(SyncEvent.synced_at <= end)
    return q.count()


def _count_habit_completions_in_period(db: Session, quest: Quest) -> int:
    start, end = _period_window(quest)
    q = db.query(HabitCompletion)
    if quest.match_text:
        q = q.join(Habit, Habit.id == HabitCompletion.habit_id).filter(
            Habit.title.ilike(f"%{quest.match_text}%")
        )
    if start is not None:
        q = q.filter(HabitCompletion.completed_at >= start)
    if end is not None:
        q = q.filter(HabitCompletion.completed_at <= end)
    return q.count()


def _complete_quest(db: Session, hero: HeroProfile, quest: Quest, when: Optional[datetime] = None) -> None:
    """Award a quest's global + stat XP and either close it or re-arm it.

    Does not commit — the caller owns the transaction.
    """
    when = when or datetime.utcnow()

    hero.total_xp += quest.xp_reward
    db.add(
        XpEvent(
            event_type="quest_complete",
            source="quest",
            source_id=quest.slug,
            xp=quest.xp_reward,
            attribute=quest.attribute,
            title=f"Quest completed: {quest.title}",
            description=quest.description,
            created_at=when,
        )
    )
    stat_total = award_stat_xp(
        db,
        parse_stat_rewards(quest.stat_rewards),
        source="quest",
        source_id=quest.slug,
        title=quest.title,
        when=when,
    )
    hero.level = recalc_level(hero.total_xp)

    if quest.repeatable and (quest.period or "weekly").lower() != "once":
        # Re-arm for the next window starting now; past events won't recount.
        quest.current_value = 0
        quest.completed_at = None
        quest.period_start = when
        quest.period_end = _period_end_from(when, quest.period)
    else:
        quest.completed_at = when
        quest.active = False

    logger.info(
        "Quest completed: %s (+%d XP, +%d stat XP)", quest.title, quest.xp_reward, stat_total
    )


def evaluate_quests(db: Session, hero: HeroProfile) -> list[str]:
    """
    Recalculate progress for auto-tracked quests and complete any that hit target.

    Manual quests are user-driven and never auto-completed here.
    Returns the titles of quests newly completed.
    """
    newly_completed: list[str] = []
    active_quests = db.query(Quest).filter(Quest.active == True).all()

    workouts_this_week = _count_workouts_this_week(db)
    home_hero_days = _count_home_hero_days_this_week(db)

    for quest in active_quests:
        if quest.completed_at is not None:
            continue

        qtype = (quest.quest_type or "").lower()
        if quest.slug == "week-warrior":
            quest.current_value = workouts_this_week
        elif quest.slug == "home-hero-full-week":
            quest.current_value = home_hero_days
        elif qtype == "workout_count":
            quest.current_value = _count_workouts_in_period(db, quest)
        elif qtype == "habit_count":
            quest.current_value = _count_habit_completions_in_period(db, quest)
        elif qtype == "manual":
            continue  # progressed only by explicit user action

        if quest.target_value and quest.current_value >= quest.target_value:
            _complete_quest(db, hero, quest)
            newly_completed.append(quest.title)

    db.commit()
    return newly_completed


def complete_quest_manual(
    db: Session, quest: Quest, hero: Optional[HeroProfile] = None, when: Optional[datetime] = None
) -> bool:
    """Mark a manual quest complete (user action). Returns True if it was awarded."""
    if quest.quest_type != "manual" or not quest.active or quest.completed_at is not None:
        return False
    if hero is None:
        hero = db.query(HeroProfile).first()
        if hero is None:
            hero = HeroProfile(name="Hero", level=1, total_xp=0)
            db.add(hero)
            db.flush()
    quest.current_value = max(quest.current_value, quest.target_value)
    _complete_quest(db, hero, quest, when)
    db.commit()
    return True


def _slugify(title: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return base or "quest"


def _unique_slug(db: Session, title: str) -> str:
    base = _slugify(title)
    slug = base
    n = 2
    while db.query(Quest).filter(Quest.slug == slug).first() is not None:
        slug = f"{base}-{n}"
        n += 1
    return slug


SYSTEM_QUEST_SLUGS = {"week-warrior", "home-hero-full-week"}


def _resolve_quest_xp(
    category: Optional[str],
    duration_size: Optional[str],
    effort: Optional[str],
    xp_reward: Optional[int],
    stat_rewards: Optional[dict[str, int]],
) -> tuple[int, dict[str, int]]:
    if (
        category in CATEGORY_CHOICES
        and duration_size in DURATION_CHOICES
        and effort in EFFORT_CHOICES
        and xp_reward is None
    ):
        return calculate_rewards(duration_size, effort, category)
    return max(0, int(xp_reward or 100)), stat_rewards or {}


def create_quest(
    db: Session,
    *,
    title: str,
    description: Optional[str] = None,
    quest_type: str = "manual",
    period: str = "weekly",
    target_value: int = 1,
    match_text: Optional[str] = None,
    xp_reward: Optional[int] = None,
    stat_rewards: Optional[dict[str, int]] = None,
    repeatable: bool = False,
    active: bool = True,
    category: Optional[str] = None,
    duration_size: Optional[str] = None,
    effort: Optional[str] = None,
) -> Quest:
    """Create and persist a user-defined quest."""
    xp, computed_stats = _resolve_quest_xp(
        category, duration_size, effort, xp_reward, stat_rewards
    )
    if stat_rewards is not None and xp_reward is not None:
        computed_stats = stat_rewards

    quest = Quest(
        slug=_unique_slug(db, title),
        title=title.strip(),
        description=(description or "").strip() or None,
        quest_type=quest_type if quest_type in QUEST_TYPE_CHOICES else "manual",
        period=period if period in PERIOD_CHOICES else "weekly",
        target_value=max(1, int(target_value)),
        current_value=0,
        match_text=(match_text or "").strip() or None,
        xp_reward=xp,
        stat_rewards=serialize_stat_rewards(computed_stats),
        attribute="Strength",
        active=active,
        repeatable=repeatable,
        category=category if category in CATEGORY_CHOICES else None,
        duration_size=duration_size if duration_size in DURATION_CHOICES else None,
        effort=effort if effort in EFFORT_CHOICES else None,
    )
    db.add(quest)
    db.commit()
    db.refresh(quest)
    return quest


def update_quest(
    db: Session,
    quest: Quest,
    *,
    title: str,
    description: Optional[str],
    quest_type: str,
    period: str,
    target_value: int,
    match_text: Optional[str],
    xp_reward: Optional[int] = None,
    stat_rewards: Optional[dict[str, int]] = None,
    repeatable: bool,
    active: bool,
    category: Optional[str] = None,
    duration_size: Optional[str] = None,
    effort: Optional[str] = None,
) -> Quest:
    """Update an existing quest in place (slug is preserved)."""
    xp, computed_stats = _resolve_quest_xp(
        category, duration_size, effort, xp_reward, stat_rewards
    )
    if stat_rewards is not None and xp_reward is not None:
        computed_stats = stat_rewards

    quest.title = title.strip()
    quest.description = (description or "").strip() or None
    quest.quest_type = quest_type if quest_type in QUEST_TYPE_CHOICES else quest.quest_type
    quest.period = period if period in PERIOD_CHOICES else quest.period
    quest.target_value = max(1, int(target_value))
    quest.match_text = (match_text or "").strip() or None
    quest.xp_reward = xp
    quest.stat_rewards = serialize_stat_rewards(computed_stats)
    quest.repeatable = repeatable
    quest.active = active
    quest.category = category if category in CATEGORY_CHOICES else None
    quest.duration_size = duration_size if duration_size in DURATION_CHOICES else None
    quest.effort = effort if effort in EFFORT_CHOICES else None
    quest.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(quest)
    return quest


def delete_or_archive_quest(db: Session, quest: Quest) -> str:
    """
    Hard-delete a quest if it has never been completed and has no XP events;
    deactivate (archive) it otherwise. System quests are always archived.
    Returns "deleted" or "archived".
    """
    is_system = quest.slug in SYSTEM_QUEST_SLUGS
    has_completion = quest.completed_at is not None
    has_xp_events = (
        db.query(XpEvent)
        .filter(
            XpEvent.source == "quest",
            XpEvent.source_id == str(quest.id),
        )
        .first()
        is not None
    )

    if is_system or has_completion or has_xp_events:
        quest.active = False
        quest.updated_at = datetime.utcnow()
        db.commit()
        return "archived"
    db.delete(quest)
    db.commit()
    return "deleted"


SYSTEM_QUEST_SLUGS = {"week-warrior", "home-hero-full-week"}


def delete_or_archive_quest(db: Session, quest: Quest) -> str:
    """
    Hard-delete a quest if it has no history; deactivate (archive) it otherwise.
    System quests are always archived, never deleted.
    Returns "deleted" or "archived".
    """
    # System quests are never hard-deleted
    if quest.slug in SYSTEM_QUEST_SLUGS:
        quest.active = False
        db.commit()
        return "archived"

    has_xp_events = (
        db.query(XpEvent)
        .filter(XpEvent.source == "quest", XpEvent.source_id == quest.slug)
        .first()
        is not None
    )
    has_history = has_xp_events or quest.completed_at is not None

    if has_history:
        quest.active = False
        db.commit()
        return "archived"

    db.delete(quest)
    db.commit()
    return "deleted"
