from __future__ import annotations

import math
import sqlite3
from collections.abc import Callable
from datetime import date
from pathlib import Path

import pandas as pd
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from stride.config import load_config
from stride.data import store
from stride.execution import orders
from stride.execution.broker import (
    Account,
    Broker,
    BrokerError,
    OrderState,
    OrderType,
    Position,
    Side,
)
from stride.execution.sim_broker import SimBroker
from stride.risk import guard

ROOT = Path(__file__).resolve().parents[1]
CONFIG = load_config(ROOT / "config.yaml", env={})
DAY = date(2026, 10, 23)
T1 = date(2026, 10, 26)
APPROVED = guard.GuardDecision(guard.ProposedOrder("SPY", "buy", 1.0), True)


def _vetoed(*reasons: str) -> guard.GuardDecision:
    return guard.GuardDecision(guard.ProposedOrder("SPY", "buy", 1.0), False, reasons)


class FakeBroker(Broker):
    """Records every call; `fail_submit` makes submit_order raise after (or
    before) placing the order; `fail_lookup` makes find_order raise."""

    def __init__(self, conn: sqlite3.Connection | None = None) -> None:
        self.conn = conn
        self.submitted: list[dict[str, object]] = []
        self.status_at_send: list[str] = []
        self.placed: dict[str, OrderState] = {}
        self.fail_submit: str | None = None  # None, "before" or "after" placing
        self.fail_lookup = False
        self.fractional = True

    def get_account(self) -> Account:
        return Account(1_000.0, 1_000.0, 1_000.0, 0.0)

    def get_positions(self) -> dict[str, Position]:
        return {}

    def submit_order(
        self,
        symbol: str,
        side: Side,
        notional: float,
        type: OrderType = "market",
        limit_price: float | None = None,
        *,
        client_order_id: str | None = None,
        quantity: float | None = None,
    ) -> str:
        if self.conn is not None and client_order_id is not None:
            row = self.conn.execute(
                "SELECT status FROM orders WHERE client_id = ?", (client_order_id,)
            ).fetchone()
            self.status_at_send.append(row[0] if row else "missing")
        if self.fail_submit == "before":
            raise BrokerError("alpaca submit_order failed: ConnectionError")
        order_id = f"b-{len(self.placed) + 1}"
        self.submitted.append(
            {"symbol": symbol, "side": side, "notional": notional, "type": type,
             "client_order_id": client_order_id, "quantity": quantity}
        )  # fmt: skip
        state = OrderState(order_id, symbol, side, notional, type, limit_price, DAY, "pending",
                           client_order_id=client_order_id)  # fmt: skip
        self.placed[client_order_id or order_id] = state
        if self.fail_submit == "after":
            raise BrokerError("alpaca submit_order failed: Timeout")
        return order_id

    def get_order(self, order_id: str) -> OrderState:
        return next(o for o in self.placed.values() if o.order_id == order_id)

    def find_order(self, client_order_id: str) -> OrderState | None:
        if self.fail_lookup:
            raise BrokerError("alpaca find_order failed: Timeout")
        return self.placed.get(client_order_id)

    def is_fractionable(self, symbol: str) -> bool:
        return self.fractional

    def cancel_all(self) -> int:
        return 0

    def is_market_open(self, day: date) -> bool:
        return True


def _conn() -> sqlite3.Connection:
    return store.connect(":memory:")


def _order(symbol: str, side: Side, notional: float, seq: int = 1, *, rule: str = "HOLD",
           quantity: float | None = None) -> orders.Order:  # fmt: skip
    return orders.Order(orders.client_id(DAY, symbol, side, seq), symbol, side, notional,
                        "market", None, False, rule, quantity)  # fmt: skip


def _submit(
    broker: Broker,
    planned: list[orders.Order],
    decisions: list[guard.GuardDecision],
    conn: sqlite3.Connection,
    tmp_path: Path,
) -> list[orders.Submission]:
    return orders.submit_approved(
        broker, planned, decisions, conn,
        kill_switch_path=tmp_path / "KILL_SWITCH", halt_path=tmp_path / "HALT", run_date=DAY,
    )  # fmt: skip


# Planning (pure).


def test_client_id_is_deterministic() -> None:
    assert orders.client_id(DAY, "SPY", "buy", 3) == "stride-20261023-SPY-buy-3"


