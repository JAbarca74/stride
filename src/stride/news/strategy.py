"""Scores, decisions and the simulated account of `paper_news` (design section 4). Pure.

One session d at a time, the same code for the backtest and the paper account:

1. scores from events published in the window [cutoff of session d-5, cutoff of d),
   where the cutoff is 09:30 New York (news after it decides the next session);
2. at the open of d: sells first (score, holding time, stop), then buys (score, slots,
   market brake), each filled at the open with spread and IBKR Tiered commission;
3. at the close of d: equity, and the kill switch for the next open.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pandas as pd

from stride.data.calendar import previous_trading_day
from stride.news.config import NewsConfig

NEW_YORK = ZoneInfo("America/New_York")
SELL_SCORE, SELL_TIME, SELL_STOP, SELL_KILL = "SCORE", "TIME", "STOP", "KILL_SWITCH"
BUY_SCORE = "SCORE"


def cutoff(day: date, config: NewsConfig) -> pd.Timestamp:
    """The moment news stops counting for session `day` (09:30 New York, in UTC)."""
    hour, minute = (int(x) for x in config.decision_cutoff.split(":"))
    local = datetime.combine(day, time(hour, minute), tzinfo=NEW_YORK)
    return pd.Timestamp(local).tz_convert("UTC")


def scores(
    events: pd.DataFrame, sessions: Sequence[date], index: int, config: NewsConfig
) -> dict[str, float]:
    """Score per symbol for session `sessions[index]`: event points published in
    [cutoff of the session `score_window_sessions` before, cutoff of this one).
    `events` has columns `ts` (UTC), `symbol`, `points`."""
    end = cutoff(sessions[index], config)
    # The NYSE session `score_window_sessions` before, from the calendar, so the window is
    # also exact during the first sessions (when `sessions` does not reach that far back).
    earliest = sessions[index]
    for _ in range(config.score_window_sessions):
        earliest = previous_trading_day(earliest)
    start = cutoff(earliest, config)
    if events.empty:
        return {}
    window = events[(events["ts"] < end) & (events["ts"] >= start)]
    return {str(s): float(v) for s, v in window.groupby("symbol")["points"].sum().items()}


def commission(shares: float, price: float, config: NewsConfig) -> float:
    """IBKR Pro Tiered, as the other accounts: per share with a minimum and a 1% cap,
    plus third-party fees per share."""
    if shares <= 0 or price <= 0:
        return 0.0
    base = max(config.tiered_per_share * shares, config.tiered_min_per_order)
    capped = min(base, config.tiered_max_pct_of_value * shares * price)
    return capped + config.tiered_third_party_per_share * shares


@dataclass
class Holding:
    shares: float
    entry: float  # fill price of the buy
    bought: int  # session index of the buy
    mark: float = math.nan  # last known close; a day without a close keeps it


@dataclass(frozen=True)
class Trade:
    day: date
    symbol: str
    side: str
    shares: float
    price: float
    commission: float
    spread_cost: float
    reason: str
    score: float


@dataclass
class Book:
    cash: float
    holdings: dict[str, Holding] = field(default_factory=dict)
    peak: float = 0.0
    stopped: bool = False
    kill_pending: bool = False

    def equity(self, prices: Mapping[str, float]) -> float:
        return self.cash + sum(
            h.shares * prices.get(s, h.mark if math.isfinite(h.mark) else h.entry)
            for s, h in self.holdings.items()
        )


def _fill(price: float, side: str, config: NewsConfig) -> float:
    move = config.spread_bps / 10_000
    return price * (1 + move) if side == "buy" else price * (1 - move)


@dataclass(frozen=True)
class SessionInputs:
    """What the open of one session sees: its date and index, today's opens, the
    previous closes, the scores and whether the market brake is on."""

    day: date
    index: int
    opens: Mapping[str, float]
    previous_closes: Mapping[str, float]
    scores: Mapping[str, float]
    brake_on: bool


def _sell(book: Book, s: SessionInputs, symbol: str, reason: str, config: NewsConfig) -> Trade:
    held = book.holdings.pop(symbol)
    open_ = s.opens[symbol]
    price = _fill(open_, "sell", config)
    fee = commission(held.shares, price, config)
    book.cash += held.shares * price - fee
    return Trade(s.day, symbol, "sell", held.shares, price, fee,
                 held.shares * (open_ - price), reason, s.scores.get(symbol, 0.0))  # fmt: skip


def _exit_reason(book: Book, s: SessionInputs, symbol: str, config: NewsConfig) -> str | None:
    held, score = book.holdings[symbol], s.scores.get(symbol, 0.0)
    prev = s.previous_closes.get(symbol, math.nan)
    if score <= config.sell_score:
        return SELL_SCORE
    if s.index - held.bought >= config.max_hold_sessions:
        return SELL_TIME
    if math.isfinite(prev) and prev <= held.entry * (1 - config.stop_loss):
        return SELL_STOP
    return None


def _buy(
    book: Book, s: SessionInputs, symbol: str, equity: float, config: NewsConfig
) -> Trade | None:
    price = _fill(s.opens[symbol], "buy", config)
    budget = min(config.position_weight * equity, book.cash)
    shares = budget / (price * (1 + config.tiered_max_pct_of_value)) if budget > 0 else 0.0
    fee = commission(shares, price, config)
    while shares > 0 and shares * price + fee > book.cash:
        shares *= 0.99
        fee = commission(shares, price, config)
    if shares * price < 1.0:
        return None
    book.cash -= shares * price + fee
    book.holdings[symbol] = Holding(shares, price, s.index)
    return Trade(s.day, symbol, "buy", shares, price, fee,
                 shares * (price - s.opens[symbol]), BUY_SCORE, s.scores[symbol])  # fmt: skip


def open_session(book: Book, s: SessionInputs, config: NewsConfig) -> list[Trade]:
    """Every trade at the open of the session (sells, then buys). A stock with no
    open price today is neither bought nor sold (it is kept as it is)."""
    trades: list[Trade] = []
    if book.stopped:
        return trades
    if book.kill_pending:
        trades = [_sell(book, s, sym, SELL_KILL, config) for sym in sorted(book.holdings)
                  if sym in s.opens]  # fmt: skip
        book.stopped = not book.holdings
        book.kill_pending = bool(book.holdings)
        return trades
    for symbol in sorted(book.holdings):
        reason = _exit_reason(book, s, symbol, config) if symbol in s.opens else None
        if reason is not None:
            trades.append(_sell(book, s, symbol, reason, config))
    if s.brake_on:
        return trades
    candidates = sorted(
        (-score, symbol)
        for symbol, score in s.scores.items()
        if score >= config.buy_score and symbol not in book.holdings and symbol in s.opens
    )
    equity = book.equity(s.opens)
    for _, symbol in candidates:
        if len(book.holdings) >= config.max_positions:
            break
        bought = _buy(book, s, symbol, equity, config)
        if bought is not None:
            trades.append(bought)
    return trades


def close_session(book: Book, closes: Mapping[str, float], config: NewsConfig) -> float:
    """Equity at the close; arms the kill switch for the next open when the
    drawdown from the peak reaches `kill_switch_dd`. A holding without a close today
    keeps its last known close (never its entry price)."""
    for symbol, held in book.holdings.items():
        price = closes.get(symbol, math.nan)
        if math.isfinite(price) and price > 0:
            held.mark = price
    equity = book.equity(closes)
    book.peak = max(book.peak, equity)
    if not book.stopped and book.peak > 0 and equity <= book.peak * (1 - config.kill_switch_dd):
        book.kill_pending = True
    return equity


def brake(closes: pd.Series, config: NewsConfig) -> bool:
    """True (no new buys) while the brake symbol's last close is below its
    `brake_sma`-day average; also while there is not enough history."""
    clean = closes.dropna()
    if len(clean) < config.brake_sma:
        return True
    return bool(clean.iloc[-1] < clean.iloc[-config.brake_sma :].mean())
