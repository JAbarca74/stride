from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import requests
from alpaca.common.exceptions import APIError
from alpaca.trading.enums import OrderSide, OrderStatus, OrderType, TimeInForce
from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest

from stride.config import load_config
from stride.execution.alpaca_broker import AlpacaBroker
from stride.execution.broker import Broker, BrokerError
from stride.settings import CredentialError, load_secrets

ROOT = Path(__file__).resolve().parents[1]
CONFIG = load_config(ROOT / "config.yaml", env={})  # mode: paper
FAKE_PAPER = "PKFAKENOTREAL"
FAKE_LIVE = "AKFAKENOTREAL"
FAKE_SECRET = "fake-secret-not-real"
NOW = datetime(2026, 10, 21, 13, 30, tzinfo=UTC)


def _api_error(status: int, code: int, message: str) -> APIError:
    """An alpaca-py APIError as the SDK raises it, with an HTTP status."""
    http = SimpleNamespace(response=SimpleNamespace(status_code=status), request=None)
    return APIError(json.dumps({"code": code, "message": message}), http)  # type: ignore[no-untyped-call]


class FakeClient:
    """Stands in for alpaca's TradingClient; returns SDK-shaped objects."""

    def __init__(self) -> None:
        self.submitted: list[Any] = []
        self.orders: dict[str, SimpleNamespace] = {}
        self.asset_calls = 0

    def get_account(self) -> SimpleNamespace:
        return SimpleNamespace(equity="10250.50", cash="4000", buying_power="8000",
                               maintenance_margin="1200.25")  # fmt: skip

    def get_all_positions(self) -> list[SimpleNamespace]:
        return [SimpleNamespace(symbol="SPY", qty="3.5", avg_entry_price="580.10")]

    def submit_order(self, order_data: Any) -> SimpleNamespace:
        self.submitted.append(order_data)
        return SimpleNamespace(id=f"order-{len(self.submitted)}")

    def get_order_by_id(self, order_id: Any) -> SimpleNamespace:
        return self.orders[str(order_id)]

    def get_order_by_client_id(self, client_id: str) -> SimpleNamespace:
        for order in self.orders.values():
            if order.client_order_id == client_id:
                return order
        raise _api_error(404, 40410000, "order not found")

    def get_asset(self, symbol_or_asset_id: Any) -> SimpleNamespace:
        self.asset_calls += 1
        return SimpleNamespace(symbol=symbol_or_asset_id, fractionable=symbol_or_asset_id != "UUP")

    def cancel_orders(self) -> list[SimpleNamespace]:
        return [SimpleNamespace(id="a"), SimpleNamespace(id="b")]

    def get_calendar(self, filters: Any = None) -> list[SimpleNamespace]:
        day = filters.start
        return [] if day == date(2026, 11, 26) else [SimpleNamespace(date=day)]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY"):
        monkeypatch.delenv(name, raising=False)


def _secrets(tmp_path: Path, key: str = FAKE_PAPER) -> Any:
    env = tmp_path / ".env"
    env.write_text(f"ALPACA_API_KEY={key}\nALPACA_SECRET_KEY={FAKE_SECRET}\n", encoding="utf-8")
    return load_secrets(env)


def _broker(tmp_path: Path, client: FakeClient | None = None) -> AlpacaBroker:
    return AlpacaBroker(CONFIG, _secrets(tmp_path), client or FakeClient())


def test_is_a_broker(tmp_path: Path) -> None:
    assert isinstance(_broker(tmp_path), Broker)


def test_reads_account_in_paper(tmp_path: Path) -> None:
    account = _broker(tmp_path).get_account()
    assert (account.equity, account.cash, account.buying_power, account.margin_requirement) == (
        10250.50, 4000.0, 8000.0, 1200.25)  # fmt: skip


def test_reads_positions_in_paper(tmp_path: Path) -> None:
    [(symbol, position)] = _broker(tmp_path).get_positions().items()
    assert (symbol, position.quantity, position.average_price) == ("SPY", 3.5, 580.10)


def test_refuses_live_keys(tmp_path: Path) -> None:
    with pytest.raises(CredentialError, match="not a paper-trading key") as info:
        AlpacaBroker(CONFIG, _secrets(tmp_path, FAKE_LIVE), FakeClient())
    assert FAKE_LIVE not in str(info.value)


@pytest.mark.parametrize("mode", ["live", "backtest"])
def test_refuses_any_mode_but_paper(tmp_path: Path, mode: str) -> None:
    config = CONFIG.model_copy(update={"mode": mode})
    with pytest.raises(CredentialError, match="paper only"):
        AlpacaBroker(config, _secrets(tmp_path), FakeClient())


