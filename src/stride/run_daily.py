"""The daily job (SPEC sections 8 and 10, task T17).

    python -m stride.run_daily --config config.paper_b.yaml --phase auto
    python -m stride.run_daily --config config.paper_b.yaml --phase evening
    python -m stride.run_daily --config config.paper_b.yaml --mode backtest

Phases, New York time (SPEC 8 schedule):

- evening (18:00-20:00): update prices (retried on the next tick until 20:00
  while today's bar is missing, then `no_data` and an alert), validate, read the
  broker account and positions, detect deposits, advance the time-weighted index,
  decide the kill switch, compute the allocation targets (`backtest/targets.py`,
  the backtest's own function), plan and guard the orders (`execution/orders.py`)
  and store them as "planned", with the signals and risk decisions; then send
  the daily report in Spanish by Telegram.
- morning (09:15-09:30): read the positions again. Kill switch active: cancel
  everything and liquidate from the broker's positions (A1, Q5). Otherwise send
  last evening's approved orders through `orders.submit_approved`.
- midday (12:00-13:00): order statuses and fills.
- close (16:05-17:00): cancel what is pending, record fills, reconcile broker
  positions against the expected ones (a break is an event and an alert; two
  days in a row halt the system through the kill switch module), and mark
  unfilled exits for a market resend the next day (A1, Q7).
- auto: the phase due now, once per (date, phase) (the `runs` table), nothing
  outside the windows or on NYSE holidays. One scheduled task every 15 minutes
  then covers every phase, across US daylight-saving changes.

Safety: only `mode: backtest` or `mode: paper`; in paper the broker is
`AlpacaBroker` (paper keys only) or the shadow broker (T23, `make_broker`). Any exception
records `runs.status = error`, sends an alert without secrets, and stops the
phase: nothing more is sent. Logs go to `data/logs/<instance>.log`.

`--mode backtest` (the T17 acceptance) runs the backtest engine with `SimBroker`
on the stored prices of this instance and prints its summary; it writes nothing.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sqlite3
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
from pydantic import SecretStr

from stride.backtest.engine import BacktestSettings, MarketData, run_backtest
from stride.backtest.run import format_summary, load_market, summarize
from stride.backtest.targets import (
    allocation_period,
    allocation_targets,
    rebuild_state,
    scaling_drawdown,
    update_instruments,
)
from stride.config import StrideConfig, effective_config, load_config
from stride.data import store
from stride.data.calendar import is_trading_day, previous_trading_day
from stride.data.events import flagged_dates, load_price_events
from stride.data.leveraged import EtfFacts, load_etf_facts, rate_known_at_t_minus_1
from stride.data.quality import (
    DataQualityError,
    alerts,
    blocked_symbols,
    raise_on_blocking_failure,
    validate,
)
from stride.data.sources import (
    CRYPTO_SPOT_PRODUCTS,
    CoinbaseSource,
    FredRateSource,
    PriceSource,
    TiingoSource,
)
from stride.data.update import PlanItem, UpdateResult, series_for, update_all
from stride.execution import orders
from stride.execution.broker import Broker, Position
from stride.execution.kill_switch_file import (
    HALT_FILE,
    KILL_SWITCH_FILE,
    fire,
    halt,
    is_active,
    is_halted,
)
from stride.execution.shadow_broker import ShadowBroker
from stride.reporting import daily
from stride.reporting.alerts import Alert, Alerter, AlertError, AlertKind, alerter_from_env
from stride.risk import guard
from stride.risk.kill_switch import (
    TwrState,
    advance,
    equity_from_positions,
    kill_switch_decision,
    reconciliation_breaks,
)
from stride.settings import MissingCredentialError, Secrets, load_secrets

NEW_YORK = ZoneInfo("America/New_York")
PHASES = ("evening", "morning", "midday", "close")
# (start, end) of each phase window, New York time, end excluded.
WINDOWS: dict[str, tuple[time, time]] = {
    "morning": (time(9, 15), time(9, 30)),
    "midday": (time(12, 0), time(13, 0)),
    "close": (time(16, 5), time(17, 0)),
    "evening": (time(18, 0), time(20, 0)),
}
EVENING_LAST_TRY = time(19, 45)  # the last 15-minute tick before 20:00
BINDING = "ALLOCATION"
KILLED = "KILL_SWITCH"
RECONCILIATION_BREAK = "RECONCILIATION_BREAK"
MIN_SECRET_LENGTH = 4  # shorter values are not scrubbed: they would mangle the text
DUST_SHARES = 1e-9  # an expected quantity this small is no position
logger = logging.getLogger("stride.run_daily")


class RunError(Exception):
    """The phase cannot run (bad mode, missing broker, missing data...)."""


class WaitForData(Exception):
    """Today's bar is not there yet and the evening window is still open."""


def _git_hash() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            cwd=Path(__file__).resolve().parent,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return out.stdout.strip() or "unknown"


