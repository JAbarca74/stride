"""NYSE trading-day calendar (SPEC sections 2 and 5)."""

from __future__ import annotations

from datetime import date, timedelta
from functools import cache

import pandas as pd
import pandas_market_calendars as mcal

# Long enough to step over any run of NYSE closures (weekends plus holidays).
_SEARCH_WINDOW = timedelta(days=14)


@cache
def _nyse() -> mcal.MarketCalendar:
    return mcal.get_calendar("NYSE")


def trading_days(start: date, end: date) -> pd.DatetimeIndex:
    """NYSE trading days between `start` and `end`, both inclusive, timezone-naive."""
    if end < start:
        return pd.DatetimeIndex([])
    days = _nyse().valid_days(start_date=start, end_date=end)
    return pd.DatetimeIndex(days.tz_localize(None).normalize())


def is_trading_day(day: date) -> bool:
    """Whether NYSE is open (full or early close) on `day`."""
    return len(trading_days(day, day)) == 1


def previous_trading_day(day: date) -> date:
    """The last NYSE trading day strictly before `day`."""
    days = trading_days(day - _SEARCH_WINDOW, day - timedelta(days=1))
    return days[-1].date()


def next_trading_day(day: date) -> date:
    """The first NYSE trading day strictly after `day`."""
    days = trading_days(day + timedelta(days=1), day + _SEARCH_WINDOW)
    return days[0].date()


def sessions(start: date, end: date) -> pd.DataFrame:
    """NYSE sessions between `start` and `end`, inclusive.

    Indexed by trading day (timezone-naive); columns `market_open` and `market_close`
    are UTC timestamps, so early closes (e.g. 13:00 ET) and daylight saving are exact.
    """
    if end < start:
        return pd.DataFrame(
            {
                "market_open": pd.Series(dtype="datetime64[ns, UTC]"),
                "market_close": pd.Series(dtype="datetime64[ns, UTC]"),
            }
        )
    schedule = _nyse().schedule(start_date=start, end_date=end)
    frame = pd.DataFrame(
        {"market_open": schedule["market_open"], "market_close": schedule["market_close"]}
    )
    frame.index = pd.DatetimeIndex(frame.index).normalize()
    return frame
