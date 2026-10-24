"""Alpaca broker, **paper trading only** (SPEC section 8, task T18).

It refuses to start unless `config.mode` is `paper` and the key is a paper key
(Alpaca paper key IDs start with "PK"), and it always talks to the paper endpoint
(`TradingClient(paper=True)`). Live trading goes through IBKR, never through here.

Orders are day orders for `notional` dollars (fractional shares), or for whole
shares (`qty`) when the caller converts them. Each order carries the caller's
`client_order_id` (a random one if none is given), so a resend can be detected
with `find_order`. Alpaca charges no commission, so it is 0.

Retries (SPEC 8): every call except `submit_order` is retried up to `MAX_RETRIES`
times with exponential backoff on network errors and HTTP 429/5xx. A failed or
timed-out submit is never retried here: it raises `BrokerError` and the caller
must call `find_order` before deciding anything. Error texts carry the call name,
the exception type and the HTTP status only, never a key.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable
from datetime import date
from typing import Any, Protocol, TypeVar

from stride.config import StrideConfig
from stride.execution.broker import (
    Account,
    Broker,
    BrokerError,
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
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 1.0
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
NOT_FOUND = 404
_T = TypeVar("_T")


class TradingApi(Protocol):
    """The part of `alpaca.trading.client.TradingClient` this adapter uses."""

    def get_account(self) -> Any: ...
    def get_all_positions(self) -> Any: ...
    def submit_order(self, order_data: Any) -> Any: ...
    def get_order_by_id(self, order_id: Any) -> Any: ...
    def get_order_by_client_id(self, client_id: str) -> Any: ...
    def get_asset(self, symbol_or_asset_id: Any) -> Any: ...
    def cancel_orders(self) -> Any: ...
    def get_calendar(self, filters: Any = None) -> Any: ...


def _text(value: Any) -> str:
    """Alpaca enums and plain strings alike, lower case: OrderSide.BUY -> 'buy'."""
    return str(getattr(value, "value", value)).lower()


def _float(value: Any) -> float:
    return float(value) if value is not None else 0.0


def _status_code(exc: BaseException) -> int | None:
    """The HTTP status of an alpaca-py `APIError` (or anything shaped like one)."""
    try:
        code = getattr(exc, "status_code", None)
    except Exception:
        return None
    return code if isinstance(code, int) else None


def _is_not_found(exc: BaseException) -> bool:
    if _status_code(exc) == NOT_FOUND:
        return True
    try:  # alpaca-py puts the API's own code in a JSON body: 404xxxxx is "not found"
        body = json.loads(str(exc))
    except ValueError:
        return False
    return isinstance(body, dict) and str(body.get("code", "")).startswith("404")


def _transient(exc: BaseException) -> bool:
    """Network errors and HTTP 429/5xx are worth a retry; anything else is not."""
    import requests  # noqa: PLC0415

    if isinstance(exc, (requests.ConnectionError, requests.Timeout, TimeoutError)):
        return True
    return _status_code(exc) in RETRY_STATUS


def _error(call: str, exc: BaseException) -> BrokerError:
    """A `BrokerError` naming the call, the exception type and the HTTP status only:
    the original text could echo a request, so it is never copied."""
    status = _status_code(exc)
    suffix = f" (HTTP {status})" if status is not None else ""
    return BrokerError(f"alpaca {call} failed: {type(exc).__name__}{suffix}")


class AlpacaBroker(Broker):
    def __init__(
        self,
        config: StrideConfig,
        secrets: Secrets,
        client: TradingApi | None = None,
        sleep: Callable[[float], None] = time.sleep,
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
        self._sleep = sleep
        self._fractionable: dict[str, bool] = {}

    def _call(self, name: str, fn: Callable[[], _T]) -> _T:
        """Run one read or cancel call with retries on transient errors."""
        for attempt in range(MAX_RETRIES + 1):
            try:
                return fn()
            except Exception as exc:
                if attempt < MAX_RETRIES and _transient(exc):
                    self._sleep(RETRY_BACKOFF_SECONDS * 2**attempt)
                    continue
                raise _error(name, exc) from None
        raise AssertionError("unreachable")  # pragma: no cover

    def get_account(self) -> Account:
        account = self._call("get_account", self._client.get_account)
        return Account(
            equity=_float(account.equity),
            cash=_float(account.cash),
            buying_power=_float(account.buying_power),
            margin_requirement=_float(account.maintenance_margin),
        )

    def get_positions(self) -> dict[str, Position]:
        return {
            p.symbol: Position(p.symbol, _float(p.qty), _float(p.avg_entry_price))
            for p in self._call("get_all_positions", self._client.get_all_positions)
        }

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
        """Send one order, once. With `quantity` (whole shares, for assets that are
        not fractionable) the order is for that many shares instead of `notional`.
        On any failure raise `BrokerError`; the caller must call
        `find_order(client_order_id)` before sending it again."""
        if notional <= 0:
            raise ValueError("notional must be positive")
        from alpaca.trading.enums import OrderSide, TimeInForce  # noqa: PLC0415
        from alpaca.trading.requests import (  # noqa: PLC0415
            LimitOrderRequest,
            MarketOrderRequest,
        )

        if quantity is not None and (quantity <= 0 or quantity != int(quantity)):
            raise ValueError("quantity must be a positive whole number of shares")
        size: dict[str, Any] = (
            {"qty": int(quantity)} if quantity is not None else {"notional": round(notional, 2)}
        )
        common = {
            "symbol": symbol,
            **size,
            "side": OrderSide.BUY if side == "buy" else OrderSide.SELL,
            "time_in_force": TimeInForce.DAY,
            "client_order_id": client_order_id or f"stride-{uuid.uuid4().hex}",
        }
        if type == "limit":
            if limit_price is None or limit_price <= 0:
                raise ValueError("a limit order needs a positive limit_price")
            request: Any = LimitOrderRequest(limit_price=round(limit_price, 2), **common)
        else:
            request = MarketOrderRequest(**common)
        try:
            return str(self._client.submit_order(request).id)
        except Exception as exc:
            raise _error("submit_order", exc) from None

    def get_order(self, order_id: str) -> OrderState:
        return _state(self._call("get_order", lambda: self._client.get_order_by_id(order_id)))

    def find_order(self, client_order_id: str) -> OrderState | None:
        """The order sent with this client id, or None when Alpaca has none."""

        def lookup() -> Any:
            try:
                return self._client.get_order_by_client_id(client_order_id)
            except Exception as exc:
                if _is_not_found(exc):
                    return None
                raise

        order = self._call("find_order", lookup)
        return _state(order) if order is not None else None

    def is_fractionable(self, symbol: str) -> bool:
        """The asset's `fractionable` flag, asked once per symbol."""
        if symbol not in self._fractionable:
            asset = self._call("get_asset", lambda: self._client.get_asset(symbol))
            self._fractionable[symbol] = bool(getattr(asset, "fractionable", False))
        return self._fractionable[symbol]

    def cancel_all(self) -> int:
        return len(self._call("cancel_orders", self._client.cancel_orders))

    def is_market_open(self, day: date) -> bool:
        from alpaca.trading.requests import GetCalendarRequest  # noqa: PLC0415

        sessions = self._call(
            "get_calendar",
            lambda: self._client.get_calendar(GetCalendarRequest(start=day, end=day)),
        )
        return any(session.date == day for session in sessions)


def _state(order: Any) -> OrderState:
    """An alpaca-py `Order` as an `OrderState`."""
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
        client_order_id=getattr(order, "client_order_id", None),
    )
