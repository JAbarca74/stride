from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from stride.config import load_config
from stride.risk.guard import (
    DATA_FAILED,
    INVALID_ORDER,
    KILL_SWITCH,
    MARGIN_BUFFER,
    ORDER_TOO_LARGE,
    SAME_DAY_REVERSAL,
    SHORT,
    SYMBOL_BLOCKED,
    TOO_MANY_ORDERS,
    UNKNOWN_SYMBOL,
    GuardContext,
    ProposedOrder,
    approved,
    check_liquidation,
    check_order,
    check_orders,
    underlying_map,
)

ROOT = Path(__file__).resolve().parents[1]
CONFIG = load_config(ROOT / "config.yaml", env={})  # 60% per order, 12 per day, 25% buffer
CTX = GuardContext(
    equity=1_000.0,
    positions_usd={"SPY": 300.0, "SSO": 200.0},
    data_ok=True,
    kill_switch_active=False,
)


def _buy(symbol: str = "GLD", notional: float = 100.0) -> ProposedOrder:
    return ProposedOrder(symbol, "buy", notional)


def _sell(symbol: str = "SPY", notional: float = 100.0) -> ProposedOrder:
    return ProposedOrder(symbol, "sell", notional)


def _reasons(order: ProposedOrder, ctx: GuardContext = CTX) -> tuple[str, ...]:
    return check_order(order, ctx, CONFIG).reasons


def test_clean_orders_are_approved() -> None:
    assert check_order(_buy(), CTX, CONFIG).approved
    assert check_order(_sell(), CTX, CONFIG).approved


def test_underlying_map_covers_leveraged_versions() -> None:
    mapping = underlying_map(CONFIG)
    assert mapping["SSO"] == "SPY" and mapping["TMF"] == "TLT" and mapping["ETHU"] == "ETHA"
    assert mapping["DBC"] == "DBC"


# One test per veto.


def test_veto_short() -> None:
    assert _reasons(_sell("SPY", 300.5)) == (SHORT,)
    assert _reasons(_sell("GLD", 10.0)) == (SHORT,)  # nothing held
    assert _reasons(_sell("SPY", 300.009)) == ()  # rounding tolerance of 1 cent


def test_veto_order_above_60_percent_of_equity() -> None:
    assert _reasons(_buy(notional=600.01)) == (ORDER_TOO_LARGE,)
    assert _reasons(_buy(notional=600.0)) == ()


def test_veto_13th_order_of_the_day() -> None:
    eleven = tuple(_buy(s, 10.0) for s in ["GLD"] * 11)
    assert _reasons(_buy("GLD"), replace(CTX, approved_today=eleven)) == ()  # the 12th
    twelve = (*eleven, _buy("GLD", 10.0))
    assert _reasons(_buy("GLD"), replace(CTX, approved_today=twelve)) == (TOO_MANY_ORDERS,)


def test_veto_same_day_reversal_across_instruments() -> None:
    sold_spy = replace(CTX, approved_today=(_sell("SPY"),))
    assert _reasons(_buy("SPY"), sold_spy) == (SAME_DAY_REVERSAL,)
    assert _reasons(_buy("UPRO"), sold_spy) == (SAME_DAY_REVERSAL,)  # same underlying
    assert _reasons(_sell("SSO"), sold_spy) == ()  # same side is fine
    assert _reasons(_buy("GLD"), sold_spy) == ()


def test_veto_margin_buffer() -> None:
    margin = replace(CTX, excess_liquidity=500.0, margin_requirement=1_000.0)  # need 250 left
    assert _reasons(_buy(notional=250.0), margin) == ()
    assert _reasons(_buy(notional=250.01), margin) == (MARGIN_BUFFER,)
    assert _reasons(_sell(notional=100.0), margin) == ()  # sells free margin
    assert _reasons(_buy(notional=500.0)) == ()  # cash account: no margin requirement


def test_veto_failed_data() -> None:
    assert _reasons(_buy(), replace(CTX, data_ok=False)) == (DATA_FAILED,)


def test_veto_blocked_symbol_by_underlying() -> None:
    blocked = replace(CTX, blocked=frozenset({"SPY"}))
    assert _reasons(_sell("SSO"), blocked) == (SYMBOL_BLOCKED,)
    assert _reasons(_buy("GLD"), blocked) == ()


def test_veto_kill_switch() -> None:
    assert _reasons(_buy(), replace(CTX, kill_switch_active=True)) == (KILL_SWITCH,)


def test_veto_unknown_symbol_and_invalid_amount() -> None:
    assert _reasons(_buy("QQQ")) == (UNKNOWN_SYMBOL,)
    assert INVALID_ORDER in _reasons(_buy(notional=0.0))
    assert INVALID_ORDER in _reasons(_buy(notional=float("nan")))


def test_every_applicable_veto_is_listed() -> None:
    ctx = replace(CTX, data_ok=False, kill_switch_active=True)
    assert set(_reasons(_sell("GLD", 700.0), ctx)) == {
        KILL_SWITCH,
        DATA_FAILED,
        SHORT,
        ORDER_TOO_LARGE,
    }


def test_zero_equity_vetoes_every_order() -> None:
    assert ORDER_TOO_LARGE in _reasons(_buy(notional=1.0), replace(CTX, equity=0.0))


