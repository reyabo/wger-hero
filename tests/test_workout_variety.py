"""Tests for the data-driven workout_variety quest type."""

from datetime import datetime, timedelta

from app.models import HeroProfile, Quest, XpEvent
from app.quests import (
    HOME_HERO_MATCH_TEXT,
    QUEST_TYPE_CHOICES,
    _count_workout_variety_in_period,
    create_quest,
    evaluate_quests,
    migrate_seeded_quests,
    seed_quests,
)


def _hero(db):
    hero = HeroProfile(name="Test", level=1, total_xp=0)
    db.add(hero)
    db.commit()
    return hero


def _workouts(db, titles, when=None):
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


def _variety_quest(db, match_text, target=3, period="weekly", **kw):
    return create_quest(
        db,
        title="Routine",
        quest_type="workout_variety",
        period=period,
        target_value=target,
        match_text=match_text,
        **kw,
    )


# ---------------------------------------------------------------------------
# Type registration
# ---------------------------------------------------------------------------

def test_workout_variety_is_a_valid_choice():
    assert "workout_variety" in QUEST_TYPE_CHOICES


# ---------------------------------------------------------------------------
# Counting
# ---------------------------------------------------------------------------

def test_counts_distinct_terms(db):
    _workouts(db, ["Tag 1 – Beine", "Tag 2 – Push", "Tag 3 – Pull"])
    quest = _variety_quest(db, "Tag 1,Tag 2,Tag 3")
    assert _count_workout_variety_in_period(db, quest) == 3


def test_repeated_term_counts_once(db):
    """Three Push sessions are still only one distinct day type."""
    _workouts(db, ["Push A", "Push B", "Push C"])
    quest = _variety_quest(db, "Push,Pull,Beine")
    assert _count_workout_variety_in_period(db, quest) == 1


def test_matching_is_case_insensitive(db):
    _workouts(db, ["TAG 1 – beine", "tag 2 – PUSH"])
    quest = _variety_quest(db, "Tag 1,Tag 2,Tag 3")
    assert _count_workout_variety_in_period(db, quest) == 2


def test_empty_match_text_returns_zero(db):
    _workouts(db, ["Tag 1", "Tag 2"])
    quest = _variety_quest(db, None)
    assert _count_workout_variety_in_period(db, quest) == 0


def test_blank_match_text_returns_zero(db):
    _workouts(db, ["Tag 1"])
    quest = _variety_quest(db, "   ")
    assert _count_workout_variety_in_period(db, quest) == 0


def test_blank_terms_between_commas_are_ignored(db):
    _workouts(db, ["Tag 1"])
    quest = _variety_quest(db, "Tag 1,, ,Tag 2")
    assert _count_workout_variety_in_period(db, quest) == 1


def test_unmatched_terms_do_not_count(db):
    _workouts(db, ["Tag 1 – Beine"])
    quest = _variety_quest(db, "Tag 1,Tag 2,Tag 3")
    assert _count_workout_variety_in_period(db, quest) == 1


def test_events_outside_the_window_are_ignored(db):
    _workouts(db, ["Tag 1"])
    _workouts(db, ["Tag 2"], when=datetime.utcnow() - timedelta(days=40))
    quest = _variety_quest(db, "Tag 1,Tag 2,Tag 3")
    assert _count_workout_variety_in_period(db, quest) == 1


def test_only_workout_complete_events_count(db):
    db.add(
        XpEvent(
            event_type="quest_complete",
            source="quest",
            source_id="x",
            xp=50,
            attribute="Strength",
            title="Tag 1 – Beine",
            created_at=datetime.utcnow(),
        )
    )
    db.commit()
    quest = _variety_quest(db, "Tag 1")
    assert _count_workout_variety_in_period(db, quest) == 0


def test_works_with_monthly_period(db):
    """The type must not be hard-wired to the current week."""
    _workouts(db, ["Tag 1", "Tag 2"])
    quest = _variety_quest(db, "Tag 1,Tag 2,Tag 3", period="monthly")
    assert _count_workout_variety_in_period(db, quest) == 2


