"""Gate R1 of docs/design/mc_risk_allocation.md: risk calibration on daily data.

Runs the unchanged simulator with no direction on the daily research database
(`mc_daily_data.py`) at every session since 2008 and compares the simulated risk
with what happened: per asset, the coverage of the simulated 90% interval at 5 and
20 sessions; for the equal-weight portfolio of the as-of universe, how often the
realized loss went beyond the simulated 5% and 1% quantiles. Usage:

    python scripts/research/mc_daily_calibration.py [--db PATH] [--out DIR]
        [--every 1] [--workers N]

The gate, fixed in the design before this script was run:

1. coverage of the 90% interval between 0.87 and 0.93 at 5 and 20 sessions, in the
   selection period and in the holdout, all assets pooled (as in the 30-minute
   gate; the per-asset figures are reported next to it);
2. the realized 5-session loss of the portfolio goes beyond the simulated 5%
   quantile in at most 7.5% of the evaluations and beyond the 1% quantile in at
   most 2%, in each period.

2008-2009 and 2020 are reported apart and do not gate.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from mc_daily_data import UNIVERSE

from stride.data.calendar import trading_days
from stride.data.store import read_prices
from stride.forecast.daily import build_daily_panel
from stride.forecast.daily_risk import DailyRiskParams, calibration_cases, rate_interval
from stride.forecast.returns import ReturnPanel

REPO_ROOT = Path(__file__).resolve().parents[2]

SEED = 20261112
PARAMS = DailyRiskParams()  # 252-session window, 2,000 paths, horizons 5 and 20
SELECTION = ("2008-01-01", "2016-12-31")
HOLDOUT_START = "2017-01-01"
STRESS = {"2008-2009": ("2008-01-01", "2009-12-31"), "2020": ("2020-01-01", "2020-12-31")}
COVERAGE_RANGE = (0.87, 0.93)
GATE_HORIZON = 5  # sessions of the portfolio loss check
LOSS_LIMITS = {"q05": 0.075, "q01": 0.02}  # nominal 5% and 1%
NOMINAL = {"q05": 0.05, "q01": 0.01}
LOSS_CAP = 0.08  # the portfolio loss cap of the current instance, for context only
CHUNK = 100


def load_panel(db: Path) -> ReturnPanel:
    """The daily panel of the 20 assets from the research database (read-only)."""
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        bars = {symbol: read_prices(conn, symbol) for symbol in UNIVERSE}
    finally:
        conn.close()
    first = min(frame.index.min() for frame in bars.values())
    last = max(frame.index.max() for frame in bars.values())
    return build_daily_panel(bars, trading_days(first.date(), last.date()))


def _task(task: tuple[ReturnPanel, list[int]]) -> tuple[pd.DataFrame, pd.DataFrame]:
    panel, rows = task
    return calibration_cases(panel, rows, PARAMS, SEED)


def run_cases(panel: ReturnPanel, every: int, workers: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Every `every`-th session from the start of the selection period, in parallel."""
    start = int(np.searchsorted(panel.day, pd.Timestamp(SELECTION[0])))
    rows = list(range(start, len(panel), every))
    chunks = [rows[i : i + CHUNK] for i in range(0, len(rows), CHUNK)]
    with ProcessPoolExecutor(max_workers=workers) as executor:
        results = list(executor.map(_task, [(panel, chunk) for chunk in chunks]))
    assets = pd.concat([a for a, _ in results], ignore_index=True)
    portfolio = pd.concat([p for _, p in results], ignore_index=True)
    return assets, portfolio


def periods(days: pd.Series) -> dict[str, pd.Series]:
    """Boolean masks of the two gate periods and the stress windows."""
    out = {
        "selection": (days >= SELECTION[0]) & (days <= SELECTION[1]),
        "holdout": days >= HOLDOUT_START,
    }
    for name, (start, end) in STRESS.items():
        out[name] = (days >= start) & (days <= end)
    return out


