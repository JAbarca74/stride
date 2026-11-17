"""Interactive Brokers broker (SPEC section 8; design docs/design/ibkr_broker.md).

The only path to real money. Human-reviewed module (CLAUDE.md rule 7): written on
the owner's go-ahead of 2026-11-17, with the answers to Q1-Q8 of the design doc.

It talks to an IB Gateway (or TWS) through `ib_async`'s synchronous API. IB Gateway
holds the login, so this code never sees a username or password. Before anything is
sent, the constructor connects and checks six gates, and raises `CredentialError`
(after disconnecting) if any fails:

1. mode is `paper` or `live` (never `backtest`);
2. `live` needs `LIVE_CONFIRM` (checked again here, defence in depth);
3. the configured `IBKR_ACCOUNT` is one of the Gateway's managed accounts;
4. paper needs a paper account (id starting `DU`, Q1) and live a non-`DU` one;
5. a known live port in paper, or a known paper port in live, is refused;
6. `readonly=True` connects read-only, for the owner's first live smoke check.

Orders: market orders are market-on-open (`tif="OPG"`), limits are day orders. IBKR
takes quantities, so a notional becomes `floor_to_increment(notional / reference)`,
with the previous close as the reference of a market order (Q3) and the limit of a
limit order; a buy therefore never exceeds its notional at the reference. The order
id STRIDE sees is IBKR's `permId` (Q4). Every order carries the configured account
and `orderRef = client_order_id` (`stride-` prefix), so `find_order` finds it in a
later session and `cancel_all` cancels STRIDE's orders only, never the owner's
manual ones (Q6). `submit_order` is never retried; read calls are retried up to 3
times. Error texts name the call and the exception type only: never the account id,
the host or an order payload.
"""

from __future__ import annotations

import logging
import math
import os
import time
import uuid
from collections.abc import Callable, Iterable
from datetime import date
from typing import Any, TypeVar

from ib_async import LimitOrder, MarketOrder, Order, Stock

from stride.config import LIVE_CONFIRM_ENV, LIVE_CONFIRM_VALUE, StrideConfig
from stride.data.calendar import is_trading_day
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
from stride.settings import CredentialError, IbkrConnection, Secrets

logger = logging.getLogger(__name__)

PAPER_ACCOUNT_PREFIX = "DU"  # IBKR paper accounts (Q1)
LIVE_PORTS = frozenset({4001, 7496})  # IB Gateway / TWS defaults
PAPER_PORTS = frozenset({4002, 7497})
ORDER_REF_PREFIX = "stride-"
CONNECT_TIMEOUT_S = 15.0
READ_ATTEMPTS = 3
BACKOFF_S = 1.0
UNSET = 1e300  # ib_async's UNSET_DOUBLE is the largest float

_FILLED = frozenset({"Filled"})
_CANCELLED = frozenset({"Cancelled", "ApiCancelled"})
_REJECTED = frozenset({"Inactive", "ValidationError"})

T = TypeVar("T")


class BrokerUnavailable(BrokerError):
    """IB Gateway is down, unreachable, or waiting for 2FA: nothing was sent."""


class OrderTooSmall(BrokerError):
    """The order rounds below the contract's minimum size: nothing was sent."""


# The client is an `ib_async.IB` (or a fake with the same methods in tests): connect,
# disconnect, isConnected, managedAccounts, accountSummary, positions,
# qualifyContracts, reqContractDetails, placeOrder, trades, openTrades,
# reqAllOpenOrders, reqCompletedOrders and cancelOrder.
IbClient = Any


def _error(call: str, exc: BaseException) -> BrokerError:
    """The call and the exception type only: IBKR texts can echo the account."""
    return BrokerError(f"ibkr {call} failed: {type(exc).__name__}")


def floor_to_increment(quantity: float, increment: float) -> float:
    """The largest multiple of `increment` not above `quantity` (0 when negative)."""
    if not (increment > 0 and math.isfinite(increment)):
        raise ValueError("the size increment must be a positive number")
    if not (quantity > 0 and math.isfinite(quantity)):
        return 0.0
    steps = math.floor(quantity / increment + 1e-9)
    return round(steps * increment, 10)


