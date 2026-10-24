"""SQLite storage for daily prices (SPEC section 5, `prices` table)."""

from __future__ import annotations

import math
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


_CASH_FLOWS_SCHEMA = """
CREATE TABLE IF NOT EXISTS cash_flows (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    date        TEXT    NOT NULL,
    amount      REAL    NOT NULL CHECK (amount > 0),
    type        TEXT    NOT NULL CHECK (type IN ('deposit', 'withdrawal')),
    detected_by TEXT    NOT NULL,
    recorded_at TEXT    NOT NULL
)
"""

CASH_FLOW_TYPES = ("deposit", "withdrawal")

_EQUITY_SCHEMA = """
CREATE TABLE IF NOT EXISTS equity (
    date           TEXT PRIMARY KEY,
    equity         REAL NOT NULL,
    cash           REAL NOT NULL,
    gross_exposure REAL NOT NULL CHECK (gross_exposure >= 0),
    leverage       REAL NOT NULL CHECK (leverage >= 0)
)
"""
EQUITY_COLUMNS = ("equity", "cash", "gross_exposure", "leverage")

_RISK_STATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS risk_state (
    key        TEXT PRIMARY KEY,
    value      REAL NOT NULL,
    updated_at TEXT NOT NULL
)
"""

_EVENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    at     TEXT NOT NULL,
    type   TEXT NOT NULL,
    detail TEXT NOT NULL
)
"""


_SIGNALS_SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    date          TEXT    NOT NULL,
    symbol        TEXT    NOT NULL,
    close         REAL    NOT NULL,
    sma200        REAL,
    hh55          REAL,
    ll20          REAL,
    n             REAL,
    rule          TEXT    NOT NULL,
    current_units INTEGER NOT NULL CHECK (current_units >= 0),
    target_units  INTEGER NOT NULL CHECK (target_units >= 0),
    last_entry    REAL,
    capped        INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (date, symbol)
)
"""
SIGNAL_TABLE_COLUMNS = (
    "close",
    "sma200",
    "hh55",
    "ll20",
    "n",
    "rule",
    "current_units",
    "target_units",
    "last_entry",
    "capped",
)

_RISK_DECISIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS risk_decisions (
    date           TEXT PRIMARY KEY,
    l_star         REAL,
    kelly_days     INTEGER NOT NULL,
    sigma_60       REAL    NOT NULL,
    leverage       REAL    NOT NULL CHECK (leverage >= 0),
    binding_limit  TEXT    NOT NULL,
    drawdown       REAL    NOT NULL,
    delever_active INTEGER NOT NULL,
    k              REAL    NOT NULL,
    vetoes         TEXT    NOT NULL
)
"""
RISK_DECISION_COLUMNS = (
    "l_star",
    "kelly_days",
    "sigma_60",
    "leverage",
    "binding_limit",
    "drawdown",
    "delever_active",
    "k",
    "vetoes",
)


