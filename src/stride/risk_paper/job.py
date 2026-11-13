"""Daily job of the risk-allocation paper instance (docs/design/mc_risk_paper.md).

One tick (`python -m stride.run_risk --config config.paper_risk.yaml`) is cheap when
there is nothing to do: it compares the last processed session with the last completed
NYSE session and returns. When a new daily bar is due it downloads the bars, builds the
daily panel and replays every session that is new since the last run through the
`RiskTrader` (the object the backtest drives, through the same `step_session`), then
saves the account, the trades, the equity and the decision log in the instance database.

The account is simulated: an order decided after a close fills at the next session
open, with the spread and commission of `config.yaml`. No order ever reaches a broker;
the job needs the data-only key to read prices. `enabled: false` in the instance file
makes every tick a no-op.

Prices are adjusted. When a fund pays a dividend the provider rescales its whole past,
so the job keeps the last close it used for each asset (the anchor) and, when the
stored close of that day changes, multiplies the shares held by `old / new`: the
dividend reinvested at no cost, which is what the adjusted series assumes.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import cast

import numpy as np
import pandas as pd

from stride.backtest.costs import commission, spread_bps
from stride.config import load_config
from stride.data import store
from stride.data.calendar import trading_days
from stride.data.sources import PriceSource, SourceError, TiingoSource
from stride.data.update import PlanItem, last_completed_session, update_symbol
from stride.execution.kill_switch_file import fire, is_active
from stride.forecast.daily import build_daily_panel, eligible
from stride.forecast.returns import ReturnPanel
from stride.forecast.risk_walkforward import RiskTrader, step_session
from stride.forecast.walkforward import CostModel, seed_for
from stride.reporting.alerts import Alert, Alerter, AlertError, AlertKind, alerter_from_env
from stride.risk.kill_switch import KillDecision
from stride.risk_paper.config import RiskPaperConfig, load_risk_config
from stride.settings import CredentialError, load_secrets

logger = logging.getLogger(__name__)

KILL_SWITCH_FILE = "KILL_SWITCH"
PHASE = "risk"
ADJUST_TOLERANCE = 1e-6  # relative change of an anchor close that means a revised history
VERIFY_TOLERANCE = 1e-3  # largest relative equity gap a from-scratch replay may show

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS risk_paper_state (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS risk_trades (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        day         TEXT NOT NULL,
        symbol      TEXT NOT NULL,
        side        TEXT NOT NULL,
        notional    REAL NOT NULL,
        price       REAL NOT NULL,
        commission  REAL NOT NULL,
        spread_cost REAL NOT NULL,
        reason      TEXT NOT NULL,
        UNIQUE (day, symbol, side)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS risk_equity (
        day      TEXT PRIMARY KEY,
        equity   REAL NOT NULL,
        cash     REAL NOT NULL,
        exposure REAL NOT NULL,
        drawdown REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS risk_log (
        day        TEXT PRIMARY KEY,
        rebalance  INTEGER NOT NULL,
        scale      REAL NOT NULL,
        cap_scale  REAL NOT NULL,
        dd_scale   REAL NOT NULL,
        base_total REAL NOT NULL,
        held_q05   REAL NOT NULL,
        held_q01   REAL NOT NULL,
        weights    TEXT NOT NULL
    )
    """,
)
# Not `risk_state`: data/store.py already has a table of that name with other columns.
STATE_TABLE = "risk_paper_state"


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


def _default_source(root: Path) -> PriceSource:
    """Tiingo end-of-day prices with the data-only key of `.env`."""
    return TiingoSource(api_key=load_secrets(root / ".env").tiingo_key())


@dataclass
class Deps:
    """Everything the job reads from the outside world, injectable in tests."""

    now: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    source: Callable[[Path], PriceSource] = _default_source
    alerter: Callable[[Path], Alerter] = field(
        default=lambda root: alerter_from_env(str(root / ".env"))
    )
    git_hash: Callable[[], str] = _git_hash


class RiskRunError(Exception):
    """The tick failed and should be retried at the next one."""


def init_schema(conn: sqlite3.Connection) -> None:
    """Every table the job needs: the shared ones (prices, runs, events) and its own."""
    store.init_schema(conn)
    with conn:
        for statement in _SCHEMA:
            conn.execute(statement)


