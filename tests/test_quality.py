from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from stride.data.quality import (
    LATEST_BAR,
    MAX_DAILY_CHANGE_CHECK,
    NO_MISSING_DAYS,
    SOURCES_AGREE,
    CheckResult,
    DataQualityError,
    check_daily_changes,
    check_latest_bar,
    check_no_missing_days,
    check_sources_agree,
    raise_on_blocking_failure,
    validate,
)

DAYS = pd.bdate_range("2025-06-02", periods=320)
LATEST = DAYS[-1].date()


def _prices(days: pd.DatetimeIndex = DAYS, start: float = 100.0) -> pd.DataFrame:
    close = start * np.cumprod(np.full(len(days), 1.001))
    return pd.DataFrame(
        {"open": close, "high": close * 1.01, "low": close * 0.99, "close": close},
        index=pd.DatetimeIndex(days, name="date"),
    )


def _bump(prices: pd.DataFrame, day: pd.Timestamp, factor: float) -> None:
    close = prices["close"].to_numpy(copy=True)
    close[DAYS.get_loc(day)] *= factor
    prices["close"] = close


def test_clean_data_passes_every_check() -> None:
    results = validate(_prices(), DAYS, LATEST, secondary=_prices())
    assert [r.name for r in results] == [
        NO_MISSING_DAYS,
        MAX_DAILY_CHANGE_CHECK,
        LATEST_BAR,
        SOURCES_AGREE,
    ]
    assert all(r.passed for r in results)
    raise_on_blocking_failure(results)


# Validation 1: no missing trading days in the last 300 days.


def test_missing_trading_day_fails() -> None:
    gap = DAYS[-10]
    result = check_no_missing_days(_prices(DAYS.drop([gap])), DAYS)
    assert not result.passed
    assert result.blocking
    assert gap.date().isoformat() in result.detail


def test_missing_day_older_than_lookback_is_ignored() -> None:
    old_gap = DAYS[5]  # 315 days before the end, outside the 300-day window
    assert check_no_missing_days(_prices(DAYS.drop([old_gap])), DAYS).passed


# Validation 2: no daily change above 25% without an event flag.


def _with_jump(change: float) -> pd.DataFrame:
    prices = _prices()
    prices.loc[DAYS[-5] :, "close"] *= 1 + change
    return prices


def test_unflagged_jump_above_25_percent_fails() -> None:
    result = check_daily_changes(_with_jump(0.30))
    assert not result.passed
    assert result.blocking
    assert DAYS[-5].date().isoformat() in result.detail


def test_drop_above_25_percent_fails() -> None:
    assert not check_daily_changes(_with_jump(-0.30)).passed


def test_flagged_jump_passes() -> None:
    assert check_daily_changes(_with_jump(0.30), flagged_dates={DAYS[-5].date()}).passed


def test_jump_of_25_percent_or_less_passes() -> None:
    assert check_daily_changes(_with_jump(0.24)).passed


# Validation 3: the latest bar is the expected (previous) trading day.


def test_stale_latest_bar_fails() -> None:
    result = check_latest_bar(_prices(DAYS[:-1]), LATEST)
    assert not result.passed
    assert result.blocking
    assert str(LATEST) in result.detail


def test_empty_prices_fail_latest_bar() -> None:
    assert not check_latest_bar(_prices()[:0], LATEST).passed


# Validation 4: two sources agree on the close within 0.5% (alert only).


def test_sources_disagreeing_above_tolerance_raise_an_alert() -> None:
    secondary = _prices()
    _bump(secondary, DAYS[-3], 1.01)
    result = check_sources_agree(_prices(), secondary)
    assert not result.passed
    assert not result.blocking
    assert "1 of 320" in result.detail


def test_sources_within_tolerance_pass() -> None:
    secondary = _prices()
    secondary["close"] *= 1.004
    assert check_sources_agree(_prices(), secondary).passed


def test_sources_compare_only_sample_dates() -> None:
    secondary = _prices()
    _bump(secondary, DAYS[-3], 1.01)
    sample = [d.date() for d in DAYS[:10]]
    assert check_sources_agree(_prices(), secondary, sample_dates=sample).passed


def test_sources_without_common_dates_raise_an_alert() -> None:
    result = check_sources_agree(_prices(DAYS[:10]), _prices(DAYS[-10:]))
    assert not result.passed
    assert not result.blocking


# Stopping the run.


def test_blocking_failure_stops_the_run() -> None:
    results = validate(_prices(DAYS[:-1]), DAYS, LATEST)
    with pytest.raises(DataQualityError, match=LATEST_BAR):
        raise_on_blocking_failure(results)


def test_alert_only_failure_does_not_stop_the_run() -> None:
    raise_on_blocking_failure(
        [CheckResult(NO_MISSING_DAYS, True), CheckResult(SOURCES_AGREE, False, blocking=False)]
    )


def test_expected_latest_is_supplied_by_caller() -> None:
    assert check_latest_bar(_prices(), date(2026, 10, 16)).detail.endswith("2026-10-16")