# Orders and fills (SPEC 10), with the columns proposed for execution/orders.py
# in docs/design/execution_modules.md (PR #45, section 1e).
_ORDERS_SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    date        TEXT    NOT NULL,
    client_id   TEXT    NOT NULL UNIQUE,
    broker_id   TEXT,
    symbol      TEXT    NOT NULL,
    side        TEXT    NOT NULL CHECK (side IN ('buy', 'sell')),
    notional    REAL    NOT NULL CHECK (notional > 0),
    type        TEXT    NOT NULL,
    limit_price REAL,
    rule        TEXT    NOT NULL,
    switch      INTEGER NOT NULL DEFAULT 0,
    reasons     TEXT    NOT NULL DEFAULT '',
    status      TEXT    NOT NULL,
    quantity    REAL,
    full_exit   INTEGER NOT NULL DEFAULT 0
)
"""
# Columns added after the first schema, with their definition; `init_schema` adds
# them to an existing table (`quantity`: shares for a whole-share or full-exit
# order; `full_exit`: the order closes the position, for the next-day resend).
_ORDER_EXTRA_COLUMNS = {"quantity": "REAL", "full_exit": "INTEGER NOT NULL DEFAULT 0"}
ORDER_COLUMNS = (
    "date",
    "client_id",
    "broker_id",
    "symbol",
    "side",
    "notional",
    "type",
    "limit_price",
    "rule",
    "switch",
    "reasons",
    "status",
)
# planned: approved at the evening run, waiting for the morning; sending: stored
# right before the broker call; pending: accepted by the broker; unknown: the
# send failed and the lookup failed too (resolved by `find_order` on the next
# run); failed: the send failed and the broker has no such order; skipped: not
# sent (kill switch, halt, too small); resend: an unfilled exit to resend.
ORDER_STATUSES = (
    "planned",
    "vetoed",
    "sending",
    "pending",
    "unknown",
    "failed",
    "skipped",
    "filled",
    "cancelled",
    "rejected",
    "resend",
)

_FILLS_SCHEMA = """
CREATE TABLE IF NOT EXISTS fills (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    date              TEXT NOT NULL,
    broker_id         TEXT NOT NULL,
    symbol            TEXT NOT NULL,
    side              TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    quantity          REAL NOT NULL CHECK (quantity > 0),
    price             REAL NOT NULL CHECK (price > 0),
    commission        REAL NOT NULL CHECK (commission >= 0),
    slippage_vs_close REAL
)
"""
FILL_COLUMNS = (
    "date",
    "broker_id",
    "symbol",
    "side",
    "quantity",
    "price",
    "commission",
    "slippage_vs_close",
)


_RUNS_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    date     TEXT NOT NULL,
    phase    TEXT NOT NULL,
    mode     TEXT NOT NULL,
    status   TEXT NOT NULL CHECK (status IN ('ok', 'error', 'no_data', 'skipped')),
    started  TEXT NOT NULL,
    finished TEXT NOT NULL,
    git_hash TEXT NOT NULL,
    detail   TEXT NOT NULL DEFAULT ''
)
"""
RUN_STATUSES = ("ok", "error", "no_data", "skipped")

# Paper-run state between days (P4-4): the instrument chosen per asset each day,
# for the hold days of the leveraged-ETF hysteresis, and the weights of each
# allocation, for its rules and its period.
_INSTRUMENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS instruments (
    date     TEXT    NOT NULL,
    asset    TEXT    NOT NULL,
    symbol   TEXT    NOT NULL,
    multiple INTEGER NOT NULL CHECK (multiple >= 1),
    PRIMARY KEY (date, asset)
)
"""
_ALLOCATIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS allocations (
    date   TEXT NOT NULL,
    asset  TEXT NOT NULL,
    weight REAL NOT NULL,
    PRIMARY KEY (date, asset)
)
"""
# A (date, phase) with one of these is done: `run_daily --phase auto` skips it.
DONE_RUN_STATUSES = ("ok", "no_data", "skipped")


def init_schema(conn: sqlite3.Connection) -> None:
    """Create every table (prices, cash_flows, equity, risk_state, events, signals,
    risk_decisions, orders, fills, runs, instruments, allocations) if missing,
    and add the newer `orders` columns to a table created before them."""
    with conn:
        conn.execute(_SCHEMA)
        conn.execute(_CASH_FLOWS_SCHEMA)
        conn.execute(_EQUITY_SCHEMA)
        conn.execute(_RISK_STATE_SCHEMA)
        conn.execute(_EVENTS_SCHEMA)
        conn.execute(_SIGNALS_SCHEMA)
        conn.execute(_RISK_DECISIONS_SCHEMA)
        conn.execute(_ORDERS_SCHEMA)
        conn.execute(_FILLS_SCHEMA)
        conn.execute(_RUNS_SCHEMA)
        conn.execute(_INSTRUMENTS_SCHEMA)
        conn.execute(_ALLOCATIONS_SCHEMA)
        have = {row[1] for row in conn.execute("PRAGMA table_info(orders)")}
        for name, definition in _ORDER_EXTRA_COLUMNS.items():
            if name not in have:
                conn.execute(f"ALTER TABLE orders ADD COLUMN {name} {definition}")


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


