"""Hourly job of the Monte Carlo paper portfolio (docs/design/mc_forecast.md).

One tick (`python -m stride.run_mc --config config.paper_mc.yaml`) is cheap when there is
nothing to do: it checks the clock against the NYSE calendar and returns. When a new
session slot (9:30-10:00, then every hour to the close) has ended and its bars are
published, it downloads the new 30-minute bars, builds the panel, replays every slot
that is new since the last run through the `Trader` (the same object the backtest
drives), and saves the trader, the trades, the equity and an audit of the forecasts
in the instance database.

The account is simulated: fills happen at the open of the slot that starts after the
decision, with the spread and commission of `config.yaml`. No order ever reaches a
broker; the module needs the paper keys only to read market data. `enabled: false` in
the instance file makes every tick a no-op.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import numpy as np
import pandas as pd

from stride.backtest.costs import commission, spread_bps
from stride.config import load_config
from stride.data import store
from stride.data.intraday import (
    BAR,
    DATA_DELAY,
    IntradaySource,
    last_intraday_ts,
    session_slots,
    slot_inputs,
    update_intraday,
)
from stride.data.intraday import (
    init_schema as init_intraday_schema,
)
from stride.data.sources import AlpacaSource, SourceError
from stride.execution.kill_switch_file import fire, is_active
from stride.forecast.config import McConfig, McConfigError, load_mc_config
from stride.forecast.returns import ReturnPanel, build_panel
from stride.forecast.summary import Forecast
from stride.forecast.walkforward import CostModel, Trader, forecast_at
from stride.reporting.alerts import Alert, Alerter, AlertError, AlertKind, alerter_from_env
from stride.risk.kill_switch import KillDecision
from stride.settings import CredentialError, load_secrets

logger = logging.getLogger(__name__)

NEW_YORK = "America/New_York"
KILL_SWITCH_FILE = "KILL_SWITCH"
SANITY_MOVE = 0.25  # a slot move above this is treated as bad data, not traded
MIN_FRESH_SHARE = 0.8  # share of the universe that must have a bar in the newest slot
LOOKBACK_DAYS = 6  # calendar days searched for the latest ended slot
AUDIT_HORIZONS = (1, 7, 35, 140)

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS mc_state (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mc_trades (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        slot_end    TEXT NOT NULL,
        symbol      TEXT NOT NULL,
        side        TEXT NOT NULL,
        notional    REAL NOT NULL,
        price       REAL NOT NULL,
        commission  REAL NOT NULL,
        spread_cost REAL NOT NULL,
        reason      TEXT NOT NULL,
        UNIQUE (slot_end, symbol, side)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mc_equity (
        slot_end TEXT PRIMARY KEY,
        day      TEXT NOT NULL,
        equity   REAL NOT NULL,
        cash     REAL NOT NULL,
        exposure REAL NOT NULL,
        drawdown REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mc_forecasts (
        slot_end TEXT NOT NULL,
        symbol   TEXT NOT NULL,
        horizon  INTEGER NOT NULL,
        p_up     REAL,
        p_net    REAL,
        mean     REAL,
        sigma    REAL,
        edge     REAL,
        PRIMARY KEY (slot_end, symbol, horizon)
    )
    """,
)


def _git_hash() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return out.stdout.strip() or "unknown"


def _default_source(root: Path) -> IntradaySource:
    """Alpaca market data with the paper keys of `.env` (they are only read, never traded)."""
    key, secret = load_secrets(root / ".env").alpaca_paper_keys()
    return AlpacaSource(api_key=key, secret_key=secret)


@dataclass
class Deps:
    """Everything the job reads from the outside world, injectable in tests."""

    now: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    source: Callable[[Path], IntradaySource] = _default_source
    alerter: Callable[[Path], Alerter] = field(
        default=lambda root: alerter_from_env(str(root / ".env"))
    )
    git_hash: Callable[[], str] = _git_hash
    monotonic: Callable[[], float] = time.monotonic


class McRunError(Exception):
    """The tick failed and should be retried at the next one."""


def init_schema(conn: sqlite3.Connection) -> None:
    """Every table the job needs: the shared ones, the 30-minute bars and its own."""
    store.init_schema(conn)
    init_intraday_schema(conn)
    with conn:
        for statement in _SCHEMA:
            conn.execute(statement)


