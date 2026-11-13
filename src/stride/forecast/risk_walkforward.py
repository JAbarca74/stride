"""Daily walk-forward of the risk-allocation variants (design section 3 and gate R2).

Each variant is one simulated account. The account, its fills, costs, guard limits
and the 20% kill switch are the existing `Trader` of `walkforward.py`, driven
unchanged; this module only decides the orders:

- on the first session of each month the weights are recomputed from the joint
  simulated paths (allocator, trend brake, limits);
- on every session the loss cap and the drawdown brake can lower the exposure, and
  only lower it, until the next monthly rebalance;
- an order decided after a close fills at the next session open.

No look-ahead: at row t only rows 0..t are read.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from stride.forecast.daily import BoolArray
from stride.forecast.daily_risk import DailyRiskParams, simulate_joint
from stride.forecast.params import DecisionParams
from stride.forecast.returns import FloatArray, ReturnPanel
from stride.forecast.risk_weights import (
    RiskRule,
    allocate,
    apply_limits,
    drawdown_scale,
    loss_scale,
    trend_ok,
)
from stride.forecast.walkforward import (
    REBALANCE_BAND,
    TRADE_COLUMNS,
    CostModel,
    Rule,
    Trader,
    TradingResult,
    _Order,
    _place,
    seed_for,
)

RISK_ENTER = "RISK_ENTER"
RISK_EXIT = "RISK_EXIT"
RISK_REDUCE = "RISK_REDUCE"
RISK_RESIZE = "RISK_RESIZE"

ALLOCATION_HORIZON = 20  # sessions: the horizon whose simulated risk sizes the weights
LOSS_QUANTILES = (0.05, 0.01)

# (panel, row, valid) -> weights over the valid assets, summing to 1. Replaces the
# allocator of a rule; used by the ablation that sizes with a plain trailing volatility.
Weigher = Callable[[ReturnPanel, int, BoolArray], FloatArray]

LOG_COLUMNS = (
    "day",
    "row",
    "rebalance",
    "scale",
    "cap_scale",
    "dd_scale",
    "base_total",
    "held_q05",
    "held_q01",
)


@dataclass(frozen=True)
class RiskResult:
    """What one variant did: the `TradingResult` plus the daily decision log and the
    weights held at each close (fraction of equity, one column per asset)."""

    rule: RiskRule
    trading: TradingResult
    log: pd.DataFrame
    held: pd.DataFrame


class RiskTrader:
    """One variant's account. `decide` is called once per session, after the close."""

    def __init__(
        self,
        rule: RiskRule,
        costs: CostModel,
        *,
        initial_equity: float,
        kill_switch_dd: float,
        size: int,
        weigher: Weigher | None = None,
    ) -> None:
        self.rule = rule
        self.weigher = weigher
        self.trader = Trader(
            Rule(rule.name, DecisionParams(), rule.limits),
            costs,
            initial_equity=initial_equity,
            kill_switch_dd=kill_switch_dd,
            size=size,
        )
        self.base: FloatArray = np.zeros(
            size
        )  # weights of the last monthly rebalance, after limits
        self.scale = 1.0  # lowest brake factor since that rebalance
        self.month: tuple[int, int] | None = None
        self.backlog: set[int] = set()  # assets whose rebalance order did not fit a day
        self.log: list[tuple[object, ...]] = []
        self.held: list[FloatArray] = []

    def _rebalance(
        self, panel: ReturnPanel, row: int, valid: BoolArray, paths: FloatArray, equity: float
    ) -> None:
        rule = self.rule
        weights = np.zeros(len(panel.symbols))
        if self.weigher is not None:
            weights[valid] = self.weigher(panel, row, valid)
        else:
            weights[valid] = allocate(rule.allocator, paths)
        if rule.brake == "trend":
            weights[~trend_ok(panel.close[: row + 1], rule.trend_days)] = 0.0
        self.base = apply_limits(weights, panel.symbols, equity=equity, params=rule.limits)
        self.scale = 1.0
        self.backlog = set()

    def decide(
        self,
        panel: ReturnPanel,
        row: int,
        valid: BoolArray,
        allocation_paths: FloatArray,
        risk_paths: FloatArray,
    ) -> None:
        """Place the orders decided at the close of `row`; they fill at the next open.

        `allocation_paths` and `risk_paths` are the joint simulated simple returns
        [P, valid.sum()] at the allocation and the loss-cap horizon.
        """
        book = self.trader.book
        if book.stopped or not valid.any():  # nothing to hold before the first full window
            return
        rule, limits = self.rule, self.rule.limits
        size = len(panel.symbols)
        close = panel.close[row]
        value = np.where(book.shares > 0, book.shares * close, 0.0)
        equity = book.cash + float(value.sum())
        if equity <= 0:
            return
        day = panel.day[row]
        month = (int(day.year), int(day.month))
        rebalance = month != self.month
        if rebalance:
            self.month = month
            self._rebalance(panel, row, valid, allocation_paths, equity)
        joint = np.zeros((risk_paths.shape[0], size))
        joint[:, valid] = risk_paths
        cap_scale = loss_scale(self.base, joint, limits.risk_quantile, limits.max_portfolio_loss)
        dd_scale = 1.0
        if rule.brake == "dd":
            peak = max(book.peak, equity)
            dd_scale = drawdown_scale(1.0 - equity / peak, rule.dd_floor)
        self.scale = min(self.scale, cap_scale, dd_scale)
        # The minimum order was applied to the monthly targets (`apply_limits`). A brake
        # that shrinks a held position below it does not sell the whole position: the
        # reduction itself must reach the minimum order (`_orders`).
        target = self.base * self.scale * equity
        held_weights = value / equity
        q05, q01 = np.quantile(joint @ held_weights, list(LOSS_QUANTILES))
        self.log.append(
            (
                day,
                row,
                rebalance,
                self.scale,
                cap_scale,
                dd_scale,
                float(self.base.sum()),
                float(q05),
                float(q01),
            )
        )
        self.held.append(held_weights)
        self._orders(target, value, rebalance)

    def _orders(self, target: FloatArray, value: FloatArray, rebalance: bool) -> None:
        book = self.trader.book
        due = self.trader.counter + 1  # the next session open
        busy = {order.asset for order in book.pending}
        minimum = self.rule.limits.min_order_usd
        sells: list[_Order] = []
        buys: list[_Order] = []
        for asset in range(len(target)):
            if asset in busy:
                continue
            may_buy = rebalance or asset in self.backlog
            held = bool(book.shares[asset] > 0)
            if held and target[asset] <= 0:
                sells.append(_Order(due, asset, "sell", 0.0, True, RISK_EXIT))
            elif not held and target[asset] > 0:
                if may_buy and target[asset] >= minimum:
                    buys.append(_Order(due, asset, "buy", float(target[asset]), False, RISK_ENTER))
            elif held:
                gap = float(target[asset] - value[asset])
                if abs(gap) < max(REBALANCE_BAND * target[asset], minimum):
                    continue
                if gap < 0:
                    sells.append(_Order(due, asset, "sell", -gap, False, RISK_REDUCE))
                elif may_buy:
                    buys.append(_Order(due, asset, "buy", gap, False, RISK_RESIZE))
        failed: set[int] = set()
        for order in sells + buys:  # sells first, to free cash and order slots
            if not _place(book, order):
                failed.add(order.asset)
        self.backlog = failed

    def to_state(self) -> dict[str, Any]:
        """The account and the allocation state as plain data (JSON-safe).

        The decision log and the held weights are not part of it: a live job saves
        them as they happen and clears them.
        """
        return {
            "trader": self.trader.to_state(),
            "base": [float(x) for x in self.base],
            "scale": float(self.scale),
            "month": None if self.month is None else list(self.month),
            "backlog": sorted(int(x) for x in self.backlog),
        }

    @classmethod
    def from_state(
        cls,
        state: dict[str, Any],
        rule: RiskRule,
        costs: CostModel,
        *,
        kill_switch_dd: float,
    ) -> RiskTrader:
        """Rebuild an account saved by `to_state`."""
        base = np.array(state["base"], dtype=float)
        out = cls(rule, costs, initial_equity=1.0, kill_switch_dd=kill_switch_dd, size=len(base))
        out.trader = Trader.from_state(
            state["trader"], out.trader.rule, costs, kill_switch_dd=kill_switch_dd
        )
        out.base = base
        out.scale = float(state["scale"])
        month = state["month"]
        out.month = None if month is None else (int(month[0]), int(month[1]))
        out.backlog = {int(x) for x in state["backlog"]}
        return out

    def result(self, panel: ReturnPanel) -> RiskResult:
        """The account's history as tables."""
        book = self.trader.book
        curve = pd.DataFrame(book.equity, columns=["day", "equity", "exposure"]).set_index("day")
        trades = pd.DataFrame(book.trades, columns=list(TRADE_COLUMNS))
        trading = TradingResult(self.trader.rule, curve, trades, book.stopped)
        log = pd.DataFrame(self.log, columns=list(LOG_COLUMNS))
        held = pd.DataFrame(
            np.array(self.held).reshape(len(self.held), len(panel.symbols)),
            columns=list(panel.symbols),
            index=pd.DatetimeIndex(log["day"]),
        )
        return RiskResult(self.rule, trading, log, held)


