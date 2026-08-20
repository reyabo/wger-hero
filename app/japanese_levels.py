"""The Japanese coach's 30-level curve, its ranks, tiers and rank bosses.

One source of truth for the whole progression. Nothing here touches a database
or a clock, so every rule is unit-testable on its own — and nothing may
duplicate the table into another module or a template.

Two things this module is deliberately *not*:

**Not the global Hero level.** `HeroProfile.level` stays canonical and is
computed by `app.xp` from global XP. The character level below describes the
coach's own progression and exists to validate and display what the SAVE
reports.

**Not a language proficiency scale.** Ranks and tiers are game flavour. They
carry no relation to JLPT, CEFR or any assessment, and must never be presented
as one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# Level 1 is historical prologue: the documented campaign starts at level 2,
# which is therefore the cumulative zero point. A Version-2 SAVE never needs to
# produce level 1, and no threshold is defined for it.
PROLOGUE_LEVEL = 1
BASELINE_LEVEL = 2
MAX_LEVEL = 30


@dataclass(frozen=True)
class LevelRow:
    level: int
    rank: str
    # Cumulative total XP at which this level begins. None for the prologue.
    threshold: Optional[int]
    # Cumulative total XP at which the next level begins. None at MAX_LEVEL.
    next_threshold: Optional[int]


# level → rank → cumulative threshold. The right-hand column of a SAVE's XP bar
# is the *next* level's threshold, which is why it is carried here too.
LEVELS: tuple[LevelRow, ...] = (
    LevelRow(1, "入門者", None, 0),
    LevelRow(2, "見習い", 0, 1000),
    LevelRow(3, "修行者", 1000, 2100),
    LevelRow(4, "探究者", 2100, 3300),
    LevelRow(5, "挑戦者", 3300, 4600),
    LevelRow(6, "旅人", 4600, 6000),
    LevelRow(7, "冒険者", 6000, 7500),
    LevelRow(8, "実践者", 7500, 9100),
    LevelRow(9, "使い手", 9100, 10800),
    LevelRow(10, "熟練者", 10800, 12600),
    LevelRow(11, "言葉の旅人", 12600, 14500),
    LevelRow(12, "文の探究者", 14500, 16500),
    LevelRow(13, "声の使い手", 16500, 18600),
    LevelRow(14, "会話の使い手", 18600, 20800),
    LevelRow(15, "言葉の使い手", 20800, 23100),
    LevelRow(16, "熟達者", 23100, 25500),
    LevelRow(17, "練達者", 25500, 28000),
    LevelRow(18, "達人", 28000, 30600),
    LevelRow(19, "師範代", 30600, 33300),
    LevelRow(20, "師範", 33300, 36100),
    LevelRow(21, "言葉の達人", 36100, 39000),
    LevelRow(22, "声の達人", 39000, 42000),
    LevelRow(23, "文の達人", 42000, 45100),
    LevelRow(24, "会話の達人", 45100, 48300),
    LevelRow(25, "言の葉の達人", 48300, 51600),
    LevelRow(26, "言霊使い", 51600, 55000),
    LevelRow(27, "言の葉の賢者", 55000, 58500),
    LevelRow(28, "言霊の賢者", 58500, 62100),
    LevelRow(29, "言霊の導師", 62100, 65800),
    LevelRow(30, "言霊の覇者", 65800, None),
)

_BY_LEVEL = {row.level: row for row in LEVELS}


@dataclass(frozen=True)
class RankTier:
    number: int
    numeral: str
    name: str
    first_level: int
    last_level: int


# Six tiers of five levels each. Pure coach gamification.
TIERS: tuple[RankTier, ...] = (
    RankTier(1, "I", "旅立ち", 1, 5),
    RankTier(2, "II", "修行", 6, 10),
    RankTier(3, "III", "実践", 11, 15),
    RankTier(4, "IV", "熟練", 16, 20),
    RankTier(5, "V", "奥義", 21, 25),
    RankTier(6, "VI", "言霊", 26, 30),
)


@dataclass(frozen=True)
class RankBoss:
    boss_id: str
    level: int
    title: str
    reward_id: str
    reward_name: str
    tier: int


# The six milestones. A reward is a permanent badge, never an XP bonus: the
# progression is already paid for by the XP that reached the level.
BOSSES: tuple[RankBoss, ...] = (
    RankBoss("jp-rank-boss-01", 5, "昇格試験 I – 旅立ちの試練", "jp-rank-reward-01", "旅立ちの証", 1),
    RankBoss("jp-rank-boss-02", 10, "昇格試験 II – 修行の試練", "jp-rank-reward-02", "修行の証", 2),
    RankBoss("jp-rank-boss-03", 15, "昇格試験 III – 実践の試練", "jp-rank-reward-03", "実践の証", 3),
    RankBoss("jp-rank-boss-04", 20, "昇格試験 IV – 熟練の試練", "jp-rank-reward-04", "熟練の証", 4),
    RankBoss("jp-rank-boss-05", 25, "昇格試験 V – 奥義の試練", "jp-rank-reward-05", "奥義の証", 5),
    RankBoss("jp-rank-boss-06", 30, "昇格試験 VI – 言霊の試練", "jp-rank-reward-06", "言霊の証", 6),
)

_BY_BOSS_ID = {boss.boss_id: boss for boss in BOSSES}
_BY_REWARD_ID = {boss.reward_id: boss for boss in BOSSES}

# What the coach may write as a boss status. Only a pass grants a reward.
BOSS_STATUS_PASSED = "bestanden"
BOSS_STATUS_FAILED = "nicht bestanden"
BOSS_STATUSES = (BOSS_STATUS_PASSED, BOSS_STATUS_FAILED)


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------

def level_row(level: int) -> Optional[LevelRow]:
    return _BY_LEVEL.get(level)


def rank_for_level(level: int) -> Optional[str]:
    row = _BY_LEVEL.get(level)
    return row.rank if row else None


def threshold_for_level(level: int) -> Optional[int]:
    row = _BY_LEVEL.get(level)
    return row.threshold if row else None


def next_threshold_for_level(level: int) -> Optional[int]:
    row = _BY_LEVEL.get(level)
    return row.next_threshold if row else None


def tier_for_level(level: int) -> Optional[RankTier]:
    for tier in TIERS:
        if tier.first_level <= level <= tier.last_level:
            return tier
    return None


def level_for_total_xp(total_xp: int) -> int:
    """The level a cumulative XP total corresponds to.

    Below the level-2 threshold everything is still level 2: the campaign began
    there, so a cumulative total of 0 is the starting state and not a demotion
    into the prologue.
    """
    level = BASELINE_LEVEL
    for row in LEVELS:
        if row.threshold is not None and total_xp >= row.threshold:
            level = row.level
    return level


def boss_by_id(boss_id: str) -> Optional[RankBoss]:
    return _BY_BOSS_ID.get(boss_id)


def boss_by_reward_id(reward_id: str) -> Optional[RankBoss]:
    return _BY_REWARD_ID.get(reward_id)


def boss_for_level(level: int) -> Optional[RankBoss]:
    for boss in BOSSES:
        if boss.level == level:
            return boss
    return None


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_level_claim(
    level: int, total_xp: int, rank: Optional[str], next_threshold: Optional[int]
) -> list[str]:
    """Check a Version-2 SAVE's own claims against the curve.

    Returns human-readable problems, empty when everything agrees. The raw SAVE
    is never silently corrected — a mismatch is reported and the numbers stay
    as written, because the coach is the source and this table is only a check.
    """
    problems: list[str] = []
    row = _BY_LEVEL.get(level)

    if row is None:
        problems.append(
            f"Level {level} liegt außerhalb der Kurve (gültig sind "
            f"{PROLOGUE_LEVEL}–{MAX_LEVEL})."
        )
        return problems

    expected_level = level_for_total_xp(total_xp)
    if expected_level != level:
        problems.append(
            f"Das gemeldete Level {level} passt nicht zu {total_xp} Gesamt-XP; "
            f"laut Kurve wäre das Level {expected_level}."
        )

    if rank is not None and rank.strip() and rank.strip() != row.rank:
        problems.append(
            f"Der Rang „{rank.strip()}“ passt nicht zu Level {level}; "
            f"erwartet wird „{row.rank}“."
        )

    if next_threshold is not None and row.next_threshold is not None:
        if next_threshold != row.next_threshold:
            problems.append(
                f"Die nächste Schwelle {next_threshold} passt nicht zu Level "
                f"{level}; erwartet werden {row.next_threshold}."
            )

    return problems


def validate_boss_event(
    boss_id: Optional[str],
    status: Optional[str],
    reward_id: Optional[str],
    reward_name: Optional[str],
) -> tuple[Optional[RankBoss], list[str]]:
    """Check an optional boss event. Returns (boss, problems).

    A boss is returned only when the whole group is present, internally
    consistent and marks a pass. Anything else yields problems and no boss —
    an inconsistent combination must never produce a reward, and a broken boss
    line must not destroy the rest of the SAVE.
    """
    problems: list[str] = []
    stated = [v for v in (boss_id, status, reward_id, reward_name) if v]
    if not stated:
        return None, problems

    if not boss_id:
        problems.append("Es wurde ein Boss-Ereignis angegeben, aber keine Rangstufenboss-ID.")
        return None, problems

    boss = _BY_BOSS_ID.get(boss_id.strip())
    if boss is None:
        problems.append(f"Unbekannte Rangstufenboss-ID „{boss_id.strip()}“.")
        return None, problems

    if not status:
        problems.append(
            f"Zu „{boss.boss_id}“ fehlt der Rangstufenboss-Status "
            f"(erlaubt: {', '.join(BOSS_STATUSES)})."
        )
        return None, problems

    normalized_status = status.strip().casefold()
    if normalized_status not in {s.casefold() for s in BOSS_STATUSES}:
        problems.append(
            f"Unbekannter Rangstufenboss-Status „{status.strip()}“ "
            f"(erlaubt: {', '.join(BOSS_STATUSES)})."
        )
        return None, problems

    if reward_id and reward_id.strip() != boss.reward_id:
        problems.append(
            f"Die Rangbelohnung-ID „{reward_id.strip()}“ gehört nicht zu "
            f"„{boss.boss_id}“; erwartet wird „{boss.reward_id}“."
        )
        return None, problems

    if reward_name and reward_name.strip() != boss.reward_name:
        problems.append(
            f"Der Belohnungsname „{reward_name.strip()}“ passt nicht zu "
            f"„{boss.reward_id}“; erwartet wird „{boss.reward_name}“."
        )
        return None, problems

    if normalized_status != BOSS_STATUS_PASSED.casefold():
        # A recorded failure is legitimate information, not an error.
        return None, problems

    return boss, problems


# ---------------------------------------------------------------------------
# What a view needs
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ProgressView:
    """One snapshot's position on the curve, ready to render.

    ``percent`` is always between 0 and 100. The coach's own bar can legitimately
    read past its threshold for a moment — that is what cumulative counting
    looks like just before a level-up is reflected — and a bar drawn at 104%
    would spill out of its container.
    """

    level: int
    rank: str
    tier_numeral: Optional[str]
    tier_name: Optional[str]
    cumulative: bool
    total_xp: Optional[int]
    span_start: Optional[int]
    span_end: Optional[int]
    percent: int
    at_max: bool


def progress_view(level: int, level_xp: int, level_xp_cap: int,
                  cumulative: bool) -> ProgressView:
    """Build a bounded progress view for either SAVE schema."""
    row = _BY_LEVEL.get(level)
    tier = tier_for_level(level)
    rank = row.rank if row else ""

    if cumulative:
        start = row.threshold if row and row.threshold is not None else 0
        end = row.next_threshold if row else None
        total = level_xp
        if end is None or end <= start:
            # Level 30 has no upper bound. A full bar is the honest picture:
            # the ceiling has been reached and XP simply keeps accumulating.
            return ProgressView(level, rank, tier.numeral if tier else None,
                                tier.name if tier else None, True, total,
                                start, None, 100, True)
        span = end - start
        done = max(0, min(total - start, span))
        percent = int(done * 100 / span) if span else 0
    else:
        start, end, total = 0, level_xp_cap, None
        if level_xp_cap <= 0:
            percent = 0
        else:
            percent = int(max(0, min(level_xp, level_xp_cap)) * 100 / level_xp_cap)

    return ProgressView(
        level=level,
        rank=rank,
        tier_numeral=tier.numeral if tier else None,
        tier_name=tier.name if tier else None,
        cumulative=cumulative,
        total_xp=total,
        span_start=start,
        span_end=end,
        percent=max(0, min(100, percent)),
        at_max=level >= MAX_LEVEL,
    )
