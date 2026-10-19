from __future__ import annotations

from datetime import date

import pytest

from stride.data.calendar import (
    is_trading_day,
    next_trading_day,
    previous_trading_day,
    trading_days,
)


def test_thanksgiving_2026_is_not_a_trading_day() -> None:
    assert is_trading_day(date(2026, 11, 26)) is False


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        (date(2026, 11, 25), True),  # Wednesday before Thanksgiving
        (date(2026, 11, 27), True),  # Black Friday, early close still counts
        (date(2026, 11, 28), False),  # Saturday
        (date(2026, 11, 29), False),  # Sunday
        (date(2026, 12, 25), False),  # Christmas
        (date(2027, 1, 1), False),  # New Year's Day
        (date(2026, 7, 3), False),  # Independence Day observed (July 4 is a Saturday)
        (date(2026, 10, 19), True),  # ordinary Monday
    ],
)
def test_is_trading_day(day: date, expected: bool) -> None:
    assert is_trading_day(day) is expected


def test_trading_days_are_inclusive_and_timezone_naive() -> None:
    days = trading_days(date(2026, 11, 23), date(2026, 11, 30))
    assert [d.date() for d in days] == [
        date(2026, 11, 23),
        date(2026, 11, 24),
        date(2026, 11, 25),
        date(2026, 11, 27),
        date(2026, 11, 30),
    ]
    assert days.tz is None


def test_trading_days_with_reversed_range_is_empty() -> None:
    assert len(trading_days(date(2026, 11, 30), date(2026, 11, 23))) == 0


def test_previous_trading_day_skips_holiday_and_weekend() -> None:
    assert previous_trading_day(date(2026, 11, 27)) == date(2026, 11, 25)
    assert previous_trading_day(date(2026, 11, 30)) == date(2026, 11, 27)


def test_next_trading_day_skips_holiday_and_weekend() -> None:
    assert next_trading_day(date(2026, 11, 25)) == date(2026, 11, 27)
    assert next_trading_day(date(2026, 12, 24)) == date(2026, 12, 28)
