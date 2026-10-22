"""Risk manager veto (SPEC section 7, step 3).

HUMAN-REVIEWED MODULE: read the whole file before merging. Pure, no I/O.

Every order passes `check_orders` before it reaches the broker. All applicable
vetoes are reported, not only the first. Orders are checked sells first, and each
approved order updates the context, so the daily count and the same-day rule hold
across the batch.

Same-day rule (SPEC 1.4): buying and selling the same underlying on one day is
vetoed, except an instrument switch (e.g. SPY -> SSO) that meets all of:
the orders are marked `switch` by the leverage module, the sell went first, and
the underlying's net exposure changes by less than the rebalance band.

While the system is halted (reconciliation, SPEC 1.4) no order is approved; the
kill-switch liquidation still goes through `check_liquidation`.
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
HALTED = "HALTED"
UNKNOWN_SYMBOL = "UNKNOWN_SYMBOL"
INVALID_ORDER = "INVALID_ORDER"

SHORT_TOLERANCE_USD = 0.01  # a sell may exceed the held value by rounding, no more


@dataclass(frozen=True)
class ProposedOrder:
    symbol: str
    side: Side
    notional: float
    switch: bool = False  # part of an instrument switch chosen by risk/leverage.py


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
    halted: bool = False


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


def multiple_map(config: StrideConfig) -> dict[str, int]:
    """Exposure per dollar of each symbol: 1 for the universe, m for 2x/3x versions."""
    multiples = dict.fromkeys(config.universe, 1)
    for versions in config.leveraged_etfs.values():
        multiples.update({etf: m for m, etf in versions.items()})
    return multiples


def _allowed_switch(
    order: ProposedOrder, asset: str, ctx: GuardContext, config: StrideConfig
) -> bool:
    """A same-day buy after sells of the same underlying is allowed only as a
    marked instrument switch whose net exposure moves less than the band."""
    if order.side != "buy" or not order.switch:
        return False
    underlying, multiple = underlying_map(config), multiple_map(config)
    today = [o for o in ctx.approved_today if underlying.get(o.symbol) == asset]
    if any(not o.switch for o in today):
        return False
    sold = sum(o.notional * multiple[o.symbol] for o in today if o.side == "sell")
    bought = sum(o.notional * multiple[o.symbol] for o in today if o.side == "buy")
    held_now = sum(
        value * multiple[symbol]
        for symbol, value in ctx.positions_usd.items()
        if underlying.get(symbol) == asset
    )
    before = held_now + sold - bought  # exposure before today's switch orders
    net_change = bought + order.notional * multiple[order.symbol] - sold
    return before > 0 and abs(net_change) < config.execution.rebalance_band * before


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
    if ctx.halted:
        reasons.append(HALTED)
    if not ctx.data_ok:
        reasons.append(DATA_FAILED)
    if asset is not None and asset in ctx.blocked:
        reasons.append(SYMBOL_BLOCKED)
    if order.side == "sell" and not _can_sell(order, ctx):
        reasons.append(SHORT)
    # Fail safe: a non-finite equity cannot bound the order size, so it is vetoed.
    if (
        not math.isfinite(ctx.equity)
        or not ctx.equity > 0
        or order.notional > config.execution.max_order_pct_equity * ctx.equity
    ):
        reasons.append(ORDER_TOO_LARGE)
    if len(ctx.approved_today) >= config.execution.max_orders_per_day:
        reasons.append(TOO_MANY_ORDERS)
    if (
        asset is not None
        and any(
            underlying.get(done.symbol) == asset and done.side != order.side
            for done in ctx.approved_today
        )
        and not _allowed_switch(order, asset, ctx, config)
    ):
        reasons.append(SAME_DAY_REVERSAL)
    if order.side == "buy" and not _margin_ok(order, ctx, config):
        reasons.append(MARGIN_BUFFER)
    return GuardDecision(order, not reasons, tuple(reasons))


def _can_sell(order: ProposedOrder, ctx: GuardContext) -> bool:
    """The sell stays within the held value. A non-finite holding cannot be
    checked, so it fails (fail safe)."""
    held = ctx.positions_usd.get(order.symbol, 0.0)
    return math.isfinite(held) and held > 0 and order.notional <= held + SHORT_TOLERANCE_USD


def _margin_ok(order: ProposedOrder, ctx: GuardContext, config: StrideConfig) -> bool:
    """Excess liquidity after the buy keeps the margin buffer. Non-finite or
    negative margin figures cannot be checked, so they fail (fail safe)."""
    requirement, excess = ctx.margin_requirement, ctx.excess_liquidity
    if not (math.isfinite(requirement) and math.isfinite(excess)) or requirement < 0:
        return False
    return requirement == 0 or excess - order.notional >= config.risk.margin_buffer * requirement


def _after(order: ProposedOrder, ctx: GuardContext) -> GuardContext:
    """The context once `order` is approved."""
    sign = 1.0 if order.side == "buy" else -1.0
    positions = dict(ctx.positions_usd)
    # Not clamped at 0: a sell that used the rounding tolerance leaves a tiny
    # negative balance, so the tolerance cannot be used again in the same batch.
    positions[order.symbol] = positions.get(order.symbol, 0.0) + sign * order.notional
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
    if not _can_sell(order, ctx):
        reasons.append(SHORT)
    return GuardDecision(order, not reasons, tuple(reasons))


def approved(decisions: Sequence[GuardDecision]) -> list[ProposedOrder]:
    return [d.order for d in decisions if d.approved]
