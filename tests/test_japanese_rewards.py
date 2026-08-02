"""Tests for deterministic Japanese session rewards (mode + completion).

Database-free: the reward rules live in app/japanese_saves.py and must stay
unit-testable without a session.
"""

from datetime import date

import pytest

from app.japanese_saves import (
    SaveParseError,
    JAPANESE_SESSION_REWARDS,
    PARTIAL_SESSION_CAP,
    SESSION_COMPLETIONS,
    SESSION_MODES,
    PreviousState,
    calculate_delta,
    calculate_session_reward,
    parse_save,
)

BASE = """=== 状態 SAVE ===
Datum: 2026-07-31 | Streak: 4
WaniKani: Lv 1 | Bunpro: N5, 5 Punkte
Charakter: Lv 2 (見習い) | 433 / 1000 XP
{extra}語彙 180 | 文法 250 | 読解 0 | 聴解 0 | 会話 215
Aktueller Grammatikpunkt: これ
Debuffs: keine
Neue Vokabeln heute: keine
Tagesquest: Erfüllt – Mini-Boss „Partikel-Golem“ besiegt.
=== END SAVE ==="""


def save_with(mode=None, completion=None, session_xp=None, xp=433, level=2, day="2026-07-31"):
    extra = ""
    if mode is not None:
        extra += f"Session-Modus: {mode}\n"
    if completion is not None:
        extra += f"Session-Abschluss: {completion}\n"
    if session_xp is not None:
        extra += f"Session-XP: {session_xp}\n"
    text = BASE.format(extra=extra)
    text = text.replace(
        "Charakter: Lv 2 (見習い) | 433 / 1000 XP",
        f"Charakter: Lv {level} (見習い) | {xp} / 1000 XP",
    )
    return text.replace("Datum: 2026-07-31", f"Datum: {day}")


def prev(level=2, xp=373, cap=1000, when=date(2026, 7, 30)) -> PreviousState:
    return PreviousState(character_level=level, level_xp=xp, level_xp_cap=cap, save_date=when)


# ---------------------------------------------------------------------------
# 1. Parsing / normalization of every session mode
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mode", sorted(SESSION_MODES))
def test_all_modes_parse(mode):
    save = parse_save(save_with(mode=mode, completion="vollständig"))
    assert save.session_mode == mode


@pytest.mark.parametrize("written,expected", [
    ("START VOICE", "START_VOICE"),
    ("START-VOICE", "START_VOICE"),
    ("START_VOICE", "START_VOICE"),
    ("start voice", "START_VOICE"),
    ("  Start-Voice  ", "START_VOICE"),
    ("start", "START"),
    ("Boss", "BOSS"),
    ("mini", "MINI"),
])
def test_mode_spelling_variants_normalize(written, expected):
    save = parse_save(save_with(mode=written, completion="vollständig"))
    assert save.session_mode == expected


# ---------------------------------------------------------------------------
# 2. Parsing of every completion value
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value", sorted(SESSION_COMPLETIONS))
def test_all_completions_parse(value):
    save = parse_save(save_with(mode="START", completion=value))
    assert save.session_completion == value


@pytest.mark.parametrize("written,expected", [
    ("Vollständig", "vollständig"),
    ("  VOLLSTÄNDIG  ", "vollständig"),
    ("Teilweise", "teilweise"),
    ("Abgebrochen", "abgebrochen"),
    ("Keine Leistung", "keine Leistung"),
    ("keine   leistung", "keine Leistung"),
])
def test_completion_spelling_variants_normalize(written, expected):
    save = parse_save(save_with(mode="START", completion=written))
    assert save.session_completion == expected


# ---------------------------------------------------------------------------
# 3. Unknown mode / completion is a warning case (never a reward)
# ---------------------------------------------------------------------------

