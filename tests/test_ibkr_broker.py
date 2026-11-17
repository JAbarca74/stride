"""`execution/ibkr_broker.py` against a fake IB Gateway: no network, no Gateway.

Covers docs/design/ibkr_broker.md section 8: the six gates, notional-to-shares
invariants (hypothesis), OPG vs DAY, status mapping, commission over partial fills,
find_order across sessions, cancel_all leaving manual orders alone, and no account
id or host in any error text.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from ib_async import (
    AccountValue,
    CommissionReport,
    Contract,
    ContractDetails,
    Execution,
    Fill,
    OrderStatus,
    Position,
    Trade,
    TradeLogEntry,
)
from pydantic import SecretStr

from stride import run_daily
from stride.config import LIVE_CONFIRM_ENV, LIVE_CONFIRM_VALUE, StrideConfig, load_config
from stride.execution.broker import Broker, BrokerError
from stride.execution.ibkr_broker import (
    BrokerUnavailable,
    IBKRBroker,
    OrderTooSmall,
    check_gates,
    commission_of,
    floor_to_increment,
    map_status,
    order_quantity,
)
from stride.settings import CredentialError, IbkrConnection, MissingCredentialError, Secrets

ROOT = Path(__file__).resolve().parents[1]
PAPER = load_config(ROOT / "config.paper_ibkr.yaml", env={})
ACCOUNT = "DU7654321"
LIVE_ACCOUNT = "U7654321"
HOST = "10.9.8.7"


def _secrets(account: str = ACCOUNT, port: int = 4002) -> Secrets:
    return Secrets(  # type: ignore[call-arg]
        _env_file=None,
        ibkr_host=HOST,
        ibkr_port=port,
        ibkr_client_id=7,
        ibkr_account=SecretStr(account),
    )


def _live(config: StrideConfig) -> StrideConfig:
    return config.model_copy(update={"mode": "live"})


class FakeIB:
    """Records calls and returns canned ib_async objects."""

    def __init__(self, accounts: list[str] | None = None, connected: bool = True) -> None:
        self.accounts = accounts if accounts is not None else [ACCOUNT]
        self.connected = connected
        self.connect_args: dict[str, Any] = {}
        self.placed: list[tuple[Any, Any]] = []
        self.cancelled: list[Any] = []
        self.summary: list[AccountValue] = []
        self.held: list[Position] = []
        self.details: dict[str, ContractDetails] = {}
        self.open: list[Trade] = []
        self.completed: list[Trade] = []
        self.next_perm = 1000
        self.fail_place = False
        self.disconnects = 0

    def connect(self, host: str, port: int, **kwargs: Any) -> None:
        self.connect_args = {"host": host, "port": port, **kwargs}

    def disconnect(self) -> None:
        self.disconnects += 1

    def isConnected(self) -> bool:
        return self.connected

    def managedAccounts(self) -> list[str]:
        return self.accounts

    def accountSummary(self, account: str) -> list[AccountValue]:
        return [v for v in self.summary if v.account == account]

    def positions(self, account: str) -> list[Position]:
        return [p for p in self.held if p.account == account]

    def qualifyContracts(self, *contracts: Contract) -> list[Contract]:
        return [
            Contract(symbol=c.symbol, secType="STK", currency="USD", conId=1) for c in contracts
        ]

    def reqContractDetails(self, contract: Contract) -> list[ContractDetails]:
        found = self.details.get(contract.symbol)
        return [found] if found else []

    def placeOrder(self, contract: Contract, order: Any) -> Trade:
        if self.fail_place:
            raise TimeoutError(f"no answer for {order.account} at {HOST}")
        self.next_perm += 1
        order.permId = self.next_perm
        trade = Trade(contract, order, OrderStatus(status="PreSubmitted", permId=order.permId))
        trade.log.append(TradeLogEntry(time=datetime(2026, 11, 17, 14, tzinfo=UTC)))
        self.placed.append((contract, order))
        self.open.append(trade)
        return trade

    def trades(self) -> list[Trade]:
        return [*self.open, *self.completed]

    def openTrades(self) -> list[Trade]:
        return list(self.open)

    def reqAllOpenOrders(self) -> list[Trade]:
        return list(self.open)

    def reqCompletedOrders(self, apiOnly: bool) -> list[Trade]:
        return list(self.completed)

    def cancelOrder(self, order: Any) -> None:
        self.cancelled.append(order)


def _broker(
    fake: FakeIB | None = None, config: StrideConfig = PAPER, **kwargs: Any
) -> tuple[IBKRBroker, FakeIB]:
    fake = fake or FakeIB()
    secrets = kwargs.pop("secrets", _secrets())
    broker = IBKRBroker(
        config, secrets, last_close=lambda s: 100.0, client=fake, sleep=lambda _: None, **kwargs
    )
    return broker, fake


def test_implements_the_interface() -> None:
    broker, fake = _broker()
    assert isinstance(broker, Broker)
    assert fake.connect_args["clientId"] == 7
    assert fake.connect_args["account"] == ACCOUNT


# Gates.


def test_backtest_mode_is_refused() -> None:
    config = PAPER.model_copy(update={"mode": "backtest"})
    with pytest.raises(CredentialError, match="mode"):
        _broker(config=config)


def test_live_without_live_confirm_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(LIVE_CONFIRM_ENV, raising=False)
    with pytest.raises(CredentialError, match=LIVE_CONFIRM_ENV):
        _broker(config=_live(PAPER), secrets=_secrets(LIVE_ACCOUNT, 4001))


def test_live_with_a_paper_account_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(LIVE_CONFIRM_ENV, LIVE_CONFIRM_VALUE)
    with pytest.raises(CredentialError, match="paper account"):
        _broker(config=_live(PAPER), secrets=_secrets(ACCOUNT, 4001))


def test_paper_with_a_live_account_is_refused() -> None:
    with pytest.raises(CredentialError, match="live account"):
        _broker(secrets=_secrets(LIVE_ACCOUNT, 4002))


@pytest.mark.parametrize("port", [4001, 7496])
def test_paper_on_a_live_port_is_refused(port: int) -> None:
    with pytest.raises(CredentialError, match="live port"):
        _broker(secrets=_secrets(ACCOUNT, port))


def test_live_on_a_paper_port_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(LIVE_CONFIRM_ENV, LIVE_CONFIRM_VALUE)
    with pytest.raises(CredentialError, match="paper port"):
        check_gates(_live(PAPER), IbkrConnection(HOST, 4002, 1, LIVE_ACCOUNT), readonly=True)


def test_account_not_managed_is_refused_and_disconnects() -> None:
    fake = FakeIB(accounts=["DU0000001"])
    with pytest.raises(CredentialError, match="not managed"):
        _broker(fake)
    assert fake.disconnects == 1
    assert not fake.placed


def test_gateway_down_is_broker_unavailable() -> None:
    with pytest.raises(BrokerUnavailable):
        _broker(FakeIB(connected=False))


def test_missing_settings_are_named() -> None:
    with pytest.raises(MissingCredentialError, match="IBKR_ACCOUNT"):
        Secrets(_env_file=None, ibkr_host="h", ibkr_port=1, ibkr_client_id=1).ibkr_connection()  # type: ignore[call-arg]


def test_connection_repr_hides_host_and_account() -> None:
    text = repr(_secrets().ibkr_connection())
    assert ACCOUNT not in text and HOST not in text


def test_readonly_connection_never_sends() -> None:
    broker, fake = _broker(readonly=True)
    assert fake.connect_args["readonly"] is True
    with pytest.raises(BrokerError, match="read-only"):
        broker.submit_order("SPY", "buy", 1_000.0)
    assert broker.cancel_all() == 0
    assert not fake.placed


# Orders.


def test_market_order_is_market_on_open_with_account_and_ref() -> None:
    broker, fake = _broker()
    order_id = broker.submit_order("SPY", "buy", 1_050.0, client_order_id="stride-abc")
    _, order = fake.placed[0]
    assert (order.action, order.orderType, order.tif) == ("BUY", "MKT", "OPG")
    assert order.totalQuantity == 10  # floor(1,050 / 100), whole shares by default
    assert order.account == ACCOUNT and order.orderRef == "stride-abc"
    assert order.outsideRth is False
    assert order_id == str(order.permId)


def test_limit_order_is_a_day_order_sized_at_the_limit() -> None:
    broker, fake = _broker()
    broker.submit_order("SPY", "sell", 1_000.0, "limit", 99.994)
    _, order = fake.placed[0]
    assert (order.action, order.orderType, order.tif) == ("SELL", "LMT", "DAY")
    assert order.lmtPrice == 99.99
    assert order.totalQuantity == 10  # floor(1,000 / 99.994)


def test_fractional_increment_and_given_quantity() -> None:
    fake = FakeIB()
    fake.details["SPY"] = ContractDetails(sizeIncrement=0.0001, minSize=0.0001)
    broker, _ = _broker(fake)
    assert broker.is_fractionable("SPY")
    broker.submit_order("SPY", "buy", 50.0)
    assert fake.placed[-1][1].totalQuantity == pytest.approx(0.5)
    broker.submit_order("SPY", "sell", 0.0, quantity=3.25)
    assert fake.placed[-1][1].totalQuantity == pytest.approx(3.25)


def test_order_below_the_minimum_size_is_not_sent() -> None:
    broker, fake = _broker()
    with pytest.raises(OrderTooSmall):
        broker.submit_order("SPY", "buy", 99.0)  # less than one share at 100
    assert not fake.placed


def test_submit_is_never_retried_and_hides_details() -> None:
    fake = FakeIB()
    fake.fail_place = True
    broker, _ = _broker(fake)
    with pytest.raises(BrokerError) as raised:
        broker.submit_order("SPY", "buy", 1_000.0)
    assert ACCOUNT not in str(raised.value) and HOST not in str(raised.value)
    assert "TimeoutError" in str(raised.value)
    assert not fake.placed


def test_find_order_finds_an_order_of_an_earlier_session() -> None:
    first, fake = _broker()
    first.submit_order("SPY", "buy", 1_000.0, client_order_id="stride-1")
    first.close()
    trade = fake.open.pop()
    trade.orderStatus.status = "Filled"
    trade.orderStatus.filled = 10
    trade.orderStatus.avgFillPrice = 100.5
    fake.completed.append(trade)
    second, _ = _broker(fake)
    found = second.find_order("stride-1")
    assert found is not None
    assert (found.status, found.filled_quantity, found.average_price) == ("filled", 10, 100.5)
    assert found.client_order_id == "stride-1"
    assert second.find_order("stride-unknown") is None
    assert second.get_order(found.order_id).symbol == "SPY"


def test_cancel_all_leaves_manual_orders_alone() -> None:
    broker, fake = _broker()
    broker.submit_order("SPY", "buy", 1_000.0, client_order_id="stride-x")
    broker.submit_order("SPY", "buy", 1_000.0, client_order_id="manual-order")
    assert broker.cancel_all() == 1
    assert [o.orderRef for o in fake.cancelled] == ["stride-x"]


@pytest.mark.parametrize(
    ("ib", "stride"),
    [
        ("Filled", "filled"),
        ("Cancelled", "cancelled"),
        ("ApiCancelled", "cancelled"),
        ("Inactive", "rejected"),
        ("ValidationError", "rejected"),
        ("PreSubmitted", "pending"),
        ("Submitted", "pending"),
        ("PendingSubmit", "pending"),
    ],
)
def test_status_mapping(ib: str, stride: str) -> None:
    assert map_status(ib) == stride


def test_commission_is_summed_over_partial_fills_and_skips_unset() -> None:
    def fill(value: float) -> Fill:
        return Fill(Contract(), Execution(), CommissionReport(commission=value), datetime.now(UTC))

    assert commission_of([fill(0.35), fill(0.40), fill(1.7976931348623157e308)]) == pytest.approx(
        0.75
    )


# Account and positions.


def test_account_reads_usd_rows_of_the_account() -> None:
    fake = FakeIB()
    fake.summary = [
        AccountValue(ACCOUNT, "NetLiquidation", "10500.5", "USD", ""),
        AccountValue(ACCOUNT, "TotalCashValue", "2000", "USD", ""),
        AccountValue(ACCOUNT, "BuyingPower", "4000", "USD", ""),
        AccountValue(ACCOUNT, "MaintMarginReq", "125", "USD", ""),
        AccountValue(ACCOUNT, "NetLiquidation", "1", "EUR", ""),
    ]
    broker, _ = _broker(fake)
    account = broker.get_account()
    assert (account.equity, account.cash, account.buying_power, account.margin_requirement) == (
        10500.5, 2000.0, 4000.0, 125.0,
    )  # fmt: skip


def test_account_without_net_liquidation_fails() -> None:
    broker, _ = _broker()
    with pytest.raises(BrokerError, match="missing"):
        broker.get_account()


def test_positions_report_non_usd_stock_as_a_separate_key() -> None:
    fake = FakeIB()
    fake.held = [
        Position(ACCOUNT, Contract(symbol="SPY", secType="STK", currency="USD"), 3.0, 400.0),
        Position(ACCOUNT, Contract(symbol="ES", secType="FUT", currency="USD"), 1.0, 5.0),
        Position(ACCOUNT, Contract(symbol="IEF", secType="STK", currency="USD"), 0.0, 90.0),
    ]
    broker, _ = _broker(fake)
    positions = broker.get_positions()
    assert set(positions) == {"SPY", "ES:FUT:USD"}
    assert positions["SPY"].quantity == 3.0


def test_market_open_uses_the_nyse_calendar() -> None:
    broker, _ = _broker()
    assert not broker.is_market_open(date(2026, 11, 26))  # Thanksgiving
    assert broker.is_market_open(date(2026, 11, 27))


# Invariants of code that moves money (owner rule).


@settings(max_examples=300, deadline=None)
@given(
    notional=st.floats(0.01, 1e7),
    price=st.floats(0.5, 5_000),
    increment=st.sampled_from([1.0, 0.1, 0.01, 0.0001]),
    min_multiple=st.integers(1, 5),
)
def test_a_buy_never_exceeds_its_notional(
    notional: float, price: float, increment: float, min_multiple: int
) -> None:
    min_size = increment * min_multiple
    qty = order_quantity(notional, price, increment, min_size)
    assert qty * price <= notional + 1e-6
    if qty:
        assert qty >= min_size - 1e-9
        steps = qty / increment
        assert abs(steps - round(steps)) < 1e-6 * max(1.0, steps)
    else:
        assert floor_to_increment(notional / price, increment) < min_size + 1e-9


def test_floor_to_increment_rejects_bad_increment() -> None:
    with pytest.raises(ValueError, match="increment"):
        floor_to_increment(1.0, 0.0)
    assert floor_to_increment(-1.0, 1.0) == 0.0


# Wiring.


def test_paper_ibkr_config_is_the_dip_instance_on_ibkr() -> None:
    dip = load_config(ROOT / "config.paper_dip.yaml", env={})
    expected = dip.model_dump()
    expected["data"]["db_path"] = "data/paper_ibkr.db"
    expected["execution"]["broker"] = "ibkr"
    assert PAPER.model_dump() == expected


def test_make_broker_builds_ibkr_for_broker_ibkr(monkeypatch: pytest.MonkeyPatch) -> None:
    from stride.data import store  # noqa: PLC0415

    built: dict[str, Any] = {}

    class Recorder(IBKRBroker):
        def __init__(
            self, config: StrideConfig, secrets: Secrets, last_close: Any, **_: Any
        ) -> None:
            built["config"], built["close"] = config, last_close

    monkeypatch.setattr("stride.execution.ibkr_broker.IBKRBroker", Recorder)
    run_daily.make_broker(PAPER, _secrets(), store.connect(":memory:"))
    assert built["config"] is PAPER
    with pytest.raises(run_daily.RunError, match="no stored close"):
        built["close"]("SPY")


def test_live_phases_stay_refused() -> None:
    instance = run_daily.load_instance(ROOT / "config.paper_ibkr.yaml")
    live = run_daily.Instance(instance.config_path, _live(PAPER), instance.name)
    with pytest.raises(run_daily.RunError, match="live"):
        run_daily.run_phase(live, "evening", run_daily.Deps())
