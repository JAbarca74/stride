"""Robustness check of the round 5 candidate `crypto_dip` (owner request,
2026-10-24; pre-registration in docs/design/dip_robustness.md).

Three tests on the same data as validation section 10, each against SPY over the
same days:

1. Parameters: the walk-forward of section 9 for the candidate and for each of
   its three dip parameters moved by -20% and +20%, one at a time.
2. Extreme years, removed symmetrically: the candidate's out-of-sample returns
   without the best and the worst year, without the crypto bubbles, without the
   crashes, and without all of them.
3. Start years: one continuous account from 2012, 2015 and 2018 to the end, with
   the drawdown scaling on (without it the 80% SPY core alone can trip the kill
   switch in a crash).

    python -m stride.backtest.robustness            # writes docs/results/robustness.md

Exit code 0 when every criterion of the pre-registration holds, 1 otherwise.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path

import pandas as pd

from stride.backtest import metrics, run
from stride.backtest.engine import BacktestSettings, MarketData, run_backtest
from stride.backtest.validation import (
    _choice,
    _summary,
    _table,
    candidate_config,
    walk_forward_selected,
    with_allocation,
)
from stride.config import StrideConfig, effective_config, load_config, with_search_universe
from stride.data.leveraged import load_etf_facts
from stride.data.sources import FredRateSource
from stride.data.store import connect, last_price_date

CANDIDATE = "crypto_dip"
SHIFT = 0.20  # each parameter moved by -20% and +20%
BUBBLES = (2017, 2021)  # crypto bubble years
CRASHES = (2018, 2020, 2022)  # crash years (crypto winters, COVID, 2022)
START_YEARS = (2012, 2015, 2018)


def parameter_variants() -> dict[str, dict[str, object]]:
    """The candidate and each dip parameter moved by -20% and +20%, one at a time."""
    base: dict[str, object] = {"dip_drop": 0.25, "dip_window": 90, "take_profit": 0.30}
    out: dict[str, dict[str, object]] = {"as registered": {}}
    for key, value in base.items():
        for sign, label in ((-1, "-20%"), (1, "+20%")):
            moved = float(value) * (1 + sign * SHIFT)  # type: ignore[arg-type]
            new: object = round(moved) if isinstance(value, int) else round(moved, 4)
            out[f"{key} {label} ({new})"] = {key: new}
    return out


def exclusion_sets(per_year: Mapping[int, float]) -> dict[str, tuple[int, ...]]:
    """Years to leave out, symmetric between good and bad extremes. `per_year` is
    the candidate's out-of-sample return by year (for the best and worst)."""
    sets: dict[str, tuple[int, ...]] = {"all years": ()}
    if per_year:
        best = max(per_year, key=lambda y: per_year[y])
        worst = min(per_year, key=lambda y: per_year[y])
        sets[f"without the best ({best}) and the worst ({worst}) year"] = (best, worst)
    sets["without the crypto bubbles (2017, 2021)"] = BUBBLES
    sets["without the crashes (2018, 2020, 2022)"] = CRASHES
    sets["without all of them"] = (*BUBBLES, *CRASHES)
    return sets


def _spy_returns(data: MarketData, index: pd.DatetimeIndex) -> pd.Series:
    close = data.bars["SPY"]["close"].sort_index()
    return metrics.buy_and_hold_returns(close).reindex(index).dropna()


def compare(
    returns: pd.Series, data: MarketData, label: str, years: Sequence[int] = ()
) -> dict[str, object]:
    """Candidate against SPY on the same days, `years` left out of both."""
    index = pd.DatetimeIndex(returns.index)
    kept = returns[~index.year.isin(list(years))]
    spy = _spy_returns(data, pd.DatetimeIndex(kept.index))
    kept = kept.reindex(spy.index)
    m, s = _summary(kept, data.risk_free), _summary(spy, data.risk_free)
    return {
        "run": label,
        "cagr": m.cagr,
        "spy_cagr": s.cagr,
        "sharpe": m.sharpe,
        "spy_sharpe": s.sharpe,
        "max_drawdown": m.max_drawdown,
        "beats_spy": bool(m.sharpe > s.sharpe and m.cagr > s.cagr),
    }


@dataclass(frozen=True)
class Robustness:
    parameters: pd.DataFrame
    exclusions: pd.DataFrame
    starts: pd.DataFrame
    robust: bool


