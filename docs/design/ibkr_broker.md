# Design proposal v2: `execution/ibkr_broker.py`

Status: **proposal, not approved** (2026-11-11). It replaces section 2 of
[`execution_modules.md`](execution_modules.md), which was written before the `Broker`
interface gained `find_order`, `is_fractionable`, `client_order_id` and `quantity`.
`ibkr_broker.py` is a human-reviewed module (CLAUDE.md rule 7): no code is written until
the owner approves this document and answers the questions in section 9.

API facts below were checked against `ib_async` 2.1.0 (`ib.py`, `order.py`,
`contract.py`, `objects.py`), the version `pip` resolves today for the `ibkr` extra.

---

## 1. Goal and scope

`IBKRBroker(Broker)` is the only path to real money (SPEC 8: `live` -> IBKR). It must:

- implement the current `Broker` interface exactly, so `orders.py`, the guard, the kill
  switch and reconciliation work unchanged;
- run against an **IBKR paper account first** (Phase 4) with the same code that later
  runs live (Phase 5);
- refuse to send anything when the connected account, port, mode or `LIVE_CONFIRM` do
  not all agree.

Out of scope: any risk logic (it stays in `risk/`), choosing order sizes (it stays in
`orders.py`), and changing `config.yaml` values.

## 2. Configuration and settings

New, machine-specific values in `.env` (not secrets: IB Gateway holds the login, so the
code never sees a username or password):

| Variable | Example | Use |
| --- | --- | --- |
| `IBKR_HOST` | `127.0.0.1` | already in `.env.example` |
| `IBKR_PORT` | `4002` | already in `.env.example` (default changes from TWS 7497 to Gateway 4002) |
| `IBKR_CLIENT_ID` | `1` | already in `.env.example`; each instance needs its own id |
| `IBKR_ACCOUNT` | `DU1234567` | **new**: the one account this instance may trade |

`Secrets` gains `ibkr_connection() -> IbkrConnection(host, port, client_id, account)`,
raising `MissingCredentialError` when a value is missing. The account id is never logged
or put in an error text.

`config.py`: `execution.broker` becomes `Literal["alpaca", "shadow", "ibkr"]`. No new
numeric parameter is added without the owner (see Q5). `run_daily.make_broker` returns
`IBKRBroker` when `broker == "ibkr"`; `alpaca` stays paper-only as today.

## 3. Safety gates (constructor, before any order)

The constructor connects, checks, and raises `CredentialError` (and disconnects) if any
gate fails. Nothing is sent before all gates pass.

1. **Mode.** `config.mode` must be `paper` or `live`. `backtest` never builds an
   `IBKRBroker`.
2. **Live confirmation.** For `live`, `LIVE_CONFIRM` must equal
   `I_UNDERSTAND_REAL_MONEY`. `load_config` already downgrades to paper without it; the
   broker checks again (defence in depth), reading it through `Secrets`, never echoing it.
3. **Account match.** `IB.managedAccounts()` must contain `IBKR_ACCOUNT`. Every order
   carries `order.account = IBKR_ACCOUNT`; positions and the account summary are read for
   that account only.
