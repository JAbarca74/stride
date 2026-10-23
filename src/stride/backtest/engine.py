"""Backtest engine (SPEC sections 3, 7, 8 and 9). No network, no files, no clock.

Walks NYSE trading days. On each day t it uses only bars dated on or before t and
the risk-free rate known at t-1, in the order of the live daily run:

1. Idle cash from the previous close earns the risk-free rate (actual/360).
2. `SimBroker` fills the orders sent on t-1 at the open of t (spread + IBKR
   Pro Fixed commission).
3. A deposit of `cash_flows.monthly_deposit_usd` on the first trading day of each
   month after the start. It sits in cash until today's targets invest it by the
   normal rules (SPEC 8); the time-weighted index never counts it as a gain.
4. Equity at the close of t; time-weighted index and its peak; kill switch
   (`risk/kill_switch.py`). When it fires, every position the broker reports is
   sold at the next open (`risk/guard.check_liquidation`) and the system never
   trades again in this run. Reconciliation cannot break in a backtest (the
   system's positions are the broker's), so the halt never applies.
5. Signals (`strategy/rules.decide`) -> unit caps, last in first out
   (`risk/sizing.apply_caps`) -> unlevered dollar targets (`risk/sizing.target_usd`).
6. Kelly on the unlevered strategy's daily returns and forward sigma_60 on the
   target weights -> effective L (`risk/leverage.effective_leverage`) -> one
   instrument per asset, leveraged ETFs with hysteresis
   (`risk/leverage.choose_instruments`).
7. Orders -> `risk/guard.check_orders` -> `SimBroker`, filled at the open of t+1.

PROVISIONAL: `plan_orders` below stands in for `execution/orders.py`, which is a
human-reviewed module not written yet. It follows the proposal in
`docs/design/execution_modules.md` (PR #45: band, exits and switch legs,
sells first) and must be replaced by `orders.plan_orders` once that module is
approved, so that backtest and live share it.

Indicators are computed once on each full series: every indicator only reads
data up to its own day (tests/test_lookahead.py), and the engine only looks up
rows dated on or before t (tests/test_engine.py extends the anti-look-ahead test
to the whole engine).
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, timedelta

import pandas as pd

from stride.backtest.costs import (
    account_cash_interest,
    commission,
    max_commission_rate,
    spread_bps,
)
from stride.backtest.metrics import time_weighted_returns
from stride.config import StrideConfig
from stride.data.calendar import trading_days
from stride.data.leveraged import rate_known_at_t_minus_1
from stride.execution.broker import OrderState, OrderType, Side
from stride.execution.sim_broker import SimBroker
from stride.indicators import compute_indicators
from stride.risk import guard
from stride.risk.kill_switch import TwrState, advance, kill_switch_decision
from stride.risk.leverage import (
    KELLY_WINDOW_DAYS,
    TRADING_DAYS_PER_YEAR,
    HeldInstrument,
    InstrumentChoice,
    choose_instruments,
    effective_leverage,
    forward_volatility,
    kelly_estimate,
)
from stride.risk.sizing import UnitLot, apply_caps, target_usd
from stride.strategy.rules import HOLD, Decision, Indicators, PositionState, decide

Row = tuple[float, float, float, float, float]  # close, SMA200, HH55, LL20, N

FIXED = "FIXED"  # binding limit when the run uses a fixed leverage (SPEC 9, item 5)
KILLED = "KILL_SWITCH"  # binding limit on the days after the kill switch fired
BPS = 10_000
# Days of returns passed to the EWMA covariance. With span 60 the weight left
# out beyond 1,200 days is (59/61)^1200, below 1e-17 of the total: the same
# sigma_60 as the full history, at a fixed cost per day.
COVARIANCE_WINDOW_DAYS = 1_200


@dataclass(frozen=True)
class MarketData:
    """Everything the engine reads.

    `bars` maps every tradable symbol to its daily `open`, `high`, `low`, `close`
    (adjusted), indexed by date: the universe (crypto spliced with spot history)
    and the leveraged ETFs (synthetic before launch). `risk_free` is the annual
    3-month T-bill (FRED DTB3, as a fraction) by publication date; the engine uses
    the value known at t-1. `eligible` maps an asset to its leveraged versions that
    pass the filters, e.g. {"SPY": {2: "SSO", 3: "UPRO"}}.
    """

    bars: Mapping[str, pd.DataFrame]
    risk_free: pd.Series
    eligible: Mapping[str, Mapping[int, str]] = field(default_factory=dict)


@dataclass(frozen=True)
class BacktestSettings:
    """`initial_cash` is the starting capital (SPEC 2: $100). `fixed_leverage`
    replaces the dynamic L for the leverage comparison (SPEC 9, item 5); the
    instrument route still caps what the cash account can reach."""

    start: date
    end: date
    initial_cash: float
    order_type: OrderType = "market"
    fixed_leverage: float | None = None


@dataclass(frozen=True)
class PlannedOrder:
    """An order the provisional planner proposes, before the guard."""

    symbol: str
    side: Side
    notional: float
    switch: bool
    asset: str
    rule: str


@dataclass
class BacktestResult:
    """Rows for the SPEC 10 tables, plus orders and fills (kept in memory until
    the `orders`/`fills` tables ship with `execution/orders.py`)."""

    equity: pd.DataFrame
    signals: pd.DataFrame
    risk_decisions: pd.DataFrame
    events: pd.DataFrame
    orders: pd.DataFrame
    fills: pd.DataFrame
    cash_flows: pd.Series
    unlevered_returns: pd.Series
    switches: pd.DataFrame

    @property
    def returns(self) -> pd.Series:
        """Daily time-weighted returns: deposits never count as gains."""
        return time_weighted_returns(self.equity["equity"], self.cash_flows)


# Provisional order planner (until execution/orders.py).


def plan_orders(
    targets: Mapping[str, float],
    *,
    positions_usd: Mapping[str, float],
    underlying: Mapping[str, str],
    rules: Mapping[str, str],
    config: StrideConfig,
) -> list[PlannedOrder]:
    """PROVISIONAL stand-in for `execution/orders.plan_orders` (PR #45 proposal).

    `targets` are the dollars to hold per symbol (the instrument chosen for each
    asset); `positions_usd` the value held per symbol at the close.

    - A symbol held but not targeted is sold in full (exit or the sell leg of an
      instrument switch); exits and both switch legs skip the rebalance band.
    - Otherwise no order when the difference is below `rebalance_band` x target.
    - No order below `min_order_usd`.
    - Sells first, then buys; larger first within each side.
    """
    band, minimum = config.execution.rebalance_band, config.execution.min_order_usd
    sells: list[PlannedOrder] = []
    buys: list[PlannedOrder] = []
    by_asset: dict[str, list[str]] = {}
    for symbol in set(targets) | {s for s, v in positions_usd.items() if v > 0}:
        by_asset.setdefault(underlying[symbol], []).append(symbol)
    for asset in sorted(by_asset):
        rule = rules.get(asset, "HOLD")
        wanted = [s for s in by_asset[asset] if targets.get(s, 0.0) > 0]
        target_symbol = wanted[0] if wanted else None
        switching = False
        for symbol in sorted(by_asset[asset]):
            held = positions_usd.get(symbol, 0.0)
            if symbol != target_symbol and held >= minimum:
                switching = target_symbol is not None
                sells.append(PlannedOrder(symbol, "sell", held, switching, asset, rule))
        if target_symbol is None:
            continue
        target = targets[target_symbol]
        diff = target - positions_usd.get(target_symbol, 0.0)
        if abs(diff) < minimum or (not switching and abs(diff) < band * target):
            continue
        order = PlannedOrder(
            target_symbol, "buy" if diff > 0 else "sell", abs(diff), switching, asset, rule
        )
        (buys if diff > 0 else sells).append(order)
    return sorted(sells, key=lambda o: -o.notional) + sorted(buys, key=lambda o: -o.notional)


def skip_costly_resizes(
    orders: Sequence[PlannedOrder], prices: Mapping[str, float], config: StrideConfig
) -> tuple[list[PlannedOrder], list[PlannedOrder]]:
    """Cost-aware resizing (owner request, 2026-10-23). Returns (kept, skipped).

    A top-up buy on a position whose rule is HOLD (no new signal: the target moved
    because equity, L or N moved) is skipped when its commission plus spread is
    more than `execution.max_resize_cost_pct` of the order: the correction is not
    worth its cost. Never skipped: sells (they only reduce risk, the drawdown
    deleveraging included), entries, pyramid adds, exits and switch legs."""
    limit = config.execution.max_resize_cost_pct
    kept, skipped = [], []
    for order in orders:
        price = prices.get(order.symbol, math.nan)
        resize = order.side == "buy" and order.rule == HOLD and not order.switch
        if resize and price > 0 and order.notional > 0:
            fee = commission(order.notional / price, price, config)
            rate = fee / order.notional + spread_bps(order.symbol, config) / BPS
            if rate > limit:
                skipped.append(order)
                continue
        kept.append(order)
    return kept, skipped


def split_orders(
    orders: Sequence[PlannedOrder], equity: float, config: StrideConfig
) -> list[PlannedOrder]:
    """PROVISIONAL (owner decision on PR #56, question A). Split every order above
    `max_order_pct_equity` x equity into equal parts that fit, so a large exit (a
    stop included) or a first unit worth more than 60% of equity is not vetoed
    forever. Each part still goes through the guard and counts towards the daily
    order limit; sells come first, so exits get the slots before entries."""
    cap = config.execution.max_order_pct_equity * equity * (1 - 1e-9)
    if not cap > 0:
        return list(orders)
    out = []
    for order in orders:
        parts = max(1, math.ceil(order.notional / cap))
        out += [replace(order, notional=order.notional / parts)] * parts
    return out


def fit_buys(
    orders: Sequence[PlannedOrder], cash: float, config: StrideConfig
) -> list[PlannedOrder]:
    """PROVISIONAL. Scale the buys down together so they fit in the cash plus the
    proceeds of the sells in `orders`, net of the worst-case spread and the
    commission cap (`SimBroker` rejects a buy larger than its cash). Call it with
    the orders the guard approved, so a vetoed sell never funds a buy. Buys that
    fall below `min_order_usd` are dropped. A gap at the open can still leave a
    buy short of cash; `SimBroker` then rejects it and the engine logs it."""
    fee_cap = max_commission_rate(config)
    sells = [o for o in orders if o.side == "sell"]
    buys = [o for o in orders if o.side == "buy"]
    proceeds = sum(o.notional * (1 - spread_bps(o.symbol, config) / BPS - fee_cap) for o in sells)
    available = max(cash + proceeds, 0.0)
    needed = sum(o.notional * (1 + fee_cap) for o in buys)
    if needed > available:
        scale = available / needed
        buys = [replace(o, notional=o.notional * scale) for o in buys]
    return sells + [o for o in buys if o.notional >= config.execution.min_order_usd]


# Engine.


@dataclass
class _Asset:
    """Precomputed rows of one universe asset, by date."""

    rows: dict[pd.Timestamp, Row]
    dates: pd.DatetimeIndex

    def last_row(self, day: pd.Timestamp) -> Row | None:
        """The newest row dated on or before `day` (never a later one)."""
        position = int(self.dates.searchsorted(day, side="right"))
        return None if position == 0 else self.rows[self.dates[position - 1]]


def _prepare_assets(data: MarketData, config: StrideConfig) -> dict[str, _Asset]:
    assets = {}
    for symbol in config.universe:
        bars = data.bars.get(symbol)
        if bars is None or bars.empty:
            continue
        frame = bars.sort_index()
        ind = compute_indicators(frame, config.strategy)
        values = zip(
            frame["close"].astype(float),
            ind["SMA200"],
            ind["HH55"],
            ind["LL20"],
            ind["N"],
            strict=True,
        )
        index = pd.DatetimeIndex(frame.index)
        rows = {day: tuple(map(float, row)) for day, row in zip(index, values, strict=True)}
        assets[symbol] = _Asset(rows, index)  # type: ignore[arg-type]
    return assets


def _returns_matrix(data: MarketData, assets: Sequence[str]) -> pd.DataFrame:
    closes = {s: data.bars[s]["close"].sort_index().astype(float) for s in assets}
    frame = pd.DataFrame(closes).sort_index()
    return frame.pct_change(fill_method=None)


class _Recorder:
    def __init__(self) -> None:
        self.equity: list[dict[str, object]] = []
        self.signals: list[dict[str, object]] = []
        self.risk: list[dict[str, object]] = []
        self.events: list[dict[str, object]] = []
        self.orders: list[dict[str, object]] = []
        self.fills: list[dict[str, object]] = []
        self.flows: dict[pd.Timestamp, float] = {}
        self.unlevered: dict[pd.Timestamp, float] = {}
        self.switches: list[dict[str, object]] = []

    def event(self, day: pd.Timestamp, kind: str, detail: str) -> None:
        self.events.append({"at": day.date().isoformat(), "type": kind, "detail": detail})


class Engine:
    """One backtest run. Use `run_backtest`."""

    def __init__(self, data: MarketData, config: StrideConfig, settings: BacktestSettings) -> None:
        if not settings.initial_cash > 0:
            raise ValueError("initial_cash must be positive")
        if settings.end < settings.start:
            raise ValueError("end must not be before start")
        self.data = data
        self.config = config
        self.settings = settings
        self.assets = _prepare_assets(data, config)
        if not self.assets:
            raise ValueError("no bars for any universe symbol")
        self.days = trading_days(settings.start, settings.end)
        if len(self.days) == 0:
            raise ValueError("no NYSE trading day in the backtest period")
        self.rate = rate_known_at_t_minus_1(data.risk_free, self.days)
        if self.rate.isna().any():
            missing = self.rate[self.rate.isna()].index[0].date()
            raise ValueError(f"no risk-free rate known before {missing}")
        self.returns = _returns_matrix(data, list(self.assets))
        self.return_values = self.returns.to_numpy(dtype=float)
        self.return_rows = {day: i for i, day in enumerate(pd.DatetimeIndex(self.returns.index))}
        self.column = {asset: i for i, asset in enumerate(self.returns.columns)}
        self.closes = {
            s: (pd.DatetimeIndex(b.sort_index().index), b.sort_index()["close"].to_numpy(float))
            for s, b in data.bars.items()
        }
        self.underlying = guard.underlying_map(config)
        self.multiple = guard.multiple_map(config)
        tradable = {s: b for s, b in data.bars.items() if s in self.underlying}
        self.broker = SimBroker(
            tradable, config, settings.initial_cash, self.days[0].date() - timedelta(days=1)
        )
        self.state = {asset: PositionState() for asset in self.assets}
        self.lots: list[UnitLot] = []
        self.held: dict[str, HeldInstrument] = {}
        self.chosen: dict[str, str] = {}
        self.weights: dict[str, float] = {}  # yesterday's unlevered target weights
        self.turnover = 0.0  # change in unlevered weights decided yesterday
        self.traded = 0.0  # dollars filled so far
        self.costs_paid = 0.0  # spread plus commission paid so far
        self.twr = TwrState()
        self.delever = False
        self.killed = False
        self.rec = _Recorder()

    # Market helpers (only rows on or before the day).

    def _close(self, symbol: str, day: pd.Timestamp) -> float:
        index, values = self.closes[symbol]
        position = int(index.searchsorted(day, side="right"))
        return float(values[position - 1]) if position else math.nan

    def _positions_usd(self, day: pd.Timestamp) -> dict[str, float]:
        return {
            symbol: position.quantity * self._close(symbol, day)
            for symbol, position in self.broker.get_positions().items()
        }

    # The daily loop.

    def run(self) -> BacktestResult:
        previous: pd.Timestamp | None = None
        previous_equity = self.settings.initial_cash
        for day in self.days:
            flow = self._open(day, previous)
            positions = self._positions_usd(day)
            equity = self.broker.get_account().cash + sum(positions.values())
            if previous is not None:
                self._record_unlevered(day)
                self.twr = advance(self.twr, previous_equity, equity, flow)
            self._close_of_day(day, equity, positions)
            previous, previous_equity = day, equity
        return self._result()

    def _open(self, day: pd.Timestamp, previous: pd.Timestamp | None) -> float:
        """Interest, fills at the open, deposit. Returns the day's external flow."""
        if previous is not None:
            cash = self.broker.get_account().cash
            interest = account_cash_interest(
                cash,
                self.broker.get_account().equity,
                float(self.rate[day]),
                (day - previous).days,
                self.config,
            )
            if interest > 0:
                self.broker.credit_interest(interest)
        for order in self.broker.step(day.date()):
            self._record_fill(day, order)
        amount = self.config.cash_flows.monthly_deposit_usd
        if previous is not None and day.month != previous.month and amount > 0:
            self.broker.deposit(amount)
            self.rec.flows[day] = amount
            return amount
        return 0.0

    def _record_unlevered(self, day: pd.Timestamp) -> None:
        """Return of yesterday's unlevered target weights over day t, the rest of
        the equity in cash at the risk-free rate: the series Kelly is estimated on."""
        rf = float(self.rate[day]) / TRADING_DAYS_PER_YEAR
        position = self.return_rows.get(day)
        invested = sum(self.weights.values())
        gain = 0.0
        for asset, weight in self.weights.items():
            r = (
                math.nan
                if position is None
                else float(self.return_values[position, self.column[asset]])
            )
            gain += weight * (r if math.isfinite(r) else 0.0)
        # Net of costs (owner decision on PR #56, question B): yesterday's change in
        # unlevered weights pays the cost per dollar traded seen so far in this run.
        cost = self.turnover * self._cost_rate()
        self.turnover = 0.0
        self.rec.unlevered[day] = gain + (1 - invested) * rf - cost

    def _cost_rate(self) -> float:
        """Spread plus commission per dollar traded so far; before the first fill,
        the configured spread plus the commission cap (conservative)."""
        if self.traded > 0:
            return self.costs_paid / self.traded
        return self.config.costs.spread_bps / BPS + max_commission_rate(self.config)

    def _close_of_day(self, day: pd.Timestamp, equity: float, positions: dict[str, float]) -> None:
        cash = self.broker.get_account().cash
        gross = sum(v * self.multiple.get(s, 1) for s, v in positions.items())
        self.rec.equity.append(
            {
                "date": day,
                "equity": equity,
                "cash": cash,
                "gross_exposure": gross,
                "leverage": gross / equity if equity > 0 else 0.0,
                "twr_index": self.twr.index,
            }
        )
        if self.killed:
            self._record_risk(day, None, 0, 0.0, 0.0, KILLED, 0.0, [])
            return
        decision = kill_switch_decision(self.twr, self.config.risk.kill_switch_dd, [], [])
        if decision.fire or not equity > 0:
            self._liquidate(day, positions, decision.drawdown)
            return
        self._trade(day, equity, cash, positions)

    def _liquidate(self, day: pd.Timestamp, positions: dict[str, float], drawdown: float) -> None:
        """SPEC 7, step 4: cancel pending orders, sell every broker position at the
        next open, and stop trading."""
        self.killed = True
        self.broker.cancel_all()
        self.rec.event(day, "KILL_SWITCH", f"DRAWDOWN; drawdown {drawdown:.2%}")
        ctx = guard.GuardContext(
            equity=sum(positions.values()),
            positions_usd=positions,
            data_ok=True,
            kill_switch_active=True,
        )
        vetoes = []
        for symbol, value in sorted(positions.items()):
            order = guard.ProposedOrder(symbol, "sell", value)
            check = guard.check_liquidation(order, ctx)
            planned = PlannedOrder(symbol, "sell", value, False, self.underlying[symbol], KILLED)
            self._submit(day, planned, check, "market")
            if not check.approved:
                vetoes.append(f"{symbol}:sell:{'+'.join(check.reasons)}")
        self._record_risk(day, None, 0, 0.0, 0.0, KILLED, drawdown, vetoes)

    # Signals and sizing.

    def _signals(self, day: pd.Timestamp) -> tuple[dict[str, str], set[str]]:
        """Run the state machine on every asset with a bar on t, apply the unit caps
        and update the state. Returns the rule per asset and the blocked assets."""
        decisions: dict[str, tuple[Decision, PositionState, Row]] = {}
        blocked: set[str] = set()
        for asset, series in self.assets.items():
            row = series.rows.get(day)
            if row is None:
                blocked.add(asset)  # no bar on t (SPEC 5, validation 3)
                continue
            close, sma200, hh55, ll20, n = row
            state = self.state[asset]
            decisions[asset] = (
                decide(close, Indicators(sma200, hh55, ll20, n), state, self.config.strategy),
                state,
                row,
            )
        lots = [
            lot
            for lot in self.lots
            if lot.symbol not in decisions
            or decisions[lot.symbol][0].target_units >= decisions[lot.symbol][1].units
        ]
        for seq, asset in enumerate(self.assets):
            if asset not in decisions:
                continue
            decision, state = decisions[asset][0], decisions[asset][1]
            if decision.target_units > state.units:
                held = sum(lot.symbol == asset for lot in lots)
                lots += [UnitLot(day.date(), seq, asset)] * (decision.target_units - held)
        caps = apply_caps(lots, self.config)
        self.lots = caps.kept
        units = caps.units()
        rules = {}
        for asset, (decision, state, row) in decisions.items():
            kept = units.get(asset, 0)
            capped = kept < decision.target_units
            if not capped:
                self.state[asset] = PositionState(decision.target_units, decision.last_entry)
            elif kept == state.units:  # the add was cut: nothing changes
                self.state[asset] = state
            else:  # cannot happen (one add per day), kept for safety
                self.state[asset] = PositionState(kept, decision.last_entry if kept else None)
            rules[asset] = decision.rule
            close, sma200, hh55, ll20, n = row
            self.rec.signals.append(
                {
                    "date": day,
                    "symbol": asset,
                    "close": close,
                    "sma200": sma200,
                    "hh55": hh55,
                    "ll20": ll20,
                    "n": n,
                    "rule": decision.rule,
                    "current_units": state.units,
                    "target_units": self.state[asset].units,
                    "last_entry": self.state[asset].last_entry,
                    "capped": int(capped),
                }
            )
        return rules, blocked

    def _targets(self, day: pd.Timestamp, equity: float, blocked: set[str]) -> dict[str, float]:
        """Unlevered dollar targets from the units (SPEC 6), with the last known
        close and N (on or before t). An asset whose N or price is unusable is
        blocked for the day: its position is kept, no order is sent."""
        units, n, price = {}, {}, {}
        for asset, state in self.state.items():
            if state.units <= 0:
                continue
            row = self.assets[asset].last_row(day)
            if row is None or not (row[4] > 0 and math.isfinite(row[4]) and row[0] > 0):
                blocked.add(asset)
                continue
            units[asset], n[asset], price[asset] = state.units, row[4], row[0]
        return target_usd(units, equity, n, price, self.config.risk.risk_per_unit)

    def _sigma(self, day: pd.Timestamp, weights: Mapping[str, float]) -> float:
        held = [s for s, w in weights.items() if w != 0]
        if not held:
            return 0.0
        end = self.return_rows[day] + 1 if day in self.return_rows else 0
        history = self.returns.iloc[max(end - COVARIANCE_WINDOW_DAYS, 1) : end][held]
        return forward_volatility(weights, history)

    # Leverage, instruments, orders.

    def _trade(
        self, day: pd.Timestamp, equity: float, cash: float, positions: dict[str, float]
    ) -> None:
        rules, blocked = self._signals(day)
        unlevered = self._targets(day, equity, blocked)
        weights = {a: v / equity for a, v in unlevered.items()}
        self.turnover = sum(
            abs(weights.get(a, 0.0) - self.weights.get(a, 0.0)) for a in weights | self.weights
        )
        self.weights = weights
        sigma = self._sigma(day, self.weights)
        history = pd.Series(list(self.rec.unlevered.values())[-KELLY_WINDOW_DAYS:], dtype=float)
        rf = float(self.rate[day])
        kelly = kelly_estimate(history, rf, self.config.risk.min_history_days_for_kelly)
        drawdown = min(max(self.twr.drawdown, 0.0), 1.0)
        lev = effective_leverage(
            kelly,
            sigma=sigma,
            broker_leverage=1.0,
            equity=equity,
            drawdown=drawdown,
            delever_active=self.delever,
            risk=self.config.risk,
        )
        self.delever = lev.delever_active
        leverage, binding = lev.leverage, lev.binding_limit
        if self.settings.fixed_leverage is not None:
            leverage, binding = self.settings.fixed_leverage, FIXED
        exposures = {a: v * leverage for a, v in unlevered.items()}
        k = sum(exposures.values()) / (lev.borrow * equity)
        choices = choose_instruments(
            exposures,
            equity,
            eligible=self.data.eligible,
            current=self.held,
            switch=self.config.leverage_switch,
            max_etf_portfolio_leverage=self.config.risk.max_etf_portfolio_leverage,
            borrow=lev.borrow,
        )
        self._update_instruments(day, choices)
        targets = {c.symbol: c.cash_usd for c in choices.values()}
        planned, skipped = skip_costly_resizes(
            plan_orders(
                targets,
                positions_usd=positions,
                underlying=self.underlying,
                rules=rules,
                config=self.config,
            ),
            {symbol: self._close(symbol, day) for symbol in self.closes},
            self.config,
        )
        for order in skipped:
            self.rec.event(
                day, "RESIZE_SKIPPED", f"{order.symbol} buy {order.notional:.2f}: cost above limit"
            )
        planned = split_orders(planned, equity, self.config)
        vetoes = self._check_and_submit(day, equity, cash, positions, planned, blocked)
        self._record_risk(
            day, kelly.l_star, kelly.days, sigma, leverage, binding, drawdown, vetoes, k
        )

    def _update_instruments(
        self, day: pd.Timestamp, choices: Mapping[str, InstrumentChoice]
    ) -> None:
        held: dict[str, HeldInstrument] = {}
        for asset, choice in choices.items():
            before = self.held.get(asset)
            if before is not None and before.multiple == choice.multiple:
                held[asset] = HeldInstrument(choice.multiple, before.days_held + 1)
                continue
            held[asset] = HeldInstrument(choice.multiple, 0)
            previous = self.chosen.get(asset)
            if previous is not None and previous != choice.symbol:
                self.rec.switches.append(
                    {"date": day, "asset": asset, "from": previous, "to": choice.symbol}
                )
        self.held = held
        self.chosen = {asset: c.symbol for asset, c in choices.items()}

    def _check_and_submit(  # noqa: PLR0917
        self,
        day: pd.Timestamp,
        equity: float,
        cash: float,
        positions: dict[str, float],
        planned: list[PlannedOrder],
        blocked: set[str],
    ) -> list[str]:
        """Guard the planned orders, fit the approved buys to the cash the approved
        sells free, guard the result again (the batch changed), then submit."""
        ctx = guard.GuardContext(
            equity=equity,
            positions_usd=positions,
            data_ok=True,
            kill_switch_active=False,
            blocked=frozenset(blocked),
        )
        vetoes: list[str] = []
        first = self._guard(planned, ctx)
        rejected = [(p, d) for p, d in first if not d.approved]
        fitted = fit_buys([p for p, d in first if d.approved], cash, self.config)
        final = self._guard(fitted, ctx)
        for plan, check in rejected + final:
            self._submit(day, plan, check, self.settings.order_type)
            if not check.approved:
                vetoes.append(f"{plan.symbol}:{plan.side}:{'+'.join(check.reasons)}")
                self.rec.event(day, "VETO", vetoes[-1])
        return vetoes

    def _guard(
        self, planned: Sequence[PlannedOrder], ctx: guard.GuardContext
    ) -> list[tuple[PlannedOrder, guard.GuardDecision]]:
        proposed = [guard.ProposedOrder(p.symbol, p.side, p.notional, p.switch) for p in planned]
        decisions = {id(d.order): d for d in guard.check_orders(proposed, ctx, self.config)}
        return [(p, decisions[id(o)]) for p, o in zip(planned, proposed, strict=True)]

    def _submit(
        self, day: pd.Timestamp, plan: PlannedOrder, check: guard.GuardDecision, kind: OrderType
    ) -> None:
        limit = None
        if kind == "limit":
            offset = self.config.execution.limit_offset
            close = self._close(plan.symbol, day)
            limit = round(close * (1 + offset if plan.side == "buy" else 1 - offset), 2)
        order_id = None
        if check.approved:
            order_id = self.broker.submit_order(plan.symbol, plan.side, plan.notional, kind, limit)
        self.rec.orders.append(
            {
                "date": day,
                "order_id": order_id,
                "symbol": plan.symbol,
                "asset": plan.asset,
                "side": plan.side,
                "notional": plan.notional,
                "type": kind,
                "limit_price": limit,
                "rule": plan.rule,
                "switch": plan.switch,
                "approved": check.approved,
                "reasons": "+".join(check.reasons),
            }
        )

    # Records.

    def _record_fill(self, day: pd.Timestamp, order: OrderState) -> None:
        if order.status != "filled" or order.average_price is None:
            if order.status in {"rejected", "cancelled"}:
                self.rec.event(
                    day,
                    f"ORDER_{order.status.upper()}",
                    f"{order.order_id} {order.symbol} {order.side}: {order.reason}",
                )
            return
        bars = self.data.bars[order.symbol]
        reference = (
            float(bars["open"].to_numpy()[bars.index.get_loc(day)])
            if (order.type == "market")
            else math.nan
        )
        previous_close = self._close(order.symbol, day - timedelta(days=1))
        notional = order.filled_quantity * order.average_price
        spread = abs(order.average_price - reference) * order.filled_quantity
        if not math.isfinite(spread):  # a limit fill: the configured spread
            spread = notional * spread_bps(order.symbol, self.config) / BPS
        if order.reason:
            self.rec.event(
                day,
                "ORDER_PARTIAL",
                f"{order.order_id} {order.symbol} {order.side}: {order.reason}",
            )
        self.traded += notional
        self.costs_paid += order.commission + spread
        self.rec.fills.append(
            {
                "date": day,
                "order_id": order.order_id,
                "symbol": order.symbol,
                "side": order.side,
                "quantity": order.filled_quantity,
                "price": order.average_price,
                "notional": notional,
                "commission": order.commission,
                "spread_cost": spread,
                "slippage_vs_close": order.average_price / previous_close - 1,
            }
        )

    def _record_risk(  # noqa: PLR0917 - one value per risk_decisions column
        self,
        day: pd.Timestamp,
        l_star: float | None,
        days: int,
        sigma: float,
        leverage: float,
        binding: str,
        drawdown: float,
        vetoes: list[str],
        k: float = 0.0,
    ) -> None:
        self.rec.risk.append(
            {
                "date": day,
                "l_star": math.nan if l_star is None else l_star,
                "kelly_days": days,
                "sigma_60": sigma,
                "leverage": leverage,
                "binding_limit": binding,
                "drawdown": drawdown,
                "delever_active": int(self.delever),
                "k": k,
                "vetoes": json.dumps(vetoes),
            }
        )

    def _result(self) -> BacktestResult:
        rec = self.rec

        def frame(rows: list[dict[str, object]], index: str | list[str] | None) -> pd.DataFrame:
            out = pd.DataFrame(rows)
            return out.set_index(index) if index is not None and not out.empty else out

        flows = pd.Series(rec.flows, dtype=float)
        orders = pd.DataFrame(rec.orders)
        fills = pd.DataFrame(rec.fills)
        return BacktestResult(
            equity=frame(rec.equity, "date"),
            signals=frame(rec.signals, ["date", "symbol"]),
            risk_decisions=frame(rec.risk, "date"),
            events=pd.DataFrame(rec.events, columns=["at", "type", "detail"]),
            orders=orders,
            fills=fills,
            cash_flows=flows,
            unlevered_returns=pd.Series(rec.unlevered, dtype=float),
            switches=_switch_summary(pd.DataFrame(rec.switches), orders, fills),
        )


def _switch_summary(
    switches: pd.DataFrame, orders: pd.DataFrame, fills: pd.DataFrame
) -> pd.DataFrame:
    """Instrument switches per asset and their cost, spread plus commission (SPEC 9)."""
    columns = ["switches", "cost_usd"]
    if switches.empty:
        return pd.DataFrame(columns=columns, index=pd.Index([], name="asset"))
    counts = switches.groupby("asset").size()
    cost = pd.Series(0.0, index=counts.index)
    if not orders.empty and not fills.empty:
        legs = orders[orders["switch"] & orders["approved"]][["order_id", "asset"]]
        merged = fills.merge(legs, on="order_id")
        if not merged.empty:
            per_asset = (
                (merged["spread_cost"] + merged["commission"]).groupby(merged["asset"]).sum()
            )
            cost = per_asset.reindex(counts.index).fillna(0.0)
    out = pd.DataFrame({"switches": counts, "cost_usd": cost})
    out.index.name = "asset"
    return out


def run_backtest(
    data: MarketData, config: StrideConfig, settings: BacktestSettings
) -> BacktestResult:
    """Run the backtest over `settings.start`..`settings.end` (NYSE days)."""
    return Engine(data, config, settings).run()


def eligible_instruments(
    config: StrideConfig, passing: set[str], available: set[str]
) -> dict[str, dict[int, str]]:
    """Leveraged versions that pass the filters (`passing`) and have bars."""
    out: dict[str, dict[int, str]] = {}
    for asset, versions in config.leveraged_etfs.items():
        ok: dict[int, str] = {
            int(m): etf for m, etf in versions.items() if etf in passing and etf in available
        }
        if ok:
            out[asset] = ok
    return out


__all__ = [
    "BacktestResult",
    "BacktestSettings",
    "MarketData",
    "PlannedOrder",
    "eligible_instruments",
    "fit_buys",
    "plan_orders",
    "run_backtest",
    "skip_costly_resizes",
    "split_orders",
]