def check(
    data: MarketData,
    config: StrideConfig,
    settings: BacktestSettings,
    *,
    variants: Mapping[str, Mapping[str, object]] | None = None,
    start_years: Sequence[int] = START_YEARS,
) -> Robustness:
    """The three tests of the pre-registration and its verdict."""
    base = candidate_config(config, CANDIDATE)
    params, base_run = [], None
    for label, values in (variants or parameter_variants()).items():
        cfg = with_allocation(base, **values) if values else base
        sel = walk_forward_selected(
            data, cfg, settings.initial_cash, settings.start, settings.end, levels=(1.0,)
        )
        params.append(compare(sel.returns, data, label))
        if base_run is None:
            base_run = sel
    assert base_run is not None
    table = base_run.per_window
    per_year = {
        int(y): float(c) for y, c in zip(table["out_of_sample"], table["cagr"], strict=True)
    }
    excl = [
        compare(base_run.returns, data, label, years)
        for label, years in exclusion_sets(per_year).items()
    ]
    starts = []
    for year in start_years:
        begin = max(date(year, 1, 1), settings.start)
        if begin >= settings.end:
            continue
        result = run_backtest(
            data, _choice(base, 1.0, True), replace(settings, start=begin, fixed_leverage=1.0)
        )
        row = compare(result.returns, data, f"continuous from {year}")
        row["kill_switch"] = bool((result.events["type"] == "KILL_SWITCH").any())
        starts.append(row)
    params_df, excl_df, starts_df = (pd.DataFrame(r) for r in (params, excl, starts))
    symmetric = excl_df[excl_df["run"].str.startswith(("without the best", "without all"))]
    robust = bool(
        params_df["beats_spy"].all()
        and (symmetric["sharpe"] > symmetric["spy_sharpe"]).all()
        and (starts_df.empty or (starts_df["cagr"] > starts_df["spy_cagr"]).all())
    )
    return Robustness(params_df, excl_df, starts_df, robust)


def to_markdown(r: Robustness) -> str:
    lines = [
        "# Robustness of crypto_dip (docs/design/dip_robustness.md)",
        "",
        f"**Verdict: {'ROBUST' if r.robust else 'NOT ROBUST'}**",
        "",
        "## 1. Parameters moved by -20% and +20% (walk-forward, out-of-sample only)",
        "",
        *_table(r.parameters),
        "",
        "## 2. Extreme years left out (good and bad alike)",
        "",
        *_table(r.exclusions),
        "",
        "## 3. One continuous account from each start year (drawdown scaling on)",
        "",
        *_table(r.starts),
        "",
    ]
    return "\n".join(lines)


def main(
    argv: Sequence[str] | None = None,
    rate_source: Callable[[], pd.Series] | None = None,
) -> int:
    parser = argparse.ArgumentParser(prog="python -m stride.backtest.robustness")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--results", default="docs/results", help="output folder")
    args = parser.parse_args(argv)
    config_path = Path(args.config).resolve()
    root = config_path.parent
    config = effective_config(load_config(config_path))
    prices_db = (root / config.data.db_path).resolve()
    if not prices_db.exists():
        sys.stdout.write(f"error: no price database at {prices_db}\n")
        return 2
    rate = (rate_source or FredRateSource().fetch_rate)()
    source = connect(prices_db)
    try:
        last = last_price_date(source, "SPY")
        data, _ = run.load_market(source, with_search_universe(config), rate, load_etf_facts())
    finally:
        source.close()
    if last is None:
        sys.stdout.write("error: no SPY prices stored\n")
        return 2
    settings = BacktestSettings(
        config.data.history_start, last, config.cash_flows.initial_deposit_usd
    )
    result = check(data, config, settings)
    out = (root / args.results).resolve()
    out.mkdir(parents=True, exist_ok=True)
    (out / "robustness.md").write_text(to_markdown(result), encoding="utf-8")
    sys.stdout.write(f"verdict: {'ROBUST' if result.robust else 'NOT ROBUST'}\n")
    sys.stdout.write(f"results written to {out / 'robustness.md'}\n")
    return 0 if result.robust else 1


__all__ = ["check", "compare", "exclusion_sets", "main", "parameter_variants", "to_markdown"]

if __name__ == "__main__":
    raise SystemExit(main())
