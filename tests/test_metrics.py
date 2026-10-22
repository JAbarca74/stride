from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from stride.backtest.metrics import (
    average_exposure,
    buy_and_hold_returns,
    cagr,
    calmar,
    cost_ratio,
    drawdown,
    max_drawdown,
    sharpe,
    sixty_forty_returns,
    sortino,
    summary,
    turnover,
    volatility,
)

DAYS = pd.bdate_range("2025-01-02", periods=252)


def _series(values: list[float]) -> pd.Series:
    return pd.Series(values, index=pd.bdate_range("2025-01-02", periods=len(values)))


def test_cagr_of_a_known_year() -> None:
    # 252 days of r such that (1 + r)^252 = 1.10 -> CAGR exactly 10%.
    r = 1.10 ** (1 / 252) - 1
    assert cagr(pd.Series(r, index=DAYS)) == pytest.approx(0.10)
    # Half a year with the same daily return still annualizes to 10%.
    assert cagr(pd.Series(r, index=DAYS[:126])) == pytest.approx(0.10)


def test_volatility_of_alternating_returns() -> None:
    returns = _series([0.01, -0.01] * 126)
    expected = float(np.std([0.01, -0.01] * 126, ddof=1)) * math.sqrt(252)
    assert volatility(returns) == pytest.approx(expected)
    assert volatility(_series([0.01])) != volatility(_series([0.01]))  # NaN


def test_sharpe_and_sortino_by_hand() -> None:
    returns = _series([0.02, -0.01, 0.02, -0.01])
    excess = np.array([0.02, -0.01, 0.02, -0.01])
    expected_sharpe = excess.mean() / excess.std(ddof=1) * math.sqrt(252)
    assert sharpe(returns) == pytest.approx(expected_sharpe)
    downside = math.sqrt(np.mean(np.minimum(excess, 0) ** 2))
    assert sortino(returns) == pytest.approx(excess.mean() / downside * math.sqrt(252))


def test_sharpe_subtracts_the_risk_free_rate() -> None:
    returns = _series([0.001, 0.002, 0.0, 0.003])
    shifted = sharpe(returns, risk_free=0.0252)  # 0.0001 per day
    assert shifted == pytest.approx(sharpe(returns - 0.0001))
    per_day = pd.Series(0.0252, index=returns.index)
    assert sharpe(returns, risk_free=per_day) == pytest.approx(shifted)


def test_ratios_without_risk_are_nan() -> None:
    flat = _series([0.001] * 10)
    assert math.isnan(sharpe(flat))  # zero volatility
    assert math.isnan(sortino(flat))  # no downside
    assert math.isnan(calmar(flat))  # no drawdown


def test_max_drawdown_of_a_known_path() -> None:
    # Equity 100 -> 120 -> 90 -> 95 -> 130: worst fall 120 -> 90 = 25%.
    equity = [100, 120, 90, 95, 130]
    returns = pd.Series(np.diff(equity) / np.array(equity[:-1]))
    assert max_drawdown(returns) == pytest.approx(0.25)
    assert drawdown(returns).iloc[-1] == pytest.approx(0.0)


def test_drawdown_counts_a_loss_on_the_first_day() -> None:
    assert max_drawdown(_series([-0.1, 0.05])) == pytest.approx(0.1)


def test_calmar_is_cagr_over_drawdown() -> None:
    returns = _series([0.01, -0.02, 0.015] * 84)
    assert calmar(returns) == pytest.approx(cagr(returns) / max_drawdown(returns))


def test_turnover_costs_and_exposure() -> None:
    equity = pd.Series(1_000.0, index=DAYS)
    traded = pd.Series(0.0, index=DAYS)
    traded.iloc[10] = 500.0  # one buy and one sell in the year
    traded.iloc[100] = -500.0
    assert turnover(traded, equity) == pytest.approx(1.0)
    costs = pd.Series(0.0, index=DAYS)
    costs.iloc[10] = 10.0
    costs.iloc[100] = 10.0
    assert cost_ratio(costs, equity) == pytest.approx(0.02)
    gross = pd.Series(1_500.0, index=DAYS)
    assert average_exposure(gross, equity) == pytest.approx(1.5)


def test_empty_inputs_give_nan() -> None:
    empty = pd.Series(dtype=float)
    assert math.isnan(cagr(empty))
    assert math.isnan(max_drawdown(empty))
    assert math.isnan(turnover(empty, empty))
    assert math.isnan(cost_ratio(empty, empty))
    assert math.isnan(average_exposure(empty, empty))


def test_summary_bundles_the_metrics() -> None:
    returns = _series([0.01, -0.005, 0.007, -0.002] * 63)
    result = summary(returns, risk_free=0.02)
    assert result.cagr == pytest.approx(cagr(returns))
    assert result.max_drawdown == pytest.approx(max_drawdown(returns))
    assert result.sharpe == pytest.approx(sharpe(returns, 0.02))


def test_buy_and_hold_benchmark() -> None:
    close = _series([100.0, 110.0, 99.0])
    assert buy_and_hold_returns(close).tolist() == pytest.approx([0.10, -0.10])


def test_sixty_forty_rebalances_monthly() -> None:
    days = pd.to_datetime(["2025-01-30", "2025-01-31", "2025-02-03", "2025-02-04"])
    stock = pd.Series([100.0, 110.0, 110.0, 121.0], index=days)
    bond = pd.Series([100.0, 100.0, 100.0, 100.0], index=days)
    result = sixty_forty_returns(stock, bond)
    assert result.iloc[0] == pytest.approx(0.06)  # 60% x +10%
    # Feb 3: rebalanced back to 60/40; Feb 4: stock +10% -> +6% again
    assert result.iloc[2] == pytest.approx(0.06)
    # Without a rebalance the weight would have drifted to 62.3%:
    assert result.iloc[2] != pytest.approx(0.066 / 1.06)