def last_price_date(conn: sqlite3.Connection, symbol: str) -> date | None:
    """Date of the newest stored bar for `symbol`, or None if there is none."""
    row = conn.execute("SELECT MAX(date) FROM prices WHERE symbol = ?", (symbol,)).fetchone()
    return None if row[0] is None else date.fromisoformat(row[0])


def record_cash_flow(
    conn: sqlite3.Connection,
    day: date,
    amount: float,
    kind: str,
    detected_by: str,
    *,
    recorded_at: pd.Timestamp | None = None,
) -> None:
    """Store one deposit or withdrawal (SPEC section 10). `amount` is positive;
    `detected_by` says how it was found (e.g. "cash increase without a sale")."""
    if kind not in CASH_FLOW_TYPES:
        raise ValueError(f"type must be one of {CASH_FLOW_TYPES}")
    stamp = recorded_at if recorded_at is not None else pd.Timestamp.now(tz="UTC")
    with conn:
        conn.execute(
            "INSERT INTO cash_flows (date, amount, type, detected_by, recorded_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (day.isoformat(), float(amount), kind, detected_by, stamp.isoformat()),
        )


def net_cash_flows(conn: sqlite3.Connection) -> pd.Series:
    """Net external flow per day: deposits positive, withdrawals negative."""
    frame = pd.read_sql_query(
        "SELECT date, SUM(CASE type WHEN 'deposit' THEN amount ELSE -amount END) AS net"
        " FROM cash_flows GROUP BY date ORDER BY date",
        conn,
    )
    return pd.Series(frame["net"].to_numpy(dtype=float), index=pd.to_datetime(frame["date"]))


def upsert_equity(conn: sqlite3.Connection, equity: pd.DataFrame) -> int:
    """Store one row per day (SPEC section 10): equity, cash, gross exposure, leverage.

    `equity` is indexed by date; a day already stored is replaced.
    """
    missing = [c for c in EQUITY_COLUMNS if c not in equity.columns]
    if missing:
        raise ValueError(f"missing equity columns: {missing}")
    rows = list(
        zip(
            pd.DatetimeIndex(equity.index).strftime("%Y-%m-%d").tolist(),
            *(equity[c].astype(float).tolist() for c in EQUITY_COLUMNS),
            strict=True,
        )
    )
    with conn:
        conn.executemany(
            "INSERT INTO equity (date, equity, cash, gross_exposure, leverage)"
            " VALUES (?, ?, ?, ?, ?) ON CONFLICT (date) DO UPDATE SET"
            " equity = excluded.equity, cash = excluded.cash,"
            " gross_exposure = excluded.gross_exposure, leverage = excluded.leverage",
            rows,
        )
    return len(rows)


def read_equity(
    conn: sqlite3.Connection, start: date | None = None, end: date | None = None
) -> pd.DataFrame:
    """Daily equity rows indexed by date, oldest first."""
    query = "SELECT * FROM equity WHERE 1 = 1"
    params: list[str] = []
    if start is not None:
        query += " AND date >= ?"
        params.append(pd.Timestamp(start).date().isoformat())
    if end is not None:
        query += " AND date <= ?"
        params.append(pd.Timestamp(end).date().isoformat())
    frame = pd.read_sql_query(query + " ORDER BY date", conn, params=params)
    frame["date"] = pd.to_datetime(frame["date"])
    return frame.set_index("date")


def _now() -> str:
    return pd.Timestamp.now(tz="UTC").isoformat()


def record_event(conn: sqlite3.Connection, kind: str, detail: str, at: str | None = None) -> None:
    """Append one row to `events` (SPEC section 10): alerts, errors, kill switch, resets."""
    with conn:
        conn.execute(
            "INSERT INTO events (at, type, detail) VALUES (?, ?, ?)", (at or _now(), kind, detail)
        )


def read_events(conn: sqlite3.Connection, kind: str | None = None) -> list[tuple[str, str, str]]:
    query = "SELECT at, type, detail FROM events"
    params: tuple[str, ...] = ()
    if kind is not None:
        query += " WHERE type = ?"
        params = (kind,)
    return [tuple(row) for row in conn.execute(query + " ORDER BY id", params).fetchall()]


