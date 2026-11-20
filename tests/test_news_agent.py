"""The rule-based news agent `paper_news` (docs/design/news_agent.md)."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from stride import run_news
from stride.data.calendar import trading_days
from stride.news.config import load_news_config
from stride.news.job import Deps, NewsJob
from stride.news.rules import Classifier
from stride.news.source import AlpacaNews, Article
from stride.news.strategy import (
    SELL_KILL,
    SELL_SCORE,
    SELL_STOP,
    SELL_TIME,
    Book,
    Holding,
    SessionInputs,
    brake,
    close_session,
    commission,
    cutoff,
    open_session,
    scores,
)

ROOT = Path(__file__).resolve().parents[1]
CONFIG = load_news_config(ROOT / "config.paper_news.yaml")
CLASSIFIER = Classifier.from_config(CONFIG)


def _kinds(headline: str) -> list[str]:
    return [f"{e.symbol}:{e.kind}" for e in CLASSIFIER.classify(headline)]


# Rules (design section 3), on real Benzinga headlines.


@pytest.mark.parametrize(
    ("headline", "expected"),
    [
        ("Viking Global Investors Increases Stake in Tesla", ["TSLA:STAKE_UP"]),
        ("Berkshire Hathaway Discloses 6.1% Stake in Chevron", ["CVX:STAKE_UP"]),
        ("Walmart Insider Buys Shares Worth $2,000,000", ["WMT:INSIDER_BUY"]),
        ("William Blair Upgrades Advanced Micro Devices to Outperform", ["AMD:UPGRADE"]),
        ("China Renaissance Upgrades Apple to Buy From Hold", ["AAPL:UPGRADE"]),
        ("Nike Raises Fiscal 2027 Revenue Guidance", ["NKE:GUIDANCE_UP"]),
        ("Apple Q4 EPS $1.85 Beats $1.78 Estimate", ["AAPL:BEAT"]),
        ("ExxonMobil Increases Dividend and Announces $20 Billion Buyback Plan", ["XOM:BUYBACK"]),
        ("Bernstein Raises Apple Price Target to $380", ["AAPL:TARGET_UP"]),
        ("Daiwa Securities Adjusts Price Target on Nvidia to $185 From $245", ["NVDA:TARGET_DOWN"]),
        ("NVIDIA Receives Analyst Downgrades Following CEO Resignation", ["NVDA:DOWNGRADE"]),
        ("Tesla Receives Analyst Downgrade to Sell", ["TSLA:DOWNGRADE_SELL"]),
        ("DZ Bank Downgrades Exxon Mobil to Sell", ["XOM:DOWNGRADE_SELL"]),
        ("Fund Reduces Stake in Disney", ["DIS:STAKE_DOWN"]),
        ("Intel Q3 Revenue Misses Estimates", ["INTC:MISS"]),
        ("Nike Cuts Full-Year Outlook", ["NKE:GUIDANCE_DOWN"]),
        ("NVIDIA Faces SEC Investigation and Delays Quarterly 10-Q Filing", ["NVDA:REGULATOR"]),
    ],
)
def test_events(headline: str, expected: list[str]) -> None:
    assert _kinds(headline) == expected


@pytest.mark.parametrize(
    "headline",
    [
        "JPMorgan Chase Downgrades Expedia Group to Neutral",  # JPM acts, Expedia is the subject
        "JPMorgan Chase Increases Stake in Ashtead Technology",
        "JPMorgan Chase and PYMNTS Release Report on Cash Management and Fraud Risk",
        "JPMorgan Chase's Indirect Stake in Ubisoft Surpasses 10%",
        "Amazon Surpasses One Million Robots in Fulfillment Network",
        "Bank of America Lowers Fiscal 2027 iPhone Forecast for Apple",
        "Paylocity Holding Insider Sells Shares Worth $450,824",
        "Class Action Lawsuit Filed Against Microsoft Following Global 365 Outage",
        "Cantor Fitzgerald Maintains $250 Price Target on NVIDIA",
        "Retailers Cut Cost Guidance as Tariffs Bite",  # "cost" is not COST
        "Inderes Lowers Price Target for Metacon",  # Metacon is not Meta
    ],
)
def test_no_false_events(headline: str) -> None:
    assert _kinds(headline) == []


def test_insider_sales_are_ignored() -> None:
    assert _kinds("Apple Insider Sells Shares Worth $5,000,000") == []


# Strategy (design section 4).

DAYS = [d.date() for d in trading_days(date(2026, 11, 2), date(2026, 11, 20))]


def _events(rows: Sequence[tuple[str, str, float]]) -> pd.DataFrame:
    frame = pd.DataFrame(rows, columns=["ts", "symbol", "points"])
    frame["ts"] = pd.to_datetime(frame["ts"], utc=True)
    return frame


def test_cutoff_is_0930_new_york() -> None:
    assert cutoff(date(2026, 11, 18), CONFIG) == pd.Timestamp("2026-11-18 14:30", tz="UTC")


def test_scores_use_news_before_the_cutoff_and_inside_the_window() -> None:
    i = DAYS.index(date(2026, 11, 18))
    ev = _events([
        ("2026-11-18 14:29", "TSLA", 2.0),  # 09:29 New York: counts
        ("2026-11-18 14:31", "TSLA", 5.0),  # after the cutoff: next session
        ("2026-11-12 12:00", "AAPL", 1.0),  # inside the 5-session window
        ("2026-11-02 12:00", "AAPL", 9.0),  # before the window
    ])  # fmt: skip
    assert scores(ev, DAYS, i, CONFIG) == {"TSLA": 2.0, "AAPL": 1.0}


def _inputs(
    index: int, opens: dict[str, float], score: dict[str, float], **kw: Any
) -> SessionInputs:
    return SessionInputs(
        DAYS[index], index, opens, kw.get("prev", opens), score, kw.get("brake_on", False)
    )


def test_buys_the_highest_scores_with_ten_percent_each() -> None:
    book = Book(cash=10_000.0, peak=10_000.0)
    opens = {"TSLA": 100.0, "AAPL": 50.0, "MSFT": 10.0}
    trades = open_session(book, _inputs(5, opens, {"TSLA": 2.0, "AAPL": 4.0, "MSFT": 1.5}), CONFIG)
    assert [t.symbol for t in trades] == ["AAPL", "TSLA"]
    for t in trades:
        assert 980 < t.shares * t.price < 1_000  # 10% of equity, commission reserved
    assert book.cash > 0


def test_no_buys_while_the_brake_is_on_and_never_more_than_max_positions() -> None:
    book = Book(cash=10_000.0, peak=10_000.0)
    assert (
        open_session(book, _inputs(5, {"TSLA": 100.0}, {"TSLA": 3.0}, brake_on=True), CONFIG) == []
    )
    many = {f"S{i:02d}": 10.0 for i in range(15)}
    open_session(book, _inputs(5, many, dict.fromkeys(many, 3.0)), CONFIG)
    assert len(book.holdings) == CONFIG.max_positions


@pytest.mark.parametrize(
    ("score", "bought", "prev", "reason"),
    [(-1.0, 4, 100.0, SELL_SCORE), (0.0, 4 - 20, 100.0, SELL_TIME), (0.0, 4, 89.0, SELL_STOP)],
)
def test_sell_reasons(score: float, bought: int, prev: float, reason: str) -> None:
    book = Book(cash=0.0, holdings={"TSLA": Holding(10.0, 100.0, bought)}, peak=1_000.0)
    trades = open_session(
        book, _inputs(4, {"TSLA": 95.0}, {"TSLA": score}, prev={"TSLA": prev}), CONFIG
    )
    assert [(t.side, t.reason) for t in trades] == [("sell", reason)]
    assert not book.holdings


def test_kill_switch_sells_everything_at_the_next_open_and_stops() -> None:
    book = Book(cash=0.0, holdings={"TSLA": Holding(100.0, 100.0, 0)}, peak=10_000.0)
    close_session(book, {"TSLA": 79.0}, CONFIG)  # -21% from the peak
    assert book.kill_pending
    trades = open_session(book, _inputs(5, {"TSLA": 78.0}, {"AAPL": 9.0}), CONFIG)
    assert [t.reason for t in trades] == [SELL_KILL]
    assert book.stopped and not book.holdings
    assert open_session(book, _inputs(6, {"AAPL": 1.0}, {"AAPL": 9.0}), CONFIG) == []


def test_commission_is_ibkr_tiered() -> None:
    assert commission(10, 100.0, CONFIG) == pytest.approx(0.35 + 0.03)
    assert commission(1_000, 100.0, CONFIG) == pytest.approx(3.5 + 3.0)


def test_brake() -> None:
    rising = pd.Series(np.linspace(100, 200, 250))
    assert not brake(rising, CONFIG)
    assert brake(rising[::-1].reset_index(drop=True), CONFIG)
    assert brake(rising.iloc[:50], CONFIG)  # not enough history: no buys


# Source.


class FakeHttp:
    def __init__(self, pages: list[dict[str, Any]], status: int = 200) -> None:
        self.pages, self.status = pages, status
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, *, params: Any, headers: Any, timeout: float) -> Any:
        self.calls.append(dict(params))
        status, payload = self.status, self.pages.pop(0) if self.pages else {}

        class Response:
            status_code = status

            @staticmethod
            def json() -> dict[str, Any]:
                return payload

        return Response()


def _raw(i: int, ts: str, headline: str) -> dict[str, Any]:
    return {"id": i, "created_at": ts, "headline": headline, "symbols": ["TSLA"], "url": ""}


def test_source_follows_pages_and_hides_keys() -> None:
    http = FakeHttp([
        {"news": [_raw(1, "2026-11-18T10:00:00Z", "a")], "next_page_token": "t"},
        {"news": [_raw(2, "2026-11-18T11:00:00Z", "b")]},
    ])  # fmt: skip
    news = AlpacaNews("KEY123", "SECRET456", session=http)
    got = list(news.articles(["TSLA"], pd.Timestamp("2026-11-18", tz="UTC")))
    assert [a.id for a in got] == [1, 2]
    assert http.calls[1]["page_token"] == "t"
    failing = AlpacaNews("KEY123", "SECRET456", session=FakeHttp([], status=403))
    with pytest.raises(Exception, match="HTTP 403") as raised:
        list(failing.articles(["TSLA"], pd.Timestamp("2026-11-18", tz="UTC")))
    assert "KEY123" not in str(raised.value) and "SECRET456" not in str(raised.value)


# The job, end to end with fakes.


class FakeFeed:
    def __init__(self, articles: list[Article]) -> None:
        self.items = articles

    def articles(self, symbols: Sequence[str], start: pd.Timestamp) -> Iterable[Article]:
        return [a for a in self.items if a.ts >= start]


class FakeBars:
    def fetch(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        days = trading_days(date(2025, 6, 2), end)
        base = np.linspace(100, 150, len(days))  # rising: the brake is off
        return pd.DataFrame({"open": base, "close": base}, index=days)


class Recorder:
    def __init__(self) -> None:
        self.alerts: list[Any] = []

    def send(self, alert: Any) -> None:
        self.alerts.append(alert)

    def send_document(self, filename: str, content: bytes, caption: str) -> None:
        raise AssertionError("no documents")


def _job(tmp_path: Path, clock: dict[str, datetime], feed: FakeFeed, rec: Recorder) -> NewsJob:
    (tmp_path / "config.paper_news.yaml").write_text(
        (ROOT / "config.paper_news.yaml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    deps = Deps(
        now=lambda: clock["now"], news=lambda _: feed, bars=lambda _: FakeBars(),
        alerter=lambda _: rec, git_hash=lambda: "abc1234",
    )  # fmt: skip
    return NewsJob(tmp_path / "config.paper_news.yaml", deps)


def _article(i: int, ts: str, headline: str) -> Article:
    return Article(i, pd.Timestamp(ts, tz="UTC"), headline, ("TSLA",), "")


def test_job_reads_news_alerts_strong_events_and_trades_the_next_open(tmp_path: Path) -> None:
    clock = {"now": datetime(2026, 11, 16, 15, 0, tzinfo=UTC)}  # Monday, first tick
    feed = FakeFeed([_article(1, "2026-11-13T15:00:00", "Tesla Announces New Model")])
    rec = Recorder()
    job = _job(tmp_path, clock, feed, rec)
    lines: list[str] = []
    assert job.run(lines.append) == 0
    assert not rec.alerts  # the backfill never alerts
    feed.items.append(
        _article(2, "2026-11-16T20:00:00", "Viking Global Investors Increases Stake in Tesla")
    )
    clock["now"] = datetime(2026, 11, 16, 21, 0, tzinfo=UTC)
    job.run(lines.append)
    strong = [a for a in rec.alerts if "Noticia fuerte sobre TSLA" in a.message]
    assert len(strong) == 1 and strong[0].instance == "STRIDE NOTICIAS"
    clock["now"] = datetime(2026, 11, 17, 23, 30, tzinfo=UTC)  # after 18:00 New York
    job.run(lines.append)
    report: list[str] = []
    job.report(report.append)
    assert "TSLA" in report[0]
    summary = [a for a in rec.alerts if a.message.startswith("Cierre del 17/11/2026")]
    assert summary and "Compró TSLA" in summary[0].message
    job.run(lines.append)  # the same session is never processed twice
    assert len([a for a in rec.alerts if a.message.startswith("Cierre del 17/11/2026")]) == 1


def test_disabled_and_kill_file(tmp_path: Path) -> None:
    clock = {"now": datetime(2026, 11, 16, 15, 0, tzinfo=UTC)}
    job = _job(tmp_path, clock, FakeFeed([]), Recorder())
    (tmp_path / "KILL_SWITCH_NEWS").write_text("stop", encoding="utf-8")
    lines: list[str] = []
    assert job.run(lines.append) == 0 and "kill switch" in lines[0]


def test_cli_report_without_database(tmp_path: Path) -> None:
    (tmp_path / "config.paper_news.yaml").write_text(
        (ROOT / "config.paper_news.yaml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    assert run_news.main(["--config", str(tmp_path / "config.paper_news.yaml"), "--report"]) == 1


def test_config_has_no_leverage_and_every_event() -> None:
    assert CONFIG.max_positions * CONFIG.position_weight <= 1
    assert len(CONFIG.universe) == 40
