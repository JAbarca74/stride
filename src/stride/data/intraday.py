"""Intraday bars for the Monte Carlo portfolio (docs/design/mc_forecast.md, section 2).

Regular-session 30-minute bars are stored in the `intraday_bars` table, keyed by
(symbol, ts) where `ts` is the UTC start of the bar. They are aggregated on read to
the 7 slots of a session: 9:30-10:00, then one per hour until the close.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Protocol

import pandas as pd

from stride.data.calendar import sessions

BAR_MINUTES = 30
BAR = pd.Timedelta(minutes=BAR_MINUTES)
SLOT_MINUTES = 60
SLOTS_PER_DAY = 7  # a full session; early closes have fewer
NEW_YORK = "America/New_York"
TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"  # fixed width, so text order is time order
BAR_COLUMNS = ("ts", "open", "high", "low", "close", "volume")
# The Basic plan withholds the latest 15 minutes of SIP data.
DATA_DELAY = timedelta(minutes=16)
# An adjusted close that moved by more than this was re-adjusted (dividend or split).
ADJUSTMENT_TOLERANCE = 1e-4
DEFAULT_OVERLAP = timedelta(days=5)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS intraday_bars (
    symbol     TEXT    NOT NULL,
    ts         TEXT    NOT NULL,
    open       REAL    NOT NULL,
    high       REAL    NOT NULL,
    low        REAL    NOT NULL,
    close      REAL    NOT NULL,
    volume     INTEGER NOT NULL CHECK (volume >= 0),
    source     TEXT    NOT NULL,
    fetched_at TEXT    NOT NULL,
    PRIMARY KEY (symbol, ts),
    CHECK (low <= open AND open <= high),
    CHECK (low <= close AND close <= high)
)
"""

_UPSERT = """
INSERT INTO intraday_bars (symbol, ts, open, high, low, close, volume, source, fetched_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (symbol, ts) DO UPDATE SET
    open = excluded.open,
    high = excluded.high,
    low = excluded.low,
    close = excluded.close,
    volume = excluded.volume,
    source = excluded.source,
    fetched_at = excluded.fetched_at
"""


class IntradaySource(Protocol):
    """A provider of adjusted intraday bars (see `AlpacaSource.fetch_intraday`)."""

    name: str

    def fetch_intraday(
        self, symbol: str, start: datetime, end: datetime, timeframe: str = "30Min"
    ) -> pd.DataFrame:
        """Bars with columns ts (UTC), open, high, low, close, volume."""


def init_schema(conn: sqlite3.Connection) -> None:
    """Create the `intraday_bars` table if missing."""
    with conn:
        conn.execute(_SCHEMA)


def _utc(moment: datetime | pd.Timestamp) -> pd.Timestamp:
    stamp = pd.Timestamp(moment)
    if stamp.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return stamp.tz_convert("UTC")


def _empty_bars() -> pd.DataFrame:
    frame = pd.DataFrame({column: pd.Series(dtype=float) for column in BAR_COLUMNS[1:]})
    frame["volume"] = frame["volume"].astype("int64")
    frame.insert(0, "ts", pd.Series(dtype="datetime64[ns, UTC]"))
    return frame


def session_slots(start: date, end: date) -> pd.DataFrame:
    """Every slot of every NYSE session from `start` to `end`, inclusive.

    Columns: `day` (timezone-naive), `slot` (0 = the first half hour), `start` and
    `end` (UTC). The last slot of an early-close session ends at that close.
    """
    rows: list[tuple[pd.Timestamp, int, pd.Timestamp, pd.Timestamp]] = []
    for day, session in sessions(start, end).iterrows():
        opened, closed = session["market_open"], session["market_close"]
        begin, slot = opened, 0
        while begin < closed:
            width = BAR if slot == 0 else pd.Timedelta(minutes=SLOT_MINUTES)
            finish = min(begin + width, closed)
            rows.append((pd.Timestamp(str(day)), slot, begin, finish))
            begin, slot = finish, slot + 1
    frame = pd.DataFrame(rows, columns=["day", "slot", "start", "end"])
    if frame.empty:
        frame["start"] = pd.Series(dtype="datetime64[ns, UTC]")
        frame["end"] = pd.Series(dtype="datetime64[ns, UTC]")
        frame["day"] = pd.Series(dtype="datetime64[ns]")
        frame["slot"] = frame["slot"].astype("int64")
    return frame