4. **Account type.** Paper accounts at IBKR start with `DU` (to confirm against the
   owner's paper account, Q1). `paper` requires a `DU` account; `live` requires a
   non-`DU` account. A paper config connected to a live Gateway is refused, and the
   reverse too.
5. **Port.** IB Gateway's default ports are 4002 (paper) and 4001 (live); TWS uses 7497
   and 7496. A known live port with `mode: paper`, or a known paper port with
   `mode: live`, is refused. This is a second check only: ports are user-configurable,
   so gate 4 is the primary one.
6. **Read-only smoke option.** `IBKRBroker(..., readonly=True)` passes `readonly=True` to
   `IB.connect`, so a first connection to the live account can verify gates 3-5 and read
   the account without any possibility of placing an order (used once, by the owner,
   before the first live run).

## 4. Method mapping

| `Broker` method | `ib_async` call | Notes |
| --- | --- | --- |
| `get_account()` | `accountSummary(account)` | `NetLiquidation` -> equity, `TotalCashValue` -> cash, `BuyingPower`, `MaintMarginReq`; USD rows only |
| `get_positions()` | `positions(account)` | `{symbol: Position(symbol, position, avgCost)}`; STK in USD only, anything else is reported as a reconciliation break, not hidden |
| `submit_order(...)` | `qualifyContracts(Stock(symbol, "SMART", "USD"))`, `placeOrder(contract, order)` | see section 5 |
| `get_order(order_id)` | `trades()` + `reqCompletedOrders(apiOnly=True)` | `order_id` is the IBKR `permId` as text: stable across sessions, unlike `orderId` |
| `find_order(client_order_id)` | `reqAllOpenOrders()` + `reqCompletedOrders(apiOnly=True)`, match `order.orderRef` | `None` when no order carries that ref |
| `is_fractionable(symbol)` | `reqContractDetails(contract)` | `True` when `sizeIncrement < 1`; cached per symbol |
| `cancel_all()` | `openTrades()` filtered by account and `orderRef` prefix `stride-`, then `cancelOrder` each | not `reqGlobalCancel`, which would also cancel the owner's manual orders (Q6) |
| `is_market_open(day)` | none | `data.calendar.is_trading_day(day)`: NYSE calendar, not the Gateway |

**Status mapping** (`OrderStatus` values in `ib_async`):

| IBKR | STRIDE |
| --- | --- |
| `Filled` | `filled` |
| `Cancelled`, `ApiCancelled` | `cancelled` |
| `Inactive`, `ValidationError` | `rejected` |
| everything else (`PendingSubmit`, `PreSubmitted`, `Submitted`, ...) | `pending` |

`reason` keeps the raw IBKR status. **Commission** is the sum of
`fill.commissionReport.commission` over the order's fills (from `reqExecutions`), so the
backtest's IBKR Tiered model (SPEC 9) can be checked against reality.

## 5. Orders

- **Contract:** `Stock(symbol, "SMART", "USD")`, qualified once and cached (SMART routing;
  a `primaryExchange` is added only if qualification is ambiguous).
- **Client id:** the caller's `client_order_id` goes in `order.orderRef` (a
  `stride-<uuid>` when none is given), so `find_order` can detect a resend.
- **Type and time in force:** market-on-open = `MarketOrder(action, qty, tif="OPG")`;
  limit = `LimitOrder(action, qty, round(limit_price, 2), tif="DAY")`.
  `outsideRth` stays `False`.
