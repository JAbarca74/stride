"""Walk-forward evaluation of the Monte Carlo forecast (design section 8).

Two tools, both without look-ahead: at row t they only pass rows 0..t of the panel
to the forecast.

- `forecast_table` records what the model said at each evaluation and what then
  happened, for the calibration measures.
- `run_trading` plays the decision rule and the weights through time with costs,
  the fill rule of the design (section 7) and the guard limits that matter here.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from stride.forecast.decide import MC_EXIT, decide, long_after
from stride.forecast.params import DecisionParams, ForecastParams, WeightParams
from stride.forecast.returns import FloatArray, IntArray, ReturnPanel
from stride.forecast.summary import Forecast, run_forecast
from stride.forecast.weights import target_weights

BPS = 10_000.0
MAX_ORDERS_PER_DAY = 12  # risk/guard.py
REBALANCE_BAND = 0.20  # SPEC section 8
DUST_USD = 1.0
CASH_EXPOSURE = 0.01  # below this share of equity invested, a day counts as in cash

Commission = Callable[[float, float], float]  # (shares, price) -> USD


def seed_for(base: int, row: int) -> int:
    """A reproducible seed for the evaluation after panel row `row`."""
    return int(np.random.SeedSequence([base, row]).generate_state(1)[0])


def last_slot_rows(panel: ReturnPanel) -> IntArray:
    """Rows that close a session (the evaluation after the market close)."""
    days = panel.day.to_numpy()
    last = np.ones(len(panel), dtype=bool)
    last[:-1] = days[1:] != days[:-1]
    rows: IntArray = np.flatnonzero(last).astype(np.int64)
    return rows


def realized(panel: ReturnPanel, row: int, horizon: int) -> FloatArray:
    """Simple return of each asset over the `horizon` steps after `row` (NaN if unknown)."""
    end = row + horizon
    if end >= len(panel):
        return np.full(len(panel.symbols), np.nan)
    with np.errstate(invalid="ignore"):
        out: FloatArray = panel.close[end] / panel.close[row] - 1.0
    return out


@dataclass(frozen=True)
class CostModel:
    """Spread per side and broker commission, as in `backtest/costs.py`."""

    spread_bps: FloatArray  # per asset
    commission: Commission

    def round_trip(self, price: FloatArray, order_usd: float) -> FloatArray:
        """Cost of buying and later selling `order_usd` of each asset, as a fraction."""
        out = np.full(len(price), np.nan)
        for asset, value in enumerate(price):
            if np.isfinite(value) and value > 0:
                fee = self.commission(order_usd / value, float(value))
                out[asset] = 2.0 * (fee / order_usd + self.spread_bps[asset] / BPS)
        return out


def forecast_at(
    panel: ReturnPanel,
    row: int,
    costs: CostModel,
    *,
    order_usd: float,
    params: ForecastParams,
    seed: int,
) -> Forecast | None:
    """The forecast made after `row` closed, or None while history is too short."""
    if row + 1 < params.window_steps + 1:
        return None
    cost = costs.round_trip(panel.close[row], order_usd)
    cost = np.where(np.isfinite(cost), cost, 0.0)
    known = panel.upto(row + 1)
    return run_forecast(known.ret[1:], known.slot[1:], cost, params, seed_for(seed, row))


def forecast_table(
    panel: ReturnPanel,
    rows: Sequence[int],
    costs: CostModel,
    *,
    order_usd: float,
    params: ForecastParams,
    seed: int,
) -> pd.DataFrame:
    """One line per (evaluation row, asset, horizon): the forecast and the outcome.

    Columns: row, end, symbol, horizon, p_up, p_net, mean, sigma, q05, q95, cost,
    realized. Lines without a forecast or without a known outcome are left out.
    """
    records: list[pd.DataFrame] = []
    for row in rows:
        forecast = forecast_at(
            panel, int(row), costs, order_usd=order_usd, params=params, seed=seed
        )
        if forecast is None:
            continue
        for horizon in params.horizons:
            summary = forecast.by_horizon[horizon]
            outcome = realized(panel, int(row), horizon)
            keep = forecast.valid & np.isfinite(outcome)
            if not keep.any():
                continue
            records.append(
                pd.DataFrame(
                    {
                        "row": int(row),
                        "end": panel.end[int(row)],
                        "symbol": np.array(panel.symbols)[keep],
                        "horizon": horizon,
                        "p_up": summary.p_up[keep],
                        "p_net": summary.p_net[keep],
                        "mean": summary.mean[keep],
                        "sigma": summary.sigma[keep],
                        "q05": summary.q05[keep],
                        "q95": summary.q95[keep],
                        "cost": (summary.mean - summary.edge)[keep],
                        "realized": outcome[keep],
                    }
                )
            )
    if not records:
        return pd.DataFrame(
            columns=[
                "row",
                "end",
                "symbol",
                "horizon",
                "p_up",
                "p_net",
                "mean",
                "sigma",
                "q05",
                "q95",
                "cost",
                "realized",
            ]
        )
    return pd.concat(records, ignore_index=True)


@dataclass(frozen=True)
class Rule:
    """One trading configuration: when to trade and how much."""

    name: str
    decision: DecisionParams
    weights: WeightParams


@dataclass
class _Order:
    fill_row: int
    asset: int
    side: str  # "buy" or "sell"
    notional: float  # for buys
    everything: bool  # sell the whole position
    reason: str


@dataclass
class _Book:
    cash: float
    shares: FloatArray
    peak: float
    stopped: bool = False
    pending: list[_Order] = field(default_factory=list)
    trades: list[dict[str, object]] = field(default_factory=list)
    equity: list[tuple[pd.Timestamp, float, float]] = field(default_factory=list)
    day: pd.Timestamp | None = None
    orders_today: int = 0
    bought_today: set[int] = field(default_factory=set)
    sold_today: set[int] = field(default_factory=set)


@dataclass(frozen=True)
class TradingResult:
    """What one rule did. `equity` is indexed by session with columns equity, exposure."""

    rule: Rule
    equity: pd.DataFrame
    trades: pd.DataFrame
    killed: bool


def _fill_row(panel: ReturnPanel, row: int) -> int:
    """The first slot that starts after the evaluation of `row` (design section 7)."""
    closes_session = row + 1 >= len(panel) or panel.day[row + 1] != panel.day[row]
    return row + 1 if closes_session else row + 2


def _fill_price(panel: ReturnPanel, row: int, asset: int) -> float:
    price = panel.open[row, asset]
    if not np.isfinite(price):  # no trade in that slot: the last known price
        price = panel.close[row - 1, asset]
    return float(price)


def _execute(book: _Book, order: _Order, panel: ReturnPanel, costs: CostModel) -> None:
    asset = order.asset
    reference = _fill_price(panel, order.fill_row, asset)
    if not np.isfinite(reference) or reference <= 0:
        return
    move = costs.spread_bps[asset] / BPS
    if order.side == "sell":
        shares = (
            book.shares[asset]
            if order.everything
            else min(book.shares[asset], order.notional / reference)
        )
        if shares <= 0:
            return
        price = reference * (1.0 - move)
        fee = costs.commission(float(shares), price)
        book.cash += shares * price - fee
        book.shares[asset] -= shares
        notional = shares * price
    else:
        price = reference * (1.0 + move)
        notional = min(order.notional, book.cash)
        if notional <= DUST_USD:
            return
        shares = notional / price
        fee = costs.commission(float(shares), price)
        if notional + fee > book.cash:  # leave room for the commission
            notional = book.cash - fee
            if notional <= DUST_USD:
                return
            shares = notional / price
        book.cash -= notional + fee
        book.shares[asset] += shares
    book.trades.append(
        {
            "end": panel.end[order.fill_row],
            "symbol": panel.symbols[asset],
            "side": order.side,
            "notional": float(notional),
            "price": float(price),
            "commission": float(fee),
            "spread_cost": float(abs(price - reference) * shares),
            "reason": order.reason,
        }
    )


def _place(book: _Book, order: _Order) -> bool:
    """Queue `order` unless a guard limit forbids it (SPEC section 7, step 3)."""
    if book.orders_today >= MAX_ORDERS_PER_DAY:
        return False
    if order.side == "buy" and order.asset in book.sold_today:
        return False
    if order.side == "sell" and order.asset in book.bought_today:
        return False
    (book.bought_today if order.side == "buy" else book.sold_today).add(order.asset)
    book.orders_today += 1
    book.pending.append(order)
    return True


def _evaluate(
    book: _Book,
    rule: Rule,
    forecast: Forecast,
    panel: ReturnPanel,
    *,
    row: int,
    fill_row: int,
) -> None:
    close = panel.close[row]
    value = np.where(book.shares > 0, book.shares * close, 0.0)
    equity = book.cash + float(value.sum())
    busy = {order.asset for order in book.pending}
    held = book.shares > 0
    actions = decide(forecast, held, rule.decision)
    long = long_after(actions, held)
    weights = target_weights(
        forecast,
        long,
        panel.symbols,
        equity=equity,
        horizon=rule.decision.horizon,
        params=rule.weights,
    )
    target = weights * equity
    sells: list[_Order] = []
    buys: list[_Order] = []
    for asset in range(len(panel.symbols)):
        if asset in busy:
            continue
        if held[asset] and target[asset] <= 0:
            reason = actions[asset] if actions[asset] == MC_EXIT else "MC_DROP"
            sells.append(_Order(fill_row, asset, "sell", 0.0, True, reason))
        elif not held[asset] and target[asset] > 0:
            buys.append(_Order(fill_row, asset, "buy", float(target[asset]), False, "MC_ENTER"))
        elif held[asset]:
            gap = float(target[asset] - value[asset])
            threshold = max(REBALANCE_BAND * target[asset], rule.weights.min_order_usd)
            if abs(gap) >= threshold:
                side = "buy" if gap > 0 else "sell"
                (buys if gap > 0 else sells).append(
                    _Order(fill_row, asset, side, abs(gap), False, "MC_RESIZE")
                )
    for order in sells + buys:  # sells first, to free cash
        _place(book, order)


def run_trading(
    panel: ReturnPanel,
    rows: Sequence[int],
    forecasts: Callable[[int], Forecast | None],
    rules: Sequence[Rule],
    costs: CostModel,
    *,
    initial_equity: float,
    kill_switch_dd: float,
) -> list[TradingResult]:
    """Trade every rule over the evaluation `rows` with the same forecasts.

    `forecasts(row)` returns the forecast made after `row` closed (None = skip).
    An order decided after row t fills at the open of the first slot that starts
    after the evaluation, with spread and commission. A drawdown of
    `kill_switch_dd` from the peak, measured at a session close, sells everything
    at the next open and stops that rule for good.
    """
    size = len(panel.symbols)
    books = [_Book(initial_equity, np.zeros(size), initial_equity) for _ in rules]
    wanted = {int(row) for row in rows}
    session_end = set(last_slot_rows(panel).tolist())
    for row in range(len(panel)):
        day = panel.day[row]
        for book in books:
            due = [order for order in book.pending if order.fill_row == row]
            if due:
                book.pending = [order for order in book.pending if order.fill_row != row]
                for order in sorted(due, key=lambda item: item.side != "sell"):
                    _execute(book, order, panel, costs)
            if book.day != day:
                book.day, book.orders_today = day, 0
                book.bought_today, book.sold_today = set(), set()
        fill_row = _fill_row(panel, row)
        can_fill = fill_row < len(panel)
        forecast = forecasts(row) if row in wanted and can_fill else None
        for book, rule in zip(books, rules, strict=True):
            if forecast is not None and not book.stopped:
                _evaluate(book, rule, forecast, panel, row=row, fill_row=fill_row)
            if row in session_end:
                value = np.where(book.shares > 0, book.shares * panel.close[row], 0.0)
                equity = book.cash + float(value.sum())
                book.peak = max(book.peak, equity)
                book.equity.append(
                    (day, equity, float(value.sum()) / equity if equity > 0 else 0.0)
                )
                if not book.stopped and equity <= book.peak * (1.0 - kill_switch_dd):
                    book.stopped = True
                    book.pending = [
                        _Order(row + 1, int(asset), "sell", 0.0, True, "KILL_SWITCH")
                        for asset in np.flatnonzero(book.shares > 0)
                    ]
    results = []
    for book, rule in zip(books, rules, strict=True):
        curve = pd.DataFrame(book.equity, columns=["day", "equity", "exposure"]).set_index("day")
        trades = pd.DataFrame(
            book.trades,
            columns=[
                "end",
                "symbol",
                "side",
                "notional",
                "price",
                "commission",
                "spread_cost",
                "reason",
            ],
        )
        results.append(TradingResult(rule, curve, trades, book.stopped))
    return results


def trading_metrics(result: TradingResult, initial_equity: float) -> dict[str, float]:
    """Net return, drawdown, activity and costs of one rule (no benchmark)."""
    equity = result.equity["equity"]
    if equity.empty:
        return {}
    curve = pd.concat([pd.Series([initial_equity]), equity.reset_index(drop=True)])
    drawdown = float((1.0 - curve / curve.cummax()).max())
    years = max(len(equity) / 252.0, 1e-9)
    final = float(equity.iloc[-1])
    daily = curve.pct_change().dropna()
    volatility = float(daily.std() * np.sqrt(252.0)) if len(daily) > 1 else 0.0
    cagr = (final / initial_equity) ** (1.0 / years) - 1.0 if final > 0 else -1.0
    costs = float(result.trades["commission"].sum() + result.trades["spread_cost"].sum())
    return {
        "final_equity": final,
        "total_return": final / initial_equity - 1.0,
        "cagr": cagr,
        "volatility": volatility,
        "sharpe": cagr / volatility if volatility > 0 else 0.0,
        "max_drawdown": drawdown,
        "trades": float(len(result.trades)),
        "costs_usd": costs,
        "costs_pct_of_initial": costs / initial_equity,
        "avg_exposure": float(result.equity["exposure"].mean()),
        "days_in_cash_share": float((result.equity["exposure"] < CASH_EXPOSURE).mean()),
        "killed": float(result.killed),
    }