@dataclass
class Deps:
    """Everything the job reads from the outside world, injectable in tests."""

    now: Callable[[], datetime] = field(default=lambda: datetime.now(NEW_YORK))
    broker: Callable[[StrideConfig, Secrets, sqlite3.Connection], Broker] | None = None
    update_prices: Callable[..., list[UpdateResult]] | None = None
    risk_free: Callable[[], pd.Series] = field(default=lambda: FredRateSource().fetch_rate())
    alerter: Alerter | None = None
    git_hash: Callable[[], str] = _git_hash
    facts: Callable[[], Mapping[str, EtfFacts]] = load_etf_facts


@dataclass(frozen=True)
class Instance:
    """One paper account: its config, files and name ("B", "DIP", ...)."""

    config_path: Path
    config: StrideConfig
    name: str

    @property
    def root(self) -> Path:
        return self.config_path.parent

    @property
    def db(self) -> Path:
        return self.root / self.config.data.db_path

    @property
    def kill_switch(self) -> Path:
        """This instance's own KILL_SWITCH file: `KILL_SWITCH_B`, `KILL_SWITCH_DIP`
        (both paper configs live in the same folder, so a plain `KILL_SWITCH` would
        stop both), or `KILL_SWITCH` for `config.yaml`."""
        return self.root / _suffixed(KILL_SWITCH_FILE, self.name)

    @property
    def halt(self) -> Path:
        return self.root / _suffixed(HALT_FILE, self.name)

    @property
    def kill_switch_files(self) -> tuple[Path, ...]:
        """Every file that stops this instance: its own and the shared one, which a
        person may write by hand to stop every instance at once."""
        return tuple(dict.fromkeys((self.kill_switch, self.root / KILL_SWITCH_FILE)))

    @property
    def halt_files(self) -> tuple[Path, ...]:
        return tuple(dict.fromkeys((self.halt, self.root / HALT_FILE)))

    def killed(self) -> bool:
        return any(is_active(p) for p in self.kill_switch_files)

    def halted(self) -> bool:
        return any(is_halted(p) for p in self.halt_files)

    @property
    def log_file(self) -> Path:
        return self.root / "data" / "logs" / f"{self.name.lower()}.log"

    @property
    def title(self) -> str:
        return f"STRIDE {self.name}" if self.name != "STRIDE" else "STRIDE"


def instance_name(config_path: Path) -> str:
    """`config.paper_b.yaml` -> "B", `config.paper_dip.yaml` -> "DIP", else "STRIDE"."""
    stem = config_path.name.removesuffix(".yaml")
    if stem.startswith("config.paper_"):
        return stem.removeprefix("config.paper_").upper()
    return "STRIDE"


def _suffixed(name: str, instance: str) -> str:
    return name if instance == "STRIDE" else f"{name}_{instance}"


def scrub(text: str, secrets: Secrets | None) -> str:
    """`text` with every credential value replaced: alerts and logs never carry one."""
    if secrets is None:
        return text
    for name in type(secrets).model_fields:
        value = getattr(secrets, name, None)
        if not isinstance(value, SecretStr):
            continue
        raw = value.get_secret_value()
        if len(raw) >= MIN_SECRET_LENGTH:
            text = text.replace(raw, "***")
    return text


# Which phase is due.


def due_phase(now: datetime) -> tuple[date, str] | None:
    """The (day, phase) whose window contains `now` in New York, or None outside
    every window and on days NYSE is closed."""
    local = now.astimezone(NEW_YORK)
    if not is_trading_day(local.date()):
        return None
    clock = local.time()
    for phase, (start, end) in WINDOWS.items():
        if start <= clock < end:
            return local.date(), phase
    return None


def phase_day(phase: str, now: datetime) -> date:
    """The trading day a phase run by hand works on: the evening evaluates the
    last completed session (today after 18:00, else the previous trading day);
    the other phases work on today, which must be a trading day."""
    local = now.astimezone(NEW_YORK)
    today = local.date()
    if phase == "evening":
        if is_trading_day(today) and local.time() >= WINDOWS["evening"][0]:
            return today
        return previous_trading_day(today)
    if not is_trading_day(today):
        raise RunError(f"NYSE is closed on {today}; nothing to do for {phase}")
    return today


# Data for one instance.


def allocation_symbols(config: StrideConfig) -> list[str]:
    """The underlyings the allocation trades: core, sleeve assets and refuge."""
    alloc = config.experiments.allocation
    symbols = [*alloc.assets, *([alloc.refuge] if alloc.refuge != "cash" else [])]
    if alloc.mode == "sleeve":
        symbols = [*alloc.core, *alloc.assets]
    return list(dict.fromkeys(symbols))


def download_plan(config: StrideConfig) -> list[PlanItem]:
    """Only the series this instance trades: its allocation symbols, their
    leveraged versions and the crypto spot history behind IBIT and ETHA. The
    free Tiingo plan limits requests per hour, so nothing else is downloaded."""
    items = []
    for symbol in allocation_symbols(config):
        items.append(PlanItem(symbol, "tiingo", config.data.history_start))
        for etf in config.leveraged_etfs.get(symbol, {}).values():
            items.append(PlanItem(etf, "tiingo", config.data.history_start))
        product = CRYPTO_SPOT_PRODUCTS.get(symbol)
        if product is not None:
            items.append(PlanItem(product, "coinbase", config.data.crypto_history_start))
    return items


