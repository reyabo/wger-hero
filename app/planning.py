"""
What is planned for a day and for a calendar week.

This module only *reads*. It joins habits, their optional weekday plan, the
completions that actually happened, the quests of the period and the goals
behind them into the shape the /today and /week templates need. It awards no
XP, writes no QuestCompletion, recalculates no momentum and opens no database
connection of its own — the session and the reference date are always passed in,
so every function here is testable against any day of the year.

The rules it displays all come from elsewhere: quest windows and counters from
app/quests.py, pause overlap from app/goal_progress.py, calendar weeks from
app/momentum.py. There is deliberately no second implementation of any of them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from app.goal_progress import day_was_paused, pause_windows, week_was_paused
from app.goals import STATUS_ACTIVE, STATUS_ARCHIVED, STATUS_COMPLETED, STATUS_PAUSED
from app.habits import WEEKDAY_LABELS, WEEKDAY_SHORT, scheduled_weekdays
from app.models import Goal, Habit, HabitCompletion, Quest
from app.momentum import week_end, week_start
from app.quests import already_rewarded, count_quest_progress

logger = logging.getLogger(__name__)

__all__ = [
    "DayPlan",
    "PlannedHabit",
    "QuestView",
    "WeekPlan",
    "day_plan",
    "parse_reference_date",
    "week_days",
    "week_plan",
]


# ---------------------------------------------------------------------------
# Dates — all derived from the central helpers, none recomputed here
# ---------------------------------------------------------------------------

def week_days(day: date) -> list[date]:
    """The seven days of the calendar week containing `day`, Monday first."""
    monday = week_start(day)
    return [monday + timedelta(days=n) for n in range(7)]


def parse_reference_date(raw: Optional[str], today: date) -> tuple[date, Optional[str]]:
    """Validate an optional ?date= parameter into a reference day.

    Returns ``(day, error)``. Anything unparseable falls back to `today` with a
    plain German message, so a mistyped or manipulated URL shows the current
    week instead of an error page. Only a complete ISO date is accepted; the
    value is never passed anywhere near a query.
    """
    raw = (raw or "").strip()
    if not raw:
        return today, None
    try:
        return date.fromisoformat(raw), None
    except ValueError:
        return today, "Ungültiges Datum (erwartet: JJJJ-MM-TT). Es wird die aktuelle Woche gezeigt."


# ---------------------------------------------------------------------------
# View models
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PlannedHabit:
    """One habit as it appears on one day."""

    habit: Habit
    goal: Optional[Goal]
    weekdays: list[int]
    completions: int
    paused: bool

    @property
    def planned(self) -> bool:
        """Whether the habit is tied to weekdays at all."""
        return bool(self.weekdays)

    @property
    def done(self) -> bool:
        return self.completions >= max(1, self.habit.target_count or 1)

    @property
    def status_label(self) -> str:
        """A word, not just a colour — the badge text is the status."""
        if self.paused:
            return "pausiert"
        if self.done:
            return "erledigt"
        return "offen"

    @property
    def weekday_labels(self) -> list[str]:
        return [WEEKDAY_SHORT[d] for d in self.weekdays]


@dataclass(frozen=True)
class QuestView:
    """A quest as the day and week views show it — read-only, never advanced."""

    quest: Quest
    goal: Optional[Goal]
    # None when a past or future week is displayed: the counters of app/quests.py
    # answer for the *current* period only, and inventing a historical count here
    # would be a second, unverifiable progress engine.
    current: Optional[int]
    target: int
    rewarded: bool
    paused: bool

    @property
    def has_progress(self) -> bool:
        return self.current is not None

    @property
    def satisfied(self) -> bool:
        return self.current is not None and self.target > 0 and self.current >= self.target

    @property
    def status_label(self) -> str:
        if self.paused:
            return "pausiert"
        if self.rewarded:
            return "für diesen Zeitraum belohnt"
        if self.current is None:
            return "Zähler nur für den laufenden Zeitraum"
        if self.satisfied:
            return "erfüllt, noch nicht belohnt"
        return "offen"


@dataclass(frozen=True)
class DayPlan:
    day: date
    planned: list[PlannedHabit] = field(default_factory=list)
    flexible: list[PlannedHabit] = field(default_factory=list)

    @property
    def iso_weekday(self) -> int:
        return self.day.isoweekday()

    @property
    def weekday_label(self) -> str:
        return WEEKDAY_LABELS[self.iso_weekday]

    @property
    def is_empty(self) -> bool:
        return not self.planned and not self.flexible

    @property
    def done_count(self) -> int:
        return sum(1 for p in self.planned if p.done)


@dataclass(frozen=True)
class WeekPlan:
    monday: date
    days: list[DayPlan]
    quests: list[QuestView]

    @property
    def sunday(self) -> date:
        return self.monday + timedelta(days=6)

    @property
    def previous(self) -> date:
        return self.monday - timedelta(days=7)

    @property
    def next(self) -> date:
        return self.monday + timedelta(days=7)


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def _day_bounds(day: date) -> tuple[datetime, datetime]:
    return (
        datetime.combine(day, datetime.min.time()),
        datetime.combine(day, datetime.max.time()),
    )


def _active_habits(db: Session) -> list[Habit]:
    return (
        db.query(Habit)
        .filter(Habit.active == True)  # noqa: E712
        .order_by(Habit.sort_order, Habit.title)
        .all()
    )


def _goals_by_id(db: Session) -> dict[int, Goal]:
    return {g.id: g for g in db.query(Goal).all()}


def _completions_on(db: Session, day: date, habit_ids: list[int]) -> dict[int, int]:
    """How often each habit was completed on `day`, keyed by habit id."""
    if not habit_ids:
        return {}
    start, end = _day_bounds(day)
    rows = (
        db.query(HabitCompletion)
        .filter(
            HabitCompletion.habit_id.in_(habit_ids),
            HabitCompletion.completed_at >= start,
            HabitCompletion.completed_at <= end,
        )
        .all()
    )
    counts: dict[int, int] = {}
    for row in rows:
        counts[row.habit_id] = counts.get(row.habit_id, 0) + 1
    return counts


def _is_current_task(goal: Optional[Goal]) -> bool:
    """Whether a goal's habits should still be presented as today's work.

    Straight from the existing status rules: only an active goal is scored, and
    a completed or archived goal is history rather than a current task. Paused
    stays visible but neutral — a break is not a failure and not a to-do either.
    """
    if goal is None:
        return True
    return goal.status == STATUS_ACTIVE


def day_plan(
    db: Session,
    day: date,
    *,
    habits: Optional[list[Habit]] = None,
    goals: Optional[dict[int, Goal]] = None,
    pause_cache: Optional[dict[int, list]] = None,
) -> DayPlan:
    """Everything scheduled or relevant for one calendar day.

    `planned` holds habits tied to this weekday; `flexible` holds active habits
    with no weekday plan, which stay available every day exactly as before this
    feature existed. Habits of completed or archived goals are left out of both
    — they are history, not today's work.
    """
    habits = _active_habits(db) if habits is None else habits
    goals = _goals_by_id(db) if goals is None else goals
    pause_cache = {} if pause_cache is None else pause_cache

    counts = _completions_on(db, day, [h.id for h in habits])
    iso = day.isoweekday()

    planned: list[PlannedHabit] = []
    flexible: list[PlannedHabit] = []
    for habit in habits:
        goal = goals.get(habit.goal_id) if habit.goal_id else None
        if goal is not None and goal.status in (STATUS_COMPLETED, STATUS_ARCHIVED):
            continue

        paused = False
        if goal is not None:
            if goal.id not in pause_cache:
                pause_cache[goal.id] = pause_windows(db, goal)
            paused = day_was_paused(pause_cache[goal.id], day) or (
                goal.status == STATUS_PAUSED
            )

        entry = PlannedHabit(
            habit=habit,
            goal=goal,
            weekdays=scheduled_weekdays(db, habit),
            completions=counts.get(habit.id, 0),
            paused=paused,
        )
        if iso in entry.weekdays:
            planned.append(entry)
        elif not entry.weekdays:
            flexible.append(entry)

    return DayPlan(day=day, planned=planned, flexible=flexible)


def quests_for_period(
    db: Session,
    day: date,
    *,
    periods: tuple[str, ...] = ("weekly",),
    goals: Optional[dict[int, Goal]] = None,
    pause_cache: Optional[dict[int, list]] = None,
    is_current: bool = True,
) -> list[QuestView]:
    """Active quests of the given periods that are relevant on `day`.

    A quest with an explicit fixed window is only listed while `day` falls into
    it. A recurring quest has no fixed window and is listed every period.

    Progress and "already rewarded" both come from app/quests.py. When a week
    other than the running one is displayed, `is_current` is False and progress
    is reported as unknown rather than as the current week's number. Nothing
    here completes a quest: showing a quest must never award it.
    """
    goals = _goals_by_id(db) if goals is None else goals
    pause_cache = {} if pause_cache is None else pause_cache

    views: list[QuestView] = []
    quests = (
        db.query(Quest)
        .filter(Quest.active == True)  # noqa: E712
        .order_by(Quest.sort_order, Quest.id)
        .all()
    )
    for quest in quests:
        if (quest.period or "weekly").lower() not in periods:
            continue
        if quest.is_milestone:
            continue

        # Only an explicitly pinned window restricts visibility. The derived
        # window of a recurring quest always describes the *current* period, so
        # using it as a filter would hide every quest on any other week.
        if quest.period_start and quest.period_end:
            if not (quest.period_start.date() <= day <= quest.period_end.date()):
                continue

        goal = goals.get(quest.goal_id) if quest.goal_id else None
        if goal is not None and goal.status in (STATUS_COMPLETED, STATUS_ARCHIVED):
            continue

        paused = False
        if goal is not None:
            if goal.id not in pause_cache:
                pause_cache[goal.id] = pause_windows(db, goal)
            paused = week_was_paused(pause_cache[goal.id], week_start(day)) or (
                goal.status == STATUS_PAUSED
            )

        if is_current:
            counted = count_quest_progress(db, quest)
            current = int(quest.current_value if counted is None else counted)
        else:
            current = None
        views.append(
            QuestView(
                quest=quest,
                goal=goal,
                current=current,
                target=int(quest.target_value or 0),
                rewarded=already_rewarded(db, quest),
                paused=paused,
            )
        )
    return views


def week_plan(db: Session, day: date, *, today: Optional[date] = None) -> WeekPlan:
    """The whole Monday–Sunday week containing `day`.

    `today` decides whether the displayed week is the running one; it is passed
    in rather than read from a clock so any week can be pinned in a test.
    """
    habits = _active_habits(db)
    goals = _goals_by_id(db)
    pause_cache: dict[int, list] = {}
    monday = week_start(day)

    days = [
        day_plan(db, d, habits=habits, goals=goals, pause_cache=pause_cache)
        for d in week_days(day)
    ]
    quests = quests_for_period(
        db,
        monday,
        periods=("weekly",),
        goals=goals,
        pause_cache=pause_cache,
        is_current=today is None or week_start(today) == monday,
    )
    return WeekPlan(monday=monday, days=days, quests=quests)


def today_plan(db: Session, day: date) -> dict:
    """Everything /today shows, for one calendar day."""
    goals = _goals_by_id(db)
    pause_cache: dict[int, list] = {}
    plan = day_plan(db, day, goals=goals, pause_cache=pause_cache)
    quests = quests_for_period(
        db, day, periods=("daily", "weekly"), goals=goals, pause_cache=pause_cache
    )
    return {
        "plan": plan,
        "quests": quests,
        "monday": week_start(day),
        "sunday": week_end(day),
    }
