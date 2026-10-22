"""Indicators for the strategy engine (SPEC section 6). Pure functions, no I/O.

Every value on day t uses data up to and including the close of day t, except the
breakout channels, which exclude day t: `HH55` is the highest high of the previous
55 days and `LL20` the lowest low of the previous 20 days. Windows that are not yet
full give NaN, so no signal can fire on partial history.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from stride.config import StrategyConfig

TRADING_DAYS_PER_YEAR = 252


def sma(close: pd.Series, window: int) -> pd.Series:
    """Simple moving average of the last `window` closes, including day t."""
    return close.rolling(window, min_periods=window).mean()


def highest_high_before(high: pd.Series, window: int) -> pd.Series:
    """Highest high of the `window` days before t (day t excluded)."""
    return high.shift(1).rolling(window, min_periods=window).max()


def lowest_low_before(low: pd.Series, window: int) -> pd.Series:
    """Lowest low of the `window` days before t (day t excluded)."""
    return low.shift(1).rolling(window, min_periods=window).min()


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    """max(high - low, |high - previous close|, |low - previous close|).

    The first bar has no previous close, so its true range is high - low.
    """
    previous = close.shift(1)
    ranges = pd.concat([high - low, (high - previous).abs(), (low - previous).abs()], axis=1)
    return ranges.max(axis=1, skipna=True)


def atr_wilder(high: pd.Series, low: pd.Series, close: pd.Series, period: int) -> pd.Series:
    """Average true range with Wilder smoothing (the Turtles' N).

    The first value, on bar `period`, is the simple mean of the first `period` true
    ranges; after that N_t = ((period - 1) * N_{t-1} + TR_t) / period.
    """
    tr = true_range(high, low, close).to_numpy(dtype=float)
    out = np.full(len(tr), np.nan)
    if len(tr) >= period:
        out[period - 1] = tr[:period].mean()
        for i in range(period, len(tr)):
            out[i] = ((period - 1) * out[i - 1] + tr[i]) / period
    return pd.Series(out, index=close.index)


def ewma_volatility(close: pd.Series, span: int) -> pd.Series:
    """Annualized EWMA volatility of daily returns (span `span`, bias-corrected).

    Defined only once `span` returns exist.
    """
    returns = close.pct_change()
    variance = (returns**2).ewm(span=span, min_periods=span, adjust=True).mean()
    annualized: pd.Series = np.sqrt(variance * TRADING_DAYS_PER_YEAR)
    return annualized


def compute_indicators(prices: pd.DataFrame, strategy: StrategyConfig) -> pd.DataFrame:
    """`SMA200`, `HH55`, `LL20` and `N` for one asset, with windows from `config.yaml`.

    `prices` needs `high`, `low` and `close` columns, indexed by date (oldest first).
    """
    frame = prices.sort_index()
    high, low, close = frame["high"], frame["low"], frame["close"]
    return pd.DataFrame(
        {
            "SMA200": sma(close, strategy.sma_filter),
            "HH55": highest_high_before(high, strategy.entry_breakout),
            "LL20": lowest_low_before(low, strategy.exit_breakout),
            "N": atr_wilder(high, low, close, strategy.atr_period),
        },
        index=frame.index,
    )
