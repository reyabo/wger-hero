"""
Streaks and momentum — two deliberately different answers to "how is it going".

A **streak** is strict: consecutive fully satisfied periods. It is the one that
can end.

**Momentum** is forgiving on purpose. A single missed week must not reset it to
zero, because that turns one bad week into a reason to give up entirely. It is
a weighted average over the last four *completed* calendar weeks:

    last week          40 %
    the week before    30 %
    two weeks before   20 %
    three weeks before 10 %

Every week contributes its fulfilment capped at 100 % — over-delivering in one
week cannot paper over another. The result is 0–100.

Rules that keep this from ever feeling punitive:

* The **running week is never scored**. It cannot be a failure yet, so it is
  simply not part of the calculation.
* **Paused weeks are not failures.** They are removed from the calculation and
  the remaining weights are renormalized, so a break neither helps nor hurts.
* **Missing history is not a failure either.** Weeks with no data are treated
  the same way and reported as such, rather than silently counting as zero.
* If nothing is left to score, momentum is ``None`` — "not enough data yet" —
  not 0.

Everything here is pure: dates and numbers in, numbers out. No database, no
request, no clock of its own — the caller passes ``today`` so the timezone
decision stays in one place (``quests.app_today``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional, Sequence

# Weights for the last four completed weeks, most recent first. Public so the
# UI can render the same numbers it explains.
WEEK_WEIGHTS: tuple[int, ...] = (40, 30, 20, 10)
MOMENTUM_WEEKS = len(WEEK_WEIGHTS)


def week_start(day: date) -> date:
    """Monday of the calendar week `day` falls into.

    Pure date arithmetic, so daylight saving transitions cannot shift a week
    boundary: a week is seven calendar days regardless of how many hours the
    Sunday had.
    """
    return day - timedelta(days=day.weekday())


def week_end(day: date) -> date:
    """Sunday of the calendar week `day` falls into."""
    return week_start(day) + timedelta(days=6)


def completed_week_starts(today: date, count: int = MOMENTUM_WEEKS) -> list[date]:
    """The `count` most recent *completed* weeks, newest first.

    The week containing `today` is excluded — it is still running.
    """
    current = week_start(today)
    return [current - timedelta(weeks=n) for n in range(1, count + 1)]


@dataclass(frozen=True)
class WeekOutcome:
    """What one calendar week achieved against its target."""

    week_start: date
    achieved: int = 0
    target: int = 0
    #: The goal was paused during this week — not scored, not a failure.
    paused: bool = False
    #: No record exists for this week (e.g. before the goal existed).
    has_data: bool = True

    @property
    def scorable(self) -> bool:
        return self.has_data and not self.paused and self.target > 0

    @property
    def fulfilment(self) -> int:
        """Percent of the target reached, capped at 100."""
        if self.target <= 0:
            return 0
        return min(100, max(0, round(self.achieved / self.target * 100)))

    @property
    def satisfied(self) -> bool:
        """Whether the week fully met its target."""
        return self.target > 0 and self.achieved >= self.target


@dataclass(frozen=True)
class WeekContribution:
    """One week's share of the momentum result, for the UI to explain."""

    week_start: date
    weight: int
    fulfilment: int
    counted: bool
    reason: str = ""


@dataclass(frozen=True)
class MomentumResult:
    #: 0–100, or None when there is nothing to score yet.
    value: Optional[int]
    contributions: list[WeekContribution]

    @property
    def has_value(self) -> bool:
        return self.value is not None

    @property
    def counted_weeks(self) -> int:
        return sum(1 for c in self.contributions if c.counted)


def calculate_momentum(outcomes: Sequence[WeekOutcome]) -> MomentumResult:
    """Weighted momentum over the given completed weeks, newest first.

    `outcomes` must already be the completed weeks in order, newest first —
    build them with :func:`completed_week_starts` so the running week is out.
    Weeks beyond the weight table are ignored.
    """
    contributions: list[WeekContribution] = []
    total_weight = 0
    weighted_sum = 0

    for index, weight in enumerate(WEEK_WEIGHTS):
        if index >= len(outcomes):
            contributions.append(
                WeekContribution(
                    week_start=date.min, weight=weight, fulfilment=0,
                    counted=False, reason="keine Daten",
                )
            )
            continue

        outcome = outcomes[index]
        if outcome.paused:
            reason = "pausiert"
        elif not outcome.has_data:
            reason = "keine Daten"
        elif outcome.target <= 0:
            reason = "kein Ziel gesetzt"
        else:
            reason = ""

        counted = outcome.scorable
        contributions.append(
            WeekContribution(
                week_start=outcome.week_start,
                weight=weight,
                fulfilment=outcome.fulfilment if counted else 0,
                counted=counted,
                reason=reason,
            )
        )
        if counted:
            total_weight += weight
            weighted_sum += weight * outcome.fulfilment

    if total_weight == 0:
        # Nothing scorable: paused throughout, or no history yet. Reporting 0
        # would read as failure, so report "unknown" instead.
        return MomentumResult(value=None, contributions=contributions)

    # Renormalize over the weeks that actually counted, so removing a paused
    # week neither rewards nor punishes.
    return MomentumResult(
        value=round(weighted_sum / total_weight),
        contributions=contributions,
    )


@dataclass(frozen=True)
class StreakResult:
    current: int
    best: int


def calculate_streak(
    outcomes: Sequence[WeekOutcome],
    current_week: Optional[WeekOutcome] = None,
) -> StreakResult:
    """Consecutive fully satisfied weeks.

    `outcomes` are completed weeks, newest first. `current_week` is the running
    week and counts **only** if it is already satisfied — an unfinished week
    never ends a streak.

    Paused weeks are skipped: they neither extend nor break a streak, so taking
    a deliberate break does not cost anything. Weeks without data do break it,
    because a gap is genuinely not a satisfied week — but see `has_data` if the
    caller wants to model "before this goal existed" as paused instead.
    """
    current = 0
    if current_week is not None and current_week.satisfied:
        current = 1

    for outcome in outcomes:
        if outcome.paused:
            continue  # a break is not a failure
        if outcome.satisfied:
            current += 1
        else:
            break

    # Best streak over the whole history, applying the same pause rule.
    best = 0
    run = 0
    for outcome in reversed(list(outcomes)):
        if outcome.paused:
            continue
        if outcome.satisfied:
            run += 1
            best = max(best, run)
        else:
            run = 0
    if current_week is not None and current_week.satisfied:
        run += 1
        best = max(best, run)

    return StreakResult(current=current, best=max(best, current))


def explain_momentum() -> list[str]:
    """The formula in plain German, for the UI to show next to the number."""
    labels = ["Letzte Woche", "Woche davor", "Zwei Wochen davor", "Drei Wochen davor"]
    lines = [f"{label}: {weight} %" for label, weight in zip(labels, WEEK_WEIGHTS)]
    lines.append(
        "Je Woche zählt der Erfüllungsgrad, gedeckelt auf 100 %. "
        "Die laufende Woche wird nicht bewertet. "
        "Pausierte Wochen und Wochen ohne Daten werden herausgerechnet, "
        "nicht als Fehlschlag gezählt."
    )
    return lines
