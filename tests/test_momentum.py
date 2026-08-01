"""Tests for streak and momentum calculation — all database-free."""

from datetime import date, timedelta

import pytest

from app.momentum import (
    MOMENTUM_WEEKS,
    WEEK_WEIGHTS,
    WeekOutcome,
    calculate_momentum,
    calculate_streak,
    completed_week_starts,
    explain_momentum,
    week_end,
    week_start,
)


def W(offset_weeks: int, achieved: int, target: int = 3, **kw) -> WeekOutcome:
    """A week `offset_weeks` before a fixed reference Monday."""
    monday = date(2026, 7, 27) - timedelta(weeks=offset_weeks)
    return WeekOutcome(week_start=monday, achieved=achieved, target=target, **kw)


# ---------------------------------------------------------------------------
# Calendar weeks
# ---------------------------------------------------------------------------

def test_week_starts_on_monday():
    # 2026-07-29 is a Wednesday
    assert week_start(date(2026, 7, 29)) == date(2026, 7, 27)
    assert week_start(date(2026, 7, 27)) == date(2026, 7, 27)


def test_week_ends_on_sunday():
    assert week_end(date(2026, 7, 29)) == date(2026, 8, 2)


def test_sunday_belongs_to_the_week_that_started_monday():
    sunday = date(2026, 8, 2)
    assert week_start(sunday) == date(2026, 7, 27)


def test_monday_starts_a_new_week():
    assert week_start(date(2026, 8, 3)) == date(2026, 8, 3)


def test_week_boundaries_across_a_year_change():
    # 2027-01-01 is a Friday; its week started Mon 2026-12-28
    assert week_start(date(2027, 1, 1)) == date(2026, 12, 28)


@pytest.mark.parametrize("day", [
    date(2026, 3, 29),   # DST starts in Europe/Berlin (clocks forward)
    date(2026, 3, 30),
    date(2026, 10, 25),  # DST ends (clocks back)
    date(2026, 10, 26),
])
def test_dst_transitions_do_not_shift_week_boundaries(day):
    """A week is seven calendar days regardless of a 23- or 25-hour Sunday."""
    start = week_start(day)
    assert start.weekday() == 0
    assert week_end(day) - start == timedelta(days=6)


def test_completed_weeks_exclude_the_running_week():
    today = date(2026, 7, 29)          # Wednesday
    starts = completed_week_starts(today)
    assert date(2026, 7, 27) not in starts       # the running week
    assert starts[0] == date(2026, 7, 20)        # last completed week
    assert len(starts) == MOMENTUM_WEEKS


def test_completed_weeks_are_newest_first():
    starts = completed_week_starts(date(2026, 7, 29))
    assert starts == sorted(starts, reverse=True)


# ---------------------------------------------------------------------------
# Fulfilment
# ---------------------------------------------------------------------------

def test_fulfilment_is_a_percentage():
    assert W(1, 0).fulfilment == 0
    assert W(1, 1).fulfilment == 33
    assert W(1, 3).fulfilment == 100


def test_over_fulfilment_is_capped_at_100():
    """Doing six of three cannot compensate for another week."""
    assert W(1, 6).fulfilment == 100
    assert W(1, 100).fulfilment == 100


def test_no_target_is_not_scorable():
    week = W(1, 5, target=0)
    assert week.fulfilment == 0
    assert not week.scorable


def test_satisfied_requires_reaching_the_target():
    assert not W(1, 2).satisfied
    assert W(1, 3).satisfied
    assert W(1, 4).satisfied


# ---------------------------------------------------------------------------
# Momentum
# ---------------------------------------------------------------------------

def test_all_weeks_full_gives_100():
    outcomes = [W(n, 3) for n in range(1, 5)]
    assert calculate_momentum(outcomes).value == 100


def test_all_weeks_empty_gives_0():
    outcomes = [W(n, 0) for n in range(1, 5)]
    assert calculate_momentum(outcomes).value == 0


def test_weights_are_applied_in_order():
    """Only the most recent week full: 40 % of the total weight."""
    outcomes = [W(1, 3), W(2, 0), W(3, 0), W(4, 0)]
    assert calculate_momentum(outcomes).value == 40

    outcomes = [W(1, 0), W(2, 0), W(3, 0), W(4, 3)]
    assert calculate_momentum(outcomes).value == 10


def test_a_single_missed_week_does_not_reset_momentum():
    """The whole point: one bad week must not read as a total loss."""
    outcomes = [W(1, 0), W(2, 3), W(3, 3), W(4, 3)]
    result = calculate_momentum(outcomes)
    assert result.value == 60     # 30 + 20 + 10
    assert result.value > 0


def test_partial_weeks_are_averaged():
    outcomes = [W(1, 3), W(2, 3), W(3, 0), W(4, 0)]
    assert calculate_momentum(outcomes).value == 70    # 40 + 30


def test_paused_weeks_are_removed_not_counted_as_zero(db=None):
    """A deliberate break must neither help nor hurt."""
    outcomes = [W(1, 3), W(2, 0, paused=True), W(3, 3), W(4, 3)]
    result = calculate_momentum(outcomes)
    # 30 % is removed; the remaining 70 % are all fully satisfied
    assert result.value == 100
    assert result.counted_weeks == 3


def test_a_pause_cannot_lower_a_perfect_run():
    full = calculate_momentum([W(n, 3) for n in range(1, 5)]).value
    paused = calculate_momentum(
        [W(1, 3), W(2, 0, paused=True), W(3, 3), W(4, 3)]
    ).value
    assert paused == full == 100


