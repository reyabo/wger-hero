"""Tests for weekday planning and the day/week aggregation.

Everything here passes an explicit reference date: the aggregation must never
read the clock itself, so any day of any year can be pinned in a test.
"""

from datetime import date, datetime, time, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.goals import (
    STATUS_ARCHIVED,
    STATUS_COMPLETED,
    STATUS_PAUSED,
    create_goal,
    set_status,
)
from app.habits import (
    ISO_WEEKDAYS,
    WEEKDAY_LABELS,
    InvalidWeekdayError,
    create_habit,
    parse_weekdays,
    scheduled_weekdays,
    set_weekdays,
    update_habit,
)
from app.models import Base, HabitCompletion, HabitScheduleDay, Quest, QuestCompletion
from app.planning import (
    day_plan,
    parse_reference_date,
    quests_for_period,
    today_plan,
    week_days,
    week_plan,
)


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


MONDAY = date(2026, 7, 27)      # a Monday
SUNDAY = date(2026, 8, 2)


def _habit(db, title="Gewohnheit", days=None, goal=None, **kw):
    habit = create_habit(db, title=title, base_xp_reward=10, **kw)
    if goal is not None:
        habit.goal_id = goal.id
        db.commit()
    if days:
        set_weekdays(db, habit, days)
    return habit


def _completed(db, habit, day, times=1):
    for _ in range(times):
        db.add(HabitCompletion(
            habit_id=habit.id,
            completed_at=datetime.combine(day, time(9, 0)),
            xp_awarded=10,
        ))
    db.commit()


# ---------------------------------------------------------------------------
# Weekday validation
# ---------------------------------------------------------------------------

def test_every_iso_weekday_is_accepted():
    assert parse_weekdays([str(d) for d in ISO_WEEKDAYS]) == list(ISO_WEEKDAYS)


def test_weekdays_are_sorted_and_deduplicated():
    assert parse_weekdays(["3", "1", "3", "1"]) == [1, 3]


def test_empty_selection_is_valid():
    assert parse_weekdays([]) == []
    assert parse_weekdays(["", "  "]) == []


@pytest.mark.parametrize("bad", ["0", "8", "-1", "montag", "1.5", "1;DROP"])
def test_invalid_weekday_is_rejected(bad):
    with pytest.raises(InvalidWeekdayError):
        parse_weekdays([bad])


def test_weekday_labels_cover_all_seven_days():
    assert sorted(WEEKDAY_LABELS) == list(ISO_WEEKDAYS)


# ---------------------------------------------------------------------------
# Storing a plan
# ---------------------------------------------------------------------------

def test_a_habit_without_a_plan_stays_valid(db):
    habit = _habit(db)
    assert scheduled_weekdays(db, habit) == []


def test_several_weekdays_can_be_stored(db):
    habit = _habit(db, days=[1, 3, 5])
    assert scheduled_weekdays(db, habit) == [1, 3, 5]


def test_a_weekday_is_never_stored_twice(db):
    habit = _habit(db, days=[1, 1, 1])
    rows = db.query(HabitScheduleDay).filter(HabitScheduleDay.habit_id == habit.id).all()
    assert len(rows) == 1


def test_duplicate_weekday_is_refused_by_the_database(db):
    habit = _habit(db, days=[1])
    db.add(HabitScheduleDay(habit_id=habit.id, iso_weekday=1))
    with pytest.raises(Exception):
        db.commit()
    db.rollback()


def test_setting_a_plan_twice_is_idempotent(db):
    habit = _habit(db, days=[2, 4])
    set_weekdays(db, habit, [2, 4])
    assert scheduled_weekdays(db, habit) == [2, 4]


def test_clearing_the_plan_removes_it_controlled(db):
    habit = _habit(db, days=[2, 4])
    set_weekdays(db, habit, [])
    assert scheduled_weekdays(db, habit) == []


def test_renaming_a_habit_keeps_its_plan(db):
    habit = _habit(db, title="Alt", days=[1, 6])
    update_habit(
        db, habit, title="Neu", description=None, active=True,
        recurrence="daily", target_count=1, base_xp_reward=10,
    )
    assert habit.title == "Neu"
    assert scheduled_weekdays(db, habit) == [1, 6]


