"""Leveraged ETF selection and synthetic history (SPEC sections 7 and 9, task T21).

A leveraged ETF is eligible only if its 30-day median bid-ask spread and its net
assets (from `reference/etf_facts.yaml`) pass the filters in `config.yaml`.
Before an ETF existed, its history is simulated from the underlying:

    r_syn(t) = m * r_u(t) - (m - 1) * (rf(t-1) + spread) / 252 - fee / 252

where rf(t-1) is the 3-month T-bill (FRED DTB3) known on the previous trading day.
The financing spread is calibrated per asset on the overlap with the real ETF, and
the synthetic series must stay within 2% per year of it.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Annotated, Literal

import numpy as np
import pandas as pd
import yaml
from pydantic import BaseModel, ConfigDict, Field

from stride.config import LeveragedEtfFilters

logger = logging.getLogger(__name__)

TRADING_DAYS_PER_YEAR = 252
MAX_TRACKING_DIFF_PER_YEAR = 0.02  # SPEC section 9: synthetic vs real
SPREAD_SEARCH = (-0.05, 0.10)  # bracket for the calibrated financing spread
_BISECTION_STEPS = 60


DEFAULT_FACTS_PATH = Path(__file__).resolve().parents[3] / "reference" / "etf_facts.yaml"


class EtfFacts(BaseModel):
    """Issuer facts for one leveraged ETF (`reference/etf_facts.yaml`, SPEC 7)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticker: str
    underlying: str
    multiple: Literal[2, 3]
    expense_ratio: Annotated[float, Field(ge=0, lt=0.1)] | None
    aum_usd: Annotated[float, Field(ge=0)] | None
    median_spread_30d: Annotated[float, Field(ge=0, lt=1)] | None
    as_of: date | None
    source: list[str]


def load_etf_facts(path: str | Path = DEFAULT_FACTS_PATH) -> dict[str, EtfFacts]:
    """Read and validate the issuer facts file, keyed by ticker."""
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return {ticker: EtfFacts(ticker=ticker, **fields) for ticker, fields in raw.items()}


def passes_filters(facts: EtfFacts, filters: LeveragedEtfFilters) -> bool:
    """Whether the ETF may be used. Any missing fact fails the filters."""
    if facts.median_spread_30d is None or facts.aum_usd is None:
        return False
    return (
        facts.median_spread_30d <= filters.max_median_spread
        and facts.aum_usd >= filters.min_aum_usd
    )


def expense_ratio_for(ticker: str, facts: Mapping[str, EtfFacts], fallback: float) -> float:
    """The ETF's own net expense ratio; `costs.leveraged_etf_fee` only as a fallback,
    which logs a warning (SPEC 1.2, section 9)."""
    known = facts.get(ticker)
    if known is not None and known.expense_ratio is not None:
        return known.expense_ratio
    logger.warning(
        "no expense ratio for %s in etf_facts.yaml; using fallback %.4f", ticker, fallback
    )
    return fallback


def rate_known_at_t_minus_1(rate: pd.Series, days: pd.DatetimeIndex) -> pd.Series:
    """Annual risk-free rate (fraction) to use on each of `days`: the last value
    published strictly before that day. Never looks at the same or a later day."""
    clean = rate.dropna().sort_index()
    clean.index = pd.DatetimeIndex(clean.index).normalize()
    shifted_days = pd.DatetimeIndex(days) - pd.Timedelta(days=1)
    known = clean.reindex(clean.index.union(shifted_days)).ffill().reindex(shifted_days)
    known.index = pd.DatetimeIndex(days)
    return known


def synthetic_returns(
    underlying_close: pd.Series,
    multiple: int,
    rate: pd.Series,
    spread: float,
    annual_fee: float,
) -> pd.Series:
    """Daily synthetic returns of an m-times leveraged ETF on `underlying_close`.

    `rate` is the annual risk-free rate (fraction) already aligned to t-1 (see
    `rate_known_at_t_minus_1`) and indexed like `underlying_close`.
    """
    underlying_return = underlying_close.sort_index().pct_change().iloc[1:]
    rf = rate.reindex(underlying_return.index)
    if rf.isna().any():
        raise ValueError("risk-free rate missing for some days")
    financing = (multiple - 1) * (rf + spread) / TRADING_DAYS_PER_YEAR
    return multiple * underlying_return - financing - annual_fee / TRADING_DAYS_PER_YEAR


def synthetic_prices(
    returns: pd.Series, base_date: pd.Timestamp, anchor_date: pd.Timestamp, anchor_close: float
) -> pd.Series:
    """Price path from daily `returns` starting at `base_date` (the day before the
    first return), scaled so it equals `anchor_close` on `anchor_date`. Used to splice
    the synthetic history onto the real ETF without a jump."""
    growth = pd.concat([pd.Series([1.0], index=[base_date]), (1 + returns).cumprod()])
    if anchor_date not in growth.index:
        raise ValueError(f"anchor date {anchor_date.date()} is not in the series")
    scaled: pd.Series = growth * (anchor_close / float(growth[anchor_date]))
    return scaled


def annualized_return(returns: pd.Series) -> float:
    growth = float(np.prod(1 + returns.to_numpy()))
    return float(growth ** (TRADING_DAYS_PER_YEAR / len(returns)) - 1)


def tracking_diff_per_year(synthetic: pd.Series, real: pd.Series) -> float:
    """Absolute difference of annualized returns over the common days."""
    common = synthetic.index.intersection(real.index)
    if len(common) == 0:
        raise ValueError("no common days between synthetic and real returns")
    return abs(annualized_return(synthetic[common]) - annualized_return(real[common]))


@dataclass(frozen=True)
class Calibration:
    spread: float
    tracking_diff_per_year: float

    @property
    def passes(self) -> bool:
        return self.tracking_diff_per_year < MAX_TRACKING_DIFF_PER_YEAR


def calibrate_spread(
    underlying_close: pd.Series,
    real_etf_close: pd.Series,
    multiple: int,
    rate: pd.Series,
    annual_fee: float,
) -> Calibration:
    """Financing spread that makes the synthetic annualized return match the real
    ETF on their overlap. The synthetic return falls as the spread rises, so the
    root is found by bisection inside `SPREAD_SEARCH`."""
    real = real_etf_close.sort_index().pct_change().iloc[1:]
    overlap = underlying_close.sort_index()
    overlap = overlap[overlap.index >= real.index[0] - pd.Timedelta(days=7)]
    overlap = overlap[overlap.index <= real.index[-1]]
    rf = rate.reindex(overlap.index)

    def gap(spread: float) -> float:
        # Compare on common days only: a leveraged ETF may predate its underlying
        # (BITX predates IBIT), and those early returns have no synthetic match.
        syn = synthetic_returns(overlap, multiple, rf, spread, annual_fee)
        common = syn.index.intersection(real.index)
        return annualized_return(syn[common]) - annualized_return(real[common])

    low, high = SPREAD_SEARCH
    if gap(low) < 0 or gap(high) > 0:
        best = low if abs(gap(low)) < abs(gap(high)) else high
    else:
        for _ in range(_BISECTION_STEPS):
            mid = (low + high) / 2
            if gap(mid) > 0:
                low = mid
            else:
                high = mid
        best = (low + high) / 2
    syn = synthetic_returns(overlap, multiple, rf, best, annual_fee)
    return Calibration(best, tracking_diff_per_year(syn, real))
