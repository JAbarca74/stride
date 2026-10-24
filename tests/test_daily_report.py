"""Daily HTML report: what was done and what was not (SPEC section 10)."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from stride.data.calendar import trading_days
from stride.data.store import (
    connect,
    read_table,
    record_cash_flow,
    record_event,
    record_fills,
    record_orders,
    upsert_equity,
    upsert_prices,
    upsert_risk_decisions,
    upsert_signals,
)
from stride.reporting import daily

ROOT = Path(__file__).resolve().parents[1]
T1, T2 = date(2026, 10, 21), date(2026, 10, 22)


def _db(path: Path) -> sqlite3.Connection:
    conn = connect(path)
    upsert_equity(
        conn,
        pd.DataFrame(
            {"equity": [100.0, 121.0], "cash": [100.0, 40.0], "gross_exposure": [0.0, 81.0],
             "leverage": [0.0, 0.67]},
            index=pd.to_datetime([T1, T2]),
        ),
    )  # fmt: skip
    record_cash_flow(conn, T2, 20.0, "deposit", "test")
    signals = pd.DataFrame(
        [
            {"date": T2, "symbol": "SPY", "close": 1.0, "sma200": 1.0, "hh55": 1.0, "ll20": 1.0,
             "n": 1.0, "rule": "ENTRY_HH55", "current_units": 0, "target_units": 1,
             "last_entry": 1.0, "capped": 0},
            {"date": T2, "symbol": "GLD", "close": 1.0, "sma200": 1.0, "hh55": 1.0, "ll20": 1.0,
             "n": 1.0, "rule": "PYRAMID", "current_units": 2, "target_units": 2,
             "last_entry": 1.0, "capped": 1},
            {"date": T2, "symbol": "TLT", "close": 1.0, "sma200": 1.0, "hh55": 1.0, "ll20": 1.0,
             "n": 1.0, "rule": "HOLD", "current_units": 0, "target_units": 0,
             "last_entry": None, "capped": 0},
        ]
    ).set_index(["date", "symbol"])  # fmt: skip
    upsert_signals(conn, signals)
    upsert_risk_decisions(
        conn,
        pd.DataFrame(
            [{"date": T2, "l_star": 1.5, "kelly_days": 300, "sigma_60": 0.2, "leverage": 1.25,
              "binding_limit": "KELLY", "drawdown": 0.03, "delever_active": 0, "k": 0.8,
              "vetoes": '["GLD:sell:ORDER_TOO_LARGE"]'}]
        ).set_index("date"),
    )  # fmt: skip
    record_orders(
        conn,
        pd.DataFrame(
            [{"date": T2, "client_id": "a", "broker_id": "sim-1", "symbol": "SPY", "side": "buy",
              "notional": 81.0, "type": "market", "limit_price": None, "rule": "ENTRY_HH55",
              "switch": 0, "reasons": "", "status": "pending"},
             {"date": T2, "client_id": "b", "broker_id": None, "symbol": "GLD", "side": "sell",
              "notional": 70.0, "type": "market", "limit_price": None, "rule": "EXIT_LL20",
              "switch": 0, "reasons": "ORDER_TOO_LARGE", "status": "vetoed"}]
        ),
    )  # fmt: skip
    record_fills(
        conn,
        pd.DataFrame(
            [{"date": T2, "broker_id": "sim-0", "symbol": "IEF", "side": "sell", "quantity": 0.5,
              "price": 95.0, "commission": 0.48, "slippage_vs_close": -0.001}]
        ),
    )  # fmt: skip
    record_event(conn, "ORDER_REJECTED", "sim-0b DBC buy: not enough cash", at=f"{T2}T13:30:00")
    record_event(conn, "VETO", "GLD:sell:ORDER_TOO_LARGE", at=T2.isoformat())
    record_event(conn, "ORDER_REJECTED", "an older day", at=T1.isoformat())
    return conn


def test_summary_lists_what_was_done_and_not_done(tmp_path: Path) -> None:
    conn = _db(tmp_path / "x.db")
    s = daily.build_daily(conn, T2, "paper")
    assert s.status == daily.ATTENTION
    assert s.equity == 121.0
    assert s.day_return == pytest.approx((121 - 20) / 100 - 1)  # the deposit is not a gain
    assert (s.drawdown, s.leverage, s.binding_limit) == (0.03, 1.25, "KELLY")
    assert s.done == [
        "SPY: 0 → 1 unidades (entrada por máximo de 55 días, ENTRY_HH55)",
        "Ejecutada: venta de 0.5000 IEF a $95.00 (comisión $0.48)",
        (
            "Orden enviada para la apertura siguiente: compra de $81.00 en SPY"
            + " (entrada por máximo de 55 días, ENTRY_HH55)"
        ),
        "Depósito recibido: $20.00",
    ]
    assert s.not_done == [
        (
            "Vetada: venta de $70.00 en GLD (salida por mínimo de 20 días, EXIT_LL20)."
            + " Motivo: orden mayor al límite por orden (ORDER_TOO_LARGE)"
        ),
        "GLD: la unidad nueva quedó recortada por los topes de exposición (piramidación, PYRAMID)",
        "Orden rechazada por el broker: sim-0b DBC buy: not enough cash",
    ]  # the VETO event and the older rejection are not repeated
    assert s.positions == [("GLD", 2, "PYRAMID"), ("SPY", 1, "ENTRY_HH55")]  # by symbol
    conn.close()


def test_html_is_short_escaped_and_mobile_friendly(tmp_path: Path) -> None:
    conn = _db(tmp_path / "x.db")
    record_event(conn, "ERROR", "<script>alert(1)</script>", at=T2.isoformat())
    html = daily.render_daily(daily.build_daily(conn, T2, "paper"))
    assert html.startswith("<!doctype html>")
    assert 'name="viewport"' in html and "prefers-color-scheme: dark" in html
    assert '<html lang="es">' in html
    for heading in ("Cuenta", "Hecho", "No hecho", "Posiciones"):
        assert f"<h2>{heading}" in html
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert len(html) < 10_000
    conn.close()


def test_status_for_no_run_quiet_day_halt_and_kill_switch(tmp_path: Path) -> None:
    conn = _db(tmp_path / "x.db")
    missing = daily.build_daily(conn, date(2026, 10, 23), "paper")
    assert missing.status == daily.NO_RUN and missing.equity is None
    assert "No hay corrida registrada" in daily.render_daily(missing)
    quiet = daily.build_daily(conn, T1, "paper")
    assert quiet.status == daily.ATTENTION  # the older rejection belongs to T1
    record_event(conn, "HALT", "reconciliation breaks on two consecutive days", at=T2.isoformat())
    assert daily.build_daily(conn, T2, "paper").status == daily.HALTED
    record_event(conn, "KILL_SWITCH", "DRAWDOWN; drawdown 40.10%", at=T2.isoformat())
    assert daily.build_daily(conn, T2, "paper").status == daily.KILLED
    conn.close()


def test_quiet_day_is_ok(tmp_path: Path) -> None:
    conn = connect(tmp_path / "q.db")
    upsert_equity(
        conn,
        pd.DataFrame(
            {"equity": [100.0], "cash": [100.0], "gross_exposure": [0.0], "leverage": [0.0]},
            index=pd.to_datetime([T2]),
        ),
    )
    s = daily.build_daily(conn, T2, "backtest")
    assert s.status == daily.OK and s.done == [] and s.not_done == [] and s.day_return is None
    html = daily.render_daily(s)
    assert "Nada que hacer hoy" in html and "Todo lo planificado se ejecutó" in html
    assert (
        daily.caption(s)
        == "STRIDE [backtest] 2026-10-22: OK; equity $100.00 (n/d); 0 hechas, 0 no hechas"
    )
    conn.close()


def test_risk_vetoes_are_used_when_no_orders_were_recorded(tmp_path: Path) -> None:
    conn = _db(tmp_path / "x.db")
    with conn:
        conn.execute("DELETE FROM orders")
    s = daily.build_daily(conn, T2, "paper")
    assert s.not_done[0] == (
        "Vetada: venta en GLD. Motivo: orden mayor al límite por orden (ORDER_TOO_LARGE)"
    )
    conn.close()


def _config(tmp_path: Path) -> Path:
    text = (ROOT / "config.yaml").read_text(encoding="utf-8")
    path = tmp_path / "config.yaml"
    path.write_text(text.replace("db_path: data/stride.db", "db_path: x.db"), encoding="utf-8")
    return path


def test_main_writes_the_file_and_needs_telegram_to_send(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("ALERT_TELEGRAM_TOKEN", "ALERT_TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(name, raising=False)
    _db(tmp_path / "x.db").close()
    config = _config(tmp_path)
    assert daily.main(["--date", T2.isoformat(), "--config", str(config)]) == 0
    written = tmp_path / "reports" / "daily" / f"{T2}.html"
    assert written.read_text(encoding="utf-8").startswith("<!doctype html>")
    assert "ATENCIÓN" in capsys.readouterr().out
    env = tmp_path / "empty.env"
    env.write_text("", encoding="utf-8")
    args = ["--date", T2.isoformat(), "--config", str(config), "--send", "--env-file", str(env)]
    assert daily.main(args) == 2
    assert "not configured" in capsys.readouterr().out
    assert daily.main(["--date", T2.isoformat(), "--config", str(config), "--db", "nope.db"]) == 2


def test_orders_table_rejects_unknown_status(tmp_path: Path) -> None:
    conn = connect(tmp_path / "o.db")
    bad = pd.DataFrame(
        [{"date": T2, "client_id": "z", "broker_id": None, "symbol": "SPY", "side": "buy",
          "notional": 1.0, "type": "market", "limit_price": None, "rule": "HOLD", "switch": 0,
          "reasons": "", "status": "lost"}]
    )  # fmt: skip
    with pytest.raises(ValueError, match="status"):
        record_orders(conn, bad)
    assert read_table(conn, "orders").empty and read_table(conn, "fills").empty
    conn.close()


def test_orders_not_sent_are_listed_as_not_done(tmp_path: Path) -> None:
    conn = _db(tmp_path / "x.db")
    record_orders(
        conn,
        pd.DataFrame(
            [{"date": T2, "client_id": "c", "broker_id": None, "symbol": "UUP", "side": "buy",
              "notional": 20.0, "type": "market", "limit_price": None, "rule": "HOLD",
              "switch": 0, "reasons": "HALTED", "status": "skipped"},
             {"date": T2, "client_id": "d", "broker_id": None, "symbol": "TLT", "side": "buy",
              "notional": 30.0, "type": "market", "limit_price": None, "rule": "HOLD",
              "switch": 0, "reasons": "SEND_FAILED", "status": "failed"}]
        ),
    )  # fmt: skip
    s = daily.build_daily(conn, T2, "paper")
    assert any(x.startswith("No enviada: compra de $20.00 en UUP") for x in s.not_done)
    assert any("el broker no tiene la orden (SEND_FAILED)" in x for x in s.not_done)
    assert not any("UUP" in x for x in s.done)


def _make_prices(dates: Sequence[date], start: float, step: float) -> pd.DataFrame:
    rows = []
    price = start
    for d in dates:
        rows.append(
            {
                "date": pd.Timestamp(d),
                "symbol": "SPY",
                "open": price,
                "high": price * 1.01,
                "low": price * 0.99,
                "close": price,
                "volume": 1000.0,
                "source": "test",
                "fetched_at": pd.Timestamp("2026-10-01", tz="UTC"),
            }
        )
        price *= 1.0 + step
    return pd.DataFrame(rows).set_index("date")


def _make_equity(dates: Sequence[date], start: float, step: float) -> pd.DataFrame:
    eq = start
    rows = []
    for d in dates:
        rows.append(
            {
                "date": pd.Timestamp(d),
                "equity": eq,
                "cash": eq,
                "gross_exposure": 0.0,
                "leverage": 0.0,
            }
        )
        eq *= 1.0 + step
    return pd.DataFrame(rows).set_index("date")


def test_evaluate_phase4_under_40_days(tmp_path: Path) -> None:
    conn = connect(tmp_path / "p4_under.db")
    days = list(trading_days(date(2026, 1, 2), date(2026, 3, 31)))[:25]
    upsert_equity(conn, _make_equity(days, 10_000.0, 0.001))
    verdict = daily.evaluate_phase4(conn, title="STRIDE B")
    assert not verdict.ready
    assert not verdict.passed
    assert verdict.days == 25
    assert verdict.message == "STRIDE B: prueba en curso (25/40 días hábiles completados)."
    conn.close()


def test_evaluate_phase4_passed_beats_spy(tmp_path: Path) -> None:
    conn = connect(tmp_path / "p4_passed.db")
    days = list(trading_days(date(2026, 1, 2), date(2026, 3, 31)))[:40]
    upsert_equity(conn, _make_equity(days, 10_000.0, 0.005))
    upsert_prices(conn, _make_prices(days, 100.0, 0.001))
    verdict = daily.evaluate_phase4(conn, title="STRIDE B")
    assert verdict.ready
    assert verdict.passed
    assert verdict.days == 40
    assert verdict.clean_operations
    assert verdict.strategy_return > verdict.spy_return
    assert verdict.message.startswith("EN HORABUENA YA ESTA LISTO Y EL RESULTADO ES POSITIVO")
    assert "STRIDE B, 40 días hábiles" in verdict.message
    conn.close()


def test_evaluate_phase4_failed_when_spy_beats_strategy(tmp_path: Path) -> None:
    conn = connect(tmp_path / "p4_failed_spy.db")
    days = list(trading_days(date(2026, 1, 2), date(2026, 3, 31)))[:40]
    upsert_equity(conn, _make_equity(days, 10_000.0, 0.001))
    upsert_prices(conn, _make_prices(days, 100.0, 0.005))
    verdict = daily.evaluate_phase4(conn, title="STRIDE B")
    assert verdict.ready
    assert not verdict.passed
    assert verdict.message.startswith("Buena prueba amigo, pero el syp500 es mejor")
    conn.close()


def test_evaluate_phase4_failed_when_operational_issue(tmp_path: Path) -> None:
    conn = connect(tmp_path / "p4_failed_ops.db")
    days = list(trading_days(date(2026, 1, 2), date(2026, 3, 31)))[:40]
    upsert_equity(conn, _make_equity(days, 10_000.0, 0.005))
    upsert_prices(conn, _make_prices(days, 100.0, 0.001))
    record_event(conn, "KILL_SWITCH", "kill switch fired", at=f"{days[20].isoformat()}T18:00:00")
    verdict = daily.evaluate_phase4(conn, title="STRIDE B")
    assert verdict.ready
    assert not verdict.clean_operations
    assert not verdict.passed
    assert verdict.message.startswith("Buena prueba amigo, pero el syp500 es mejor")
    conn.close()


def test_cli_verdict_flag(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db_file = tmp_path / "x.db"
    conn = connect(db_file)
    days = list(trading_days(date(2026, 1, 2), date(2026, 3, 31)))[:15]
    upsert_equity(conn, _make_equity(days, 10_000.0, 0.001))
    conn.close()
    config = _config(tmp_path)
    exit_code = daily.main(["--config", str(config), "--verdict"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "STRIDE: prueba en curso (15/40 días hábiles completados)." in out