def default_update_prices(
    conn: sqlite3.Connection,
    config: StrideConfig,
    day: date,
    secrets: Secrets,
    only_missing: bool,
) -> list[UpdateResult]:
    """Incremental download of the instance plan; on a retry only the symbols
    still lacking bar t are asked for again."""
    plan = download_plan(config)
    if only_missing:
        plan = [p for p in plan if (store.last_price_date(conn, p.symbol) or date.min) < day]
    sources: dict[str, PriceSource] = {
        "tiingo": TiingoSource(api_key=secrets.tiingo_key()),
        "coinbase": CoinbaseSource(),
    }
    return update_all(conn, plan, sources, day)


def validate_instance(
    conn: sqlite3.Connection, config: StrideConfig, day: date
) -> tuple[list[str], set[str]]:
    """SPEC 5 validations 1-3 on the allocation symbols. Raises `DataQualityError`
    when the run must stop; returns the alert texts and the blocked symbols."""
    events = load_price_events()
    results = []
    for symbol in allocation_symbols(config):
        results += validate(
            symbol, series_for(conn, symbol), day, config.data.quality,
            flagged_dates=flagged_dates(events, symbol),
        )  # fmt: skip
    raise_on_blocking_failure(results)
    texts = [f"{r.symbol} {r.name}: {r.detail}" for r in alerts(results)]
    return texts, blocked_symbols(results)


def closes_on(bars: Mapping[str, pd.DataFrame], day: date) -> dict[str, float]:
    """The last close on or before `day` per symbol (never a later one)."""
    stamp = pd.Timestamp(day)
    out = {}
    for symbol, frame in bars.items():
        upto = frame.loc[:stamp, "close"]
        if len(upto):
            out[symbol] = float(upto.iloc[-1])
    return out


# The job.