def test_deactivating_a_habit_keeps_plan_and_history(db):
    habit = _habit(db, days=[1])
    _completed(db, habit, MONDAY)
    update_habit(
        db, habit, title=habit.title, description=None, active=False,
        recurrence="daily", target_count=1, base_xp_reward=10,
    )
    assert scheduled_weekdays(db, habit) == [1]
    assert db.query(HabitCompletion).count() == 1


def test_changing_the_plan_never_touches_completions(db):
    habit = _habit(db, days=[1, 2])
    _completed(db, habit, MONDAY)
    set_weekdays(db, habit, [5])
    assert db.query(HabitCompletion).count() == 1


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------

def test_a_week_runs_monday_to_sunday():
    days = week_days(date(2026, 7, 29))     # a Wednesday
    assert days[0] == MONDAY
    assert days[-1] == SUNDAY
    assert len(days) == 7


def test_monday_and_sunday_belong_to_the_same_week():
    assert week_days(MONDAY) == week_days(SUNDAY)


def test_a_week_may_span_a_month_change():
    days = week_days(date(2026, 7, 30))
    assert date(2026, 7, 31) in days and date(2026, 8, 1) in days


def test_a_week_may_span_a_year_change():
    days = week_days(date(2027, 1, 1))
    assert days[0] == date(2026, 12, 28)
    assert date(2027, 1, 3) in days


@pytest.mark.parametrize("day", [date(2026, 3, 29), date(2026, 10, 25)])
def test_dst_days_do_not_shift_the_week(day):
    days = week_days(day)
    assert days[0].weekday() == 0
    assert days[-1] - days[0] == timedelta(days=6)
    assert day in days


def test_reference_date_defaults_to_today():
    assert parse_reference_date(None, MONDAY) == (MONDAY, None)
    assert parse_reference_date("", MONDAY) == (MONDAY, None)


def test_an_explicit_reference_date_is_used():
    day, error = parse_reference_date("2026-08-01", MONDAY)
    assert day == date(2026, 8, 1)
    assert error is None


@pytest.mark.parametrize("bad", ["quatsch", "2026-13-01", "01.08.2026", "2026-08-01; DROP"])
def test_an_invalid_reference_date_falls_back_with_a_message(bad):
    day, error = parse_reference_date(bad, MONDAY)
    assert day == MONDAY
    assert error and "JJJJ-MM-TT" in error


def test_previous_and_next_week(db):
    plan = week_plan(db, date(2026, 7, 29))
    assert plan.previous == date(2026, 7, 20)
    assert plan.next == date(2026, 8, 3)


# ---------------------------------------------------------------------------
# Day plan
# ---------------------------------------------------------------------------

def test_an_unplanned_habit_is_available_every_day(db):
    _habit(db, title="Flexibel")
    for day in week_days(MONDAY):
        plan = day_plan(db, day)
        assert [p.habit.title for p in plan.flexible] == ["Flexibel"]
        assert plan.planned == []


def test_a_monday_habit_appears_only_on_monday(db):
    _habit(db, title="Nur Montag", days=[1])
    assert [p.habit.title for p in day_plan(db, MONDAY).planned] == ["Nur Montag"]
    for day in week_days(MONDAY)[1:]:
        assert day_plan(db, day).planned == []


def test_a_habit_on_several_days_appears_on_each(db):
    _habit(db, title="Mo Mi Fr", days=[1, 3, 5])
    planned = [bool(day_plan(db, d).planned) for d in week_days(MONDAY)]
    assert planned == [True, False, True, False, True, False, False]


def test_a_planned_habit_is_not_listed_as_flexible(db):
    _habit(db, title="Geplant", days=[1])
    assert day_plan(db, MONDAY).flexible == []


def test_an_inactive_habit_does_not_appear(db):
    habit = _habit(db, title="Inaktiv", days=[1])
    habit.active = False
    db.commit()
    assert day_plan(db, MONDAY).planned == []


def test_a_completed_habit_reads_as_done(db):
    habit = _habit(db, days=[1])
    _completed(db, habit, MONDAY)
    entry = day_plan(db, MONDAY).planned[0]
    assert entry.done
    assert entry.status_label == "erledigt"


def test_an_open_habit_reads_as_open(db):
    _habit(db, days=[1])
    entry = day_plan(db, MONDAY).planned[0]
    assert not entry.done
    assert entry.status_label == "offen"


def test_a_completion_counts_only_on_its_own_day(db):
    habit = _habit(db, days=[1, 2])
    _completed(db, habit, MONDAY)
    assert day_plan(db, MONDAY).planned[0].done
    assert not day_plan(db, MONDAY + timedelta(days=1)).planned[0].done


