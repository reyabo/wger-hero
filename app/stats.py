"""
Character stats registry and stat-XP reward helpers.

Global XP (HeroProfile.total_xp) and stat XP (HeroStat.xp) are kept strictly
separate: global XP drives the character level, stat XP feeds the per-attribute
progression used by the future stats / radar screen.

Everything here is deterministic and transparent — stat rewards are defined by
the user on each habit / quest and applied exactly as written. No AI, no
hidden weighting.
"""

import json
import logging
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from app.models import HeroStat, StatXpEvent

logger = logging.getLogger(__name__)

# Canonical stat keys (stored) → German display names (shown).
# The order here is the canonical display order for the future radar chart.
STATS: dict[str, str] = {
    "strength": "Stärke",
    "endurance": "Ausdauer",
    "dexterity": "Geschicklichkeit",
    "mobility": "Beweglichkeit",
    "body_control": "Körperkontrolle",
    "technique": "Technik",
    "discipline": "Disziplin",
    "knowledge": "Wissen",
    "creativity": "Kreativität",
    "recovery": "Regeneration",
}

STAT_KEYS: list[str] = list(STATS.keys())


def display_name(stat_key: str) -> str:
    """German display name for a stat key (falls back to the key itself)."""
    return STATS.get(stat_key, stat_key)


def parse_stat_rewards(raw: object) -> dict[str, int]:
    """
    Normalize an arbitrary stat-rewards value into a clean ``{stat_key: xp}`` dict.

    Accepts a dict or a JSON string. Unknown stat keys and non-positive / invalid
    amounts are dropped. The result only ever contains known stat keys mapped to
    positive integers, so callers can trust it without further validation.
    """
    if raw is None:
        return {}
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            return {}
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            logger.warning("Ignoring malformed stat_rewards JSON")
            return {}
    if not isinstance(raw, dict):
        return {}

    cleaned: dict[str, int] = {}
    for key, value in raw.items():
        if key not in STATS:
            continue
        try:
            amount = int(value)
        except (TypeError, ValueError):
            continue
        if amount > 0:
            cleaned[key] = amount
    return cleaned


def serialize_stat_rewards(rewards: dict[str, int]) -> Optional[str]:
    """Serialize a stat-rewards dict to a compact JSON string (or None if empty)."""
    cleaned = parse_stat_rewards(rewards)
    if not cleaned:
        return None
    return json.dumps(cleaned, sort_keys=True)


def award_stat_xp(
    db: Session,
    rewards: dict[str, int],
    *,
    source: str,
    source_id: Optional[str],
    title: str,
    when: Optional[datetime] = None,
) -> int:
    """
    Apply stat XP to the per-attribute totals and record an audit event per stat.

    Returns the total stat XP awarded. Does not commit — the caller owns the
    transaction so habit/quest completion stays atomic.
    """
    rewards = parse_stat_rewards(rewards)
    if not rewards:
        return 0

    when = when or datetime.utcnow()
    total = 0
    for stat_key, amount in rewards.items():
        stat = db.query(HeroStat).filter(HeroStat.stat_key == stat_key).first()
        if stat is None:
            stat = HeroStat(stat_key=stat_key, xp=0)
            db.add(stat)
            db.flush()
        stat.xp += amount
        stat.updated_at = when

        db.add(
            StatXpEvent(
                stat_key=stat_key,
                xp=amount,
                source=source,
                source_id=source_id,
                title=title,
                created_at=when,
            )
        )
        total += amount

    return total


def get_stat_totals(db: Session) -> dict[str, int]:
    """
    Return every stat key mapped to its cumulative XP (0 if never awarded).

    Always returns all 10 stats in canonical order so the UI / future radar
    chart has a complete, stable dataset to render.
    """
    rows = {s.stat_key: s.xp for s in db.query(HeroStat).all()}
    return {key: rows.get(key, 0) for key in STAT_KEYS}
