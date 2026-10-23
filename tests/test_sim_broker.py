from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from stride.backtest.costs import commission
from stride.config import load_config
from stride.execution.broker import Broker
from stride.execution.sim_broker import SimBroker

ROOT = Path(__file__).resolve().parents[1]
CONFIG = load_config(ROOT / "config.yaml", env={})
T0, T1, T2 = date(2026, 10, 14), date(2026, 10, 15), date(2026, 10, 16)


def _bars(rows: list[tuple[float, float, float, float]]) -> pd.DataFrame:
    index = pd.to_datetime([T0, T1, T2][: len(rows)])
    return pd.DataFrame(rows, index=index, columns=["open", "high", "low", "close"])


PRICES = {
    "SPY": _bars([(99, 101, 98, 100), (102, 104, 100, 103), (103, 105, 101, 104)]),
    "IBIT": _bars([(40, 41, 39, 40), (42, 43, 41, 42), (42, 43, 41, 42)]),
}


def _broker(cash: float = 10_000.0) -> SimBroker:
    return SimBroker(PRICES, CONFIG, cash=cash, start=T0)


def test_sim_broker_implements_the_interface() -> None:
    assert isinstance(_broker(), Broker)


def test_order_sent_on_t_fills_at_next_open_with_spread() -> None:
    broker = _broker()
    order_id = broker.submit_order("SPY", "buy", 1_000.0)
    assert broker.get_order(order_id).status == "pending"
    broker.step(T1)
    order = broker.get_order(order_id)
    assert order.status == "filled"
    assert order.filled == T1
    assert order.average_price == pytest.approx(102 * 1.0005)  # open of t+1 + 5 bps
    assert order.filled_quantity == pytest.approx(1_000 / (102 * 1.0005))
    assert order.commission == pytest.approx(
        commission(order.filled_quantity, order.average_price or 0, CONFIG)
    )
    assert broker.get_account().cash == pytest.approx(10_000 - 1_000 - order.commission)


def test_crypto_fills_with_crypto_spread() -> None:
    broker = _broker()
    order_id = broker.submit_order("IBIT", "buy", 500.0)
    broker.step(T1)
    assert broker.get_order(order_id).average_price == pytest.approx(42 * 1.001)


def test_order_never_fills_on_the_day_it_is_sent() -> None:
    broker = _broker()
    broker.step(T1)
    order_id = broker.submit_order("SPY", "buy", 1_000.0)  # sent on T1
    assert broker.get_order(order_id).status == "pending"
    broker.step(T2)
    assert broker.get_order(order_id).filled == T2


def test_sell_closes_the_position_and_sells_before_buys() -> None:
    broker = _broker(cash=1_100.0)
    broker.submit_order("SPY", "buy", 1_000.0)
    broker.step(T1)
    held = broker.get_positions()["SPY"].quantity
    # Not enough cash for the IBIT buy unless the SPY sale settles first.
    buy_id = broker.submit_order("IBIT", "buy", 900.0)
    sell_id = broker.submit_order("SPY", "sell", 5_000.0)  # more than held: capped
    broker.step(T2)
    sell = broker.get_order(sell_id)
    assert sell.filled_quantity == pytest.approx(held)
    assert sell.average_price == pytest.approx(103 * (1 - 0.0005))
    assert "SPY" not in broker.get_positions()
    assert broker.get_order(buy_id).status == "filled"


def test_long_only_and_cash_only() -> None:
    broker = _broker(cash=100.0)
    sell_id = broker.submit_order("SPY", "sell", 50.0)
    buy_id = broker.submit_order("SPY", "buy", 500.0)
    broker.step(T1)
    assert broker.get_order(sell_id).status == "rejected"
    buy = broker.get_order(buy_id)
    assert buy.status == "filled" and buy.reason == "partial fill: cash available"
    cost = buy.filled_quantity * (buy.average_price or 0) + buy.commission
    assert cost == pytest.approx(100.0 / 1.01 + buy.commission)
    assert broker.get_account().cash >= 0
    broke = _broker(cash=0.0)
    empty = broke.submit_order("SPY", "buy", 50.0)
    broke.step(T1)
    assert broke.get_order(empty).status == "rejected"
    assert broke.get_order(empty).reason == "not enough cash"


def test_buy_limit_fills_at_open_or_limit_or_expires() -> None:
    broker = _broker()
    at_open = broker.submit_order("SPY", "buy", 100.0, "limit", 103.0)  # open 102 <= 103
    intraday = broker.submit_order("SPY", "buy", 100.0, "limit", 101.0)  # low 100 reaches it
    missed = broker.submit_order("SPY", "buy", 100.0, "limit", 99.0)  # never reached
    broker.step(T1)
    assert broker.get_order(at_open).average_price == pytest.approx(102 * 1.0005)
    assert broker.get_order(intraday).average_price == pytest.approx(101 * 1.0005)
    assert broker.get_order(missed).status == "cancelled"  # day order, cancelled at close


