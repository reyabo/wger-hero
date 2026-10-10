"""The 30-level curve, its ranks, tiers and bosses.

Pure data plus pure functions, so this file needs no database and no clock. It
exists mostly to stop the table drifting: the curve is quoted in the README and
rendered in a template, and a silent edit here would make all three disagree.
"""

import pytest

from app.japanese_levels import (
    BASELINE_LEVEL,
    BOSS_STATUS_FAILED,
    BOSS_STATUS_PASSED,
    BOSSES,
    LEVELS,
    MAX_LEVEL,
    PROLOGUE_LEVEL,
    TIERS,
    boss_by_id,
    boss_by_reward_id,
    boss_for_level,
    level_for_total_xp,
    level_row,
    next_threshold_for_level,
    rank_for_level,
    threshold_for_level,
    tier_for_level,
    validate_boss_event,
    validate_level_claim,
)


# ---------------------------------------------------------------------------
# The curve itself
# ---------------------------------------------------------------------------

def test_the_curve_has_thirty_levels():
    assert len(LEVELS) == MAX_LEVEL == 30
    assert LEVELS[0].level == PROLOGUE_LEVEL == 1
    assert LEVELS[-1].level == 30


def test_levels_are_consecutive():
    assert [row.level for row in LEVELS] == list(range(1, 31))


def test_the_campaign_starts_at_level_two():
    assert BASELINE_LEVEL == 2
    assert threshold_for_level(2) == 0


def test_the_prologue_has_no_threshold():
    assert threshold_for_level(1) is None


def test_thresholds_increase_strictly():
    thresholds = [row.threshold for row in LEVELS if row.threshold is not None]
    assert thresholds == sorted(thresholds)
    assert len(set(thresholds)) == len(thresholds)


def test_each_next_threshold_is_the_following_level_threshold():
    """The right-hand number of a SAVE bar must be the next level's start."""
    for row in LEVELS:
        if row.level == MAX_LEVEL:
            assert row.next_threshold is None
            continue
        assert row.next_threshold == threshold_for_level(row.level + 1)


@pytest.mark.parametrize(
    "level,rank,threshold",
    [
        (2, "見習い", 0),
        (3, "修行者", 1000),
        (4, "探究者", 2100),
        (5, "挑戦者", 3300),
        (10, "熟練者", 10800),
        (15, "言葉の使い手", 20800),
        (20, "師範", 33300),
        (25, "言の葉の達人", 48300),
        (30, "言霊の覇者", 65800),
    ],
)
def test_named_rows_match_the_specification(level, rank, threshold):
    assert rank_for_level(level) == rank
    assert threshold_for_level(level) == threshold


def test_ranks_are_unique():
    ranks = [row.rank for row in LEVELS]
    assert len(set(ranks)) == len(ranks)


def test_max_level_has_no_next_threshold():
    assert next_threshold_for_level(MAX_LEVEL) is None


def test_an_unknown_level_has_no_row():
    assert level_row(0) is None
    assert level_row(31) is None
    assert rank_for_level(99) is None


# ---------------------------------------------------------------------------
# Total XP → level
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "total,expected",
    [
        (0, 2), (1, 2), (999, 2),
        (1000, 3), (1038, 3), (2099, 3),
        (2100, 4), (3299, 4),
        (3300, 5),
        (65799, 29), (65800, 30),
    ],
)
def test_the_level_follows_the_cumulative_total(total, expected):
    assert level_for_total_xp(total) == expected


def test_a_total_below_the_baseline_stays_at_level_two():
    """Cumulative 0 is the starting state, not a demotion to the prologue."""
    assert level_for_total_xp(0) == 2
    assert level_for_total_xp(-5) == 2


def test_above_max_the_level_stays_thirty():
    """Level 30 is the ceiling; XP beyond it keeps accumulating."""
    assert level_for_total_xp(65800) == 30
    assert level_for_total_xp(999_999) == 30


def test_every_threshold_maps_back_to_its_own_level():
    for row in LEVELS:
        if row.threshold is None:
            continue
        assert level_for_total_xp(row.threshold) == row.level


# ---------------------------------------------------------------------------
# Tiers
# ---------------------------------------------------------------------------

def test_there_are_six_tiers():
    assert len(TIERS) == 6


def test_the_tiers_cover_every_level_exactly_once():
    covered = []
    for tier in TIERS:
        covered.extend(range(tier.first_level, tier.last_level + 1))
    assert sorted(covered) == list(range(1, 31))


@pytest.mark.parametrize(
    "level,name", [(1, "旅立ち"), (5, "旅立ち"), (6, "修行"), (15, "実践"),
                   (16, "熟練"), (25, "奥義"), (26, "言霊"), (30, "言霊")]
)
def test_levels_land_in_the_right_tier(level, name):
    assert tier_for_level(level).name == name


def test_a_level_outside_the_curve_has_no_tier():
    assert tier_for_level(0) is None
    assert tier_for_level(31) is None


# ---------------------------------------------------------------------------
# Bosses
# ---------------------------------------------------------------------------

def test_there_are_six_bosses():
    assert len(BOSSES) == 6


def test_a_boss_sits_at_the_end_of_each_tier():
    assert [boss.level for boss in BOSSES] == [5, 10, 15, 20, 25, 30]
    for boss in BOSSES:
        assert tier_for_level(boss.level).number == boss.tier


def test_boss_and_reward_ids_are_unique():
    assert len({b.boss_id for b in BOSSES}) == 6
    assert len({b.reward_id for b in BOSSES}) == 6


