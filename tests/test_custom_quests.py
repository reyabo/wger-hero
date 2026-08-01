"""Tests for custom quests: manual, habit_count, and workout_count types."""

from datetime import datetime, timedelta

from app.habits import complete_habit, create_habit
from app.models import HeroProfile, Quest, SyncEvent
from app.quests import (
    complete_quest_manual,
    create_quest,
    evaluate_quests,
    seed_quests,
)
from app.stats import get_stat_totals, parse_stat_rewards


def _hero(db):
    hero = HeroProfile(name="Test", level=1, total_xp=0)
    db.add(hero)
    db.commit()
    return hero


def _add_syncs(db, n):
    for i in range(n):
        db.add(
            SyncEvent(
                source="wger",
                source_id=f"s{i}",
                source_hash=f"h{i}",
                synced_at=datetime.utcnow(),
                raw_summary="workout",
                xp_awarded=100,
            )
        )
    db.commit()


class TestManualQuests:
    def test_create_manual_quest(self, db):
        quest = create_quest(
            db,
            title="Finish project block",
            quest_type="manual",
            period="once",
            target_value=1,
            xp_reward=150,
            stat_rewards={"creativity": 20},
        )
        assert quest.id is not None
        assert quest.slug
        assert quest.quest_type == "manual"
        assert parse_stat_rewards(quest.stat_rewards) == {"creativity": 20}

    def test_manual_completion_awards_global_and_stat_xp(self, db):
        hero = _hero(db)
        quest = create_quest(
            db,
            title="Theater prep",
            quest_type="manual",
            period="once",
            xp_reward=120,
            stat_rewards={"technique": 10, "creativity": 5},
        )

        assert complete_quest_manual(db, quest, hero) is True

        reloaded = db.query(Quest).first()
        assert reloaded.completed_at is not None
        assert reloaded.active is False
        assert db.query(HeroProfile).first().total_xp == 120
        totals = get_stat_totals(db)
        assert totals["technique"] == 10
        assert totals["creativity"] == 5

    def test_manual_quest_cannot_double_complete(self, db):
        hero = _hero(db)
        quest = create_quest(db, title="One off", quest_type="manual", xp_reward=50)

        assert complete_quest_manual(db, quest, hero) is True
        assert complete_quest_manual(db, quest, hero) is False
        assert db.query(HeroProfile).first().total_xp == 50

    def test_evaluate_does_not_auto_complete_manual(self, db):
        hero = _hero(db)
        quest = create_quest(
            db, title="Manual goal", quest_type="manual", target_value=1, xp_reward=50
        )
        evaluate_quests(db, hero)
        reloaded = db.query(Quest).first()
        assert reloaded.completed_at is None
        assert reloaded.active is True


class TestHabitCountQuests:
    def test_progress_counts_completions(self, db):
        hero = _hero(db)
        quest = create_quest(
            db, title="Read 3x", quest_type="habit_count", period="weekly", target_value=3
        )
        habit = create_habit(db, title="Read", base_xp_reward=5)
        now = datetime.utcnow()
        complete_habit(db, habit, hero, when=now)
        complete_habit(db, habit, hero, when=now + timedelta(seconds=5))

        evaluate_quests(db, hero)

        reloaded = db.query(Quest).filter(Quest.id == quest.id).first()
        assert reloaded.current_value == 2
        assert reloaded.completed_at is None

    def test_completes_and_awards_at_target(self, db):
        hero = _hero(db)
        quest = create_quest(
            db,
            title="Read 2x",
            quest_type="habit_count",
            period="weekly",
            target_value=2,
            xp_reward=100,
            stat_rewards={"knowledge": 30},
        )
        habit = create_habit(db, title="Read", base_xp_reward=5)
        now = datetime.utcnow()
        complete_habit(db, habit, hero, when=now)
        complete_habit(db, habit, hero, when=now + timedelta(seconds=5))
        xp_before = db.query(HeroProfile).first().total_xp  # 10 from habit base XP

        evaluate_quests(db, hero)

        reloaded = db.query(Quest).filter(Quest.id == quest.id).first()
        assert reloaded.completed_at is not None
        assert db.query(HeroProfile).first().total_xp == xp_before + 100
        assert get_stat_totals(db)["knowledge"] == 30

    def test_match_text_filters_by_habit_title(self, db):
        hero = _hero(db)
        quest = create_quest(
            db,
            title="Japanese 2x",
            quest_type="habit_count",
            period="weekly",
            target_value=2,
            match_text="Japanese",
        )
        japanese = create_habit(db, title="Learn Japanese", base_xp_reward=5)
        other = create_habit(db, title="Read", base_xp_reward=5)
        now = datetime.utcnow()
        complete_habit(db, japanese, hero, when=now)
        complete_habit(db, other, hero, when=now + timedelta(seconds=5))

        evaluate_quests(db, hero)

        reloaded = db.query(Quest).filter(Quest.id == quest.id).first()
        assert reloaded.current_value == 1  # only the Japanese habit counts