def test_a_pause_cannot_raise_a_bad_run():
    outcomes = [W(1, 0), W(2, 0, paused=True), W(3, 0), W(4, 0)]
    assert calculate_momentum(outcomes).value == 0


def test_weeks_without_data_are_neutral():
    outcomes = [W(1, 3), W(2, 0, has_data=False), W(3, 3), W(4, 3)]
    result = calculate_momentum(outcomes)
    assert result.value == 100
    assert result.counted_weeks == 3


def test_no_scorable_week_yields_none_not_zero():
    """"Not enough data yet" must not look like "you failed"."""
    outcomes = [W(n, 0, has_data=False) for n in range(1, 5)]
    result = calculate_momentum(outcomes)
    assert result.value is None
    assert not result.has_value


def test_entirely_paused_history_yields_none():
    outcomes = [W(n, 0, paused=True) for n in range(1, 5)]
    assert calculate_momentum(outcomes).value is None


def test_fewer_weeks_than_the_window_still_works():
    """A goal created two weeks ago has only two completed weeks."""
    result = calculate_momentum([W(1, 3), W(2, 3)])
    assert result.value == 100
    assert result.counted_weeks == 2


def test_empty_history_yields_none():
    assert calculate_momentum([]).value is None


def test_result_is_always_within_range():
    for achieved in (0, 1, 2, 3, 99):
        value = calculate_momentum([W(n, achieved) for n in range(1, 5)]).value
        assert value is None or 0 <= value <= 100


def test_contributions_explain_every_week():
    outcomes = [W(1, 3), W(2, 0, paused=True), W(3, 0, has_data=False), W(4, 3)]
    contributions = calculate_momentum(outcomes).contributions
    assert len(contributions) == MOMENTUM_WEEKS
    assert [c.weight for c in contributions] == list(WEEK_WEIGHTS)
    assert contributions[1].reason == "pausiert"
    assert contributions[2].reason == "keine Daten"
    assert contributions[0].counted and not contributions[1].counted


def test_the_formula_is_explainable():
    lines = explain_momentum()
    assert any("40" in line for line in lines)
    assert any("laufende Woche" in line for line in lines)
    assert any("pausiert" in line.lower() for line in lines)


# ---------------------------------------------------------------------------
# Streaks
# ---------------------------------------------------------------------------

def test_streak_counts_consecutive_satisfied_weeks():
    outcomes = [W(1, 3), W(2, 3), W(3, 3), W(4, 0)]
    assert calculate_streak(outcomes).current == 3


def test_an_unsatisfied_completed_week_ends_the_streak():
    outcomes = [W(1, 2), W(2, 3), W(3, 3)]
    assert calculate_streak(outcomes).current == 0


def test_the_running_week_counts_only_when_already_satisfied():
    completed = [W(1, 3), W(2, 3)]
    assert calculate_streak(completed, current_week=W(0, 1)).current == 2
    assert calculate_streak(completed, current_week=W(0, 3)).current == 3


def test_an_unfinished_running_week_never_ends_a_streak():
    completed = [W(1, 3), W(2, 3), W(3, 3)]
    assert calculate_streak(completed, current_week=W(0, 0)).current == 3


def test_paused_weeks_do_not_break_a_streak():
    outcomes = [W(1, 3), W(2, 0, paused=True), W(3, 3), W(4, 3)]
    assert calculate_streak(outcomes).current == 3


def test_paused_weeks_do_not_extend_a_streak_either():
    outcomes = [W(1, 0, paused=True), W(2, 0, paused=True)]
    assert calculate_streak(outcomes).current == 0


def test_best_streak_survives_a_later_break():
    # newest first: broke recently, but had a run of three before
    outcomes = [W(1, 0), W(2, 3), W(3, 3), W(4, 3)]
    result = calculate_streak(outcomes)
    assert result.current == 0
    assert result.best == 3


def test_best_is_never_below_current():
    outcomes = [W(1, 3), W(2, 3)]
    result = calculate_streak(outcomes)
    assert result.best >= result.current


def test_streak_on_empty_history():
    result = calculate_streak([])
    assert result.current == 0
    assert result.best == 0


def test_no_negative_values_anywhere():
    outcomes = [W(1, 0), W(2, 0), W(3, 0)]
    result = calculate_streak(outcomes)
    assert result.current >= 0 and result.best >= 0
    momentum = calculate_momentum(outcomes)
    assert momentum.value is None or momentum.value >= 0


# ---------------------------------------------------------------------------
# The module must stay pure
# ---------------------------------------------------------------------------

def test_momentum_module_imports_nothing_stateful():
    """No database, no FastAPI, no clock of its own — like app/xp.py."""
    import ast
    from pathlib import Path

    source = Path(__file__).resolve().parent.parent / "app" / "momentum.py"
    tree = ast.parse(source.read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    for forbidden in ("sqlalchemy", "fastapi", "app"):
        assert forbidden not in imported, f"app/momentum.py must not import {forbidden}"


def test_momentum_module_never_reads_the_clock():
    """Every date comes in as an argument, so tests can pin any week.

    Checked on the syntax tree, not on the text: the docstring may well name
    `quests.app_today` as the place where "today" is decided elsewhere.
    """
    import ast
    from pathlib import Path

    source = Path(__file__).resolve().parent.parent / "app" / "momentum.py"
    tree = ast.parse(source.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            assert name not in ("today", "now", "utcnow", "app_today"), (
                f"app/momentum.py must not call {name}()"
            )


def test_the_pause_rule_is_stated_in_the_explanation():
    lines = " ".join(explain_momentum())
    assert "Pausenzeiträume" in lines
    assert "nicht rückwirkend" in lines
    assert "0 %" in lines