def test_full_exit_and_switch_legs_pass_the_band() -> None:
    plan = orders.plan(
        {"SSO": 1_000.0},
        positions_usd={"SPY": 1_000.0, "GLD": 5.0},
        rules={"SPY": "HOLD", "GLD": "EXIT_LL20"},
        prev_close={"SPY": 500.0, "SSO": 100.0, "GLD": 250.0},
        equity=5_000.0,
        config=CONFIG,
    )
    by_symbol = {(o.symbol, o.side): o for o in plan.orders}
    assert by_symbol["GLD", "sell"].notional == 5.0  # below the band, still sold (Q1)
    assert by_symbol["SPY", "sell"].switch and by_symbol["SSO", "buy"].switch
    assert [o.side for o in plan.orders] == ["sell", "sell", "buy"]


def test_inside_the_band_no_order() -> None:
    plan = orders.plan({"SPY": 1_000.0}, positions_usd={"SPY": 900.0}, rules={},
                       prev_close={"SPY": 500.0}, equity=5_000.0, config=CONFIG)  # fmt: skip
    assert plan.orders == []


def test_below_the_minimum_no_order() -> None:
    plan = orders.plan({}, positions_usd={"SPY": 0.5}, rules={}, prev_close={"SPY": 500.0},
                       equity=5_000.0, config=CONFIG)  # fmt: skip
    assert plan.orders == []


def test_costly_hold_resize_is_skipped() -> None:
    plan = orders.plan({"SPY": 10.0}, positions_usd={"SPY": 5.0}, rules={"SPY": "HOLD"},
                       prev_close={"SPY": 500.0}, equity=50.0, config=CONFIG)  # fmt: skip
    assert plan.orders == [] and len(plan.skipped) == 1


def test_large_orders_are_split() -> None:
    plan = orders.plan({"SPY": 900.0}, positions_usd={}, rules={"SPY": "ENTRY_HH55"},
                       prev_close={"SPY": 500.0}, equity=1_000.0, config=CONFIG)  # fmt: skip
    assert [o.notional for o in plan.orders] == [450.0, 450.0]


def test_approve_uses_the_guard_and_fits_buys() -> None:
    planned = orders.plan(
        {"SPY": 500.0}, positions_usd={}, rules={"SPY": "ENTRY_HH55"},
        prev_close={"SPY": 500.0}, equity=1_000.0, config=CONFIG,
    ).orders  # fmt: skip
    ctx = guard.GuardContext(1_000.0, {}, data_ok=True, kill_switch_active=False)
    [(order, decision)] = orders.approve(planned, ctx, 100.0, CONFIG)
    assert decision.approved
    assert order.notional < 100.0  # scaled to the cash
    blocked = guard.GuardContext(1_000.0, {}, data_ok=True, kill_switch_active=False,
                                 blocked=frozenset({"SPY"}))  # fmt: skip
    [(_, decision)] = orders.approve(planned, blocked, 1_000.0, CONFIG)
    assert decision.reasons == (guard.SYMBOL_BLOCKED,)


# Pricing (pure).


def _price(
    approved: list[tuple[orders.PlannedOrder, guard.GuardDecision]],
    held: dict[str, float] | None = None,
    targets: dict[str, float] | None = None,
    *,
    limits: Callable[[str], bool] = lambda _: False,
    fractionable: Callable[[str], bool] = lambda _: True,
    resend: frozenset[str] = frozenset(),
) -> tuple[list[tuple[orders.Order, guard.GuardDecision]], list[orders.PlannedOrder]]:
    return orders.price(
        approved, run_date=DAY, prev_close={"SPY": 500.0, "UUP": 28.0, "GLD": 250.0},
        held_quantity=held or {}, targets=targets or {}, config=CONFIG, limits=limits,
        fractionable=fractionable, resend=resend,
    )  # fmt: skip


def _planned(symbol: str, side: Side, notional: float, rule: str = "HOLD") -> orders.PlannedOrder:
    return orders.PlannedOrder(symbol, side, notional, False, symbol, rule)


def test_market_day_orders_by_default() -> None:
    [(order, _)], _ = _price([(_planned("SPY", "buy", 100.0), APPROVED)], targets={"SPY": 100.0})
    assert (order.type, order.limit_price, order.quantity) == ("market", None, None)
    assert order.client_id == "stride-20261023-SPY-buy-1"


