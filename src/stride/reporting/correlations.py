"""Correlation matrix of the universe (SPEC section 10, task T20). Pure, no I/O.

Pearson correlation of daily returns over the last 3 years (756 trading days)
up to a given day; pairs above 0.7 are flagged as highly correlated.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date

import pandas as pd

WINDOW_DAYS = 3 * 252  # SPEC section 10: 3 years of daily returns
FLAG_ABOVE = 0.7


def daily_returns(
    closes: Mapping[str, pd.Series], end: date, window: int = WINDOW_DAYS
) -> pd.DataFrame:
    """Daily returns per symbol for the last `window` trading days up to `end`.

    Symbols are aligned on common dates; a day missing for one symbol is dropped
    for all, so every pair is measured on the same days.
    """
    frame = pd.DataFrame({symbol: close.sort_index() for symbol, close in closes.items()})
    frame = frame.loc[: pd.Timestamp(end)]
    returns = frame.pct_change(fill_method=None).iloc[1:].dropna()
    return returns.iloc[-window:]


def correlation_matrix(
    closes: Mapping[str, pd.Series], end: date, window: int = WINDOW_DAYS
) -> pd.DataFrame:
    """Pearson correlation of daily returns over the window ending on `end`."""
    return daily_returns(closes, end, window).corr()


@dataclass(frozen=True)
class FlaggedPair:
    a: str
    b: str
    correlation: float


def flag_pairs(matrix: pd.DataFrame, threshold: float = FLAG_ABOVE) -> list[FlaggedPair]:
    """Pairs whose correlation is above `threshold`, highest first."""
    symbols = [str(symbol) for symbol in matrix.columns]
    values = matrix.to_numpy(dtype=float)
    pairs = [
        FlaggedPair(symbols[i], symbols[j], float(values[i, j]))
        for i in range(len(symbols))
        for j in range(i + 1, len(symbols))
        if values[i, j] > threshold
    ]
    return sorted(pairs, key=lambda pair: pair.correlation, reverse=True)
