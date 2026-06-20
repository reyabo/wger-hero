"""
Character stats registry and stat-XP reward helpers.

Global XP (HeroProfile.total_xp) and stat XP (HeroStat.xp) are kept strictly
separate: global XP drives the character level, stat XP feeds the per-attribute
progression used by the stats / radar screen.

Everything here is deterministic and transparent — stat rewards are defined by
the user on each habit / quest and applied exactly as written. No AI, no
hidden weighting.
"""

import json
import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from app.models import HeroStat, StatXpEvent

logger = logging.getLogger(__name__)

# Canonical stat keys (stored) → German display names (shown).
# The order here is the canonical display order for the radar chart.
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

STAT_ABBR: dict[str, str] = {
    "strength": "STR",
    "endurance": "END",
    "dexterity": "DEX",
    "mobility": "MOB",
    "body_control": "CTRL",
    "technique": "TEC",
    "discipline": "DIS",
    "knowledge": "KNO",
    "creativity": "CRE",
    "recovery": "REC",
}


def display_name(stat_key: str) -> str:
    """German display name for a stat key (falls back to the key itself)."""
    return STATS.get(stat_key, stat_key)


# ---------------------------------------------------------------------------
# Stat level formula
# ---------------------------------------------------------------------------

def xp_for_stat_level(stat_level: int) -> int:
    """XP needed to advance from stat_level to stat_level+1."""
    return 100 + stat_level * 50


def calculate_stat_level(total_stat_xp: int) -> "StatLevelInfo":
    """Derive current stat level, in-level XP, and progress from cumulative XP."""
    if total_stat_xp < 0:
        total_stat_xp = 0
    level = 1
    remaining = total_stat_xp
    while True:
        needed = xp_for_stat_level(level)
        if remaining < needed:
            break
        remaining -= needed
        level += 1
    needed_next = xp_for_stat_level(level)
    pct = int(remaining / needed_next * 100) if needed_next else 0
    return StatLevelInfo(
        level=level,
        total_xp=total_stat_xp,
        xp_in_level=remaining,
        xp_for_next=needed_next,
        pct=pct,
    )


@dataclass
class StatLevelInfo:
    level: int
    total_xp: int
    xp_in_level: int
    xp_for_next: int
    pct: int


@dataclass
class StatProgressView:
    key: str
    name: str
    abbr: str
    level: int
    total_xp: int
    xp_in_level: int
    xp_for_next: int
    pct: int


def get_all_stat_progress(db: Session) -> list[StatProgressView]:
    """Return StatProgressView for every stat in canonical order."""
    rows = {s.stat_key: s.xp for s in db.query(HeroStat).all()}
    result = []
    for key in STAT_KEYS:
        total = rows.get(key, 0)
        info = calculate_stat_level(total)
        result.append(StatProgressView(
            key=key,
            name=STATS[key],
            abbr=STAT_ABBR[key],
            level=info.level,
            total_xp=info.total_xp,
            xp_in_level=info.xp_in_level,
            xp_for_next=info.xp_for_next,
            pct=info.pct,
        ))
    return result


def get_recent_stat_gains(db: Session, limit: int = 20) -> list[StatXpEvent]:
    return (
        db.query(StatXpEvent)
        .order_by(StatXpEvent.created_at.desc())
        .limit(limit)
        .all()
    )


def get_stat_summary(db: Session) -> dict:
    """Return summary data for the stats page (strongest, weakest, recent XP)."""
    rows = {s.stat_key: s.xp for s in db.query(HeroStat).all()}
    totals = {key: rows.get(key, 0) for key in STAT_KEYS}
    non_zero = {k: v for k, v in totals.items() if v > 0}

    now = datetime.utcnow()
    week_ago = now - timedelta(days=7)
    month_ago = now - timedelta(days=30)

    week_xp = (
        db.query(StatXpEvent)
        .filter(StatXpEvent.created_at >= week_ago)
        .with_entities(StatXpEvent.xp)
        .all()
    )
    month_xp = (
        db.query(StatXpEvent)
        .filter(StatXpEvent.created_at >= month_ago)
        .with_entities(StatXpEvent.xp)
        .all()
    )

    strongest_key = max(totals, key=totals.get) if non_zero else None
    weakest_key = min(non_zero, key=non_zero.get) if len(non_zero) > 1 else None

    return {
        "strongest_key": strongest_key,
        "strongest_name": STATS[strongest_key] if strongest_key else None,
        "strongest_xp": totals[strongest_key] if strongest_key else 0,
        "weakest_key": weakest_key,
        "weakest_name": STATS[weakest_key] if weakest_key else None,
        "weakest_xp": totals[weakest_key] if weakest_key else 0,
        "week_xp": sum(r.xp for r in week_xp),
        "month_xp": sum(r.xp for r in month_xp),
    }


