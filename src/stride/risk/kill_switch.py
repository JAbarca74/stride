"""Kill switch decision (SPEC section 7, step 4, as decided in SPEC 1.3).

HUMAN-REVIEWED MODULE: read the whole file before merging. Pure, no I/O: the
time-weighted index and its peak are stored by `data/store.py`, and the
`KILL_SWITCH` file is handled by `execution/kill_switch_file.py`.

Evaluated once a day on the close (the 18:00 run). Two levels (SPEC 1.4):

- KILL: the time-weighted index is `kill_switch_dd` (40%) or more below its peak.
  Liquidate everything at the next open, using the positions the broker reports.
- HALT: reconciliation breaks on two consecutive days (any symbols). No new
  orders, keep positions, alert, and wait for a person.

A halt never liquidates: when the system is not sure what it holds, selling
blind can make things worse and costs about 1% per order. During a halt the
drawdown kill switch stays armed.

-40% is a trigger, not a guaranteed maximum loss: a fall during the day or a gap
at the next open, when positions are closed, can push the real loss further.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass

DRAWDOWN = "DRAWDOWN"
RECONCILIATION = "RECONCILIATION"


@dataclass(frozen=True)
class TwrState:
    """Time-weighted return index (deposits and withdrawals excluded) and its peak."""

    index: float = 1.0
    peak: float = 1.0

    @property
    def drawdown(self) -> float:
        """Fall from the peak as a positive fraction (0.4 means -40%). NaN when the
        peak is not a positive finite number, which fires the kill switch (fail safe)."""
        if not (math.isfinite(self.peak) and self.peak > 0):
            return math.nan
        return 1 - self.index / self.peak


def advance(state: TwrState, previous_equity: float, equity: float, net_flow: float) -> TwrState:
    """Move the index one day: r = (E_t - F_t) / E_{t-1} - 1, where F_t is the net
    external flow of the day (deposits positive). The peak never goes down."""
    for name, value in {"previous_equity": previous_equity, "equity": equity,
                        "net_flow": net_flow}.items():  # fmt: skip
        if not math.isfinite(value):
            raise ValueError(f"{name} must be finite")
    if previous_equity <= 0:
        raise ValueError("previous_equity must be positive")
    growth = (equity - net_flow) / previous_equity
    index = state.index * max(growth, 0.0)
    return TwrState(index, max(state.peak, index))


def reconciliation_breaks(
    system_usd: Mapping[str, float],
    broker_usd: Mapping[str, float],
    tolerance: float,
    floor_usd: float,
) -> list[str]:
    """Symbols whose broker and system values differ by more than
    max(tolerance x value, floor_usd), plus any symbol held on only one side or
    with a non-finite value on either side (it cannot be compared: fail safe)."""
    breaks = []
    for symbol in sorted(set(system_usd) | set(broker_usd)):
        if symbol not in system_usd or symbol not in broker_usd:
            breaks.append(symbol)
            continue
        expected, actual = system_usd[symbol], broker_usd[symbol]
        if not (math.isfinite(expected) and math.isfinite(actual)):
            breaks.append(symbol)
            continue
        allowed = max(tolerance * max(abs(expected), abs(actual)), floor_usd)
        if abs(actual - expected) > allowed:
            breaks.append(symbol)
    return breaks


@dataclass(frozen=True)
class KillDecision:
    fire: bool  # kill switch: liquidate everything (drawdown only)
    halt: bool  # freeze: no new orders, keep positions (reconciliation only)
    reasons: tuple[str, ...]
    drawdown: float


def kill_switch_decision(
    state: TwrState,
    kill_switch_dd: float,
    breaks_today: list[str],
    breaks_yesterday: list[str],
) -> KillDecision:
    """Kill on a drawdown at or beyond `kill_switch_dd` (a non-finite drawdown
    kills, fail safe); halt on reconciliation breaks two days in a row, on any
    symbols (different symbols on each day suggest a system problem)."""
    drawdown = state.drawdown
    kill = not math.isfinite(drawdown) or drawdown >= kill_switch_dd
    halt = bool(breaks_today) and bool(breaks_yesterday)
    reasons = (DRAWDOWN,) * kill + (RECONCILIATION,) * halt
    return KillDecision(kill, halt, reasons, drawdown)


def equity_from_positions(
    quantities: Mapping[str, float], closes: Mapping[str, float], cash: float
) -> float:
    """Equity as in the backtest: broker quantities x closing prices + cash."""
    missing = [s for s, q in quantities.items() if q and s not in closes]
    if missing:
        raise ValueError(f"no close for {missing}")
    return cash + sum(q * closes[s] for s, q in quantities.items() if q)
