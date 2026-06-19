"""XP rules and level progression formula."""

from dataclasses import dataclass


def xp_for_next_level(level: int) -> int:
    """XP required to advance from `level` to `level + 1`."""
    return 1000 + level * 250


def level_from_total_xp(total_xp: int) -> tuple[int, int, int]:
    """Return (level, xp_within_current_level, xp_needed_for_next_level)."""
    level = 1
    accumulated = 0
    while True:
        needed = xp_for_next_level(level)
        if accumulated + needed > total_xp:
            break
        accumulated += needed
        level += 1
    xp_in_level = total_xp - accumulated
    xp_needed = xp_for_next_level(level)
    return level, xp_in_level, xp_needed


def recalc_level(total_xp: int) -> int:
    """Return the character level for a given total XP (drops xp-in-level info)."""
    level, _, _ = level_from_total_xp(total_xp)
    return level


@dataclass
class XpAward:
    event_type: str
    xp: int
    attribute: str
    title: str
    description: str


CONDITIONING_KEYWORDS = {
    "bear crawl",
    "broad jump",
    "pogo jump",
    "mountain climber",
    "skater",
    "hollow body",
    "plank",
    "carry",
    "sprint",
    "burpee",
    "jump",
}


def calculate_xp_awards(workout) -> list[XpAward]:
    """
    Calculate XP awards for a NormalizedWorkoutLog.
    Returns a list of XpAward objects (may be empty only if invalid).
    """
    from app.sync import NormalizedWorkoutLog  # local import to avoid circular

    awards: list[XpAward] = []

    label = workout.title or workout.routine_name or "Workout"

    # Base completion award
    awards.append(
        XpAward(
            event_type="workout_complete",
            xp=100,
            attribute="Strength",
            title=label,
            description=f"Completed workout on {workout.date}",
        )
    )

    # Conditioning bonus
    exercise_names = {e.name.lower() for e in workout.exercises}
    if any(
        any(kw in name for kw in CONDITIONING_KEYWORDS)
        for name in exercise_names
    ):
        awards.append(
            XpAward(
                event_type="conditioning_bonus",
                xp=25,
                attribute="Conditioning",
                title="Conditioning Bonus",
                description="Conditioning or finisher exercise detected",
            )
        )

    # Honest RIR bonus
    if any(e.rir is not None for e in workout.exercises):
        awards.append(
            XpAward(
                event_type="rir_logged",
                xp=10,
                attribute="Mindfulness",
                title="Honest RIR Logged",
                description="RIR (Reps In Reserve) was recorded",
            )
        )

    return awards
