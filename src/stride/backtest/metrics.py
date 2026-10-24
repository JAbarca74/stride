"""Always-reported metrics (SPEC section 9). Pure, no I/O.

Inputs are daily, time-weighted returns (deposits never count as gains) indexed by
trading day, plus the daily equity, traded amounts and costs where a metric needs
them. Annualization uses 252 trading days. Drawdowns are positive magnitudes
(0.25 means -25%), like `config.yaml`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

TRADING_DAYS_PER_YEAR = 252
_ZERO = 1e-12  # below this a standard deviation is treated as zero
_FLAT = 1e-9  # daily returns this steady are a flat (cash-only) series: no Sharpe


def _excess(returns: pd.Series, risk_free: float | pd.Series) -> pd.Series:
    """Daily excess returns; `risk_free` is an annual rate (scalar or per day)."""
    if isinstance(risk_free, pd.Series):
        rf = risk_free.reindex(returns.index).fillna(0.0)
    else:
        rf = pd.Series(risk_free, index=returns.index)
    return returns - rf / TRADING_DAYS_PER_YEAR


def time_weighted_returns(equity: pd.Series, flows: pd.Series | None = None) -> pd.Series:
    """Daily time-weighted returns: deposits and withdrawals never count as gains.

    `equity` is the end-of-day value; `flows` is the net external flow per day
    (deposits positive), which arrives as cash during that day, so
    r_t = (E_t - F_t) / E_{t-1} - 1.
    """
    equity = equity.sort_index().astype(float)
    flow = (
        flows.groupby(level=0).sum().reindex(equity.index).fillna(0.0)
        if flows is not None
        else pd.Series(0.0, index=equity.index)
    )
    previous = equity.shift(1)
    returns: pd.Series = ((equity - flow) / previous - 1).iloc[1:]
    return returns


def deposits_vs_balance(equity: pd.Series, flows: pd.Series) -> pd.DataFrame:
    """Total deposited (net of withdrawals) against the balance, per day."""
    flow = flows.groupby(level=0).sum().reindex(equity.index).fillna(0.0)
    return pd.DataFrame({"deposited": flow.cumsum(), "balance": equity})


def cagr(returns: pd.Series) -> float:
    """Compound annual growth rate of the time-weighted return."""
    if returns.empty:
        return math.nan
    growth = float(np.prod(1 + returns.to_numpy(dtype=float)))
    return float(growth ** (TRADING_DAYS_PER_YEAR / len(returns)) - 1)


def volatility(returns: pd.Series) -> float:
    """Annualized standard deviation of daily returns."""
    if len(returns) < 2:  # noqa: PLR2004
        return math.nan
    return float(returns.std(ddof=1) * math.sqrt(TRADING_DAYS_PER_YEAR))


def _flat(returns: pd.Series) -> bool:
    """A series that never moves (e.g. a stopped system in cash with no interest):
    its excess return is only the risk-free rate, so a Sharpe would be noise."""
    return len(returns) > 1 and float(returns.std(ddof=1)) < _FLAT


def sharpe(returns: pd.Series, risk_free: float | pd.Series = 0.0) -> float:
    """(Return - risk-free) / volatility, annualized from daily excess returns."""
    if _flat(returns):
        return math.nan
    excess = _excess(returns, risk_free)
    std = float(excess.std(ddof=1)) if len(excess) > 1 else math.nan
    if math.isnan(std) or std < _ZERO:
        return math.nan
    return float(excess.mean() / std * math.sqrt(TRADING_DAYS_PER_YEAR))


def sortino(returns: pd.Series, risk_free: float | pd.Series = 0.0) -> float:
    """Like Sharpe, with downside deviation (root mean square of negative excess)."""
    if _flat(returns):
        return math.nan
    excess = _excess(returns, risk_free)
    if excess.empty:
        return math.nan
    downside = math.sqrt(float((excess.clip(upper=0.0) ** 2).mean()))
    if downside < _ZERO:
        return math.nan
    return float(excess.mean() / downside * math.sqrt(TRADING_DAYS_PER_YEAR))


def drawdown(returns: pd.Series) -> pd.Series:
    """Fall from the running peak on each day, as a positive fraction."""
    wealth = (1 + returns).cumprod()
    peak = wealth.cummax().clip(lower=1.0)  # the starting value counts as a peak
    result: pd.Series = 1 - wealth / peak
    return result


def max_drawdown(returns: pd.Series) -> float:
    """Largest peak-to-trough fall, as a positive fraction."""
    if returns.empty:
        return math.nan
    return float(drawdown(returns).max())


def calmar(returns: pd.Series) -> float:
    """CAGR / max drawdown."""
    mdd = max_drawdown(returns)
    if math.isnan(mdd) or mdd < _ZERO:
        return math.nan
    return cagr(returns) / mdd


def _per_year(total: float, days: int) -> float:
    return total * TRADING_DAYS_PER_YEAR / days


def turnover(traded_usd: pd.Series, equity: pd.Series) -> float:
    """Annual traded amount / average equity."""
    if equity.empty or equity.mean() <= 0:
        return math.nan
    return _per_year(float(traded_usd.abs().sum()), len(equity)) / float(equity.mean())


def cost_ratio(costs_usd: pd.Series, equity: pd.Series) -> float:
    """Total costs per year as a fraction of average equity."""
    if equity.empty or equity.mean() <= 0:
        return math.nan
    return _per_year(float(costs_usd.sum()), len(equity)) / float(equity.mean())


def average_exposure(gross_exposure: pd.Series, equity: pd.Series) -> float:
    """Average leverage used: mean of gross exposure / equity."""
    ratio = (gross_exposure / equity.replace(0, np.nan)).dropna()
    return float(ratio.mean()) if not ratio.empty else math.nan


@dataclass(frozen=True)
class Metrics:
    cagr: float
    volatility: float
    sharpe: float
    sortino: float
    max_drawdown: float
    calmar: float


def summary(returns: pd.Series, risk_free: float | pd.Series = 0.0) -> Metrics:
    """The return-based metrics of section 9 for one series (strategy or benchmark)."""
    return Metrics(
        cagr=cagr(returns),
        volatility=volatility(returns),
        sharpe=sharpe(returns, risk_free),
        sortino=sortino(returns, risk_free),
        max_drawdown=max_drawdown(returns),
        calmar=calmar(returns),
    )


def buy_and_hold_returns(close: pd.Series) -> pd.Series:
    """Daily returns of holding one asset (benchmark: SPY)."""
    return close.sort_index().pct_change().dropna()


def sixty_forty_returns(stock_close: pd.Series, bond_close: pd.Series) -> pd.Series:
    """Daily returns of 60/40 (e.g. SPY/IEF) rebalanced on the first day of each month."""
    prices = pd.concat([stock_close, bond_close], axis=1, keys=["s", "b"]).sort_index().dropna()
    asset_returns = prices.pct_change().iloc[1:]
    target = np.array([0.6, 0.4])
    weights = target
    month: tuple[int, int] | None = None
    out = []
    for day, row in zip(
        pd.DatetimeIndex(asset_returns.index), asset_returns.to_numpy(), strict=True
    ):
        if (day.year, day.month) != month:
            weights = target  # rebalance at the start of each month
            month = (day.year, day.month)
        growth = weights * (1 + row)
        out.append(float(growth.sum() - 1))
        weights = growth / growth.sum()
    return pd.Series(out, index=asset_returns.index)