def test_sell_limit() -> None:
    broker = _broker()
    broker.submit_order("SPY", "buy", 1_000.0)
    broker.step(T1)
    sell_id = broker.submit_order("SPY", "sell", 100.0, "limit", 104.5)  # high 105 reaches it
    broker.step(T2)
    assert broker.get_order(sell_id).average_price == pytest.approx(104.5 * (1 - 0.0005))


def test_account_values_positions_at_the_close() -> None:
    broker = _broker()
    broker.submit_order("SPY", "buy", 1_000.0)
    broker.step(T1)
    account = broker.get_account()
    quantity = broker.get_positions()["SPY"].quantity
    assert account.equity == pytest.approx(account.cash + quantity * 103)
    assert account.margin_requirement == 0.0


def test_buying_more_averages_the_price() -> None:
    broker = _broker()
    broker.submit_order("SPY", "buy", 1_000.0)
    broker.step(T1)
    broker.submit_order("SPY", "buy", 1_000.0)
    broker.step(T2)
    position = broker.get_positions()["SPY"]
    first, second = 1_000 / (102 * 1.0005), 1_000 / (103 * 1.0005)
    assert position.quantity == pytest.approx(first + second)
    assert position.average_price == pytest.approx(2_000 / (first + second))


def test_cancel_all_and_validation() -> None:
    broker = _broker()
    broker.submit_order("SPY", "buy", 100.0)
    broker.submit_order("SPY", "buy", 100.0)
    assert broker.cancel_all() == 2
    assert broker.cancel_all() == 0
    with pytest.raises(ValueError, match="notional"):
        broker.submit_order("SPY", "buy", 0)
    with pytest.raises(ValueError, match="limit_price"):
        broker.submit_order("SPY", "buy", 100.0, "limit")
    with pytest.raises(ValueError, match="no prices"):
        broker.submit_order("QQQ", "buy", 100.0)
    with pytest.raises(ValueError, match="forward"):
        broker.step(T0)


def test_missing_bar_cancels_and_market_calendar() -> None:
    prices = {"SPY": PRICES["SPY"].drop(index=pd.Timestamp(T1))}
    broker = SimBroker(prices, CONFIG, cash=1_000.0, start=T0)
    order_id = broker.submit_order("SPY", "buy", 100.0)
    broker.step(T1)
    assert broker.get_order(order_id).status == "cancelled"
    assert broker.is_market_open(T1)
    assert not broker.is_market_open(date(2026, 11, 26))  # Thanksgiving
    assert broker.today == T1


def test_full_sell_closes_the_position_after_a_gap_up() -> None:
    gap = {"SPY": _bars([(99, 101, 98, 100), (102, 104, 100, 103), (110, 112, 109, 111)])}
    broker = SimBroker(gap, CONFIG, cash=1_000.0, start=T0)
    broker.submit_order("SPY", "buy", 500.0)
    broker.step(T1)
    value = broker.get_positions()["SPY"].quantity * 103  # the close of T1
    full = broker.submit_order("SPY", "sell", value)
    broker.step(T2)  # opens 7% higher: value / price would leave shares behind
    assert broker.get_order(full).status == "filled"
    assert "SPY" not in broker.get_positions()


def test_partial_sell_is_not_a_full_exit() -> None:
    broker = _broker()
    broker.submit_order("SPY", "buy", 1_000.0)
    broker.step(T1)
    held = broker.get_positions()["SPY"].quantity
    broker.submit_order("SPY", "sell", 0.5 * held * 103)
    broker.step(T2)
    assert broker.get_positions()["SPY"].quantity == pytest.approx(
        held - 0.5 * held * 103 / (103 * (1 - 0.0005))
    )


def test_deposits_and_interest_credit_cash() -> None:
    broker = _broker(cash=100.0)
    broker.deposit(20.0)
    broker.credit_interest(0.5)
    broker.credit_interest(0.0)
    assert broker.get_account().cash == pytest.approx(120.5)
    with pytest.raises(ValueError, match="deposit"):
        broker.deposit(0.0)
    with pytest.raises(ValueError, match="interest"):
        broker.credit_interest(-1.0)


def test_exit_split_in_two_orders_closes_the_whole_position() -> None:
    gap = {"SPY": _bars([(99, 101, 98, 100), (102, 104, 100, 103), (110, 112, 109, 111)])}
    broker = SimBroker(gap, CONFIG, cash=1_000.0, start=T0)
    broker.submit_order("SPY", "buy", 900.0)
    broker.step(T1)
    value = broker.get_positions()["SPY"].quantity * 103
    first = broker.submit_order("SPY", "sell", 0.5 * value)
    last = broker.submit_order("SPY", "sell", 0.5 * value)  # together: the whole position
    broker.step(T2)
    assert broker.get_order(first).status == "filled"
    assert broker.get_order(last).status == "filled"
    assert "SPY" not in broker.get_positions()