def test_a_target_count_above_one_needs_that_many_completions(db):
    habit = _habit(db, days=[1], target_count=3)
    _completed(db, habit, MONDAY, times=2)
    assert not day_plan(db, MONDAY).planned[0].done
    _completed(db, habit, MONDAY)
    assert day_plan(db, MONDAY).planned[0].done


def test_an_empty_day_is_reported_as_empty(db):
    assert day_plan(db, MONDAY).is_empty


# ---------------------------------------------------------------------------
# Goals and pauses
# ---------------------------------------------------------------------------

def test_a_habit_of_an_active_goal_is_shown_normally(db):
    goal = create_goal(db, title="Kraftpfad")
    _habit(db, days=[1], goal=goal)
    entry = day_plan(db, MONDAY).planned[0]
    assert entry.goal is goal
    assert not entry.paused


def test_a_habit_of_a_paused_goal_is_marked_neutral(db):
    goal = create_goal(db, title="Pause")
    _habit(db, days=[1], goal=goal)
    set_status(db, goal, STATUS_PAUSED)
    entry = day_plan(db, MONDAY).planned[0]
    assert entry.paused
    assert entry.status_label == "pausiert"


def test_a_day_inside_a_recorded_pause_is_neutral(db):
    goal = create_goal(db, title="Ziel")
    _habit(db, days=[1, 4], goal=goal)
    from app.models import GoalPauseInterval

    set_status(db, goal, STATUS_PAUSED)
    set_status(db, goal, "active")          # break is over; the record remains
    iv = db.query(GoalPauseInterval).one()
    iv.started_at = datetime.combine(MONDAY - timedelta(days=1), time(12, 0))
    iv.ended_at = datetime.combine(MONDAY + timedelta(days=1), time(12, 0))
    db.commit()

    assert day_plan(db, MONDAY).planned[0].paused
    assert not day_plan(db, MONDAY + timedelta(days=3)).planned[0].paused


def test_a_habit_of_a_completed_goal_is_not_a_current_task(db):
    goal = create_goal(db, title="Fertig")
    _habit(db, days=[1], goal=goal)
    set_status(db, goal, STATUS_COMPLETED)
    assert day_plan(db, MONDAY).planned == []


def test_a_habit_of_an_archived_goal_is_not_a_current_task(db):
    goal = create_goal(db, title="Archiv")
    _habit(db, days=[1], goal=goal)
    set_status(db, goal, STATUS_ARCHIVED)
    assert day_plan(db, MONDAY).planned == []


def test_history_of_an_archived_goal_survives(db):
    goal = create_goal(db, title="Archiv")
    habit = _habit(db, days=[1], goal=goal)
    _completed(db, habit, MONDAY)
    set_status(db, goal, STATUS_ARCHIVED)
    assert db.query(HabitCompletion).count() == 1
    assert scheduled_weekdays(db, habit) == [1]


# ---------------------------------------------------------------------------
# Week plan
# ---------------------------------------------------------------------------

def test_a_week_plan_has_all_seven_days(db):
    plan = week_plan(db, date(2026, 7, 29))
    assert len(plan.days) == 7
    assert plan.monday == MONDAY and plan.sunday == SUNDAY
    assert [d.iso_weekday for d in plan.days] == [1, 2, 3, 4, 5, 6, 7]


def test_planned_habits_land_on_the_right_days(db):
    _habit(db, title="Di", days=[2])
    _habit(db, title="So", days=[7])
    plan = week_plan(db, MONDAY)
    assert [p.habit.title for p in plan.days[1].planned] == ["Di"]
    assert [p.habit.title for p in plan.days[6].planned] == ["So"]
    assert plan.days[0].planned == []


def test_a_completion_is_assigned_to_the_right_date(db):
    habit = _habit(db, title="Mi", days=[3])
    _completed(db, habit, MONDAY + timedelta(days=2))
    plan = week_plan(db, MONDAY)
    assert plan.days[2].planned[0].done
    assert plan.days[2].done_count == 1


def test_the_previous_week_shows_its_own_completions(db):
    habit = _habit(db, title="Mo", days=[1])
    _completed(db, habit, MONDAY - timedelta(days=7))
    previous = week_plan(db, MONDAY - timedelta(days=7))
    assert previous.days[0].planned[0].done
    assert not week_plan(db, MONDAY).days[0].planned[0].done


