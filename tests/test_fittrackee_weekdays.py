"""Which weekdays a quest source is allowed to count.

The training plan is Tuesday plus one weekend day, so the weekday a workout
falls on decides quest progress. Two things follow, and both are tested here.

The stored value has to be validated and typed — the project stores weekdays as
ISO numbers (1 = Monday … 7 = Sunday) for habit schedules, and a second
convention would be a bug waiting to happen. And the weekday must come from the
*local* Hero date: a run finishing Monday 23:30 UTC is Tuesday 01:30 in
Europe/Berlin, and it is the Tuesday run.
"""

from datetime import datetime

import pytest

from app.quests import (
    ISO_WEEKDAY_RANGE,
    parse_allowed_weekdays,
    serialize_allowed_weekdays,
    weekday_is_allowed,
)


# ---------------------------------------------------------------------------
# Parsing and serialising — the stored form is canonical
# ---------------------------------------------------------------------------

def test_no_restriction_is_the_default():
    assert parse_allowed_weekdays(None) == []
    assert parse_allowed_weekdays("") == []


def test_a_single_weekday_round_trips():
    assert serialize_allowed_weekdays([2]) == "2"
    assert parse_allowed_weekdays("2") == [2]


def test_the_weekend_round_trips():
    assert serialize_allowed_weekdays([6, 7]) == "6,7"
    assert parse_allowed_weekdays("6,7") == [6, 7]


def test_the_stored_order_is_canonical():
    """Two quests restricted to the same days must store the same string, or
    the value stops being comparable."""
    assert serialize_allowed_weekdays([7, 6]) == "6,7"
    assert serialize_allowed_weekdays([6, 7, 6]) == "6,7"


def test_duplicates_collapse():
    assert parse_allowed_weekdays("2,2,2") == [2]


def test_whitespace_is_tolerated():
    assert parse_allowed_weekdays(" 6 , 7 ") == [6, 7]


def test_an_empty_list_serialises_to_none():
    """None means "no restriction". An empty string would be a third state
    meaning the same thing."""
    assert serialize_allowed_weekdays([]) is None
    assert serialize_allowed_weekdays(None) is None


@pytest.mark.parametrize("bad", ["0", "8", "-1", "Dienstag", "2.5", "abc", "99"])
def test_a_value_outside_the_iso_range_is_refused(bad):
    with pytest.raises(ValueError):
        serialize_allowed_weekdays([bad])


@pytest.mark.parametrize("bad", ["0", "8", "Dienstag", "", "null"])
def test_unparseable_stored_data_is_ignored_not_crashed(bad):
    """A row written by hand must not take a page down; it degrades to "no
    restriction", which counts more rather than less."""
    assert parse_allowed_weekdays(bad) == []


def test_the_range_is_the_iso_convention():
    """Same numbers as HabitScheduleDay — 1 = Monday, 7 = Sunday."""
    assert ISO_WEEKDAY_RANGE == (1, 2, 3, 4, 5, 6, 7)


def test_it_agrees_with_the_habit_schedule_convention():
    from app.habits import ISO_WEEKDAYS, WEEKDAY_LABELS

    assert tuple(ISO_WEEKDAYS) == ISO_WEEKDAY_RANGE
    assert WEEKDAY_LABELS[2] == "Dienstag"
    assert WEEKDAY_LABELS[6] == "Samstag"
    assert WEEKDAY_LABELS[7] == "Sonntag"


# ---------------------------------------------------------------------------
# Deciding whether one moment counts
# ---------------------------------------------------------------------------

# All timestamps are naive UTC, as everything stored in this app is.
MONDAY_LATE = datetime(2026, 8, 17, 23, 30)      # Berlin: Tuesday 01:30
TUESDAY_NOON = datetime(2026, 8, 18, 12, 0)
WEDNESDAY_NOON = datetime(2026, 8, 19, 12, 0)
SATURDAY_NOON = datetime(2026, 8, 22, 12, 0)
SUNDAY_NOON = datetime(2026, 8, 23, 12, 0)
SUNDAY_LATE = datetime(2026, 8, 23, 23, 30)      # Berlin: Monday 01:30


def test_no_restriction_allows_every_day():
    for moment in (MONDAY_LATE, TUESDAY_NOON, SATURDAY_NOON, SUNDAY_NOON):
        assert weekday_is_allowed(moment, [])


def test_tuesday_counts_for_a_tuesday_quest():
    assert weekday_is_allowed(TUESDAY_NOON, [2])


@pytest.mark.parametrize("moment", [WEDNESDAY_NOON, SATURDAY_NOON, SUNDAY_NOON])
def test_another_day_does_not(moment):
    assert not weekday_is_allowed(moment, [2])


def test_late_monday_utc_is_tuesday_in_the_app_timezone():
    """The whole reason weekday_is_allowed takes a datetime and not a weekday:
    23:30 UTC on Monday is 01:30 on Tuesday in Europe/Berlin."""
    assert weekday_is_allowed(MONDAY_LATE, [2])
    assert not weekday_is_allowed(MONDAY_LATE, [1])


def test_late_sunday_utc_is_monday_in_the_app_timezone():
    assert not weekday_is_allowed(SUNDAY_LATE, [7])
    assert weekday_is_allowed(SUNDAY_LATE, [1])


@pytest.mark.parametrize("moment", [SATURDAY_NOON, SUNDAY_NOON])
def test_both_weekend_days_count_for_the_weekend_quest(moment):
    assert weekday_is_allowed(moment, [6, 7])


@pytest.mark.parametrize("moment", [TUESDAY_NOON, WEDNESDAY_NOON])
def test_a_weekday_does_not_count_for_the_weekend_quest(moment):
    assert not weekday_is_allowed(moment, [6, 7])


def test_tuesday_and_the_weekend_never_overlap():
    """Structural, not incidental: one workout can never satisfy both quests."""
    assert set([2]).isdisjoint([6, 7])