def test_limits_at_previous_close_when_allowed_but_not_for_a_resend() -> None:
    buy = (_planned("SPY", "buy", 100.0), APPROVED)
    sell = (_planned("GLD", "sell", 100.0), APPROVED)
    priced, _ = _price([buy, sell], {"GLD": 1.0}, {"SPY": 100.0, "GLD": 150.0},
                       limits=lambda _: True)  # fmt: skip
    assert [(o.symbol, o.type, o.limit_price) for o, _ in priced] == [
        ("GLD", "limit", 248.75), ("SPY", "limit", 502.5)]  # fmt: skip
    priced, _ = _price([sell], {"GLD": 1.0}, {"GLD": 150.0}, limits=lambda _: True,
                       resend=frozenset({"GLD"}))  # fmt: skip
    assert priced[0][0].type == "market"


def test_not_fractionable_is_floored_to_whole_shares() -> None:
    priced, small = _price(
        [(_planned("UUP", "buy", 100.0), APPROVED), (_planned("SPY", "buy", 300.0), APPROVED)],
        targets={"UUP": 100.0, "SPY": 300.0}, fractionable=lambda _: False,
    )  # fmt: skip
    [(order, _)] = priced
    assert (order.symbol, order.quantity, order.notional) == ("UUP", 3, 84.0)
    assert [p.symbol for p in small] == ["SPY"]  # 300 / 500 < 1 share: ORDER_TOO_SMALL


def test_full_exit_sends_the_shares_held() -> None:
    parts = [(_planned("SPY", "sell", 300.0), APPROVED), (_planned("SPY", "sell", 300.0), APPROVED)]
    priced, _ = _price(parts, {"SPY": 1.25})
    assert sum(o.quantity or 0 for o, _ in priced) == pytest.approx(1.25)
    assert all(o.full_exit for o, _ in priced)
    assert [o.client_id for o, _ in priced] == [
        "stride-20261023-SPY-sell-1", "stride-20261023-SPY-sell-2"]  # fmt: skip


def test_vetoed_orders_are_kept_with_their_reasons() -> None:
    [(order, decision)], small = _price([(_planned("SPY", "sell", 50.0), _vetoed("SHORT"))])
    assert (order.quantity, order.notional, decision.reasons, small) == (None, 50.0, ("SHORT",), [])


def test_liquidation_sells_every_broker_position() -> None:
    positions = {"SPY": Position("SPY", 1.5, 400.0), "GLD": Position("GLD", 2.0, 200.0)}
    planned = orders.liquidation_orders(positions, {"SPY": 500.0, "GLD": 250.0})
    checked = orders.check_liquidation(planned, {"SPY": 750.0, "GLD": 500.0})
    priced, _ = _price(checked, {"SPY": 1.5, "GLD": 2.0})
    assert {o.symbol: o.quantity for o, _ in priced} == {"SPY": 1.5, "GLD": 2.0}
    assert all(o.rule == orders.KILL_SWITCH_RULE and d.approved for o, d in priced)


# Storage.


def test_store_plan_and_read_back() -> None:
    conn = _conn()
    priced = [(_order("SPY", "sell", 50.0, 1, quantity=0.1), APPROVED),
              (_order("GLD", "buy", 40.0, 2), _vetoed("SHORT"))]  # fmt: skip
    assert orders.store_plan(conn, DAY, priced) == 2
    rows = store.read_orders(conn, day=DAY)
    assert list(rows["status"]) == ["planned", "vetoed"]
    assert list(rows["reasons"]) == ["", "SHORT"]
    back = orders.stored_orders(rows)
    assert back[0] == priced[0][0]
    assert orders.store_plan(conn, DAY, priced[:1]) == 1  # a rerun replaces the unsent plan
    assert len(store.read_orders(conn, day=DAY)) == 1


# Submission.


def test_sells_first_and_stored_before_sending(tmp_path: Path) -> None:
    conn = _conn()
    broker = FakeBroker(conn)
    planned = [_order("SPY", "buy", 100.0, 2), _order("GLD", "sell", 50.0, 1)]
    out = _submit(broker, planned, [APPROVED, APPROVED], conn, tmp_path)
    assert [s["side"] for s in broker.submitted] == ["sell", "buy"]
    assert broker.status_at_send == ["sending", "sending"]
    assert [s.status for s in out] == ["pending", "pending"]
    assert list(store.read_orders(conn)["broker_id"]) == ["b-1", "b-2"]


