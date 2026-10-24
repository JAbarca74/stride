# Design proposal: `execution/orders.py` and `execution/ibkr_broker.py`

Status: **proposal, no code**. Both modules are on the "do not assign" list (SPEC section 13). Nothing here is implemented until the owner approves this document and gives a separate go-ahead for each module; each then ships in its own PR, reviewed line by line.

References: SPEC 1.4 sections 7 (steps 3 and 4) and 8; `risk/guard.py`, `risk/kill_switch.py`, `execution/kill_switch_file.py` and `execution/broker.py` as merged on `main`.

## Principles

- **Planning is pure, sending is not.** `orders.py` has a pure planner (positions and targets in, proposed orders out) and a thin submitter that is the only code that calls `Broker.submit_order`. The planner is tested with Hypothesis like the risk modules; the submitter is tested with `SimBroker` and fakes.
- **One door to the broker.** No other module submits orders. The submitter refuses to send an order that has no approved `GuardDecision`.
- **Fail safe.** Any doubt (unknown order state, broker error, missing price) means *do not send*, log an event and alert. Never resend an order whose fate is unknown.
- **Same code for every mode.** `orders.py` only sees the `Broker` interface. `IBKRBroker` is one more implementation, selected by `mode`.

---

## 1. `execution/orders.py`

### 1a. Proposed API

```python
@dataclass(frozen=True)
class Target:
    symbol: str  # instrument chosen by risk/leverage.py (SPY, SSO, ...)
    target_usd: float  # >= 0, long only
    switch: bool = False  # part of an instrument switch (leverage.InstrumentChoice)
    rule: str = "HOLD"  # SPEC 6 rule that produced it, for the orders table


@dataclass(frozen=True)
class PlannedOrder:
    symbol: str
    side: Side
    notional: float
    type: OrderType  # "limit" or "market" (market-on-open)
    limit_price: float | None
    switch: bool
    rule: str


def plan_orders(
    targets: Sequence[Target],
    positions_usd: Mapping[str, float],  # broker positions x close of t
    equity: float,
    prev_close: Mapping[str, float],
    config: StrideConfig,
    limits_allowed: Callable[[str], bool],  # broker accepts limits for this symbol
) -> list[PlannedOrder]:
    """Pure. SPEC 8 order flow steps 1-4."""


def to_proposed(order: PlannedOrder) -> guard.ProposedOrder: ...


def liquidation_orders(positions_usd: Mapping[str, float]) -> list[PlannedOrder]:
    """Pure. Kill switch: one market sell per held symbol, full value (SPEC 7, step 4)."""


@dataclass(frozen=True)
class Submission:
    order: PlannedOrder
    order_id: str | None  # None when vetoed or not sent
    reasons: tuple[str, ...]  # guard vetoes or the broker error, by name


def submit_approved(
    broker: Broker,
    planned: Sequence[PlannedOrder],
    decisions: Sequence[guard.GuardDecision],
    conn: sqlite3.Connection,
) -> list[Submission]:
    """Not pure. Sends only approved orders, sells first, records each one in `orders`."""
```

### 1b. Planning rules (pure)

1. For every symbol in targets or positions: `diff = target_usd - positions_usd`. A symbol held but absent from the targets has target 0 (full sell).
2. **Rebalance band** (`execution.rebalance_band`, `execution.min_order_usd`): no order if `|diff| < band x target_usd` or `|diff| < min_order_usd`. Exceptions: a full exit (target 0) and both legs of a switch always pass the band, otherwise an exit could be skipped forever (see question Q1).
3. **Sells before buys**; inside each side, larger notional first (deterministic order for tests).
4. **Price:** limit at `prev_close x (1 + limit_offset)` for buys and `(1 - limit_offset)` for sells, rounded to the cent away from crossing; market-on-open where `limits_allowed(symbol)` is false (SPEC 8, step 4).
5. A sell never exceeds the held value; the 1-cent tolerance is the guard's (`SHORT_TOLERANCE_USD`), not the planner's.

Invariants for Hypothesis: no short ever planned; every sell precedes every buy; applying all planned orders lands each symbol within the band of its target; totals are finite and non-negative; planning is idempotent once positions equal targets (empty list).

### 1c. Integration with the guard and the kill switch

