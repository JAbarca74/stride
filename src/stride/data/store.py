"""SQLite storage for daily prices (SPEC section 5, `prices` table)."""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import pandas as pd

PRICE_COLUMNS = (
    "symbol",
    "date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "source",
    "fetched_at",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS prices (
    symbol     TEXT    NOT NULL,
    date       TEXT    NOT NULL,
    open       REAL    NOT NULL,
    high       REAL    NOT NULL,
    low        REAL    NOT NULL,
    close      REAL    NOT NULL,
    volume     INTEGER NOT NULL CHECK (volume >= 0),
    source     TEXT    NOT NULL,
    fetched_at TEXT    NOT NULL,
    PRIMARY KEY (symbol, date),
    CHECK (low <= open AND open <= high),
    CHECK (low <= close AND close <= high)
)
"""

_UPSERT = f"""
INSERT INTO prices ({", ".join(PRICE_COLUMNS)})
VALUES ({", ".join("?" for _ in PRICE_COLUMNS)})
ON CONFLICT (symbol, date) DO UPDATE SET
    open = excluded.open,
    high = excluded.high,
    low = excluded.low,
    close = excluded.close,
    volume = excluded.volume,
    source = excluded.source,
    fetched_at = excluded.fetched_at
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open the database at `db_path` (creating parent folders) and ensure the schema."""
    path = Path(db_path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    init_schema(conn)
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """Create the `prices` table if it does not exist."""
    with conn:
        conn.execute(_SCHEMA)


def upsert_prices(conn: sqlite3.Connection, prices: pd.DataFrame) -> int:
    """Insert or update rows keyed by (symbol, date); returns the number of rows written.

    `prices` must have the section 5 contract columns. `date` may be a column or the
    index; `fetched_at` must be a UTC timestamp.
    """
    frame = prices.reset_index() if "date" not in prices.columns else prices
    missing = [c for c in PRICE_COLUMNS if c not in frame.columns]
    if missing:
        raise ValueError(f"missing price columns: {missing}")
    # Reject bad downloads instead of coercing them into plausible rows.
    empty = [c for c in PRICE_COLUMNS if frame[c].isna().any()]
    if empty:
        raise ValueError(f"empty values in price columns: {empty}")
    volume = pd.to_numeric(frame["volume"])
    if not (volume == volume.round()).all():
        raise ValueError("volume must be a whole number")
    rows = list(
        zip(
            frame["symbol"].astype(str).tolist(),
            pd.to_datetime(frame["date"]).dt.strftime("%Y-%m-%d").tolist(),
            *(frame[c].astype(float).tolist() for c in ("open", "high", "low", "close")),
            volume.astype("int64").tolist(),
            frame["source"].astype(str).tolist(),
            [ts.isoformat() for ts in pd.to_datetime(frame["fetched_at"], utc=True)],
            strict=True,
        )
    )
    with conn:
        conn.executemany(_UPSERT, rows)
    return len(rows)


def read_prices(
    conn: sqlite3.Connection,
    symbol: str,
    start: date | None = None,
    end: date | None = None,
) -> pd.DataFrame:
    """Prices for `symbol` indexed by `date` (timezone-naive), oldest first."""
    query = "SELECT * FROM prices WHERE symbol = ?"
    params: list[str] = [symbol]
    # Bind plain ISO dates: a Timestamp's isoformat() adds "T00:00:00", which sorts
    # after the stored "YYYY-MM-DD" text and would drop the start day.
    if start is not None:
        query += " AND date >= ?"
        params.append(pd.Timestamp(start).date().isoformat())
    if end is not None:
        query += " AND date <= ?"
        params.append(pd.Timestamp(end).date().isoformat())
    query += " ORDER BY date"
    frame = pd.read_sql_query(query, conn, params=params)
    frame["date"] = pd.to_datetime(frame["date"])
    frame["fetched_at"] = pd.to_datetime(frame["fetched_at"], utc=True)
    return frame.set_index("date")