def test_never_sends_without_approval(tmp_path: Path) -> None:
    conn = _conn()
    broker = FakeBroker(conn)
    out = _submit(broker, [_order("SPY", "buy", 100.0)], [_vetoed("TOO_MANY_ORDERS")], conn,
                  tmp_path)  # fmt: skip
    assert broker.submitted == []
    assert (out[0].status, out[0].reasons) == ("vetoed", ("TOO_MANY_ORDERS",))
    assert store.read_orders(conn)["status"].tolist() == ["vetoed"]


def test_kill_switch_and_halt_files_are_read_before_each_send(tmp_path: Path) -> None:
    conn = _conn()
    broker = FakeBroker(conn)
    (tmp_path / "HALT").write_text("halted", encoding="utf-8")
    liquidation = _order("GLD", "sell", 50.0, 1, rule=orders.KILL_SWITCH_RULE)
    out = _submit(broker, [_order("SPY", "buy", 100.0, 2), liquidation],
                  [APPROVED, APPROVED], conn, tmp_path)  # fmt: skip
    assert broker.submitted == []  # halt: nothing, and a liquidation needs the kill switch
    assert [s.reasons for s in out] == [(guard.KILL_SWITCH,), (guard.HALTED,)]
    (tmp_path / "KILL_SWITCH").write_text("fired", encoding="utf-8")
    conn = _conn()
    out = _submit(broker, [_order("SPY", "buy", 100.0, 2), liquidation],
                  [APPROVED, APPROVED], conn, tmp_path)  # fmt: skip
    assert [s["symbol"] for s in broker.submitted] == ["GLD"]  # only the liquidation
    assert [s.status for s in out] == ["pending", "skipped"]


def test_error_then_found_is_pending(tmp_path: Path) -> None:
    conn = _conn()
    broker = FakeBroker(conn)
    broker.fail_submit = "after"  # placed, but the reply was lost
    [sub] = _submit(broker, [_order("SPY", "buy", 100.0)], [APPROVED], conn, tmp_path)
    assert (sub.status, sub.order_id, len(broker.submitted)) == ("pending", "b-1", 1)


def test_error_then_not_found_is_failed_and_not_resent(tmp_path: Path) -> None:
    conn = _conn()
    broker = FakeBroker(conn)
    broker.fail_submit = "before"
    [sub] = _submit(broker, [_order("SPY", "buy", 100.0)], [APPROVED], conn, tmp_path)
    assert (sub.status, sub.reasons, broker.submitted) == ("failed", (orders.SEND_FAILED,), [])
    broker.fail_submit = None
    [again] = _submit(broker, [_order("SPY", "buy", 100.0)], [APPROVED], conn, tmp_path)
    assert (again.status, broker.submitted) == ("failed", [])  # final: re-planned tonight


def test_error_and_lookup_error_is_unknown_then_resolved(tmp_path: Path) -> None:
    conn = _conn()
    broker = FakeBroker(conn)
    broker.fail_submit, broker.fail_lookup = "after", True
    [sub] = _submit(broker, [_order("SPY", "buy", 100.0)], [APPROVED], conn, tmp_path)
    assert sub.status == "unknown"
    broker.fail_submit, broker.fail_lookup = None, False
    [again] = _submit(broker, [_order("SPY", "buy", 100.0)], [APPROVED], conn, tmp_path)
    assert (again.status, again.order_id, len(broker.submitted)) == ("pending", "b-1", 1)


def test_a_second_run_never_resends(tmp_path: Path) -> None:
    conn = _conn()
    broker = FakeBroker(conn)
    planned = [_order("SPY", "buy", 100.0)]
    _submit(broker, planned, [APPROVED], conn, tmp_path)
    _submit(broker, planned, [APPROVED], conn, tmp_path)
    assert len(broker.submitted) == 1


def test_duplicate_client_id_in_one_run_is_sent_once(tmp_path: Path) -> None:
    conn = _conn()
    broker = FakeBroker(conn)
    order = _order("SPY", "buy", 100.0)
    out = _submit(broker, [order, order], [APPROVED, APPROVED], conn, tmp_path)
    assert len(broker.submitted) == 1
    assert out[1].reasons == ("DUPLICATE",)


def test_one_decision_per_order(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="one decision"):
        _submit(FakeBroker(), [_order("SPY", "buy", 1.0)], [], _conn(), tmp_path)


