"""Current vs target positions -> orders, and the only door to the broker
(SPEC section 8, order flow; docs/design/execution_modules.md, section 1).

HUMAN-REVIEWED MODULE: read the whole file before merging. Created with the
owner's approval of 2026-10-24 (design answers Q1, Q4, Q5, Q7 and Q8).

Planning is pure and shared with the backtest: `plan` calls the engine's
`plan_orders`, `skip_costly_resizes` and `split_orders`, and `approve` calls its
`approve_batch` (guard, fit the buys to the cash, guard again), so paper and
backtest follow one set of rules:

- every symbol in the targets or held: dollars to buy or sell; a symbol held but
  not targeted is sold in full;
- rebalance band: no order below `rebalance_band` x target or `min_order_usd`;
  full exits and both legs of an instrument switch always pass the band, the
  $1 minimum still applies (Q1);
- cost-aware resizing, the split above `max_order_pct_equity`, sells first.

`price` turns the approved orders into what the broker receives (SPEC 8, step 4):
a limit at the previous close +/- `limit_offset` where the broker accepts limits,
otherwise a market day order sent before the open, which fills at the open.
Alpaca is not relied on for fractional limit or market-on-open orders, so
`limits_allowed` is False and every order is a market order. A full exit is
sent as the number of shares held, so a gap at the open leaves no remainder. A
symbol that is not fractionable trades whole shares, floored at the previous
close; below one share the order is skipped (`ORDER_TOO_SMALL`).

`submit_approved` is the only code that calls `Broker.submit_order`:

- sells first; nothing without an approved `GuardDecision`;
- the `KILL_SWITCH` and `HALT` files are read again right before each send, and
  only kill-switch liquidations go out while either exists (SPEC 1.4);
- each order is stored with its deterministic client id
  `stride-YYYYMMDD-SYMBOL-side-seq` before the call; after any error the broker
  is asked for that id (`find_order`) before anything else is decided, and an
  order whose fate is unknown is never resent (SPEC 8);
- one run never sends the same client id twice.
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pandas as pd

from stride.backtest.engine import (
    PlannedOrder,
    approve_batch,
    plan_orders,
    skip_costly_resizes,
    split_orders,
)
from stride.config import StrideConfig
from stride.data import store
from stride.execution.broker import Broker, OrderState, OrderType, Position
from stride.execution.kill_switch_file import is_active, is_halted
from stride.risk import guard

ORDER_TOO_SMALL = "ORDER_TOO_SMALL"  # whole shares: below one share at the previous close
KILL_SWITCH_RULE = "KILL_SWITCH"  # rule of the kill-switch liquidation sells
SEND_FAILED = "SEND_FAILED"  # the broker call failed and the broker has no such order
SEND_UNKNOWN = "SEND_UNKNOWN"  # the call failed and so did the lookup: fate unknown
_SHARE_EPSILON = 1e-9  # absorbs float error when flooring to whole shares
_OPEN = ("sending", "pending", "unknown")  # sent, or maybe sent, and not final


@dataclass(frozen=True)
class Order:
    """An order as the broker receives it, with its deterministic client id."""

    client_id: str
    symbol: str
    side: guard.Side
    notional: float
    type: OrderType
    limit_price: float | None
    switch: bool
    rule: str
    quantity: float | None = None  # shares instead of notional (whole or full exit)
    full_exit: bool = False


@dataclass(frozen=True)
class Plan:
    orders: list[PlannedOrder]  # sells first, split, before the guard
    skipped: list[PlannedOrder]  # resizes skipped because they cost too much


@dataclass(frozen=True)
class Submission:
    order: Order
    order_id: str | None  # None when vetoed, skipped or not sent
    status: str  # the `orders` status it ended with
    reasons: tuple[str, ...] = ()  # guard vetoes or why it was not sent


def client_id(run_date: date, symbol: str, side: str, seq: int) -> str:
    """The deterministic client order id: the same run plans the same ids."""
    return f"stride-{run_date:%Y%m%d}-{symbol}-{side}-{seq}"


def limits_allowed(broker: Broker, symbol: str) -> bool:
    """Whether to send limit orders to `broker` for `symbol`. False for every
    broker so far: Alpaca's fractional limit and market-on-open orders are not
    relied on, and the backtest's `SimBroker` sends market orders."""
    del broker, symbol
    return False


