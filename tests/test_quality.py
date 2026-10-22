from __future__ import annotations

from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from stride.config import DataQualityConfig, load_config
from stride.data.calendar import trading_days
from stride.data.quality import (
    ALERT,
    BLOCK_SYMBOL,
    LATEST_BAR,
    LATEST_SOURCES_AGREE,
    MAX_DAILY_CHANGE,
    NO_MISSING_DAYS,
    SOURCES_AGREE,
    STOP_RUN,
    CheckResult,
    DataQualityError,
    alerts,
    blocked_symbols,
    check_daily_changes,
    check_latest_bar,
    check_latest_close_agrees,
    check_no_missing_days,
    check_sample_closes_agree,
    expected_trading_days,
    raise_on_blocking_failure,
    validate,
)

ROOT = Path(__file__).resolve().parents[1]
QUALITY: DataQualityConfig = load_config(ROOT / "config.yaml", env={}).data.quality
EVAL = date(2026, 10, 16)  # Friday, a trading day
DAYS = trading_days(date(2025, 6, 2), EVAL)  # about 345 NYSE trading days


def _prices(days: pd.DatetimeIndex = DAYS) -> pd.DataFrame:
    close = 100.0 * np.cumprod(np.full(len(days), 1.001))
    return pd.DataFrame(
        {"open": close, "high": close * 1.01, "low": close * 0.99, "close": close},
        index=pd.DatetimeIndex(days, name="date"),
    )


def _close(days: pd.DatetimeIndex = DAYS) -> pd.Series:
    return _prices(days)["close"]


def _bump(close: pd.Series, day: date, factor: float) -> pd.Series:
    values = close.to_numpy(copy=True)
    values[DAYS.get_loc(pd.Timestamp(day))] *= factor
    return pd.Series(values, index=close.index)


def test_reference_thresholds_are_loaded_from_config() -> None:
    assert QUALITY.missing_days_lookback == 300
    assert QUALITY.max_daily_change == 0.25
    assert QUALITY.source_close_tolerance == 0.005
    assert QUALITY.latest_bar_max_diff == 0.02


def test_clean_data_passes_every_check() -> None:
    results = validate(
        "SPY", _prices(), EVAL, QUALITY, primary_raw_close=_close(), secondary_raw_close=_close()
    )
    assert [r.name for r in results] == [
        NO_MISSING_DAYS,
        MAX_DAILY_CHANGE,
        LATEST_BAR,
        SOURCES_AGREE,
        LATEST_SOURCES_AGREE,
    ]
    assert all(r.passed for r in results)
    assert all(r.symbol == "SPY" for r in results)
    raise_on_blocking_failure(results)
    assert blocked_symbols(results) == set()


# Validation 1: no missing bars in the last 300 NYSE trading days.


def test_expected_days_are_the_last_nyse_trading_days() -> None:
    days = expected_trading_days(EVAL, 300)
    assert len(days) == 300
    assert days[-1] == pd.Timestamp(EVAL)
    assert pd.Timestamp("2025-11-27") not in days  # Thanksgiving 2025
    assert not days.dayofweek.isin([5, 6]).any()


def test_missing_trading_day_fails() -> None:
    gap = DAYS[-10]
    result = check_no_missing_days(_prices(DAYS.drop([gap])), DAYS, 300)
    assert not result.passed
    assert result.severity == STOP_RUN
    assert gap.date().isoformat() in result.detail


def test_missing_day_older_than_lookback_is_ignored() -> None:
    old_gap = DAYS[5]  # outside the last 300 trading days
    assert check_no_missing_days(_prices(DAYS.drop([old_gap])), DAYS, 300).passed


def test_short_history_uses_all_available_bars() -> None:
    young = DAYS[-120:]  # e.g. a recently launched ETF
    assert check_no_missing_days(_prices(young), DAYS, 300).passed
    assert not check_no_missing_days(_prices(young.drop([young[50]])), DAYS, 300).passed


def test_no_bars_fails_missing_days() -> None:
    assert not check_no_missing_days(_prices()[:0], DAYS, 300).passed


# Validation 2: no daily change above 25% without an event flag.


def _with_jump(change: float) -> pd.DataFrame:
    prices = _prices()
    prices.loc[DAYS[-5] :, "close"] *= 1 + change
    return prices


def test_unflagged_jump_above_25_percent_fails() -> None:
    result = check_daily_changes(_with_jump(0.30), 0.25)
    assert not result.passed
    assert result.severity == STOP_RUN
    assert DAYS[-5].date().isoformat() in result.detail


def test_drop_above_25_percent_fails() -> None:
    assert not check_daily_changes(_with_jump(-0.30), 0.25).passed


def test_flagged_jump_passes() -> None:
    assert check_daily_changes(_with_jump(0.30), 0.25, flagged_dates={DAYS[-5].date()}).passed


def test_jump_of_25_percent_or_less_passes() -> None:
    assert check_daily_changes(_with_jump(0.24), 0.25).passed


# Validation 3: the latest bar is day t.


def test_latest_bar_of_day_t_passes() -> None:
    assert check_latest_bar(_prices(), EVAL).passed


def test_bar_of_previous_day_blocks_the_symbol() -> None:
    result = check_latest_bar(_prices(DAYS[:-1]), EVAL)
    assert not result.passed
    assert result.severity == BLOCK_SYMBOL
    assert "expected 2026-10-16" in result.detail


def test_missing_bar_t_does_not_count_as_a_missing_history_day() -> None:
    results = validate("SPY", _prices(DAYS[:-1]), EVAL, QUALITY)
    by_name = {r.name: r for r in results}
    assert by_name[NO_MISSING_DAYS].passed
    assert not by_name[LATEST_BAR].passed


