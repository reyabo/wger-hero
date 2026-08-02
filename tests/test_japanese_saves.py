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
    """A core line is still mandatory — the character line drives level progress."""
    without_character = "\n".join(
        ln for ln in VALID_SAVE.splitlines() if not ln.startswith("Charakter")
    )
    with pytest.raises(SaveParseError) as exc:
        parse_save(without_character)
    assert any(e.field == "character_level" for e in exc.value.errors)


def test_an_optional_coach_line_is_no_longer_required():
    """Debuffs used to be mandatory; a SAVE without it is a valid snapshot."""
    without_debuffs = "\n".join(
        ln for ln in VALID_SAVE.splitlines() if not ln.startswith("Debuffs")
    )
    assert parse_save(without_debuffs).debuffs is None


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


# ---------------------------------------------------------------------------
# WaniKani/Bunpro line — SRS grammar-point wording (regression)
# ---------------------------------------------------------------------------

REPORTED_LINE = "WaniKani: Lv 2 | Bunpro: N5, 10 Grammatikpunkte im SRS"


def test_reported_line_parses():
    """The exact line from the bug report must import."""
    parse_save(_save(WaniKani=REPORTED_LINE))


def test_reported_line_yields_wanikani_level():
    assert parse_save(_save(WaniKani=REPORTED_LINE)).wanikani_level == 2


def test_reported_line_yields_bunpro_level():
    assert parse_save(_save(WaniKani=REPORTED_LINE)).bunpro_level == "N5"


def test_reported_line_yields_srs_points():
    assert parse_save(_save(WaniKani=REPORTED_LINE)).bunpro_points == 10


def test_whitespace_around_the_separator_is_optional():
    line = "WaniKani: Lv 2|Bunpro: N5, 10 Grammatikpunkte im SRS"
    save = parse_save(_save(WaniKani=line))
    assert (save.wanikani_level, save.bunpro_level, save.bunpro_points) == (2, "N5", 10)


def test_singular_grammar_point_is_accepted():
    line = "WaniKani: Lv 2 | Bunpro: N5, 1 Grammatikpunkt im SRS"
    assert parse_save(_save(WaniKani=line)).bunpro_points == 1


def test_plural_grammar_points_are_accepted():
    line = "WaniKani: Lv 2 | Bunpro: N5, 10 Grammatikpunkte im SRS"
    assert parse_save(_save(WaniKani=line)).bunpro_points == 10


def test_level_spelled_out_is_accepted():
    line = "WaniKani: Level 2 | Bunpro: N5, 10 Grammatikpunkte im SRS"
    assert parse_save(_save(WaniKani=line)).wanikani_level == 2


def test_documented_format_without_srs_suffix_still_works():
    save = parse_save(VALID_SAVE)
    assert (save.wanikani_level, save.bunpro_level, save.bunpro_points) == (1, "N5", 5)


def test_non_numeric_wanikani_level_is_rejected():
    line = "WaniKani: Lv zwei | Bunpro: N5, zehn Grammatikpunkte im SRS"
    with pytest.raises(SaveParseError) as excinfo:
        parse_save(_save(WaniKani=line))
    assert any(e.field == "wanikani_level" for e in excinfo.value.errors)


def test_negative_srs_value_is_rejected():
    line = "WaniKani: Lv 2 | Bunpro: N5, -10 Grammatikpunkte im SRS"
    with pytest.raises(SaveParseError):
        parse_save(_save(WaniKani=line))


def test_missing_bunpro_value_is_rejected():
    line = "WaniKani: Lv 2 | Bunpro: unbekannt"
    with pytest.raises(SaveParseError):
        parse_save(_save(WaniKani=line))


def test_parse_error_message_names_the_line_without_internals():
    line = "WaniKani: Lv 2 | Bunpro: unbekannt"
    with pytest.raises(SaveParseError) as excinfo:
        parse_save(_save(WaniKani=line))
    message = " ".join(e.message for e in excinfo.value.errors)
    # The message now names the offending field itself rather than only quoting
    # the whole line — still German, still free of any internal detail.
    assert "Bunpro-Level" in message
    assert "\\s" not in message and "(?P<" not in message
    assert "bunpro_level" not in message


def test_srs_wording_does_not_change_the_session_reward():
    """XP comes from mode and completion only — never from the WaniKani line."""
    plain = calculate_delta(parse_save(VALID_SAVE), _prev())
    srs = calculate_delta(parse_save(_save(WaniKani=REPORTED_LINE)), _prev())
    assert srs.xp_delta == plain.xp_delta
    assert srs.reward_calculation == plain.reward_calculation
    assert srs.classification == plain.classification


def test_srs_wording_produces_a_stable_duplicate_hash():
    """Same values, different wording — the canonical hash must not care."""
    a = parse_save(_save(WaniKani="WaniKani: Lv 2 | Bunpro: N5, 10 Grammatikpunkte im SRS"))
    b = parse_save(_save(WaniKani="WaniKani: Lv 2 | Bunpro: N5, 10 Punkte"))
    c = parse_save(_save(WaniKani="WaniKani: Level 2|Bunpro: n5, 10 Grammatikpunkt im SRS"))
    assert a.normalized_hash() == b.normalized_hash() == c.normalized_hash()