def plan(
    targets: Mapping[str, float],
    *,
    positions_usd: Mapping[str, float],
    rules: Mapping[str, str],
    prev_close: Mapping[str, float],
    equity: float,
    config: StrideConfig,
) -> Plan:
    """Pure. SPEC 8 order flow steps 1-3 with the backtest's own functions.

    `targets` are the dollars to hold per instrument, `positions_usd` the value
    held per instrument at the previous close, `rules` the rule per underlying."""
    planned = plan_orders(
        targets,
        positions_usd=positions_usd,
        underlying=guard.underlying_map(config),
        rules=rules,
        config=config,
    )
    kept, skipped = skip_costly_resizes(planned, prev_close, config, targets)
    return Plan(split_orders(kept, equity, config), skipped)


def approve(
    planned: Sequence[PlannedOrder], ctx: guard.GuardContext, cash: float, config: StrideConfig
) -> list[tuple[PlannedOrder, guard.GuardDecision]]:
    """Pure. Every order passes `risk/guard.py` (SPEC 8, step 5), as in the backtest."""
    return approve_batch(planned, ctx, cash, config)


def liquidation_orders(positions: Mapping[str, Position], closes: Mapping[str, float]) -> list[
    PlannedOrder
]:  # fmt: skip
    """Pure. Kill switch (SPEC 7, step 4): one sell per symbol the broker reports,
    for its full value at the last close."""
    underlying = {s: s for s in positions}
    return [
        PlannedOrder(s, "sell", p.quantity * closes[s], False, underlying[s], KILL_SWITCH_RULE)
        for s, p in sorted(positions.items())
        if p.quantity > 0
    ]


def check_liquidation(
    planned: Sequence[PlannedOrder], positions_usd: Mapping[str, float]
) -> list[tuple[PlannedOrder, guard.GuardDecision]]:
    """Pure. The guard's liquidation check for each kill-switch sell."""
    ctx = guard.GuardContext(
        equity=sum(positions_usd.values()),
        positions_usd=positions_usd,
        data_ok=True,
        kill_switch_active=True,
    )
    return [
        (p, guard.check_liquidation(guard.ProposedOrder(p.symbol, p.side, p.notional), ctx))
        for p in planned
    ]


def limit_price(side: str, close: float, config: StrideConfig) -> float:
    """Previous close +/- `limit_offset`, to the cent (the engine's formula)."""
    offset = config.execution.limit_offset
    return round(close * (1 + offset if side == "buy" else 1 - offset), 2)


def price(
    approved: Sequence[tuple[PlannedOrder, guard.GuardDecision]],
    *,
    run_date: date,
    prev_close: Mapping[str, float],
    held_quantity: Mapping[str, float],
    targets: Mapping[str, float],
    config: StrideConfig,
    limits: Callable[[str], bool],
    fractionable: Callable[[str], bool],
    resend: frozenset[str] = frozenset(),
) -> tuple[list[tuple[Order, guard.GuardDecision]], list[PlannedOrder]]:
    """Pure. Orders ready for the broker, sells first, each with its client id,
    and the orders skipped because they are below one whole share.

    - A sell of a symbol not in `targets` (a full exit, or a kill-switch sell)
      is sent as the shares held, split in proportion when the split cut it.
    - Not fractionable: whole shares floored at the previous close.
    - `resend` (A1, Q7): symbols whose exit went unfilled yesterday; their sells
      are market orders whatever `limits` says.
    - Notional below `min_order_usd` after rounding: dropped."""
    sells = [(p, d) for p, d in approved if p.side == "sell"]
    buys = [(p, d) for p, d in approved if p.side == "buy"]
    exits = _exit_quantities(sells, held_quantity, targets, fractionable)
    out: list[tuple[Order, guard.GuardDecision]] = []
    too_small: list[PlannedOrder] = []
    minimum = config.execution.min_order_usd
    for seq, (p, decision) in enumerate(sells + buys, start=1):
        cid = client_id(run_date, p.symbol, p.side, seq)
        if not decision.approved:  # recorded as planned, with the guard reasons
            out.append((Order(cid, p.symbol, p.side, p.notional, "market", None, p.switch,
                              p.rule), decision))  # fmt: skip
            continue
        close = prev_close.get(p.symbol, math.nan)
        quantity = exits.get(seq - 1)
        full_exit = quantity is not None
        notional = quantity * close if quantity is not None and close > 0 else p.notional
        if not full_exit and not fractionable(p.symbol):
            quantity = math.floor(p.notional / close + _SHARE_EPSILON) if close > 0 else 0.0
            notional = quantity * close if close > 0 else 0.0
        if quantity is not None and quantity <= 0:
            too_small.append(p)
            continue
        if notional < minimum:
            continue
        kind: OrderType = "market"
        limit = None
        resent = p.side == "sell" and p.symbol in resend
        if limits(p.symbol) and not resent and close > 0:
            kind, limit = "limit", limit_price(p.side, close, config)
        order = Order(
            cid,
            p.symbol,
            p.side,
            notional,
            kind,
            limit,
            p.switch,
            p.rule,
            quantity,
            full_exit,
        )
        out.append((order, decision))
    return out, too_small


