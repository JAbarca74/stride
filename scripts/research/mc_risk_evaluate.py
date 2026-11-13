"""Gate R2 of docs/design/mc_risk_allocation.md: the daily walk-forward of the
risk-allocation variants.

Runs the 9 pre-registered variants (3 allocators x 3 brakes) on the daily research
database, in the two set-ups of the design (`U20`: the 20 assets with 10,000 USD;
`S6`: one asset per class with 1,000 USD), over the selection period (2008-2016)
and the holdout (2017 on), each starting in cash. Also the pre-registered extras:
the inverse-volatility variant sized with a plain 60-day volatility, every variant
without the portfolio loss cap, and `U20X`, the 20 assets with spot crypto as a
proxy before the crypto ETFs existed. Usage:

    python scripts/research/mc_risk_evaluate.py [--db PATH] [--out DIR] [--workers N]

The gate, fixed in the design before this script was run: the variant is selected
on the selection period only (best return over maximum drawdown among the variants
that traded and whose kill switch did not fire), and passes if (1) its kill switch
fired in neither period, (2) its net return is positive in both, (3) its costs in
the holdout are below 1% of the starting capital a year, (4) its return over
maximum drawdown in the holdout is at least that of `EW_none`, (5) at least 6 of
the 9 variants avoid the kill switch in both periods and its four neighbours have a
positive holdout return.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from mc_daily_data import CRYPTO_SPOT, UNIVERSE

from stride.backtest.costs import commission, spread_bps
from stride.config import load_config
from stride.data.calendar import trading_days
from stride.data.store import read_prices
from stride.forecast.daily import BoolArray, build_daily_panel
from stride.forecast.daily_risk import DailyRiskParams
from stride.forecast.params import WeightParams
from stride.forecast.returns import FloatArray, ReturnPanel
from stride.forecast.risk_walkforward import RiskResult, RiskTrader, run_risk_trading
from stride.forecast.risk_weights import ALLOCATORS, BRAKES, RiskRule, inverse_vol_weights
from stride.forecast.walkforward import CostModel, realized, trading_metrics

REPO_ROOT = Path(__file__).resolve().parents[2]

SEED = 20261112
PARAMS = DailyRiskParams()  # 252-session window, 2,000 paths, horizons 5 and 20
KILL_SWITCH_DD = 0.20
PERIODS = {"selection": ("2008-01-01", "2016-12-31"), "holdout": ("2017-01-01", "2100-01-01")}
# The limits of the current instance (config.paper_mc.yaml), not tuned.
GROUPS = {
    "crypto": ("IBIT", "ETHA"),
    "metals": ("GLD", "SLV", "CPER"),
    "us_equity": ("QQQ", "IWM"),
}
GROUP_CAPS = {"crypto": 0.30, "metals": 0.35, "us_equity": 0.40}
LIMITS = WeightParams(
    max_weight=0.25,
    max_total=1.0,
    groups=GROUPS,
    group_caps=GROUP_CAPS,
    risk_horizon=5,
    risk_quantile=0.01,
    max_portfolio_loss=0.08,
    min_order_usd=150.0,
)
NO_CAP = replace(LIMITS, max_portfolio_loss=10.0)  # a loss cap that never binds
# One asset per class: the lowest spread measured in docs/design/mc_universe.md among
# the 20 (US equity QQQ 0.59 bps, international equity EFA 0.96, bonds and dollar TLT
# 1.24, metals GLD 0.54, energy and agriculture USO 2.83, real estate VNQ 1.03).
S6 = ("QQQ", "EFA", "TLT", "GLD", "USO", "VNQ")
SPOT_OF = {"IBIT": "BTC-USD", "ETHA": "ETH-USD"}
PLAIN_VOL_DAYS = 60
REFERENCE = "EW_none"
MIN_SURVIVORS = 6
MAX_COST_PER_YEAR = 0.01
SHARPE_ALARM = 1.5
INVESTED = 0.01  # a day with less than this share invested is not a loss-cap case


@dataclass(frozen=True)
class Setup:
    """One set-up of the design: which assets and how much capital."""

    name: str
    symbols: tuple[str, ...]
    equity: float
    spot_proxy: bool = False


SETUPS = (
    Setup("U20", UNIVERSE, 10_000.0),
    Setup("S6", S6, 1_000.0),
    Setup("U20X", UNIVERSE, 10_000.0, spot_proxy=True),
)


def load_bars(db: Path) -> dict[str, pd.DataFrame]:
    """Daily bars of the 20 assets and the two spot series (read-only)."""
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        return {symbol: read_prices(conn, symbol) for symbol in (*UNIVERSE, *CRYPTO_SPOT)}
    finally:
        conn.close()


def with_spot_history(etf: pd.DataFrame, spot: pd.DataFrame) -> pd.DataFrame:
    """The ETF series with scaled spot bars before its first day (a proxy, not the ETF)."""
    switch = etf.index.min()
    if switch not in spot.index:
        raise ValueError(f"the spot series has no bar on {switch.date()}")
    ratio = float(etf["close"].loc[switch]) / float(spot["close"].loc[switch])
    before = spot.loc[spot.index < switch, ["open", "close"]] * ratio
    return pd.concat([before, etf[["open", "close"]]]).sort_index()


def build_panel(bars: dict[str, pd.DataFrame], setup: Setup) -> ReturnPanel:
    """The daily panel of a set-up."""
    chosen = {symbol: bars[symbol] for symbol in setup.symbols}
    if setup.spot_proxy:
        for etf, spot in SPOT_OF.items():
            chosen[etf] = with_spot_history(bars[etf], bars[spot])
    first = min(frame.index.min() for frame in chosen.values())
    last = max(bars[symbol].index.max() for symbol in setup.symbols)
    return build_daily_panel(chosen, trading_days(first.date(), last.date()))


def build_costs(symbols: tuple[str, ...]) -> CostModel:
    """The cost model of `config.yaml` (read, not changed), as B, DIP and paper_mc."""
    cfg = load_config(REPO_ROOT / "config.yaml")
    spreads = np.array([spread_bps(symbol, cfg) for symbol in symbols])
    return CostModel(spreads, lambda shares, price: commission(shares, price, cfg))


def plain_vol_weigher(panel: ReturnPanel, row: int, valid: BoolArray) -> FloatArray:
    """Inverse of the standard deviation of the last 60 daily returns (the ablation)."""
    window = panel.ret[row - PLAIN_VOL_DAYS + 1 : row + 1][:, valid]
    return inverse_vol_weights(window.std(axis=0))


def make_traders(setup: Setup, costs: CostModel) -> list[tuple[str, RiskTrader]]:
    """The 9 variants, then the extras; each with its kind."""
    size = len(setup.symbols)

    def trader(rule: RiskRule, weigher: Any = None) -> RiskTrader:
        return RiskTrader(
            rule,
            costs,
            initial_equity=setup.equity,
            kill_switch_dd=KILL_SWITCH_DD,
            size=size,
            weigher=weigher,
        )

    out: list[tuple[str, RiskTrader]] = []
    for allocator in ALLOCATORS:
        for brake in BRAKES:
            out.append(("main", trader(RiskRule(f"{allocator}_{brake}", allocator, brake, LIMITS))))
    out.append(
        ("plain_vol", trader(RiskRule("IV60_none", "IV", "none", LIMITS), plain_vol_weigher))
    )
    for allocator in ALLOCATORS:
        for brake in BRAKES:
            rule = RiskRule(f"{allocator}_{brake}_nocap", allocator, brake, NO_CAP)
            out.append(("no_cap", trader(rule)))
    return out


def held_exceedance(panel: ReturnPanel, result: RiskResult) -> dict[str, float]:
    """How often the weights actually held lost more over 5 sessions than their own
    simulated 5% and 1% quantiles (cash earns nothing; no trading in between)."""
    log, held = result.log, result.held.to_numpy()
    hits05, hits01, cases = 0, 0, 0
    for index, row in enumerate(log["row"].to_numpy()):
        if held[index].sum() < INVESTED:
            continue
        outcome = realized(panel, int(row), LIMITS.risk_horizon)
        used = held[index] > 0
        if not np.isfinite(outcome[used]).all():
            continue
        value = float(outcome[used] @ held[index][used])
        cases += 1
        hits05 += value < log["held_q05"].iloc[index]
        hits01 += value < log["held_q01"].iloc[index]
    if cases == 0:
        return {"held_cases": 0.0, "held_exceed_q05": np.nan, "held_exceed_q01": np.nan}
    return {
        "held_cases": float(cases),
        "held_exceed_q05": hits05 / cases,
        "held_exceed_q01": hits01 / cases,
    }


def run_job(task: tuple[dict[str, pd.DataFrame], Setup, str]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """One set-up over one period: a table of runs and the equity curves."""
    bars, setup, period = task
    panel = build_panel(bars, setup)
    begin, end = PERIODS[period]
    start = int(np.searchsorted(panel.day, pd.Timestamp(begin)))
    stop = int(np.searchsorted(panel.day, pd.Timestamp(end), side="right"))
    kinds_traders = make_traders(setup, build_costs(setup.symbols))
    results = run_risk_trading(
        panel, start, stop, [t for _, t in kinds_traders], params=PARAMS, base_seed=SEED
    )
    rows, curves = [], {}
    for (kind, _), result in zip(kinds_traders, results, strict=True):
        trading = result.trading
        metrics = trading_metrics(trading, setup.equity)
        kills = trading.trades[trading.trades["reason"] == "KILL_SWITCH"]
        log = result.log
        years = len(trading.equity) / 252.0
        rows.append(
            {
                "setup": setup.name,
                "period": period,
                "variant": result.rule.name,
                "kind": kind,
                "years": years,
                **metrics,
                "return_over_dd": (
                    metrics["total_return"] / metrics["max_drawdown"]
                    if metrics["max_drawdown"] > 0
                    else np.nan
                ),
                "costs_pct_per_year": metrics["costs_pct_of_initial"] / years,
                "kill_day": (
                    "" if kills.empty else pd.Timestamp(kills["end"].iloc[0]).date().isoformat()
                ),
                "cap_binding_share": float((log["cap_scale"] < 1.0).mean()) if len(log) else 0.0,
                "min_scale": float(log["scale"].min()) if len(log) else 1.0,
                "mean_positions": float((result.held > 0).sum(axis=1).mean()) if len(log) else 0.0,
                **held_exceedance(panel, result),
            }
        )
        curves[result.rule.name] = trading.equity["equity"]
    frame = pd.DataFrame(curves)
    frame.insert(0, "period", period)
    frame.insert(0, "setup", setup.name)
    return pd.DataFrame(rows), frame


def yearly_returns(curves: pd.DataFrame, equity: float) -> pd.DataFrame:
    """Return of each calendar year for every variant of one set-up and period."""
    values = curves.drop(columns=["setup", "period"])
    last = values.groupby(pd.DatetimeIndex(values.index).year).last()
    previous = last.shift(1)
    previous.iloc[0] = equity
    out = last / previous - 1.0
    out.index.name = "year"
    return out.reset_index()


def gate(runs: pd.DataFrame, setup: str) -> dict[str, Any]:
    """The R2 verdict for one set-up."""
    main = runs[(runs["setup"] == setup) & (runs["kind"] == "main")]
    sel = main[main["period"] == "selection"].set_index("variant")
    hold = main[main["period"] == "holdout"].set_index("variant")
    survivors = [v for v in sel.index if sel.loc[v, "killed"] == 0 and hold.loc[v, "killed"] == 0]
    candidates = sel[(sel["killed"] == 0) & (sel["trades"] > 0) & sel["return_over_dd"].notna()]
    out: dict[str, Any] = {
        "setup": setup,
        "variants_without_kill_switch": len(survivors),
        "survivors": survivors,
    }
    if candidates.empty:
        out.update({"selected": None, "passed": False, "checks": []})
        return out
    selected = str(candidates["return_over_dd"].idxmax())
    allocator, brake = selected.split("_")
    neighbours = [f"{allocator}_{b}" for b in BRAKES if b != brake]
    neighbours += [f"{a}_{brake}" for a in ALLOCATORS if a != allocator]
    s, h, ref = sel.loc[selected], hold.loc[selected], hold.loc[REFERENCE]
    ref_ratio = float(ref["return_over_dd"])
    checks = [
        {
            "check": "1 kill switch fired in neither period",
            "value": f"selection {int(s['killed'])}, holdout {int(h['killed'])}",
            "passed": bool(s["killed"] == 0 and h["killed"] == 0),
        },
        {
            "check": "2 net return positive in both periods",
            "value": f"selection {s['total_return']:+.4f}, holdout {h['total_return']:+.4f}",
            "passed": bool(s["total_return"] > 0 and h["total_return"] > 0),
        },
        {
            "check": "3 holdout costs below 1% of starting capital a year",
            "value": f"{h['costs_pct_per_year']:.4f}",
            "passed": bool(h["costs_pct_per_year"] < MAX_COST_PER_YEAR),
        },
        {
            "check": f"4 holdout return over drawdown at least that of {REFERENCE}",
            "value": f"{h['return_over_dd']:.3f} against {ref_ratio:.3f}",
            "passed": bool(h["return_over_dd"] >= ref_ratio),
        },
        {
            "check": "5a at least 6 of 9 variants avoid the kill switch in both periods",
            "value": f"{len(survivors)} of 9",
            "passed": len(survivors) >= MIN_SURVIVORS,
        },
        {
            "check": "5b the four neighbours have a positive holdout return",
            "value": ", ".join(f"{n} {hold.loc[n, 'total_return']:+.4f}" for n in neighbours),
            "passed": bool(all(hold.loc[n, "total_return"] > 0 for n in neighbours)),
        },
    ]
    out.update(
        {
            "selected": selected,
            "passed": all(check["passed"] for check in checks),
            "checks": checks,
            "sharpe_alarm": bool(h["sharpe"] > SHARPE_ALARM),
        }
    )
    return out


def main() -> None:
    """Run every set-up and period, write the tables and print the verdicts."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=REPO_ROOT / "data" / "mc_daily_research.db")
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "reports" / "mc_risk")
    parser.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    args = parser.parse_args()
    bars = load_bars(args.db)
    tasks = [(bars, setup, period) for setup in SETUPS for period in PERIODS]
    with ProcessPoolExecutor(max_workers=min(args.workers, len(tasks))) as executor:
        results = list(executor.map(run_job, tasks))
    runs = pd.concat([table for table, _ in results], ignore_index=True)
    args.out.mkdir(parents=True, exist_ok=True)
    runs.to_csv(args.out / "risk_runs.csv", index=False)
    yearly = []
    for (_, setup, period), (_, curves) in zip(tasks, results, strict=True):
        curves.to_csv(args.out / f"equity_{setup.name}_{period}.csv")
        table = yearly_returns(curves, setup.equity)
        table.insert(0, "period", period)
        table.insert(0, "setup", setup.name)
        yearly.append(table)
    pd.concat(yearly, ignore_index=True).to_csv(args.out / "risk_yearly.csv", index=False)
    verdicts = [gate(runs, setup.name) for setup in SETUPS]
    summary = {
        "seed": SEED,
        "window_days": PARAMS.window_days,
        "n_paths": PARAMS.n_paths,
        "kill_switch_dd": KILL_SWITCH_DD,
        "periods": PERIODS,
        "last_day": max(bars[s].index.max() for s in UNIVERSE).date().isoformat(),
        "s6": list(S6),
        "gates": verdicts,
    }
    (args.out / "r2_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    pd.set_option("display.width", 250)
    shown = [
        "setup",
        "period",
        "variant",
        "total_return",
        "cagr",
        "volatility",
        "sharpe",
        "max_drawdown",
        "return_over_dd",
        "trades",
        "costs_pct_per_year",
        "avg_exposure",
        "killed",
        "kill_day",
    ]
    print(runs[runs["kind"] == "main"][shown].round(4).to_string(index=False))
    for verdict in verdicts:
        print(f"\n{verdict['setup']}: selected {verdict['selected']}")
        for check in verdict["checks"]:
            mark = "ok  " if check["passed"] else "FAIL"
            print(f"  {mark} {check['check']}: {check['value']}")
        print(f"  GATE R2 ({verdict['setup']}):", "PASSED" if verdict["passed"] else "NOT MET")


if __name__ == "__main__":
    main()