# ---------------------------------------------------------------------------
# Quests
# ---------------------------------------------------------------------------

def _weekly_quest(db, slug="wq", target=3, current=0, goal=None):
    quest = Quest(
        slug=slug, title=slug, period="weekly", quest_type="workout_count",
        target_value=target, current_value=current, active=True, repeatable=True,
        goal_id=goal.id if goal else None,
    )
    db.add(quest)
    db.commit()
    return quest


def test_weekly_quests_appear_in_the_week_view(db):
    _weekly_quest(db)
    assert len(week_plan(db, MONDAY).quests) == 1


def test_quest_progress_and_target_are_shown(db):
    _weekly_quest(db, target=3)
    view = week_plan(db, MONDAY).quests[0]
    assert view.target == 3
    assert view.current == 0
    assert view.status_label == "offen"


def test_an_unrewarded_but_satisfied_quest_says_so(db):
    quest = _weekly_quest(db, target=1)
    quest.quest_type = "manual"
    quest.current_value = 1
    db.commit()
    view = week_plan(db, MONDAY).quests[0]
    assert view.satisfied and not view.rewarded
    assert view.status_label == "erfüllt, noch nicht belohnt"


def test_an_already_rewarded_quest_says_so(db):
    from app.quests import completion_key

    quest = _weekly_quest(db)
    db.add(QuestCompletion(
        quest_id=quest.id, dedup_key=completion_key(quest), xp_awarded=100,
    ))
    db.commit()
    view = week_plan(db, MONDAY).quests[0]
    assert view.rewarded
    assert view.status_label == "für diesen Zeitraum belohnt"


def test_showing_a_quest_never_completes_it(db):
    quest = _weekly_quest(db, target=1)
    week_plan(db, MONDAY)
    today_plan(db, MONDAY)
    db.refresh(quest)
    assert quest.completed_at is None
    assert db.query(QuestCompletion).count() == 0


def test_a_milestone_is_not_a_weekly_quest(db):
    quest = _weekly_quest(db)
    quest.is_milestone = True
    db.commit()
    assert week_plan(db, MONDAY).quests == []


def test_a_quest_of_a_paused_goal_is_neutral(db):
    goal = create_goal(db, title="Ziel")
    _weekly_quest(db, goal=goal)
    set_status(db, goal, STATUS_PAUSED)
    view = week_plan(db, MONDAY).quests[0]
    assert view.paused
    assert view.status_label == "pausiert"


def test_daily_and_weekly_quests_reach_the_day_view(db):
    _weekly_quest(db, slug="woche")
    daily = _weekly_quest(db, slug="tag")
    daily.period = "daily"
    db.commit()
    slugs = {v.quest.slug for v in today_plan(db, MONDAY)["quests"]}
    assert slugs == {"woche", "tag"}


def test_quests_for_period_filters_by_period(db):
    _weekly_quest(db, slug="woche")
    assert quests_for_period(db, MONDAY, periods=("daily",)) == []
    assert len(quests_for_period(db, MONDAY, periods=("weekly",))) == 1


def test_today_plan_reports_its_week_bounds(db):
    data = today_plan(db, date(2026, 7, 29))
    assert data["monday"] == MONDAY
    assert data["sunday"] == SUNDAY


# ---------------------------------------------------------------------------
# The aggregation must stay read-only and clock-free
# ---------------------------------------------------------------------------

def test_planning_never_reads_the_clock():
    """Every reference date is injected, so any day can be pinned in a test."""
    import ast
    from pathlib import Path

    tree = ast.parse((Path(__file__).resolve().parent.parent / "app" / "planning.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            assert name not in ("today", "now", "utcnow", "app_today"), (
                f"app/planning.py must not call {name}()"
            )


def test_planning_never_writes():
    """No commit, no add, no delete — a view may not change anything."""
    import ast
    from pathlib import Path

    tree = ast.parse((Path(__file__).resolve().parent.parent / "app" / "planning.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in ("commit", "add", "delete", "flush"), (
                f"app/planning.py must not call {node.func.attr}()"
            )


def test_planning_opens_no_database_connection():
    import ast
    from pathlib import Path

    tree = ast.parse((Path(__file__).resolve().parent.parent / "app" / "planning.py").read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
    assert "app.database" not in imported
    assert "fastapi" not in imported