def test_unknown_mode_is_warning_case():
    save = parse_save(save_with(mode="TURBO", completion="vollständig"))
    assert save.session_mode is None
    assert save.session_mode_raw == "TURBO"
    result = calculate_delta(save, prev())
    assert result.classification == "warning"
    assert result.reward_calculation == "warning"
    assert result.xp_delta == 0
    assert result.warning


def test_unknown_completion_is_warning_case():
    save = parse_save(save_with(mode="START", completion="halb"))
    assert save.session_completion is None
    result = calculate_delta(save, prev())
    assert result.xp_delta == 0
    assert result.reward_calculation == "warning"


def test_a_half_written_session_pair_is_refused():
    """Mode and completion are one statement; half of it cannot be evaluated.

    This used to import as a warning case with 0 XP. It is now a plain
    validation error instead, so the incomplete line is corrected rather than
    silently stored — the reward is 0 either way, nothing about the XP rules
    changed.
    """
    with pytest.raises(SaveParseError):
        parse_save(save_with(mode="START"))
    with pytest.raises(SaveParseError):
        parse_save(save_with(completion="vollständig"))


# ---------------------------------------------------------------------------
# 4.-10. Deterministic reward table
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mode,expected", [
    ("MINI", 15),
    ("START", 40),
    ("START_VOICE", 40),
    ("GENKI", 40),
    ("IRODORI", 40),
    ("SCHWACH", 40),
    ("BOSS", 80),
    ("STATUS", 0),
])
def test_full_completion_rewards(mode, expected):
    assert calculate_session_reward(mode, "vollständig") == expected


def test_partial_boss_is_capped_at_15():
    assert calculate_session_reward("BOSS", "teilweise") == 15


def test_partial_mini_never_exceeds_full():
    # min(15, full) — MINI's full value is 15, so partial stays 15
    assert calculate_session_reward("MINI", "teilweise") == 15


def test_partial_status_stays_zero():
    assert calculate_session_reward("STATUS", "teilweise") == 0


@pytest.mark.parametrize("mode", sorted(SESSION_MODES))
def test_aborted_is_always_zero(mode):
    assert calculate_session_reward(mode, "abgebrochen") == 0


@pytest.mark.parametrize("mode", sorted(SESSION_MODES))
def test_no_performance_is_always_zero(mode):
    assert calculate_session_reward(mode, "keine Leistung") == 0


@pytest.mark.parametrize("completion", sorted(SESSION_COMPLETIONS))
def test_status_is_always_zero(completion):
    assert calculate_session_reward("STATUS", completion) == 0


def test_partial_uses_min_of_cap_and_full():
    for mode, full in JAPANESE_SESSION_REWARDS.items():
        assert calculate_session_reward(mode, "teilweise") == min(PARTIAL_SESSION_CAP, full)


# ---------------------------------------------------------------------------
# Delta integration: deterministic path
# ---------------------------------------------------------------------------

def test_deterministic_session_used_when_both_lines_present():
    save = parse_save(save_with(mode="START", completion="vollständig"))
    result = calculate_delta(save, prev())
    assert result.reward_calculation == "deterministic_session"
    assert result.classification == "progress"
    assert result.xp_delta == 40


def test_boss_full_gives_80():
    save = parse_save(save_with(mode="BOSS", completion="vollständig"))
    assert calculate_delta(save, prev()).xp_delta == 80


def test_aborted_session_gives_zero_but_is_not_a_warning():
    save = parse_save(save_with(mode="START", completion="abgebrochen"))
    result = calculate_delta(save, prev())
    assert result.xp_delta == 0
    assert result.reward_calculation == "deterministic_session"


# ---------------------------------------------------------------------------
# 15./16./17. Competence deltas and reported Session-XP do not drive rewards
# ---------------------------------------------------------------------------

def test_competence_deltas_do_not_change_reward():
    """Wildly different language values must not move the XP."""
    a = parse_save(save_with(mode="START", completion="vollständig"))
    b_text = save_with(mode="START", completion="vollständig").replace(
        "語彙 180 | 文法 250 | 読解 0 | 聴解 0 | 会話 215",
        "語彙 900 | 文法 900 | 読解 900 | 聴解 900 | 会話 900",
    )
    b = parse_save(b_text)
    assert calculate_delta(a, prev()).xp_delta == calculate_delta(b, prev()).xp_delta == 40