class DailyJob:
    def __init__(
        self,
        instance: Instance,
        conn: sqlite3.Connection,
        deps: Deps,
        secrets: Secrets,
    ) -> None:
        self.inst = instance
        self.config = instance.config
        self.conn = conn
        self.deps = deps
        self.secrets = secrets
        self.alerter = deps.alerter or alerter_from_env()
        self._broker: Broker | None = None

    # Shared helpers.

    @property
    def broker(self) -> Broker:
        if self._broker is None:
            if self.deps.broker is None:
                raise RunError("no broker configured")
            self._broker = self.deps.broker(self.config, self.secrets, self.conn)
        return self._broker

    def alert(self, kind: AlertKind, text: str) -> None:
        message = scrub(f"{self.inst.title}: {text}", self.secrets)
        try:
            self.alerter.send(Alert(kind, message))
        except AlertError as exc:
            logger.warning("alert not sent: %s", scrub(str(exc), self.secrets))

    def event(self, kind: str, detail: str, day: date) -> None:
        store.record_event(self.conn, kind, detail, at=f"{day.isoformat()}T{_clock(self.deps)}")

    def market(self) -> MarketData:
        data, _ = load_market(self.conn, self.config, self.deps.risk_free(), self.deps.facts())
        return data

    def positions(self) -> dict[str, Position]:
        return {s: p for s, p in self.broker.get_positions().items() if p.quantity > 0}

    # Evening.

    def evening(self, day: date) -> str:
        """Returns the run detail; raises `WaitForData` to retry on the next tick."""
        now = self.deps.now().astimezone(NEW_YORK)
        final = now.date() > day or now.time() >= EVENING_LAST_TRY
        updater = self.deps.update_prices or default_update_prices
        results = updater(self.conn, self.config, day, self.secrets, True)
        for r in results:
            if r.error:
                logger.warning("download %s: %s", r.symbol, scrub(r.error, self.secrets))
        missing = [
            s
            for s in allocation_symbols(self.config)
            if (store.last_price_date(self.conn, s) or date.min) < day
        ]
        if missing and not final:
            raise WaitForData(f"no bar for {day} yet: {', '.join(missing)}")
        try:
            texts, blocked = validate_instance(self.conn, self.config, day)
        except DataQualityError as exc:
            if len(missing) == len(allocation_symbols(self.config)):
                raise _NoData(f"no bar for {day} after 20:00: {', '.join(missing)}") from exc
            raise
        for text in texts:
            self.alert("DATA_FAILED", text)
            self.event("DATA_FAILED", text, day)

        data = self.market()
        closes = closes_on(data.bars, day)
        # Fills the broker settled since the close phase (the shadow broker fills
        # at the open only once today's bar is stored), before reading the cash.
        orders.sync_orders(self.conn, self.broker, closes_on(data.bars, previous_trading_day(day)))
        held = self.positions()
        equity, cash, twr = self._account(day, closes, held)
        positions_usd = {s: p.quantity * closes[s] for s, p in held.items()}

        decision = kill_switch_decision(
            twr, self.config.risk.kill_switch_dd, self._breaks(day), self._breaks_before(day)
        )
        if fire(self.conn, decision, self.inst.kill_switch):
            self.alert("KILL_SWITCH", f"kill switch fired; drawdown {decision.drawdown:.2%}")
        if halt(self.conn, decision, self.inst.halt):
            self.alert("RECONCILIATION", "halted: reconciliation breaks on two consecutive days")
        killed, halted = self.inst.killed(), self.inst.halted()
        drawdown = min(max(twr.drawdown, 0.0), 1.0) if math.isfinite(twr.drawdown) else 1.0
        before = store.read_table(self.conn, "risk_decisions")
        before = before[pd.DatetimeIndex(before["date"]) < pd.Timestamp(day)]
        was = float(before["drawdown"].iloc[-1]) if not before.empty else 0.0
        if drawdown >= self.config.risk.dd_delever_at > was:  # SPEC 10: crossing -20%
            self.alert("DRAWDOWN", f"drawdown {drawdown:.2%}")
        if killed:
            self.broker.cancel_all()
            store.delete_unsent_orders(self.conn, day)
            self._risk_row(day, 0.0, KILLED, drawdown, [])
            self._send_report(day)
            self._check_phase4_verdict(day)
            return "kill switch active: the morning liquidates from the broker's positions"

        planned_count = self._plan(day, data, closes, equity, cash, held, positions_usd,
                                   blocked, halted, drawdown)  # fmt: skip
        self._send_report(day)
        self._check_phase4_verdict(day)
        return f"{planned_count} orders planned; equity {equity:.2f}"

    def _account(
        self, day: date, closes: Mapping[str, float], held: Mapping[str, Position]
    ) -> tuple[float, float, TwrState]:
        """Equity from closes x the broker's positions + cash (SPEC 7), compared with
        the broker's own figure; deposits; the time-weighted index; the equity row."""
        account = self.broker.get_account()
        quantities = {s: p.quantity for s, p in held.items()}
        missing_close = [s for s in quantities if s not in closes]
        if missing_close:
            raise RunError(f"no stored close for held {missing_close}")
        equity = equity_from_positions(quantities, closes, account.cash)
        if abs(equity - account.equity) > self.config.risk.reconciliation_floor_usd:
            logger.info("equity %.2f from closes vs %.2f reported by the broker",
                        equity, account.equity)  # fmt: skip
        flow = self._cash_flow(day, account.cash)
        twr = self._advance_twr(day, equity, flow)
        multiple = guard.multiple_map(self.config)
        gross = sum(q * closes[s] * multiple.get(s, 1) for s, q in quantities.items())
        store.upsert_equity(
            self.conn,
            pd.DataFrame(
                {"equity": [equity], "cash": [account.cash], "gross_exposure": [gross],
                 "leverage": [gross / equity if equity > 0 else 0.0]},
                index=pd.DatetimeIndex([pd.Timestamp(day)], name="date"),
            ),
        )  # fmt: skip
        return equity, account.cash, twr

    def _cash_flow(self, day: date, cash: float) -> float:
        """SPEC 8: cash up (or down) without a trade that explains it is a deposit
        (or a withdrawal), recorded in `cash_flows`. Returns today's net flow."""
        flows = store.net_cash_flows(self.conn)
        if pd.Timestamp(day) in flows.index:  # already detected by an earlier attempt
            return float(flows[pd.Timestamp(day)])
        before = store.read_equity(self.conn, end=day - timedelta(days=1))
        if before.empty:
            return 0.0
        last_day = before.index[-1]
        fills = store.read_table(self.conn, "fills")
        fills = fills[pd.DatetimeIndex(fills["date"]) > last_day] if not fills.empty else fills
        explained = 0.0
        for f in fills.to_dict("records"):
            value = float(f["quantity"]) * float(f["price"])
            fee = float(f["commission"])
            explained += value - fee if f["side"] == "sell" else -(value + fee)
        change = cash - float(before["cash"].iloc[-1]) - explained
        floor = self.config.risk.reconciliation_floor_usd
        if abs(change) <= floor:
            return 0.0
        kind = "deposit" if change > 0 else "withdrawal"
        store.record_cash_flow(self.conn, day, abs(change), kind, "cash change without a trade")
        self.alert("CASH_FLOW", f"{kind} detected: {abs(change):.2f} USD")
        return change

    def _advance_twr(self, day: date, equity: float, flow: float) -> TwrState:
        """The stored index and peak (SPEC 1.3), advanced once per day."""
        index, peak = store.load_twr(self.conn)
        state = TwrState(index, peak)
        last = store.twr_date(self.conn)
        if last is not None and last >= day:
            return state
        before = store.read_equity(self.conn, end=day - timedelta(days=1))
        if not before.empty and float(before["equity"].iloc[-1]) > 0:
            state = advance(state, float(before["equity"].iloc[-1]), equity, flow)
        store.save_twr(self.conn, state.index, state.peak)
        store.save_twr_date(self.conn, day)
        index, peak = store.load_twr(self.conn)  # the stored peak never goes down
        return TwrState(index, peak)

    def _breaks(self, day: date) -> list[str]:
        return [
            s
            for at, _, detail in store.read_events(self.conn, RECONCILIATION_BREAK)
            if at.startswith(day.isoformat())
            for s in detail.split(",")
            if s
        ]

    def _breaks_before(self, day: date) -> list[str]:
        return self._breaks(previous_trading_day(day))

    def _plan(  # noqa: PLR0917 - the evening's numbers, computed once above
        self,
        day: date,
        data: MarketData,
        closes: Mapping[str, float],
        equity: float,
        cash: float,
        held: Mapping[str, Position],
        positions_usd: Mapping[str, float],
        blocked: set[str],
        halted: bool,
        drawdown: float,
    ) -> int:
        alloc = self.config.experiments.allocation
        stored = store.read_equity(self.conn, end=day)
        state = rebuild_state(
            stored["equity"], store.net_cash_flows(self.conn), store.read_instruments(self.conn),
            store.last_allocation(self.conn, before=day), alloc.rebalance,
        )  # fmt: skip
        twr = TwrState(*store.load_twr(self.conn))
        period = allocation_period(pd.Timestamp(day), alloc.rebalance)
        symbols = allocation_symbols(self.config)
        history = pd.DataFrame({s: data.bars[s]["close"] for s in symbols if s in data.bars})
        history = history.sort_index().loc[: pd.Timestamp(day)]
        if period == state.last_period or history.empty:
            self._risk_row(day, 0.0, BINDING, drawdown, [])
            return 0
        rate = rate_known_at_t_minus_1(data.risk_free, pd.DatetimeIndex([pd.Timestamp(day)]))
        rf = float(rate.iloc[0]) if len(rate) and math.isfinite(float(rate.iloc[0])) else 0.0
        result = allocation_targets(
            self.config, history, rf, equity,
            scaling_drawdown=scaling_drawdown(
                twr, state.twr_history, self.config.experiments.dd_scale_window
            ),
            held=state.held, eligible=data.eligible, previous_weights=state.weights,
        )  # fmt: skip
        _, _, switches = update_instruments(state.held, state.chosen, result.choices)
        for asset, before, after in switches:
            self.event("SWITCH", f"{asset}: {before} -> {after}", day)
        store.save_instruments(
            self.conn, day, {a: (c.symbol, c.multiple) for a, c in result.choices.items()}
        )
        store.save_allocation(self.conn, day, result.weights)
        self._signal_rows(day, history, result.rules, state.weights, result.weights)

        prev = {s: closes.get(s, math.nan) for s in set(result.targets) | set(positions_usd)}
        plan = orders.plan(result.targets, positions_usd=positions_usd, rules=result.rules,
                           prev_close=prev, equity=equity, config=self.config)  # fmt: skip
        for o in plan.skipped:
            self.event("RESIZE_SKIPPED", f"{o.symbol} {o.side} {o.notional:.2f}: cost", day)
        underlying = guard.underlying_map(self.config)
        no_close = {underlying.get(s, s) for s in result.targets if not prev.get(s, 0) > 0}
        ctx = guard.GuardContext(
            equity=equity,
            positions_usd=positions_usd,
            data_ok=True,
            kill_switch_active=False,
            blocked=frozenset(blocked | no_close),
            halted=halted,
        )
        approved = orders.approve(plan.orders, ctx, cash, self.config)
        priced, too_small = orders.price(
            approved, run_date=day, prev_close=prev,
            held_quantity={s: p.quantity for s, p in held.items()}, targets=result.targets,
            config=self.config, limits=lambda s: orders.limits_allowed(self.broker, s),
            fractionable=self.broker.is_fractionable,
            resend=orders.resend_symbols(self.conn, day),
        )  # fmt: skip
        for p in too_small:
            self.event(orders.ORDER_TOO_SMALL, f"{p.symbol} {p.side} {p.notional:.2f}", day)
        orders.store_plan(self.conn, day, priced)
        vetoes = [f"{o.symbol}:{o.side}:{'+'.join(d.reasons)}" for o, d in priced if not d.approved]
        for veto in vetoes:
            self.event("VETO", veto, day)
            self.alert("ORDER_VETOED", f"order vetoed {veto}")
        self._risk_row(day, result.gross, BINDING, drawdown, vetoes)
        return sum(d.approved for _, d in priced)

    def _signal_rows(
        self,
        day: date,
        history: pd.DataFrame,
        rules: Mapping[str, str],
        before: Mapping[str, float],
        after: Mapping[str, float],
    ) -> None:
        rows = [
            {"date": pd.Timestamp(day), "symbol": asset,
             "close": float(history[asset].iloc[-1]) if asset in history else math.nan,
             "sma200": math.nan, "hh55": math.nan, "ll20": math.nan, "n": math.nan,
             "rule": rule, "current_units": int(before.get(asset, 0.0) > 0),
             "target_units": int(after.get(asset, 0.0) > 0), "last_entry": None, "capped": 0}
            for asset, rule in sorted(rules.items())
        ]  # fmt: skip
        if rows:
            store.upsert_signals(self.conn, pd.DataFrame(rows).set_index(["date", "symbol"]))

    def _risk_row(
        self, day: date, gross: float, binding: str, drawdown: float, vetoes: list[str]
    ) -> None:
        row = {"l_star": math.nan, "kelly_days": 0, "sigma_60": 0.0, "leverage": gross,
               "binding_limit": binding, "drawdown": drawdown, "delever_active": 0,
               "k": gross, "vetoes": json.dumps(vetoes)}  # fmt: skip
        frame = pd.DataFrame([row], index=pd.DatetimeIndex([pd.Timestamp(day)], name="date"))
        store.upsert_risk_decisions(self.conn, frame)

    def _send_report(self, day: date) -> None:
        out = self.inst.root / "reports" / "daily" / f"{self.inst.name.lower()}-{day}.html"
        summary = daily.write_daily(self.conn, day, self.config.mode, out)
        caption = daily.caption(summary, title=self.inst.title)
        try:
            self.alerter.send_document(out.name, out.read_bytes(), caption)
        except AlertError as exc:
            logger.warning("daily report not sent: %s", scrub(str(exc), self.secrets))

    def _check_phase4_verdict(self, day: date) -> None:
        if store.read_events(self.conn, daily.PHASE4_VERDICT_EVENT):
            return
        verdict = daily.evaluate_phase4(self.conn, title=self.inst.title)
        if verdict.ready:
            self.event(daily.PHASE4_VERDICT_EVENT, verdict.message, day)
            self.alert("PHASE4_VERDICT", verdict.message)

    # Morning.

    def morning(self, day: date) -> str:
        evening = previous_trading_day(day)
        held = self.positions()
        if self.inst.killed():
            self.broker.cancel_all()
            closes = self._stored_closes(evening, held)
            sells = orders.liquidation_orders(held, closes)
            checked = orders.check_liquidation(
                sells, {s: p.quantity * closes[s] for s, p in held.items()}
            )
            priced, _ = orders.price(
                checked, run_date=day, prev_close=closes,
                held_quantity={s: p.quantity for s, p in held.items()}, targets={},
                config=self.config, limits=lambda _: False, fractionable=lambda _: True,
            )  # fmt: skip
            sent = self._submit([o for o, _ in priced], [d for _, d in priced], day)
            self.alert("KILL_SWITCH", f"liquidation: {sent} sells sent")
            return f"kill switch: {sent} liquidation sells sent"
        stored = store.read_orders(self.conn, statuses=("planned", "sending", "unknown"),
                                   day=evening)  # fmt: skip
        planned = orders.stored_orders(stored)
        if not planned:
            return "no planned orders"
        closes = self._stored_closes(evening, held, extra=[o.symbol for o in planned])
        equity_rows = store.read_equity(self.conn, end=evening)
        equity = float(equity_rows["equity"].iloc[-1]) if not equity_rows.empty else 0.0
        ctx = guard.GuardContext(
            equity=equity,
            positions_usd={s: p.quantity * closes[s] for s, p in held.items()},
            data_ok=True,
            kill_switch_active=False,
            halted=self.inst.halted(),
        )
        proposed = [guard.ProposedOrder(o.symbol, o.side, o.notional, o.switch) for o in planned]
        by_id = {id(d.order): d for d in guard.check_orders(proposed, ctx, self.config)}
        decisions = [by_id[id(p)] for p in proposed]
        sent = self._submit(planned, decisions, evening)
        return f"{sent} of {len(planned)} orders sent"

    def _submit(
        self, planned: Sequence[orders.Order], decisions: Sequence[guard.GuardDecision], run: date
    ) -> int:
        out = orders.submit_approved(
            self.broker, planned, decisions, self.conn,
            kill_switch_path=self.inst.kill_switch_files, halt_path=self.inst.halt_files,
            run_date=run,
        )  # fmt: skip
        for s in out:
            if s.status in {"failed", "unknown"}:
                self.alert(
                    "ORDER_PROBLEM", f"order {s.order.client_id} {s.status}: {'+'.join(s.reasons)}"
                )
            if s.status == "vetoed":
                self.alert(
                    "ORDER_VETOED", f"vetoed at send time {s.order.symbol}: {'+'.join(s.reasons)}"
                )
        return sum(s.status == "pending" for s in out)

    def _stored_closes(
        self, day: date, held: Mapping[str, Position], extra: Sequence[str] = ()
    ) -> dict[str, float]:
        closes = {}
        for symbol in {*held, *extra}:
            bars = series_for(self.conn, symbol)
            upto = bars.loc[: pd.Timestamp(day), "close"] if not bars.empty else bars
            if not len(upto):
                raise RunError(f"no stored close for {symbol} on or before {day}")
            closes[symbol] = float(upto.to_numpy(dtype=float)[-1])
        return closes

    # Midday and close.

    def midday(self, day: date) -> str:
        held = self.positions()
        open_orders = store.read_orders(self.conn, statuses=("pending", "sending", "unknown"))
        closes = self._stored_closes(
            previous_trading_day(day), held, extra=list(open_orders["symbol"])
        )
        states = orders.sync_orders(self.conn, self.broker, closes)
        return f"{len(states)} orders checked"

    def close(self, day: date) -> str:
        cancelled = self.broker.cancel_all()
        held = self.positions()
        open_orders = store.read_orders(self.conn, statuses=("pending", "sending", "unknown"))
        closes = self._stored_closes(
            previous_trading_day(day), held, extra=list(open_orders["symbol"])
        )
        final = not getattr(self.broker, "settles_at_evening", False)
        orders.sync_orders(self.conn, self.broker, closes, final=final)
        breaks = self._reconcile(day, held, closes)
        resend = orders.mark_resends(self.conn, previous_trading_day(day))
        if resend:
            self.alert("ORDER_PROBLEM", f"unfilled exits resent tomorrow at market: {resend}")
        return f"{cancelled} cancelled; breaks {breaks or 'none'}; resend {resend or 'none'}"

    def _reconcile(
        self, day: date, held: Mapping[str, Position], closes: Mapping[str, float]
    ) -> list[str]:
        """Expected positions (yesterday's plus today's fills) against the broker's
        (SPEC 7, step 4). A symbol breaks above max(1% of value, $1)."""
        broker_qty = {s: p.quantity for s, p in held.items()}
        last = store.last_positions(self.conn, before=day)
        resolved = [at for at, _, _ in store.read_events(self.conn, "HALT_RESOLVED")]
        if last is None or (resolved and resolved[-1][:10] >= last[0].isoformat()):
            store.save_positions(self.conn, day, broker_qty)  # first day, or after a halt
            return []
        expected = dict(last[1])
        fills = store.read_table(self.conn, "fills")
        if not fills.empty:
            fills = fills[pd.DatetimeIndex(fills["date"]) > pd.Timestamp(last[0])]
        for f in fills.to_dict("records"):
            sign = 1.0 if f["side"] == "buy" else -1.0
            expected[str(f["symbol"])] = expected.get(str(f["symbol"]), 0.0) + sign * float(
                f["quantity"]
            )
        expected = {s: q for s, q in expected.items() if abs(q) > DUST_SHARES}
        breaks = reconciliation_breaks(
            {s: q * closes.get(s, math.nan) for s, q in expected.items()},
            {s: q * closes.get(s, math.nan) for s, q in broker_qty.items()},
            self.config.risk.reconciliation_tolerance,
            self.config.risk.reconciliation_floor_usd,
        )
        store.save_positions(self.conn, day, expected)
        if breaks:
            self.event(RECONCILIATION_BREAK, ",".join(breaks), day)
            self.alert("RECONCILIATION", f"reconciliation break: {', '.join(breaks)}")
        return breaks


