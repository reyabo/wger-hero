"""Tests for the Japanese SAVE parser, hashing, and delta calculation.

All tests here are database-free: app/japanese_saves.py must stay unit-testable
without a session, like app/xp.py and app/rewards.py.
"""

from datetime import date

import pytest

from app.japanese_saves import (
    MAX_SAVE_LENGTH,
    JapaneseSave,
    PreviousState,
    SaveParseError,
    calculate_delta,
    parse_save,
)

VALID_SAVE = """=== 状態 SAVE ===
Datum: 2026-07-31 | Streak: 4
WaniKani: Lv 1 | Bunpro: N5, 5 Punkte
Charakter: Lv 2 (見習い) | 433 / 1000 XP
語彙 180 | 文法 250 | 読解 0 | 聴解 0 | 会話 215
Aktueller Grammatikpunkt: これ
Debuffs: keine
Neue Vokabeln heute: keine
Tagesquest: Erfüllt – Mini-Boss „Partikel-Golem“ besiegt.
=== END SAVE ==="""


def _save(**overrides) -> str:
    """Build a SAVE block, replacing whole lines by their field label."""
    lines = VALID_SAVE.splitlines()
    for label, new_line in overrides.items():
        for i, line in enumerate(lines):
            if line.startswith(label):
                lines[i] = new_line
                break
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Parser — happy path
# ---------------------------------------------------------------------------

def test_parses_valid_save():
    save = parse_save(VALID_SAVE)
    assert save.save_date == date(2026, 7, 31)
    assert save.streak == 4
    assert save.wanikani_level == 1
    assert save.bunpro_level == "N5"
    assert save.bunpro_points == 5
    assert save.character_level == 2
    assert save.character_rank == "見習い"
    assert save.level_xp == 433
    assert save.level_xp_cap == 1000
    assert save.vocabulary == 180
    assert save.grammar == 250
    assert save.reading == 0
    assert save.listening == 0
    assert save.speaking == 215
    assert save.grammar_point == "これ"
    assert save.debuffs == "keine"
    assert save.new_vocabulary == "keine"
    assert "Partikel-Golem" in save.daily_quest
    assert save.session_xp is None


def test_raw_text_is_preserved():
    save = parse_save(VALID_SAVE)
    assert save.raw == VALID_SAVE


# ---------------------------------------------------------------------------
# Parser — tolerance
# ---------------------------------------------------------------------------

def test_tolerates_crlf():
    save = parse_save(VALID_SAVE.replace("\n", "\r\n"))
    assert save.save_date == date(2026, 7, 31)
    assert save.level_xp == 433


def test_tolerates_extra_whitespace():
    noisy = "\n".join("   " + ln + "   " for ln in VALID_SAVE.splitlines())
    save = parse_save(noisy)
    assert save.character_level == 2
    assert save.speaking == 215


def test_tolerates_lv_with_dot():
    save = parse_save(_save(**{
        "WaniKani": "WaniKani: Lv. 1 | Bunpro: N5, 5 Punkte",
        "Charakter": "Charakter: Lv. 2 (見習い) | 433 / 1000 XP",
    }))
    assert save.wanikani_level == 1
    assert save.character_level == 2


def test_tolerates_singular_punkt():
    save = parse_save(_save(**{"WaniKani": "WaniKani: Lv 1 | Bunpro: N5, 1 Punkt"}))
    assert save.bunpro_points == 1


def test_tolerates_missing_trailing_period():
    save = parse_save(_save(**{
        "Tagesquest": "Tagesquest: Erfüllt – Mini-Boss „Partikel-Golem“ besiegt",
    }))
    assert save.daily_quest.endswith("besiegt")


def test_tolerates_typographic_quotes():
    curly = parse_save(VALID_SAVE)
    straight = parse_save(_save(**{
        "Tagesquest": 'Tagesquest: Erfüllt – Mini-Boss "Partikel-Golem" besiegt.',
    }))
    # quotes are normalized away, so the semantic hash matches
    assert curly.normalized_hash() == straight.normalized_hash()


def test_optional_session_xp_is_parsed():
    save = parse_save(VALID_SAVE.replace(
        "=== END SAVE ===", "Session-XP: 60\n=== END SAVE ==="
    ))
    assert save.session_xp == 60


# ---------------------------------------------------------------------------
# Parser — errors (German, field-scoped)
# ---------------------------------------------------------------------------

def test_missing_start_marker():
    with pytest.raises(SaveParseError) as exc:
        parse_save(VALID_SAVE.replace("=== 状態 SAVE ===", ""))
    assert any("Startmarker" in e.message for e in exc.value.errors)


def test_missing_end_marker():
    with pytest.raises(SaveParseError) as exc:
        parse_save(VALID_SAVE.replace("=== END SAVE ===", ""))
    assert any("Endmarker" in e.message for e in exc.value.errors)


def test_invalid_date():
    with pytest.raises(SaveParseError) as exc:
        parse_save(_save(**{"Datum": "Datum: 2026-13-45 | Streak: 4"}))
    assert any(e.field == "save_date" for e in exc.value.errors)


def test_negative_value_rejected():
    with pytest.raises(SaveParseError) as exc:
        parse_save(_save(**{"Datum": "Datum: 2026-07-31 | Streak: -4"}))
    assert any(e.field == "streak" for e in exc.value.errors)


def test_missing_required_field():
    without_debuffs = "\n".join(
        ln for ln in VALID_SAVE.splitlines() if not ln.startswith("Debuffs")
    )
    with pytest.raises(SaveParseError) as exc:
        parse_save(without_debuffs)
    assert any(e.field == "debuffs" for e in exc.value.errors)