def step_session(
    panel: ReturnPanel,
    row: int,
    traders: Sequence[RiskTrader],
    params: DailyRiskParams,
    seed: int,
    *,
    decide: bool = True,
) -> None:
    """One session for every trader: fills at its open, the decision after its close
    (unless `decide` is False), then equity and the kill switch.

    The backtest and the live job both go through this function, so they cannot
    drift apart. The simulation is shared by all traders and reads rows up to `row`.
    """
    if ALLOCATION_HORIZON not in params.horizons:
        raise ValueError(f"params.horizons must contain {ALLOCATION_HORIZON}")
    alloc_index = params.horizons.index(ALLOCATION_HORIZON)
    for trader in traders:
        trader.trader.open_slot(panel, row)
    if decide:
        valid, paths = simulate_joint(panel, row, params, seed)
        for trader in traders:
            horizon = trader.rule.limits.risk_horizon
            if horizon not in params.horizons:
                raise ValueError(f"risk_horizon {horizon} is not simulated")
            trader.decide(
                panel, row, valid, paths[alloc_index], paths[params.horizons.index(horizon)]
            )
    for trader in traders:
        trader.trader.close_slot(panel, row, session_end=True)


def run_risk_trading(
    panel: ReturnPanel,
    start: int,
    stop: int,
    traders: Sequence[RiskTrader],
    *,
    params: DailyRiskParams,
    base_seed: int,
) -> list[RiskResult]:
    """Play every trader over rows `start` to `stop - 1` with the same simulations.

    The account opens in cash at `start`. The last row places no order (nothing after
    it could fill). The simulation of a row is shared by all traders and depends
    only on the rows up to it and on `base_seed`.
    """
    if not 0 <= start < stop <= len(panel):
        raise ValueError("need 0 <= start < stop <= len(panel)")
    if ALLOCATION_HORIZON not in params.horizons:
        raise ValueError(f"params.horizons must contain {ALLOCATION_HORIZON}")
    for row in range(start, stop):
        step_session(panel, row, traders, params, seed_for(base_seed, row), decide=row + 1 < stop)
    return [trader.result(panel) for trader in traders]