# ---------------------------------------------------------------------------
# Flexible SAVE: a small required core plus independent optional fields
# ---------------------------------------------------------------------------

MINIMAL_SAVE = """=== 状態 SAVE ===
Datum: 2026-08-01 | Streak: 4
Charakter: Lv 2 (見習い) | 433 / 1000 XP
語彙 180 | 文法 250 | 読解 0 | 聴解 0 | 会話 215
=== END SAVE ==="""


def _minimal(*extra_lines: str) -> str:
    """The required core plus any number of optional lines."""
    lines = MINIMAL_SAVE.splitlines()
    return "\n".join(lines[:-1] + list(extra_lines) + [lines[-1]])


def _without(label: str) -> str:
    """The full documented SAVE with one line removed."""
    return "\n".join(l for l in VALID_SAVE.splitlines() if not l.startswith(label))


# --- the required core ------------------------------------------------------

def test_the_minimal_save_is_valid():
    save = parse_save(MINIMAL_SAVE)
    assert save.save_date == date(2026, 8, 1)
    assert save.streak == 4
    assert save.character_level == 2
    assert save.vocabulary == 180


def test_the_minimal_save_leaves_the_learning_metrics_unset():
    save = parse_save(MINIMAL_SAVE)
    assert save.wanikani_level is None
    assert save.bunpro_level is None
    assert save.bunpro_points is None


def test_the_minimal_save_leaves_the_coach_notes_unset():
    save = parse_save(MINIMAL_SAVE)
    assert save.grammar_point is None
    assert save.debuffs is None
    assert save.new_vocabulary is None
    assert save.daily_quest is None


@pytest.mark.parametrize("label", [
    "Aktueller Grammatikpunkt:", "Debuffs:", "Neue Vokabeln heute:", "Tagesquest:",
])
def test_each_coach_line_may_be_missing_on_its_own(label):
    parse_save(_without(label))


def test_a_present_coach_line_is_still_stored():
    save = parse_save(_minimal("Debuffs: Müdigkeit"))
    assert save.debuffs == "Müdigkeit"
    assert save.grammar_point is None


def test_an_empty_coach_line_is_none_not_an_invented_value():
    save = parse_save(_minimal("Debuffs:"))
    assert save.debuffs is None


@pytest.mark.parametrize("label", ["Datum:", "Charakter:", "語彙"])
def test_the_required_core_cannot_be_reduced(label):
    body = "\n".join(l for l in MINIMAL_SAVE.splitlines() if not l.startswith(label))
    with pytest.raises(SaveParseError):
        parse_save(body)


# --- independent learning metrics -------------------------------------------

def test_only_the_wanikani_level():
    save = parse_save(_minimal("WaniKani-Level: 2"))
    assert save.wanikani_level == 2
    assert save.bunpro_level is None
    assert save.bunpro_points is None


def test_only_the_bunpro_level():
    save = parse_save(_minimal("Bunpro-Level: N5"))
    assert save.bunpro_level == "N5"
    assert save.wanikani_level is None
    assert save.bunpro_points is None


def test_only_the_srs_points():
    save = parse_save(_minimal("Grammatikpunkte im SRS: 37"))
    assert save.bunpro_points == 37
    assert save.wanikani_level is None
    assert save.bunpro_level is None


def test_zero_srs_points_is_an_explicit_value():
    save = parse_save(_minimal("Grammatikpunkte im SRS: 0"))
    assert save.bunpro_points == 0
    assert save.bunpro_points is not None


@pytest.mark.parametrize("value", ["1", "10", "37", "250"])
def test_any_non_negative_srs_count_is_accepted(value):
    assert parse_save(_minimal(f"Grammatikpunkte im SRS: {value}")).bunpro_points == int(value)


def test_the_separate_form_carries_all_three_values():
    save = parse_save(_minimal(
        "WaniKani-Level: 2", "Bunpro-Level: N5", "Grammatikpunkte im SRS: 10"
    ))
    assert (save.wanikani_level, save.bunpro_level, save.bunpro_points) == (2, "N5", 10)


# --- invalid optional values ------------------------------------------------

@pytest.mark.parametrize("line,field", [
    ("Grammatikpunkte im SRS: -1", "bunpro_points"),
    ("Grammatikpunkte im SRS: viele", "bunpro_points"),
    ("Grammatikpunkte im SRS:", "bunpro_points"),
    ("WaniKani-Level: zwei", "wanikani_level"),
    ("WaniKani-Level: -2", "wanikani_level"),
    ("Bunpro-Level: unbekannt", "bunpro_level"),
])
def test_an_invalid_optional_value_is_rejected(line, field):
    with pytest.raises(SaveParseError) as excinfo:
        parse_save(_minimal(line))
    assert any(e.field == field for e in excinfo.value.errors)


def test_the_error_names_the_field_in_plain_german():
    with pytest.raises(SaveParseError) as excinfo:
        parse_save(_minimal("Grammatikpunkte im SRS: viele"))
    message = " ".join(e.message for e in excinfo.value.errors)
    assert "Grammatikpunkte im SRS" in message
    assert "nichtnegative Ganzzahl" in message
    assert "(?P<" not in message and "\\d" not in message