The daily run (future `run_daily.py`, T17) calls, in this order:

```
decision = kill_switch.kill_switch_decision(...)          # pure
if decision.fire:  kill_switch_file.fire(conn, decision)  # writes KILL_SWITCH
if decision.halt:  kill_switch_file.halt(conn, decision)  # writes HALT

ctx = GuardContext(..., kill_switch_active=is_active(paths.kill_switch),
                        halted=is_halted(paths.halt), blocked=..., data_ok=...)

if ctx.kill_switch_active:
    broker.cancel_all()
    planned   = orders.liquidation_orders(broker positions x close)
    decisions = [guard.check_liquidation(to_proposed(p), ctx) for p in planned]
else:
    planned   = orders.plan_orders(targets, ...)
    decisions = guard.check_orders(map(to_proposed, planned), ctx, config)

orders.submit_approved(broker, planned, decisions, conn)
```

- The planner never decides whether to trade; the guard does. While `HALT` exists `check_orders` approves nothing, so the submitter sends nothing, but the kill-switch path above still runs (SPEC 1.4: the kill switch stays armed during a halt).
- Liquidation uses **the positions the broker reports**, never the system's expectation.
- `submit_approved` re-checks `is_active` / `is_halted` immediately before each send (a file written by another process between planning and 09:25 must still stop the run). With the kill switch active it only sends orders whose decision came from `check_liquidation`.
- Every vetoed order is recorded with its guard reasons and alerts through `reporting/alerts.py` (SPEC 10).

### 1d. Submission, retries and duplicates

- Each planned order gets a **client order id** derived from `(run date, symbol, side, sequence)` and stored in `orders` *before* the call. On a timeout or error the submitter looks the id up at the broker before any retry (SPEC 8: never resend an unconfirmed order). This needs a `find_order(client_id)` method on `Broker` (question Q4).
- Broker calls retry with exponential backoff, at most 3, like `data/sources.py`.
- 12:00 check: `get_order` per id; 16:05: `cancel_all`, then reconciliation (`kill_switch.reconciliation_breaks`) and fills into `fills`.

### 1e. New tables (SPEC 10)

`orders` (run date, client id, broker id, symbol, side, notional, type, limit price, rule, switch, guard reasons, status) and `fills` (order id, price, quantity, commission, slippage vs previous close). Added in `data/store.py` in the same PR as `orders.py`.

---

## 2. `execution/ibkr_broker.py`

### 2a. Proposed API

`IBKRBroker(Broker)` over `ib_async` (already the `ibkr` optional extra in `pyproject.toml`), talking to IB Gateway (SPEC 15).

```python
class IBKRBroker(Broker):
    def __init__(
        self,
        client: IbClient,            # Protocol over ib_async.IB, injected; fakes in tests
        account_id: str,
        mode: Literal["paper", "live"],
        live_confirm: str | None,    # from settings, never logged
        calendar: Callable[[date], bool] = calendar.is_trading_day,
    ) -> None: ...
    def get_account(self) -> Account          # NetLiquidation, TotalCashValue, BuyingPower, MaintMarginReq
    def get_positions(self) -> dict[str, Position]
    def submit_order(symbol, side, notional, type="market", limit_price=None) -> str
    def get_order(order_id) -> OrderState     # status, filled qty, avg price, commission
    def cancel_all() -> int
    def is_market_open(day) -> bool           # NYSE calendar, not the gateway
```

### 2b. Safety rules built into the class

- **Paper by default.** The constructor refuses to start unless the connected account matches the mode: IBKR paper account ids start with `DU` (to confirm with the owner's paper account); a live connection requires `mode == "live"` **and** `live_confirm == "I_UNDERSTAND_REAL_MONEY"` (SPEC 8, 11). Otherwise it raises before any request. Agents only ever run it against paper.
- **Port check:** IB Gateway uses different ports for paper and live; the port is read from settings, and a paper config pointing at the live port is refused (question Q6).
- Credentials never enter the code: IB Gateway holds the login (IBC, SPEC 15). The class only needs host, port, client id and account id.
- **Notional orders:** IBKR's API takes quantities, not dollars. `submit_order` converts `notional / reference price` to a fractional quantity (rounded down to the instrument's increment) and records both. The reference price is the limit price, or the previous close for market-on-open (question Q3).
- Market-on-open is `MKT` with `tif="OPG"`; limit is `LMT` with `tif="DAY"`. Whether IBKR accepts fractional `OPG` or `LMT` orders through the API is unverified and decides `limits_allowed` (question Q2).
- Commission comes from IBKR's commission reports, so the backtest's Fixed-pricing model can be checked against reality (SPEC 9).
- Every call has a timeout; a disconnected gateway (2FA not approved, SPEC 15) raises a `BrokerUnavailable` error, the run sends nothing and alerts.

