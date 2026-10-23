"""The backtest command: load stored prices, run, write the result tables."""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from stride.backtest.engine import BacktestSettings, run_backtest
from stride.backtest.run import format_summary, load_market, main, summarize, write_results
from stride.config import load_config
from stride.data.calendar import trading_days
from stride.data.leveraged import EtfFacts
from stride.data.store import (
    connect,
    read_equity,
    read_events,
    read_table,
    upsert_prices,
    upsert_risk_decisions,
    upsert_signals,
)

ROOT = Path(__file__).resolve().parents[1]
CONFIG = load_config(ROOT / "config.yaml", env={})
DAYS = trading_days(date(2018, 1, 2), date(2020, 6, 30))
RATE = pd.Series(0.02, index=pd.date_range("2017-12-01", "2020-07-31"))


def _contract(symbol: str, seed: int, drift: float = 0.0008, scale: float = 100.0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = scale * np.cumprod(1 + rng.normal(drift, 0.015, len(DAYS)))
    open_ = close * (1 + rng.normal(0, 0.003, len(DAYS)))
    high = np.maximum(open_, close) * 1.004
    low = np.minimum(open_, close) * 0.996
    return pd.DataFrame(
        {"symbol": symbol, "open": open_, "high": high, "low": low, "close": close,
         "volume": 1_000, "source": "test", "fetched_at": pd.Timestamp("2020-07-01", tz="UTC")},
        index=pd.DatetimeIndex(DAYS, name="date"),
    )  # fmt: skip


def _price_db(path: Path) -> None:
    conn = connect(path)
    for i, symbol in enumerate(["SPY", "IEF", "GLD", "DBC", "SSO"]):
        frame = _contract(symbol, i, scale=50.0 if symbol == "SSO" else 100.0)
        if symbol == "SSO":
            frame = frame.iloc[300:]  # launched later: synthetic history before
        upsert_prices(conn, frame)
    conn.close()


def _facts(spread: float | None) -> dict[str, EtfFacts]:
    return {
        "SSO": EtfFacts(
            ticker="SSO", underlying="SPY", multiple=2, expense_ratio=0.009, aum_usd=1e9,
            median_spread_30d=spread, as_of=date(2026, 1, 1), source=[],
        )
    }  # fmt: skip


def test_load_market_builds_leveraged_history_only_for_passing_etfs(tmp_path: Path) -> None:
    _price_db(tmp_path / "p.db")
    conn = connect(tmp_path / "p.db")
    data, calibrated = load_market(conn, CONFIG, RATE, _facts(0.001))
    assert data.eligible == {"SPY": {2: "SSO"}}
    assert data.bars["SSO"].index[0] == data.bars["SPY"].index[0]  # synthetic before launch
    assert [c.etf for c in calibrated] == ["SSO"]
    data, calibrated = load_market(conn, CONFIG, RATE, _facts(None))  # missing spread fails
    assert data.eligible == {} and "SSO" not in data.bars and calibrated == []
    assert set(data.bars) == {"SPY", "IEF", "GLD", "DBC"}
    conn.close()


def test_write_results_fills_the_tables(tmp_path: Path) -> None:
    _price_db(tmp_path / "p.db")
    source = connect(tmp_path / "p.db")
    data, _ = load_market(source, CONFIG, RATE, _facts(0.001))
    result = run_backtest(data, CONFIG, BacktestSettings(date(2019, 1, 2), date(2020, 6, 30), 1e4))
    out = connect(tmp_path / "out.db")
    write_results(out, result, {"SPY": _contract("SPY", 0)})
    write_results(out, result, {"SPY": _contract("SPY", 0)})  # a rerun replaces, not appends
    assert len(read_equity(out)) == len(result.equity)
    assert len(read_table(out, "signals")) == len(result.signals)
    risk = read_table(out, "risk_decisions")
    assert len(risk) == len(result.risk_decisions)
    assert all(isinstance(json.loads(v), list) for v in risk["vetoes"])
    deposits = out.execute("SELECT COUNT(*), SUM(amount) FROM cash_flows").fetchone()
    assert deposits == (len(result.cash_flows), pytest.approx(result.cash_flows.sum()))
    assert len(read_events(out)) == len(result.events)
    summary = summarize(result, data.bars, RATE)
    assert summary.spy is not None and summary.sixty_forty is not None
    assert any(line.startswith("STRIDE") for line in format_summary(summary))
    source.close()
    out.close()


def test_signal_and_risk_rows_round_trip_with_missing_values(tmp_path: Path) -> None:
    conn = connect(tmp_path / "x.db")
    day = pd.Timestamp("2020-01-02")
    signals = pd.DataFrame(
        [{"date": day, "symbol": "SPY", "close": 1.0, "sma200": math.nan, "hh55": math.nan,
          "ll20": math.nan, "n": math.nan, "rule": "TREND_FILTER", "current_units": 0,
          "target_units": 0, "last_entry": None, "capped": 0}]
    ).set_index(["date", "symbol"])  # fmt: skip
    upsert_signals(conn, signals)
    risk = pd.DataFrame(
        [{"date": day, "l_star": math.nan, "kelly_days": 0, "sigma_60": 0.0, "leverage": 1.0,
          "binding_limit": "KELLY", "drawdown": 0.0, "delever_active": 0, "k": 0.0,
          "vetoes": "[]"}]
    ).set_index("date")  # fmt: skip
    upsert_risk_decisions(conn, risk)
    assert read_table(conn, "signals")["sma200"].isna().all()
    assert read_table(conn, "risk_decisions")["l_star"].isna().all()
    with pytest.raises(ValueError, match="missing"):
        upsert_risk_decisions(conn, risk.drop(columns="k"))
    with pytest.raises(ValueError, match="unknown"):
        read_table(conn, "prices")
    conn.close()


def _config(tmp_path: Path) -> Path:
    text = (ROOT / "config.yaml").read_text(encoding="utf-8")
    path = tmp_path / "config.yaml"
    path.write_text(text.replace("db_path: data/stride.db", "db_path: p.db"), encoding="utf-8")
    return path


def test_main_runs_and_writes_a_separate_database(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _price_db(tmp_path / "p.db")
    config = _config(tmp_path)
    code = main(
        ["--config", str(config), "--start", "2019-01-02", "--out", "out.db",
         "--report", str(tmp_path / "r.html")],
        rate_source=lambda: RATE,
    )  # fmt: skip
    assert code == 0
    output = capsys.readouterr().out
    assert "STRIDE" in output and "kill switch" in output
    out = sqlite3.connect(tmp_path / "out.db")
    assert out.execute("SELECT COUNT(*) FROM equity").fetchone()[0] > 0
    out.close()
    prices = sqlite3.connect(tmp_path / "p.db")
    assert prices.execute("SELECT COUNT(*) FROM equity").fetchone()[0] == 0  # untouched
    prices.close()
    assert (tmp_path / "r.html").read_text(encoding="utf-8").startswith("<!doctype html>")


def test_main_refuses_to_write_into_the_price_database(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _price_db(tmp_path / "p.db")
    config = _config(tmp_path)
    assert main(["--config", str(config), "--out", "p.db"], rate_source=lambda: RATE) == 2
    assert "must not be the price database" in capsys.readouterr().out
    (tmp_path / "other").mkdir()
    missing = _config(tmp_path / "other")  # no price database next to it
    assert main(["--config", str(missing)], rate_source=lambda: RATE) == 2
