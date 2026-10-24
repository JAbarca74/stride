"""Simulated broker for the backtest and the tests (SPEC sections 8 and 9).

An order submitted on day t is filled at the open of the next trading day it is
stepped to (t+1), with spread and slippage applied (`backtest/costs.py`) and the
IBKR Pro Fixed commission. Orders are day orders: whatever is not filled that day
is cancelled at its close. Cash only, long only: a buy larger than the cash left
at the fill (after the sells, so after a gap at the open) is filled for what
the cash pays, commission included, and sells are capped at the shares held.

Limit orders: a buy fills at the open if the open is at or below the limit,
otherwise at the limit if the day's low reaches it; sells mirror this with the
high. Spread is applied to the fill price in both cases.

A sell that, with the other sells of the symbol sent the same day, covers the
whole position at that day's close (within `FULL_SELL_TOLERANCE_USD`), or whose
`quantity` covers every share held, is a full exit: it fills after them and
sells every share left, so a gap up at the open leaves no remainder (an exit
split into several orders included). Otherwise orders fill by `notional`: the
simulation is always fractionable, so a given `quantity` only marks a full exit.
Deposits and interest on idle cash are credited by the backtest engine
(`deposit`, `credit_interest`).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import date

import pandas as pd

from stride.backtest.costs import commission, fill_price, max_commission_rate
from stride.config import StrideConfig
from stride.data.calendar import is_trading_day
from stride.execution.broker import (
    Account,
    Broker,
    OrderState,
    OrderType,
    Position,
    Side,
)

_DUST = 1e-12  # a remaining quantity this small counts as a closed position
FULL_SELL_TOLERANCE_USD = 0.01  # the guard's rounding tolerance for sells


class SimBroker(Broker):
    def __init__(
        self,
        prices: Mapping[str, pd.DataFrame],
        config: StrideConfig,
        cash: float,
        start: date,
    ) -> None:
        """`prices` maps symbol -> daily bars (`open`, `high`, `low`, `close`)."""
        self._prices = {s: p.sort_index() for s, p in prices.items()}
        # Closes as arrays for fast "last close on or before a day" lookups.
        self._close_index = {s: pd.DatetimeIndex(p.index) for s, p in self._prices.items()}
        self._close_values = {s: p["close"].to_numpy(dtype=float) for s, p in self._prices.items()}
        self._config = config
        self._cash = float(cash)
        self._today = start
        self._positions: dict[str, Position] = {}
        self._orders: dict[str, OrderState] = {}
        self._full_sells: set[str] = set()  # order ids that close the whole position
        self._client_ids: dict[str, str] = {}  # client order id -> order id

    # Broker interface.

    def get_account(self) -> Account:
        equity = self._cash + sum(
            p.quantity * self._close(p.symbol, self._today) for p in self._positions.values()
        )
        return Account(equity=equity, cash=self._cash, buying_power=self._cash,
                       margin_requirement=0.0)  # fmt: skip

    def get_positions(self) -> dict[str, Position]:
        return dict(self._positions)

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
        if quantity is not None and not quantity > 0:
            raise ValueError("quantity must be positive")
        if client_order_id is not None and client_order_id in self._client_ids:
            raise ValueError("duplicate client_order_id")
        if notional <= 0:
            raise ValueError("notional must be positive")
        if type == "limit" and (limit_price is None or limit_price <= 0):
            raise ValueError("a limit order needs a positive limit_price")
        if symbol not in self._prices:
            raise ValueError(f"no prices for {symbol}")
        order_id = f"sim-{len(self._orders) + 1}"
        self._orders[order_id] = OrderState(
            order_id, symbol, side, float(notional), type, limit_price, self._today, "pending",
            client_order_id=client_order_id,
        )  # fmt: skip
        if client_order_id is not None:
            self._client_ids[client_order_id] = order_id
        held = self._positions.get(symbol)
        if side == "sell" and held is not None:
            value = held.quantity * self._close(symbol, self._today)
            already = sum(
                o.notional
                for o in self._orders.values()
                if o.symbol == symbol and o.side == "sell" and o.status == "pending"
                and o.submitted == self._today and o.order_id != order_id
            )  # fmt: skip
            covers = quantity is not None and quantity >= held.quantity - _DUST
            if covers or already + notional >= value - FULL_SELL_TOLERANCE_USD:
                self._full_sells.add(order_id)
        return order_id

    def get_order(self, order_id: str) -> OrderState:
        return self._orders[order_id]

    def find_order(self, client_order_id: str) -> OrderState | None:
        order_id = self._client_ids.get(client_order_id)
        return self._orders[order_id] if order_id is not None else None

    def is_fractionable(self, symbol: str) -> bool:
        return True

    def cancel_all(self) -> int:
        pending = [o for o in self._orders.values() if o.status == "pending"]
        for order in pending:
            self._orders[order.order_id] = replace(order, status="cancelled")
        return len(pending)

    def is_market_open(self, day: date) -> bool:
        return is_trading_day(day)

    # Cash flows (backtest only).

    def deposit(self, amount: float) -> None:
        """An external deposit credited to cash (SPEC 9: $20 each month)."""
        if not amount > 0:
            raise ValueError("a deposit must be positive")
        self._cash += float(amount)

    def credit_interest(self, amount: float) -> None:
        """Interest earned by idle cash (`costs.cash_interest`); never negative."""
        if amount < 0:
            raise ValueError("interest on cash cannot be negative")
        self._cash += float(amount)

    # Simulation clock.

    @property
    def today(self) -> date:
        return self._today

    def step(self, day: date) -> list[OrderState]:
        """Move to `day`: fill pending orders from earlier days at its open, sells
        first, then cancel what is left (day orders). Returns the orders handled."""
        if day <= self._today:
            raise ValueError("the simulation only moves forward")
        self._today = day
        due = [o for o in self._orders.values() if o.status == "pending" and o.submitted < day]
        # Sells before buys, to free cash; a full exit after the other sells of its symbol.
        due.sort(key=lambda o: (o.side != "sell", o.order_id in self._full_sells))
        handled = []
        for order in due:
            result = self._fill(order, day)
            self._orders[order.order_id] = result
            handled.append(result)
        return handled

    # Internals.

    def _bar(self, symbol: str, day: date) -> pd.Series | None:
        bars = self._prices[symbol]
        stamp = pd.Timestamp(day)
        if stamp not in bars.index:
            return None
        row = bars.loc[stamp]
        return row if isinstance(row, pd.Series) else None

    def _close(self, symbol: str, day: date) -> float:
        position = int(self._close_index[symbol].searchsorted(pd.Timestamp(day), side="right"))
        return float(self._close_values[symbol][position - 1]) if position else 0.0

    def _reference(self, order: OrderState, bar: pd.Series) -> float | None:
        open_, high, low = float(bar["open"]), float(bar["high"]), float(bar["low"])
        if order.type == "market" or order.limit_price is None:
            return open_
        limit = order.limit_price
        if order.side == "buy":
            if open_ <= limit:
                return open_
            return limit if low <= limit else None
        if open_ >= limit:
            return open_
        return limit if high >= limit else None

    def _fill(self, order: OrderState, day: date) -> OrderState:
        bar = self._bar(order.symbol, day)
        if bar is None:
            return replace(order, status="cancelled", reason="no bar on the fill day")
        reference = self._reference(order, bar)
        if reference is None:
            return replace(order, status="cancelled", reason="limit not reached")
        price = fill_price(reference, order.side, order.symbol, self._config)
        held = self._positions.get(order.symbol)
        if order.side == "sell":
            quantity = min(order.notional / price, held.quantity if held else 0.0)
            if order.order_id in self._full_sells and held is not None:
                quantity = held.quantity
            if quantity <= 0:
                return replace(order, status="rejected", reason="no position to sell (long only)")
        else:
            quantity = min(order.notional, self._affordable()) / price
            if quantity <= 0:
                return replace(order, status="rejected", reason="not enough cash")
        fee = commission(quantity, price, self._config)
        cash_change = -(quantity * price + fee) if order.side == "buy" else quantity * price - fee
        if self._cash + cash_change < -_DUST:
            return replace(order, status="rejected", reason="not enough cash")
        self._cash += cash_change
        self._update_position(order.symbol, order.side, quantity, price)
        partial = order.side == "buy" and quantity * price < order.notional * (1 - _DUST)
        return replace(
            order,
            status="filled",
            reason="partial fill: cash available" if partial else "",
            filled_quantity=quantity,
            average_price=price,
            commission=fee,
            filled=day,
        )

    def _affordable(self) -> float:
        """The largest notional the cash pays, commission included (the commission
        is at most `costs.max_commission_rate` of the notional)."""
        if self._cash <= 0:
            return 0.0
        return self._cash / (1 + max_commission_rate(self._config))

    def _update_position(self, symbol: str, side: Side, quantity: float, price: float) -> None:
        held = self._positions.get(symbol)
        if side == "buy":
            if held is None:
                self._positions[symbol] = Position(symbol, quantity, price)
            else:
                total = held.quantity + quantity
                average = (held.quantity * held.average_price + quantity * price) / total
                self._positions[symbol] = Position(symbol, total, average)
            return
        assert held is not None
        remaining = held.quantity - quantity
        if remaining <= _DUST:
            del self._positions[symbol]
        else:
            self._positions[symbol] = Position(symbol, remaining, held.average_price)