Runner = DailyJob


class _NoData(Exception):
    """Today's bar is still missing at the end of the evening window."""


def _clock(deps: Deps) -> str:
    return deps.now().astimezone(NEW_YORK).strftime("%H:%M:%S")


# Running one phase, recorded in `runs`.


def run_phase(
    instance: Instance,
    phase: str,
    deps: Deps,
    *,
    secrets: Secrets | None = None,
    day: date | None = None,
) -> int:
    """Run one phase (or `auto`). Exit code 0 when it ran or had nothing to do,
    1 on an error. Every finished phase is a `runs` row."""
    if instance.config.mode not in {"backtest", "paper"}:
        raise RunError(f"refusing to run with mode {instance.config.mode!r}")
    if instance.config.mode != "paper":
        raise RunError("phases need mode: paper (use --mode backtest for a backtest)")
    now = deps.now()
    if phase == "auto":
        due = due_phase(now)
        if due is None:
            logger.info("%s: nothing due at %s", instance.name, now.astimezone(NEW_YORK))
            return 0
        day, phase = due
    elif day is None:
        day = phase_day(phase, now)
    conn = store.connect(instance.db)
    try:
        if store.run_done(conn, day, phase):
            logger.info("%s: %s for %s already done", instance.name, phase, day)
            return 0
        secrets = secrets if secrets is not None else load_secrets(instance.root / ".env")
        job = DailyJob(instance, conn, deps, secrets)
        started = now.isoformat()
        git = deps.git_hash()

        def record(status: str, detail: str) -> None:
            store.record_run(conn, day, phase, instance.config.mode, status, started,
                             deps.now().isoformat(), git, scrub(detail, secrets))  # fmt: skip

        try:
            detail = getattr(job, phase)(day)
        except WaitForData as exc:
            logger.info("%s: %s; retrying on the next tick", instance.name, exc)
            return 0
        except _NoData as exc:
            record("no_data", str(exc))
            job.alert("DATA_FAILED", f"no trading today: {exc}")
            return 0
        except Exception as exc:
            text = scrub(f"{phase} failed: {type(exc).__name__}: {exc}", secrets)
            logger.error("%s: %s", instance.name, text)
            record("error", text)
            job.alert("RUN_FAILED", text)
            return 1
        record("ok", detail)
        logger.info("%s: %s %s ok: %s", instance.name, phase, day, scrub(detail, secrets))
        return 0
    finally:
        conn.close()