def test_refuses_missing_keys(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("ALPACA_API_KEY=\n", encoding="utf-8")
    with pytest.raises(CredentialError, match="not set"):
        AlpacaBroker(CONFIG, load_secrets(env), FakeClient())


def test_default_client_targets_the_paper_endpoint(tmp_path: Path) -> None:
    broker = AlpacaBroker(CONFIG, _secrets(tmp_path))  # builds a real TradingClient, no call
    assert broker._client._use_raw_data is False  # type: ignore[attr-defined]
    assert "paper" in broker._client._base_url  # type: ignore[attr-defined]


def test_submits_notional_day_orders(tmp_path: Path) -> None:
    client = FakeClient()
    broker = _broker(tmp_path, client)
    market_id = broker.submit_order("SPY", "buy", 20.004)
    limit_id = broker.submit_order("GLD", "sell", 15.0, "limit", 251.256)
    market, limit = client.submitted
    assert (market_id, limit_id) == ("order-1", "order-2")
    assert isinstance(market, MarketOrderRequest)
    assert (market.symbol, market.notional, market.side, market.time_in_force) == (
        "SPY", 20.0, OrderSide.BUY, TimeInForce.DAY)  # fmt: skip
    assert isinstance(limit, LimitOrderRequest)
    assert (limit.side, limit.limit_price) == (OrderSide.SELL, 251.26)
    assert market.client_order_id != limit.client_order_id
    assert str(market.client_order_id).startswith("stride-")


def test_order_validation(tmp_path: Path) -> None:
    broker = _broker(tmp_path)
    with pytest.raises(ValueError, match="notional"):
        broker.submit_order("SPY", "buy", 0)
    with pytest.raises(ValueError, match="limit_price"):
        broker.submit_order("SPY", "buy", 10.0, "limit")


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (OrderStatus.FILLED, "filled"),
        (OrderStatus.NEW, "pending"),
        (OrderStatus.PARTIALLY_FILLED, "pending"),
        (OrderStatus.CANCELED, "cancelled"),
        (OrderStatus.EXPIRED, "cancelled"),
        (OrderStatus.REJECTED, "rejected"),
    ],
)
def test_order_status_mapping(tmp_path: Path, status: OrderStatus, expected: str) -> None:
    client = FakeClient()
    filled = status == OrderStatus.FILLED
    client.orders["x"] = SimpleNamespace(
        id="x", symbol="SPY", side=OrderSide.BUY, type=OrderType.MARKET, notional="20",
        limit_price=None, submitted_at=NOW, filled_at=NOW if filled else None,
        status=status, filled_qty="0.0345" if filled else "0",
        filled_avg_price="580.0" if filled else None,
    )  # fmt: skip
    order = _broker(tmp_path, client).get_order("x")
    assert order.status == expected
    assert order.commission == 0.0
    assert order.submitted == NOW.date()
    if filled:
        assert (order.filled_quantity, order.average_price, order.filled) == (
            0.0345, 580.0, NOW.date())  # fmt: skip


def test_limit_order_state(tmp_path: Path) -> None:
    client = FakeClient()
    client.orders["y"] = SimpleNamespace(
        id="y", symbol="GLD", side=OrderSide.SELL, type=OrderType.LIMIT, notional="15",
        limit_price="251.26", submitted_at=NOW, filled_at=None, status=OrderStatus.NEW,
        filled_qty="0", filled_avg_price=None,
    )  # fmt: skip
    order = _broker(tmp_path, client).get_order("y")
    assert (order.side, order.type, order.limit_price) == ("sell", "limit", 251.26)


def test_cancel_all_and_calendar(tmp_path: Path) -> None:
    broker = _broker(tmp_path)
    assert broker.cancel_all() == 2
    assert broker.is_market_open(date(2026, 10, 21))
    assert not broker.is_market_open(date(2026, 11, 26))


def _order(order_id: str, client_order_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=order_id, client_order_id=client_order_id, symbol="SPY", side=OrderSide.BUY,
        type=OrderType.MARKET, notional="20", limit_price=None, submitted_at=NOW,
        filled_at=None, status=OrderStatus.NEW, filled_qty="0", filled_avg_price=None,
    )  # fmt: skip


class FlakyClient(FakeClient):
    """Fails the first `failures` calls of every method with `error`."""

    def __init__(self, error: Exception, failures: int) -> None:
        super().__init__()
        self.error, self.failures, self.calls = error, failures, 0

    def _maybe_fail(self) -> None:
        self.calls += 1
        if self.calls <= self.failures:
            raise self.error

    def get_account(self) -> SimpleNamespace:
        self._maybe_fail()
        return super().get_account()

    def submit_order(self, order_data: Any) -> SimpleNamespace:
        self._maybe_fail()
        return super().submit_order(order_data)