def _exit_quantities(
    sells: Sequence[tuple[PlannedOrder, guard.GuardDecision]],
    held_quantity: Mapping[str, float],
    targets: Mapping[str, float],
    fractionable: Callable[[str], bool],
) -> dict[int, float]:
    """Shares for each approved sell (by position in `sells`) that closes its
    position: all the shares held, shared in proportion to the notional when the
    split cut the exit into parts, the last part taking what is left."""
    parts: dict[str, list[int]] = {}
    for i, (p, d) in enumerate(sells):
        if d.approved and targets.get(p.symbol, 0.0) <= 0:
            parts.setdefault(p.symbol, []).append(i)
    out: dict[int, float] = {}
    for symbol, indexes in parts.items():
        held = held_quantity.get(symbol, 0.0)
        total = sum(sells[i][0].notional for i in indexes)
        left = held
        for i in indexes[:-1]:
            share = held * sells[i][0].notional / total
            out[i] = math.floor(share + _SHARE_EPSILON) if not fractionable(symbol) else share
            left -= out[i]
        out[indexes[-1]] = left
    return out


def order_rows(
    run_date: date, priced: Sequence[tuple[Order, guard.GuardDecision]], status: str
) -> pd.DataFrame:
    """Rows of the `orders` table: approved ones with `status`, vetoed ones as
    "vetoed" with the guard reasons."""
    rows = [
        {
            "date": pd.Timestamp(run_date),
            "client_id": o.client_id,
            "broker_id": None,
            "symbol": o.symbol,
            "side": o.side,
            "notional": o.notional,
            "type": o.type,
            "limit_price": o.limit_price,
            "rule": o.rule,
            "switch": int(o.switch),
            "reasons": "+".join(d.reasons),
            "status": status if d.approved else "vetoed",
            "quantity": o.quantity,
            "full_exit": int(o.full_exit),
        }
        for o, d in priced
    ]
    columns = [*store.ORDER_COLUMNS, "quantity", "full_exit"]
    return pd.DataFrame(rows, columns=columns)


def store_plan(
    conn: sqlite3.Connection,
    run_date: date,
    priced: Sequence[tuple[Order, guard.GuardDecision]],
) -> int:
    """Store the evening's orders: approved ones as "planned" for the morning,
    vetoed ones with their reasons. Replaces an earlier plan of the same day
    that was never sent."""
    store.delete_unsent_orders(conn, run_date)
    rows = order_rows(run_date, priced, "planned")
    return store.record_orders(conn, rows) if not rows.empty else 0


def stored_orders(frame: pd.DataFrame) -> list[Order]:
    """`orders` rows back as `Order`s."""
    out = []
    for row in frame.to_dict("records"):
        quantity = row.get("quantity")
        limit = row.get("limit_price")
        out.append(
            Order(
                str(row["client_id"]),
                str(row["symbol"]),
                "buy" if row["side"] == "buy" else "sell",
                float(row["notional"]),
                "limit" if row["type"] == "limit" else "market",
                None if limit is None or pd.isna(limit) else float(limit),
                bool(row["switch"]),
                str(row["rule"]),
                None if quantity is None or pd.isna(quantity) else float(quantity),
                bool(row.get("full_exit", 0)),
            )
        )
    return out