def run_backtest_mode(instance: Instance, deps: Deps, out: Callable[[str], None]) -> int:
    """The T17 acceptance: the engine with `SimBroker` on this instance's stored
    prices, from `data.history_start` to the last stored bar. Writes nothing."""
    config = effective_config(instance.config)
    if not instance.db.exists():
        out(f"error: no price database at {instance.db}; run the evening phase first")
        return 2
    conn = store.connect(instance.db)
    try:
        rate = deps.risk_free()
        data, _ = load_market(conn, config, rate, deps.facts())
        symbol = allocation_symbols(config)[0]
        last = store.last_price_date(conn, symbol)
    finally:
        conn.close()
    if last is None:
        out(f"error: no {symbol} prices stored")
        return 2
    settings = BacktestSettings(
        config.data.history_start, last, config.cash_flows.initial_deposit_usd
    )
    result = run_backtest(data, config, settings)
    for line in format_summary(summarize(result, data.bars, rate)):
        out(line)
    out(f"{instance.title} backtest {settings.start}..{settings.end}: ok")
    return 0


def setup_logging(instance: Instance) -> None:
    """Log to `data/logs/<instance>.log` next to the config: one file per instance."""
    instance.log_file.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger("stride")
    for old in [h for h in root.handlers if getattr(h, "stride_instance", False)]:
        root.removeHandler(old)
        old.close()
    handler = logging.FileHandler(instance.log_file, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    handler.stride_instance = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    root.setLevel(logging.INFO)


def load_instance(config_path: str | Path) -> Instance:
    path = Path(config_path).resolve()
    return Instance(path, load_config(path), instance_name(path))


def main(argv: Sequence[str] | None = None, deps: Deps | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m stride.run_daily")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--phase", choices=[*PHASES, "auto"])
    parser.add_argument("--mode", choices=["backtest"], help="run the backtest instead")
    parser.add_argument("--date", type=date.fromisoformat, help="the day (default: from the clock)")
    args = parser.parse_args(argv)
    if args.phase is None and args.mode is None:
        parser.error("give --phase or --mode backtest")
    instance = load_instance(args.config)
    setup_logging(instance)
    deps = deps or default_deps(instance)

    def out(line: str) -> None:
        sys.stdout.write(line + "\n")

    try:
        if args.mode == "backtest":
            return run_backtest_mode(instance, deps, out)
        code = run_phase(instance, args.phase, deps, day=args.date)
    except RunError as exc:
        out(f"error: {exc}")
        return 2
    out(f"{instance.title} {args.phase}: {'ok' if code == 0 else 'error'}")
    return code


def default_deps(instance: Instance) -> Deps:
    """The real world: the clock, the paper broker, Tiingo, FRED and Telegram."""
    return Deps(
        broker=make_broker,
        update_prices=default_update_prices,
        alerter=alerter_from_env(str(instance.root / ".env")),
    )


def traded_symbols(config: StrideConfig) -> list[str]:
    """The allocation symbols and their leveraged versions."""
    symbols = allocation_symbols(config)
    for symbol in list(symbols):
        symbols += list(config.leveraged_etfs.get(symbol, {}).values())
    return symbols


def make_broker(config: StrideConfig, secrets: Secrets, conn: sqlite3.Connection) -> Broker:
    """The paper broker of an instance (T23):

    - `execution.broker: shadow`: the shadow broker, on the instance's database;
    - `alpaca`: `AlpacaBroker` with the keys named by `execution.alpaca_key_env`,
      paper keys only. When the DIP keys are simply missing, the DIP instance
      logs a warning and runs on the shadow broker instead; any other key problem
      (a live key, B's keys missing) stops the run."""
    if config.mode != "paper":
        raise RunError(f"no paper broker in mode {config.mode!r}")
    if config.execution.broker == "shadow":
        return ShadowBroker(config, conn, traded_symbols(config))
    from stride.execution.alpaca_broker import AlpacaBroker  # noqa: PLC0415 - optional extra

    try:
        return AlpacaBroker(config, secrets)
    except MissingCredentialError:
        if config.execution.alpaca_key_env != "ALPACA_DIP":
            raise
        logger.warning("ALPACA_DIP keys are not in .env: using the shadow broker")
        return ShadowBroker(config, conn, traded_symbols(config))


if __name__ == "__main__":
    raise SystemExit(main())
