"""The `paper_news` job (docs/design/news_agent.md section 5). Simulated money only.

Every tick: download the news published since the last stored article, record its
events, and send strong ones to Telegram at once. After 18:00 New York: download the
day's bars and process every finished session not processed yet with the pure code
of `stride.news.strategy` (orders at the open, values at the close). The account
lives in the job's own database; nothing is ever sent to a broker.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import subprocess
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from stride.data import store
from stride.data.calendar import is_trading_day, next_trading_day, trading_days
from stride.data.sources import AlpacaSource, SourceError
from stride.news.config import NewsConfig, load_news_config
from stride.news.rules import Classifier
from stride.news.source import AlpacaNews, Article
from stride.news.strategy import (
    NEW_YORK,
    Book,
    Holding,
    SessionInputs,
    Trade,
    brake,
    close_session,
    open_session,
    scores,
)
from stride.reporting import messages as msg
from stride.reporting.alerts import Alert, Alerter, AlertError, AlertKind, alerter_from_env
from stride.settings import CredentialError, load_secrets

logger = logging.getLogger(__name__)

KILL_SWITCH_FILE = "KILL_SWITCH"
SETTLE = time(18, 0)  # New York: the session's bars are final
BACKFILL_DAYS = 10  # news fetched before the first tick, so the first scores exist
BAR_HISTORY_DAYS = 420  # calendar days of bars: enough for the 200-day brake

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS news_articles (
        id INTEGER PRIMARY KEY, ts TEXT NOT NULL, headline TEXT NOT NULL, symbols TEXT)""",
    """CREATE TABLE IF NOT EXISTS news_events (
        article_id INTEGER, ts TEXT NOT NULL, symbol TEXT, kind TEXT, points REAL,
        PRIMARY KEY (article_id, symbol))""",
    """CREATE TABLE IF NOT EXISTS news_bars (
        symbol TEXT, date TEXT, open REAL, close REAL, PRIMARY KEY (symbol, date))""",
    """CREATE TABLE IF NOT EXISTS news_trades (
        day TEXT, symbol TEXT, side TEXT, shares REAL, price REAL, commission REAL,
        spread_cost REAL, reason TEXT, score REAL)""",
    """CREATE TABLE IF NOT EXISTS news_equity (
        day TEXT PRIMARY KEY, equity REAL, cash REAL, invested REAL, drawdown REAL)""",
    """CREATE TABLE IF NOT EXISTS news_state (key TEXT PRIMARY KEY, value TEXT NOT NULL)""",
)

EVENT_TEXT = {
    "INSIDER_BUY": "un directivo compró acciones",
    "STAKE_UP": "un inversor grande aumentó o tomó una participación",
    "UPGRADE": "un analista subió la recomendación",
    "GUIDANCE_UP": "la empresa subió su pronóstico",
    "BEAT": "resultados mejores de lo esperado",
    "BUYBACK": "recompra de acciones",
    "TARGET_UP": "un analista subió el precio objetivo",
    "TARGET_DOWN": "un analista bajó el precio objetivo",
    "DOWNGRADE": "un analista bajó la recomendación",
    "DOWNGRADE_SELL": "un analista recomienda vender",
    "STAKE_DOWN": "un inversor grande redujo su participación",
    "MISS": "resultados peores de lo esperado",
    "GUIDANCE_DOWN": "la empresa bajó su pronóstico",
    "REGULATOR": "investigación de un regulador",
}


# The news feed is an `AlpacaNews` (`articles(symbols, start)`) and the bar source an
# `AlpacaSource` (`fetch(symbol, start, end)`), or fakes with the same methods in tests.
NewsFeed = Any
BarSource = Any


def _keys(root: Path) -> tuple[str, str]:
    return load_secrets(root / ".env").alpaca_paper_keys("ALPACA")


def _git_hash() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return out.stdout.strip() or "unknown"


@dataclass
class Deps:
    """Everything the job reads from the outside world, injectable in tests."""

    now: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    news: Callable[[Path], NewsFeed] = field(default=lambda root: AlpacaNews(*_keys(root)))
    bars: Callable[[Path], BarSource] = field(default=lambda root: AlpacaSource(*_keys(root)))
    alerter: Callable[[Path], Alerter] = field(
        default=lambda root: alerter_from_env(str(root / ".env"))
    )
    git_hash: Callable[[], str] = _git_hash


