"""The abstract Broker interface (SPEC section 8).

The same code runs in backtest, paper and live; only the implementation chosen by
`mode` changes. All amounts are USD; quantities may be fractional.
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


class Broker(ABC):
    @abstractmethod
    def get_account(self) -> Account:
        """Equity, cash, buying power and margin requirement."""

    @abstractmethod
    def get_positions(self) -> dict[str, Position]:
        """Quantity and average price per symbol."""

    @abstractmethod
    def submit_order(
        self,
        symbol: str,
        side: Side,
        notional: float,
        type: OrderType = "market",
        limit_price: float | None = None,
    ) -> str:
        """Send an order for `notional` dollars; returns its order id."""

    @abstractmethod
    def get_order(self, order_id: str) -> OrderState:
        """Status, filled quantity, average price and commission."""

    @abstractmethod
    def cancel_all(self) -> int:
        """Cancel every pending order; returns how many were cancelled."""

    @abstractmethod
    def is_market_open(self, day: date) -> bool:
        """Whether NYSE is open on `day`."""
