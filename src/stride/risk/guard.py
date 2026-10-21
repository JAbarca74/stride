"""Risk manager veto (SPEC section 7, step 3).

HUMAN-REVIEWED MODULE: read the whole file before merging. Pure, no I/O.

Every order passes `check_orders` before it reaches the broker. All applicable
vetoes are reported, not only the first. Orders are checked sells first, and each
approved order updates the context, so the daily count and the same-day rule hold
across the batch.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Literal

from stride.config import StrideConfig

Side = Literal["buy", "sell"]

SHORT = "SHORT"
ORDER_TOO_LARGE = "ORDER_TOO_LARGE"
TOO_MANY_ORDERS = "TOO_MANY_ORDERS"
SAME_DAY_REVERSAL = "SAME_DAY_REVERSAL"
MARGIN_BUFFER = "MARGIN_BUFFER"
DATA_FAILED = "DATA_FAILED"
SYMBOL_BLOCKED = "SYMBOL_BLOCKED"
KILL_SWITCH = "KILL_SWITCH"
UNKNOWN_SYMBOL = "UNKNOWN_SYMBOL"
INVALID_ORDER = "INVALID_ORDER"

SHORT_TOLERANCE_USD = 0.01  # a sell may exceed the held value by rounding, no more


@dataclass(frozen=True)
class ProposedOrder:
    symbol: str
    side: Side
    notional: float


@dataclass(frozen=True)
class GuardContext:
    """What the guard knows on the day. `positions_usd` is the market value held
    per symbol; `approved_today` are the orders already approved today; `blocked`
    holds the underlyings blocked by section 5 validations 3 and 4."""

    equity: float
    positions_usd: Mapping[str, float]
    data_ok: bool
    kill_switch_active: bool
    blocked: frozenset[str] = frozenset()
    approved_today: tuple[ProposedOrder, ...] = ()
    excess_liquidity: float = 0.0
    margin_requirement: float = 0.0


@dataclass(frozen=True)
class GuardDecision:
    order: ProposedOrder
    approved: bool
    reasons: tuple[str, ...] = field(default_factory=tuple)


def underlying_map(config: StrideConfig) -> dict[str, str]:
    """Every tradable symbol -> its underlying asset (leveraged versions included)."""
    mapping = {symbol: symbol for symbol in config.universe}
    for asset, versions in config.leveraged_etfs.items():
        mapping.update({etf: asset for etf in versions.values()})
    return mapping


def check_order(order: ProposedOrder, ctx: GuardContext, config: StrideConfig) -> GuardDecision:
    """Every veto that applies to `order` in `ctx` (SPEC 7, step 3)."""
    underlying = underlying_map(config)
    reasons: list[str] = []
    asset = underlying.get(order.symbol)
    if asset is None:
        reasons.append(UNKNOWN_SYMBOL)
    if not math.isfinite(order.notional) or order.notional <= 0:
        reasons.append(INVALID_ORDER)
    if ctx.kill_switch_active:
        reasons.append(KILL_SWITCH)
    if not ctx.data_ok:
        reasons.append(DATA_FAILED)
    if asset is not None and asset in ctx.blocked:
        reasons.append(SYMBOL_BLOCKED)
    held = ctx.positions_usd.get(order.symbol, 0.0)
    if order.side == "sell" and order.notional > held + SHORT_TOLERANCE_USD:
        reasons.append(SHORT)
    if not ctx.equity > 0 or order.notional > config.execution.max_order_pct_equity * ctx.equity:
        reasons.append(ORDER_TOO_LARGE)
    if len(ctx.approved_today) >= config.execution.max_orders_per_day:
        reasons.append(TOO_MANY_ORDERS)
    if asset is not None and any(
        underlying.get(done.symbol) == asset and done.side != order.side
        for done in ctx.approved_today
    ):
        reasons.append(SAME_DAY_REVERSAL)
    if (
        order.side == "buy"
        and ctx.margin_requirement > 0
        and ctx.excess_liquidity - order.notional
        < config.risk.margin_buffer * ctx.margin_requirement
    ):
        reasons.append(MARGIN_BUFFER)
    return GuardDecision(order, not reasons, tuple(reasons))


def _after(order: ProposedOrder, ctx: GuardContext) -> GuardContext:
    """The context once `order` is approved."""
    sign = 1.0 if order.side == "buy" else -1.0
    positions = dict(ctx.positions_usd)
    positions[order.symbol] = max(positions.get(order.symbol, 0.0) + sign * order.notional, 0.0)
    return replace(
        ctx,
        positions_usd=positions,
        approved_today=(*ctx.approved_today, order),
        excess_liquidity=ctx.excess_liquidity - order.notional * (order.side == "buy"),
    )


def check_orders(
    orders: Iterable[ProposedOrder], ctx: GuardContext, config: StrideConfig
) -> list[GuardDecision]:
    """Check a batch in execution order (sells first), carrying approvals forward."""
    decisions = []
    for order in sorted(orders, key=lambda o: o.side != "sell"):
        decision = check_order(order, ctx, config)
        if decision.approved:
            ctx = _after(order, ctx)
        decisions.append(decision)
    return decisions


def check_liquidation(order: ProposedOrder, ctx: GuardContext) -> GuardDecision:
    """Kill-switch exit orders skip the size, count, data and same-day limits, but
    never the long-only rule: they can only sell what is held."""
    reasons = []
    if order.side != "sell":
        reasons.append(KILL_SWITCH)
    if not math.isfinite(order.notional) or order.notional <= 0:
        reasons.append(INVALID_ORDER)
    if order.notional > ctx.positions_usd.get(order.symbol, 0.0) + SHORT_TOLERANCE_USD:
        reasons.append(SHORT)
    return GuardDecision(order, not reasons, tuple(reasons))


def approved(decisions: Sequence[GuardDecision]) -> list[ProposedOrder]:
    return [d.order for d in decisions if d.approved]
