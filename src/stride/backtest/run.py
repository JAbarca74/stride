"""Run the backtest on the stored prices and write its tables (SPEC sections 9 and 10).

Usage, from the repository root (needs `data/stride.db` from `stride.data.update`
and network access to FRED for the DTB3 rate; no API key):

    python -m stride.backtest.run                         # 2007 to the last stored day
    python -m stride.backtest.run --fixed-leverage 2      # leverage comparison run
    python -m stride.backtest.run --report reports/backtest.html

Results go to a separate database (`data/backtest.db` by default) with the
signals, risk_decisions, equity, events and cash_flows tables plus the prices the
report needs. It never writes to the price database, whose equity and
risk_state tables belong to the paper or live account.
"""

from __future__ import annotations

import argparse
import math
import sqlite3
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pandas as pd

from stride.backtest import metrics
from stride.backtest.engine import (
    BacktestResult,
    BacktestSettings,
    MarketData,
    eligible_instruments,
    run_backtest,
)
from stride.config import StrideConfig, load_config
from stride.data import store
from stride.data.leveraged import (
    EtfFacts,
    calibrate_spread,
    expense_ratio_for,
    leveraged_bars,
    load_etf_facts,
    passes_filters,
    rate_known_at_t_minus_1,
)
from stride.data.sources import FredRateSource
from stride.data.store import (
    connect,
    last_price_date,
    read_prices,
    record_cash_flow,
    record_event,
    upsert_equity,
    upsert_prices,
    upsert_risk_decisions,
    upsert_signals,
)
from stride.data.update import series_for

RESULT_TABLES = (
    "equity",
    "signals",
    "risk_decisions",
    "events",
    "cash_flows",
    "orders",
    "fills",
)
OHLC = ["open", "high", "low", "close"]


@dataclass(frozen=True)
class Calibrated:
    etf: str
    multiple: int
    spread: float
    tracking_diff_per_year: float


def load_market(
    conn: sqlite3.Connection,
    config: StrideConfig,
    rate: pd.Series,
    facts: Mapping[str, EtfFacts],
) -> tuple[MarketData, list[Calibrated]]:
    """Universe bars (crypto spliced with spot) and, for every leveraged ETF that
    passes the filters and has stored bars, real bars with a synthetic history
    before launch (SPEC 9), its financing spread calibrated on the overlap."""
    bars: dict[str, pd.DataFrame] = {}
    for symbol in config.universe:
        series = series_for(conn, symbol)
        if not series.empty:
            bars[symbol] = series[OHLC]
    passing = {t for t, f in facts.items() if passes_filters(f, config.leveraged_etf_filters)}
    calibrated = []
    for asset, versions in config.leveraged_etfs.items():
        if asset not in bars:
            continue
        for multiple, etf in versions.items():
            real = read_prices(conn, etf)
            if etf not in passing or len(real) < 2:  # noqa: PLR2004
                continue
            base = bars[asset]
            fee = expense_ratio_for(etf, facts, config.costs.leveraged_etf_fee)
            aligned = rate_known_at_t_minus_1(rate, pd.DatetimeIndex(base.index))
            cal = calibrate_spread(base["close"], real["close"], multiple, aligned, fee)
            bars[etf] = leveraged_bars(
                base, real, multiple, rate=aligned, spread=cal.spread, annual_fee=fee
            )[OHLC]
            calibrated.append(Calibrated(etf, multiple, cal.spread, cal.tracking_diff_per_year))
    eligible = eligible_instruments(config, passing, set(bars))
    return MarketData(bars, rate, eligible), calibrated


def order_rows(result: BacktestResult) -> pd.DataFrame:
    """The run's orders as rows of the `orders` table, with their final status:
    vetoed by the guard, filled, or rejected or cancelled by the broker."""
    orders = result.orders
    if orders.empty:
        return pd.DataFrame(columns=list(store.ORDER_COLUMNS))
    filled = set(result.fills["order_id"]) if not result.fills.empty else set()
    outcome: dict[str, str] = {}
    for kind, detail in zip(result.events["type"], result.events["detail"], strict=True):
        if kind in {"ORDER_REJECTED", "ORDER_CANCELLED"}:
            outcome[str(detail).split(" ", 1)[0]] = kind.removeprefix("ORDER_").lower()

    def status(row: pd.Series) -> str:
        if not row["approved"]:
            return "vetoed"
        if row["order_id"] in filled:
            return "filled"
        return outcome.get(str(row["order_id"]), "pending")

    rows = orders.assign(
        client_id=[f"bt-{pd.Timestamp(d).date()}-{i}" for i, d in enumerate(orders["date"])],
        broker_id=orders["order_id"],
        status=orders.apply(status, axis=1),
        switch=orders["switch"].astype(int),
    )
    return rows[list(store.ORDER_COLUMNS)]


