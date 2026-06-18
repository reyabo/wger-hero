from datetime import date

import pytest

from app.sync import NormalizedExerciseLog, NormalizedWorkoutLog
from app.xp import calculate_xp_awards, level_from_total_xp, xp_for_next_level


class TestLevelFormula:
    def test_level_1_threshold(self):
        assert xp_for_next_level(1) == 1250

    def test_level_2_threshold(self):
        assert xp_for_next_level(2) == 1500

    def test_formula_grows(self):
        for lvl in range(1, 20):
            assert xp_for_next_level(lvl) < xp_for_next_level(lvl + 1)

    def test_zero_xp_is_level_1(self):
        level, xp_in, xp_needed = level_from_total_xp(0)
        assert level == 1
        assert xp_in == 0
        assert xp_needed == xp_for_next_level(1)

    def test_just_enough_for_level_2(self):
        threshold = xp_for_next_level(1)
        level, xp_in, _ = level_from_total_xp(threshold)
        assert level == 2
        assert xp_in == 0

    def test_partial_xp_stays_at_level(self):
        level, xp_in, xp_needed = level_from_total_xp(500)
        assert level == 1
        assert xp_in == 500
        assert xp_needed == xp_for_next_level(1)

    def test_multi_level_accumulation(self):
        total = xp_for_next_level(1) + xp_for_next_level(2) + xp_for_next_level(3)
        level, xp_in, _ = level_from_total_xp(total)
        assert level == 4
        assert xp_in == 0


def _make_workout(exercises=None, title=None) -> NormalizedWorkoutLog:
    return NormalizedWorkoutLog(
        source_id="test-1",
        date=date(2024, 1, 15),
        title=title,
        routine_name=None,
        exercises=exercises or [],
        raw_hash="abc123",
    )


class TestXpAwards:
    def test_base_award(self):
        workout = _make_workout()
        awards = calculate_xp_awards(workout)
        types = [a.event_type for a in awards]
        assert "workout_complete" in types
        base = next(a for a in awards if a.event_type == "workout_complete")
        assert base.xp == 100

    def test_conditioning_bonus_detected(self):
        exercises = [NormalizedExerciseLog(name="Bear Crawl")]
        workout = _make_workout(exercises=exercises)
        awards = calculate_xp_awards(workout)
        types = [a.event_type for a in awards]
        assert "conditioning_bonus" in types
        bonus = next(a for a in awards if a.event_type == "conditioning_bonus")
        assert bonus.xp == 25

    def test_no_conditioning_bonus_for_strength(self):
        exercises = [NormalizedExerciseLog(name="Sandbag Front Squat")]
        workout = _make_workout(exercises=exercises)
        awards = calculate_xp_awards(workout)
        types = [a.event_type for a in awards]
        assert "conditioning_bonus" not in types

    def test_rir_bonus_when_logged(self):
        exercises = [NormalizedExerciseLog(name="Ring Pull-up", rir=2.0)]
        workout = _make_workout(exercises=exercises)
        awards = calculate_xp_awards(workout)
        types = [a.event_type for a in awards]
        assert "rir_logged" in types
        rir = next(a for a in awards if a.event_type == "rir_logged")
        assert rir.xp == 10

    def test_no_rir_bonus_when_not_logged(self):
        exercises = [NormalizedExerciseLog(name="Ring Pull-up", rir=None)]
        workout = _make_workout(exercises=exercises)
        awards = calculate_xp_awards(workout)
        types = [a.event_type for a in awards]
        assert "rir_logged" not in types

    def test_all_bonuses_stack(self):
        exercises = [
            NormalizedExerciseLog(name="Bear Crawl", rir=1.0),
        ]
        workout = _make_workout(exercises=exercises)
        awards = calculate_xp_awards(workout)
        total = sum(a.xp for a in awards)
        assert total == 100 + 25 + 10

    def test_title_used_in_award(self):
        workout = _make_workout(title="Tag 1 – Beine")
        awards = calculate_xp_awards(workout)
        base = next(a for a in awards if a.event_type == "workout_complete")
        assert "Tag 1" in base.title
