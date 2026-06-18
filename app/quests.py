"""Quest seeding and progress evaluation."""

import logging
from datetime import date, datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from app.models import HeroProfile, Quest, SyncEvent, XpEvent

logger = logging.getLogger(__name__)

HOME_HERO_DAYS = {"tag 1", "tag 2", "tag 3", "beine", "push", "pull"}

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


def evaluate_quests(db: Session, hero: HeroProfile) -> list[str]:
    """
    Recalculate quest progress. Returns list of newly completed quest titles.
    """
    newly_completed: list[str] = []
    active_quests = db.query(Quest).filter(Quest.active == True).all()

    workouts_this_week = _count_workouts_this_week(db)
    home_hero_days = _count_home_hero_days_this_week(db)

    for quest in active_quests:
        if quest.completed_at is not None:
            continue

        if quest.slug == "week-warrior":
            quest.current_value = workouts_this_week
        elif quest.slug == "home-hero-full-week":
            quest.current_value = home_hero_days

        if quest.current_value >= quest.target_value:
            quest.completed_at = datetime.utcnow()
            quest.active = False
            hero.total_xp += quest.xp_reward

            xp_event = XpEvent(
                event_type="quest_complete",
                source="quest",
                source_id=quest.slug,
                xp=quest.xp_reward,
                attribute=quest.attribute,
                title=f"Quest completed: {quest.title}",
                description=quest.description,
                created_at=datetime.utcnow(),
            )
            db.add(xp_event)
            newly_completed.append(quest.title)
            logger.info("Quest completed: %s (+%d XP)", quest.title, quest.xp_reward)

    db.commit()
    return newly_completed