### 2c. Integration with the guard and the kill switch

`IBKRBroker` has no risk logic of its own: it is reached only through `orders.submit_approved`. Two broker-side extras feed the guard and the kill switch:

- `get_account().margin_requirement` and excess liquidity feed `GuardContext.excess_liquidity` / `margin_requirement` (the 25% buffer only matters when `allow_margin` is true, i.e. the paper Kelly account).
- `get_positions()` is the source of truth for reconciliation and for the kill-switch liquidation.

### 2d. Tests

All without network or gateway: a fake `IbClient` records calls and returns canned accounts, positions, trades and commission reports. Tests cover: refusal on live account without `LIVE_CONFIRM`; refusal on paper/live port mismatch; notional-to-quantity rounding never exceeds the notional; OPG vs DAY mapping; status mapping; disconnect handling; no credential or account id in any error message. A manual paper smoke test (`pytest -m ibkr_paper`, skipped in CI) runs only when the owner has IB Gateway paper running.

---

## Open questions for the owner

| # | Question | Proposal |
| --- | --- | --- |
| Q1 | Does the 20% band apply to full exits (target 0) and to switch legs? Applied literally, a small leftover position could never be sold. | Exits and switch legs always pass the band; the $1 minimum still applies. |
| Q2 | IBKR: are fractional market-on-open and fractional limit orders accepted via API for every ETF in the map? | Verify in Phase 4 with the paper account; until then `limits_allowed` is per symbol in config, default market-on-open. |
| Q3 | Reference price for converting notional to shares on market-on-open orders. | Previous close; buys rounded down so cash is never exceeded. |
| Q4 | Add `find_order(client_order_id)` to the `Broker` interface (and to `SimBroker`/`AlpacaBroker`) to honor "never resend an unconfirmed order". | Yes, as a small separate PR before `orders.py`. |
| Q5 | When the kill switch fires at 18:00, the liquidation is sent at 09:25 next day. Is a fresh `get_positions()` at 09:25 required (positions may differ after a corporate action)? | Yes: recompute the liquidation list from broker positions at 09:25. |
| Q6 | Where do the IB Gateway host/port/client id/account id live? | `.env` (not secrets, but machine-specific), validated by `settings.py`; paper and live ports configured separately. |
| Q7 | Unfilled limit orders are cancelled at 16:05 and "re-evaluated next day". Should a cancelled *exit* be resent as market-on-open next day instead of a new limit? | Yes for exits (target 0) and kill-switch sells; entries and pyramids stay limits. |
| Q8 | Does `orders.py` own the `orders`/`fills` tables, or does `data/store.py`? | `data/store.py`, like the other tables; `orders.py` only calls it. |

## Owner decisions (2026-10-24)

The owner approved the creation of `execution/orders.py` following section 1 of this document, with these answers to the open questions:

| # | Decision |
| --- | --- |
| Q1 | Full exits (target 0) and both legs of an instrument switch always pass the rebalance band; the $1 minimum still applies. |
| Q2 | IBKR only; out of scope for the Alpaca paper work. |
| Q3 | IBKR only; out of scope for the Alpaca paper work. |
| Q4 | Yes: add `find_order(client_order_id)` to the `Broker` interface, `SimBroker` and `AlpacaBroker`, in its own PR before `orders.py`. |
| Q5 | Yes: a kill-switch liquidation is recomputed from the broker's positions at the morning phase. |
| Q6 | IBKR only; out of scope for the Alpaca paper work. |
| Q7 | Yes: an unfilled exit or kill-switch sell is resent the next day as a market order; entries and adds are re-planned normally. |
| Q8 | The `orders`, `fills` and `runs` tables live in `data/store.py`. |

`execution/ibkr_broker.py` (section 2) is not approved and stays a proposal.