def test_duplicate_required_field():
    lines = VALID_SAVE.splitlines()
    lines.insert(-1, "Debuffs: doch nicht keine")
    with pytest.raises(SaveParseError) as exc:
        parse_save("\n".join(lines))
    assert any("mehrfach" in e.message.lower() for e in exc.value.errors)


def test_errors_are_german():
    with pytest.raises(SaveParseError) as exc:
        parse_save("kein save")
    assert exc.value.errors
    for e in exc.value.errors:
        assert e.message.strip()


def test_input_length_is_capped():
    with pytest.raises(SaveParseError) as exc:
        parse_save("x" * (MAX_SAVE_LENGTH + 1))
    assert any("lang" in e.message.lower() for e in exc.value.errors)


# ---------------------------------------------------------------------------
# Hash — semantic normalization
# ---------------------------------------------------------------------------

def test_identical_hash_despite_formatting():
    a = parse_save(VALID_SAVE)
    b = parse_save(
        "\n".join("  " + ln for ln in VALID_SAVE.replace("\n", "\r\n").splitlines())
        .replace("Lv 1", "Lv. 1")
    )
    assert a.normalized_hash() == b.normalized_hash()


def test_different_content_changes_hash():
    a = parse_save(VALID_SAVE)
    b = parse_save(_save(**{"Charakter": "Charakter: Lv 2 (見習い) | 500 / 1000 XP"}))
    assert a.normalized_hash() != b.normalized_hash()


def test_hash_is_sha256_hex():
    save = parse_save(VALID_SAVE)
    digest = save.normalized_hash()
    assert len(digest) == 64
    int(digest, 16)  # raises if not hex


# ---------------------------------------------------------------------------
# Delta calculation
# ---------------------------------------------------------------------------

def _prev(level=2, xp=373, cap=1000, when=date(2026, 7, 30)) -> PreviousState:
    return PreviousState(
        character_level=level, level_xp=xp, level_xp_cap=cap, save_date=when
    )


def test_first_import_is_baseline_with_zero_xp():
    save = parse_save(VALID_SAVE)
    result = calculate_delta(save, None)
    assert result.classification == "baseline"
    assert result.xp_delta == 0


def test_baseline_optional_starting_credit():
    save = parse_save(VALID_SAVE)
    result = calculate_delta(save, None, accept_baseline_credit=True)
    assert result.classification == "baseline"
    assert result.xp_delta == 433


def test_same_level_progress():
    save = parse_save(VALID_SAVE)  # Lv 2, 433/1000
    result = calculate_delta(save, _prev(level=2, xp=373))
    assert result.classification == "progress"
    assert result.xp_delta == 60


def test_level_up_by_one():
    save = parse_save(_save(**{"Charakter": "Charakter: Lv 3 (見習い) | 20 / 1000 XP"}))
    result = calculate_delta(save, _prev(level=2, xp=990, cap=1000))
    assert result.classification == "progress"
    assert result.xp_delta == 30


def test_level_jump_greater_than_one_awards_nothing():
    save = parse_save(_save(**{"Charakter": "Charakter: Lv 5 (見習い) | 20 / 1000 XP"}))
    result = calculate_delta(save, _prev(level=2, xp=990))
    assert result.classification == "warning"
    assert result.xp_delta == 0
    assert result.warning


def test_level_decrease_awards_nothing():
    save = parse_save(_save(**{"Charakter": "Charakter: Lv 1 (見習い) | 20 / 1000 XP"}))
    result = calculate_delta(save, _prev(level=2, xp=990))
    assert result.classification == "warning"
    assert result.xp_delta == 0


def test_negative_delta_awards_nothing():
    save = parse_save(VALID_SAVE)  # Lv 2, 433
    result = calculate_delta(save, _prev(level=2, xp=800))
    assert result.classification == "warning"
    assert result.xp_delta == 0
    assert result.warning


def test_implausible_cap_awards_nothing():
    save = parse_save(_save(**{"Charakter": "Charakter: Lv 2 (見習い) | 433 / 0 XP"}))
    result = calculate_delta(save, _prev(level=2, xp=100, cap=1000))
    assert result.classification == "warning"
    assert result.xp_delta == 0


def test_explicit_session_xp_wins():
    save = parse_save(VALID_SAVE.replace(
        "=== END SAVE ===", "Session-XP: 42\n=== END SAVE ==="
    ))
    result = calculate_delta(save, _prev(level=2, xp=373))
    assert result.xp_delta == 42
    assert result.classification == "progress"


def test_explicit_session_xp_negative_is_rejected():
    save = parse_save(VALID_SAVE.replace(
        "=== END SAVE ===", "Session-XP: 0\n=== END SAVE ==="
    ))
    result = calculate_delta(save, _prev(level=2, xp=373))
    assert result.xp_delta == 0


def test_older_date_is_historical():
    save = parse_save(VALID_SAVE)  # 2026-07-31
    result = calculate_delta(save, _prev(when=date(2026, 8, 5)))
    assert result.classification == "historical"
    assert result.xp_delta == 0


def test_same_date_still_counts_as_progress():
    save = parse_save(VALID_SAVE)
    result = calculate_delta(save, _prev(xp=373, when=date(2026, 7, 31)))
    assert result.classification == "progress"
    assert result.xp_delta == 60


def test_no_progress_is_not_a_warning():
    save = parse_save(VALID_SAVE)
    result = calculate_delta(save, _prev(level=2, xp=433))
    assert result.xp_delta == 0
    assert result.classification == "progress"