# Fills and the next-day resend, with SimBroker.


def _bars(rows: list[tuple[float, float, float, float]]) -> pd.DataFrame:
    index = pd.to_datetime([DAY, T1][: len(rows)])
    return pd.DataFrame(rows, index=index, columns=["open", "high", "low", "close"])


def test_sync_records_fills_with_slippage(tmp_path: Path) -> None:
    conn = _conn()
    sim = SimBroker({"SPY": _bars([(99, 101, 98, 100), (102, 104, 100, 103)])}, CONFIG,
                    cash=1_000.0, start=DAY)  # fmt: skip
    _submit(sim, [_order("SPY", "buy", 100.0)], [APPROVED], conn, tmp_path)
    assert orders.sync_orders(conn, sim, {"SPY": 100.0})[0].status == "pending"
    sim.step(T1)
    orders.sync_orders(conn, sim, {"SPY": 100.0}, final=True)
    [fill] = store.read_table(conn, "fills").to_dict("records")
    assert fill["price"] == pytest.approx(102 * 1.0005)
    assert fill["slippage_vs_close"] == pytest.approx(102 * 1.0005 / 100 - 1)
    assert store.read_orders(conn)["status"].tolist() == ["filled"]
    orders.sync_orders(conn, sim, {"SPY": 100.0}, final=True)
    assert len(store.read_table(conn, "fills")) == 1  # never twice


def test_unfilled_exit_is_marked_for_a_market_resend(tmp_path: Path) -> None:
    conn = _conn()
    exit_ = orders.Order(orders.client_id(DAY, "GLD", "sell", 1), "GLD", "sell", 50.0,
                         "limit", 249.0, False, "EXIT_LL20", 0.2, True)  # fmt: skip
    entry = _order("SPY", "buy", 50.0, 2, rule="ENTRY_HH55")
    orders.store_plan(conn, DAY, [(exit_, APPROVED), (entry, APPROVED)])
    for o in (exit_, entry):
        store.update_order(conn, o.client_id, "cancelled", broker_id=f"x-{o.symbol}")
    assert orders.mark_resends(conn, DAY) == ["GLD"]  # the entry is re-planned normally
    assert orders.resend_symbols(conn, T1) == frozenset({"GLD"})
    assert orders.resend_symbols(conn, DAY) == frozenset()


def test_limits_allowed_is_false() -> None:
    assert not orders.limits_allowed(FakeBroker(), "SPY")


# Invariants (hypothesis).

ASSETS = ["SPY", "TLT", "GLD", "IBIT", "DBC", "UUP"]
VERSIONS = {"SPY": ["SPY", "SSO", "UPRO"], "TLT": ["TLT", "TMF"], "GLD": ["GLD", "UGL"]}
money = st.floats(min_value=0, max_value=5_000, allow_nan=False)


@st.composite
def books(draw: st.DrawFn) -> tuple[dict[str, float], dict[str, float]]:
    """Targets with at most one instrument per asset, and positions."""
    targets: dict[str, float] = {}
    positions: dict[str, float] = {}
    for asset in ASSETS:
        versions = VERSIONS.get(asset, [asset])
        if draw(st.booleans()):
            targets[draw(st.sampled_from(versions))] = draw(money)
        for symbol in versions:
            if draw(st.booleans()):
                positions[symbol] = draw(money)
    return targets, positions


def _plan(targets: dict[str, float], positions: dict[str, float]) -> orders.Plan:
    # A signal rule, so the cost-aware resizing (HOLD only) leaves the band as is.
    return orders.plan(targets, positions_usd=positions, rules=dict.fromkeys(ASSETS, "PYRAMID"),
                       prev_close={}, equity=1e9, config=CONFIG)  # fmt: skip


@settings(max_examples=500, deadline=None)
@given(book=books())
def test_invariants_of_the_plan(book: tuple[dict[str, float], dict[str, float]]) -> None:
    targets, positions = book
    planned = _plan(targets, positions).orders
    sides = [o.side for o in planned]
    assert sides == sorted(sides, key=lambda s: s != "sell")  # every sell before every buy
    final = dict(positions)
    sold: dict[str, float] = {}
    for o in planned:
        assert o.notional > 0 and math.isfinite(o.notional)
        sold[o.symbol] = sold.get(o.symbol, 0.0) + o.notional * (o.side == "sell")
        final[o.symbol] = final.get(o.symbol, 0.0) + o.notional * (1 if o.side == "buy" else -1)
    for symbol, amount in sold.items():  # never a short
        assert amount <= positions.get(symbol, 0.0) + 1e-9
    band, minimum = CONFIG.execution.rebalance_band, CONFIG.execution.min_order_usd
    for symbol, value in final.items():
        target = targets.get(symbol, 0.0)
        if target > 0:
            assert abs(value - target) <= max(band * target, minimum) + 1e-6
        else:
            assert value < minimum + 1e-9  # sold in full, or too small to sell


