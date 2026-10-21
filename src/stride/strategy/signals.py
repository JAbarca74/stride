"""Daily target units per asset (SPEC section 6). Pure, no I/O.

`generate_signals` walks one asset's history day by day, assuming each target is
reached, and returns one row per day with the indicators, the rule that fired and
the current and target units: the content of the `signals` table (SPEC section 10).
Only data up to and including day t is used for day t.
"""

from __future__ import annotations

import pandas as pd

from stride.config import StrategyConfig
from stride.indicators import compute_indicators
from stride.strategy.rules import Indicators, PositionState, decide

SIGNAL_COLUMNS = (
    "close",
    "SMA200",
    "HH55",
    "LL20",
    "N",
    "rule",
    "current_units",
    "target_units",
    "last_entry",
)


def generate_signals(
    prices: pd.DataFrame,
    strategy: StrategyConfig,
    initial: PositionState | None = None,
) -> pd.DataFrame:
    """One row per day: indicators, rule fired, current and target units.

    `prices` needs `high`, `low` and `close`, indexed by date. `initial` is the
    position held before the first day (none by default).
    """
    frame = prices.sort_index()
    indicators = compute_indicators(frame, strategy)
    state = initial or PositionState()
    rows = []
    for close, sma200, hh55, ll20, n in zip(
        frame["close"].astype(float),
        indicators["SMA200"],
        indicators["HH55"],
        indicators["LL20"],
        indicators["N"],
        strict=True,
    ):
        decision = decide(close, Indicators(sma200, hh55, ll20, n), state, strategy)
        rows.append(
            (close, sma200, hh55, ll20, n, decision.rule, state.units,
             decision.target_units, decision.last_entry)
        )  # fmt: skip
        state = PositionState(decision.target_units, decision.last_entry)
    return pd.DataFrame(rows, index=frame.index, columns=list(SIGNAL_COLUMNS))