- **Notional to shares.** IBKR orders take a quantity. When `quantity` is given (whole
  shares, or a full exit) it is sent as is. Otherwise
  `qty = floor_to_increment(notional / reference_price, sizeIncrement)`:
  - the reference price is `limit_price` for a limit, and the previous close for
    market-on-open (owner's proposal in Q3 of `execution_modules.md`), read through an
    injected `last_close: Callable[[str], float]` backed by `data/store.py`, so the
    `Broker.submit_order` signature and `orders.py` stay unchanged;
  - buys are rounded **down**, so a buy never exceeds its notional at the reference price;
  - if `qty` falls below the contract's `minSize`, nothing is sent and an
    `OrderTooSmall(BrokerError)` is raised; `orders.py` already records a failed submit.
- `Order.cashQty` (cash-quantity orders) is **not** used: IBKR does not accept it for
  every stock order type via the API, and the floor-to-increment path is predictable.
- **Never retried.** Like `AlpacaBroker`, `submit_order` is sent once. On any failure it
  raises `BrokerError` and the caller must call `find_order` first (SPEC 8).

## 6. Connection, retries and errors

- `ib_async`'s synchronous API is used; `run_daily` is synchronous. One connection per
  phase (`with IBKRBroker(...) as broker:`), disconnected in `finally`.
- `IB.connect(host, port, clientId, timeout=...)`. Read calls (`get_*`, `find_order`,
  `cancel_all`) are retried up to 3 times with exponential backoff on `ConnectionError`,
  `TimeoutError` and a dropped connection (`not ib.isConnected()` -> reconnect once).
- Gateway down, or 2FA not approved: `BrokerUnavailable(BrokerError)`. The run sends
  nothing and the existing alert path fires.
- Error texts carry the call name and the exception type only: never the account id,
  host, or an order payload. Same rule as `alpaca_broker._error`.

## 7. Where it runs

The paper run today executes on GitHub Actions (`paper.yml`, `ubuntu-latest`). IB Gateway
needs a machine that is **always on**, logged in, and reachable on localhost, so IBKR cannot
run on GitHub-hosted runners. Options for the owner (Q7):

- **The Ubuntu server from `scripts/setup_server.sh`**, with IB Gateway and IBC
  (auto-login, daily restart). IBKR asks for 2FA on the IBKR Mobile app about once a week.
- The owner's Windows PC, with IB Gateway open; only works if the PC is on at 09:25 and
  16:05 New York time.

## 8. Tests

All without network or Gateway: an `IbClient` Protocol covers the `ib_async.IB` methods
used, and a fake records calls and returns canned accounts, positions, trades, contract
details and commission reports.

- Gates: refuse `live` without `LIVE_CONFIRM`; refuse a `DU` account in `live` and a
  non-`DU` account in `paper`; refuse an `IBKR_ACCOUNT` that is not managed; refuse a
  live port in paper; nothing is placed when a gate fails.
- Hypothesis invariants (owner's rule for code that moves money): for any notional,
  price, increment and min size, a buy's `qty * reference_price <= notional`; `qty` is a
  multiple of the increment; below `minSize` nothing is sent.
- Every order carries the configured account and the `orderRef`.
- `find_order` finds an order sent in an earlier session (via completed/open orders).
- OPG vs DAY mapping; status mapping table; commission summed over partial fills.
- `cancel_all` leaves orders without the `stride-` ref alone.
- No account id, host or key in any `BrokerError` text.
- A manual smoke test, `pytest -m ibkr_paper`, skipped in CI, runs only when the owner
  has IB Gateway paper running.

## 9. Questions for the owner

| # | Question | Proposal |
| --- | --- | --- |
| Q1 | Does your IBKR paper account id start with `DU`? | Gate 4 uses `DU` = paper; confirm with your account. |
| Q2 | Is fractional trading enabled on the IBKR account, and accepted via API with `OPG` and `LMT` for every ETF in the map? | Verify on IBKR paper in Phase 4; until then market-on-open, whole shares where `sizeIncrement` is 1. |
| Q3 | Reference price for market-on-open buys. | Previous close, buys rounded down (as already proposed). |
| Q4 | Order id exposed to STRIDE: `permId` or `orderId`? | `permId`: it survives reconnects and Gateway restarts. |
| Q5 | Do you want an extra live-only ceiling for the first live month (for example, `execution.live_max_order_usd`)? | Optional; it would be a new `config.yaml` parameter, so only if you ask for it. |
| Q6 | `cancel_all` cancels only STRIDE's orders, leaving your manual orders alone? | Yes. |
| Q7 | Where does IB Gateway run: the Ubuntu server or your PC? | The Ubuntu server with IBC. |
| Q8 | How long on IBKR paper before going live? | At least 20 trading days with fills, zero reconciliation breaks and costs within the backtest's estimate. |

## 10. Rollout (one PR each, owner reviews line by line)

1. **Settings and config:** `IBKR_ACCOUNT`, `Secrets.ibkr_connection()`,
   `execution.broker: ibkr`, `.env.example`. No behaviour change.
2. **`execution/ibkr_broker.py`** with the fake-client tests and hypothesis invariants
   above. Not wired in yet.
3. **Wiring:** `make_broker` returns `IBKRBroker` for `broker: ibkr`; a
   `config.paper_ibkr.yaml` instance with its own database.
4. **Phase 4, IBKR paper**, for the period in Q8. Paper run results are reviewed before
   going on.
5. **Phase 5, live: the owner's step only.** The owner deposits money at IBKR, runs the
   read-only smoke check (gate 6), then sets `mode: live` and `LIVE_CONFIRM` in their own
   `.env`. Agents never do this step (CLAUDE.md rule 6).

## 11. What the owner does (nobody else can)

- Open the IBKR Pro account, verify identity, deposit funds (SPEC 2: $100 plus $20 a month).
- Enable fractional shares and API access in the account settings; buy any market data
  subscription the Gateway requires.
- Install IB Gateway (and IBC) on the chosen machine, approve 2FA when asked.
- Answer Q1-Q8 and approve this document.