def _read_state(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute(f"SELECT value FROM {STATE_TABLE} WHERE key = ?", (key,)).fetchone()
    return None if row is None else str(row[0])


def _write_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        f"INSERT INTO {STATE_TABLE} (key, value) VALUES (?, ?)"
        " ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def _now_iso(moment: datetime) -> str:
    return pd.Timestamp(moment).tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%SZ")


def session_seed(base: int, day: pd.Timestamp) -> int:
    """The seed of a session: it depends on the date only, not on how the panel is cut."""
    return seed_for(base, int(day.strftime("%Y%m%d")))


def build_costs(root: Path, universe: list[str]) -> CostModel:
    """The cost model of `config.yaml` (the one B and DIP pay) for this universe."""
    base = load_config(root / "config.yaml", env={})
    spreads = np.array([spread_bps(symbol, base) for symbol in universe])
    return CostModel(spreads, lambda shares, price: commission(shares, price, base))


class RiskJob:
    """One tick of the risk-allocation paper instance."""

    def __init__(self, config_path: Path, deps: Deps | None = None) -> None:
        self.path = config_path.resolve()
        self.root = self.path.parent
        self.config: RiskPaperConfig = load_risk_config(self.path)
        self.deps = deps or Deps()
        self.db = self.root / self.config.db_path
        self.kill_file = self.root / f"{KILL_SWITCH_FILE}_RISK"

    def alert(self, kind: AlertKind, text: str) -> None:
        """Send an alert tagged RISK; a delivery failure is logged, never raised."""
        try:
            self.deps.alerter(self.root).send(Alert(kind, f"RISK: {text}"))
        except AlertError:
            logger.warning("alert could not be delivered: %s", kind)

    def killed(self) -> bool:
        """Whether this instance (`KILL_SWITCH_RISK`) or all of them (`KILL_SWITCH`) are stopped."""
        return is_active(self.kill_file) or is_active(self.root / KILL_SWITCH_FILE)

    # --- one tick -------------------------------------------------------------

    def run(self, out: Callable[[str], None]) -> int:
        """One tick: returns 0 when done or waiting, 2 when it failed and will be retried."""
        if not self.config.enabled:
            out("STRIDE RISK: disabled in the config (enabled: false); nothing to do")
            return 0
        now = self.deps.now()
        target = last_completed_session(now)
        self.db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db)
        try:
            init_schema(conn)
            last = _read_state(conn, "last_day")
            if last is not None and date.fromisoformat(last) >= target:
                out("STRIDE RISK: up to date")
                return 0
            started = _now_iso(now)

            def record(status: str, detail: str) -> None:
                store.record_run(
                    conn,
                    now.date(),
                    PHASE,
                    "paper",
                    status,
                    started,
                    _now_iso(self.deps.now()),
                    self.deps.git_hash(),
                    detail,
                )

            try:
                detail = self._process(conn, now, target)
            except (RiskRunError, SourceError, CredentialError) as exc:
                conn.rollback()
                record("error", str(exc))
                self.alert(
                    "DATA_FAILED" if isinstance(exc, SourceError) else "RUN_FAILED", str(exc)
                )
                out(f"STRIDE RISK: error: {exc}")
                return 2
            except Exception as exc:
                conn.rollback()
                logger.exception("the RISK tick failed")
                record("error", f"{type(exc).__name__}: {exc}")
                self.alert("RUN_FAILED", f"unexpected {type(exc).__name__}; see the log")
                out(f"STRIDE RISK: unexpected error: {type(exc).__name__}")
                return 2
            record("no_data" if detail.startswith("no data") else "ok", detail)
            out(f"STRIDE RISK: {detail}")
            return 0
        finally:
            conn.close()

    def _history_start(self, conn: sqlite3.Connection, target: date) -> date:
        """The first day of history kept; fixed at the first run so a replay can be redone."""
        saved = _read_state(conn, "history_start")
        if saved is not None:
            return date.fromisoformat(saved)
        start = target - timedelta(days=self.config.history_days)
        with conn:
            _write_state(conn, "history_start", start.isoformat())
        return start

    def _download(self, conn: sqlite3.Connection, start: date, target: date) -> None:
        """Bring every symbol up to `target`; a revised history is downloaded again in full."""
        source = self.deps.source(self.root)
        failed: list[str] = []
        for symbol in self.config.universe:
            try:
                update_symbol(conn, source, PlanItem(symbol, source.name, start), target)
            except SourceError as exc:
                failed.append(f"{symbol} ({exc})")
        if failed:
            raise SourceError("no bars for " + ", ".join(failed))

    def _panel(self, conn: sqlite3.Connection, start: date, target: date) -> ReturnPanel:
        bars = {s: store.read_prices(conn, s, start, target) for s in self.config.universe}
        return build_daily_panel(bars, trading_days(start, target))

    def _check_data(self, panel: ReturnPanel, rows: list[int]) -> str | None:
        """A reason to wait for the next tick (None = go ahead); bad data raises."""
        cfg = self.config
        newest = rows[-1]
        members = eligible(panel, newest, cfg.simulation.window_days)
        if not members.any():
            have = int(np.isfinite(panel.open[newest]).sum())
            if have == 0:
                return f"no data: the bar of {panel.day[newest]:%Y-%m-%d} is not published yet"
            raise RiskRunError("no asset has enough history for the simulation window")
        fresh = int(np.isfinite(panel.open[newest][members]).sum())
        if fresh < cfg.min_fresh_share * int(members.sum()):
            return (
                f"no data: only {fresh} of {int(members.sum())} assets have the bar of "
                f"{panel.day[newest]:%Y-%m-%d} yet"
            )
        moves = np.abs(np.expm1(panel.ret[rows]))
        if np.isfinite(moves).any() and np.nanmax(moves) > cfg.sanity_move:
            worst = panel.symbols[int(np.nanargmax(np.nanmax(moves, axis=0)))]
            raise RiskRunError(
                f"{worst} moved more than {cfg.sanity_move:.0%} in one day: bad data"
            )
        return None

    def _anchors(self, conn: sqlite3.Connection, upto: date) -> dict[str, list[object]]:
        """The last stored bar of each asset up to `upto`: [day, close]."""
        out: dict[str, list[object]] = {}
        for symbol in self.config.universe:
            row = conn.execute(
                "SELECT date, close FROM prices WHERE symbol = ? AND date <= ?"
                " ORDER BY date DESC LIMIT 1",
                (symbol, upto.isoformat()),
            ).fetchone()
            if row is not None:
                out[symbol] = [str(row[0]), float(row[1])]
        return out

    def _adjust_for_revisions(
        self, conn: sqlite3.Connection, trader: RiskTrader
    ) -> list[tuple[str, str]]:
        """Rescale the shares of every held asset whose history was revised since the
        last tick (a dividend or a split). Returns the events to log."""
        saved: dict[str, list[object]] = json.loads(_read_state(conn, "anchors") or "{}")
        events: list[tuple[str, str]] = []
        shares = trader.trader.book.shares
        for asset, symbol in enumerate(self.config.universe):
            anchor = saved.get(symbol)
            if anchor is None or shares[asset] <= 0:
                continue
            day, old = str(anchor[0]), float(cast(float, anchor[1]))
            row = conn.execute(
                "SELECT close FROM prices WHERE symbol = ? AND date = ?", (symbol, day)
            ).fetchone()
            if row is None or float(row[0]) <= 0:
                raise RiskRunError(f"{symbol}: the bar of {day} disappeared from the history")
            factor = old / float(row[0])
            if abs(factor - 1.0) > ADJUST_TOLERANCE:
                shares[asset] *= factor
                events.append(
                    (
                        "DIVIDEND_ADJUST",
                        f"{symbol}: history revised, shares multiplied by {factor:.6f} "
                        f"(close of {day}: {old:.4f} -> {float(row[0]):.4f})",
                    )
                )
        return events

    def _load_trader(
        self, conn: sqlite3.Connection, now: datetime, panel: ReturnPanel, costs: CostModel
    ) -> tuple[RiskTrader, list[int], list[tuple[str, str]]]:
        """The saved account, the panel rows still to replay (a new account: the last)
        and the events of any dividend adjustment."""
        cfg = self.config
        rule = cfg.rule()
        saved = _read_state(conn, "trader")
        if saved is None:
            trader = RiskTrader(
                rule,
                costs,
                initial_equity=cfg.capital_usd,
                kill_switch_dd=cfg.kill_switch_dd,
                size=len(cfg.universe),
            )
            _write_state(conn, "started_at", _now_iso(now))
            _write_state(conn, "first_day", panel.day[-1].date().isoformat())
            _write_state(conn, "universe", json.dumps(list(cfg.universe)))
            return trader, [len(panel) - 1], []  # the history only feeds the simulation
        if json.loads(_read_state(conn, "universe") or "[]") != list(cfg.universe):
            raise RiskRunError("the universe changed after the account started")
        trader = RiskTrader.from_state(
            json.loads(saved), rule, costs, kill_switch_dd=cfg.kill_switch_dd
        )
        events = self._adjust_for_revisions(conn, trader)
        last = pd.Timestamp(_read_state(conn, "last_day") or "1970-01-01")
        return trader, [r for r in range(len(panel)) if panel.day[r] > last], events

    def _record_row(
        self, conn: sqlite3.Connection, trader: RiskTrader, panel: ReturnPanel, row: int
    ) -> list[tuple[str, str]]:
        """Equity, decision log and fills of session `row` (not committed yet)."""
        inner = trader.trader
        day = panel.day[row].date().isoformat()
        equity, exposure = inner.equity_now(panel, row)
        peak = max(inner.book.peak, equity)
        conn.execute(
            "INSERT OR REPLACE INTO risk_equity (day, equity, cash, exposure, drawdown)"
            " VALUES (?, ?, ?, ?, ?)",
            (
                day,
                equity,
                float(inner.book.cash),
                exposure,
                1.0 - equity / peak if peak > 0 else 0.0,
            ),
        )
        for entry, held in zip(trader.log, trader.held, strict=True):
            weights = {
                symbol: round(float(weight), 6)
                for symbol, weight in zip(panel.symbols, held, strict=True)
                if weight > 0
            }
            conn.execute(
                "INSERT OR REPLACE INTO risk_log (day, rebalance, scale, cap_scale, dd_scale,"
                " base_total, held_q05, held_q01, weights) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    day,
                    int(bool(entry[2])),
                    *(float(cast(float, value)) for value in entry[3:9]),
                    json.dumps(weights),
                ),
            )
        trader.log.clear()
        trader.held.clear()
        events = []
        for trade in inner.book.trades:
            conn.execute(
                "INSERT OR IGNORE INTO risk_trades (day, symbol, side, notional, price,"
                " commission, spread_cost, reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    day,
                    trade["symbol"],
                    trade["side"],
                    trade["notional"],
                    trade["price"],
                    trade["commission"],
                    trade["spread_cost"],
                    trade["reason"],
                ),
            )
            events.append(
                (
                    "RISK_TRADE",
                    f"{trade['side']} {trade['symbol']} {trade['notional']:.2f} USD at "
                    f"{trade['price']:.2f} ({trade['reason']})",
                )
            )
        inner.book.trades.clear()
        return events

    def _process(self, conn: sqlite3.Connection, now: datetime, target: date) -> str:
        cfg = self.config
        start = self._history_start(conn, target)
        self._download(conn, start, target)
        panel = self._panel(conn, start, target)
        if len(panel) == 0:
            return "no data: no session in the history window"
        costs = build_costs(self.root, list(cfg.universe))
        trader, rows, events = self._load_trader(conn, now, panel, costs)
        if not rows:
            return "no data: no new session"
        wait = self._check_data(panel, rows)
        if wait is not None:
            conn.rollback()  # nothing of this tick is kept: the anchors are still the old ones
            return wait
        inner = trader.trader
        params = cfg.risk_params()
        newly_stopped = False
        for row in rows:
            was_stopped = inner.stopped
            seed = session_seed(cfg.simulation.seed, panel.day[row])
            step_session(panel, row, [trader], params, seed)
            newly_stopped = newly_stopped or (inner.stopped and not was_stopped)
            events += self._record_row(conn, trader, panel, row)
        if self.killed() and not inner.stopped:
            # After the replay, so the sells fill at the next open and not at one that
            # had already passed when the file was written.
            inner.liquidate(reason="KILL_SWITCH_FILE")
            events.append(("RISK_KILL_FILE", "the KILL_SWITCH file is present: liquidating"))
        last_day = panel.day[rows[-1]].date()
        _write_state(conn, "trader", json.dumps(trader.to_state()))
        _write_state(conn, "last_day", last_day.isoformat())
        _write_state(conn, "anchors", json.dumps(self._anchors(conn, last_day)))
        conn.commit()  # the whole tick lands together or not at all
        for kind, detail in events:
            store.record_event(conn, kind, detail)
        if newly_stopped:
            self._fire_kill_switch(conn, trader, panel, rows[-1])
        self.alert("INFO", self.summary(conn))
        equity = inner.equity_now(panel, rows[-1])[0]
        return f"{len(rows)} session(s) processed up to {last_day}, equity {equity:.2f} USD"

    def _fire_kill_switch(
        self, conn: sqlite3.Connection, trader: RiskTrader, panel: ReturnPanel, row: int
    ) -> None:
        inner = trader.trader
        equity, _ = inner.equity_now(panel, row)
        drawdown = 1.0 - equity / inner.book.peak if inner.book.peak > 0 else 1.0
        decision = KillDecision(True, False, ("drawdown",), drawdown)
        if fire(conn, decision, self.kill_file):
            self.alert(
                "KILL_SWITCH",
                f"drawdown {drawdown:.1%} reached the {self.config.kill_switch_dd:.0%} limit; "
                "selling everything at the next open and stopping",
            )

    # --- reports --------------------------------------------------------------

    def summary(self, conn: sqlite3.Connection) -> str:
        """A short status for the alert after each session and for `--report`."""
        cfg = self.config
        row = conn.execute(
            "SELECT day, equity, cash, exposure, drawdown FROM risk_equity"
            " ORDER BY day DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return "no equity recorded yet"
        log = conn.execute(
            "SELECT scale, held_q01, weights FROM risk_log WHERE day = ?", (row[0],)
        ).fetchone()
        trades = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(commission + spread_cost), 0) FROM risk_trades"
            " WHERE day = ?",
            (row[0],),
        ).fetchone()
        costs = conn.execute(
            "SELECT COALESCE(SUM(commission + spread_cost), 0) FROM risk_trades"
        ).fetchone()[0]
        worst = conn.execute("SELECT MAX(drawdown) FROM risk_equity").fetchone()[0]
        started = conn.execute("SELECT MIN(day) FROM risk_equity").fetchone()[0]
        total = row[1] / cfg.capital_usd - 1.0
        text = (
            f"close {row[0]}: equity {row[1]:.2f} USD ({total:+.2%} since {started}), "
            f"cash {row[2]:.2f}, invested {row[3]:.0%}, drawdown {row[4]:.1%} "
            f"(worst {worst:.1%}). Fills today: {trades[0]} (costs {trades[1]:.2f} USD; "
            f"{costs:.2f} USD since the start)."
        )
        if log is not None:
            weights: dict[str, float] = json.loads(log[2])
            held = ", ".join(f"{symbol} {weight:.1%}" for symbol, weight in weights.items())
            text += (
                f" Brake factor {log[0]:.2f}; simulated 5-session loss (1% tail) of what is "
                f"held {max(0.0, -log[1]):.1%}. Weights: {held or 'none (cash)'}."
            )
        state = json.loads(_read_state(conn, "trader") or "{}")
        if state.get("trader", {}).get("stopped"):
            text += " STOPPED by the kill switch."
        return text

    def report(self, out: Callable[[str], None]) -> int:
        """Print the status of the account and its latest fills."""
        if not self.db.exists():
            out("STRIDE RISK: no database yet")
            return 1
        conn = sqlite3.connect(self.db)
        try:
            init_schema(conn)
            out(self.summary(conn))
            for line in conn.execute(
                "SELECT day, symbol, side, notional, price, reason FROM risk_trades"
                " ORDER BY id DESC LIMIT 10"
            ):
                out("  fill " + " | ".join(str(x) for x in line))
        finally:
            conn.close()
        return 0

    def verify(self, out: Callable[[str], None]) -> int:
        """Replay the account from its first session with the stored bars and compare
        the equity with what the job recorded (criterion 4 of the design, section 6).

        Returns 0 when the largest relative gap is within `VERIFY_TOLERANCE`. After a
        dividend the stored history is rescaled, so share counts and per-share
        commissions differ slightly from the ones of the day: the gap is then small
        but not zero. A liquidation by a `KILL_SWITCH` file is not replayed.
        """
        if not self.db.exists():
            out("STRIDE RISK: no database yet")
            return 1
        cfg = self.config
        conn = sqlite3.connect(self.db)
        try:
            init_schema(conn)
            first, last = _read_state(conn, "first_day"), _read_state(conn, "last_day")
            start = _read_state(conn, "history_start")
            if first is None or last is None or start is None:
                out("STRIDE RISK: the account has not started")
                return 1
            panel = self._panel(conn, date.fromisoformat(start), date.fromisoformat(last))
            recorded = dict(conn.execute("SELECT day, equity FROM risk_equity").fetchall())
        finally:
            conn.close()
        trader = RiskTrader(
            cfg.rule(),
            build_costs(self.root, list(cfg.universe)),
            initial_equity=cfg.capital_usd,
            kill_switch_dd=cfg.kill_switch_dd,
            size=len(cfg.universe),
        )
        params = cfg.risk_params()
        worst, sessions = 0.0, 0
        for row in range(len(panel)):
            if panel.day[row] < pd.Timestamp(first):
                continue
            step_session(
                panel, row, [trader], params, session_seed(cfg.simulation.seed, panel.day[row])
            )
            day = panel.day[row].date().isoformat()
            if day in recorded:
                replayed = trader.trader.equity_now(panel, row)[0]
                worst = max(worst, abs(replayed / float(recorded[day]) - 1.0))
                sessions += 1
        ok = sessions > 0 and worst <= VERIFY_TOLERANCE
        out(
            f"STRIDE RISK: replay of {sessions} session(s) from {first} to {last}: largest "
            f"equity gap {worst:.6%} ({'ok' if ok else 'MISMATCH'}, tolerance "
            f"{VERIFY_TOLERANCE:.1%})"
        )
        return 0 if ok else 1