class NewsJob:
    def __init__(self, config_path: Path, deps: Deps | None = None) -> None:
        self.path = config_path.resolve()
        self.root = self.path.parent
        self.config: NewsConfig = load_news_config(self.path)
        self.deps = deps or Deps()
        self.db = self.root / self.config.db_path
        self.kill_file = self.root / f"{KILL_SWITCH_FILE}_NEWS"
        self.classifier = Classifier.from_config(self.config)

    def alert(self, kind: AlertKind, text: str) -> None:
        try:
            self.deps.alerter(self.root).send(Alert(kind, text, instance="STRIDE NOTICIAS"))
        except AlertError:
            logger.warning("alert could not be delivered: %s", kind)

    # --- the tick --------------------------------------------------------------

    def run(self, out: Callable[[str], None]) -> int:
        if not self.config.enabled:
            out("STRIDE NEWS: disabled")
            return 0
        if self.kill_file.exists() or (self.root / KILL_SWITCH_FILE).exists():
            out("STRIDE NEWS: stopped by a kill switch file")
            return 0
        now = pd.Timestamp(self.deps.now()).tz_convert("UTC")
        conn = store.connect(self.db)
        try:
            for statement in _SCHEMA:
                conn.execute(statement)
            started = now.isoformat()
            try:
                new = self._poll(conn, now)
                processed = self._process(conn, now)
            except (SourceError, CredentialError) as exc:
                conn.rollback()
                store.record_run(
                    conn,
                    now.date(),
                    "news",
                    "paper",
                    "error",
                    started,
                    now.isoformat(),
                    self.deps.git_hash(),
                    str(exc),
                )
                self.alert("DATA_FAILED", msg.failed("news", str(exc)))
                out(f"STRIDE NEWS: error: {exc}")
                return 2
            detail = f"{new} new article(s); {processed} session(s) processed"
            store.record_run(
                conn,
                now.date(),
                "news",
                "paper",
                "ok",
                started,
                pd.Timestamp(self.deps.now()).isoformat(),
                self.deps.git_hash(),
                detail,
            )
            out(f"STRIDE NEWS: {detail}")
            return 0
        finally:
            conn.close()

    # --- news ------------------------------------------------------------------

    def _poll(self, conn: sqlite3.Connection, now: pd.Timestamp) -> int:
        last = _state(conn, "last_article_ts")
        if _state(conn, "first_day") is None:
            _set_state(conn, "first_day", _first_session(now).isoformat())
        start = pd.Timestamp(last) if last else now - pd.Timedelta(days=BACKFILL_DAYS)
        feed = self.deps.news(self.root)
        new = 0
        strong: list[tuple[Article, str, str, float]] = []
        for article in feed.articles(self.config.symbols, start):
            inserted = conn.execute(
                "INSERT OR IGNORE INTO news_articles VALUES (?,?,?,?)",
                (article.id, article.ts.isoformat(), article.headline, ",".join(article.symbols)),
            ).rowcount
            if not inserted:
                continue
            new += 1
            for event in self.classifier.classify(article.headline):
                conn.execute(
                    "INSERT OR IGNORE INTO news_events VALUES (?,?,?,?,?)",
                    (article.id, article.ts.isoformat(), event.symbol, event.kind, event.points),
                )
                if abs(event.points) >= self.config.alert_points and last is not None:
                    strong.append((article, event.symbol, event.kind, event.points))
            _set_state(conn, "last_article_ts", article.ts.isoformat())
        conn.commit()
        for article, symbol, kind, points in strong:
            self._alert_event(article, symbol, kind, points)
        return new

    def _alert_event(self, article: Article, symbol: str, kind: str, points: float) -> None:
        action = (
            "Si su puntaje llega a +2 o más, se compra en la próxima apertura."
            if points > 0
            else "Si la tiene, se vende en la próxima apertura."
        )
        self.alert(
            "INFO",
            f"Noticia fuerte sobre {symbol}: {EVENT_TEXT.get(kind, kind)} ({points:+g} puntos).\n"
            f"Titular: {article.headline}\n{action}",
        )

    # --- sessions --------------------------------------------------------------

    def _process(self, conn: sqlite3.Connection, now: pd.Timestamp) -> int:
        first = date.fromisoformat(str(_state(conn, "first_day")))
        last_done = _state(conn, "last_session")
        target = _last_settled(now)
        if target < first:
            return 0
        start = next_trading_day(date.fromisoformat(last_done)) if last_done else first
        days = [d.date() for d in trading_days(start, target)]
        if not days:
            return 0
        self._download_bars(conn, target)
        opens, closes = _bars(conn)
        sessions = [d.date() for d in trading_days(first, target)]
        events = _events(conn)
        book = _load_book(conn, self.config)
        done = 0
        for day in days:
            stamp = pd.Timestamp(day)
            if stamp not in opens.index or pd.isna(closes.loc[stamp].get(self.config.brake_symbol)):
                break  # its bars are not stored yet: the next tick tries again
            index = sessions.index(day)
            prior = closes.loc[:stamp].iloc[:-1]
            inputs = SessionInputs(
                day,
                index,
                _row(opens, stamp),
                _row(prior, prior.index[-1]) if len(prior) else {},
                scores(events, sessions, index, self.config),
                brake(prior[self.config.brake_symbol], self.config),
            )
            trades = open_session(book, inputs, self.config)
            was_armed = book.kill_pending
            equity = close_session(book, _row(closes, stamp), self.config)
            self._record(conn, day, trades, book, equity)
            if book.kill_pending and not was_armed:
                fall = msg.pct(1 - equity / book.peak, signed=False)
                self.alert(
                    "KILL_SWITCH",
                    f"La cuenta cayó {fall} desde su máximo: vende todo en la próxima apertura"
                    " y se detiene.",
                )
            self.alert("INFO", self._summary(day, trades, book, equity, inputs.brake_on))
            done += 1
        return done

    def _download_bars(self, conn: sqlite3.Connection, target: date) -> None:
        # The whole history every time: adjusted prices are revised after dividends and
        # splits, and the 200-day brake must never mix two adjustment bases.
        start = target - timedelta(days=BAR_HISTORY_DAYS)
        conn.execute("DELETE FROM news_bars")
        source = self.deps.bars(self.root)
        for symbol in [*self.config.symbols, self.config.brake_symbol]:
            frame = source.fetch(symbol, start, target)
            rows = [
                (symbol, pd.Timestamp(d).date().isoformat(), float(o), float(c))
                for d, o, c in zip(frame.index, frame["open"], frame["close"], strict=True)
            ]
            conn.executemany("INSERT OR REPLACE INTO news_bars VALUES (?,?,?,?)", rows)
        conn.commit()

    def _record(
        self, conn: sqlite3.Connection, day: date, trades: list[Trade], book: Book, equity: float
    ) -> None:
        conn.executemany(
            "INSERT INTO news_trades VALUES (?,?,?,?,?,?,?,?,?)",
            [
                (
                    t.day.isoformat(),
                    t.symbol,
                    t.side,
                    t.shares,
                    t.price,
                    t.commission,
                    t.spread_cost,
                    t.reason,
                    t.score,
                )
                for t in trades
            ],
        )
        invested = equity - book.cash
        drawdown = 1 - equity / book.peak if book.peak > 0 else 0.0
        conn.execute(
            "INSERT OR REPLACE INTO news_equity VALUES (?,?,?,?,?)",
            (day.isoformat(), equity, book.cash, invested, drawdown),
        )
        _set_state(conn, "book", json.dumps(_book_json(book)))
        _set_state(conn, "last_session", day.isoformat())
        conn.commit()

    def _summary(
        self, day: date, trades: list[Trade], book: Book, equity: float, brake_on: bool
    ) -> str:
        total = equity / self.config.capital_usd - 1
        lines = [
            f"Cierre del {msg.day_es(day)}",
            f"Valor de la cuenta: {msg.usd(equity)} ({msg.pct(total)} desde que empezó)",
            f"En efectivo: {msg.usd(book.cash)}. Acciones en cartera: {len(book.holdings)}",
        ]
        for t in trades:
            verb = "Compró" if t.side == "buy" else "Vendió"
            why = {
                "SCORE": "por las noticias",
                "TIME": "por tiempo (20 sesiones)",
                "STOP": "por stop (-10%)",
                "KILL_SWITCH": "por el freno de emergencia",
            }
            lines.append(
                f"{verb} {t.symbol} a {msg.usd(t.price)} {why.get(t.reason, t.reason)}"
                f" (puntaje {t.score:+g})"
            )
        if not trades:
            lines.append("Hoy no hubo operaciones.")
        if brake_on:
            lines.append(
                "Freno de mercado activo: SPY está bajo su promedio de 200 días, "
                "no se hacen compras nuevas."
            )
        return "\n".join(lines)

    # --- report ------------------------------------------------------------------

    def report(self, out: Callable[[str], None]) -> int:
        if not self.db.exists():
            out("STRIDE NEWS: no database yet")
            return 1
        conn = sqlite3.connect(self.db)
        try:
            row = conn.execute(
                "SELECT day, equity, cash, drawdown FROM news_equity ORDER BY day DESC LIMIT 1"
            ).fetchone()
            if row is None:
                out("STRIDE NEWS: no session processed yet")
                return 0
            trades = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(commission + spread_cost), 0) FROM news_trades"
            ).fetchone()
            book = json.loads(_state(conn, "book") or "{}")
            out(
                f"STRIDE NEWS {row[0]}: equity {row[1]:.2f} USD, cash {row[2]:.2f}, "
                f"drawdown {row[3]:.1%}, trades {trades[0]}, costs {trades[1]:.2f} USD, "
                f"holdings {sorted(book.get('holdings', {}))}"
            )
            return 0
        finally:
            conn.close()


