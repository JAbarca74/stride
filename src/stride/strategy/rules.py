"""The section 6 state machine for one asset on one day. Pure, no I/O.

Rules in priority order (the first that applies wins):

1. TREND_FILTER: close < SMA200 -> 0 units (also closes anything open).
2. STOP_2N: with a position, close < last_entry - stop_n * N -> 0 units.
3. EXIT_LL20: with a position, close < LL20 -> 0 units.
4. ENTRY_HH55: with no position, close > HH55 -> 1 unit.
5. PYRAMID: with a position, close >= last_entry + pyramid_step_n * N -> +1 unit,
   up to max_units_per_asset (one unit per day).
6. HOLD: keep the current units.

Long only, never short. A missing indicator (not enough history) never lets a
position open or grow: a missing SMA200 counts as a failed trend filter.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from stride.config import StrategyConfig

TREND_FILTER = "TREND_FILTER"
STOP_2N = "STOP_2N"
EXIT_LL20 = "EXIT_LL20"
ENTRY_HH55 = "ENTRY_HH55"
PYRAMID = "PYRAMID"
HOLD = "HOLD"


@dataclass(frozen=True)
class PositionState:
    """Units held after the previous day, and the close of the last entry or add."""

    units: int = 0
    last_entry: float | None = None


@dataclass(frozen=True)
class Indicators:
    sma200: float
    hh55: float
    ll20: float
    n: float


@dataclass(frozen=True)
class Decision:
    rule: str
    target_units: int
    last_entry: float | None


def _missing(value: float) -> bool:
    return math.isnan(value)


def decide(
    close: float, ind: Indicators, state: PositionState, strategy: StrategyConfig
) -> Decision:
    """Target units for one asset after the close of day t."""
    if _missing(ind.sma200) or close < ind.sma200:
        return Decision(TREND_FILTER, 0, None)
    if state.units <= 0 or state.last_entry is None:
        return _flat(close, ind)
    return _exit(close, ind, state.last_entry, strategy) or _hold_or_add(
        close, ind, state, state.last_entry, strategy
    )


def _flat(close: float, ind: Indicators) -> Decision:
    if not _missing(ind.hh55) and not _missing(ind.n) and close > ind.hh55:
        return Decision(ENTRY_HH55, 1, close)
    return Decision(HOLD, 0, None)


def _exit(
    close: float, ind: Indicators, last_entry: float, strategy: StrategyConfig
) -> Decision | None:
    if not _missing(ind.n) and close < last_entry - strategy.stop_n * ind.n:
        return Decision(STOP_2N, 0, None)
    if not _missing(ind.ll20) and close < ind.ll20:
        return Decision(EXIT_LL20, 0, None)
    return None


def _hold_or_add(
    close: float,
    ind: Indicators,
    state: PositionState,
    last_entry: float,
    strategy: StrategyConfig,
) -> Decision:
    if (
        state.units < strategy.max_units_per_asset
        and not _missing(ind.n)
        and close >= last_entry + strategy.pyramid_step_n * ind.n
    ):
        return Decision(PYRAMID, state.units + 1, close)
    return Decision(HOLD, state.units, last_entry)
