"""Alpaca broker, **paper trading only** (SPEC section 8, task T18).

It refuses to start unless `config.mode` is `paper` and the key is a paper key
(Alpaca paper key IDs start with "PK"), and it always talks to the paper endpoint
(`TradingClient(paper=True)`). Live trading goes through IBKR, never through here.

Orders are day orders for `notional` dollars (fractional shares). Each order gets a
fresh `client_order_id` so a resend can be detected; retry policy belongs to the
order layer, not to this adapter. Alpaca charges no commission, so it is 0.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any, Protocol

from stride.config import StrideConfig
from stride.execution.broker import (
    Account,
    Broker,
    OrderState,
    OrderStatus,
    OrderType,
    Position,
    Side,
)
from stride.settings import ALPACA_PAPER_KEY_PREFIX, CredentialError, Secrets

_STATUS: dict[str, OrderStatus] = {
    "filled": "filled",
    "canceled": "cancelled",
    "expired": "cancelled",
    "done_for_day": "cancelled",
    "replaced": "cancelled",
    "rejected": "rejected",
    "suspended": "rejected",
}


class TradingApi(Protocol):
    """The part of `alpaca.trading.client.TradingClient` this adapter uses."""

    def get_account(self) -> Any: ...
    def get_all_positions(self) -> Any: ...
    def submit_order(self, order_data: Any) -> Any: ...
    def get_order_by_id(self, order_id: Any) -> Any: ...
    def cancel_orders(self) -> Any: ...
    def get_calendar(self, filters: Any = None) -> Any: ...


def _text(value: Any) -> str:
    """Alpaca enums and plain strings alike, lower case: OrderSide.BUY -> 'buy'."""
    return str(getattr(value, "value", value)).lower()


def _float(value: Any) -> float:
    return float(value) if value is not None else 0.0


class AlpacaBroker(Broker):
    def __init__(
        self, config: StrideConfig, secrets: Secrets, client: TradingApi | None = None
    ) -> None:
        if config.mode != "paper":
            raise CredentialError(f"AlpacaBroker is paper only; config mode is {config.mode!r}")
        key, secret = secrets.alpaca_paper_keys()  # raises unless the key is a paper key
        if not key.startswith(ALPACA_PAPER_KEY_PREFIX):  # pragma: no cover - guarded above
            raise CredentialError("not a paper-trading key")
        if client is None:
            from alpaca.trading.client import TradingClient  # noqa: PLC0415

            client = TradingClient(key, secret, paper=True)
        self._client: TradingApi = client

    def get_account(self) -> Account:
        account = self._client.get_account()
        return Account(
            equity=_float(account.equity),
            cash=_float(account.cash),
            buying_power=_float(account.buying_power),
            margin_requirement=_float(account.maintenance_margin),
        )

    def get_positions(self) -> dict[str, Position]:
        return {
            p.symbol: Position(p.symbol, _float(p.qty), _float(p.avg_entry_price))
            for p in self._client.get_all_positions()
        }

    def submit_order(
        self,
        symbol: str,
        side: Side,
        notional: float,
        type: OrderType = "market",
        limit_price: float | None = None,
    ) -> str:
        if notional <= 0:
            raise ValueError("notional must be positive")
        from alpaca.trading.enums import OrderSide, TimeInForce  # noqa: PLC0415
        from alpaca.trading.requests import (  # noqa: PLC0415
            LimitOrderRequest,
            MarketOrderRequest,
        )

        common = {
            "symbol": symbol,
            "notional": round(notional, 2),
            "side": OrderSide.BUY if side == "buy" else OrderSide.SELL,
            "time_in_force": TimeInForce.DAY,
            "client_order_id": f"stride-{uuid.uuid4().hex}",
        }
        if type == "limit":
            if limit_price is None or limit_price <= 0:
                raise ValueError("a limit order needs a positive limit_price")
            request: Any = LimitOrderRequest(limit_price=round(limit_price, 2), **common)
        else:
            request = MarketOrderRequest(**common)
        return str(self._client.submit_order(request).id)

    def get_order(self, order_id: str) -> OrderState:
        order = self._client.get_order_by_id(order_id)
        raw_status = _text(order.status)
        side: Side = "buy" if _text(order.side) == "buy" else "sell"
        kind: OrderType = "limit" if _text(order.type) == "limit" else "market"
        filled_at = order.filled_at.date() if order.filled_at is not None else None
        return OrderState(
            order_id=str(order.id),
            symbol=order.symbol,
            side=side,
            notional=_float(order.notional),
            type=kind,
            limit_price=float(order.limit_price) if order.limit_price is not None else None,
            submitted=order.submitted_at.date(),
            status=_STATUS.get(raw_status, "pending"),
            filled_quantity=_float(order.filled_qty),
            average_price=(
                float(order.filled_avg_price) if order.filled_avg_price is not None else None
            ),
            commission=0.0,
            filled=filled_at,
            reason=raw_status,
        )

    def cancel_all(self) -> int:
        return len(self._client.cancel_orders())

    def is_market_open(self, day: date) -> bool:
        from alpaca.trading.requests import GetCalendarRequest  # noqa: PLC0415

        sessions = self._client.get_calendar(GetCalendarRequest(start=day, end=day))
        return any(session.date == day for session in sessions)