@pytest.mark.parametrize(
    "boss_id,reward_id,reward_name",
    [
        ("jp-rank-boss-01", "jp-rank-reward-01", "旅立ちの証"),
        ("jp-rank-boss-02", "jp-rank-reward-02", "修行の証"),
        ("jp-rank-boss-03", "jp-rank-reward-03", "実践の証"),
        ("jp-rank-boss-04", "jp-rank-reward-04", "熟練の証"),
        ("jp-rank-boss-05", "jp-rank-reward-05", "奥義の証"),
        ("jp-rank-boss-06", "jp-rank-reward-06", "言霊の証"),
    ],
)
def test_the_boss_reward_mapping_matches_the_specification(boss_id, reward_id, reward_name):
    boss = boss_by_id(boss_id)
    assert boss.reward_id == reward_id
    assert boss.reward_name == reward_name
    assert boss_by_reward_id(reward_id) is boss


def test_lookups_reject_the_unknown():
    assert boss_by_id("jp-rank-boss-99") is None
    assert boss_by_reward_id("jp-rank-reward-99") is None
    assert boss_for_level(7) is None


def test_a_boss_can_be_found_by_its_level():
    assert boss_for_level(5).boss_id == "jp-rank-boss-01"


# ---------------------------------------------------------------------------
# Validating a level claim
# ---------------------------------------------------------------------------

def test_a_consistent_claim_has_no_problems():
    assert validate_level_claim(3, 1038, "修行者", 2100) == []


def test_a_level_that_disagrees_with_the_total_is_reported():
    problems = validate_level_claim(5, 1038, "挑戦者", 4600)
    assert any("passt nicht zu 1038 Gesamt-XP" in p for p in problems)


def test_a_wrong_rank_is_reported():
    problems = validate_level_claim(3, 1038, "見習い", 2100)
    assert any("Rang" in p for p in problems)


def test_a_wrong_next_threshold_is_reported():
    problems = validate_level_claim(3, 1038, "修行者", 9999)
    assert any("nächste Schwelle" in p for p in problems)


def test_a_level_outside_the_curve_is_reported():
    problems = validate_level_claim(99, 1038, None, None)
    assert any("außerhalb der Kurve" in p for p in problems)


def test_an_absent_rank_or_threshold_is_not_a_problem():
    """Only what the SAVE actually claims is checked."""
    assert validate_level_claim(3, 1038, None, None) == []


def test_max_level_needs_no_next_threshold():
    assert validate_level_claim(30, 70000, "言霊の覇者", None) == []


# ---------------------------------------------------------------------------
# Validating a boss event
# ---------------------------------------------------------------------------

def test_no_boss_fields_is_not_an_event():
    boss, problems = validate_boss_event(None, None, None, None)
    assert boss is None and problems == []


def test_a_complete_pass_yields_the_boss():
    boss, problems = validate_boss_event(
        "jp-rank-boss-01", "bestanden", "jp-rank-reward-01", "旅立ちの証"
    )
    assert boss.boss_id == "jp-rank-boss-01"
    assert problems == []


def test_the_id_alone_is_enough_when_the_status_is_a_pass():
    boss, problems = validate_boss_event("jp-rank-boss-02", "bestanden", None, None)
    assert boss.boss_id == "jp-rank-boss-02"
    assert problems == []


def test_a_failed_boss_grants_nothing_but_is_not_an_error():
    boss, problems = validate_boss_event("jp-rank-boss-01", BOSS_STATUS_FAILED, None, None)
    assert boss is None
    assert problems == []


def test_a_status_without_an_id_is_a_problem():
    boss, problems = validate_boss_event(None, BOSS_STATUS_PASSED, None, None)
    assert boss is None
    assert any("keine Rangstufenboss-ID" in p for p in problems)


def test_a_reward_id_without_a_boss_id_is_a_problem():
    boss, problems = validate_boss_event(None, None, "jp-rank-reward-01", None)
    assert boss is None
    assert problems


def test_an_id_without_a_status_is_a_problem():
    boss, problems = validate_boss_event("jp-rank-boss-01", None, None, None)
    assert boss is None
    assert any("fehlt der Rangstufenboss-Status" in p for p in problems)


def test_an_unknown_boss_id_is_a_problem():
    boss, problems = validate_boss_event("jp-rank-boss-99", "bestanden", None, None)
    assert boss is None
    assert any("Unbekannte Rangstufenboss-ID" in p for p in problems)


def test_an_unknown_status_is_a_problem():
    boss, problems = validate_boss_event("jp-rank-boss-01", "vielleicht", None, None)
    assert boss is None
    assert any("Unbekannter Rangstufenboss-Status" in p for p in problems)


def test_a_mismatched_reward_id_grants_nothing():
    """The exact case the specification calls out: an inconsistent combination
    must never produce a reward."""
    boss, problems = validate_boss_event(
        "jp-rank-boss-01", "bestanden", "jp-rank-reward-02", None
    )
    assert boss is None
    assert any("gehört nicht zu" in p for p in problems)


def test_a_mismatched_reward_name_grants_nothing():
    boss, problems = validate_boss_event(
        "jp-rank-boss-01", "bestanden", "jp-rank-reward-01", "修行の証"
    )
    assert boss is None
    assert any("passt nicht zu" in p for p in problems)


def test_the_status_is_matched_case_insensitively():
    boss, _ = validate_boss_event("jp-rank-boss-01", "Bestanden", None, None)
    assert boss is not None


def test_surrounding_whitespace_is_tolerated():
    boss, problems = validate_boss_event(
        " jp-rank-boss-01 ", " bestanden ", " jp-rank-reward-01 ", " 旅立ちの証 "
    )
    assert boss is not None and problems == []