def order_quantity(notional: float, reference: float, increment: float, min_size: float) -> float:
    """Shares for a notional at a reference price, floored to the increment; 0 when
    that is below `min_size` (the caller then sends nothing)."""
    if not (reference > 0 and math.isfinite(reference)):
        raise ValueError("the reference price must be a positive number")
    quantity = floor_to_increment(notional / reference, increment)
    return quantity if quantity >= max(min_size, increment) - 1e-12 else 0.0


def map_status(ib_status: str) -> OrderStatus:
    """IBKR order status -> STRIDE (design doc section 4)."""
    if ib_status in _FILLED:
        return "filled"
    if ib_status in _CANCELLED:
        return "cancelled"
    if ib_status in _REJECTED:
        return "rejected"
    return "pending"


def check_gates(config: StrideConfig, connection: IbkrConnection, readonly: bool) -> None:
    """Gates 1, 2, 4 (by the configured account) and 5, before connecting."""
    del readonly  # read-only connections pass the same gates
    if config.mode not in {"paper", "live"}:
        raise CredentialError(f"no IBKR broker in mode {config.mode!r}")
    paper_account = connection.account.startswith(PAPER_ACCOUNT_PREFIX)
    if config.mode == "live":
        if os.environ.get(LIVE_CONFIRM_ENV) != LIVE_CONFIRM_VALUE:
            raise CredentialError(f"mode live needs {LIVE_CONFIRM_ENV}; refusing")
        if paper_account:
            raise CredentialError("mode live with a paper account; refusing")
        if connection.port in PAPER_PORTS:
            raise CredentialError("mode live on a paper port; refusing")
    else:
        if not paper_account:
            raise CredentialError("mode paper with a live account; refusing")
        if connection.port in LIVE_PORTS:
            raise CredentialError("mode paper on a live port; refusing")


