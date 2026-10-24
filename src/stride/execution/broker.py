"""The abstract Broker interface (SPEC section 8).

The same code runs in backtest, paper and live; only the implementation chosen by
`mode` changes. All amounts are USD; quantities may be fractional.

Duplicates (SPEC 8): the caller gives each order a deterministic
`client_order_id`. A failed or timed-out `submit_order` is never retried blindly:
the caller looks the id up with `find_order` first, and resends only if the
broker has no order with that id.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date
from typing import Literal

Side = Literal["buy", "sell"]
OrderType = Literal["market", "limit"]
OrderStatus = Literal["pending", "filled", "cancelled", "rejected"]


@dataclass(frozen=True)
class Account:
    equity: float
    cash: float
    buying_power: float
    margin_requirement: float


@dataclass(frozen=True)
class Position:
    symbol: str
    quantity: float
    average_price: float


@dataclass(frozen=True)
class OrderState:
    order_id: str
    symbol: str
    side: Side
    notional: float
    type: OrderType
    limit_price: float | None
    submitted: date
    status: OrderStatus
    filled_quantity: float = 0.0
    average_price: float | None = None
    commission: float = 0.0
    filled: date | None = None
    reason: str = ""
    client_order_id: str | None = None


class BrokerError(Exception):
    """A broker call failed. For `submit_order` the order's fate is unknown until
    `find_order` says otherwise. The text never carries a credential."""


class Broker(ABC):
    @abstractmethod
    def get_account(self) -> Account: ...

    @abstractmethod
    def get_positions(self) -> dict[str, Position]: ...

    @abstractmethod
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
        """Send one order and return the broker's order id. `quantity` (whole
        shares) replaces `notional` for an asset that is not fractionable."""

    @abstractmethod
    def get_order(self, order_id: str) -> OrderState: ...

    @abstractmethod
    def find_order(self, client_order_id: str) -> OrderState | None:
        """The order sent with this client id, or None if the broker has none."""

    @abstractmethod
    def is_fractionable(self, symbol: str) -> bool:
        """Whether the broker accepts fractional (notional) orders for `symbol`."""

    @abstractmethod
    def cancel_all(self) -> int: ...

    @abstractmethod
    def is_market_open(self, day: date) -> bool: ...