def submit_approved(
    broker: Broker,
    planned: Sequence[Order],
    decisions: Sequence[guard.GuardDecision],
    conn: sqlite3.Connection,
    *,
    kill_switch_path: str | Path | Sequence[str | Path],
    halt_path: str | Path | Sequence[str | Path],
    run_date: date,
) -> list[Submission]:
    """Not pure. Send the approved orders, sells first, and record each one.

    `kill_switch_path` and `halt_path` may each be several files (any one counts).
    Orders not yet in `orders` are stored before the call. A kill-switch sell
    (rule `KILL_SWITCH`) goes out only while the kill switch is active, even
    during a halt; any other order only while neither file exists."""
    if len(planned) != len(decisions):
        raise ValueError("one decision per planned order")
    pairs = sorted(zip(planned, decisions, strict=True), key=lambda pd_: pd_[0].side != "sell")
    known = store.read_orders(conn)
    status_of = dict(zip(known["client_id"], known["status"], strict=True))
    sent: set[str] = set()
    out: list[Submission] = []
    for order, decision in pairs:
        cid = order.client_id
        if cid not in status_of:
            rows = order_rows(run_date, [(order, decision)], "planned")
            store.record_orders(conn, rows)
            status_of[cid] = str(rows["status"].iloc[0])
        if cid in sent:
            out.append(Submission(order, None, status_of[cid], ("DUPLICATE",)))
            continue
        if status_of[cid] in _OPEN:  # sent, or maybe sent, by an earlier run
            sent.add(cid)
            out.append(_look_up(broker, conn, order, unknown_if_lookup_fails=False))
            status_of[cid] = out[-1].status
            continue
        if not decision.approved:
            if status_of[cid] in {"planned", "vetoed"}:
                store.update_order(conn, cid, "vetoed", reasons="+".join(decision.reasons))
                status_of[cid] = "vetoed"
            out.append(Submission(order, None, "vetoed", decision.reasons))
            sent.add(cid)  # decided for this run
            continue
        if status_of[cid] != "planned":  # filled, cancelled, failed...: already final
            out.append(Submission(order, None, status_of[cid], ("ALREADY_DONE",)))
            continue
        blocked = _blocked(order, kill_switch_path, halt_path)
        if blocked:
            store.update_order(conn, cid, "skipped", reasons=blocked)
            out.append(Submission(order, None, "skipped", (blocked,)))
            status_of[cid] = "skipped"
            continue
        sent.add(cid)
        store.update_order(conn, cid, "sending")
        try:
            order_id = broker.submit_order(
                order.symbol,
                order.side,
                order.notional,
                order.type,
                order.limit_price,
                client_order_id=cid,
                quantity=order.quantity,
            )
        except Exception:
            out.append(_look_up(broker, conn, order, unknown_if_lookup_fails=True))
            status_of[cid] = out[-1].status
            continue
        store.update_order(conn, cid, "pending", broker_id=order_id)
        status_of[cid] = "pending"
        out.append(Submission(order, order_id, "pending"))
    return out


def _paths(value: str | Path | Sequence[str | Path]) -> tuple[str | Path, ...]:
    return (value,) if isinstance(value, (str, Path)) else tuple(value)


def _blocked(
    order: Order,
    kill_switch_path: str | Path | Sequence[str | Path],
    halt_path: str | Path | Sequence[str | Path],
) -> str:
    """Why `order` must not be sent now ("" if it may): files read at send time.
    Any of several files (the instance's own and a shared one) counts."""
    killed = any(is_active(p) for p in _paths(kill_switch_path))
    halted = any(is_halted(p) for p in _paths(halt_path))
    if order.rule == KILL_SWITCH_RULE:
        return "" if killed and order.side == "sell" else guard.KILL_SWITCH
    if killed:
        return guard.KILL_SWITCH
    if halted:
        return guard.HALTED
    return ""


def _look_up(
    broker: Broker, conn: sqlite3.Connection, order: Order, *, unknown_if_lookup_fails: bool
) -> Submission:
    """Ask the broker whether the order exists (after a failed send, or for an
    order an earlier run left sending, pending or unknown); never resend blindly.
    Not found: it was never placed, so it is marked failed and the next evening
    plans again. A failed lookup leaves its fate unknown."""
    try:
        found = broker.find_order(order.client_id)
    except Exception:
        if unknown_if_lookup_fails:
            store.update_order(conn, order.client_id, "unknown", reasons=SEND_UNKNOWN)
        return Submission(order, None, "unknown", (SEND_UNKNOWN,))
    if found is None:
        store.update_order(conn, order.client_id, "failed", reasons=SEND_FAILED)
        return Submission(order, None, "failed", (SEND_FAILED,))
    store.update_order(conn, order.client_id, "pending", broker_id=found.order_id)
    return Submission(order, found.order_id, "pending")


