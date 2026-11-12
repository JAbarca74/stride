"""Daily panel for the risk-allocation research (docs/design/mc_risk_allocation.md).

One step per NYSE session, close to close. The panel is the same `ReturnPanel` the
30-minute module uses, with a single slot per day, so `standardize` and `simulate`
(with `n_slots=DAILY_SLOTS`) run on it unchanged. Pure: no network, clock or files.
"""

from __future__ import annotations

from collections.abc import Mapping

import numpy as np
import pandas as pd

from stride.forecast.returns import FloatArray, ReturnPanel

DAILY_SLOTS = 1
NEW_YORK = "America/New_York"
CLOSE_HOUR = 16

BoolArray = np.ndarray[tuple[int], np.dtype[np.bool_]]

QUALITY_COLUMNS = (
    "symbol",
    "first",
    "last",
    "bars",
    "missing_sessions",
    "off_calendar_bars",
    "non_positive_prices",
    "max_abs_change",
    "max_abs_change_date",
    "longest_flat_run",
)


def _session_index(values: pd.Index) -> pd.DatetimeIndex:
    return pd.DatetimeIndex(pd.to_datetime(values)).normalize()


def build_daily_panel(bars: Mapping[str, pd.DataFrame], days: pd.DatetimeIndex) -> ReturnPanel:
    """Align each asset's daily bars on the sessions `days` (oldest first).

    `bars[symbol]` is indexed by date with columns `open` and `close` (adjusted).
    A session without a bar repeats the previous close (return 0) and has no open;
    rows before an asset's first bar are NaN, so is the return of its first bar.
    Bars dated outside `days` are ignored: nothing after the last session enters.
    """
    sessions = _session_index(days)
    if not sessions.is_monotonic_increasing or not sessions.is_unique:
        raise ValueError("days must be unique and sorted, oldest first")
    symbols = tuple(bars)
    opens = np.full((len(sessions), len(symbols)), np.nan)
    closes = np.full((len(sessions), len(symbols)), np.nan)
    for column, symbol in enumerate(symbols):
        frame = bars[symbol][["open", "close"]].copy()
        frame.index = _session_index(frame.index)
        if not frame.index.is_unique:
            raise ValueError(f"{symbol}: more than one bar for a session")
        aligned = frame.reindex(sessions)
        opens[:, column] = aligned["open"].to_numpy(dtype=float)
        closes[:, column] = aligned["close"].ffill().to_numpy(dtype=float)
    ret = np.full_like(closes, np.nan)
    if len(sessions) > 1:
        with np.errstate(invalid="ignore", divide="ignore"):
            ret[1:] = np.log(closes[1:] / closes[:-1])
    end = (sessions.tz_localize(NEW_YORK) + pd.Timedelta(hours=CLOSE_HOUR)).tz_convert("UTC")
    return ReturnPanel(
        symbols,
        pd.DatetimeIndex(end),
        sessions,
        np.zeros(len(sessions), dtype=np.int64),
        opens,
        closes,
        ret,
    )


def eligible(panel: ReturnPanel, row: int, window: int) -> BoolArray:
    """Assets with a full window of `window` returns ending at `row` (the as-of universe).

    Uses rows up to `row` only. An asset joins `window` sessions after its first bar.
    """
    if window < 1:
        raise ValueError("window must be at least 1")
    if row < 0 or row >= len(panel):
        raise ValueError("row is outside the panel")
    start = row - window + 1
    if start < 0:
        return np.zeros(len(panel.symbols), dtype=np.bool_)
    block: FloatArray = panel.ret[start : row + 1]
    out: BoolArray = np.isfinite(block).all(axis=0)
    return out


def _longest_flat_run(close: pd.Series) -> int:
    """Longest run of consecutive sessions whose close did not change."""
    flat = close.diff().eq(0.0).to_numpy()
    longest = current = 0
    for value in flat:
        current = current + 1 if value else 0
        longest = max(longest, current)
    return longest


def quality_table(bars: Mapping[str, pd.DataFrame], days: pd.DatetimeIndex) -> pd.DataFrame:
    """One row per symbol: span, gaps against the sessions `days`, and suspect values.

    `missing_sessions` counts sessions between the first and last bar of the symbol
    without a bar; `off_calendar_bars` counts bars dated on a day that is not a
    session; `max_abs_change` is the largest close-to-close move and its date.
    """
    sessions = _session_index(days)
    rows: list[dict[str, object]] = []
    for symbol, raw in bars.items():
        frame = raw.copy()
        frame.index = _session_index(frame.index)
        frame = frame.sort_index()
        if frame.empty:
            rows.append({"symbol": symbol, "bars": 0})
            continue
        index = pd.DatetimeIndex(frame.index)
        first, last = index[0], index[-1]
        span = sessions[(sessions >= first) & (sessions <= last)]
        close = frame["close"].astype(float)
        change = close.pct_change().abs()
        has_change = bool(change.notna().any())
        prices = frame[["open", "close"]].astype(float)
        rows.append(
            {
                "symbol": symbol,
                "first": first.date().isoformat(),
                "last": last.date().isoformat(),
                "bars": len(frame),
                "missing_sessions": len(span.difference(index)),
                "off_calendar_bars": len(index.difference(sessions)),
                "non_positive_prices": int((prices <= 0).any(axis=1).sum()),
                "max_abs_change": float(change.max()) if has_change else float("nan"),
                "max_abs_change_date": (
                    index[int(np.nanargmax(change.to_numpy()))].date().isoformat()
                    if has_change
                    else ""
                ),
                "longest_flat_run": _longest_flat_run(close),
            }
        )
    return pd.DataFrame(rows, columns=list(QUALITY_COLUMNS))
