from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from alpaca.trading.enums import OrderSide, OrderStatus, OrderType, TimeInForce
from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest

from stride.config import load_config
from stride.execution.alpaca_broker import AlpacaBroker
from stride.execution.broker import Broker
from stride.settings import CredentialError, load_secrets

ROOT = Path(__file__).resolve().parents[1]
CONFIG = load_config(ROOT / "config.yaml", env={})  # mode: paper
FAKE_PAPER = "PKFAKENOTREAL"
FAKE_LIVE = "AKFAKENOTREAL"
FAKE_SECRET = "fake-secret-not-real"
NOW = datetime(2026, 10, 21, 13, 30, tzinfo=UTC)


class FakeClient:
    """Stands in for alpaca's TradingClient; returns SDK-shaped objects."""

    def __init__(self) -> None:
        self.submitted: list[Any] = []
        self.orders: dict[str, SimpleNamespace] = {}

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
