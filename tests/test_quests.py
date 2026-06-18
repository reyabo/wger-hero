"""Test quest seeding, progress, and completion."""

from datetime import datetime, timedelta

import pytest

from app.models import HeroProfile, Quest, SyncEvent, XpEvent
from app.quests import evaluate_quests, seed_quests, _current_week_bounds


def _add_hero(db, xp=0):
    hero = HeroProfile(name="Test", level=1, total_xp=xp)
    db.add(hero)
    db.commit()
    return hero


def _add_sync(db, label="session", n=1):
    for i in range(n):
        db.add(SyncEvent(
            source="wger",
            source_id=f"{label}-{i}",
            source_hash=f"hash{i}",
            synced_at=datetime.utcnow(),
            raw_summary=f"Workout {i}",
            xp_awarded=100,
        ))
    db.commit()


class TestQuestSeeding:
    def test_seeds_default_quests(self, db):
        seed_quests(db)
        quests = db.query(Quest).all()
        slugs = {q.slug for q in quests}
        assert "week-warrior" in slugs
        assert "home-hero-full-week" in slugs

    def test_seeding_is_idempotent(self, db):
        seed_quests(db)
        seed_quests(db)
        count = db.query(Quest).count()
        assert count == 2


class TestQuestProgress:
    def test_week_warrior_progress_increases(self, db):
        seed_quests(db)
        hero = _add_hero(db)
        _add_sync(db, n=2)

        evaluate_quests(db, hero)
        quest = db.query(Quest).filter(Quest.slug == "week-warrior").first()
        assert quest.current_value == 2

    def test_week_warrior_completes_at_target(self, db):
        seed_quests(db)
        hero = _add_hero(db)
        _add_sync(db, n=3)

        evaluate_quests(db, hero)
        quest = db.query(Quest).filter(Quest.slug == "week-warrior").first()
        assert quest.completed_at is not None
        assert quest.active is False

    def test_quest_completion_awards_xp(self, db):
        seed_quests(db)
        hero = _add_hero(db)
        _add_sync(db, n=3)
        initial_xp = hero.total_xp

        evaluate_quests(db, hero)
        hero = db.query(HeroProfile).first()
        assert hero.total_xp > initial_xp

    def test_completed_quest_not_evaluated_again(self, db):
        seed_quests(db)
        hero = _add_hero(db)
        _add_sync(db, n=3)

        evaluate_quests(db, hero)
        xp_after_first = db.query(HeroProfile).first().total_xp

        _add_sync(db, label="extra", n=2)
        evaluate_quests(db, hero)
        xp_after_second = db.query(HeroProfile).first().total_xp

        assert xp_after_first == xp_after_second
