"""Shadow broker (T23): a paper account simulated on the real stored prices.

Used by a paper instance with `execution.broker: shadow`, or by the DIP instance
when its own Alpaca paper keys (`ALPACA_DIP_*`) are not in `.env`. It is a
`SimBroker` that lives across runs: its cash, positions and orders are stored in
the instance's own database (`shadow_state`) after every call that changes them.

It fills like the backtest: an order sent on the morning of day d fills at the
open of d, with the backtest's spread and commission, once the bar of d is stored
(the evening download). So until that evening the order reads "pending", and
`cancel_all` leaves it alone: it is in flight for an open that already happened.
The daily job therefore records the shadow's fills at the evening run
(`settles_at_evening`). The starting cash is `cash_flows.initial_deposit_usd`.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import date

import pandas as pd

from stride.config import StrideConfig
from stride.data import store
from stride.data.calendar import previous_trading_day, trading_days
from stride.data.update import series_for
from stride.execution.broker import Account, Broker, OrderState, OrderType, Position, Side
from stride.execution.sim_broker import SimBroker

OHLC = ["open", "high", "low", "close"]


class ShadowBroker(Broker):
    settles_at_evening = True  # fills are known only once the day's bar is stored

    def __init__(self, config: StrideConfig, conn: sqlite3.Connection, symbols: list[str]) -> None:
        """`symbols`: every instrument the instance may trade (bars from `conn`)."""
        if config.mode != "paper":
            raise ValueError("the shadow broker runs paper instances only")
        self._conn = conn
        prices = {}
        for symbol in symbols:
            bars = series_for(conn, symbol)
            if not bars.empty:
                prices[symbol] = bars[OHLC]
        if not prices:
            raise ValueError("the shadow broker needs stored prices")
        self._anchor = symbols[0]
        self._prices = prices
        last = self._last_bar()
        self._sim = SimBroker(
            prices, config, config.cash_flows.initial_deposit_usd, previous_trading_day(last)
        )
        stored = store.load_shadow_state(conn)
        if stored is not None:
            self._sim.restore(json.loads(stored))
        self._advance()

    def _last_bar(self) -> date:
        frame = self._prices.get(self._anchor)
        if frame is None or frame.empty:
            frame = next(iter(self._prices.values()))
        return pd.DatetimeIndex(frame.index).max().date()

    def _save(self) -> None:
        store.save_shadow_state(self._conn, json.dumps(self._sim.snapshot()))

    def _advance(self) -> None:
        """Step the simulation to every stored session after its clock: orders
        fill at the open of the session after the one they were sent in."""
        last = self._last_bar()
        if last <= self._sim.today:
            return
        for day in trading_days(self._sim.today, last):
            if day.date() > self._sim.today:
                self._sim.step(day.date())
        self._save()

    # Broker interface.

    def get_account(self) -> Account:
        return self._sim.get_account()

    def get_positions(self) -> dict[str, Position]:
        return self._sim.get_positions()

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
        order_id = self._sim.submit_order(
            symbol, side, notional, type, limit_price,
            client_order_id=client_order_id, quantity=quantity,
        )  # fmt: skip
        self._save()
        return f"shadow-{order_id}"

    def get_order(self, order_id: str) -> OrderState:
        state = self._sim.get_order(order_id.removeprefix("shadow-"))
        return _shadow(state)

    def find_order(self, client_order_id: str) -> OrderState | None:
        state = self._sim.find_order(client_order_id)
        return _shadow(state) if state is not None else None

    def is_fractionable(self, symbol: str) -> bool:
        return True

    def cancel_all(self) -> int:
        """Orders still pending are in flight for a session whose bar is not
        stored yet (the simulation already cancelled the rest): none to cancel."""
        return 0

    def is_market_open(self, day: date) -> bool:
        return self._sim.is_market_open(day)

    def deposit(self, amount: float) -> None:
        """A simulated deposit (the owner's monthly $20 in a shadow account)."""
        self._sim.deposit(amount)
        self._save()


def _shadow(state: OrderState) -> OrderState:
    """The simulation's order with the id the shadow broker gave it."""
    return replace(state, order_id=f"shadow-{state.order_id}")
