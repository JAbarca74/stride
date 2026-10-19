"""Data-quality validations (SPEC section 5).

Checks 1-3 are blocking: if any fails, the run stops and the system does not trade
that day. Check 4 (two-source agreement) only raises an alert.

All functions are pure: the caller supplies the prices and the expected NYSE days.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable
from dataclasses import dataclass
from datetime import date

import pandas as pd

MISSING_DAYS_LOOKBACK = 300
MAX_DAILY_CHANGE = 0.25
SOURCE_CLOSE_TOLERANCE = 0.005

NO_MISSING_DAYS = "NO_MISSING_DAYS"
MAX_DAILY_CHANGE_CHECK = "MAX_DAILY_CHANGE"
LATEST_BAR = "LATEST_BAR"
SOURCES_AGREE = "SOURCES_AGREE"

_MAX_SHOWN = 5  # dates quoted in a failure message


class DataQualityError(Exception):
    """A blocking data-quality check failed; the system must not trade today."""


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    detail: str = ""
    blocking: bool = True


def _dates(values: pd.Index | Collection[date]) -> pd.DatetimeIndex:
    return pd.DatetimeIndex(list(values)).normalize()


def _show(days: pd.DatetimeIndex) -> str:
    shown = ", ".join(day.date().isoformat() for day in days[:_MAX_SHOWN])
    extra = len(days) - _MAX_SHOWN
    return f"{shown} (+{extra} more)" if extra > 0 else shown


def check_no_missing_days(
    prices: pd.DataFrame,
    expected_days: pd.DatetimeIndex,
    lookback: int = MISSING_DAYS_LOOKBACK,
) -> CheckResult:
    """Check 1: every one of the last `lookback` expected trading days has a bar."""
    window = _dates(expected_days).sort_values()[-lookback:]
    missing = window.difference(_dates(prices.index))
    if len(missing) == 0:
        return CheckResult(NO_MISSING_DAYS, True)
    return CheckResult(NO_MISSING_DAYS, False, f"{len(missing)} missing days: {_show(missing)}")


def check_daily_changes(
    prices: pd.DataFrame,
    flagged_dates: Collection[date] = (),
    max_change: float = MAX_DAILY_CHANGE,
) -> CheckResult:
    """Check 2: no close-to-close change above `max_change` unless the day is flagged."""
    close = prices["close"].copy()
    close.index = _dates(close.index)
    close = close.sort_index()
    change = close.pct_change().abs()
    is_jump = (change > max_change).to_numpy() & ~close.index.isin(_dates(flagged_dates))
    jumps = pd.DatetimeIndex(close.index[is_jump])
    if len(jumps) == 0:
        return CheckResult(MAX_DAILY_CHANGE_CHECK, True)
    return CheckResult(
        MAX_DAILY_CHANGE_CHECK,
        False,
        f"{len(jumps)} unflagged moves above {max_change:.0%}: {_show(jumps)}",
    )


def check_latest_bar(prices: pd.DataFrame, expected_latest: date) -> CheckResult:
    """Check 3: the most recent bar is dated `expected_latest`."""
    if prices.empty:
        return CheckResult(LATEST_BAR, False, "no bars")
    latest = pd.Timestamp(prices.index.max()).date()
    if latest == expected_latest:
        return CheckResult(LATEST_BAR, True)
    return CheckResult(LATEST_BAR, False, f"latest bar is {latest}, expected {expected_latest}")


def check_sources_agree(
    primary: pd.DataFrame,
    secondary: pd.DataFrame,
    sample_dates: Collection[date] | None = None,
    tolerance: float = SOURCE_CLOSE_TOLERANCE,
) -> CheckResult:
    """Check 4 (alert only): two sources agree on the close within `tolerance`."""
    a = primary["close"].copy()
    b = secondary["close"].copy()
    a.index = _dates(a.index)
    b.index = _dates(b.index)
    common = a.index.intersection(b.index)
    if sample_dates is not None:
        common = common.intersection(_dates(sample_dates))
    if len(common) == 0:
        return CheckResult(SOURCES_AGREE, False, "no common dates to compare", blocking=False)
    diff = ((a[common] - b[common]).abs() / b[common]).sort_index()
    bad = diff[diff > tolerance]
    if bad.empty:
        return CheckResult(SOURCES_AGREE, True, blocking=False)
    bad_days = pd.DatetimeIndex(bad.index)[:_MAX_SHOWN]
    shown = ", ".join(
        f"{day.date()} ({value:.2%})"
        for day, value in zip(bad_days, bad.to_numpy()[:_MAX_SHOWN], strict=True)
    )
    return CheckResult(
        SOURCES_AGREE,
        False,
        f"{len(bad)} of {len(common)} closes differ by more than {tolerance:.1%}: {shown}",
        blocking=False,
    )


def validate(
    prices: pd.DataFrame,
    expected_days: pd.DatetimeIndex,
    expected_latest: date,
    flagged_dates: Collection[date] = (),
    secondary: pd.DataFrame | None = None,
) -> list[CheckResult]:
    """Run all section 5 checks on one symbol's prices."""
    results = [
        check_no_missing_days(prices, expected_days),
        check_daily_changes(prices, flagged_dates),
        check_latest_bar(prices, expected_latest),
    ]
    if secondary is not None:
        results.append(check_sources_agree(prices, secondary))
    return results


def raise_on_blocking_failure(results: Iterable[CheckResult]) -> None:
    """Raise `DataQualityError` if any blocking check failed."""
    failed = [r for r in results if r.blocking and not r.passed]
    if failed:
        raise DataQualityError("; ".join(f"{r.name}: {r.detail}" for r in failed))