class TestWorkoutCountQuests:
    def test_progress_counts_sync_events(self, db):
        hero = _hero(db)
        quest = create_quest(
            db, title="3 workouts", quest_type="workout_count", period="weekly", target_value=3
        )
        _add_syncs(db, 2)

        evaluate_quests(db, hero)

        reloaded = db.query(Quest).filter(Quest.id == quest.id).first()
        assert reloaded.current_value == 2

    def test_seeded_workout_quest_still_completes(self, db):
        """Existing seeded week-warrior (workout-count) behaviour is preserved."""
        seed_quests(db)
        hero = _hero(db)
        _add_syncs(db, 3)

        evaluate_quests(db, hero)

        week_warrior = db.query(Quest).filter(Quest.slug == "week-warrior").first()
        assert week_warrior.completed_at is not None
        assert week_warrior.active is False


def _add_workout_events(db, titles, when=None):
    """Add workout_complete XpEvents — this is where the workout name lives."""
    from app.models import XpEvent

    for i, title in enumerate(titles):
        db.add(
            XpEvent(
                event_type="workout_complete",
                source="wger",
                source_id=f"w{i}-{title}",
                xp=100,
                attribute="Strength",
                title=title,
                created_at=when or datetime.utcnow(),
            )
        )
    db.commit()


class TestWorkoutCountMatchText:
    """workout_count must be restrictable to one routine via match_text."""

    def test_match_text_counts_only_matching_workouts(self, db):
        hero = _hero(db)
        _add_workout_events(db, ["Rinne Arc – Push", "Beine", "Rinne Arc – Pull"])
        quest = create_quest(
            db,
            title="Rinne Arc diese Woche",
            quest_type="workout_count",
            period="weekly",
            target_value=5,
            match_text="Rinne Arc",
        )
        evaluate_quests(db, hero)
        db.refresh(quest)
        assert quest.current_value == 2      # "Beine" is not counted

    def test_match_text_is_case_insensitive(self, db):
        hero = _hero(db)
        _add_workout_events(db, ["RINNE ARC – Push", "rinne arc – pull"])
        quest = create_quest(
            db,
            title="Arc",
            quest_type="workout_count",
            period="weekly",
            target_value=5,
            match_text="Rinne Arc",
        )
        evaluate_quests(db, hero)
        db.refresh(quest)
        assert quest.current_value == 2

    def test_without_match_text_all_workouts_still_count(self, db):
        """Existing quests must behave exactly as before."""
        hero = _hero(db)
        _add_syncs(db, 3)
        quest = create_quest(
            db,
            title="Drei Workouts",
            quest_type="workout_count",
            period="weekly",
            target_value=5,
        )
        evaluate_quests(db, hero)
        db.refresh(quest)
        assert quest.current_value == 3

    def test_match_text_ignores_events_outside_the_window(self, db):
        hero = _hero(db)
        _add_workout_events(db, ["Rinne Arc – Push"])
        _add_workout_events(
            db, ["Rinne Arc – alt"], when=datetime.utcnow() - timedelta(days=40)
        )
        quest = create_quest(
            db,
            title="Arc",
            quest_type="workout_count",
            period="weekly",
            target_value=5,
            match_text="Rinne Arc",
        )
        evaluate_quests(db, hero)
        db.refresh(quest)
        assert quest.current_value == 1
