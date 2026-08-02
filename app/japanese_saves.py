"""
Parsing, normalization and delta calculation for the Japanese coach SAVE block.

Deliberately database-free so the rules stay unit-testable without a session —
same principle as ``app/xp.py`` and ``app/rewards.py``. Nothing here calls a
model, a session, or the network.

The SAVE block is copy-pasted by the user. Only a small core is required —
the date, the character line and the five scores::

    === 状態 SAVE ===
    Datum: 2026-08-01 | Streak: 4
    Charakter: Lv 2 (見習い) | 433 / 1000 XP
    語彙 180 | 文法 250 | 読解 0 | 聴解 0 | 会話 215
    === END SAVE ===

Everything else is optional and independent: the learning metrics (as three
separate lines or as the combined legacy line), the session pair, and the coach
notes. A missing optional value is ``None`` — never ``0``, never a placeholder,
and never the value from an earlier import::

    === 状態 SAVE ===
    Datum: 2026-08-01 | Streak: 4
    WaniKani-Level: 2
    Bunpro-Level: N5
    Grammatikpunkte im SRS: 37
    Charakter: Lv 2 (見習い) | 433 / 1000 XP
    Session-Modus: START
    Session-Abschluss: vollständig
    語彙 180 | 文法 250 | 読解 0 | 聴解 0 | 会話 215
    Aktueller Grammatikpunkt: これ
    === END SAVE ===

``433 / 1000 XP`` is progress *inside the current source level*, not a lifetime
total. Only the derived per-session increase is ever awarded as global XP; the
wger-hero level stays canonical and is recalculated by ``app.xp.recalc_level``.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, replace
from datetime import date
from typing import Optional

# Guard against pathological pastes. Generous enough for any real SAVE block.
MAX_SAVE_LENGTH = 10_000

START_MARKER = "状態 SAVE"
END_MARKER = "END SAVE"

# Classification values stored on JapaneseSaveImport.classification
CLASSIFICATION_BASELINE = "baseline"
CLASSIFICATION_PROGRESS = "progress"
CLASSIFICATION_DUPLICATE = "duplicate"
CLASSIFICATION_HISTORICAL = "historical"
CLASSIFICATION_WARNING = "warning"

# How the reward was derived (stored on JapaneseSaveImport.reward_calculation)
CALC_BASELINE = "baseline"
CALC_DETERMINISTIC = "deterministic_session"
CALC_LEGACY = "legacy_level_delta"
CALC_HISTORICAL = "historical"
CALC_DUPLICATE = "duplicate"
CALC_WARNING = "warning"

# ---------------------------------------------------------------------------
# Deterministic session rewards
#
# The reward for a Japanese session is decided by wger-hero from two objective
# fields the coach reports — session mode and completion. The five competence
# scores are a factual snapshot and never influence XP; a reported "Session-XP"
# line is kept as source metadata only. No percentages, no AI weighting.
# ---------------------------------------------------------------------------

JAPANESE_SESSION_REWARDS: dict[str, int] = {
    "MINI": 15,
    "START": 40,
    "START_VOICE": 40,
    "GENKI": 40,
    "IRODORI": 40,
    "SCHWACH": 40,
    "BOSS": 80,
    "STATUS": 0,
}

SESSION_MODES = frozenset(JAPANESE_SESSION_REWARDS)

# A partially completed session is worth a mini reward at most.
PARTIAL_SESSION_CAP = 15

COMPLETION_FULL = "vollständig"
COMPLETION_PARTIAL = "teilweise"
COMPLETION_ABORTED = "abgebrochen"
COMPLETION_NONE = "keine Leistung"

SESSION_COMPLETIONS = frozenset({
    COMPLETION_FULL, COMPLETION_PARTIAL, COMPLETION_ABORTED, COMPLETION_NONE,
})

# The learning category whose canonical stat split every Japanese session uses.
# Reusing rewards.CATEGORY_STAT_MAP keeps one single answer to "learning →
# attributes" instead of inventing a second, Japanese-specific table.
JAPANESE_STAT_CATEGORY = "knowledge_learning"


def normalize_session_mode(raw: str) -> Optional[str]:
    """Normalize a user-written mode to a canonical key, or None if unknown.

    Accepts "START VOICE", "START-VOICE" and "START_VOICE" alike, in any case.
    """
    if raw is None:
        return None
    key = re.sub(r"[\s\-]+", "_", raw.strip()).upper()
    key = re.sub(r"_+", "_", key).strip("_")
    return key if key in SESSION_MODES else None


def normalize_session_completion(raw: str) -> Optional[str]:
    """Normalize a completion value to its canonical spelling, or None."""
    if raw is None:
        return None
    key = re.sub(r"\s+", " ", raw.strip()).casefold()
    for value in SESSION_COMPLETIONS:
        if value.casefold() == key:
            return value
    return None


def calculate_session_reward(mode: str, completion: str) -> int:
    """Global XP for a session, purely from mode and completion.

    STATUS is always 0. A full session pays its table value, a partial one at
    most PARTIAL_SESSION_CAP, and aborted / no-performance sessions pay nothing.
    """
    full = JAPANESE_SESSION_REWARDS.get(mode)
    if full is None:
        return 0
    if completion == COMPLETION_FULL:
        return full
    if completion == COMPLETION_PARTIAL:
        return min(PARTIAL_SESSION_CAP, full)
    return 0  # abgebrochen | keine Leistung


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FieldError:
    """One field-scoped, user-facing (German) validation problem."""

    field: str
    message: str


class SaveParseError(Exception):
    """Raised when a SAVE block cannot be parsed. Carries all field errors."""

    def __init__(self, errors: list[FieldError]):
        self.errors = errors
        super().__init__("; ".join(f"{e.field}: {e.message}" for e in errors))


# ---------------------------------------------------------------------------
# Normalization helpers
# ---------------------------------------------------------------------------

_QUOTE_MAP = {
    "„": '"',  # „
    "“": '"',  # "
    "”": '"',  # "
    "‚": "'",  # ‚
    "‘": "'",  # '
    "’": "'",  # '
    "«": '"',  # «
    "»": '"',  # »
    "‹": "'",
    "›": "'",
}

_DASH_MAP = {
    "–": "-",  # –
    "—": "-",  # —
    "−": "-",  # −
}


def _normalize_text(value: str) -> str:
    """Normalize a free-text field for comparison and hashing.

    Unifies quote and dash characters, collapses whitespace, and drops a
    trailing sentence period so ``besiegt.`` and ``besiegt`` compare equal.
    """
    text = unicodedata.normalize("NFC", value)
    for src, dst in {**_QUOTE_MAP, **_DASH_MAP}.items():
        text = text.replace(src, dst)
    text = re.sub(r"\s+", " ", text).strip()
    return text.rstrip(".").strip()


def _strip_markers_and_normalize(raw: str) -> str:
    """Normalize line endings and unify typographic characters for parsing."""
    text = unicodedata.normalize("NFC", raw)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    for src, dst in {**_QUOTE_MAP, **_DASH_MAP}.items():
        text = text.replace(src, dst)
    return text


# ---------------------------------------------------------------------------
# Parsed SAVE
# ---------------------------------------------------------------------------

def _opt_number(value: Optional[int]) -> str:
    """Canonical text for an optional number: "" when unset, never "0"."""
    return "" if value is None else str(value)


@dataclass(frozen=True)
class JapaneseSave:
    """One fully parsed and validated SAVE snapshot."""

    save_date: date
    streak: int
    # Independent, optional learning metrics. None means "not stated in this
    # SAVE" and is deliberately not 0: a missing count and a counted zero are
    # different facts, and only one of them may be stored as a number.
    wanikani_level: Optional[int]
    bunpro_level: Optional[str]
    bunpro_points: Optional[int]
    character_level: int
    character_rank: str
    level_xp: int
    level_xp_cap: int
    vocabulary: int
    grammar: int
    reading: int
    listening: int
    speaking: int
    # Optional coach notes. Absent or empty is None — no placeholder is
    # invented and no value is carried over from an earlier import.
    grammar_point: Optional[str]
    debuffs: Optional[str]
    new_vocabulary: Optional[str]
    daily_quest: Optional[str]
    session_xp: Optional[int]
    raw: str
    # New deterministic-reward fields. None when the line is absent (old format)
    # or when the written value could not be recognized — the *_raw fields keep
    # what was written so the warning can quote it.
    session_mode: Optional[str] = None
    session_mode_raw: Optional[str] = None
    session_completion: Optional[str] = None
    session_completion_raw: Optional[str] = None

    @property
    def has_session_fields(self) -> bool:
        """True when both new-format lines are present and understood."""
        return self.session_mode is not None and self.session_completion is not None

    @property
    def has_any_session_line(self) -> bool:
        """True when at least one new-format line was written at all."""
        return self.session_mode_raw is not None or self.session_completion_raw is not None

    def normalized_text(self) -> str:
        """Canonical, format-independent representation used for hashing."""
        parts = [
            self.save_date.isoformat(),
            str(self.streak),
            _opt_number(self.wanikani_level),
            (self.bunpro_level or "").upper(),
            _opt_number(self.bunpro_points),
            str(self.character_level),
            _normalize_text(self.character_rank),
            str(self.level_xp),
            str(self.level_xp_cap),
            str(self.vocabulary),
            str(self.grammar),
            str(self.reading),
            str(self.listening),
            str(self.speaking),
            _normalize_text(self.grammar_point or ""),
            _normalize_text(self.debuffs or ""),
            _normalize_text(self.new_vocabulary or ""),
            _normalize_text(self.daily_quest or ""),
            "" if self.session_xp is None else str(self.session_xp),
            self.session_mode or _normalize_text(self.session_mode_raw or ""),
            self.session_completion or _normalize_text(self.session_completion_raw or ""),
        ]
        return "|".join(parts)

    def normalized_hash(self) -> str:
        """SHA-256 over the canonical representation (duplicate detection)."""
        return hashlib.sha256(self.normalized_text().encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

# "Lv 2" / "Lv. 2" / "Lv.2" / "Level 2"
_LV = r"(?:Lv\.?|Level)\s*(\d+)"

# The Bunpro count is written either as bare "5 Punkte" or, in newer coach
# output, as "10 Grammatikpunkte im SRS". Same number, same meaning — only the
# wording differs, so it is a wording variant of one field, not a second field.
_BUNPRO_POINTS = r"(?P<points>-?\d+)\s*(?:Grammatik)?Punkte?(?:\s+im\s+SRS)?"

# Bunpro levels the coach may report. JLPT N1–N5 are the levels Bunpro itself
# organises its grammar by; nothing beyond them is invented here, and anything
# else is refused rather than stored as an unknown string.
BUNPRO_LEVELS = ("N1", "N2", "N3", "N4", "N5")

# The optional Bunpro tail of the combined line: ", 10 Grammatikpunkte im SRS",
# ", 5 Punkte", or nothing at all.
_BUNPRO_TAIL = r"(?:\s*,\s*" + _BUNPRO_POINTS + r")?"

_PATTERNS: dict[str, re.Pattern] = {
    "datum": re.compile(
        r"^Datum:\s*(?P<date>[0-9]{1,4}-[0-9]{1,2}-[0-9]{1,2})\s*\|\s*"
        r"Streak:\s*(?P<streak>-?\d+)\s*$",
        re.IGNORECASE,
    ),
    # The combined legacy line. Everything after the WaniKani level is optional,
    # so "WaniKani: Lv 2", "… | Bunpro: N5" and the full form all parse.
    "wanikani": re.compile(
        r"^WaniKani:\s*" + _LV +
        r"(?:\s*\|\s*Bunpro:\s*(?P<bunpro>[A-Za-z0-9]+)" + _BUNPRO_TAIL + r")?\s*$",
        re.IGNORECASE,
    ),
    # The same Bunpro half on a line of its own, as older SAVEs wrote it.
    "bunpro": re.compile(
        r"^Bunpro:\s*(?P<bunpro>[A-Za-z0-9]+)" + _BUNPRO_TAIL + r"\s*$",
        re.IGNORECASE,
    ),
    # Independent modern spellings. Each may appear alone, together with the
    # others, or not at all. The value is captured loosely and validated below,
    # so "zwei" produces a plain message instead of an unreadable-line error.
    "wanikani_level": re.compile(r"^WaniKani-Level:\s*(?P<value>.*)$", re.IGNORECASE),
    "bunpro_level": re.compile(r"^Bunpro-Level:\s*(?P<value>.*)$", re.IGNORECASE),
    "srs_points": re.compile(
        r"^Grammatikpunkte im SRS:\s*(?P<value>.*)$", re.IGNORECASE
    ),
    "charakter": re.compile(
        r"^Charakter:\s*" + _LV + r"\s*\((?P<rank>[^)]*)\)\s*\|\s*"
        r"(?P<xp>-?\d+)\s*/\s*(?P<cap>-?\d+)\s*XP\s*$",
        re.IGNORECASE,
    ),
    "scores": re.compile(
        r"^語彙\s*(?P<vocab>-?\d+)\s*\|\s*文法\s*(?P<grammar>-?\d+)\s*\|\s*"
        r"読解\s*(?P<reading>-?\d+)\s*\|\s*聴解\s*(?P<listening>-?\d+)\s*\|\s*"
        r"会話\s*(?P<speaking>-?\d+)\s*$"
    ),
    "grammatikpunkt": re.compile(r"^Aktueller Grammatikpunkt:\s*(?P<value>.*)$", re.IGNORECASE),
    "debuffs": re.compile(r"^Debuffs:\s*(?P<value>.*)$", re.IGNORECASE),
    "vokabeln": re.compile(r"^Neue Vokabeln heute:\s*(?P<value>.*)$", re.IGNORECASE),
    "tagesquest": re.compile(r"^Tagesquest:\s*(?P<value>.*)$", re.IGNORECASE),
    "session_xp": re.compile(r"^Session-XP:\s*(?P<value>-?\d+)\s*$", re.IGNORECASE),
    "session_mode": re.compile(r"^Session-Modus:\s*(?P<value>.+?)\s*$", re.IGNORECASE),
    "session_completion": re.compile(r"^Session-Abschluss:\s*(?P<value>.+?)\s*$", re.IGNORECASE),
}

# Which parsed key feeds which model/dataclass field (for error reporting)
_FIELD_OF_KEY = {
    "datum": "save_date",
    "wanikani": "wanikani_level",
    "bunpro": "bunpro_level",
    "wanikani_level": "wanikani_level",
    "bunpro_level": "bunpro_level",
    "srs_points": "bunpro_points",
    "charakter": "character_level",
    "scores": "scores",
    "grammatikpunkt": "grammar_point",
    "debuffs": "debuffs",
    "vokabeln": "new_vocabulary",
    "tagesquest": "daily_quest",
    "session_xp": "session_xp",
    "session_mode": "session_mode",
    "session_completion": "session_completion",
}

# The required core. Everything else is optional: the coach does not always
# report a learning metric or a note, and a SAVE without them is a complete
# snapshot, not a broken one. These three stay mandatory because the date drives
# the baseline/historical classification, the character line drives the level
# progress, and the five scores are the snapshot itself — dropping any of them
# would leave existing XP or history logic undefined.
_REQUIRED_KEYS = [
    "datum",
    "charakter",
    "scores",
]

# Only the required core can be reported as missing; everything else is
# optional and its absence is a fact, not an error.
_MISSING_MESSAGES = {
    "datum": "Zeile „Datum: … | Streak: …“ fehlt.",
    "charakter": "Zeile „Charakter: Lv … | … / … XP“ fehlt.",
    "scores": "Zeile mit den fünf Sprachwerten (語彙 / 文法 / 読解 / 聴解 / 会話) fehlt.",
}

# Prefix → key, used to detect a line that is *meant* as a field but malformed,
# and to detect the same field appearing twice.
_PREFIXES = [
    ("datum:", "datum"),
    ("wanikani-level:", "wanikani_level"),
    ("bunpro-level:", "bunpro_level"),
    ("grammatikpunkte im srs:", "srs_points"),
    ("wanikani:", "wanikani"),
    ("bunpro:", "bunpro"),
    ("charakter:", "charakter"),
    ("aktueller grammatikpunkt:", "grammatikpunkt"),
    ("debuffs:", "debuffs"),
    ("neue vokabeln heute:", "vokabeln"),
    ("tagesquest:", "tagesquest"),
    ("session-xp:", "session_xp"),
    ("session-modus:", "session_mode"),
    ("session-abschluss:", "session_completion"),
]


# ---------------------------------------------------------------------------
# Optional learning metrics: parse, then merge the two spellings
# ---------------------------------------------------------------------------

# Human-readable names, used in messages instead of internal field names.
_METRIC_LABELS = {
    "wanikani_level": "WaniKani-Level",
    "bunpro_level": "Bunpro-Level",
    "bunpro_points": "Grammatikpunkte im SRS",
}


def _parse_count(raw: str, field: str) -> tuple[Optional[int], Optional[FieldError]]:
    """A non-negative integer, or a plain German message saying why not.

    An empty value is an error, not "unset": the line was written, so it was
    meant to state something, and silently dropping it would hide a typo.
    """
    text = (raw or "").strip()
    expected = (
        f"Ungültiger Wert für „{_METRIC_LABELS[field]}“: "
        "Es wird eine nichtnegative Ganzzahl erwartet."
    )
    if not text:
        return None, FieldError(field, expected)
    if not re.fullmatch(r"-?\d+", text):
        return None, FieldError(field, expected)
    value = int(text)
    if value < 0:
        return None, FieldError(field, expected)
    return value, None


def _parse_bunpro_level(raw: str) -> tuple[Optional[str], Optional[FieldError]]:
    """One of the known Bunpro levels, or a message listing them."""
    text = (raw or "").strip().upper()
    if text in BUNPRO_LEVELS:
        return text, None
    return None, FieldError(
        "bunpro_level",
        f"Unbekanntes Bunpro-Level: „{text[:20]}“. "
        f"Erwartet wird eines von: {', '.join(BUNPRO_LEVELS)}.",
    )


def merge_metric(field: str, first, second) -> tuple[object, Optional[FieldError]]:
    """Combine the combined-line value and the separate-line value of one metric.

    Either may be None (not stated). If both are given they must agree —
    a contradiction is reported, never resolved by quietly preferring one side.
    """
    if first is None:
        return second, None
    if second is None:
        return first, None
    if first == second:
        return first, None
    return None, FieldError(
        field,
        f"Widersprüchliche Angaben für „{_METRIC_LABELS[field]}“: "
        f"{first} und {second}.",
    )


def _optional_note(found: dict, key: str) -> Optional[str]:
    """A coach note, or None when the line is missing or written empty.

    An absent note is not "keine" and not the previous import's value — the
    SAVE simply did not say, and that is what gets stored.
    """
    match = found.get(key)
    if match is None:
        return None
    return _normalize_text(match.group("value")) or None


def _key_for_line(line: str) -> Optional[str]:
    lowered = line.lower()
    for prefix, key in _PREFIXES:
        if lowered.startswith(prefix):
            return key
    if line.startswith("語彙"):
        return "scores"
    return None


def parse_save(raw: str) -> JapaneseSave:
    """Parse a SAVE block into a :class:`JapaneseSave`.

    Raises :class:`SaveParseError` with German, field-scoped messages. Never
    guesses a value it could not read.
    """
    errors: list[FieldError] = []

    if raw is None or not raw.strip():
        raise SaveParseError([FieldError("raw_save", "Es wurde kein SAVE-Text übergeben.")])

    if len(raw) > MAX_SAVE_LENGTH:
        raise SaveParseError([
            FieldError(
                "raw_save",
                f"Der Text ist zu lang (max. {MAX_SAVE_LENGTH} Zeichen).",
            )
        ])

    text = _strip_markers_and_normalize(raw)

    # --- markers -----------------------------------------------------------
    if START_MARKER not in text:
        errors.append(FieldError("raw_save", "Startmarker „=== 状態 SAVE ===“ fehlt."))
    if END_MARKER not in text:
        errors.append(FieldError("raw_save", "Endmarker „=== END SAVE ===“ fehlt."))
    if errors:
        raise SaveParseError(errors)

    start = text.index(START_MARKER)
    end = text.index(END_MARKER, start)
    # Cut to the body strictly between the two marker lines.
    body_start = text.find("\n", start)
    body_end = text.rfind("\n", start, end)
    if body_start == -1 or body_end == -1 or body_end <= body_start:
        raise SaveParseError([
            FieldError("raw_save", "Zwischen den Markern steht kein SAVE-Inhalt.")
        ])
    body = text[body_start:body_end]

    # --- line scan ---------------------------------------------------------
    found: dict[str, re.Match] = {}
    seen_keys: set[str] = set()

    for line in body.split("\n"):
        line = line.strip()
        if not line:
            continue
        key = _key_for_line(line)
        if key is None:
            continue  # unknown extra line: tolerated, ignored
        if key in seen_keys:
            errors.append(FieldError(
                _FIELD_OF_KEY[key],
                f"Das Feld kommt mehrfach vor: „{line[:40]}“.",
            ))
            continue
        seen_keys.add(key)
        match = _PATTERNS[key].match(line)
        if match is None:
            errors.append(FieldError(
                _FIELD_OF_KEY[key],
                f"Die Zeile konnte nicht gelesen werden: „{line[:60]}“.",
            ))
            continue
        found[key] = match

    for key in _REQUIRED_KEYS:
        if key not in seen_keys:
            errors.append(FieldError(_FIELD_OF_KEY[key], _MISSING_MESSAGES[key]))

    if errors:
        raise SaveParseError(errors)

    # --- optional learning metrics ------------------------------------------
    # Two spellings may state the same three facts: the combined legacy line and
    # the independent modern lines. Both are read into the same fields and then
    # merged, so a SAVE may use either — or both, as long as they agree.
    combined: dict[str, object] = {
        "wanikani_level": None, "bunpro_level": None, "bunpro_points": None,
    }
    separate: dict[str, object] = dict(combined)

    if "wanikani" in found:
        match = found["wanikani"]
        combined["wanikani_level"] = int(match.group(1))
        if match.group("bunpro"):
            level, error = _parse_bunpro_level(match.group("bunpro"))
            errors.append(error) if error else None
            combined["bunpro_level"] = level
        if match.group("points") is not None:
            value, error = _parse_count(match.group("points"), "bunpro_points")
            errors.append(error) if error else None
            combined["bunpro_points"] = value

    if "bunpro" in found:
        match = found["bunpro"]
        level, error = _parse_bunpro_level(match.group("bunpro"))
        errors.append(error) if error else None
        # A standalone Bunpro line belongs to the same side as the combined one,
        # which is why writing both halves of the old split format never
        # conflicts with itself.
        merged_level, conflict = merge_metric(
            "bunpro_level", combined["bunpro_level"], level
        )
        errors.append(conflict) if conflict else None
        combined["bunpro_level"] = merged_level
        if match.group("points") is not None:
            value, error = _parse_count(match.group("points"), "bunpro_points")
            errors.append(error) if error else None
            merged_points, conflict = merge_metric(
                "bunpro_points", combined["bunpro_points"], value
            )
            errors.append(conflict) if conflict else None
            combined["bunpro_points"] = merged_points

    if "wanikani_level" in found:
        value, error = _parse_count(found["wanikani_level"].group("value"), "wanikani_level")
        errors.append(error) if error else None
        separate["wanikani_level"] = value
    if "bunpro_level" in found:
        level, error = _parse_bunpro_level(found["bunpro_level"].group("value"))
        errors.append(error) if error else None
        separate["bunpro_level"] = level
    if "srs_points" in found:
        value, error = _parse_count(found["srs_points"].group("value"), "bunpro_points")
        errors.append(error) if error else None
        separate["bunpro_points"] = value

    metrics: dict[str, object] = {}
    for field_name in ("wanikani_level", "bunpro_level", "bunpro_points"):
        value, conflict = merge_metric(
            field_name, combined[field_name], separate[field_name]
        )
        if conflict:
            errors.append(conflict)
        metrics[field_name] = value

    # --- session pair --------------------------------------------------------
    # Mode and completion are one statement in two lines. Half of it cannot be
    # evaluated deterministically, so it is refused rather than guessed at.
    if ("session_mode" in found) != ("session_completion" in found):
        errors.append(FieldError(
            "session_mode",
            "„Session-Modus“ und „Session-Abschluss“ müssen gemeinsam "
            "angegeben werden.",
        ))

    if errors:
        raise SaveParseError(errors)

    # --- typed extraction --------------------------------------------------
    datum = found["datum"]
    charakter, scores = found["charakter"], found["scores"]

    try:
        save_date = date.fromisoformat(datum.group("date"))
    except ValueError:
        errors.append(FieldError("save_date", "Das Datum ist ungültig (erwartet: JJJJ-MM-TT)."))
        save_date = None  # type: ignore[assignment]

    numbers = {
        "streak": int(datum.group("streak")),
        "character_level": int(charakter.group(1)),
        "level_xp": int(charakter.group("xp")),
        "level_xp_cap": int(charakter.group("cap")),
        "vocabulary": int(scores.group("vocab")),
        "grammar": int(scores.group("grammar")),
        "reading": int(scores.group("reading")),
        "listening": int(scores.group("listening")),
        "speaking": int(scores.group("speaking")),
    }
    for field, value in numbers.items():
        if value < 0:
            errors.append(FieldError(field, "Der Wert darf nicht negativ sein."))

    session_xp: Optional[int] = None
    if "session_xp" in found:
        session_xp = int(found["session_xp"].group("value"))
        if session_xp < 0:
            errors.append(FieldError("session_xp", "Session-XP darf nicht negativ sein."))

    if numbers["character_level"] < 1:
        errors.append(FieldError("character_level", "Das Level muss mindestens 1 sein."))

    # Optional new-format lines. An unrecognized value is NOT a parse error —
    # the save still imports as a snapshot, but the reward becomes a warning
    # case with 0 XP (see calculate_delta).
    session_mode_raw = session_mode = None
    if "session_mode" in found:
        session_mode_raw = found["session_mode"].group("value").strip()
        session_mode = normalize_session_mode(session_mode_raw)

    session_completion_raw = session_completion = None
    if "session_completion" in found:
        session_completion_raw = found["session_completion"].group("value").strip()
        session_completion = normalize_session_completion(session_completion_raw)

    if errors:
        raise SaveParseError(errors)

    return JapaneseSave(
        save_date=save_date,
        streak=numbers["streak"],
        wanikani_level=metrics["wanikani_level"],
        bunpro_level=metrics["bunpro_level"],
        bunpro_points=metrics["bunpro_points"],
        character_level=numbers["character_level"],
        character_rank=charakter.group("rank").strip(),
        level_xp=numbers["level_xp"],
        level_xp_cap=numbers["level_xp_cap"],
        vocabulary=numbers["vocabulary"],
        grammar=numbers["grammar"],
        reading=numbers["reading"],
        listening=numbers["listening"],
        speaking=numbers["speaking"],
        grammar_point=_optional_note(found, "grammatikpunkt"),
        debuffs=_optional_note(found, "debuffs"),
        new_vocabulary=_optional_note(found, "vokabeln"),
        daily_quest=_optional_note(found, "tagesquest"),
        session_xp=session_xp,
        raw=raw,
        session_mode=session_mode,
        session_mode_raw=session_mode_raw,
        session_completion=session_completion,
        session_completion_raw=session_completion_raw,
    )


# ---------------------------------------------------------------------------
# Delta calculation
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PreviousState:
    """The newest previously imported snapshot, reduced to what delta needs."""

    character_level: int
    level_xp: int
    level_xp_cap: int
    save_date: date


@dataclass(frozen=True)
class DeltaResult:
    classification: str
    xp_delta: int
    warning: Optional[str] = None
    # How the value above was derived — see the CALC_* constants.
    reward_calculation: str = CALC_LEGACY
    # A coach-reported "Session-XP:" value, kept for comparison only.
    reported_session_xp: Optional[int] = None
    # True when the coach reported a value that differs from the rule result.
    reported_mismatch: bool = False

    @property
    def is_legacy(self) -> bool:
        return self.reward_calculation == CALC_LEGACY

    @property
    def awards_stat_xp(self) -> bool:
        """Only a fully specified session moves the attributes.

        Attribute XP requires a reward wger-hero derived itself from session
        mode and completion. The legacy path still pays global XP for backward
        compatibility, but its amount comes from the coach-maintained progress
        bar — so it must not reach the radar. A baseline credit is likewise
        global-only.
        """
        return self.xp_delta > 0 and self.reward_calculation == CALC_DETERMINISTIC


def calculate_delta(
    current: JapaneseSave,
    previous: Optional[PreviousState],
    *,
    accept_baseline_credit: bool = False,
) -> DeltaResult:
    """Derive the global XP increase for ``current`` relative to ``previous``.

    Rules:
      * No previous import → ``baseline``, 0 XP. The historical total cannot be
        reconstructed from a level bar, so nothing is invented. Only on explicit
        opt-in (``accept_baseline_credit``) is the current bar value credited,
        and that is an incomplete historical credit by definition.
      * Older date than the newest import → ``historical``, 0 XP.
      * Explicit ``Session-XP:`` line → authoritative, only checked for obvious
        contradictions.
      * Same source level → ``new - old``.
      * Source level exactly +1 → ``(old_cap - old) + new``.
      * Level jump > 1, level decrease, negative delta or implausible caps →
        ``warning``, 0 XP. Never a deduction: nothing is ever taken away.
    """
    reported = current.session_xp

    if previous is None:
        # A baseline never pays, not even with a valid session mode: the
        # historical total cannot be reconstructed from a level bar.
        if accept_baseline_credit:
            return DeltaResult(
                classification=CLASSIFICATION_BASELINE,
                xp_delta=max(0, current.level_xp),
                warning=(
                    "Startgutschrift übernommen: nur der aktuelle Balkenwert, "
                    "keine vollständige Historie. Es werden keine Attribut-XP vergeben."
                ),
                reward_calculation=CALC_BASELINE,
                reported_session_xp=reported,
            )
        return DeltaResult(
            classification=CLASSIFICATION_BASELINE,
            xp_delta=0,
            warning=(
                "Erster Import (Baseline). Es wird kein XP vergeben, weil der "
                "historische Gesamtfortschritt aus dem Levelbalken nicht "
                "rekonstruierbar ist."
            ),
            reward_calculation=CALC_BASELINE,
            reported_session_xp=reported,
        )

    if current.save_date < previous.save_date:
        return DeltaResult(
            classification=CLASSIFICATION_HISTORICAL,
            xp_delta=0,
            warning=(
                f"Das SAVE-Datum ({current.save_date.isoformat()}) liegt vor dem "
                f"letzten Import ({previous.save_date.isoformat()}). "
                "Es wird kein XP vergeben."
            ),
            reward_calculation=CALC_HISTORICAL,
            reported_session_xp=reported,
        )

    # --- new format: mode + completion decide everything -------------------
    if current.has_session_fields:
        xp = calculate_session_reward(current.session_mode, current.session_completion)
        mismatch = reported is not None and reported != xp
        warning = None
        if mismatch:
            warning = (
                f"Der SAVE meldet {reported} Session-XP. Nach den Regeln von "
                f"wger-hero ergibt diese Sitzung {xp} XP. Verwendet werden {xp} XP."
            )
        return DeltaResult(
            classification=CLASSIFICATION_PROGRESS,
            xp_delta=xp,
            warning=warning,
            reward_calculation=CALC_DETERMINISTIC,
            reported_session_xp=reported,
            reported_mismatch=mismatch,
        )

    # A session line was written but could not be understood, or only one of
    # the two was given. Never guess — snapshot only, no reward.
    if current.has_any_session_line:
        problems = []
        if current.session_mode_raw is not None and current.session_mode is None:
            problems.append(
                f"unbekannter Sitzungsmodus „{current.session_mode_raw}“ "
                f"(erlaubt: {', '.join(sorted(SESSION_MODES))})"
            )
        elif current.session_mode_raw is None:
            problems.append("die Zeile „Session-Modus:“ fehlt")
        if current.session_completion_raw is not None and current.session_completion is None:
            problems.append(
                f"unbekannter Abschluss „{current.session_completion_raw}“ "
                f"(erlaubt: {', '.join(sorted(SESSION_COMPLETIONS))})"
            )
        elif current.session_completion_raw is None:
            problems.append("die Zeile „Session-Abschluss:“ fehlt")
        return DeltaResult(
            classification=CLASSIFICATION_WARNING,
            xp_delta=0,
            warning=(
                "Die Sitzung konnte nicht bewertet werden: "
                + " und ".join(problems)
                + ". Der Snapshot kann gespeichert werden, es wird kein XP vergeben."
            ),
            reward_calculation=CALC_WARNING,
            reported_session_xp=reported,
        )

    # --- legacy format: fall back to the level-bar delta --------------------
    # Method stays 'legacy_level_delta' even for its warning outcomes;
    # classification carries the warning state, reward_calculation the method.
    return replace(
        _legacy_level_delta(current, previous),
        reward_calculation=CALC_LEGACY,
        reported_session_xp=reported,
    )


def _legacy_level_delta(
    current: JapaneseSave, previous: PreviousState
) -> DeltaResult:
    """Pre-session-mode behaviour: derive the delta from the source level bar."""

    # Implausible caps make every derived delta meaningless.
    if current.level_xp_cap <= 0 or previous.level_xp_cap <= 0:
        return DeltaResult(
            classification=CLASSIFICATION_WARNING,
            xp_delta=0,
            warning="Unplausible XP-Obergrenze (0 oder negativ). Es wird kein XP vergeben.",
        )
    if current.level_xp > current.level_xp_cap:
        return DeltaResult(
            classification=CLASSIFICATION_WARNING,
            xp_delta=0,
            warning=(
                "Der Levelbalken liegt über der Obergrenze "
                f"({current.level_xp} / {current.level_xp_cap}). Es wird kein XP vergeben."
            ),
        )

    level_diff = current.character_level - previous.character_level

    # An explicit Session-XP line is authoritative.
    if current.session_xp is not None:
        if level_diff < 0:
            return DeltaResult(
                classification=CLASSIFICATION_WARNING,
                xp_delta=0,
                warning=(
                    "Session-XP angegeben, aber das Quell-Level ist gesunken. "
                    "Es wird kein XP vergeben."
                ),
            )
        return DeltaResult(
            classification=CLASSIFICATION_PROGRESS,
            xp_delta=max(0, current.session_xp),
        )

    if level_diff == 0:
        delta = current.level_xp - previous.level_xp
        if delta < 0:
            return DeltaResult(
                classification=CLASSIFICATION_WARNING,
                xp_delta=0,
                warning=(
                    f"Der Levelbalken ist gesunken ({previous.level_xp} → "
                    f"{current.level_xp}). Es wird kein XP vergeben."
                ),
            )
        return DeltaResult(classification=CLASSIFICATION_PROGRESS, xp_delta=delta)

    if level_diff == 1:
        remaining = previous.level_xp_cap - previous.level_xp
        delta = remaining + current.level_xp
        if delta < 0:
            return DeltaResult(
                classification=CLASSIFICATION_WARNING,
                xp_delta=0,
                warning="Negativer Zuwachs beim Levelaufstieg. Es wird kein XP vergeben.",
            )
        return DeltaResult(classification=CLASSIFICATION_PROGRESS, xp_delta=delta)

    if level_diff > 1:
        return DeltaResult(
            classification=CLASSIFICATION_WARNING,
            xp_delta=0,
            warning=(
                f"Levelsprung um {level_diff} Stufen "
                f"(Lv {previous.character_level} → Lv {current.character_level}). "
                "Der Zuwachs ist nicht sicher berechenbar, es wird kein XP vergeben."
            ),
        )

    return DeltaResult(
        classification=CLASSIFICATION_WARNING,
        xp_delta=0,
        warning=(
            f"Das Quell-Level ist gesunken (Lv {previous.character_level} → "
            f"Lv {current.character_level}). Es wird kein XP vergeben."
        ),
    )