class IBKRBroker(Broker):
    def __init__(
        self,
        config: StrideConfig,
        secrets: Secrets,
        last_close: Callable[[str], float],
        *,
        client: IbClient | None = None,
        readonly: bool = False,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Connect and check every gate; raises `CredentialError` (after
        disconnecting) when one fails and `BrokerUnavailable` when the Gateway
        cannot be reached. `last_close(symbol)` is the previous close, the
        reference price of a market-on-open order."""
        connection = secrets.ibkr_connection()
        check_gates(config, connection, readonly)
        self._config = config
        self._account = connection.account
        self._connection = connection
        self._readonly = readonly
        self._last_close = last_close
        self._sleep = sleep
        self._contracts: dict[str, Any] = {}
        self._sizes: dict[str, tuple[float, float]] = {}  # symbol -> (increment, min size)
        if client is None:
            from ib_async import IB  # noqa: PLC0415 - only with a real Gateway

            client = IB()
        self._ib: IbClient = client
        self._connect()
        try:
            if self._account not in self._ib.managedAccounts():
                raise CredentialError("IBKR_ACCOUNT is not managed by this Gateway; refusing")
        except CredentialError:
            self.close()
            raise
        except Exception as exc:
            self.close()
            raise _error("managedAccounts", exc) from None

    # Connection.

    def _connect(self) -> None:
        c = self._connection
        try:
            self._ib.connect(
                c.host, c.port, clientId=c.client_id, timeout=CONNECT_TIMEOUT_S,
                readonly=self._readonly, account=c.account,
            )  # fmt: skip
        except Exception as exc:
            raise BrokerUnavailable(f"ibkr connect failed: {type(exc).__name__}") from None
        if not self._ib.isConnected():
            raise BrokerUnavailable("ibkr connect failed: not connected")

    def close(self) -> None:
        """Disconnect (always safe to call)."""
        try:
            self._ib.disconnect()
        except Exception as exc:
            logger.warning("ibkr disconnect failed: %s", type(exc).__name__)

    def __enter__(self) -> IBKRBroker:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _read(self, call: str, fn: Callable[[], T]) -> T:
        """A read call, retried with backoff on a dropped connection."""
        for attempt in range(READ_ATTEMPTS):
            try:
                if not self._ib.isConnected():
                    self._connect()
                return fn()
            except BrokerUnavailable:
                if attempt == READ_ATTEMPTS - 1:
                    raise
            except (ConnectionError, TimeoutError, OSError) as exc:
                if attempt == READ_ATTEMPTS - 1:
                    raise _error(call, exc) from None
            except Exception as exc:
                raise _error(call, exc) from None
            self._sleep(BACKOFF_S * 2**attempt)
        raise BrokerError(f"ibkr {call} failed")  # pragma: no cover - loop always returns

    # Broker interface.

    def get_account(self) -> Account:
        rows = self._read("accountSummary", lambda: self._ib.accountSummary(self._account))
        values: dict[str, float] = {}
        for row in rows:
            if row.account != self._account or row.currency not in {"USD", "BASE"}:
                continue
            try:
                values[row.tag] = float(row.value)
            except (TypeError, ValueError):
                continue
        missing = [t for t in ("NetLiquidation", "TotalCashValue") if t not in values]
        if missing:
            raise BrokerError(f"ibkr accountSummary failed: missing {missing}")
        return Account(
            equity=values["NetLiquidation"],
            cash=values["TotalCashValue"],
            buying_power=values.get("BuyingPower", values["TotalCashValue"]),
            margin_requirement=values.get("MaintMarginReq", 0.0),
        )

    def get_positions(self) -> dict[str, Position]:
        """Long USD stock positions of the account. Anything else (another currency,
        a non-stock contract) is reported under its own key so reconciliation sees
        it as a break instead of hiding it."""
        rows = self._read("positions", lambda: self._ib.positions(self._account))
        out: dict[str, Position] = {}
        for row in rows:
            if row.account != self._account or not row.position:
                continue
            contract = row.contract
            symbol = contract.symbol
            if contract.secType != "STK" or contract.currency != "USD":
                symbol = f"{contract.symbol}:{contract.secType}:{contract.currency}"
            out[symbol] = Position(symbol, float(row.position), float(row.avgCost))
        return out

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
        """Send one order, never retried. Returns the IBKR `permId` as text."""
        if self._readonly:
            raise BrokerError("ibkr submit_order refused: read-only connection")
        if side not in {"buy", "sell"}:
            raise ValueError("side must be buy or sell")
        if type == "limit" and (limit_price is None or not limit_price > 0):
            raise ValueError("a limit order needs a positive limit_price")
        ref = client_order_id or f"{ORDER_REF_PREFIX}{uuid.uuid4()}"
        contract = self._contract(symbol)
        increment, min_size = self._size(symbol)
        if quantity is not None:
            shares = float(quantity)
        else:
            reference = float(limit_price) if type == "limit" and limit_price else 0.0
            if type == "market":
                reference = float(self._last_close(symbol))
            shares = order_quantity(notional, reference, increment, min_size)
        if not shares > 0:
            raise OrderTooSmall(f"ibkr submit_order: {symbol} rounds below the minimum size")
        action = "BUY" if side == "buy" else "SELL"
        order: Order
        if type == "limit":
            assert limit_price is not None
            order = LimitOrder(action, shares, round(limit_price, 2), tif="DAY")
        else:
            order = MarketOrder(action, shares, tif="OPG")
        order.account = self._account
        order.orderRef = ref
        order.outsideRth = False
        try:
            trade = self._ib.placeOrder(contract, order)
        except Exception as exc:
            raise _error("placeOrder", exc) from None
        perm_id = getattr(trade.order, "permId", 0) or getattr(trade.orderStatus, "permId", 0)
        if not perm_id:  # not acknowledged yet: the caller looks it up by its ref
            raise BrokerError("ibkr placeOrder failed: no permId yet; use find_order")
        return str(perm_id)

    def get_order(self, order_id: str) -> OrderState:
        for trade in self._all_trades():
            if str(trade.order.permId) == order_id:
                return self._state(trade)
        raise BrokerError("ibkr get_order failed: unknown order id")

    def find_order(self, client_order_id: str) -> OrderState | None:
        def fetch() -> list[Any]:
            return [*self._ib.reqAllOpenOrders(), *self._ib.reqCompletedOrders(apiOnly=True)]

        found = [
            t
            for t in [*self._read("find_order", fetch), *self._all_trades()]
            if t.order.orderRef == client_order_id and t.order.account == self._account
        ]
        return self._state(found[0]) if found else None

    def is_fractionable(self, symbol: str) -> bool:
        increment, _ = self._size(symbol)
        return increment < 1

    def cancel_all(self) -> int:
        """Cancel this account's open STRIDE orders only (orderRef `stride-...`)."""
        if self._readonly:
            return 0
        open_trades = self._read("openTrades", self._ib.openTrades)
        cancelled = 0
        for trade in open_trades:
            order = trade.order
            if order.account != self._account:
                continue
            if not str(order.orderRef or "").startswith(ORDER_REF_PREFIX):
                continue
            try:
                self._ib.cancelOrder(order)
            except Exception as exc:
                raise _error("cancelOrder", exc) from None
            cancelled += 1
        return cancelled

    def is_market_open(self, day: date) -> bool:
        return is_trading_day(day)

    # Internals.

    def _all_trades(self) -> list[Any]:
        return [
            t for t in self._read("trades", self._ib.trades) if t.order.account == self._account
        ]

    def _contract(self, symbol: str) -> Any:
        if symbol not in self._contracts:
            qualified = self._read(
                "qualifyContracts", lambda: self._ib.qualifyContracts(Stock(symbol, "SMART", "USD"))
            )
            if not qualified:
                raise BrokerError(f"ibkr qualifyContracts failed: {symbol} not found")
            self._contracts[symbol] = qualified[0]
        return self._contracts[symbol]

    def _size(self, symbol: str) -> tuple[float, float]:
        """(size increment, minimum size) from the contract details, cached."""
        if symbol not in self._sizes:
            contract = self._contract(symbol)
            details = self._read(
                "reqContractDetails", lambda: self._ib.reqContractDetails(contract)
            )
            increment, min_size = 1.0, 1.0
            if details:
                increment = _positive(details[0].sizeIncrement, 1.0)
                min_size = _positive(details[0].minSize, increment)
            self._sizes[symbol] = (increment, min_size)
        return self._sizes[symbol]

    def _state(self, trade: Any) -> OrderState:
        order, status = trade.order, trade.orderStatus
        side: Side = "buy" if order.action == "BUY" else "sell"
        kind: OrderType = "limit" if order.orderType == "LMT" else "market"
        limit = float(order.lmtPrice) if kind == "limit" else None
        price = float(status.avgFillPrice) if status.filled else None
        times = [f.time for f in trade.fills if getattr(f, "time", None) is not None]
        filled_day = max(times).date() if times else None
        submitted = min(e.time for e in trade.log).date() if trade.log else date.today()
        return OrderState(
            order_id=str(order.permId),
            symbol=trade.contract.symbol,
            side=side,
            notional=float(order.totalQuantity) * (price or limit or 0.0),
            type=kind,
            limit_price=limit,
            submitted=submitted,
            status=map_status(status.status),
            filled_quantity=float(status.filled or 0.0),
            average_price=price,
            commission=commission_of(trade.fills),
            filled=filled_day,
            reason=str(status.status),
            client_order_id=order.orderRef or None,
        )


def commission_of(fills: Iterable[Any]) -> float:
    """Sum of the commission reports of an order's fills (partial fills included)."""
    total = 0.0
    for fill in fills:
        report = getattr(fill, "commissionReport", None)
        value = getattr(report, "commission", None)
        # ib_async marks an unset value with the largest float: skip it.
        if value is not None and math.isfinite(float(value)) and abs(float(value)) < UNSET:
            total += float(value)
    return total


def _positive(value: object, default: float) -> float:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return number if number > 0 and math.isfinite(number) else default