def latest_ended_slot(now: datetime) -> pd.Timestamp | None:
    """End of the newest slot whose bars are published at `now` (None if none lately)."""
    stamp = pd.Timestamp(now).tz_convert("UTC") - pd.Timedelta(DATA_DELAY)
    today = stamp.tz_convert(NEW_YORK).date()
    grid = session_slots(today - timedelta(days=LOOKBACK_DAYS), today)
    ended = grid[grid["end"] <= stamp]
    return None if ended.empty else pd.Timestamp(ended["end"].max())


def closes_session(day: pd.Timestamp, slot: int) -> bool:
    """Whether `slot` is the last of its session (early closes have fewer slots)."""
    grid = session_slots(day.date(), day.date())
    return bool(len(grid) and slot == int(grid["slot"].max()))


def _read_state(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM mc_state WHERE key = ?", (key,)).fetchone()
    return None if row is None else str(row[0])


def _write_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO mc_state (key, value) VALUES (?, ?)"
        " ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def build_costs(root: Path, universe: Sequence[str]) -> CostModel:
    """The cost model of `config.yaml` (the one B and DIP pay) for this universe."""
    base = load_config(root / "config.yaml", env={})
    spreads = np.array([spread_bps(symbol, base) for symbol in universe])
    return CostModel(spreads, lambda shares, price: commission(shares, price, base))


def _iso(stamp: pd.Timestamp) -> str:
    return stamp.tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%SZ")


def _save_trades(conn: sqlite3.Connection, trader: Trader) -> list[dict[str, Any]]:
    trades = [dict(t) for t in trader.book.trades]
    for trade in trades:
        conn.execute(
            "INSERT OR IGNORE INTO mc_trades (slot_end, symbol, side, notional, price, commission,"
            " spread_cost, reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                _iso(cast(pd.Timestamp, trade["end"])),
                trade["symbol"],
                trade["side"],
                trade["notional"],
                trade["price"],
                trade["commission"],
                trade["spread_cost"],
                trade["reason"],
            ),
        )
    trader.book.trades.clear()
    return trades