def coverage_tables(assets: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Coverage of the 90% interval: all assets pooled, and asset by asset."""
    rng = np.random.default_rng(0)
    inside = ((assets["realized"] >= assets["q05"]) & (assets["realized"] <= assets["q95"])).astype(
        float
    )
    months = assets["day"].dt.strftime("%Y-%m")
    pooled, single = [], []
    for period, mask in periods(assets["day"]).items():
        for horizon in PARAMS.horizons:
            pick = mask & (assets["horizon"] == horizon)
            stat = rate_interval(inside[pick].to_numpy(), months[pick].to_numpy(), rng)
            pooled.append(
                {
                    "period": period,
                    "horizon": horizon,
                    "cases": stat.cases,
                    "coverage90": stat.rate,
                    "ci_low": stat.low,
                    "ci_high": stat.high,
                }
            )
            for symbol, group in inside[pick].groupby(assets["symbol"][pick]):
                single.append(
                    {
                        "period": period,
                        "horizon": horizon,
                        "symbol": symbol,
                        "cases": len(group),
                        "coverage90": float(group.mean()),
                    }
                )
    return pd.DataFrame(pooled), pd.DataFrame(single)


def portfolio_table(portfolio: pd.DataFrame) -> pd.DataFrame:
    """How often the realized portfolio return fell below each simulated quantile."""
    rng = np.random.default_rng(1)
    months = portfolio["day"].dt.strftime("%Y-%m")
    out = []
    for period, mask in periods(portfolio["day"]).items():
        for horizon in PARAMS.horizons:
            pick = mask & (portfolio["horizon"] == horizon)
            for quantile, nominal in NOMINAL.items():
                hits = (portfolio["realized"][pick] < portfolio[quantile][pick]).astype(float)
                stat = rate_interval(hits.to_numpy(), months[pick].to_numpy(), rng)
                out.append(
                    {
                        "period": period,
                        "horizon": horizon,
                        "quantile": quantile,
                        "nominal": nominal,
                        "cases": stat.cases,
                        "exceedance": stat.rate,
                        "ci_low": stat.low,
                        "ci_high": stat.high,
                        "mean_assets": float(portfolio["assets"][pick].mean()),
                        "worst_realized": float(portfolio["realized"][pick].min()),
                        "mean_simulated_quantile": float(portfolio[quantile][pick].mean()),
                    }
                )
    return pd.DataFrame(out)


def breach_table(portfolio: pd.DataFrame) -> pd.DataFrame:
    """Per year: evaluations whose realized portfolio return fell below the simulated
    1% quantile, how far beyond it they went, and the worst outcomes of the year."""
    out = []
    for horizon in PARAMS.horizons:
        cases = portfolio[portfolio["horizon"] == horizon]
        for year, group in cases.groupby(cases["day"].dt.year):
            breach = group[group["realized"] < group["q01"]]
            ratio = breach["realized"] / breach["q01"]
            out.append(
                {
                    "horizon": horizon,
                    "year": int(year),
                    "evaluations": len(group),
                    "breaches_q01": len(breach),
                    "median_realized_over_q01": float(ratio.median()) if len(breach) else np.nan,
                    "max_realized_over_q01": float(ratio.max()) if len(breach) else np.nan,
                    "worst_realized": float(group["realized"].min()),
                    "worst_simulated_q01": float(group["q01"].min()),
                    "share_q01_beyond_cap": float((group["q01"] < -LOSS_CAP).mean()),
                }
            )
    return pd.DataFrame(out)


def gate(pooled: pd.DataFrame, losses: pd.DataFrame) -> dict[str, Any]:
    """The R1 verdict from the two tables."""
    checks = []
    for period in ("selection", "holdout"):
        for horizon in PARAMS.horizons:
            row = pooled[(pooled["period"] == period) & (pooled["horizon"] == horizon)].iloc[0]
            value = float(row["coverage90"])
            checks.append(
                {
                    "check": f"coverage90 h={horizon} {period}",
                    "value": value,
                    "limit": f"{COVERAGE_RANGE[0]} to {COVERAGE_RANGE[1]}",
                    "passed": bool(COVERAGE_RANGE[0] <= value <= COVERAGE_RANGE[1]),
                }
            )
        for quantile, limit in LOSS_LIMITS.items():
            row = losses[
                (losses["period"] == period)
                & (losses["horizon"] == GATE_HORIZON)
                & (losses["quantile"] == quantile)
            ].iloc[0]
            value = float(row["exceedance"])
            checks.append(
                {
                    "check": f"portfolio {quantile} exceedance h={GATE_HORIZON} {period}",
                    "value": value,
                    "limit": f"at most {limit}",
                    "passed": bool(value <= limit),
                }
            )
    return {"passed": all(check["passed"] for check in checks), "checks": checks}


def main() -> None:
    """Run gate R1 and write its tables."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=REPO_ROOT / "data" / "mc_daily_research.db")
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "reports" / "mc_daily")
    parser.add_argument("--every", type=int, default=1, help="evaluate every n-th session")
    parser.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    args = parser.parse_args()
    panel = load_panel(args.db)
    assets, portfolio = run_cases(panel, args.every, args.workers)
    pooled, single = coverage_tables(assets)
    losses = portfolio_table(portfolio)
    verdict = gate(pooled, losses)
    args.out.mkdir(parents=True, exist_ok=True)
    assets.to_csv(args.out / "r1_asset_cases.csv", index=False)
    portfolio.to_csv(args.out / "r1_portfolio_cases.csv", index=False)
    pooled.to_csv(args.out / "r1_coverage.csv", index=False)
    single.to_csv(args.out / "r1_coverage_by_asset.csv", index=False)
    losses.to_csv(args.out / "r1_portfolio_losses.csv", index=False)
    breach_table(portfolio).to_csv(args.out / "r1_breaches_by_year.csv", index=False)
    summary = {
        "seed": SEED,
        "every": args.every,
        "window_days": PARAMS.window_days,
        "n_paths": PARAMS.n_paths,
        "first_day": panel.day[0].date().isoformat(),
        "last_day": panel.day[-1].date().isoformat(),
        "evaluations": int(portfolio["row"].nunique()),
        "gate": verdict,
    }
    (args.out / "r1_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    pd.set_option("display.width", 200)
    print(pooled.round(4).to_string(index=False))
    print(losses.round(4).to_string(index=False))
    for check in verdict["checks"]:
        mark = "ok  " if check["passed"] else "FAIL"
        print(f"{mark} {check['check']}: {check['value']:.4f} ({check['limit']})")
    print("GATE R1:", "PASSED" if verdict["passed"] else "NOT MET")


if __name__ == "__main__":
    main()
