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
    quest_type: Mapped[str] = mapped_column(String(50), default="weekly")
    target_value: Mapped[int] = mapped_column(Integer, default=1)
    current_value: Mapped[int] = mapped_column(Integer, default=0)
    xp_reward: Mapped[int] = mapped_column(Integer, default=100)
    attribute: Mapped[str] = mapped_column(String(50), default="Strength")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    period_start: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    period_end: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


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