# --- helpers ---------------------------------------------------------------------


def _state(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM news_state WHERE key = ?", (key,)).fetchone()
    return None if row is None else str(row[0])


def _set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO news_state VALUES (?, ?)", (key, value))


def _first_session(now: pd.Timestamp) -> date:
    """The first session the account trades: the next trading day after the first tick."""
    return next_trading_day(now.tz_convert(NEW_YORK).date())


def _last_settled(now: pd.Timestamp) -> date:
    """The newest session whose bars are final (after 18:00 New York on its day)."""
    local = now.tz_convert(NEW_YORK)
    day = local.date()
    if not (is_trading_day(day) and local.time() >= SETTLE):
        day = day - timedelta(days=1)
        while not is_trading_day(day):
            day -= timedelta(days=1)
    return day


def _bars(conn: sqlite3.Connection) -> tuple[pd.DataFrame, pd.DataFrame]:
    frame = pd.read_sql(
        "SELECT symbol, date, open, close FROM news_bars", conn, parse_dates=["date"]
    )
    opens = frame.pivot(index="date", columns="symbol", values="open").sort_index()
    closes = frame.pivot(index="date", columns="symbol", values="close").sort_index()
    return opens, closes


def _row(frame: pd.DataFrame, stamp: pd.Timestamp) -> dict[str, float]:
    """One date of a symbol-by-column frame as {symbol: value}, without gaps."""
    row = pd.Series(frame.loc[stamp]).dropna()
    return {str(k): float(v) for k, v in zip(row.index, row.to_numpy(dtype=float), strict=True)}


def _events(conn: sqlite3.Connection) -> pd.DataFrame:
    frame = pd.read_sql("SELECT ts, symbol, points FROM news_events", conn)
    frame["ts"] = pd.to_datetime(frame["ts"], utc=True)
    return frame


def _book_json(book: Book) -> dict[str, object]:
    return {
        "cash": book.cash,
        "peak": book.peak,
        "stopped": book.stopped,
        "kill_pending": book.kill_pending,
        "holdings": {s: asdict(h) for s, h in book.holdings.items()},
    }


def _load_book(conn: sqlite3.Connection, config: NewsConfig) -> Book:
    raw = _state(conn, "book")
    if raw is None:
        return Book(cash=config.capital_usd, peak=config.capital_usd)
    data = json.loads(raw)
    holdings = {s: Holding(**h) for s, h in data["holdings"].items()}
    return Book(
        cash=float(data["cash"]),
        holdings=holdings,
        peak=float(data["peak"]),
        stopped=bool(data["stopped"]),
        kill_pending=bool(data["kill_pending"]),
    )