def generate_radar_points(stats: list[StatProgressView], cx: int = 200, cy: int = 200, r: int = 150) -> list[tuple[float, float]]:
    """Compute SVG polygon points for a radar chart. Uses stat level as the value."""
    n = len(stats)
    if n == 0:
        return []
    max_level = max((s.level for s in stats), default=1)
    scale = max(max_level, 5)  # at least 5 rings for readability
    points = []
    for i, stat in enumerate(stats):
        angle = math.radians(i * 360 / n - 90)
        ratio = stat.level / scale
        px = cx + r * ratio * math.cos(angle)
        py = cy + r * ratio * math.sin(angle)
        points.append((px, py))
    return points


def generate_radar_grid_points(level: int, n: int, cx: int = 200, cy: int = 200, r: int = 150, scale: int = 5) -> list[tuple[float, float]]:
    """Grid ring points for a given level ring."""
    pts = []
    ratio = level / scale
    for i in range(n):
        angle = math.radians(i * 360 / n - 90)
        pts.append((cx + r * ratio * math.cos(angle), cy + r * ratio * math.sin(angle)))
    return pts


# Minimum normalized radius so a fresh / all-level-1 character still shows a
# small but visible baseline shape instead of collapsing to a single point.
RADAR_MIN_RATIO = 0.18


def build_radar(
    stats: list[StatProgressView],
    cx: int = 200,
    cy: int = 200,
    r: int = 150,
) -> dict:
    """
    Build a fully resolved radar chart description for the template.

    Returns a dict with ready-to-render values so the Jinja template needs no
    coordinate math:

        {
            "rings":           [{"points": "x,y x,y ..."}, ...],
            "axes":            [{"x1","y1","x2","y2"}, ...],
            "labels":          [{"x","y","text","full_name"}, ...],
            "polygon_points":  "x,y x,y ...",
            "points":          [{"cx","cy","label","value"}, ...],
            "rings_count":     int,
        }

    The data polygon uses a visible minimum radius (RADAR_MIN_RATIO) so a fresh
    character still renders a small shape rather than an invisible dot.
    """
    n = len(stats)
    if n == 0:
        return {
            "rings": [],
            "axes": [],
            "labels": [],
            "polygon_points": "",
            "points": [],
            "rings_count": 0,
        }

    max_level = max((s.level for s in stats), default=1)
    scale = max(max_level, 5)  # at least 5 rings for readability
    rings_count = scale

    def _fmt(pts: list[tuple[float, float]]) -> str:
        return " ".join(f"{x:.2f},{y:.2f}" for x, y in pts)

    # Concentric grid rings (one polygon per level).
    rings = []
    for ring in range(1, rings_count + 1):
        ratio = ring / scale
        ring_pts = []
        for i in range(n):
            angle = math.radians(i * 360 / n - 90)
            ring_pts.append((cx + r * ratio * math.cos(angle), cy + r * ratio * math.sin(angle)))
        rings.append({"points": _fmt(ring_pts)})

    # Axis spokes + labels + data points.
    axes = []
    labels = []
    points = []
    data_pts = []
    for i, stat in enumerate(stats):
        angle = math.radians(i * 360 / n - 90)
        # axis spoke from centre to outer ring
        ax = cx + r * math.cos(angle)
        ay = cy + r * math.sin(angle)
        axes.append({"x1": cx, "y1": cy, "x2": round(ax, 2), "y2": round(ay, 2)})

        # data vertex (clamped to a visible minimum)
        ratio = max(stat.level / scale, RADAR_MIN_RATIO)
        px = cx + r * ratio * math.cos(angle)
        py = cy + r * ratio * math.sin(angle)
        data_pts.append((px, py))
        points.append({
            "cx": round(px, 2),
            "cy": round(py, 2),
            "label": stat.name,
            "value": stat.level,
        })

        # label just outside the outer ring
        lx = cx + (r + 28) * math.cos(angle)
        ly = cy + (r + 28) * math.sin(angle)
        labels.append({
            "x": round(lx, 2),
            "y": round(ly, 2),
            "text": stat.abbr,
            "full_name": stat.name,
        })

    return {
        "rings": rings,
        "axes": axes,
        "labels": labels,
        "polygon_points": _fmt(data_pts),
        "points": points,
        "rings_count": rings_count,
    }


# ---------------------------------------------------------------------------
# Parse / serialize helpers (used by habits, quests)
# ---------------------------------------------------------------------------

def parse_stat_rewards(raw: object) -> dict[str, int]:
    """
    Normalize an arbitrary stat-rewards value into a clean ``{stat_key: xp}`` dict.

    Accepts a dict or a JSON string. Unknown stat keys and non-positive / invalid
    amounts are dropped.
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

    Always returns all 10 stats in canonical order.
    """
    rows = {s.stat_key: s.xp for s in db.query(HeroStat).all()}
    return {key: rows.get(key, 0) for key in STAT_KEYS}