@settings(max_examples=300, deadline=None)
@given(book=books())
def test_replanning_at_the_targets_is_empty(
    book: tuple[dict[str, float], dict[str, float]],
) -> None:
    targets, _ = book
    assert _plan(targets, dict(targets)).orders == []


@settings(max_examples=300, deadline=None)
@given(held=st.dictionaries(st.sampled_from(ASSETS),
                            st.floats(min_value=0.03, max_value=1e4), min_size=1))  # fmt: skip
def test_liquidation_sells_exactly_the_broker_positions(held: dict[str, float]) -> None:
    """Every position worth at least the $1 minimum at the close (37.5 x 0.03)."""
    positions = {s: Position(s, q, 1.0) for s, q in held.items()}
    closes = dict.fromkeys(held, 37.5)
    planned = orders.liquidation_orders(positions, closes)
    checked = orders.check_liquidation(planned, {s: q * 37.5 for s, q in held.items()})
    priced, _ = orders.price(
        checked, run_date=DAY, prev_close=closes, held_quantity=held, targets={},
        config=CONFIG, limits=lambda _: False, fractionable=lambda _: True,
    )  # fmt: skip
    assert {o.symbol: o.quantity for o, _ in priced} == held
    assert all(o.side == "sell" for o, _ in priced)


order_specs = st.lists(
    st.tuples(
        st.sampled_from(["SPY", "GLD", "TLT"]),
        st.sampled_from(["buy", "sell"]),
        st.integers(min_value=1, max_value=4),  # seq: repeats make duplicate client ids
        st.booleans(),  # approved
        st.booleans(),  # kill-switch liquidation
    ),
    max_size=12,
)


@settings(
    max_examples=300, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(specs=order_specs, kill=st.booleans(), halt=st.booleans())
def test_invariants_of_submission(
    tmp_path: Path, specs: list[tuple[str, Side, int, bool, bool]], kill: bool, halt: bool
) -> None:
    folder = tmp_path / f"k{int(kill)}h{int(halt)}"
    folder.mkdir(exist_ok=True)
    for name, on in (("KILL_SWITCH", kill), ("HALT", halt)):
        path = folder / name
        if on:
            path.write_text("x", encoding="utf-8")
        elif path.exists():
            path.unlink()
    conn = _conn()
    broker = FakeBroker(conn)
    planned, decisions = [], []
    for symbol, side, seq, ok, liquidation in specs:
        rule = orders.KILL_SWITCH_RULE if liquidation else "HOLD"
        planned.append(_order(symbol, side, 10.0, seq, rule=rule))
        decisions.append(APPROVED if ok else _vetoed("SHORT"))
    orders.submit_approved(
        broker, planned, decisions, conn,
        kill_switch_path=folder / "KILL_SWITCH", halt_path=folder / "HALT", run_date=DAY,
    )  # fmt: skip
    sent_ids = [str(s["client_order_id"]) for s in broker.submitted]
    assert len(sent_ids) == len(set(sent_ids))  # never the same client id twice
    sides = [s["side"] for s in broker.submitted]
    assert sides == sorted(sides, key=lambda s: s != "sell")
    approved_ids = {o.client_id for o, d in zip(planned, decisions, strict=True) if d.approved}
    assert set(sent_ids) <= approved_ids  # never without an approved decision
    rules: dict[str, str] = {}  # the first of each client id, in sending order
    for o in sorted(planned, key=lambda o: o.side != "sell"):
        rules.setdefault(o.client_id, o.rule)
    if kill or halt:  # only kill-switch liquidations, and only with the kill switch
        assert all(rules[c] == orders.KILL_SWITCH_RULE and kill for c in sent_ids)
    else:
        assert all(rules[c] != orders.KILL_SWITCH_RULE for c in sent_ids)