def write_results(
    conn: sqlite3.Connection, result: BacktestResult, prices: Mapping[str, pd.DataFrame]
) -> None:
    """Replace the result tables in `conn` with this run, and store the prices the
    report needs (universe and benchmarks)."""
    with conn:
        for table in RESULT_TABLES:
            conn.execute(f"DELETE FROM {table}")
        conn.execute("DELETE FROM prices")
    upsert_equity(conn, result.equity)
    if not result.signals.empty:
        upsert_signals(conn, result.signals)
    if not result.risk_decisions.empty:
        upsert_risk_decisions(conn, result.risk_decisions)
    for row in result.events.itertuples(index=False):
        record_event(conn, str(row.type), str(row.detail), at=str(row.at))
    for day, amount in zip(
        pd.DatetimeIndex(result.cash_flows.index), result.cash_flows, strict=True
    ):
        record_cash_flow(conn, day.date(), float(amount), "deposit", "backtest monthly deposit")
    orders = order_rows(result)
    if not orders.empty:
        store.record_orders(conn, orders)
    if not result.fills.empty:
        store.record_fills(conn, result.fills.rename(columns={"order_id": "broker_id"}))
    for frame in prices.values():
        upsert_prices(conn, frame)


@dataclass(frozen=True)
class Summary:
    strategy: metrics.Metrics
    spy: metrics.Metrics | None
    sixty_forty: metrics.Metrics | None
    final_equity: float
    deposited: float
    turnover: float
    cost_ratio: float
    average_exposure: float
    orders: int
    vetoes: int
    rejected: int
    kill_switch: str | None
    switches: int
    switch_cost: float


def summarize(result: BacktestResult, bars: Mapping[str, pd.DataFrame], rate: pd.Series) -> Summary:
    """Section 9 metrics of the run and of the benchmarks over the same days."""
    returns = result.returns
    rf = rate_known_at_t_minus_1(rate, pd.DatetimeIndex(returns.index))
    days = pd.DatetimeIndex(result.equity.index)

    def bench(series: pd.Series) -> metrics.Metrics:
        return metrics.summary(series, rf.reindex(series.index).fillna(0.0))

    spy = ief = None
    if "SPY" in bars:
        spy = bars["SPY"]["close"].reindex(days).dropna()
    if "IEF" in bars:
        ief = bars["IEF"]["close"].reindex(days).dropna()
    equity = result.equity["equity"]
    fills = result.fills
    traded = fills.set_index("date")["notional"] if not fills.empty else pd.Series(dtype=float)
    costs = (
        (fills["commission"] + fills["spread_cost"]).set_axis(fills["date"])
        if not fills.empty
        else pd.Series(dtype=float)
    )
    events = result.events
    kill = events[events["type"] == "KILL_SWITCH"]
    orders = result.orders
    return Summary(
        strategy=metrics.summary(returns, rf),
        spy=bench(metrics.buy_and_hold_returns(spy)) if spy is not None and len(spy) > 2 else None,  # noqa: PLR2004
        sixty_forty=bench(metrics.sixty_forty_returns(spy, ief))
        if spy is not None and ief is not None and len(spy) > 2  # noqa: PLR2004
        else None,
        final_equity=float(equity.iloc[-1]),
        deposited=float(equity.iloc[0]) + float(result.cash_flows.sum()),
        turnover=metrics.turnover(traded, equity),
        cost_ratio=metrics.cost_ratio(costs, equity),
        average_exposure=metrics.average_exposure(result.equity["gross_exposure"], equity),
        orders=len(orders),
        vetoes=int((~orders["approved"]).sum()) if not orders.empty else 0,
        rejected=int(events["type"].isin(["ORDER_REJECTED"]).sum()),
        kill_switch=str(kill["at"].iloc[0]) if not kill.empty else None,
        switches=int(result.switches["switches"].sum()) if not result.switches.empty else 0,
        switch_cost=float(result.switches["cost_usd"].sum()) if not result.switches.empty else 0.0,
    )