def regular_session(bars: pd.DataFrame) -> pd.DataFrame:
    """Only the bars that lie fully inside an NYSE regular session.

    Drops pre-market, after-hours, holiday and post-early-close bars.
    """
    if bars.empty:
        return _empty_bars()
    stamps = pd.DatetimeIndex(pd.to_datetime(bars["ts"], utc=True))
    days = stamps.tz_convert(NEW_YORK).tz_localize(None).normalize()
    schedule = sessions(days.min().date(), days.max().date())
    opened = schedule["market_open"].reindex(days).to_numpy()
    closed = schedule["market_close"].reindex(days).to_numpy()
    start = stamps.tz_localize(None).to_numpy()
    finish = (stamps + BAR).tz_localize(None).to_numpy()
    opened_naive = pd.DatetimeIndex(opened).tz_localize(None).to_numpy()
    closed_naive = pd.DatetimeIndex(closed).tz_localize(None).to_numpy()
    keep = (start >= opened_naive) & (finish <= closed_naive)  # NaT (no session) is False
    return bars.loc[keep].reset_index(drop=True)


def upsert_intraday(
    conn: sqlite3.Connection, symbol: str, bars: pd.DataFrame, source: str, fetched_at: datetime
) -> int:
    """Insert or update bars keyed by (symbol, ts); returns the number of rows written."""
    missing = [column for column in BAR_COLUMNS if column not in bars.columns]
    if missing:
        raise ValueError(f"missing intraday columns: {missing}")
    empty = [column for column in BAR_COLUMNS if bars[column].isna().any()]
    if empty:
        raise ValueError(f"empty values in intraday columns: {empty}")
    stamps = pd.DatetimeIndex(pd.to_datetime(bars["ts"], utc=True))
    fetched = _utc(fetched_at).isoformat()
    rows = list(
        zip(
            [symbol] * len(bars),
            stamps.strftime(TS_FORMAT).tolist(),
            *(bars[column].astype(float).tolist() for column in ("open", "high", "low", "close")),
            pd.to_numeric(bars["volume"]).round().astype("int64").tolist(),
            [source] * len(bars),
            [fetched] * len(bars),
            strict=True,
        )
    )
    with conn:
        conn.executemany(_UPSERT, rows)
    return len(rows)


def read_intraday(
    conn: sqlite3.Connection,
    symbol: str,
    start: datetime | None = None,
    end: datetime | None = None,
) -> pd.DataFrame:
    """Stored bars for `symbol` with `start <= ts <= end`, oldest first (BAR_COLUMNS)."""
    query = "SELECT ts, open, high, low, close, volume FROM intraday_bars WHERE symbol = ?"
    params: list[str] = [symbol]
    if start is not None:
        query += " AND ts >= ?"
        params.append(_utc(start).strftime(TS_FORMAT))
    if end is not None:
        query += " AND ts <= ?"
        params.append(_utc(end).strftime(TS_FORMAT))
    frame = pd.read_sql_query(query + " ORDER BY ts", conn, params=params)
    frame["ts"] = pd.to_datetime(frame["ts"], utc=True)
    return frame


def last_intraday_ts(conn: sqlite3.Connection, symbol: str) -> pd.Timestamp | None:
    """Start of the newest stored bar for `symbol`, or None if there is none."""
    row = conn.execute("SELECT MAX(ts) FROM intraday_bars WHERE symbol = ?", (symbol,)).fetchone()
    return None if row[0] is None else pd.Timestamp(row[0])


@dataclass(frozen=True)
class IntradayUpdate:
    """What one `update_intraday` call did for a symbol."""

    symbol: str
    rows: int
    refetched: bool  # the stored history was replaced because its adjustment changed


def _completed(bars: pd.DataFrame, now: datetime) -> pd.DataFrame:
    """Regular-session bars that had closed, and were published, by `now`."""
    kept = regular_session(bars)
    if kept.empty:
        return kept
    cutoff = _utc(now) - DATA_DELAY
    done = pd.DatetimeIndex(kept["ts"]) + BAR <= cutoff
    return kept.loc[done].reset_index(drop=True)


def _readjusted(stored: pd.DataFrame, fresh: pd.DataFrame) -> bool:
    """Whether bars present in both differ in close: the provider re-adjusted history."""
    both = stored.merge(fresh, on="ts", suffixes=("_old", "_new"))
    if both.empty:
        return False
    change = (both["close_new"] / both["close_old"] - 1.0).abs()
    return bool((change > ADJUSTMENT_TOLERANCE).any())