def test_primary_missing_bar_t_for_one_symbol_blocks_only_that_symbol() -> None:
    results = validate("GLD", _prices(DAYS[:-1]), EVAL, QUALITY) + validate(
        "SPY", _prices(), EVAL, QUALITY
    )
    raise_on_blocking_failure(results)  # the run goes on
    assert blocked_symbols(results) == {"GLD"}
    assert [(r.symbol, r.name) for r in alerts(results)] == [("GLD", LATEST_BAR)]


def test_primary_missing_bar_t_for_every_symbol_skips_the_run() -> None:
    results = validate("GLD", _prices(DAYS[:-1]), EVAL, QUALITY) + validate(
        "SPY", _prices(DAYS[:-1]), EVAL, QUALITY
    )
    with pytest.raises(DataQualityError, match="every symbol"):
        raise_on_blocking_failure(results)


def test_secondary_missing_bar_t_is_an_alert_only() -> None:
    results = validate(
        "SPY",
        _prices(),
        EVAL,
        QUALITY,
        primary_raw_close=_close(),
        secondary_raw_close=_close(DAYS[:-1]),
    )
    raise_on_blocking_failure(results)
    assert blocked_symbols(results) == set()
    [alert] = alerts(results)
    assert (alert.name, alert.severity) == (LATEST_SOURCES_AGREE, ALERT)


def test_empty_prices_fail_latest_bar() -> None:
    assert not check_latest_bar(_prices()[:0], EVAL).passed


# Validation 4a: historical sample of unadjusted closes, alert only.


def test_sample_mismatch_above_tolerance_raises_an_alert() -> None:
    secondary = _bump(_close(), DAYS[-3].date(), 1.01)
    result = check_sample_closes_agree(_close(), secondary, EVAL, 0.005)
    assert not result.passed
    assert result.severity == ALERT
    assert f"1 of {len(DAYS) - 1}" in result.detail


def test_sample_within_tolerance_passes() -> None:
    assert check_sample_closes_agree(_close(), _close() * 1.004, EVAL, 0.005).passed


def test_sample_excludes_day_t() -> None:
    secondary = _bump(_close(), EVAL, 1.01)
    assert check_sample_closes_agree(_close(), secondary, EVAL, 0.005).passed


def test_sample_without_common_dates_raises_an_alert() -> None:
    result = check_sample_closes_agree(_close(DAYS[:10]), _close(DAYS[-10:-5]), EVAL, 0.005)
    assert not result.passed
    assert result.severity == ALERT


# Validation 4b: latest bar, blocks the symbol above 2%.


def test_latest_close_mismatch_above_2_percent_blocks_the_symbol() -> None:
    secondary = _bump(_close(), EVAL, 1.025)
    result = check_latest_close_agrees(_close(), secondary, EVAL, 0.02)
    assert not result.passed
    assert result.severity == BLOCK_SYMBOL
    assert "2.44%" in result.detail


def test_latest_close_within_2_percent_passes() -> None:
    secondary = _bump(_close(), EVAL, 1.015)
    assert check_latest_close_agrees(_close(), secondary, EVAL, 0.02).passed


def test_latest_close_missing_in_one_source_raises_an_alert() -> None:
    result = check_latest_close_agrees(_close(), _close(DAYS[:-1]), EVAL, 0.02)
    assert not result.passed
    assert result.severity == ALERT


def test_latest_mismatch_blocks_only_that_symbol_and_does_not_stop_the_run() -> None:
    bad = validate(
        "GLD",
        _prices(),
        EVAL,
        QUALITY,
        primary_raw_close=_close(),
        secondary_raw_close=_bump(_close(), EVAL, 1.05),
    )
    good = validate(
        "SPY", _prices(), EVAL, QUALITY, primary_raw_close=_close(), secondary_raw_close=_close()
    )
    raise_on_blocking_failure(bad + good)
    assert blocked_symbols(bad + good) == {"GLD"}


# Stopping the run.


def test_run_stopping_failure_raises() -> None:
    prices = _prices(DAYS.drop([DAYS[-10]]))  # validation 1 stops the run
    results = validate("SPY", prices, EVAL, QUALITY)
    with pytest.raises(DataQualityError, match=f"SPY {NO_MISSING_DAYS}"):
        raise_on_blocking_failure(results)


def test_validation_4_is_skipped_without_both_sources() -> None:
    names = [r.name for r in validate("SPY", _prices(), EVAL, QUALITY)]
    assert names == [NO_MISSING_DAYS, MAX_DAILY_CHANGE, LATEST_BAR]


def test_alert_only_failure_does_not_stop_the_run() -> None:
    raise_on_blocking_failure(
        [CheckResult(NO_MISSING_DAYS, True), CheckResult(SOURCES_AGREE, False, severity=ALERT)]
    )


def test_jump_before_the_window_is_ignored() -> None:
    prices = _prices()
    prices.loc[DAYS[5] :, "close"] *= 1.5  # a genuine old move, like early ETH history
    assert not check_daily_changes(prices, 0.25).passed
    assert check_daily_changes(prices, 0.25, window_start=DAYS[10].date()).passed


def test_validate_uses_the_300_day_window_for_validation_2() -> None:
    prices = _prices()
    prices.loc[DAYS[5] :, "close"] *= 1.5  # more than 300 trading days before EVAL
    results = {r.name: r for r in validate("ETH", prices, EVAL, QUALITY)}
    assert results[MAX_DAILY_CHANGE].passed