def test_explicit_window_is_respected(db):
    inside = datetime.utcnow() - timedelta(days=2)
    outside = datetime.utcnow() - timedelta(days=30)
    _workouts(db, ["Tag 1"], when=inside)
    _workouts(db, ["Tag 2"], when=outside)
    quest = _variety_quest(db, "Tag 1,Tag 2,Tag 3", period="once")
    quest.period_start = datetime.utcnow() - timedelta(days=7)
    quest.period_end = datetime.utcnow()
    db.commit()
    assert _count_workout_variety_in_period(db, quest) == 1


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def test_evaluate_quests_drives_progress(db):
    hero = _hero(db)
    _workouts(db, ["Tag 1 – Beine", "Tag 2 – Push", "Tag 2 – Push again"])
    quest = _variety_quest(db, "Tag 1,Tag 2,Tag 3", target=3)
    evaluate_quests(db, hero)
    db.refresh(quest)
    assert quest.current_value == 2      # Tag 2 twice is still one type
    assert quest.completed_at is None


def test_quest_completes_when_all_types_hit(db):
    hero = _hero(db)
    _workouts(db, ["Tag 1 – Beine", "Tag 2 – Push", "Tag 3 – Pull"])
    quest = _variety_quest(db, "Tag 1,Tag 2,Tag 3", target=3)
    evaluate_quests(db, hero)
    db.refresh(quest)
    assert quest.current_value == 3
    assert quest.completed_at is not None


# ---------------------------------------------------------------------------
# Migration of the seeded HOME HERO quest
# ---------------------------------------------------------------------------

def test_seeded_home_hero_uses_the_new_type(db):
    seed_quests(db)
    quest = db.query(Quest).filter(Quest.slug == "home-hero-full-week").one()
    assert quest.quest_type == "workout_variety"
    assert quest.match_text == HOME_HERO_MATCH_TEXT


def test_migration_lifts_a_legacy_quest(db):
    """A quest seeded by an older version is raised to the new type."""
    db.add(
        Quest(
            slug="home-hero-full-week",
            title="HOME HERO × SUPERMOVER 3 – Full Week",
            quest_type="weekly",
            period="weekly",
            target_value=3,
            current_value=2,
            xp_reward=200,
            attribute="Strength",
            active=True,
        )
    )
    db.commit()

    migrate_seeded_quests(db)

    quest = db.query(Quest).filter(Quest.slug == "home-hero-full-week").one()
    assert quest.quest_type == "workout_variety"
    assert quest.match_text == HOME_HERO_MATCH_TEXT
    # history and reward are untouched
    assert quest.current_value == 2
    assert quest.xp_reward == 200
    assert quest.active is True


def test_migration_is_idempotent(db):
    seed_quests(db)
    quest = db.query(Quest).filter(Quest.slug == "home-hero-full-week").one()
    quest.current_value = 3
    db.commit()

    migrate_seeded_quests(db)
    migrate_seeded_quests(db)

    db.refresh(quest)
    assert quest.quest_type == "workout_variety"
    assert quest.current_value == 3
    assert db.query(Quest).filter(Quest.slug == "home-hero-full-week").count() == 1


def test_migration_does_not_touch_user_match_text(db):
    """Once migrated, a match_text the user edited must survive."""
    seed_quests(db)
    quest = db.query(Quest).filter(Quest.slug == "home-hero-full-week").one()
    quest.match_text = "Eigene,Begriffe"
    db.commit()

    migrate_seeded_quests(db)

    db.refresh(quest)
    assert quest.match_text == "Eigene,Begriffe"


def test_migration_without_the_quest_is_safe(db):
    migrate_seeded_quests(db)   # must not raise
    assert db.query(Quest).count() == 0


def test_week_warrior_is_left_untouched(db):
    seed_quests(db)
    migrate_seeded_quests(db)
    quest = db.query(Quest).filter(Quest.slug == "week-warrior").one()
    assert quest.quest_type == "weekly"
    assert quest.match_text is None
