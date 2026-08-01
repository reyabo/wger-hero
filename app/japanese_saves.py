"""
Parsing, normalization and delta calculation for the Japanese coach SAVE block.

Deliberately database-free so the rules stay unit-testable without a session —
same principle as ``app/xp.py`` and ``app/rewards.py``. Nothing here calls a
model, a session, or the network.

The SAVE block is copy-pasted by the user and looks like this::

    === 状態 SAVE ===
    Datum: 2026-07-31 | Streak: 4
    WaniKani: Lv 1 | Bunpro: N5, 5 Punkte
    Charakter: Lv 2 (見習い) | 433 / 1000 XP
    語彙 180 | 文法 250 | 読解 0 | 聴解 0 | 会話 215
    Aktueller Grammatikpunkt: これ
    Debuffs: keine
    Neue Vokabeln heute: keine
    Tagesquest: Erfüllt – Mini-Boss „Partikel-Golem“ besiegt.
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

@dataclass(frozen=True)
class JapaneseSave:
    """One fully parsed and validated SAVE snapshot."""

    save_date: date
    streak: int
    wanikani_level: int
    bunpro_level: str
    bunpro_points: int
    character_level: int
    character_rank: str
    level_xp: int
    level_xp_cap: int
    vocabulary: int
    grammar: int
    reading: int
    listening: int
    speaking: int
    grammar_point: str
    debuffs: str
    new_vocabulary: str
    daily_quest: str
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
            str(self.wanikani_level),
            self.bunpro_level.upper(),
            str(self.bunpro_points),
            str(self.character_level),
            _normalize_text(self.character_rank),
            str(self.level_xp),
            str(self.level_xp_cap),
            str(self.vocabulary),
            str(self.grammar),
            str(self.reading),
            str(self.listening),
            str(self.speaking),
            _normalize_text(self.grammar_point),
            _normalize_text(self.debuffs),
            _normalize_text(self.new_vocabulary),
            _normalize_text(self.daily_quest),
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

_PATTERNS: dict[str, re.Pattern] = {
    "datum": re.compile(
        r"^Datum:\s*(?P<date>[0-9]{1,4}-[0-9]{1,2}-[0-9]{1,2})\s*\|\s*"
        r"Streak:\s*(?P<streak>-?\d+)\s*$",
        re.IGNORECASE,
    ),
    "wanikani": re.compile(
        r"^WaniKani:\s*" + _LV + r"\s*\|\s*Bunpro:\s*"
        r"(?P<bunpro>[A-Za-z0-9]+)\s*,\s*" + _BUNPRO_POINTS + r"\s*$",
        re.IGNORECASE,
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

_REQUIRED_KEYS = [
    "datum",
    "wanikani",
    "charakter",
    "scores",
    "grammatikpunkt",
    "debuffs",
    "vokabeln",
    "tagesquest",
]

_MISSING_MESSAGES = {
    "datum": "Zeile „Datum: … | Streak: …“ fehlt.",
    "wanikani": "Zeile „WaniKani: … | Bunpro: …“ fehlt.",
    "charakter": "Zeile „Charakter: Lv … | … / … XP“ fehlt.",
    "scores": "Zeile mit den fünf Sprachwerten (語彙 / 文法 / 読解 / 聴解 / 会話) fehlt.",
    "grammatikpunkt": "Zeile „Aktueller Grammatikpunkt: …“ fehlt.",
    "debuffs": "Zeile „Debuffs: …“ fehlt.",
    "vokabeln": "Zeile „Neue Vokabeln heute: …“ fehlt.",
    "tagesquest": "Zeile „Tagesquest: …“ fehlt.",
}

# Prefix → key, used to detect a line that is *meant* as a field but malformed,
# and to detect the same field appearing twice.
_PREFIXES = [
    ("datum:", "datum"),
    ("wanikani:", "wanikani"),
    ("charakter:", "charakter"),
    ("aktueller grammatikpunkt:", "grammatikpunkt"),
    ("debuffs:", "debuffs"),
    ("neue vokabeln heute:", "vokabeln"),
    ("tagesquest:", "tagesquest"),
    ("session-xp:", "session_xp"),
    ("session-modus:", "session_mode"),
    ("session-abschluss:", "session_completion"),
]


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

    # --- typed extraction --------------------------------------------------
    datum, wanikani = found["datum"], found["wanikani"]
    charakter, scores = found["charakter"], found["scores"]

    try:
        save_date = date.fromisoformat(datum.group("date"))
    except ValueError:
        errors.append(FieldError("save_date", "Das Datum ist ungültig (erwartet: JJJJ-MM-TT)."))
        save_date = None  # type: ignore[assignment]

    numbers = {
        "streak": int(datum.group("streak")),
        "wanikani_level": int(wanikani.group(1)),
        "bunpro_points": int(wanikani.group("points")),
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
        wanikani_level=numbers["wanikani_level"],
        bunpro_level=wanikani.group("bunpro").upper(),
        bunpro_points=numbers["bunpro_points"],
        character_level=numbers["character_level"],
        character_rank=charakter.group("rank").strip(),
        level_xp=numbers["level_xp"],
        level_xp_cap=numbers["level_xp_cap"],
        vocabulary=numbers["vocabulary"],
        grammar=numbers["grammar"],
        reading=numbers["reading"],
        listening=numbers["listening"],
        speaking=numbers["speaking"],
        grammar_point=_normalize_text(found["grammatikpunkt"].group("value")),
        debuffs=_normalize_text(found["debuffs"].group("value")),
        new_vocabulary=_normalize_text(found["vokabeln"].group("value")),
        daily_quest=_normalize_text(found["tagesquest"].group("value")),
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
