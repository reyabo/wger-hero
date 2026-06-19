"""Deterministic XP and stat reward calculation.

All rewards follow visible rules — no hidden weighting, no AI.
Users choose a category, duration, and effort level; the app
calculates XP and stat distribution automatically.
"""

# ---------------------------------------------------------------------------
# Duration size → base XP
# ---------------------------------------------------------------------------

DURATION_XP: dict[str, int] = {
    "mini": 15,    # 5–10 min
    "short": 25,   # 10–20 min
    "normal": 40,  # 20–40 min
    "long": 60,    # 40–60 min
    "large": 80,   # 60+ min
}

DURATION_LABELS: dict[str, str] = {
    "mini": "Mini (5–10 min)",
    "short": "Kurz (10–20 min)",
    "normal": "Normal (20–40 min)",
    "long": "Lang (40–60 min)",
    "large": "Groß (60+ min)",
}

DURATION_CHOICES = list(DURATION_XP.keys())

# ---------------------------------------------------------------------------
# Effort → XP multiplier
# ---------------------------------------------------------------------------

EFFORT_MULTIPLIER: dict[str, float] = {
    "easy": 0.8,
    "normal": 1.0,
    "demanding": 1.25,
}

EFFORT_LABELS: dict[str, str] = {
    "easy": "Leicht",
    "normal": "Normal",
    "demanding": "Anspruchsvoll",
}

EFFORT_CHOICES = list(EFFORT_MULTIPLIER.keys())

# ---------------------------------------------------------------------------
# Category → German display name
# ---------------------------------------------------------------------------

CATEGORIES: dict[str, str] = {
    "strength_training": "Krafttraining",
    "endurance": "Ausdauer",
    "mobility": "Mobility",
    "technique_skill": "Technik / Skill",
    "knowledge_learning": "Wissen / Lernen",
    "creativity": "Kreativität",
    "household_order": "Haushalt / Ordnung",
    "project_work": "Projektarbeit",
    "recovery": "Regeneration",
    "social_community": "Sozial / Gemeinschaft",
}

CATEGORY_CHOICES = list(CATEGORIES.keys())

# ---------------------------------------------------------------------------
# Category → stat distribution (percentages, sum to 100)
# ---------------------------------------------------------------------------

CATEGORY_STAT_MAP: dict[str, dict[str, int]] = {
    "strength_training": {
        "strength": 45, "body_control": 25, "discipline": 20, "technique": 10
    },
    "endurance": {
        "endurance": 60, "discipline": 25, "recovery": 15
    },
    "mobility": {
        "mobility": 65, "recovery": 25, "discipline": 10
    },
    "technique_skill": {
        "technique": 45, "body_control": 30, "discipline": 15, "dexterity": 10
    },
    "knowledge_learning": {
        "knowledge": 70, "discipline": 20, "technique": 10
    },
    "creativity": {
        "creativity": 65, "technique": 20, "discipline": 15
    },
    "household_order": {
        "discipline": 60, "recovery": 25, "dexterity": 15
    },
    "project_work": {
        "knowledge": 40, "technique": 35, "discipline": 25
    },
    "recovery": {
        "recovery": 70, "mobility": 20, "discipline": 10
    },
    "social_community": {
        "creativity": 35, "recovery": 35, "discipline": 20, "knowledge": 10
    },
}


def calculate_xp(duration_size: str, effort: str) -> int:
    """Return global XP rounded to nearest 5."""
    base = DURATION_XP.get(duration_size, 40)
    mult = EFFORT_MULTIPLIER.get(effort, 1.0)
    raw = base * mult
    return int(round(raw / 5) * 5)


def calculate_stat_rewards(category: str, global_xp: int) -> dict[str, int]:
    """Return stat rewards dict using category percentages of global_xp."""
    dist = CATEGORY_STAT_MAP.get(category, {})
    result: dict[str, int] = {}
    for stat, pct in dist.items():
        val = round(global_xp * pct / 100)
        if val > 0:
            result[stat] = val
    return result


def calculate_rewards(duration_size: str, effort: str, category: str) -> tuple[int, dict[str, int]]:
    """Return (global_xp, stat_rewards)."""
    xp = calculate_xp(duration_size, effort)
    stats = calculate_stat_rewards(category, xp)
    return xp, stats