TWR_INDEX = "twr_index"
TWR_PEAK = "twr_peak"


def load_twr(conn: sqlite3.Connection) -> tuple[float, float]:
    """Stored time-weighted index and its peak; (1.0, 1.0) before the first run."""
    rows = dict(conn.execute("SELECT key, value FROM risk_state").fetchall())
    return float(rows.get(TWR_INDEX, 1.0)), float(rows.get(TWR_PEAK, 1.0))


def save_twr(conn: sqlite3.Connection, index: float, peak: float) -> None:
    """Store the index and peak. The peak never goes down here; only
    `reset_twr_peak` can lower it. A non-finite or negative value is refused, so a
    bad index can never be stored as the peak."""
    if not (math.isfinite(index) and math.isfinite(peak)) or index < 0 or peak <= 0:
        raise ValueError("the index must be finite and non-negative, the peak finite and positive")
    _, stored_peak = load_twr(conn)
    stamp = _now()
    with conn:
        conn.executemany(
            "INSERT INTO risk_state (key, value, updated_at) VALUES (?, ?, ?)"
            " ON CONFLICT (key) DO UPDATE SET value = excluded.value,"
            " updated_at = excluded.updated_at",
            [(TWR_INDEX, index, stamp), (TWR_PEAK, max(peak, stored_peak), stamp)],
        )


def reset_twr_peak(conn: sqlite3.Connection, by: str, reason: str) -> None:
    """Explicit human reset of the peak to the current index (SPEC 1.3), recorded
    in `events`. Only called by the manual kill-switch reset."""
    if not by.strip():
        raise ValueError("a reset needs the name of the person doing it")
    index, peak = load_twr(conn)
    stamp = _now()
    with conn:
        conn.execute(
            "INSERT INTO risk_state (key, value, updated_at) VALUES (?, ?, ?)"
            " ON CONFLICT (key) DO UPDATE SET value = excluded.value,"
            " updated_at = excluded.updated_at",
            (TWR_PEAK, index, stamp),
        )
    record_event(conn, "PEAK_RESET", f"by {by}: {reason} (peak {peak:.6f} -> {index:.6f})", stamp)


def _none_if_nan(value: object) -> object:
    return None if isinstance(value, float) and math.isnan(value) else value


def _upsert_frame(
    conn: sqlite3.Connection, table: str, keys: tuple[str, ...], frame: pd.DataFrame,
    columns: tuple[str, ...],
) -> int:  # fmt: skip
    missing = [c for c in columns if c not in frame.columns]
    if missing:
        raise ValueError(f"missing {table} columns: {missing}")
    flat = frame.reset_index()
    flat["date"] = pd.to_datetime(flat["date"]).dt.strftime("%Y-%m-%d")
    names = (*keys, *columns)
    rows = [
        tuple(_none_if_nan(v.item() if hasattr(v, "item") else v) for v in row)
        for row in flat[list(names)].itertuples(index=False, name=None)
    ]
    updates = ", ".join(f"{c} = excluded.{c}" for c in columns)
    with conn:
        conn.executemany(
            f"INSERT INTO {table} ({', '.join(names)}) VALUES ({', '.join('?' for _ in names)})"
            f" ON CONFLICT ({', '.join(keys)}) DO UPDATE SET {updates}",
            rows,
        )
    return len(rows)


def upsert_signals(conn: sqlite3.Connection, signals: pd.DataFrame) -> int:
    """One row per asset and day (SPEC section 10): indicators, rule fired, current
    and target units. `signals` has `date` and `symbol` as columns or index levels."""
    return _upsert_frame(conn, "signals", ("date", "symbol"), signals, SIGNAL_TABLE_COLUMNS)


def upsert_risk_decisions(conn: sqlite3.Connection, decisions: pd.DataFrame) -> int:
    """One row per day (SPEC section 10): L*, effective L, binding limit, drawdown and
    the vetoes applied (a JSON list). Indexed by date."""
    return _upsert_frame(conn, "risk_decisions", ("date",), decisions, RISK_DECISION_COLUMNS)