def _pct(value: float) -> str:
    return "n/a" if math.isnan(value) else f"{value:.2%}"


def _num(value: float) -> str:
    return "n/a" if math.isnan(value) else f"{value:.2f}"


def format_summary(summary: Summary) -> list[str]:
    lines = ["series   CAGR     vol      Sharpe  Sortino  maxDD    Calmar"]
    for name, m in (("STRIDE", summary.strategy), ("SPY", summary.spy),
                    ("60/40", summary.sixty_forty)):  # fmt: skip
        if m is None:
            continue
        lines.append(
            f"{name:8} {_pct(m.cagr):8} {_pct(m.volatility):8} {_num(m.sharpe):7} "
            f"{_num(m.sortino):8} {_pct(m.max_drawdown):8} {_num(m.calmar)}"
        )
    lines += [
        f"final equity ${summary.final_equity:,.2f}, deposited ${summary.deposited:,.2f}",
        f"turnover {_num(summary.turnover)}x/yr, costs {_pct(summary.cost_ratio)}/yr, "
        f"average exposure {_num(summary.average_exposure)}x",
        f"orders {summary.orders}, vetoed {summary.vetoes}, rejected by the broker "
        f"{summary.rejected}",
        f"instrument switches {summary.switches}, cost ${summary.switch_cost:,.2f}",
        f"kill switch: {summary.kill_switch or 'never fired'}",
    ]
    return lines


def _print(line: str) -> None:
    sys.stdout.write(line + "\n")


def main(
    argv: Sequence[str] | None = None,
    rate_source: Callable[[], pd.Series] | None = None,
) -> int:
    parser = argparse.ArgumentParser(prog="python -m stride.backtest.run")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--start", type=date.fromisoformat, help="default: data.history_start")
    parser.add_argument("--end", type=date.fromisoformat, help="default: last stored SPY bar")
    parser.add_argument(
        "--initial-cash", type=float, help="default: cash_flows.initial_deposit_usd"
    )
    parser.add_argument("--order-type", choices=["market", "limit"], default="market")
    parser.add_argument("--fixed-leverage", type=float, help="replace the dynamic L (SPEC 9)")
    parser.add_argument("--out", default="data/backtest.db", help="results database")
    parser.add_argument("--report", help="also write the HTML report to this path")
    args = parser.parse_args(argv)

    config_path = Path(args.config).resolve()
    config = load_config(config_path)
    root = config_path.parent
    prices_db = (root / config.data.db_path).resolve()
    out_db = (root / args.out).resolve()
    if out_db == prices_db:
        _print("error: --out must not be the price database (it holds the account's tables)")
        return 2
    if not prices_db.exists():
        _print(f"error: no price database at {prices_db}; run stride.data.update prices first")
        return 2
    rate = (rate_source or FredRateSource().fetch_rate)()
    source = connect(prices_db)
    try:
        last = last_price_date(source, "SPY")
        data, calibrated = load_market(source, config, rate, load_etf_facts())
        report_prices = {s: series_for(source, s) for s in config.universe}
    finally:
        source.close()
    if last is None:
        _print("error: no SPY prices stored")
        return 2
    settings = BacktestSettings(
        start=args.start or config.data.history_start,
        end=args.end or last,
        initial_cash=args.initial_cash or config.cash_flows.initial_deposit_usd,
        order_type=args.order_type,
        fixed_leverage=args.fixed_leverage,
    )
    for c in calibrated:
        _print(
            f"{c.etf:5} {c.multiple}x spread={c.spread:.2%} diff={c.tracking_diff_per_year:.2%}/yr"
        )
    result = run_backtest(data, config, settings)
    out = connect(out_db)
    try:
        write_results(out, result, {s: f for s, f in report_prices.items() if not f.empty})
        if args.report:
            from stride.reporting.report import write_report  # noqa: PLC0415 - matplotlib

            rf = float(rate.iloc[-1]) if len(rate) else 0.0
            write_report(out, config, settings.start, settings.end, args.report, risk_free=rf)
    finally:
        out.close()
    for line in format_summary(summarize(result, data.bars, rate)):
        _print(line)
    _print(f"results written to {out_db}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
