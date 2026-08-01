"""Tests for QuestCompletion, deduplication, habit binding and Japanese sessions."""

from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import (
    Base,
    HabitCompletion,
    Habit,
    HeroProfile,
    JapaneseSaveImport,
    Quest,
    QuestCompletion,
    StatXpEvent,
    XpEvent,
)
from app.quests import (
    already_rewarded,
    app_today,
    complete_quest_manual,
    completion_key,
    create_quest,
    evaluate_quests,
    japanese_import_counts,
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


def _hero(db):
    hero = HeroProfile(name="Test", level=1, total_xp=0)
    db.add(hero)
    db.commit()
    return hero


def _save(db, *, day, calc="deterministic_session", classification="progress",
          xp=40, mode="START", completion="vollständig", digest=None):
    row = JapaneseSaveImport(
        save_date=day,
        streak=1,
        wanikani_level=1,
        bunpro_level="N5",
        bunpro_points=1,
        source_character_level=2,
        source_level_xp=100,
        source_level_xp_cap=1000,
        vocabulary_score=1, grammar_score=1, reading_score=0,
        listening_score=0, speaking_score=1,
        raw_save="x",
        normalized_hash=digest or f"h{day}{calc}{classification}{xp}{mode}",
        classification=classification,
        xp_awarded=xp,
        session_mode=mode,
        session_completion=completion,
        reward_calculation=calc,
    )
    db.add(row)
    db.commit()
    return row


# ---------------------------------------------------------------------------
# Dedup key
# ---------------------------------------------------------------------------

def test_key_is_stable_for_the_same_period(db):
    quest = create_quest(db, title="Wöchentlich", period="weekly", target_value=1)
    assert completion_key(quest) == completion_key(quest)


def test_key_shape_weekly(db):
    quest = create_quest(db, title="W", period="weekly", target_value=1)
    key = completion_key(quest)
    assert key.startswith(f"quest:{quest.id}:weekly:")
    # the date part is the Monday of the current week in the app timezone
    monday = app_today() - timedelta(days=app_today().weekday())
    assert key.endswith(monday.isoformat())


def test_key_shape_monthly(db):
    quest = create_quest(db, title="M", period="monthly", target_value=1)
    key = completion_key(quest)
    assert key.startswith(f"quest:{quest.id}:monthly:")
    assert key.endswith(app_today().replace(day=1).isoformat())


def test_key_shape_once(db):
    quest = create_quest(db, title="Einmalig", period="once", target_value=1)
    assert completion_key(quest) == f"quest:{quest.id}:once"


def test_keys_differ_between_quests(db):
    a = create_quest(db, title="A", period="weekly", target_value=1)
    b = create_quest(db, title="B", period="weekly", target_value=1)
    assert completion_key(a) != completion_key(b)


def test_keys_differ_between_periods(db):
    quest = create_quest(db, title="Q", period="weekly", target_value=1)
    this_week = completion_key(quest)
    last_week = completion_key(
        quest, datetime.combine(app_today() - timedelta(days=7), datetime.min.time())
    )
    assert this_week != last_week


# ---------------------------------------------------------------------------
# Completion records
# ---------------------------------------------------------------------------

def test_manual_completion_writes_a_record(db):
    hero = _hero(db)
    quest = create_quest(db, title="Manuell", quest_type="manual",
                         period="once", target_value=1, xp_reward=100)
    assert complete_quest_manual(db, quest, hero)

    record = db.query(QuestCompletion).one()
    assert record.quest_id == quest.id
    assert record.xp_awarded == 100
    assert record.dedup_key == f"quest:{quest.id}:once"
    assert record.completed_at is not None


def test_second_completion_awards_nothing(db):
    hero = _hero(db)
    quest = create_quest(db, title="Manuell", quest_type="manual",
                         period="once", target_value=1, xp_reward=100)
    complete_quest_manual(db, quest, hero)
    xp_after_first = db.query(HeroProfile).one().total_xp

    quest.active = True          # force a second attempt past the guards
    quest.completed_at = None
    db.commit()
    assert not complete_quest_manual(db, quest, hero)

    assert db.query(QuestCompletion).count() == 1
    assert db.query(HeroProfile).one().total_xp == xp_after_first
    assert db.query(XpEvent).count() == 1


def test_repeatable_quest_rewards_once_per_week(db):
    hero = _hero(db)
    quest = create_quest(db, title="Wöchentlich", quest_type="habit_count",
                         period="weekly", target_value=1, repeatable=True,
                         xp_reward=50)
    habit = Habit(title="H", base_xp_reward=10)
    db.add(habit)
    db.commit()
    quest.habit_id = habit.id
    db.commit()

    db.add(HabitCompletion(habit_id=habit.id, completed_at=datetime.utcnow()))
    db.commit()

    evaluate_quests(db, hero)
    evaluate_quests(db, hero)     # re-running must not pay again
    evaluate_quests(db, hero)

    assert db.query(QuestCompletion).count() == 1
    assert db.query(HeroProfile).one().total_xp == 50


def test_a_new_period_can_be_rewarded_again(db):
    hero = _hero(db)
    quest = create_quest(db, title="Wöchentlich", quest_type="manual",
                         period="weekly", target_value=1, repeatable=True,
                         xp_reward=50)
    complete_quest_manual(db, quest, hero)
    assert db.query(QuestCompletion).count() == 1

    # Simulate the next week by moving the recorded key into the past
    record = db.query(QuestCompletion).one()
    record.dedup_key = f"quest:{quest.id}:weekly:2020-01-06"
    quest.period_start = None
    quest.period_end = None
    quest.active = True
    quest.completed_at = None
    db.commit()

    assert complete_quest_manual(db, quest, hero)
    assert db.query(QuestCompletion).count() == 2
    assert db.query(HeroProfile).one().total_xp == 100


def test_stat_xp_is_recorded_once(db):
    hero = _hero(db)
    quest = create_quest(db, title="Mit Stats", quest_type="manual", period="once",
                         target_value=1, xp_reward=40,
                         stat_rewards={"knowledge": 28, "discipline": 8})
    complete_quest_manual(db, quest, hero)

    record = db.query(QuestCompletion).one()
    assert record.stat_xp_awarded == 36
    assert db.query(StatXpEvent).count() == 2

    quest.active = True
    quest.completed_at = None
    db.commit()
    complete_quest_manual(db, quest, hero)
    assert db.query(StatXpEvent).count() == 2      # unchanged


def test_unique_constraint_is_enforced_by_the_database(db):
    """Not just by a preceding query — two rows with one key must be impossible."""
    from sqlalchemy.exc import IntegrityError

    db.add(QuestCompletion(quest_id=1, dedup_key="quest:1:weekly:2026-07-27"))
    db.commit()
    db.add(QuestCompletion(quest_id=1, dedup_key="quest:1:weekly:2026-07-27"))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_concurrent_completion_does_not_double_reward(db):
    """Simulate the race: the row appears between the check and the insert."""
    hero = _hero(db)
    quest = create_quest(db, title="Rennen", quest_type="manual", period="once",
                         target_value=1, xp_reward=100)
    # Another worker got there first.
    db.add(QuestCompletion(quest_id=quest.id, dedup_key=completion_key(quest),
                           xp_awarded=100))
    db.commit()

    assert not complete_quest_manual(db, quest, hero)
    assert db.query(QuestCompletion).count() == 1
    assert db.query(HeroProfile).one().total_xp == 0
    assert db.query(XpEvent).count() == 0


def test_already_rewarded_helper(db):
    quest = create_quest(db, title="Q", period="once", target_value=1)
    assert not already_rewarded(db, quest)
    db.add(QuestCompletion(quest_id=quest.id, dedup_key=completion_key(quest)))
    db.commit()
    assert already_rewarded(db, quest)


def test_existing_quests_without_a_record_still_work(db):
    """A quest finished before this revision has no row and must not re-pay."""
    hero = _hero(db)
    quest = create_quest(db, title="Alt", quest_type="manual", period="once",
                         target_value=1, xp_reward=100)
    quest.completed_at = datetime(2026, 1, 1)
    quest.active = False
    db.commit()

    assert not complete_quest_manual(db, quest, hero)   # guards still hold
    assert db.query(QuestCompletion).count() == 0
    assert db.query(HeroProfile).one().total_xp == 0


# ---------------------------------------------------------------------------
# Stable habit binding
# ---------------------------------------------------------------------------

def test_habit_id_takes_precedence_over_match_text(db):
    hero = _hero(db)
    wanted = Habit(title="SRS-Review")
    other = Habit(title="SRS-Review Extra")
    db.add_all([wanted, other])
    db.commit()

    quest = create_quest(db, title="Fünf", quest_type="habit_count",
                         period="weekly", target_value=5, match_text="SRS")
    quest.habit_id = wanted.id
    db.commit()

    db.add(HabitCompletion(habit_id=wanted.id, completed_at=datetime.utcnow()))
    db.add(HabitCompletion(habit_id=other.id, completed_at=datetime.utcnow()))
    db.commit()

    evaluate_quests(db, hero)
    db.refresh(quest)
    assert quest.current_value == 1     # only the bound habit


def test_match_text_still_works_without_habit_id(db):
    hero = _hero(db)
    habit = Habit(title="Japanisch lesen")
    db.add(habit)
    db.commit()
    db.add(HabitCompletion(habit_id=habit.id, completed_at=datetime.utcnow()))
    db.commit()

    quest = create_quest(db, title="Alt", quest_type="habit_count",
                         period="weekly", target_value=5, match_text="Japanisch")
    evaluate_quests(db, hero)
    db.refresh(quest)
    assert quest.current_value == 1


def test_renaming_a_habit_does_not_break_the_id_binding(db):
    hero = _hero(db)
    habit = Habit(title="Alter Name")
    db.add(habit)
    db.commit()
    quest = create_quest(db, title="Q", quest_type="habit_count",
                         period="weekly", target_value=5)
    quest.habit_id = habit.id
    db.commit()
    db.add(HabitCompletion(habit_id=habit.id, completed_at=datetime.utcnow()))
    db.commit()

    habit.title = "Ganz anderer Name"
    db.commit()

    evaluate_quests(db, hero)
    db.refresh(quest)
    assert quest.current_value == 1


def test_archiving_a_habit_keeps_historic_counting(db):
    hero = _hero(db)
    habit = Habit(title="H", active=True)
    db.add(habit)
    db.commit()
    quest = create_quest(db, title="Q", quest_type="habit_count",
                         period="weekly", target_value=5)
    quest.habit_id = habit.id
    db.commit()
    db.add(HabitCompletion(habit_id=habit.id, completed_at=datetime.utcnow()))
    db.commit()

    habit.active = False        # archived
    db.commit()

    evaluate_quests(db, hero)
    db.refresh(quest)
    assert quest.current_value == 1     # the completion still counts


def test_unknown_habit_id_counts_nothing(db):
    hero = _hero(db)
    quest = create_quest(db, title="Q", quest_type="habit_count",
                         period="weekly", target_value=5)
    quest.habit_id = 9999
    db.commit()
    evaluate_quests(db, hero)
    db.refresh(quest)
    assert quest.current_value == 0


# ---------------------------------------------------------------------------
# japanese_session_count — the counting rule
# ---------------------------------------------------------------------------

def test_confirmed_session_counts(db):
    row = _save(db, day=app_today())
    assert japanese_import_counts(row)


@pytest.mark.parametrize("kwargs,reason", [
    ({"calc": "baseline"}, "baseline"),
    ({"calc": "historical"}, "historical import"),
    ({"calc": "warning"}, "warning case"),
    ({"calc": "legacy_level_delta"}, "legacy level-bar path"),
    ({"classification": "duplicate"}, "duplicate"),
    ({"classification": "historical"}, "back-dated"),
    ({"classification": "warning"}, "warning"),
    ({"xp": 0, "mode": "STATUS"}, "STATUS session"),
    ({"xp": 0, "completion": "abgebrochen"}, "aborted session"),
    ({"xp": 0, "completion": "keine Leistung"}, "no performance"),
    ({"xp": 0}, "no reward paid out"),
])
def test_non_sessions_do_not_count(db, kwargs, reason):
    row = _save(db, day=app_today(), **kwargs)
    assert not japanese_import_counts(row), f"{reason} must not count"


def test_quest_counts_only_confirmed_sessions(db):
    hero = _hero(db)
    today = app_today()
    _save(db, day=today, digest="a")
    _save(db, day=today, digest="b")
    _save(db, day=today, calc="baseline", digest="c")
    _save(db, day=today, xp=0, mode="STATUS", digest="d")
    _save(db, day=today, classification="duplicate", digest="e")

    quest = create_quest(db, title="Zwei Gespräche",
                         quest_type="japanese_session_count",
                         period="weekly", target_value=2, xp_reward=60)
    evaluate_quests(db, hero)
    db.refresh(quest)
    assert quest.current_value == 2


def test_sessions_outside_the_window_do_not_count(db):
    hero = _hero(db)
    _save(db, day=app_today(), digest="in")
    _save(db, day=app_today() - timedelta(days=40), digest="out")

    quest = create_quest(db, title="Q", quest_type="japanese_session_count",
                         period="weekly", target_value=5)
    evaluate_quests(db, hero)
    db.refresh(quest)
    assert quest.current_value == 1


def test_counting_uses_save_date_not_import_time(db):
    """A session imported late still credits the week it happened in."""
    hero = _hero(db)
    row = _save(db, day=app_today(), digest="late")
    row.created_at = datetime.utcnow() + timedelta(days=30)   # imported "later"
    db.commit()

    quest = create_quest(db, title="Q", quest_type="japanese_session_count",
                         period="weekly", target_value=5)
    evaluate_quests(db, hero)
    db.refresh(quest)
    assert quest.current_value == 1


# ---------------------------------------------------------------------------
# No double Japanese reward
# ---------------------------------------------------------------------------

def test_quest_bonus_does_not_re_award_session_xp(db):
    hero = _hero(db)
    today = app_today()
    _save(db, day=today, xp=40, digest="s1")
    _save(db, day=today, xp=40, digest="s2")
    # The imports themselves already paid out; simulate that ledger.
    hero.total_xp = 80
    db.commit()

    quest = create_quest(db, title="Zwei Gespräche",
                         quest_type="japanese_session_count",
                         period="weekly", target_value=2, xp_reward=60)
    evaluate_quests(db, hero)

    hero = db.query(HeroProfile).one()
    assert hero.total_xp == 140          # 80 session XP + 60 quest bonus only
    assert db.query(QuestCompletion).count() == 1


def test_quest_does_not_modify_import_rows(db):
    hero = _hero(db)
    row = _save(db, day=app_today(), xp=40)
    before = (row.xp_awarded, row.classification, row.reward_calculation,
              row.stat_xp_awarded)

    quest = create_quest(db, title="Eine", quest_type="japanese_session_count",
                         period="weekly", target_value=1, xp_reward=60)
    evaluate_quests(db, hero)

    db.refresh(row)
    assert (row.xp_awarded, row.classification, row.reward_calculation,
            row.stat_xp_awarded) == before


def test_quest_bonus_is_paid_only_once_per_week(db):
    hero = _hero(db)
    _save(db, day=app_today(), digest="only")

    quest = create_quest(db, title="Eine", quest_type="japanese_session_count",
                         period="weekly", target_value=1, repeatable=True,
                         xp_reward=60)
    evaluate_quests(db, hero)
    evaluate_quests(db, hero)
    evaluate_quests(db, hero)

    assert db.query(QuestCompletion).count() == 1
    assert db.query(HeroProfile).one().total_xp == 60


# ---------------------------------------------------------------------------
# Existing quest types keep working
# ---------------------------------------------------------------------------

def test_workout_count_still_works(db):
    from app.models import SyncEvent

    hero = _hero(db)
    for i in range(3):
        db.add(SyncEvent(source="wger", source_id=f"s{i}", source_hash=f"h{i}",
                         synced_at=datetime.utcnow(), xp_awarded=100))
    db.commit()

    quest = create_quest(db, title="Drei", quest_type="workout_count",
                         period="weekly", target_value=3, xp_reward=200)
    evaluate_quests(db, hero)
    db.refresh(quest)
    assert quest.current_value == 3
    assert db.query(QuestCompletion).count() == 1


def test_workout_variety_still_works(db):
    hero = _hero(db)
    for title in ("Tag 1 – Beine", "Tag 2 – Push", "Tag 3 – Pull"):
        db.add(XpEvent(event_type="workout_complete", source="wger",
                       source_id=title, xp=100, attribute="Strength",
                       title=title, created_at=datetime.utcnow()))
    db.commit()

    quest = create_quest(db, title="Variety", quest_type="workout_variety",
                         period="weekly", target_value=3,
                         match_text="Tag 1,Tag 2,Tag 3")
    evaluate_quests(db, hero)
    db.refresh(quest)
    assert quest.current_value == 3


def test_once_quest_with_range_still_works(db):
    hero = _hero(db)
    quest = create_quest(db, title="Arc", quest_type="workout_variety",
                         period="once", target_value=1, match_text="Push",
                         period_start=datetime.utcnow() - timedelta(days=7),
                         period_end=datetime.utcnow() + timedelta(days=7))
    db.add(XpEvent(event_type="workout_complete", source="wger", source_id="p",
                   xp=100, attribute="Strength", title="Push",
                   created_at=datetime.utcnow()))
    db.commit()

    evaluate_quests(db, hero)
    db.refresh(quest)
    assert quest.current_value == 1
