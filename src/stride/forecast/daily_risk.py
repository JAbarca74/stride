"""Risk calibration on daily data: gate R1 of docs/design/mc_risk_allocation.md.

The 30-minute simulator (`standardize` + `simulate`) is run unchanged on a daily
panel with no direction (drift shrink 0, the null model). For each evaluation this
module records, per asset, whether the outcome fell inside the simulated 90%
interval and, for an equal-weight portfolio of the as-of universe, whether the
realized loss went beyond the simulated 5% and 1% quantiles. Pure: no network,
clock or files; the same inputs and seed give the same output.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
import pandas as pd

from stride.forecast.daily import DAILY_SLOTS, BoolArray, eligible
from stride.forecast.returns import FloatArray, ReturnPanel, standardize
from stride.forecast.simulate import simulate
from stride.forecast.walkforward import realized, seed_for

INTERVAL_TAIL = 0.05  # the simulated 90% interval is [5%, 95%]
LOSS_QUANTILES = (0.05, 0.01)  # the portfolio loss quantiles of the gate

ASSET_COLUMNS = ("row", "day", "symbol", "horizon", "q05", "q95", "realized")
PORTFOLIO_COLUMNS = ("row", "day", "horizon", "assets", "q05", "q01", "realized")


@dataclass(frozen=True)
class DailyRiskParams:
    """How the daily futures are simulated (design section 3)."""

    window_days: int = 252  # history used, in sessions
    vol_span_days: int = 60  # EWMA span of the volatility filter
    mean_block_days: float = 2.0  # mean length of a resampled run
    n_paths: int = 2000
    horizons: tuple[int, ...] = (5, 20)  # sessions

    def __post_init__(self) -> None:
        if self.window_days < 20:  # noqa: PLR2004
            raise ValueError("window_days must be at least 20")
        if self.n_paths < 1:
            raise ValueError("n_paths must be positive")
        if not self.horizons or min(self.horizons) < 1:
            raise ValueError("horizons must be positive session counts")
        if tuple(sorted(set(self.horizons))) != self.horizons:
            raise ValueError("horizons must be increasing and unique")


def simulate_joint(
    panel: ReturnPanel, row: int, params: DailyRiskParams, seed: int
) -> tuple[BoolArray, FloatArray]:
    """Joint simulated simple returns from what was known at the close of `row`.

    Returns `valid` [N] (the as-of universe: assets with a full window ending at
    `row`) and the paths [len(horizons), n_paths, valid.sum()]. Every asset uses the
    same historical dates in a path, which keeps their correlations. Only rows up
    to `row` are read.
    """
    valid = eligible(panel, row, params.window_days)
    count = int(valid.sum())
    if count == 0:
        return valid, np.empty((len(params.horizons), params.n_paths, 0))
    start = row - params.window_days + 1
    window = panel.ret[start : row + 1][:, valid]
    slot = panel.slot[start : row + 1]
    filtered = standardize(window, slot, params.vol_span_days)
    log_paths = simulate(
        filtered,
        slot,
        start_slot=0,
        horizons=params.horizons,
        n_paths=params.n_paths,
        mean_block=params.mean_block_days,
        drift_shrink=0.0,
        vol_span=params.vol_span_days,
        rng=np.random.default_rng(seed),
        n_slots=DAILY_SLOTS,
    )
    paths: FloatArray = np.expm1(log_paths)
    return valid, paths


def portfolio_quantiles(
    joint: FloatArray, weights: FloatArray, quantiles: Sequence[float]
) -> FloatArray:
    """Quantiles of the simulated portfolio return `joint` [P, n] @ `weights` [n]."""
    outcome = joint @ weights
    out: FloatArray = np.quantile(outcome, list(quantiles))
    return out


def calibration_cases(
    panel: ReturnPanel, rows: Sequence[int], params: DailyRiskParams, base_seed: int
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Simulated risk against what happened, for each evaluation row.

    The first table has one line per row, asset and horizon (the simulated 90%
    interval and the realized simple return). The second has one line per row and
    horizon for the equal-weight portfolio of the as-of universe (its simulated 5%
    and 1% return quantiles and the realized return of buying it at the close of
    the row and holding it). Rows whose outcome is not in the panel yet are left out.
    """
    asset_lines: list[tuple[object, ...]] = []
    portfolio_lines: list[tuple[object, ...]] = []
    for row in rows:
        valid, paths = simulate_joint(panel, row, params, seed_for(base_seed, row))
        count = int(valid.sum())
        if count == 0:
            continue
        symbols = [s for s, ok in zip(panel.symbols, valid, strict=True) if ok]
        weights = np.full(count, 1.0 / count)
        day = panel.day[row]
        for k, horizon in enumerate(params.horizons):
            outcome = realized(panel, row, horizon)[valid]
            if not np.isfinite(outcome).all():
                continue
            low = np.quantile(paths[k], INTERVAL_TAIL, axis=0)
            high = np.quantile(paths[k], 1.0 - INTERVAL_TAIL, axis=0)
            for column, symbol in enumerate(symbols):
                asset_lines.append(
                    (
                        row,
                        day,
                        symbol,
                        horizon,
                        float(low[column]),
                        float(high[column]),
                        float(outcome[column]),
                    )
                )
            q05, q01 = portfolio_quantiles(paths[k], weights, LOSS_QUANTILES)
            portfolio_lines.append(
                (row, day, horizon, count, float(q05), float(q01), float(outcome @ weights))
            )
    return (
        pd.DataFrame(asset_lines, columns=list(ASSET_COLUMNS)),
        pd.DataFrame(portfolio_lines, columns=list(PORTFOLIO_COLUMNS)),
    )


@dataclass(frozen=True)
class RateInterval:
    """A frequency with the interval of its block bootstrap."""

    rate: float
    low: float
    high: float
    cases: int


def rate_interval(
    hits: FloatArray,
    groups: np.ndarray,
    rng: np.random.Generator,
    *,
    resamples: int = 2000,
    level: float = 0.95,
) -> RateInterval:
    """Share of `hits` (0 or 1) with a bootstrap interval that resamples whole `groups`.

    Evaluations made close together overlap in time and are not independent, so the
    bootstrap draws whole groups (calendar months) with replacement.
    """
    if len(hits) == 0:
        return RateInterval(float("nan"), float("nan"), float("nan"), 0)
    labels, inverse = np.unique(groups, return_inverse=True)
    hit_sum = np.bincount(inverse, weights=hits, minlength=len(labels))
    case_sum = np.bincount(inverse, minlength=len(labels)).astype(float)
    draws = rng.integers(0, len(labels), size=(resamples, len(labels)))
    rates = hit_sum[draws].sum(axis=1) / case_sum[draws].sum(axis=1)
    tail = (1.0 - level) / 2.0
    return RateInterval(
        float(np.mean(hits)),
        float(np.quantile(rates, tail)),
        float(np.quantile(rates, 1.0 - tail)),
        len(hits),
    )
