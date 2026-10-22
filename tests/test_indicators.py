from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from stride.config import StrategyConfig
from stride.indicators import (
    atr_wilder,
    compute_indicators,
    ewma_volatility,
    highest_high_before,
    lowest_low_before,
    sma,
    true_range,
)

# A 30-day series with a gap up on day 10 (close 12 -> 16).
CLOSES = [10, 11, 12, 11, 10, 9, 10, 12, 13, 12, 16, 15, 14, 15, 16,
          17, 16, 15, 14, 13, 12, 13, 14, 15, 16, 15, 14, 15, 16, 17]  # fmt: skip
DAYS = pd.bdate_range("2026-01-05", periods=30)
CLOSE = pd.Series(CLOSES, index=DAYS, dtype=float)
HIGH = CLOSE + 1
LOW = CLOSE - 1
SMALL = StrategyConfig(
    sma_filter=5,
    entry_breakout=4,
    exit_breakout=2,
    atr_period=3,
    stop_n=2.0,
    pyramid_step_n=0.5,
    max_units_per_asset=4,
)


# Hand-computed values.


def test_sma_by_hand() -> None:
    result = sma(CLOSE, 3)
    assert result.iloc[:2].isna().all()
    assert result.iloc[2] == pytest.approx((10 + 11 + 12) / 3)
    assert result.iloc[5] == pytest.approx((11 + 10 + 9) / 3)
    assert result.iloc[29] == pytest.approx((15 + 16 + 17) / 3)


def test_highest_high_excludes_day_t_by_hand() -> None:
    result = highest_high_before(HIGH, 3)
    assert result.iloc[:3].isna().all()  # needs 3 bars before t
    assert result.iloc[3] == 13  # highs of days 0-2: 11, 12, 13
    assert result.iloc[11] == 17  # days 8-10 include the gap day (high 17)
    assert result.iloc[10] == 14  # day 10 itself (high 17) is excluded


def test_lowest_low_excludes_day_t_by_hand() -> None:
    result = lowest_low_before(LOW, 3)
    assert result.iloc[4] == 10  # lows of days 1-3: 10, 11, 10
    assert result.iloc[6] == 8  # lows of days 3-5: 10, 9, 8 (day 6 excluded)


def test_true_range_by_hand() -> None:
    tr = true_range(HIGH, LOW, CLOSE)
    assert tr.iloc[0] == 2  # no previous close: high - low
    assert tr.iloc[3] == 2  # H 12, L 10, previous close 12
    assert tr.iloc[10] == 5  # gap: H 17, L 15, previous close 12 -> |17 - 12|


def test_atr_wilder_by_hand() -> None:
    atr = atr_wilder(HIGH, LOW, CLOSE, 3)
    assert atr.iloc[:2].isna().all()
    assert atr.iloc[2] == pytest.approx(2.0)  # mean of the first three true ranges
    # True ranges of days 3-9: 2, 2, 2, 2, 3 (day 7: H13 vs previous close 10), 2, 2.
    assert atr.iloc[6] == pytest.approx(2.0)
    assert atr.iloc[7] == pytest.approx((2 * 2.0 + 3) / 3)  # 7/3
    assert atr.iloc[9] == pytest.approx(58 / 27)
    assert atr.iloc[10] == pytest.approx(251 / 81)  # gap day: TR 5
    assert atr.iloc[11] == pytest.approx(664 / 243)  # TR day 11: H16 L14, previous 16 -> 2


def test_atr_matches_a_naive_loop() -> None:
    previous = [0.0, *CLOSES[:-1]]
    tr = [
        max(h - lo, abs(h - pc), abs(lo - pc)) if i else h - lo
        for i, (h, lo, pc) in enumerate(zip(HIGH, LOW, previous, strict=True))
    ]
    expected = [math.nan, math.nan, sum(tr[:3]) / 3]
    for value in tr[3:]:
        expected.append((2 * expected[-1] + value) / 3)
    np.testing.assert_allclose(atr_wilder(HIGH, LOW, CLOSE, 3).to_numpy(), expected)


def test_ewma_volatility_of_constant_returns() -> None:
    close = pd.Series(100 * 1.01 ** np.arange(30), index=DAYS)
    vol = ewma_volatility(close, span=10)
    assert vol.iloc[:10].isna().all()
    assert vol.iloc[10] == pytest.approx(0.01 * math.sqrt(252))
    assert vol.iloc[-1] == pytest.approx(0.01 * math.sqrt(252))


def test_ewma_volatility_reacts_to_a_shock() -> None:
    close = pd.Series(100.0, index=DAYS)
    close.iloc[20:] = 110.0  # one +10% day
    vol = ewma_volatility(close, span=10)
    assert vol.iloc[19] == 0
    assert vol.iloc[20] > vol.iloc[25] > 0  # decays after the shock


# compute_indicators and look-ahead.


def test_compute_indicators_uses_config_windows() -> None:
    prices = pd.DataFrame({"high": HIGH, "low": LOW, "close": CLOSE})
    result = compute_indicators(prices, SMALL)
    assert list(result.columns) == ["SMA200", "HH55", "LL20", "N"]
    pd.testing.assert_series_equal(result["SMA200"], sma(CLOSE, 5), check_names=False)
    pd.testing.assert_series_equal(result["HH55"], highest_high_before(HIGH, 4), check_names=False)
    pd.testing.assert_series_equal(result["LL20"], lowest_low_before(LOW, 2), check_names=False)
    pd.testing.assert_series_equal(result["N"], atr_wilder(HIGH, LOW, CLOSE, 3), check_names=False)


def test_compute_indicators_sorts_by_date() -> None:
    prices = pd.DataFrame({"high": HIGH, "low": LOW, "close": CLOSE})
    shuffled = prices.iloc[::-1]
    pd.testing.assert_frame_equal(
        compute_indicators(shuffled, SMALL), compute_indicators(prices, SMALL)
    )


def test_future_prices_do_not_change_past_indicators() -> None:
    prices = pd.DataFrame({"high": HIGH, "low": LOW, "close": CLOSE})
    future = prices.copy()
    future.iloc[20:] *= 3  # fake future prices from day 20 on
    past = slice(None, DAYS[19])
    pd.testing.assert_frame_equal(
        compute_indicators(prices, SMALL).loc[past], compute_indicators(future, SMALL).loc[past]
    )
    pd.testing.assert_series_equal(
        ewma_volatility(CLOSE, 10).loc[past],
        ewma_volatility(future["close"], 10).loc[past],
        check_names=False,
    )


def test_channel_on_day_t_ignores_day_t() -> None:
    high = HIGH.copy()
    high.iloc[15] = 1_000  # extreme high on day t
    assert highest_high_before(high, 4).iloc[15] == highest_high_before(HIGH, 4).iloc[15]
    assert highest_high_before(high, 4).iloc[16] == 1_000