def update_intraday(
    conn: sqlite3.Connection,
    source: IntradaySource,
    symbol: str,
    start: datetime,
    now: datetime,
    *,
    overlap: timedelta = DEFAULT_OVERLAP,
) -> IntradayUpdate:
    """Bring `symbol` up to date with completed regular-session bars.

    Incremental: only bars newer than the last stored one are requested, plus an
    `overlap` that is compared with what is stored. Prices are adjusted, so after a
    dividend or split the provider rewrites the past; when the overlap shows that,
    the whole history since `start` is downloaded again and replaces the stored one.
    """
    init_schema(conn)
    last = last_intraday_ts(conn, symbol)
    refetched = False
    if last is None:
        bars = _completed(source.fetch_intraday(symbol, start, now), now)
    else:
        since = max(_utc(start), last - overlap)
        bars = _completed(source.fetch_intraday(symbol, since, now), now)
        if _readjusted(read_intraday(conn, symbol, start=since), bars):
            bars = _completed(source.fetch_intraday(symbol, start, now), now)
            refetched = True
    if bars.empty:
        return IntradayUpdate(symbol, 0, refetched=False)
    with conn:
        if refetched:
            conn.execute("DELETE FROM intraday_bars WHERE symbol = ?", (symbol,))
        rows = upsert_intraday(conn, symbol, bars, source.name, now)
    return IntradayUpdate(symbol, rows, refetched)


def slot_bars(bars: pd.DataFrame, asof: datetime | None = None) -> pd.DataFrame:
    """Aggregate 30-minute bars into session slots.

    Returns one row per (day, slot) that has at least one bar, oldest first, with
    columns day, slot, end (UTC scheduled end of the slot), open, high, low, close,
    volume. With `asof`, slots that had not ended by then are left out, so a slot
    is never built from half of its bars.
    """
    columns = ["day", "slot", "end", "open", "high", "low", "close", "volume"]
    kept = regular_session(bars)
    if kept.empty:
        return pd.DataFrame(columns=columns)
    stamps = pd.DatetimeIndex(pd.to_datetime(kept["ts"], utc=True))
    local = stamps.tz_convert(NEW_YORK).tz_localize(None)
    days = local.normalize()
    grid = session_slots(days.min().date(), days.max().date())
    opened = grid[grid["slot"] == 0].set_index("day")["start"].reindex(days)
    minutes = (stamps - pd.DatetimeIndex(opened)).total_seconds() // 60
    slot = [0 if m < BAR_MINUTES else 1 + int(m - BAR_MINUTES) // SLOT_MINUTES for m in minutes]
    frame = kept.assign(day=days, slot=slot).sort_values("ts")
    out = (
        frame.groupby(["day", "slot"], sort=True)
        .agg(
            open=("open", "first"),
            high=("high", "max"),
            low=("low", "min"),
            close=("close", "last"),
            volume=("volume", "sum"),
        )
        .reset_index()
    )
    out = out.merge(grid[["day", "slot", "end"]], on=["day", "slot"], how="left")
    if asof is not None:
        out = out[out["end"] <= _utc(asof)]
    return out[columns].reset_index(drop=True)


def slot_inputs(
    conn: sqlite3.Connection, symbols: Sequence[str], asof: datetime
) -> tuple[pd.DataFrame, dict[str, pd.DataFrame]]:
    """Session slot grid and per-symbol slot bars, up to the last slot that had ended by `asof`.

    The grid (columns day, slot, end) starts on the earliest stored bar of any symbol,
    so assets listed later have no bars in the early slots. Feed both results to
    `forecast.returns.build_panel`.
    """
    stamp = _utc(asof)
    starts = [last for last in (_first_ts(conn, symbol) for symbol in symbols) if last is not None]
    if not starts:
        raise ValueError("no intraday bars are stored for these symbols")
    first_day = min(starts).tz_convert(NEW_YORK).date()
    grid = session_slots(first_day, stamp.tz_convert(NEW_YORK).date())
    grid = grid[grid["end"] <= stamp].reset_index(drop=True)
    slots = {symbol: slot_bars(read_intraday(conn, symbol), asof=stamp) for symbol in symbols}
    return grid[["day", "slot", "end"]], slots


def _first_ts(conn: sqlite3.Connection, symbol: str) -> pd.Timestamp | None:
    row = conn.execute("SELECT MIN(ts) FROM intraday_bars WHERE symbol = ?", (symbol,)).fetchone()
    return None if row[0] is None else pd.Timestamp(row[0])