# After the open: statuses, fills and the next-day resend.


def sync_orders(
    conn: sqlite3.Connection,
    broker: Broker,
    prev_close: Mapping[str, float],
    *,
    final: bool = False,
) -> list[OrderState]:
    """Not pure. Read every open order from the broker, update its status and
    record its fill (price, quantity, commission, slippage vs the previous close).
    With `final` (after the close and `cancel_all`) a partly filled order that
    was cancelled records what filled. Returns the broker states read."""
    states = []
    for row in store.read_orders(conn, statuses=("pending", "sending", "unknown")).to_dict(
        "records"
    ):
        broker_id = row["broker_id"]
        cid = str(row["client_id"])
        state = (
            broker.get_order(str(broker_id))
            if broker_id is not None and not pd.isna(broker_id)
            else broker.find_order(cid)
        )
        if state is None:
            if final:
                store.update_order(conn, cid, "failed", reasons=SEND_FAILED)
            continue
        states.append(state)
        if state.status == "pending" and not final:
            store.update_order(conn, cid, "pending", broker_id=state.order_id)
            continue
        status = state.status if state.status != "pending" else "cancelled"
        store.update_order(conn, cid, status, broker_id=state.order_id)
        if (
            state.filled_quantity > 0
            and state.average_price
            and not store.has_fill(conn, state.order_id)
        ):
            _record_fill(conn, state, prev_close.get(state.symbol, math.nan))
    return states


def _record_fill(conn: sqlite3.Connection, state: OrderState, previous_close: float) -> None:
    assert state.average_price is not None
    slippage = state.average_price / previous_close - 1 if previous_close > 0 else math.nan
    fill = pd.DataFrame(
        [
            {
                "date": pd.Timestamp(state.filled or state.submitted),
                "broker_id": state.order_id,
                "symbol": state.symbol,
                "side": state.side,
                "quantity": state.filled_quantity,
                "price": state.average_price,
                "commission": state.commission,
                "slippage_vs_close": slippage,
            }
        ]
    )
    store.record_fills(conn, fill)


def mark_resends(conn: sqlite3.Connection, day: date) -> list[str]:
    """A1, Q7: an exit or kill-switch sell of run date `day` that did not fill
    (cancelled, rejected or expired) is marked "resend": the next evening sends
    its sell as a market order. Entries and adds are re-planned normally."""
    orders = store.read_orders(conn, statuses=("cancelled", "rejected", "failed"), day=day)
    marked = []
    for row in orders.to_dict("records"):
        exit_ = bool(row.get("full_exit")) or row["rule"] == KILL_SWITCH_RULE
        if row["side"] == "sell" and exit_ and not store.has_fill(conn, str(row["broker_id"])):
            store.update_order(conn, str(row["client_id"]), "resend")
            marked.append(str(row["symbol"]))
    return sorted(set(marked))


def resend_symbols(conn: sqlite3.Connection, before: date) -> frozenset[str]:
    """Symbols flagged "resend" by the latest run date before `before`."""
    orders = store.read_orders(conn, statuses=("resend",))
    if orders.empty:
        return frozenset()
    earlier = orders[pd.DatetimeIndex(orders["date"]).date < before]
    if earlier.empty:
        return frozenset()
    last = earlier["date"].max()
    return frozenset(earlier.loc[earlier["date"] == last, "symbol"].astype(str))


__all__ = [
    "KILL_SWITCH_RULE",
    "ORDER_TOO_SMALL",
    "Order",
    "Plan",
    "PlannedOrder",
    "Submission",
    "approve",
    "check_liquidation",
    "client_id",
    "limit_price",
    "limits_allowed",
    "liquidation_orders",
    "mark_resends",
    "order_rows",
    "plan",
    "price",
    "resend_symbols",
    "store_plan",
    "stored_orders",
    "submit_approved",
    "sync_orders",
]
