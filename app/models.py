from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class HeroProfile(Base):
    __tablename__ = "hero_profile"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), default="Hero")
    level: Mapped[int] = mapped_column(Integer, default=1)
    total_xp: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class SyncEvent(Base):
    __tablename__ = "sync_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(50), default="wger")
    source_id: Mapped[str] = mapped_column(String(100), index=True)
    source_hash: Mapped[str] = mapped_column(String(64))
    synced_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    raw_summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    xp_awarded: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)


class XpEvent(Base):
    __tablename__ = "xp_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_type: Mapped[str] = mapped_column(String(50))
    source: Mapped[str] = mapped_column(String(50), default="wger")
    source_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    xp: Mapped[int] = mapped_column(Integer)
    attribute: Mapped[str] = mapped_column(String(50), default="Strength")
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Quest(Base):
    __tablename__ = "quests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # quest_type: manual | habit_count | workout_count (legacy seeds: weekly)
    quest_type: Mapped[str] = mapped_column(String(50), default="weekly")
    # period: daily | weekly | monthly | once
    period: Mapped[str] = mapped_column(String(20), default="weekly")
    target_value: Mapped[int] = mapped_column(Integer, default=1)
    current_value: Mapped[int] = mapped_column(Integer, default=0)
    # Optional substring matched against habit titles for habit_count quests
    match_text: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    xp_reward: Mapped[int] = mapped_column(Integer, default=100)
    # JSON object of {stat_key: xp} awarded on completion
    stat_rewards: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    attribute: Mapped[str] = mapped_column(String(50), default="Strength")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    # When true, the quest re-arms for the next period after completion
    repeatable: Mapped[bool] = mapped_column(Boolean, default=False)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    period_start: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    period_end: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    # Python-side defaults (not server_default) so values are always supplied on
    # insert, even on databases migrated via ALTER TABLE ADD COLUMN.
    created_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=True
    )
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=True
    )


class ApiCheckEvent(Base):
    __tablename__ = "api_check_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    checked_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    endpoint: Mapped[str] = mapped_column(String(200))
    http_status: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    result_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    is_success: Mapped[bool] = mapped_column(Boolean, default=False)


class Achievement(Base):
    __tablename__ = "achievements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    unlocked_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class Habit(Base):
    """A repeatable, user-defined action that can be completed for XP."""

    __tablename__ = "habits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    # recurrence: daily | weekly | monthly | flexible
    recurrence: Mapped[str] = mapped_column(String(20), default="daily")
    target_count: Mapped[int] = mapped_column(Integer, default=1)
    base_xp_reward: Mapped[int] = mapped_column(Integer, default=20)
    # JSON object of {stat_key: xp} awarded on each completion
    stat_rewards: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class HabitCompletion(Base):
    """One recorded completion of a habit (the auditable source of habit XP)."""

    __tablename__ = "habit_completions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    habit_id: Mapped[int] = mapped_column(Integer, index=True)
    completed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    xp_awarded: Mapped[int] = mapped_column(Integer, default=0)
    stat_xp_awarded: Mapped[int] = mapped_column(Integer, default=0)


class HeroStat(Base):
    """Cumulative stat XP per attribute (feeds the future stats / radar screen)."""

    __tablename__ = "hero_stats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    stat_key: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    xp: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class StatXpEvent(Base):
    """Audit record for a single stat-XP award (separate from global XpEvent)."""

    __tablename__ = "stat_xp_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    stat_key: Mapped[str] = mapped_column(String(50), index=True)
    xp: Mapped[int] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(String(50), default="habit")
    source_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    title: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
