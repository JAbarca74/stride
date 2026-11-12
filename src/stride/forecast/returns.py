"""Slot returns and the volatility filter (design sections 2 and 3, step 1).

A panel holds one row per session slot and one column per asset. The filter turns
returns into standardized shocks: each return divided by the volatility that was
known just before it and by the usual size of its slot of the day.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import numpy as np
import pandas as pd

from stride.forecast.params import SLOTS_PER_DAY

FloatArray = np.ndarray[tuple[int, ...], np.dtype[np.float64]]
IntArray = np.ndarray[tuple[int, ...], np.dtype[np.int64]]

MIN_SLOT_FACTOR = 0.05
VARIANCE_FLOOR = 1e-16


@dataclass(frozen=True)
class ReturnPanel:
    """Aligned slot data for several assets, oldest first.

    `ret[t, i]` is the log return of asset i from the close of slot t-1 to the close
    of slot t (the first slot of a day spans the overnight gap). A slot without
    trades repeats the previous close (return 0). Rows before an asset's first bar
    are NaN.
    """

    symbols: tuple[str, ...]
    end: pd.DatetimeIndex  # UTC end of each slot
    day: pd.DatetimeIndex  # session of each slot (timezone-naive)
    slot: IntArray
    open: FloatArray  # NaN where the slot had no trades
    close: FloatArray  # forward-filled
    ret: FloatArray

    def __len__(self) -> int:
        return len(self.slot)

    def window(self, start: int, stop: int) -> ReturnPanel:
        """Rows `start` to `stop - 1`, for evaluating one period of a longer history."""
        return ReturnPanel(
            self.symbols,
            self.end[start:stop],
            self.day[start:stop],
            self.slot[start:stop],
            self.open[start:stop],
            self.close[start:stop],
            self.ret[start:stop],
        )

    def upto(self, rows: int) -> ReturnPanel:
        """The first `rows` rows: what was known after slot `rows - 1` closed."""
        return ReturnPanel(
            self.symbols,
            self.end[:rows],
            self.day[:rows],
            self.slot[:rows],
            self.open[:rows],
            self.close[:rows],
            self.ret[:rows],
        )


def build_panel(slots: Mapping[str, pd.DataFrame], grid: pd.DataFrame) -> ReturnPanel:
    """Align each asset's slot bars (`data.intraday.slot_bars`) on the session `grid`.

    `grid` comes from `data.intraday.session_slots`, already cut at the last slot
    that has closed; nothing after it enters the panel.
    """
    symbols = tuple(slots)
    base = grid[["day", "slot", "end"]].reset_index(drop=True)
    opens = np.full((len(base), len(symbols)), np.nan)
    closes = np.full((len(base), len(symbols)), np.nan)
    for column, symbol in enumerate(symbols):
        bars = slots[symbol][["day", "slot", "open", "close"]]
        merged = base.merge(bars, on=["day", "slot"], how="left")
        opens[:, column] = merged["open"].to_numpy(dtype=float)
        closes[:, column] = merged["close"].ffill().to_numpy(dtype=float)
    ret = np.full_like(closes, np.nan)
    if len(base) > 1:
        with np.errstate(invalid="ignore", divide="ignore"):
            ret[1:] = np.log(closes[1:] / closes[:-1])
    return ReturnPanel(
        symbols,
        pd.DatetimeIndex(base["end"]),
        pd.DatetimeIndex(base["day"]),
        base["slot"].to_numpy(dtype=np.int64),
        opens,
        closes,
        ret,
    )


@dataclass(frozen=True)
class Filtered:
    """Output of the volatility filter over one window (W steps, N assets)."""

    shocks: FloatArray  # [W, N] standardized returns z
    variance: FloatArray  # [W + 1, N]; variance[t] was known before return t
    factors: FloatArray  # [slots, N] usual size of each slot of the day
    scaled: FloatArray  # [W, N] returns divided by their slot factor

    @property
    def next_variance(self) -> FloatArray:
        """The variance forecast for the first simulated step."""
        last: FloatArray = self.variance[-1]
        return last


def slot_factors(ret: FloatArray, slot: IntArray, n_slots: int = SLOTS_PER_DAY) -> FloatArray:
    """Root mean square return of each slot relative to all slots, per asset.

    The overnight slot is larger than midday; dividing by this factor makes the
    returns of different slots comparable. A slot absent from the window gets 1.
    """
    squared = ret**2
    overall = squared.mean(axis=0)
    factors = np.ones((n_slots, ret.shape[1]))
    for index in range(n_slots):
        rows = slot == index
        if rows.any():
            with np.errstate(invalid="ignore", divide="ignore"):
                ratio = np.sqrt(squared[rows].mean(axis=0) / overall)
            factors[index] = np.where(np.isfinite(ratio), ratio, 1.0)
    clipped: FloatArray = np.maximum(factors, MIN_SLOT_FACTOR)
    return clipped


def ewma_variance(scaled: FloatArray, span: int) -> FloatArray:
    """EWMA variance; row t uses returns before t only, row W is the next forecast.

    Seeded with the window's mean square, so the first rows are the unconditional
    level rather than a noisy estimate.
    """
    if span < 1:
        raise ValueError("span must be at least 1")
    alpha = 2.0 / (span + 1.0)
    variance = np.empty((scaled.shape[0] + 1, scaled.shape[1]))
    variance[0] = (scaled**2).mean(axis=0)
    for t in range(scaled.shape[0]):
        variance[t + 1] = (1.0 - alpha) * variance[t] + alpha * scaled[t] ** 2
    return variance


def standardize(ret: FloatArray, slot: IntArray, span: int) -> Filtered:
    """Filter a window of returns `ret` [W, N] (no NaN) into standardized shocks."""
    if ret.ndim != 2 or ret.shape[0] != slot.shape[0]:  # noqa: PLR2004
        raise ValueError("ret must be [W, N] with one slot per row")
    if not np.isfinite(ret).all():
        raise ValueError("the window contains missing returns")
    factors = slot_factors(ret, slot)
    scaled = ret / factors[slot]
    variance = ewma_variance(scaled, span)
    shocks = scaled / np.sqrt(np.maximum(variance[:-1], VARIANCE_FLOOR))
    shocks = np.where(variance[:-1] > VARIANCE_FLOOR, shocks, 0.0)
    return Filtered(shocks, variance, factors, scaled)