# --- the combined legacy line -----------------------------------------------

@pytest.mark.parametrize("line,expected", [
    ("WaniKani: Lv 2 | Bunpro: N5, 10 Grammatikpunkte im SRS", (2, "N5", 10)),
    ("WaniKani: Lv 2|Bunpro: N5, 10 Grammatikpunkte im SRS", (2, "N5", 10)),
    ("WaniKani: Level 2 | Bunpro: N5, 10 Grammatikpunkte im SRS", (2, "N5", 10)),
    ("WaniKani: Lv 2 | Bunpro: N5, 1 Grammatikpunkt im SRS", (2, "N5", 1)),
    ("WaniKani: Lv 2 | Bunpro: N5, 5 Punkte", (2, "N5", 5)),
    ("WaniKani: Lv 2 | Bunpro: N5", (2, "N5", None)),
    ("WaniKani: Lv 2", (2, None, None)),
])
def test_the_combined_line_keeps_working(line, expected):
    save = parse_save(_minimal(line))
    assert (save.wanikani_level, save.bunpro_level, save.bunpro_points) == expected


def test_a_standalone_bunpro_line_is_understood():
    save = parse_save(_minimal("Bunpro: N5"))
    assert save.bunpro_level == "N5"
    assert save.wanikani_level is None


def test_the_old_split_form_still_works():
    save = parse_save(_minimal("WaniKani: Lv 2", "Bunpro: N5"))
    assert (save.wanikani_level, save.bunpro_level) == (2, "N5")


# --- semantic equality ------------------------------------------------------

def test_combined_and_separate_forms_mean_the_same():
    combined = parse_save(_minimal("WaniKani: Lv 2 | Bunpro: N5, 10 Grammatikpunkte im SRS"))
    separate = parse_save(_minimal(
        "WaniKani-Level: 2", "Bunpro-Level: N5", "Grammatikpunkte im SRS: 10"
    ))
    assert (combined.wanikani_level, combined.bunpro_level, combined.bunpro_points) == (2, "N5", 10)
    assert combined.normalized_text() == separate.normalized_text()
    assert combined.normalized_hash() == separate.normalized_hash()


def test_an_unset_metric_is_not_the_same_as_zero():
    absent = parse_save(MINIMAL_SAVE)
    zero = parse_save(_minimal("Grammatikpunkte im SRS: 0"))
    assert absent.normalized_hash() != zero.normalized_hash()


# --- conflicts between both spellings ---------------------------------------

def test_agreeing_duplicate_values_are_accepted():
    save = parse_save(_minimal(
        "WaniKani: Lv 2 | Bunpro: N5, 10 Grammatikpunkte im SRS",
        "WaniKani-Level: 2", "Bunpro-Level: N5", "Grammatikpunkte im SRS: 10",
    ))
    assert (save.wanikani_level, save.bunpro_level, save.bunpro_points) == (2, "N5", 10)


def test_a_contradicting_srs_count_is_refused():
    with pytest.raises(SaveParseError) as excinfo:
        parse_save(_minimal(
            "WaniKani: Lv 2 | Bunpro: N5, 10 Grammatikpunkte im SRS",
            "Grammatikpunkte im SRS: 37",
        ))
    message = " ".join(e.message for e in excinfo.value.errors)
    assert "Widersprüchliche Angaben" in message
    assert "10" in message and "37" in message


def test_a_contradicting_wanikani_level_is_refused():
    with pytest.raises(SaveParseError) as excinfo:
        parse_save(_minimal("WaniKani: Lv 2", "WaniKani-Level: 3"))
    assert any(e.field == "wanikani_level" for e in excinfo.value.errors)


def test_a_contradicting_bunpro_level_is_refused():
    with pytest.raises(SaveParseError) as excinfo:
        parse_save(_minimal("Bunpro-Level: N5", "Bunpro: N4"))
    assert any(e.field == "bunpro_level" for e in excinfo.value.errors)


def test_neither_value_is_silently_preferred():
    with pytest.raises(SaveParseError):
        parse_save(_minimal(
            "WaniKani: Lv 2 | Bunpro: N5, 10 Grammatikpunkte im SRS",
            "Grammatikpunkte im SRS: 37",
        ))


# --- the session pair -------------------------------------------------------

def test_both_session_lines_are_read():
    save = parse_save(_minimal("Session-Modus: START", "Session-Abschluss: vollständig"))
    assert save.has_session_fields


def test_neither_session_line_is_fine():
    assert not parse_save(MINIMAL_SAVE).has_any_session_line


@pytest.mark.parametrize("line", ["Session-Modus: START", "Session-Abschluss: vollständig"])
def test_a_half_written_session_pair_is_refused(line):
    with pytest.raises(SaveParseError) as excinfo:
        parse_save(_minimal(line))
    message = " ".join(e.message for e in excinfo.value.errors)
    assert "Session-Modus" in message and "Session-Abschluss" in message
    assert "gemeinsam" in message