def _save_forecast(
    conn: sqlite3.Connection, end: pd.Timestamp, symbols: Sequence[str], forecast: Forecast
) -> None:
    rows = []
    for horizon in AUDIT_HORIZONS:
        summary = forecast.by_horizon.get(horizon)
        if summary is None:
            continue
        for asset, symbol in enumerate(symbols):
            values = [
                summary.p_up[asset],
                summary.p_net[asset],
                summary.mean[asset],
                summary.sigma[asset],
                summary.edge[asset],
            ]
            clean = [None if not np.isfinite(v) else float(v) for v in values]
            rows.append((_iso(end), symbol, horizon, *clean))
    conn.executemany(
        "INSERT OR REPLACE INTO mc_forecasts (slot_end, symbol, horizon, p_up, p_net, mean,"
        " sigma, edge) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )


def _event(conn: sqlite3.Connection, kind: str, detail: str) -> None:
    store.record_event(conn, kind, detail)


def forward_calibration(
    conn: sqlite3.Connection, universe: Sequence[str], asof: datetime
) -> pd.DataFrame:
    """The audited forecasts that have matured, against what happened (live counterpart of stage A).

    One row per horizon with the cases, the Brier score of the probabilities of an increase
    that the account saw, the Brier score of the null model (always 50%), and the skill
    against it. Cases of neighbouring hours overlap, so the figures are indicative; the
    walk-forward in `docs/design/mc_backtest_results.md` is the reference.
    """
    columns = ["horizon", "cases", "brier", "brier_null", "skill_vs_null", "mean_p_up", "freq_up"]
    audit = pd.read_sql_query(
        "SELECT slot_end, symbol, horizon, p_up FROM mc_forecasts WHERE p_up IS NOT NULL", conn
    )
    if audit.empty:
        return pd.DataFrame(columns=columns)
    grid, slots = slot_inputs(conn, universe, asof)
    panel = build_panel(slots, grid)
    row_of = {stamp.strftime("%Y-%m-%dT%H:%M:%SZ"): row for row, stamp in enumerate(panel.end)}
    column_of = {symbol: column for column, symbol in enumerate(panel.symbols)}
    by_horizon: dict[int, list[tuple[float, float]]] = {}
    for slot_end, symbol, horizon, p_up in zip(
        audit["slot_end"].tolist(),
        audit["symbol"].tolist(),
        audit["horizon"].tolist(),
        audit["p_up"].tolist(),
        strict=True,
    ):
        row, column = row_of.get(slot_end), column_of.get(symbol)
        if row is None or column is None or row + int(horizon) >= len(panel):
            continue
        first, last = panel.close[row, column], panel.close[row + int(horizon), column]
        if np.isfinite(first) and np.isfinite(last) and first > 0:
            by_horizon.setdefault(int(horizon), []).append((float(p_up), float(last > first)))
    out = []
    for horizon in sorted(by_horizon):
        said = np.array([p for p, _ in by_horizon[horizon]])
        up = np.array([u for _, u in by_horizon[horizon]])
        brier = float(np.mean((said - up) ** 2))
        null = float(np.mean((0.5 - up) ** 2))
        skill = 1.0 - brier / null if null > 0 else 0.0
        out.append((horizon, len(said), brier, null, skill, float(said.mean()), float(up.mean())))
    return pd.DataFrame(out, columns=columns)


class McJob:
    """One tick of the Monte Carlo paper instance."""

    def __init__(self, config_path: Path, deps: Deps | None = None) -> None:
        self.path = config_path.resolve()
        self.root = self.path.parent
        self.config: McConfig = load_mc_config(self.path)
        self.deps = deps or Deps()
        self.db = self.root / self.config.db_path
        self.kill_file = self.root / f"{KILL_SWITCH_FILE}_MC"

    def alert(self, kind: AlertKind, text: str) -> None:
        """Send an alert tagged MC; a delivery failure is logged, never raised."""
        try:
            self.deps.alerter(self.root).send(Alert(kind, f"MC: {text}"))
        except AlertError:
            logger.warning("alert could not be delivered: %s", kind)

    def killed(self) -> bool:
        """Whether this instance (`KILL_SWITCH_MC`) or all of them (`KILL_SWITCH`) are stopped."""
        return is_active(self.kill_file) or is_active(self.root / KILL_SWITCH_FILE)

    # --- one tick -------------------------------------------------------------

    def run(self, out: Callable[[str], None]) -> int:
        """One tick: returns 0 when done or waiting, 2 when it failed and will be retried."""
        cfg = self.config
        if not cfg.enabled:
            out("STRIDE MC: disabled in the config (enabled: false); nothing to do")
            return 0
        now = self.deps.now()
        newest = latest_ended_slot(now)
        if newest is None:
            out("STRIDE MC: no session slot has ended lately")
            return 0
        self.db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db)
        try:
            init_schema(conn)
            last = _read_state(conn, "last_end")
            if last is not None and pd.Timestamp(last) >= newest:
                out("STRIDE MC: up to date")
                return 0
            started = _iso(pd.Timestamp(now))
            try:
                detail = self._process(conn, now, newest)
            except (McRunError, SourceError, CredentialError) as exc:
                store.record_run(
                    conn,
                    now.date(),
                    "mc",
                    "paper",
                    "error",
                    started,
                    _iso(pd.Timestamp(self.deps.now())),
                    self.deps.git_hash(),
                    str(exc),
                )
                self.alert(
                    "DATA_FAILED" if isinstance(exc, SourceError) else "RUN_FAILED", str(exc)
                )
                out(f"STRIDE MC: error: {exc}")
                return 2
            except Exception as exc:
                conn.rollback()
                logger.exception("the MC tick failed")
                store.record_run(
                    conn,
                    now.date(),
                    "mc",
                    "paper",
                    "error",
                    started,
                    _iso(pd.Timestamp(self.deps.now())),
                    self.deps.git_hash(),
                    f"{type(exc).__name__}: {exc}",
                )
                self.alert("RUN_FAILED", f"unexpected {type(exc).__name__}; see the log")
                out(f"STRIDE MC: unexpected error: {type(exc).__name__}")
                return 2
            status = "no_data" if detail.startswith("no data") else "ok"
            store.record_run(
                conn,
                now.date(),
                "mc",
                "paper",
                status,
                started,
                _iso(pd.Timestamp(self.deps.now())),
                self.deps.git_hash(),
                detail,
            )
            out(f"STRIDE MC: {detail}")
            return 0
        finally:
            conn.close()

    def _download(self, conn: sqlite3.Connection, now: datetime) -> list[str]:
        """Bring every symbol up to date within the tick budget; returns the ones left.

        The most out of date symbols go first, so a first start (20 to 80 s per symbol for
        400 days) finishes over a few ticks without holding up the other instances.
        """
        source = self.deps.source(self.root)
        start = now - timedelta(days=self.config.history_days)
        epoch = pd.Timestamp("1970-01-01", tz="UTC")
        order = sorted(
            self.config.universe, key=lambda symbol: last_intraday_ts(conn, symbol) or epoch
        )
        began = self.deps.monotonic()
        failed: list[str] = []
        left: list[str] = []
        for number, symbol in enumerate(order):
            if self.deps.monotonic() - began > self.config.download_budget_s:
                left = order[number:]
                break
            try:
                update_intraday(conn, source, symbol, start, now)
            except SourceError as exc:
                failed.append(f"{symbol} ({exc})")
        cutoff = _iso(pd.Timestamp(start))
        with conn:
            conn.execute("DELETE FROM intraday_bars WHERE ts < ?", (cutoff,))
        if failed:
            raise SourceError("no bars for " + ", ".join(failed))
        return left

    def _panel(self, conn: sqlite3.Connection, now: datetime) -> ReturnPanel:
        asof = pd.Timestamp(now).tz_convert("UTC") - pd.Timedelta(DATA_DELAY)
        grid, slots = slot_inputs(conn, self.config.universe, asof.to_pydatetime())
        return build_panel(slots, grid)

    def _check_data(
        self, conn: sqlite3.Connection, panel: ReturnPanel, newest: pd.Timestamp
    ) -> str | None:
        """A reason to wait for the next tick (None = go ahead); bad data raises.

        The slot is only used once most of the universe has the closing bar of the
        slot: a slot built from its first half hour alone would be a partial one.
        """
        universe = self.config.universe
        if len(panel) == 0 or panel.end[-1] < newest:
            return "no data: the newest slot is not published yet"
        last_bar = _iso(newest - BAR)
        fresh = conn.execute(
            "SELECT COUNT(DISTINCT symbol) FROM intraday_bars WHERE ts = ?", (last_bar,)
        ).fetchone()[0]
        if fresh < MIN_FRESH_SHARE * len(universe):
            return f"no data: only {fresh} of {len(universe)} assets have the closing bar yet"
        moves = np.abs(panel.ret[-1])
        if np.nanmax(moves) > SANITY_MOVE:
            worst = panel.symbols[int(np.nanargmax(moves))]
            raise McRunError(f"{worst} moved more than {SANITY_MOVE:.0%} in one slot: bad data")
        return None

    def _load_trader(
        self, conn: sqlite3.Connection, now: datetime, panel: ReturnPanel, costs: CostModel
    ) -> tuple[Trader, list[int]]:
        """The saved account and the panel rows still to replay (a new account: the last)."""
        cfg = self.config
        rule = cfg.rule()
        saved = _read_state(conn, "trader")
        if saved is None:
            trader = Trader(
                rule,
                costs,
                initial_equity=cfg.capital_usd,
                kill_switch_dd=cfg.kill_switch_dd,
                size=len(cfg.universe),
            )
            _write_state(conn, "started_at", _iso(pd.Timestamp(now)))
            _write_state(conn, "universe", json.dumps(list(cfg.universe)))
            return trader, [len(panel) - 1]  # the history only feeds the forecasts
        if json.loads(_read_state(conn, "universe") or "[]") != list(cfg.universe):
            raise McRunError("the universe changed after the account started")
        trader = Trader.from_state(
            json.loads(saved), rule, costs, kill_switch_dd=cfg.kill_switch_dd
        )
        last = pd.Timestamp(_read_state(conn, "last_end") or "1970-01-01T00:00:00Z")
        return trader, [r for r in range(len(panel)) if panel.end[r] > last]

    def _record_row(
        self, conn: sqlite3.Connection, trader: Trader, panel: ReturnPanel, row: int
    ) -> list[tuple[str, str]]:
        """Equity of slot `row` and the trades that filled at its open (not committed yet).

        Returns the events to log once the whole tick is committed.
        """
        equity, exposure = trader.equity_now(panel, row)
        peak = max(trader.book.peak, equity)
        conn.execute(
            "INSERT OR REPLACE INTO mc_equity (slot_end, day, equity, cash, exposure, drawdown)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                _iso(panel.end[row]),
                panel.day[row].date().isoformat(),
                equity,
                float(trader.book.cash),
                exposure,
                1.0 - equity / peak if peak > 0 else 0.0,
            ),
        )
        return [
            (
                "MC_TRADE",
                f"{trade['side']} {trade['symbol']} {trade['notional']:.2f} USD at "
                f"{trade['price']:.2f} ({trade['reason']})",
            )
            for trade in _save_trades(conn, trader)
        ]

    def _replay(
        self,
        conn: sqlite3.Connection,
        trader: Trader,
        panel: ReturnPanel,
        rows: list[int],
        costs: CostModel,
    ) -> tuple[int, Forecast | None, bool, list[tuple[str, str]]]:
        """Run the new slots through the trader.

        Returns the forecasts made, the last one, whether the kill switch fired and the
        events to log after the commit.
        """
        cfg = self.config
        params = cfg.forecast_params()
        decided = 0
        last_forecast: Forecast | None = None
        newly_stopped = False
        events: list[tuple[str, str]] = []
        for row in rows:
            trader.open_slot(panel, row)
            closes = closes_session(panel.day[row], int(panel.slot[row]))
            forecast = None
            if row >= len(panel) - cfg.catchup_slots and not trader.stopped:
                forecast = forecast_at(
                    panel,
                    row,
                    costs,
                    order_usd=cfg.forecast.order_usd,
                    params=params,
                    seed=cfg.forecast.seed,
                    salt=int(panel.end[row].timestamp()),
                )
                decided += forecast is not None
                last_forecast = forecast if forecast is not None else last_forecast
            was_stopped = trader.stopped
            trader.decide(panel, row, forecast, closes_session=closes)
            trader.close_slot(panel, row, session_end=closes)
            newly_stopped = newly_stopped or (trader.stopped and not was_stopped)
            events += self._record_row(conn, trader, panel, row)
        return decided, last_forecast, newly_stopped, events

    def _process(self, conn: sqlite3.Connection, now: datetime, newest: pd.Timestamp) -> str:
        cfg = self.config
        self._download(conn, now)
        empty = [s for s in cfg.universe if last_intraday_ts(conn, s) is None]
        if empty:  # a first start still downloading: the symbols left have no history yet
            done = len(cfg.universe) - len(empty)
            return f"no data: still downloading history ({done} of {len(cfg.universe)} symbols)"
        panel = self._panel(conn, now)
        wait = self._check_data(conn, panel, newest)
        if wait is not None:
            return wait
        costs = build_costs(self.root, cfg.universe)
        trader, rows = self._load_trader(conn, now, panel, costs)
        events: list[tuple[str, str]] = []
        if self.killed() and not trader.stopped:
            trader.liquidate(reason="KILL_SWITCH_FILE")
            events.append(("MC_KILL_FILE", "the KILL_SWITCH file is present: liquidating"))
        decided, last_forecast, newly_stopped, replay_events = self._replay(
            conn, trader, panel, rows, costs
        )
        end = panel.end[rows[-1]]
        if last_forecast is not None:
            _save_forecast(conn, end, panel.symbols, last_forecast)
        _write_state(conn, "trader", json.dumps(trader.to_state()))
        _write_state(conn, "last_end", _iso(end))
        conn.commit()  # the whole tick lands together or not at all
        for kind, detail in [*events, *replay_events]:
            _event(conn, kind, detail)
        if newly_stopped:
            self._fire_kill_switch(conn, trader, panel, rows[-1])
        if closes_session(panel.day[rows[-1]], int(panel.slot[rows[-1]])):
            self.alert("INFO", self.summary(conn))
        return (
            f"{len(rows)} slot(s) processed up to {end:%Y-%m-%d %H:%M} UTC, "
            f"{decided} forecast(s), equity {trader.equity_now(panel, rows[-1])[0]:.2f} USD"
        )

    def _fire_kill_switch(
        self, conn: sqlite3.Connection, trader: Trader, panel: ReturnPanel, row: int
    ) -> None:
        equity, _ = trader.equity_now(panel, row)
        drawdown = 1.0 - equity / trader.book.peak if trader.book.peak > 0 else 1.0
        decision = KillDecision(True, False, ("drawdown",), drawdown)
        if fire(conn, decision, self.kill_file):
            self.alert(
                "KILL_SWITCH",
                f"drawdown {drawdown:.1%} reached the {self.config.kill_switch_dd:.0%} limit; "
                "selling everything at the next open and stopping",
            )

    # --- reports --------------------------------------------------------------

    def summary(self, conn: sqlite3.Connection) -> str:
        """A short status for the alert at each close and for `--report`."""
        cfg = self.config
        row = conn.execute(
            "SELECT slot_end, equity, cash, exposure, drawdown FROM mc_equity"
            " ORDER BY slot_end DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return "no equity recorded yet"
        state = json.loads(_read_state(conn, "trader") or "{}")
        held = [
            f"{symbol} {shares:.3f}"
            for symbol, shares in zip(cfg.universe, state.get("shares", []), strict=False)
            if shares > 0
        ]
        today = str(row[0])[:10]
        trades = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(commission + spread_cost), 0) FROM mc_trades"
            " WHERE slot_end >= ?",
            (today,),
        ).fetchone()
        started = conn.execute("SELECT MIN(slot_end) FROM mc_equity").fetchone()[0]
        total = row[1] / cfg.capital_usd - 1.0
        text = (
            f"close {row[0]}: equity {row[1]:.2f} USD ({total:+.2%} since the start, "
            f"{started}), cash {row[2]:.2f}, invested {row[3]:.0%}, drawdown {row[4]:.1%}. "
            f"Trades today: {trades[0]} (costs {trades[1]:.2f} USD). "
            f"Positions: {', '.join(held) if held else 'none (cash)'}."
        )
        if state.get("stopped"):
            text += " STOPPED by the kill switch."
        return text

    def report(self, out: Callable[[str], None]) -> int:
        """Print the status of the account and its latest trades."""
        if not self.db.exists():
            out("STRIDE MC: no database yet")
            return 1
        conn = sqlite3.connect(self.db)
        try:
            init_schema(conn)
            out(self.summary(conn))
            for line in conn.execute(
                "SELECT slot_end, symbol, side, notional, price, reason FROM mc_trades"
                " ORDER BY id DESC LIMIT 10"
            ):
                out("  trade " + " | ".join(str(x) for x in line))
            forward = forward_calibration(conn, self.config.universe, self.deps.now())
            if forward.empty:
                out("forward calibration: no audited forecast has matured yet")
            for case in forward.itertuples(index=False):
                out(
                    f"forward calibration, horizon {case.horizon} slots: {case.cases} cases, "
                    f"P(up) said {case.mean_p_up:.3f}, happened {case.freq_up:.3f}, "
                    f"Brier {case.brier:.4f} against {case.brier_null:.4f} for the null, "
                    f"skill {case.skill_vs_null:+.4f}"
                )
        finally:
            conn.close()
        return 0


def main(argv: Sequence[str] | None = None, deps: Deps | None = None) -> int:
    """Entry point: `python -m stride.run_mc --config config.paper_mc.yaml [--report]`."""
    parser = argparse.ArgumentParser(prog="python -m stride.run_mc")
    parser.add_argument("--config", default="config.paper_mc.yaml")
    parser.add_argument("--report", action="store_true", help="print the status and stop")
    args = parser.parse_args(argv)

    def out(line: str) -> None:
        """Write one line to the standard output."""
        sys.stdout.write(line + "\n")

    try:
        job = McJob(Path(args.config), deps)
        return job.report(out) if args.report else job.run(out)
    except McConfigError as exc:
        out(f"STRIDE MC: invalid config: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
