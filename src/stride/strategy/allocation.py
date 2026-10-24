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
                     volatility / realized volatility over `vol_window` days, capped.
- momentum_vol       Round 2: dual momentum, the chosen asset weighted as in
                     vol_managed (the refuge stays at 1).
- risk_parity        Round 2: inverse-volatility weights, the whole portfolio
                     scaled to the target volatility, total weight capped.
- hold               Round 3: equal weights in the assets, always invested.
- sleeve             Round 5: `core` holds 1 - `sleeve`; each asset gets an equal
                     part of `sleeve` while its signal is on (hold, trend above
                     an average, breakout 55/20, range at 20-day lows/highs, or
                     dip: bought after a deep fall, held past +30% until the
                     rise breaks),
                     sized by a fraction of Kelly; the rest stays in cash.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

import numpy as np
import pandas as pd

from stride.config import AllocationConfig

TRADING_DAYS_PER_YEAR = 252


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


def _daily_returns(close: pd.Series, window: int) -> pd.Series:
    """The last `window` daily returns (fewer if the history is short)."""
    return close.dropna().pct_change().dropna().iloc[-window:]


def realized_vol(close: pd.Series, window: int) -> float:
    """Annualized volatility of the last `window` daily returns (NaN if short)."""
    returns = _daily_returns(close, window)
    if len(returns) < window:
        return math.nan
    return float(returns.std(ddof=1)) * math.sqrt(TRADING_DAYS_PER_YEAR)


def vol_managed(
    closes: pd.DataFrame,
    risk_free: float,
    *,
    assets: Sequence[str],
    target_vol: float,
    max_weight: float,
    vol_window: int,
) -> dict[str, float]:
    """The first asset with weight target_vol / realized volatility."""
    if not assets or assets[0] not in closes.columns:
        return {}
    realized = realized_vol(closes[assets[0]], vol_window)
    if not realized > 0:
        return {}
    return {assets[0]: min(max_weight, target_vol / realized)}


def momentum_vol(
    closes: pd.DataFrame,
    risk_free: float,
    *,
    assets: Sequence[str],
    refuge: str,
    lookback: int,
    skip: int,
    target_vol: float,
    max_weight: float,
    vol_window: int,
) -> dict[str, float]:
    """Dual momentum; the chosen asset (not the refuge) sized by volatility."""
    weights = dual_momentum(
        closes, risk_free, assets=assets, refuge=refuge, lookback=lookback, skip=skip
    )
    chosen = [a for a in weights if a in assets]
    if not chosen:
        return weights
    return vol_managed(
        closes,
        risk_free,
        assets=chosen,
        target_vol=target_vol,
        max_weight=max_weight,
        vol_window=vol_window,
    )


def risk_parity(
    closes: pd.DataFrame,
    risk_free: float,
    *,
    assets: Sequence[str],
    target_vol: float,
    max_weight: float,
    vol_window: int,
) -> dict[str, float]:
    """Weights proportional to 1 / volatility over the assets with enough history,
    scaled so the portfolio's realized volatility over the same days matches the
    target; the total weight is capped at `max_weight`."""
    vols = {a: realized_vol(closes[a], vol_window) for a in assets if a in closes.columns}
    vols = {a: v for a, v in vols.items() if math.isfinite(v) and v > 0}
    if not vols:
        return {}
    inverse = {a: 1.0 / v for a, v in vols.items()}
    total = sum(inverse.values())
    base = {a: x / total for a, x in inverse.items()}
    returns = pd.DataFrame({a: _daily_returns(closes[a], vol_window) for a in base}).dropna()
    if len(returns) < vol_window:
        return {}
    portfolio = returns.to_numpy() @ [base[a] for a in returns.columns]
    realized = float(pd.Series(portfolio).std(ddof=1)) * math.sqrt(TRADING_DAYS_PER_YEAR)
    if not realized > 0:
        return {}
    scale = min(max_weight, target_vol / realized)
    return {a: w * scale for a, w in base.items()}


def hold(closes: pd.DataFrame, risk_free: float, *, assets: Sequence[str]) -> dict[str, float]:
    """Equal weights in the assets that have a price today, always invested."""
    held = [a for a in assets if a in closes.columns and closes[a].notna().any()]
    return dict.fromkeys(held, 1.0 / len(held)) if held else {}


def trend_on(close: pd.Series, sma: int) -> bool:
    """Whether the last close is above its `sma`-day average (False if short)."""
    clean = close.dropna()
    if len(clean) < sma:
        return False
    return bool(clean.iloc[-1] > clean.iloc[-sma:].mean())


def _last_true(flags: pd.Series) -> int:
    hits = np.flatnonzero(flags.to_numpy(dtype=bool))
    return int(hits[-1]) if len(hits) else -1


def breakout_on(close: pd.Series, entry: int, exit: int) -> bool:
    """Turtle state from closes: in after a close above the prior `entry`-day high,
    out after a close below the prior `exit`-day low (rolling windows exclude the
    day itself); in only if the last entry is more recent than the last exit."""
    clean = close.dropna()
    high = clean.shift(1).rolling(entry).max()
    low = clean.shift(1).rolling(exit).min()
    last_in, last_out = _last_true(clean > high), _last_true(clean < low)
    return last_in >= 0 and last_in > last_out