# Batches.


def test_batch_checks_sells_first_and_carries_approvals() -> None:
    orders = [_buy("SPY", 50.0), _sell("SPY", 50.0), _buy("GLD", 50.0)]
    decisions = check_orders(orders, CTX, CONFIG)
    assert [(d.order.symbol, d.order.side, d.approved) for d in decisions] == [
        ("SPY", "sell", True),
        ("SPY", "buy", False),  # reversal: SPY was sold today
        ("GLD", "buy", True),
    ]
    assert decisions[1].reasons == (SAME_DAY_REVERSAL,)


def test_batch_stops_at_12_orders() -> None:
    decisions = check_orders([_buy("GLD", 10.0)] * 15, CTX, CONFIG)
    assert len(approved(decisions)) == 12
    assert all(d.reasons == (TOO_MANY_ORDERS,) for d in decisions[12:])


def test_batch_cannot_sell_the_same_holding_twice() -> None:
    decisions = check_orders([_sell("SPY", 200.0), _sell("SPY", 200.0)], CTX, CONFIG)
    assert [d.approved for d in decisions] == [True, False]
    assert decisions[1].reasons == (SHORT,)


def test_batch_uses_up_margin_buffer() -> None:
    margin = replace(CTX, excess_liquidity=500.0, margin_requirement=1_000.0)
    decisions = check_orders([_buy("GLD", 200.0), _buy("SLV", 200.0)], margin, CONFIG)
    assert [d.approved for d in decisions] == [True, False]


# Kill-switch liquidation.


def test_liquidation_skips_limits_but_stays_long_only() -> None:
    ctx = replace(CTX, kill_switch_active=True, data_ok=False, equity=100.0)
    assert check_liquidation(_sell("SPY", 300.0), ctx).approved  # > 60% of equity: allowed
    assert check_liquidation(_sell("SPY", 301.0), ctx).reasons == (SHORT,)
    assert check_liquidation(_buy("SPY", 10.0), ctx).reasons == (KILL_SWITCH,)
    assert INVALID_ORDER in check_liquidation(_sell("SPY", -1.0), ctx).reasons


# Invariants (hypothesis).

symbols = st.sampled_from(["SPY", "SSO", "UPRO", "TLT", "TMF", "GLD", "IBIT", "DBC"])
orders = st.builds(
    ProposedOrder,
    symbol=symbols,
    side=st.sampled_from(["buy", "sell"]),
    notional=st.floats(min_value=0.01, max_value=2_000),
)


@settings(max_examples=1_000, deadline=None)
@given(
    batch=st.lists(orders, max_size=25),
    held=st.dictionaries(symbols, st.floats(min_value=0, max_value=2_000), max_size=8),
    equity=st.floats(min_value=0, max_value=5_000),
    data_ok=st.booleans(),
    kill=st.booleans(),
    blocked=st.frozensets(st.sampled_from(["SPY", "TLT", "GLD"])),
)
def test_approved_orders_always_respect_every_rule(  # noqa: PLR0917
    batch: list[ProposedOrder],
    held: dict[str, float],
    equity: float,
    data_ok: bool,
    kill: bool,
    blocked: frozenset[str],
) -> None:
    ctx = GuardContext(equity, held, data_ok, kill, blocked)
    ok = approved(check_orders(batch, ctx, CONFIG))
    mapping = underlying_map(CONFIG)
    assert len(ok) <= CONFIG.execution.max_orders_per_day
    if kill or not data_ok:
        assert ok == []
    remaining = dict(held)
    sides: dict[str, set[str]] = {}
    for order in ok:
        assert order.notional <= CONFIG.execution.max_order_pct_equity * equity
        assert mapping[order.symbol] not in blocked
        if order.side == "sell":  # never short
            assert order.notional <= remaining.get(order.symbol, 0.0) + 0.01
            remaining[order.symbol] = remaining.get(order.symbol, 0.0) - order.notional
        else:
            remaining[order.symbol] = remaining.get(order.symbol, 0.0) + order.notional
        sides.setdefault(mapping[order.symbol], set()).add(order.side)
    assert all(len(s) == 1 for s in sides.values())  # no same-day reversal


@settings(max_examples=300, deadline=None)
@given(order=orders, held=st.floats(min_value=0, max_value=2_000))
def test_liquidation_never_shorts(order: ProposedOrder, held: float) -> None:
    ctx = GuardContext(1_000.0, {order.symbol: held}, True, True)
    if check_liquidation(order, ctx).approved:
        assert order.side == "sell"
        assert order.notional <= held + 0.01


def test_order_reason_constants_are_unique() -> None:
    names = [
        SHORT,
        ORDER_TOO_LARGE,
        TOO_MANY_ORDERS,
        SAME_DAY_REVERSAL,
        MARGIN_BUFFER,
        DATA_FAILED,
        SYMBOL_BLOCKED,
        KILL_SWITCH,
        UNKNOWN_SYMBOL,
        INVALID_ORDER,
    ]
    assert len(set(names)) == len(names)


@pytest.mark.parametrize("side", ["buy", "sell"])
def test_decision_keeps_the_order(side: str) -> None:
    order = ProposedOrder("SPY", side, 50.0)  # type: ignore[arg-type]
    assert check_order(order, CTX, CONFIG).order is order
