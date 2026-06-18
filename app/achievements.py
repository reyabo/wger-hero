"""Achievement definitions and unlock logic."""

import logging
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.models import Achievement, HeroProfile, SyncEvent, XpEvent

logger = logging.getLogger(__name__)

DEFAULT_ACHIEVEMENTS = [
    {
        "slug": "first-blood",
        "title": "First Blood",
        "description": "Completed your first ever workout.",
    },
    {
        "slug": "triple-threat",
        "title": "Triple Threat",
        "description": "Completed 3 workouts in a single week.",
    },
    {
        "slug": "home-hero-week",
        "title": "Full Week Hero",
        "description": "Completed all three HOME HERO × SUPERMOVER 3 training days in one week.",
    },
    {
        "slug": "four-week-streak",
        "title": "Iron Consistency",
        "description": "Trained consistently for 4 weeks in a row. (Placeholder — requires streak tracking)",
    },
]


def seed_achievements(db: Session) -> None:
    for template in DEFAULT_ACHIEVEMENTS:
        existing = db.query(Achievement).filter(Achievement.slug == template["slug"]).first()
        if existing is None:
            db.add(Achievement(**template))
    db.commit()


def _unlock(db: Session, hero: HeroProfile, slug: str, xp: int = 50) -> bool:
    achievement = db.query(Achievement).filter(Achievement.slug == slug).first()
    if achievement is None or achievement.unlocked_at is not None:
        return False
    achievement.unlocked_at = datetime.utcnow()
    hero.total_xp += xp
    db.add(
        XpEvent(
            event_type="achievement",
            source="achievement",
            source_id=slug,
            xp=xp,
            attribute="Glory",
            title=f"Achievement unlocked: {achievement.title}",
            description=achievement.description,
            created_at=datetime.utcnow(),
        )
    )
    db.commit()
    logger.info("Achievement unlocked: %s", achievement.title)
    return True


def check_achievements(db: Session, hero: HeroProfile) -> list[str]:
    """Run all achievement checks; return titles of newly unlocked achievements."""
    unlocked: list[str] = []

    total_synced = db.query(SyncEvent).filter(SyncEvent.source == "wger").count()

    if total_synced >= 1:
        if _unlock(db, hero, "first-blood"):
            unlocked.append("First Blood")

    # Check for 3 workouts in any single week (look at last 7 days for simplicity)
    week_ago = datetime.utcnow() - timedelta(days=7)
    recent_count = (
        db.query(SyncEvent)
        .filter(SyncEvent.source == "wger", SyncEvent.synced_at >= week_ago)
        .count()
    )
    if recent_count >= 3:
        if _unlock(db, hero, "triple-threat"):
            unlocked.append("Triple Threat")

    return unlocked
