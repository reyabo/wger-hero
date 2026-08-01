"""Tests for the opt-in starter campaign: preview, activation, idempotency."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.goals import STATUS_ACTIVE, create_goal
from app.habits import create_habit, scheduled_weekdays
from app.models import (
    Base,
    Goal,
    GoalPauseInterval,
    Habit,
    HabitCompletion,
    HabitScheduleDay,
    Quest,
    QuestCompletion,
    XpEvent,
)
from app.quests import create_quest, seed_quests
from app.starter import (
    CONFLICT,
    CONTROL_SCHEDULE,
    CREATE,
    EXTEND,
    REUSE,
    SAFETY_NOTE,
    SKIP,
    StarterError,
    apply_starter,
    plan_starter,
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


def _goal(db, slug):
    return db.query(Goal).filter(Goal.slug == slug).first()


def _quest(db, title):
    return db.query(Quest).filter(Quest.title == title).first()


def _habit(db, title):
    return db.query(Habit).filter(Habit.title == title).first()


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------

def test_the_preview_writes_nothing(db):
    plan = plan_starter(db)
    assert plan.items
    assert db.query(Goal).count() == 0
    assert db.query(Habit).count() == 0
    assert db.query(Quest).count() == 0


def test_the_preview_lists_every_area(db):
    kinds = {item.kind for item in plan_starter(db).items}
    assert kinds == {"Ziel", "Gewohnheit", "Quest", "Meilenstein"}


def test_the_preview_names_all_three_goals(db):
    names = {i.name for i in plan_starter(db).items if i.kind == "Ziel"}
    assert names == {"Kraftpfad", "Weg des Japanischen", "Körperkontrolle"}


def test_the_preview_agrees_with_the_activation(db):
    preview = [(i.kind, i.name, i.action) for i in plan_starter(db).items]
    applied = [(i.kind, i.name, i.action) for i in apply_starter(db).items]
    assert preview == applied


def test_a_second_preview_reports_no_changes(db):
    apply_starter(db)
    plan = plan_starter(db)
    assert plan.changes == 0
    assert not plan.of_action(CREATE)


# ---------------------------------------------------------------------------
# Activation and idempotency
# ---------------------------------------------------------------------------

def test_the_first_activation_creates_the_campaign(db):
    apply_starter(db)
    assert db.query(Goal).count() == 3
    assert db.query(Habit).count() == 6
    assert db.query(Quest).count() == 15


def test_a_second_activation_creates_no_duplicates(db):
    apply_starter(db)
    before = (db.query(Goal).count(), db.query(Habit).count(),
              db.query(Quest).count(), db.query(HabitScheduleDay).count())
    apply_starter(db)
    after = (db.query(Goal).count(), db.query(Habit).count(),
             db.query(Quest).count(), db.query(HabitScheduleDay).count())
    assert before == after


def test_a_third_activation_is_still_stable(db):
    apply_starter(db)
    apply_starter(db)
    plan = apply_starter(db)
    assert plan.changes == 0
    assert db.query(Quest).count() == 15


def test_activation_awards_no_xp(db):
    apply_starter(db)
    assert db.query(XpEvent).count() == 0


def test_activation_creates_no_completions(db):
    apply_starter(db)
    assert db.query(HabitCompletion).count() == 0
    assert db.query(QuestCompletion).count() == 0


def test_activation_creates_no_pause_intervals(db):
    apply_starter(db)
    assert db.query(GoalPauseInterval).count() == 0


def test_no_milestone_starts_out_completed(db):
    apply_starter(db)
    for quest in db.query(Quest).filter(Quest.is_milestone == True).all():  # noqa: E712
        assert quest.completed_at is None
        assert quest.current_value == 0


def test_a_user_edited_title_is_never_overwritten(db):
    apply_starter(db)
    goal = _goal(db, "kraftpfad")
    goal.title = "Mein eigener Kraftpfad"
    goal.description = "selbst geschrieben"
    db.commit()

    apply_starter(db)
    db.refresh(goal)
    assert goal.title == "Mein eigener Kraftpfad"
    assert goal.description == "selbst geschrieben"


def test_an_existing_goal_slug_is_reused_not_duplicated(db):
    create_goal(db, title="Eigenes Ziel", slug="kraftpfad")
    plan = apply_starter(db)
    assert db.query(Goal).filter(Goal.slug == "kraftpfad").count() == 1
    assert any(i.action == REUSE and i.name == "Kraftpfad" for i in plan.items)


def test_a_habit_of_another_goal_is_reported_as_a_conflict(db):
    other = create_goal(db, title="Anderes Ziel")
    habit = create_habit(db, title="SRS-Review", base_xp_reward=10)
    habit.goal_id = other.id
    db.commit()

    plan = apply_starter(db)
    conflicts = [i.name for i in plan.conflicts]
    assert "SRS-Review" in conflicts
    db.refresh(habit)
    assert habit.goal_id == other.id      # left untouched


def test_an_unassigned_existing_habit_is_extended_not_duplicated(db):
    create_habit(db, title="SRS-Review", base_xp_reward=10)
    plan = apply_starter(db)
    assert db.query(Habit).filter(Habit.title == "SRS-Review").count() == 1
    assert any(i.name == "SRS-Review" and i.action == EXTEND for i in plan.items)
    assert scheduled_weekdays(db, _habit(db, "SRS-Review")) == [1, 2, 3, 4, 5]


def test_a_failure_leaves_nothing_behind(db, monkeypatch):
    """A half-created campaign must not survive an error."""
    import app.starter as starter

    original = starter._japanese

    def boom(*args, **kwargs):
        raise RuntimeError("simulierter Fehler")

    monkeypatch.setattr(starter, "_japanese", boom)
    with pytest.raises(StarterError):
        apply_starter(db)
    monkeypatch.setattr(starter, "_japanese", original)

    assert db.query(Goal).count() == 0
    assert db.query(Habit).count() == 0
    assert db.query(Quest).count() == 0
    assert db.query(HabitScheduleDay).count() == 0


def test_the_error_message_carries_no_traceback(db, monkeypatch):
    import app.starter as starter

    monkeypatch.setattr(starter, "_japanese", lambda *a, **k: 1 / 0)
    with pytest.raises(StarterError) as excinfo:
        apply_starter(db)
    message = str(excinfo.value)
    assert "Traceback" not in message and "ZeroDivisionError" not in message
    assert "nichts gespeichert" in message


def test_a_failure_keeps_pre_existing_data(db, monkeypatch):
    import app.starter as starter

    mine = create_habit(db, title="Meine Gewohnheit", base_xp_reward=10)
    monkeypatch.setattr(starter, "_body_control", lambda *a, **k: 1 / 0)
    with pytest.raises(StarterError):
        apply_starter(db)

    assert db.query(Habit).filter(Habit.id == mine.id).count() == 1


# ---------------------------------------------------------------------------
# Kraftpfad
# ---------------------------------------------------------------------------

def test_the_strength_goal_is_created(db):
    apply_starter(db)
    goal = _goal(db, "kraftpfad")
    assert goal.title == "Kraftpfad"
    assert goal.status == STATUS_ACTIVE


def test_the_weekly_workout_quest_uses_the_existing_source(db):
    apply_starter(db)
    quest = _quest(db, "Dreifachschlag")
    assert quest.quest_type == "workout_count"
    assert quest.period == "weekly"
    assert quest.target_value == 3
    assert quest.repeatable


def test_an_existing_week_warrior_is_not_duplicated(db):
    seed_quests(db)
    plan = apply_starter(db)

    assert _quest(db, "Dreifachschlag") is None
    assert db.query(Quest).filter(Quest.slug == "week-warrior").count() == 1
    warrior = db.query(Quest).filter(Quest.slug == "week-warrior").one()
    assert warrior.goal_id == _goal(db, "kraftpfad").id
    assert warrior.title == "Week Warrior"        # never renamed
    assert any("Week Warrior" == i.name and i.action == EXTEND for i in plan.items)


def test_week_warrior_of_another_goal_is_a_conflict(db):
    seed_quests(db)
    other = create_goal(db, title="Anderes Ziel")
    warrior = db.query(Quest).filter(Quest.slug == "week-warrior").one()
    warrior.goal_id = other.id
    db.commit()

    plan = apply_starter(db)
    assert any(i.action == CONFLICT and i.name == "Week Warrior" for i in plan.items)
    assert _quest(db, "Dreifachschlag") is None   # still no second quest
    db.refresh(warrior)
    assert warrior.goal_id == other.id


def test_reusing_week_warrior_twice_stays_stable(db):
    seed_quests(db)
    apply_starter(db)
    plan = apply_starter(db)
    assert db.query(Quest).filter(Quest.slug == "week-warrior").count() == 1
    assert any(i.name == "Week Warrior" and i.action == REUSE for i in plan.items)


def test_the_strength_goal_has_three_milestones(db):
    apply_starter(db)
    goal = _goal(db, "kraftpfad")
    milestones = db.query(Quest).filter(
        Quest.goal_id == goal.id, Quest.is_milestone == True  # noqa: E712
    ).all()
    assert len(milestones) == 3
    assert {m.target_value for m in milestones} == {1, 4, 12}


# ---------------------------------------------------------------------------
# Weg des Japanischen
# ---------------------------------------------------------------------------

def test_the_japanese_goal_is_created(db):
    apply_starter(db)
    assert _goal(db, "weg-des-japanischen").title == "Weg des Japanischen"


def test_the_review_habit_is_planned_monday_to_friday(db):
    apply_starter(db)
    assert scheduled_weekdays(db, _habit(db, "SRS-Review")) == [1, 2, 3, 4, 5]


def test_the_review_habit_is_not_planned_on_the_weekend(db):
    apply_starter(db)
    days = scheduled_weekdays(db, _habit(db, "SRS-Review"))
    assert 6 not in days and 7 not in days


def test_the_review_quest_uses_a_stable_habit_binding(db):
    apply_starter(db)
    quest = _quest(db, "Fünf Schriftrollen")
    assert quest.quest_type == "habit_count"
    assert quest.habit_id == _habit(db, "SRS-Review").id
    assert quest.match_text is None       # no fuzzy fallback for new data
    assert quest.target_value == 5


def test_the_session_quest_uses_the_japanese_source(db):
    apply_starter(db)
    quest = _quest(db, "Zwei Gespräche mit dem Sensei")
    assert quest.quest_type == "japanese_session_count"
    assert quest.target_value == 2
    assert quest.period == "weekly"


def test_no_second_habit_rewards_a_japanese_session(db):
    """The SAVE import stays the single reward channel for a session."""
    apply_starter(db)
    titles = {h.title for h in db.query(Habit).all()}
    assert not any("Coach" in t or "Session" in t for t in titles)


def test_the_starter_imports_no_japanese_save(db):
    from app.models import JapaneseSaveImport

    apply_starter(db)
    assert db.query(JapaneseSaveImport).count() == 0


def test_the_japanese_goal_has_four_milestones(db):
    apply_starter(db)
    goal = _goal(db, "weg-des-japanischen")
    milestones = db.query(Quest).filter(
        Quest.goal_id == goal.id, Quest.is_milestone == True  # noqa: E712
    ).all()
    assert len(milestones) == 4
    assert {m.target_value for m in milestones} == {1, 20, 8, 4}


# ---------------------------------------------------------------------------
# Körperkontrolle
# ---------------------------------------------------------------------------

def test_the_body_control_goal_has_a_neutral_short_label(db):
    apply_starter(db)
    goal = _goal(db, "koerperkontrolle")
    assert goal.title == "Körperkontrolle"
    assert goal.short_label == "Routine K"


def test_the_five_routines_are_planned_on_their_days(db):
    apply_starter(db)
    for title, weekday in CONTROL_SCHEDULE.items():
        assert scheduled_weekdays(db, _habit(db, title)) == [weekday]


def test_friday_and_sunday_stay_free(db):
    apply_starter(db)
    goal = _goal(db, "koerperkontrolle")
    planned = set()
    for habit in db.query(Habit).filter(Habit.goal_id == goal.id).all():
        planned.update(scheduled_weekdays(db, habit))
    assert planned == {1, 2, 3, 4, 6}


def test_existing_control_habits_are_reused_not_duplicated(db):
    from app.seed_programs import seed_program

    seed_program(db, "control")
    before = db.query(Habit).count()
    apply_starter(db)

    for title in CONTROL_SCHEDULE:
        assert db.query(Habit).filter(Habit.title == title).count() == 1
    # only SRS-Review is added on top of the existing CONTROL habits
    assert db.query(Habit).count() == before + 1


def test_existing_control_stages_are_reused_not_duplicated(db):
    from app.seed_programs import CONTROL, seed_program

    seed_program(db, "control")
    apply_starter(db)
    for stage in CONTROL.quests:
        assert db.query(Quest).filter(Quest.title == stage.title).count() == 1


def test_reused_control_habits_keep_their_text(db):
    from app.seed_programs import CONTROL, seed_program

    seed_program(db, "control")
    original = {h.title: h.description for h in db.query(Habit).all()}
    apply_starter(db)
    for habit in db.query(Habit).all():
        if habit.title in original:
            assert habit.description == original[habit.title]
    assert len(CONTROL.habits) == 5


def test_the_weekly_rhythm_quest_covers_five_routines(db):
    apply_starter(db)
    quest = _quest(db, "Der Fünfer-Rhythmus")
    assert quest.target_value == 5
    assert quest.period == "weekly"
    assert quest.repeatable
    assert quest.match_text is None        # no free-text heuristic


def test_the_body_control_goal_has_four_neutral_milestones(db):
    apply_starter(db)
    goal = _goal(db, "koerperkontrolle")
    milestones = db.query(Quest).filter(
        Quest.goal_id == goal.id, Quest.is_milestone == True  # noqa: E712
    ).all()
    assert len(milestones) == 4


def test_the_seed_data_stays_neutral(db):
    """No intimate wording, no body metrics, no outcome protocol."""
    apply_starter(db)
    blob = " ".join(
        " ".join(filter(None, [row.title, row.description or ""]))
        for row in list(db.query(Habit).all()) + list(db.query(Quest).all())
        + list(db.query(Goal).all())
    ).lower()
    for word in ("orgasm", "ejakul", "penis", "sperma", "erektion", "sex",
                 "masturb", "libido", "cm", "minuten stand"):
        assert word not in blob


def test_the_safety_note_is_factual_and_not_medical_advice(db):
    note = SAFETY_NOTE.lower()
    assert "pausieren" in note
    assert "kostet kein xp" in note
    assert "ärztlich" in note
    for word in ("diagnose", "therapie", "behandlung", "dosis", "medikament"):
        assert word not in note


def test_repeating_the_campaign_keeps_the_routine_stable(db):
    apply_starter(db)
    apply_starter(db)
    for title, weekday in CONTROL_SCHEDULE.items():
        assert db.query(Habit).filter(Habit.title == title).count() == 1
        assert scheduled_weekdays(db, _habit(db, title)) == [weekday]


# ---------------------------------------------------------------------------
# No network, no second seed engine
# ---------------------------------------------------------------------------

def test_the_starter_module_touches_no_network():
    import ast
    from pathlib import Path

    tree = ast.parse((Path(__file__).resolve().parent.parent / "app" / "starter.py").read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    for forbidden in ("httpx", "requests", "urllib", "socket", "app.wger_client"):
        assert forbidden not in imported


def test_the_starter_uses_the_existing_services():
    """No parallel create logic — the shared services are what it calls."""
    import inspect

    import app.starter as starter

    source = inspect.getsource(starter)
    assert "create_goal(" in source
    assert "create_habit(" in source
    assert "create_quest(" in source
    assert "set_weekdays(" in source


def test_no_alembic_revision_seeds_the_campaign():
    """A campaign is data, never part of a migration."""
    from pathlib import Path

    versions = Path(__file__).resolve().parent.parent / "migrations" / "versions"
    for path in versions.glob("*.py"):
        text = path.read_text()
        assert "starter" not in text.lower()
        assert "apply_starter" not in text