def range_on(close: pd.Series, entry: int, exit: int) -> bool:
    """Range trading from closes: in after a close at or below the prior
    `entry`-day low, out after a close at or above the prior `exit`-day high."""
    clean = close.dropna()
    low = clean.shift(1).rolling(entry).min()
    high = clean.shift(1).rolling(exit).max()
    last_in, last_out = _last_true(clean <= low), _last_true(clean >= high)
    return last_in >= 0 and last_in > last_out


def dip_on(close: pd.Series, *, drop: float, window: int, take_profit: float, trail: int) -> bool:
    """Buy the dip (owner idea, round 5): in once the close is `drop` below the
    highest close of the prior `window` days. No stop: it waits for the recovery.
    Once a close is `take_profit` above the buy close the target is reached, but
    it holds while the rise goes on and sells on the first close below the
    lowest close of the prior `trail` days."""
    clean = close.dropna()
    high = clean.shift(1).rolling(window).max().to_numpy()
    low = clean.shift(1).rolling(trail).min().to_numpy()
    values = clean.to_numpy()
    bought, reached = math.nan, False
    for price, top, floor in zip(values, high, low, strict=True):
        if math.isnan(bought):
            if math.isfinite(top) and price <= top * (1 - drop):
                bought, reached = float(price), False
            continue
        if reached and math.isfinite(floor) and price < floor:
            bought, reached = math.nan, False
            continue
        if price >= bought * (1 + take_profit):
            reached = True
    return not math.isnan(bought)


def kelly_share(close: pd.Series, risk_free: float, *, fraction: float, window: int) -> float:
    """`fraction` of the Kelly weight (excess return / variance, annualized) from
    the last `window` daily returns, clipped to [0, 1]; 1 when `fraction` is 0
    (no Kelly sizing), 0 with too short a history."""
    if fraction == 0:
        return 1.0
    returns = _daily_returns(close, window)
    if len(returns) < window:
        return 0.0
    variance = float(returns.var(ddof=1)) * TRADING_DAYS_PER_YEAR
    if not variance > 0:
        return 0.0
    excess = float(returns.mean()) * TRADING_DAYS_PER_YEAR - risk_free
    return min(max(fraction * excess / variance, 0.0), 1.0)


def sleeve(
    closes: pd.DataFrame,
    risk_free: float,
    *,
    core: Sequence[str],
    assets: Sequence[str],
    share: float,
    signal: str,
    trend_sma: int,
    entry: int,
    exit: int,
    kelly_fraction: float,
    kelly_window: int,
    dip_drop: float = 0.25,
    dip_window: int = 90,
    take_profit: float = 0.30,
    core_leverage: float = 1.0,
) -> dict[str, float]:
    """Core plus a sleeve: `core` assets split 1 - `share`, times `core_leverage`;
    each sleeve asset may use `share` / len(assets) while its signal is on, times
    its Kelly share. Only
    the last `kelly_window` + `trend_sma` days are read, so the signal state is
    the one those days imply."""
    tail = closes.iloc[-(kelly_window + trend_sma + 1) :]
    held = [c for c in core if c in tail.columns and tail[c].notna().any()]
    core_weight = (1.0 - share) * core_leverage / len(held) if held else 0.0
    weights = dict.fromkeys(held, core_weight)
    part = share / len(assets) if assets else 0.0
    for asset in assets:
        if asset not in tail.columns or tail[asset].dropna().empty:
            continue
        close = tail[asset]
        on = {
            "hold": True,
            "trend": trend_on(close, trend_sma),
            "breakout": breakout_on(close, entry, exit),
            "range": range_on(close, entry, exit),
            "dip": signal == "dip"
            and dip_on(
                close, drop=dip_drop, window=dip_window, take_profit=take_profit, trail=exit
            ),
        }[signal]
        if not on:
            continue
        size = part * kelly_share(close, risk_free, fraction=kelly_fraction, window=kelly_window)
        if size > 0:
            weights[asset] = weights.get(asset, 0.0) + size
    return weights


def target_weights(  # noqa: PLR0911 - one return per mode
    settings: AllocationConfig, closes: pd.DataFrame, risk_free: float
) -> dict[str, float]:
    """Dispatch on `settings.mode` (`experiments.allocation` in config.yaml)."""
    if settings.mode == "sleeve":
        return sleeve(
            closes,
            risk_free,
            core=settings.core,
            assets=settings.assets,
            share=settings.sleeve,
            signal=settings.signal,
            trend_sma=settings.trend_sma,
            entry=settings.entry,
            exit=settings.exit,
            kelly_fraction=settings.kelly_fraction,
            kelly_window=settings.kelly_window,
            dip_drop=settings.dip_drop,
            dip_window=settings.dip_window,
            take_profit=settings.take_profit,
            core_leverage=settings.core_leverage,
        )
    if settings.mode == "hold":
        return hold(closes, risk_free, assets=settings.assets)
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
    if settings.mode == "momentum_vol":
        return momentum_vol(
            closes,
            risk_free,
            assets=settings.assets,
            refuge=settings.refuge,
            lookback=settings.lookback,
            skip=settings.skip,
            target_vol=settings.target_vol,
            max_weight=settings.max_weight,
            vol_window=settings.vol_window,
        )
    if settings.mode == "risk_parity":
        return risk_parity(
            closes,
            risk_free,
            assets=settings.assets,
            target_vol=settings.target_vol,
            max_weight=settings.max_weight,
            vol_window=settings.vol_window,
        )
    return vol_managed(
        closes,
        risk_free,
        assets=settings.assets,
        target_vol=settings.target_vol,
        max_weight=settings.max_weight,
        vol_window=settings.vol_window,
    )
