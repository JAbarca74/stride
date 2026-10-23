"""Monthly allocation strategies for the pre-registered search (owner request,
2026-10-23; docs/design/strategy_search.md). Pure functions, no I/O.

Each function gets the closes up to and including the evaluation day (one column
per symbol, oldest first) and the annual risk-free rate known that day, and
returns target weights per symbol as fractions of equity, before leverage. A
missing or too short history never adds a position.

- dual_momentum      Antonacci's GEM: the better of the assets by lookback return
                     if it beats T-bills over the same span, else the refuge.
- relative_strength  Faber: the top N assets by lookback return (skipping the
                     last `skip` days), each only if it beats T-bills; empty
                     slots go to the refuge.
- vol_managed        Moreira and Muir: the first asset, weighted by target
                     volatility / realized volatility over 21 days, capped.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

import pandas as pd

from stride.config import AllocationConfig

TRADING_DAYS_PER_YEAR = 252
VOL_WINDOW = 21  # Moreira and Muir: one month of daily returns


def lookback_return(close: pd.Series, lookback: int, skip: int = 0) -> float:
    """Return from `lookback` days ago to `skip` days ago (NaN if too short)."""
    clean = close.dropna()
    if len(clean) <= lookback or skip >= lookback:
        return math.nan
    end = float(clean.iloc[-1 - skip])
    start = float(clean.iloc[-1 - lookback])
    return end / start - 1 if start > 0 else math.nan


def hurdle(risk_free: float, lookback: int, skip: int = 0) -> float:
    """The T-bill return over the same span, from the annual rate known today."""
    return risk_free * (lookback - skip) / TRADING_DAYS_PER_YEAR


def _refuge(refuge: str, weight: float) -> dict[str, float]:
    return {} if refuge == "cash" or weight <= 0 else {refuge: weight}


def dual_momentum(
    closes: pd.DataFrame,
    risk_free: float,
    *,
    assets: Sequence[str],
    refuge: str,
    lookback: int,
    skip: int = 0,
) -> dict[str, float]:
    """All in the best asset if its return beats T-bills, else all in the refuge."""
    momentum = {
        a: lookback_return(closes[a], lookback, skip) for a in assets if a in closes.columns
    }
    momentum = {a: m for a, m in momentum.items() if math.isfinite(m)}
    if not momentum:
        return {}
    best = max(momentum, key=lambda a: momentum[a])
    if momentum[best] > hurdle(risk_free, lookback, skip):
        return {best: 1.0}
    return _refuge(refuge, 1.0)


def relative_strength(
    closes: pd.DataFrame,
    risk_free: float,
    *,
    assets: Sequence[str],
    refuge: str,
    lookback: int,
    skip: int,
    top: int,
) -> dict[str, float]:
    """Equal weights in the top `top` assets that beat T-bills; the rest in the refuge."""
    momentum = {
        a: lookback_return(closes[a], lookback, skip) for a in assets if a in closes.columns
    }
    ranked = sorted(
        (a for a, m in momentum.items() if math.isfinite(m)),
        key=lambda a: momentum[a],
        reverse=True,
    )
    bar = hurdle(risk_free, lookback, skip)
    chosen = [a for a in ranked[:top] if momentum[a] > bar]
    weights = dict.fromkeys(chosen, 1.0 / top)
    return weights | _refuge(refuge, 1.0 - len(chosen) / top)


def vol_managed(
    closes: pd.DataFrame,
    risk_free: float,
    *,
    assets: Sequence[str],
    target_vol: float,
    max_weight: float,
) -> dict[str, float]:
    """The first asset with weight target_vol / realized volatility (21 days)."""
    if not assets or assets[0] not in closes.columns:
        return {}
    returns = closes[assets[0]].dropna().pct_change().dropna().iloc[-VOL_WINDOW:]
    if len(returns) < VOL_WINDOW:
        return {}
    realized = float(returns.std(ddof=1)) * math.sqrt(TRADING_DAYS_PER_YEAR)
    if not realized > 0:
        return {}
    return {assets[0]: min(max_weight, target_vol / realized)}


def target_weights(
    settings: AllocationConfig, closes: pd.DataFrame, risk_free: float
) -> dict[str, float]:
    """Dispatch on `settings.mode` (`experiments.allocation` in config.yaml)."""
    if settings.mode == "dual_momentum":
        return dual_momentum(
            closes,
            risk_free,
            assets=settings.assets,
            refuge=settings.refuge,
            lookback=settings.lookback,
            skip=settings.skip,
        )
    if settings.mode == "relative_strength":
        return relative_strength(
            closes,
            risk_free,
            assets=settings.assets,
            refuge=settings.refuge,
            lookback=settings.lookback,
            skip=settings.skip,
            top=settings.top,
        )
    return vol_managed(
        closes,
        risk_free,
        assets=settings.assets,
        target_vol=settings.target_vol,
        max_weight=settings.max_weight,
    )
