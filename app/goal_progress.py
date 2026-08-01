"""
Turn stored history into the week outcomes that momentum and streaks consume.

This is the thin database-facing layer for app/momentum.py: it reads what
actually happened and hands plain WeekOutcome values to the pure calculation.
Keeping the two apart means the scoring rules stay unit-testable without a
session, and this module stays a mapping exercise with no arithmetic of its own.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from app.goals import counts_towards_progress, pause_intervals_of
from app.models import Goal, QuestCompletion, Quest
from app.momentum import (
    MOMENTUM_WEEKS,
    MomentumResult,
    StreakResult,
    WeekOutcome,
    calculate_momentum,
    calculate_streak,
    completed_week_starts,
    week_start,
)
from app.quests import app_date_of, app_today

logger = logging.getLogger(__name__)


def _bounds(monday: date) -> tuple[datetime, datetime]:
    return (
        datetime.combine(monday, datetime.min.time()),
        datetime.combine(monday + timedelta(days=6), datetime.max.time()),
    )


def weekly_quests_of(db: Session, goal: Goal) -> list[Quest]:
    """The goal's recurring weekly quests — what a "satisfied week" means."""
    return (
        db.query(Quest)
        .filter(
            Quest.goal_id == goal.id,
            Quest.is_milestone == False,  # noqa: E712
            Quest.period == "weekly",
        )
        .all()
    )


def pause_windows(db: Session, goal: Goal) -> list[tuple[date, Optional[date]]]:
    """Recorded breaks as calendar days in the application timezone.

    An open interval keeps `None` as its end, meaning "still running".
    """
    return [
        (app_date_of(iv.started_at), app_date_of(iv.ended_at) if iv.ended_at else None)
        for iv in pause_intervals_of(db, goal)
    ]


def week_was_paused(windows: list[tuple[date, Optional[date]]], monday: date) -> bool:
    """Whether a break touched this calendar week at all.

    Deliberately generous: *any* overlap neutralises the whole week. A pause
    that starts on Wednesday must not leave Monday and Tuesday behind as an
    unfinished week that reads as a failure.
    """
    sunday = monday + timedelta(days=6)
    return any(
        start <= sunday and (end is None or end >= monday) for start, end in windows
    )


def week_outcome(
    db: Session,
    goal: Goal,
    monday: date,
    quests: Optional[list[Quest]] = None,
    windows: Optional[list[tuple[date, Optional[date]]]] = None,
) -> WeekOutcome:
    """How one calendar week went for a goal.

    A week is satisfied when every weekly quest of the goal was rewarded in it,
    which QuestCompletion records exactly once per period. Weeks before the goal
    existed carry has_data=False so they read as "no history", not as a failure.
    """
    quests = weekly_quests_of(db, goal) if quests is None else quests
    windows = pause_windows(db, goal) if windows is None else windows
    start, end = _bounds(monday)

    if not quests:
        return WeekOutcome(week_start=monday, achieved=0, target=0, has_data=False)

    # Before the goal existed there is nothing to judge.
    created = goal.created_at.date() if goal.created_at else monday
    if monday + timedelta(days=6) < created:
        return WeekOutcome(week_start=monday, achieved=0, target=0, has_data=False)

    quest_ids = [q.id for q in quests]
    rewarded = (
        db.query(QuestCompletion)
        .filter(
            QuestCompletion.quest_id.in_(quest_ids),
            QuestCompletion.completed_at >= start,
            QuestCompletion.completed_at <= end,
        )
        .count()
    )

    # Whether *this* week was a break, from the recorded intervals. The current
    # status is deliberately not applied backwards: resuming a goal must not
    # turn old breaks into failures, and pausing today must not erase a week
    # that really was missed while the goal was running.
    return WeekOutcome(
        week_start=monday,
        achieved=rewarded,
        target=len(quest_ids),
        paused=week_was_paused(windows, monday),
    )


def goal_momentum(db: Session, goal: Goal, today: Optional[date] = None) -> MomentumResult:
    today = today or app_today()
    quests = weekly_quests_of(db, goal)
    windows = pause_windows(db, goal)
    outcomes = [
        week_outcome(db, goal, monday, quests, windows)
        for monday in completed_week_starts(today, MOMENTUM_WEEKS)
    ]
    return calculate_momentum(outcomes)


def goal_streak(
    db: Session, goal: Goal, today: Optional[date] = None, history_weeks: int = 52
) -> StreakResult:
    """Current and best streak, looking back up to `history_weeks`."""
    today = today or app_today()
    quests = weekly_quests_of(db, goal)
    windows = pause_windows(db, goal)
    completed = [
        week_outcome(db, goal, monday, quests, windows)
        for monday in completed_week_starts(today, history_weeks)
    ]
    current = week_outcome(db, goal, week_start(today), quests, windows)
    return calculate_streak(completed, current_week=current)


def goal_week_summary(db: Session, goal: Goal, today: Optional[date] = None) -> dict:
    """Everything a goal card needs: this week, momentum, streaks, status."""
    today = today or app_today()
    quests = weekly_quests_of(db, goal)
    windows = pause_windows(db, goal)
    this_week = week_outcome(db, goal, week_start(today), quests, windows)
    momentum = goal_momentum(db, goal, today)
    streak = goal_streak(db, goal, today)
    return {
        "this_week": this_week,
        "momentum": momentum,
        "streak": streak,
        "scored": counts_towards_progress(goal.status),
        "weekly_quests": len(quests),
    }