def _insert_rows(
    conn: sqlite3.Connection, table: str, frame: pd.DataFrame, columns: tuple[str, ...]
) -> int:
    missing = [c for c in columns if c not in frame.columns]
    if missing:
        raise ValueError(f"missing {table} columns: {missing}")
    flat = frame.copy()
    flat["date"] = pd.to_datetime(flat["date"]).dt.strftime("%Y-%m-%d")
    rows = [
        tuple(_none_if_nan(v.item() if hasattr(v, "item") else v) for v in row)
        for row in flat[list(columns)].itertuples(index=False, name=None)
    ]
    with conn:
        conn.executemany(
            f"INSERT INTO {table} ({', '.join(columns)})"
            f" VALUES ({', '.join('?' for _ in columns)})",
            rows,
        )
    return len(rows)


def record_orders(conn: sqlite3.Connection, orders: pd.DataFrame) -> int:
    """Append orders (SPEC 10): one row per order planned, vetoed ones included,
    with the guard reasons and the status. `client_id` is unique. The optional
    `quantity` and `full_exit` columns are stored when present."""
    bad = set(orders["status"]) - set(ORDER_STATUSES) if not orders.empty else set()
    if bad:
        raise ValueError(f"unknown order status: {sorted(bad)}")
    extra = tuple(c for c in _ORDER_EXTRA_COLUMNS if c in orders.columns)
    return _insert_rows(conn, "orders", orders, ORDER_COLUMNS + extra)


def update_order(
    conn: sqlite3.Connection,
    client_id: str,
    status: str,
    *,
    broker_id: str | None = None,
    reasons: str | None = None,
) -> None:
    """Set an order's status (and its broker id or reasons when given)."""
    if status not in ORDER_STATUSES:
        raise ValueError(f"unknown order status: {status!r}")
    sets, params = ["status = ?"], [status]
    if broker_id is not None:
        sets.append("broker_id = ?")
        params.append(broker_id)
    if reasons is not None:
        sets.append("reasons = ?")
        params.append(reasons)
    with conn:
        changed = conn.execute(
            f"UPDATE orders SET {', '.join(sets)} WHERE client_id = ?", (*params, client_id)
        ).rowcount
    if changed != 1:
        raise ValueError(f"no order with client id {client_id!r}")


def read_orders(
    conn: sqlite3.Connection,
    statuses: tuple[str, ...] | None = None,
    day: date | None = None,
) -> pd.DataFrame:
    """Orders, oldest first, optionally only some statuses or one run date."""
    query, params = "SELECT * FROM orders WHERE 1 = 1", []
    if statuses is not None:
        query += f" AND status IN ({', '.join('?' for _ in statuses)})"
        params += list(statuses)
    if day is not None:
        query += " AND date = ?"
        params.append(day.isoformat())
    frame = pd.read_sql_query(query + " ORDER BY date, id", conn, params=params)
    frame["date"] = pd.to_datetime(frame["date"])
    return frame


def delete_unsent_orders(conn: sqlite3.Connection, day: date) -> int:
    """Remove the planned and vetoed orders of run date `day` (never sent), so a
    rerun of that day's planning replaces them."""
    with conn:
        return conn.execute(
            "DELETE FROM orders WHERE date = ? AND status IN ('planned', 'vetoed')",
            (day.isoformat(),),
        ).rowcount


def has_fill(conn: sqlite3.Connection, broker_id: str) -> bool:
    row = conn.execute("SELECT 1 FROM fills WHERE broker_id = ? LIMIT 1", (broker_id,)).fetchone()
    return row is not None


def record_run(  # noqa: PLR0917 - one value per runs column
    conn: sqlite3.Connection,
    day: date,
    phase: str,
    mode: str,
    status: str,
    started: str,
    finished: str,
    git_hash: str,
    detail: str = "",
) -> None:
    """Append one row to `runs` (SPEC 10): a phase of the daily job and its outcome."""
    if status not in RUN_STATUSES:
        raise ValueError(f"run status must be one of {RUN_STATUSES}")
    with conn:
        conn.execute(
            "INSERT INTO runs (date, phase, mode, status, started, finished, git_hash, detail)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (day.isoformat(), phase, mode, status, started, finished, git_hash, detail),
        )