def _flaky(tmp_path: Path, client: FakeClient) -> tuple[AlpacaBroker, list[float]]:
    sleeps: list[float] = []
    return AlpacaBroker(CONFIG, _secrets(tmp_path), client, sleep=sleeps.append), sleeps


def test_client_order_id_is_passed_through(tmp_path: Path) -> None:
    client = FakeClient()
    _broker(tmp_path, client).submit_order(
        "SPY", "buy", 20.0, client_order_id="stride-20261021-SPY-buy-1"
    )
    assert client.submitted[0].client_order_id == "stride-20261021-SPY-buy-1"


def test_find_order_found(tmp_path: Path) -> None:
    client = FakeClient()
    client.orders["o-1"] = _order("o-1", "stride-20261021-SPY-buy-1")
    found = _broker(tmp_path, client).find_order("stride-20261021-SPY-buy-1")
    assert found is not None
    assert (found.order_id, found.client_order_id, found.status) == (
        "o-1", "stride-20261021-SPY-buy-1", "pending")  # fmt: skip


def test_find_order_not_found_is_none(tmp_path: Path) -> None:
    assert _broker(tmp_path).find_order("stride-20261021-SPY-buy-9") is None


def test_find_order_other_errors_raise(tmp_path: Path) -> None:
    class Forbidden(FakeClient):
        def get_order_by_client_id(self, client_id: str) -> SimpleNamespace:
            raise _api_error(403, 40310000, "forbidden")

    with pytest.raises(BrokerError, match=r"find_order failed: APIError \(HTTP 403\)"):
        _broker(tmp_path, Forbidden()).find_order("x")


def test_submit_is_never_retried(tmp_path: Path) -> None:
    client = FlakyClient(requests.ConnectionError("boom"), failures=1)
    broker, sleeps = _flaky(tmp_path, client)
    with pytest.raises(BrokerError, match="submit_order failed: ConnectionError"):
        broker.submit_order("SPY", "buy", 20.0, client_order_id="stride-20261021-SPY-buy-1")
    assert (client.calls, client.submitted, sleeps) == (1, [], [])


def test_reads_retry_transient_errors_with_backoff(tmp_path: Path) -> None:
    client = FlakyClient(_api_error(503, 50300000, "unavailable"), failures=2)
    broker, sleeps = _flaky(tmp_path, client)
    assert broker.get_account().equity == 10250.50
    assert (client.calls, sleeps) == (3, [1.0, 2.0])


def test_reads_give_up_after_three_retries(tmp_path: Path) -> None:
    client = FlakyClient(requests.Timeout("slow"), failures=10)
    broker, sleeps = _flaky(tmp_path, client)
    with pytest.raises(BrokerError, match="get_account failed: Timeout"):
        broker.get_account()
    assert (client.calls, sleeps) == (4, [1.0, 2.0, 4.0])


def test_reads_do_not_retry_client_errors(tmp_path: Path) -> None:
    client = FlakyClient(_api_error(401, 40110000, "unauthorized"), failures=10)
    broker, sleeps = _flaky(tmp_path, client)
    with pytest.raises(BrokerError, match="HTTP 401"):
        broker.get_account()
    assert (client.calls, sleeps) == (1, [])


@pytest.mark.parametrize("method", ["submit", "read"])
def test_no_key_in_any_error_text(tmp_path: Path, method: str) -> None:
    leaky = RuntimeError(f"request failed with key {FAKE_PAPER} and {FAKE_SECRET}")
    broker, _ = _flaky(tmp_path, FlakyClient(leaky, failures=10))
    with pytest.raises(BrokerError) as info:
        if method == "submit":
            broker.submit_order("SPY", "buy", 20.0)
        else:
            broker.get_account()
    text = str(info.value) + repr(info.value)
    assert FAKE_PAPER not in text
    assert FAKE_SECRET not in text
    assert info.value.__cause__ is None
    assert info.value.__suppress_context__


def test_is_fractionable_uses_the_asset_flag_once(tmp_path: Path) -> None:
    client = FakeClient()
    broker = _broker(tmp_path, client)
    assert broker.is_fractionable("SPY")
    assert broker.is_fractionable("SPY")
    assert not broker.is_fractionable("UUP")
    assert client.asset_calls == 2


def test_quantity_orders(tmp_path: Path) -> None:
    client = FakeClient()
    broker = _broker(tmp_path, client)
    broker.submit_order("UUP", "buy", 56.0, quantity=2)
    broker.submit_order("SPY", "sell", 20.0, quantity=0.0345)
    whole, full_exit = client.submitted
    assert (whole.qty, whole.notional) == (2, None)
    assert (full_exit.qty, full_exit.notional) == (0.0345, None)
    with pytest.raises(ValueError, match="quantity"):
        broker.submit_order("UUP", "buy", 56.0, quantity=0)
