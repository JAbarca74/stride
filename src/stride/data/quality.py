"""Data-quality validations (SPEC section 5).

Validations 1 and 2 stop the run if any fails: the system does not trade that day.
Validation 3 acts per symbol: if the primary source lacks bar t, that symbol is
blocked for the day (position kept, no orders); if it lacks bar t for every
symbol, the whole run stops. Validation 4 never stops the run: a mismatch in the
historical sample, or a secondary source without bar t, is an alert, and a
mismatch on the latest bar blocks orders for that symbol only.

Thresholds come from `config.yaml` (`data.quality`). Apart from the NYSE calendar,
the functions are pure: the caller supplies prices and the evaluation day.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal

import pandas as pd

from stride.config import DataQualityConfig
from stride.data.calendar import trading_days

NO_MISSING_DAYS = "NO_MISSING_DAYS"
MAX_DAILY_CHANGE = "MAX_DAILY_CHANGE"
LATEST_BAR = "LATEST_BAR"
SOURCES_AGREE = "SOURCES_AGREE"
LATEST_SOURCES_AGREE = "LATEST_SOURCES_AGREE"

Severity = Literal["run", "symbol", "alert"]
STOP_RUN: Severity = "run"
BLOCK_SYMBOL: Severity = "symbol"
ALERT: Severity = "alert"

_MAX_SHOWN = 5  # dates quoted in a failure message
_CALENDAR_DAYS_PER_TRADING_DAY = 1.6  # generous: about 1.45 in practice


class DataQualityError(Exception):
    """A run-stopping data-quality check failed; the system must not trade today."""


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    detail: str = ""
    severity: Severity = STOP_RUN
    symbol: str | None = None


def _dates(values: pd.Index | Collection[date]) -> pd.DatetimeIndex:
    return pd.DatetimeIndex(list(values)).normalize()


def _show(days: pd.DatetimeIndex) -> str:
    shown = ", ".join(day.date().isoformat() for day in days[:_MAX_SHOWN])
    extra = len(days) - _MAX_SHOWN
    return f"{shown} (+{extra} more)" if extra > 0 else shown


def expected_trading_days(evaluation_date: date, lookback: int) -> pd.DatetimeIndex:
    """The last `lookback` NYSE trading days up to and including `evaluation_date`."""
    span = timedelta(days=int(lookback * _CALENDAR_DAYS_PER_TRADING_DAY) + 10)
    return trading_days(evaluation_date - span, evaluation_date)[-lookback:]


def check_no_missing_days(
    prices: pd.DataFrame, expected_days: pd.DatetimeIndex, lookback: int
) -> CheckResult:
    """Validation 1: no missing bar in the last `lookback` expected trading days.

    For a symbol with less history, the window starts at its first bar.
    """
    if prices.empty:
        return CheckResult(NO_MISSING_DAYS, False, "no bars")
    window = _dates(expected_days).sort_values()[-lookback:]
    have = _dates(prices.index)
    window = window[window >= have.min()]
    missing = window.difference(have)
    if len(missing) == 0:
        return CheckResult(NO_MISSING_DAYS, True)
    return CheckResult(NO_MISSING_DAYS, False, f"{len(missing)} missing days: {_show(missing)}")


def check_daily_changes(
    prices: pd.DataFrame,
    max_change: float,
    flagged_dates: Collection[date] = (),
    window_start: date | None = None,
) -> CheckResult:
    """Validation 2: no close-to-close change above `max_change` unless the day is flagged.

    Only changes dated on or after `window_start` count (SPEC 1.2: the same window of
    the last 300 NYSE trading days as validation 1), so genuine moves deep in the past,
    such as early crypto history, do not stop today's run.
    """
    close = prices["close"].copy()
    close.index = _dates(close.index)
    close = close.sort_index()
    change = close.pct_change().abs()
    is_jump = (change > max_change).to_numpy() & ~close.index.isin(_dates(flagged_dates))
    if window_start is not None:
        is_jump &= close.index >= pd.Timestamp(window_start)
    jumps = pd.DatetimeIndex(close.index[is_jump])
    if len(jumps) == 0:
        return CheckResult(MAX_DAILY_CHANGE, True)
    return CheckResult(
        MAX_DAILY_CHANGE,
        False,
        f"{len(jumps)} unflagged moves above {max_change:.0%}: {_show(jumps)}",
    )


def check_latest_bar(prices: pd.DataFrame, evaluation_date: date) -> CheckResult:
    """Validation 3: the primary source has the bar of evaluation day t.

    A failure blocks this symbol only; `raise_on_blocking_failure` stops the run
    when every symbol fails it.
    """
    if prices.empty:
        return CheckResult(LATEST_BAR, False, "no bars", BLOCK_SYMBOL)
    latest = pd.Timestamp(prices.index.max()).date()
    if latest == evaluation_date:
        return CheckResult(LATEST_BAR, True, severity=BLOCK_SYMBOL)
    return CheckResult(
        LATEST_BAR, False, f"latest bar is {latest}, expected {evaluation_date}", BLOCK_SYMBOL
    )


def _relative_diff(primary: pd.Series, secondary: pd.Series) -> pd.Series:
    a = primary.copy()
    b = secondary.copy()
    a.index = _dates(a.index)
    b.index = _dates(b.index)
    common = a.index.intersection(b.index)
    return ((a[common] - b[common]).abs() / b[common]).sort_index()


def check_sample_closes_agree(
    primary_close: pd.Series,
    secondary_close: pd.Series,
    evaluation_date: date,
    tolerance: float,
) -> CheckResult:
    """Validation 4a (alert only): unadjusted closes before day t agree within `tolerance`."""
    diff = _relative_diff(primary_close, secondary_close)
    diff = diff[diff.index < pd.Timestamp(evaluation_date)]
    if diff.empty:
        return CheckResult(SOURCES_AGREE, False, "no common dates to compare", ALERT)
    bad = diff[diff > tolerance]
    if bad.empty:
        return CheckResult(SOURCES_AGREE, True, severity=ALERT)
    bad_days = pd.DatetimeIndex(bad.index)[:_MAX_SHOWN]
    shown = ", ".join(
        f"{day.date()} ({value:.2%})"
        for day, value in zip(bad_days, bad.to_numpy()[:_MAX_SHOWN], strict=True)
    )
    return CheckResult(
        SOURCES_AGREE,
        False,
        f"{len(bad)} of {len(diff)} closes differ by more than {tolerance:.1%}: {shown}",
        ALERT,
    )


def check_latest_close_agrees(
    primary_close: pd.Series,
    secondary_close: pd.Series,
    evaluation_date: date,
    max_diff: float,
) -> CheckResult:
    """Validation 4b: the unadjusted close of day t agrees within `max_diff`.

    A larger difference blocks orders for this symbol today. If either source has no
    bar for day t the comparison is impossible, which is reported as an alert.
    """
    diff = _relative_diff(primary_close, secondary_close)
    day = pd.Timestamp(evaluation_date)
    if day not in diff.index:
        return CheckResult(
            LATEST_SOURCES_AGREE, False, f"no bar for {evaluation_date} in both sources", ALERT
        )
    value = float(diff[day])
    if value <= max_diff:
        return CheckResult(LATEST_SOURCES_AGREE, True, severity=BLOCK_SYMBOL)
    return CheckResult(
        LATEST_SOURCES_AGREE,
        False,
        f"close of {evaluation_date} differs by {value:.2%} (limit {max_diff:.0%})",
        BLOCK_SYMBOL,
    )


def validate(
    symbol: str,
    prices: pd.DataFrame,
    evaluation_date: date,
    config: DataQualityConfig,
    *,
    flagged_dates: Collection[date] = (),
    primary_raw_close: pd.Series | None = None,
    secondary_raw_close: pd.Series | None = None,
) -> list[CheckResult]:
    """Run every section 5 validation on one symbol.

    `prices` are the stored (adjusted) bars. Validation 4 runs only when both
    unadjusted close series are given.
    """
    expected = expected_trading_days(evaluation_date, config.missing_days_lookback)
    # A missing bar for day t itself is validation 3's job (per symbol), not 1's.
    expected = expected[expected < pd.Timestamp(evaluation_date)]
    results = [
        check_no_missing_days(prices, expected, config.missing_days_lookback),
        check_daily_changes(
            prices,
            config.max_daily_change,
            flagged_dates,
            window_start=expected[0].date() if len(expected) else None,
        ),
        check_latest_bar(prices, evaluation_date),
    ]
    if primary_raw_close is not None and secondary_raw_close is not None:
        results += [
            check_sample_closes_agree(
                primary_raw_close,
                secondary_raw_close,
                evaluation_date,
                config.source_close_tolerance,
            ),
            check_latest_close_agrees(
                primary_raw_close,
                secondary_raw_close,
                evaluation_date,
                config.latest_bar_max_diff,
            ),
        ]
    return [CheckResult(r.name, r.passed, r.detail, r.severity, symbol) for r in results]


def raise_on_blocking_failure(results: Iterable[CheckResult]) -> None:
    """Raise `DataQualityError` if a run-stopping check failed, or if the primary
    source lacks bar t for every symbol."""
    results = list(results)
    failed = [r for r in results if r.severity == STOP_RUN and not r.passed]
    symbols = {r.symbol for r in results}
    missing_t = {r.symbol for r in results if r.name == LATEST_BAR and not r.passed}
    if symbols and missing_t == symbols:
        raise DataQualityError("primary source lacks bar t for every symbol; skipping the run")
    if failed:
        raise DataQualityError("; ".join(f"{r.symbol or '?'} {r.name}: {r.detail}" for r in failed))


def blocked_symbols(results: Iterable[CheckResult]) -> set[str]:
    """Symbols whose orders must be blocked today: the primary source lacks bar t
    (validation 3) or the latest close differs between sources (validation 4)."""
    return {
        r.symbol
        for r in results
        if r.severity == BLOCK_SYMBOL and not r.passed and r.symbol is not None
    }


def alerts(results: Iterable[CheckResult]) -> list[CheckResult]:
    """Every failed check; each one is sent as an alert, whatever its severity."""
    return [r for r in results if not r.passed]
