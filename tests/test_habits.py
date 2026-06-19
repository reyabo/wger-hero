"""Tests for manual habits: creation, editing, completion, XP, and guards."""

from datetime import datetime, timedelta

from app.habits import complete_habit, create_habit, update_habit
from app.models import Habit, HabitCompletion, HeroProfile, StatXpEvent, XpEvent
from app.stats import get_stat_totals, parse_stat_rewards


def _hero(db):
    hero = HeroProfile(name="Test", level=1, total_xp=0)
    db.add(hero)
    db.commit()
    return hero


class TestHabitCrud:
    def test_create_stores_fields(self, db):
        habit = create_habit(
            db,
            title="Read",
            description="Read a book",
            recurrence="daily",
            target_count=1,
            base_xp_reward=20,
        )
        assert habit.id is not None
        assert habit.title == "Read"
        assert habit.recurrence == "daily"
        assert habit.base_xp_reward == 20

    def test_create_stores_only_known_stat_rewards(self, db):
        habit = create_habit(
            db,
            title="Study",
            base_xp_reward=10,
            stat_rewards={"knowledge": 15, "not_a_stat": 99, "discipline": 0},
        )
        rewards = parse_stat_rewards(habit.stat_rewards)
        # unknown key dropped, non-positive dropped
        assert rewards == {"knowledge": 15}

    def test_update_changes_fields(self, db):
        habit = create_habit(db, title="Read", base_xp_reward=10)
        update_habit(
            db,
            habit,
            title="Read more",
            description=None,
            active=True,
            recurrence="weekly",
            target_count=3,
            base_xp_reward=30,
            stat_rewards={"knowledge": 5},
        )
        reloaded = db.query(Habit).first()
        assert reloaded.title == "Read more"
        assert reloaded.recurrence == "weekly"
        assert reloaded.target_count == 3
        assert reloaded.base_xp_reward == 30
        assert parse_stat_rewards(reloaded.stat_rewards) == {"knowledge": 5}


class TestHabitCompletion:
    def test_completion_awards_global_xp(self, db):
        hero = _hero(db)
        habit = create_habit(db, title="Read", base_xp_reward=25)

        result = complete_habit(db, habit, hero)

        assert result.ok
        assert result.xp_awarded == 25
        assert db.query(HeroProfile).first().total_xp == 25
        assert db.query(HabitCompletion).count() == 1
        assert db.query(XpEvent).filter(XpEvent.source == "habit").count() == 1

    def test_completion_awards_stat_xp_separately(self, db):
        hero = _hero(db)
        habit = create_habit(
            db, title="Study", base_xp_reward=10, stat_rewards={"knowledge": 15, "discipline": 5}
        )

        result = complete_habit(db, habit, hero)

        assert result.ok
        assert result.stat_xp_awarded == 20
        totals = get_stat_totals(db)
        assert totals["knowledge"] == 15
        assert totals["discipline"] == 5
        # Global XP stays separate from stat XP.
        assert db.query(HeroProfile).first().total_xp == 10
        assert db.query(StatXpEvent).count() == 2

    def test_double_click_is_ignored(self, db):
        hero = _hero(db)
        habit = create_habit(db, title="Read", base_xp_reward=20)

        first = complete_habit(db, habit, hero)
        second = complete_habit(db, habit, hero)  # near-instant repeat

        assert first.ok
        assert not second.ok
        assert second.reason == "duplicate"
        assert db.query(HabitCompletion).count() == 1
        assert db.query(HeroProfile).first().total_xp == 20

    def test_spaced_completions_both_count(self, db):
        hero = _hero(db)
        habit = create_habit(db, title="Read", base_xp_reward=20)
        now = datetime.utcnow()

        first = complete_habit(db, habit, hero, when=now)
        second = complete_habit(db, habit, hero, when=now + timedelta(seconds=10))

        assert first.ok and second.ok
        assert db.query(HabitCompletion).count() == 2
        assert db.query(HeroProfile).first().total_xp == 40

    def test_inactive_habit_cannot_be_completed(self, db):
        hero = _hero(db)
        habit = create_habit(db, title="Read", base_xp_reward=20, active=False)

        result = complete_habit(db, habit, hero)

        assert not result.ok
        assert result.reason == "inactive"
        assert db.query(HabitCompletion).count() == 0
        assert db.query(HeroProfile).first().total_xp == 0

    def test_completion_updates_level(self, db):
        hero = _hero(db)
        # Level 2 needs 1000 + 1*250 = 1250 XP total to clear level 1.
        habit = create_habit(db, title="Big", base_xp_reward=1300)
        complete_habit(db, habit, hero)
        assert db.query(HeroProfile).first().level >= 2