def run_done(conn: sqlite3.Connection, day: date, phase: str) -> bool:
    """Whether the phase already finished for `day` (ok, no data or skipped)."""
    row = conn.execute(
        f"SELECT 1 FROM runs WHERE date = ? AND phase = ? AND status IN"
        f" ({', '.join('?' for _ in DONE_RUN_STATUSES)}) LIMIT 1",
        (day.isoformat(), phase, *DONE_RUN_STATUSES),
    ).fetchone()
    return row is not None


def read_runs(conn: sqlite3.Connection) -> pd.DataFrame:
    frame = pd.read_sql_query("SELECT * FROM runs ORDER BY id", conn)
    frame["date"] = pd.to_datetime(frame["date"])
    return frame


def record_fills(conn: sqlite3.Connection, fills: pd.DataFrame) -> int:
    """Append executions (SPEC 10): price, quantity, commission and slippage
    against the previous close."""
    return _insert_rows(conn, "fills", fills, FILL_COLUMNS)


def read_table(conn: sqlite3.Connection, table: str) -> pd.DataFrame:
    """Every row of `signals`, `risk_decisions`, `orders` or `fills`, oldest first."""
    if table not in {"signals", "risk_decisions", "orders", "fills"}:
        raise ValueError(f"unknown table {table!r}")
    order = {"orders": "date, id", "fills": "date, id", "signals": "date, symbol"}.get(
        table, "date"
    )
    frame = pd.read_sql_query(f"SELECT * FROM {table} ORDER BY {order}", conn)
    frame["date"] = pd.to_datetime(frame["date"])
    return frame


def save_instruments(
    conn: sqlite3.Connection, day: date, choices: dict[str, tuple[str, int]]
) -> None:
    """The instrument (symbol, multiple) chosen per asset on `day`; replaces that day."""
    with conn:
        conn.execute("DELETE FROM instruments WHERE date = ?", (day.isoformat(),))
        conn.executemany(
            "INSERT INTO instruments (date, asset, symbol, multiple) VALUES (?, ?, ?, ?)",
            [(day.isoformat(), a, s, int(m)) for a, (s, m) in sorted(choices.items())],
        )


def read_instruments(conn: sqlite3.Connection) -> list[tuple[pd.Timestamp, str, str, int]]:
    """Every stored choice as (date, asset, symbol, multiple), oldest first."""
    rows = conn.execute(
        "SELECT date, asset, symbol, multiple FROM instruments ORDER BY date, asset"
    ).fetchall()
    return [(pd.Timestamp(d), str(a), str(s), int(m)) for d, a, s, m in rows]


def save_allocation(conn: sqlite3.Connection, day: date, weights: dict[str, float]) -> None:
    """The weights of the allocation made on `day` (none: an empty allocation)."""
    with conn:
        conn.execute("DELETE FROM allocations WHERE date = ?", (day.isoformat(),))
        rows = [(day.isoformat(), a, float(w)) for a, w in sorted(weights.items())]
        conn.executemany(
            "INSERT INTO allocations (date, asset, weight) VALUES (?, ?, ?)",
            rows or [(day.isoformat(), "", 0.0)],  # a marker row keeps the date
        )


def last_allocation(
    conn: sqlite3.Connection, before: date | None = None
) -> tuple[pd.Timestamp, dict[str, float]] | None:
    """The latest allocation (date and weights), optionally strictly before `before`."""
    query, params = "SELECT MAX(date) FROM allocations", []
    if before is not None:
        query += " WHERE date < ?"
        params.append(before.isoformat())
    row = conn.execute(query, params).fetchone()
    if row[0] is None:
        return None
    rows = conn.execute("SELECT asset, weight FROM allocations WHERE date = ?", (row[0],))
    return pd.Timestamp(row[0]), {a: float(w) for a, w in rows.fetchall() if a}