def test_reported_session_xp_is_not_used_in_new_format():
    save = parse_save(save_with(mode="START", completion="vollständig", session_xp=60))
    result = calculate_delta(save, prev())
    assert save.session_xp == 60          # stored as source metadata
    assert result.xp_delta == 40          # but the rule wins
    assert result.reported_session_xp == 60
    assert result.reward_calculation == "deterministic_session"


def test_mismatch_between_reported_and_computed_is_flagged():
    save = parse_save(save_with(mode="START", completion="vollständig", session_xp=60))
    result = calculate_delta(save, prev())
    assert result.reported_mismatch is True
    assert "60" in result.warning and "40" in result.warning


def test_matching_reported_xp_is_not_flagged():
    save = parse_save(save_with(mode="START", completion="vollständig", session_xp=40))
    result = calculate_delta(save, prev())
    assert result.reported_mismatch is False


def test_level_bar_does_not_affect_deterministic_reward():
    """Even a shrinking level bar must not change a deterministic session."""
    save = parse_save(save_with(mode="START", completion="vollständig", xp=10))
    result = calculate_delta(save, prev(xp=900))
    assert result.xp_delta == 40
    assert result.reward_calculation == "deterministic_session"


# ---------------------------------------------------------------------------
# 18. Legacy saves keep the old level-delta behaviour
# ---------------------------------------------------------------------------

def test_legacy_save_without_mode_uses_level_delta():
    save = parse_save(save_with(xp=433))
    result = calculate_delta(save, prev(xp=373))
    assert result.reward_calculation == "legacy_level_delta"
    assert result.xp_delta == 60


def test_legacy_save_with_session_xp_still_uses_it():
    save = parse_save(save_with(xp=433, session_xp=42))
    result = calculate_delta(save, prev(xp=373))
    assert result.reward_calculation == "legacy_level_delta"
    assert result.xp_delta == 42


def test_legacy_level_up_still_works():
    save = parse_save(save_with(level=3, xp=20))
    result = calculate_delta(save, prev(level=2, xp=990))
    assert result.xp_delta == 30
    assert result.reward_calculation == "legacy_level_delta"


# ---------------------------------------------------------------------------
# 11./20. Baseline and historical
# ---------------------------------------------------------------------------

def test_baseline_stays_zero_even_with_mode():
    save = parse_save(save_with(mode="BOSS", completion="vollständig"))
    result = calculate_delta(save, None)
    assert result.classification == "baseline"
    assert result.reward_calculation == "baseline"
    assert result.xp_delta == 0


def test_baseline_credit_still_available_but_marked_baseline():
    save = parse_save(save_with(mode="BOSS", completion="vollständig"))
    result = calculate_delta(save, None, accept_baseline_credit=True)
    assert result.xp_delta == 433
    assert result.reward_calculation == "baseline"


def test_historical_import_gives_nothing_even_with_mode():
    save = parse_save(save_with(mode="BOSS", completion="vollständig", day="2026-07-01"))
    result = calculate_delta(save, prev(when=date(2026, 7, 20)))
    assert result.reward_calculation == "historical"
    assert result.xp_delta == 0


# ---------------------------------------------------------------------------
# Backward compatibility of the old format
# ---------------------------------------------------------------------------

def test_old_save_without_new_lines_still_parses():
    save = parse_save(save_with())
    assert save.session_mode is None
    assert save.session_completion is None
    assert save.vocabulary == 180
    assert save.save_date == date(2026, 7, 31)


def test_new_lines_change_the_hash():
    a = parse_save(save_with())
    b = parse_save(save_with(mode="START", completion="vollständig"))
    assert a.normalized_hash() != b.normalized_hash()
