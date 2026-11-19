"""Backtest of the news agent on past Alpaca news (docs/design/news_agent.md section 6).

    python scripts/research/news_backtest.py            # download (resumable) and run
    python scripts/research/news_backtest.py --no-download

Stores articles and bars in `data/news_research.db` (git-ignored), then runs the same
pure code as the paper account (`stride.news.strategy`) session by session and prints
the results for 2021-2023 and for 2024 to today, with SPY and the equal-weight
buy-and-hold of the 40 stocks over the same sessions.
"""

from __future__ import annotations

import argparse
import math
import sqlite3
import sys
from datetime import date
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from stride.data.calendar import trading_days  # noqa: E402
from stride.data.sources import AlpacaSource  # noqa: E402
from stride.news.config import NewsConfig, load_news_config  # noqa: E402
from stride.news.rules import Classifier  # noqa: E402
from stride.news.source import AlpacaNews  # noqa: E402
from stride.news.strategy import (  # noqa: E402
    Book,
    SessionInputs,
    Trade,
    brake,
    close_session,
    open_session,
    scores,
)
from stride.settings import load_secrets  # noqa: E402

START = date(2021, 1, 4)
HOLDOUT = date(2024, 1, 1)
BARS_START = date(2020, 1, 1)
DB = ROOT / "data" / "news_research.db"


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS articles (
            id INTEGER PRIMARY KEY, ts TEXT NOT NULL, headline TEXT NOT NULL, symbols TEXT
        );
        CREATE TABLE IF NOT EXISTS bars (
            symbol TEXT, date TEXT, open REAL, close REAL, PRIMARY KEY (symbol, date)
        );
        """
    )
    return conn


def download(conn: sqlite3.Connection, config: NewsConfig) -> None:
    key, secret = load_secrets(ROOT / ".env").alpaca_paper_keys("ALPACA")
    last = conn.execute("SELECT MAX(ts) FROM articles").fetchone()[0]
    start = pd.Timestamp(last) if last else pd.Timestamp(START.isoformat(), tz="UTC")
    news = AlpacaNews(key, secret)
    count = 0
    batch: list[tuple[int, str, str, str]] = []
    for article in news.articles(config.symbols, start):
        batch.append(
            (article.id, article.ts.isoformat(), article.headline, ",".join(article.symbols))
        )
        if len(batch) >= 500:  # noqa: PLR2004
            conn.executemany("INSERT OR IGNORE INTO articles VALUES (?,?,?,?)", batch)
            conn.commit()
            count += len(batch)
            batch.clear()
            print(f"  {count} articles, up to {article.ts:%Y-%m-%d}", flush=True)
    conn.executemany("INSERT OR IGNORE INTO articles VALUES (?,?,?,?)", batch)
    conn.commit()
    source = AlpacaSource(api_key=key, secret_key=secret)
    for symbol in [*config.symbols, config.brake_symbol]:
        frame = source.fetch(symbol, BARS_START, date.today())
        rows = [(symbol, d.date().isoformat(), float(o), float(c))
                for d, o, c in zip(frame.index, frame["open"], frame["close"], strict=True)]  # fmt: skip
        conn.executemany("INSERT OR REPLACE INTO bars VALUES (?,?,?,?)", rows)
        conn.commit()
    print(
        f"downloaded {count + len(batch)} new articles and bars for {len(config.symbols) + 1} symbols"
    )


def load(
    conn: sqlite3.Connection, classifier: Classifier
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    rows = conn.execute("SELECT ts, headline FROM articles ORDER BY ts").fetchall()
    events = [
        (pd.Timestamp(ts), e.symbol, e.kind, e.points, headline)
        for ts, headline in rows
        for e in classifier.classify(headline)
    ]
    ev = pd.DataFrame(events, columns=["ts", "symbol", "kind", "points", "headline"])
    bars = pd.read_sql("SELECT symbol, date, open, close FROM bars", conn, parse_dates=["date"])
    opens = bars.pivot(index="date", columns="symbol", values="open").sort_index()
    closes = bars.pivot(index="date", columns="symbol", values="close").sort_index()
    return ev, opens, closes


def simulate(
    config: NewsConfig,
    ev: pd.DataFrame,
    opens: pd.DataFrame,
    closes: pd.DataFrame,
    *,
    first: date,
    last: date,
) -> tuple[pd.Series, list[Trade]]:
    days = [d.date() for d in trading_days(first, last) if pd.Timestamp(d) in opens.index]
    book = Book(cash=config.capital_usd, peak=config.capital_usd)
    equity, trades = {}, []
    spy = closes[config.brake_symbol]
    for i, day in enumerate(days):
        stamp = pd.Timestamp(day)
        o = opens.loc[stamp].dropna().to_dict()
        prev = closes.loc[:stamp].iloc[:-1]
        prev_close = prev.iloc[-1].dropna().to_dict() if len(prev) else {}
        s = scores(ev, days, i, config)
        on = brake(spy.loc[:stamp].iloc[:-1], config)
        trades += open_session(book, SessionInputs(day, i, o, prev_close, s, on), config)
        equity[stamp] = close_session(book, closes.loc[stamp].dropna().to_dict(), config)
    return pd.Series(equity), trades


def metrics(curve: pd.Series) -> dict[str, float]:
    returns = curve.pct_change().dropna()
    years = len(returns) / 252
    cagr = (curve.iloc[-1] / curve.iloc[0]) ** (1 / years) - 1 if years > 0 else math.nan
    vol = float(returns.std(ddof=1) * math.sqrt(252))
    sharpe = float(returns.mean() * 252 / vol) if vol > 0 else math.nan
    dd = float((1 - curve / curve.cummax()).max())
    return {
        "cagr": cagr,
        "sharpe": sharpe,
        "max_dd": dd,
        "total": curve.iloc[-1] / curve.iloc[0] - 1,
    }


def report(
    name: str, curve: pd.Series, trades: list[Trade], closes: pd.DataFrame, config: NewsConfig
) -> None:
    days = curve.index
    spy = closes[config.brake_symbol].reindex(days)
    ew = closes[config.symbols].reindex(days)
    ew_curve = (ew / ew.iloc[0]).mean(axis=1)
    period = [t for t in trades if days[0].date() <= t.day <= days[-1].date()]
    buys = sum(t.side == "buy" for t in period)
    costs = sum(t.commission + t.spread_cost for t in period)
    print(f"\n== {name}: {days[0].date()} .. {days[-1].date()} ({len(days)} sessions)")
    print(f"{'':10} {'CAGR':>8} {'Sharpe':>7} {'MaxDD':>7} {'Total':>8}")
    for label, c in (("news", curve), ("SPY", spy), ("EW40", ew_curve)):
        m = metrics(c.dropna())
        print(
            f"{label:10} {m['cagr']:8.2%} {m['sharpe']:7.2f} {m['max_dd']:7.2%} {m['total']:8.2%}"
        )
    kinds = pd.Series([t.reason for t in period if t.side == "sell"]).value_counts().to_dict()
    print(f"buys {buys}, sells by reason {kinds}, costs {costs:,.2f} USD "
          f"({costs / config.capital_usd:.2%} of capital)")  # fmt: skip


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-download", action="store_true")
    args = parser.parse_args()
    config = load_news_config(ROOT / "config.paper_news.yaml")
    conn = connect()
    if not args.no_download:
        download(conn, config)
    ev, opens, closes = load(conn, Classifier.from_config(config))
    print(f"events: {len(ev)}; by kind: {ev['kind'].value_counts().to_dict()}")
    last = opens.index.max().date()
    for name, first, end in (
        ("2021-2023", START, date(2023, 12, 29)),
        ("holdout 2024-today", HOLDOUT, last),
    ):
        curve, trades = simulate(config, ev, opens, closes, first=first, last=end)
        report(name, curve, trades, closes, config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
