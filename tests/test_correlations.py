from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from stride.reporting.correlations import (
    WINDOW_DAYS,
    correlation_matrix,
    daily_returns,
    flag_pairs,
)

DAYS = pd.bdate_range("2022-01-03", periods=900)
END = DAYS[-1].date()


def _closes() -> dict[str, pd.Series]:
    rng = np.random.default_rng(4)
    base = rng.normal(0, 0.01, len(DAYS))
    noise = rng.normal(0, 0.01, (3, len(DAYS)))

    def path(returns: np.ndarray) -> pd.Series:
        return pd.Series(100 * np.cumprod(1 + returns), index=DAYS)

    return {
        "SPY": path(base),
        "EFA": path(0.9 * base + 0.3 * noise[0]),  # strongly tied to SPY
        "GLD": path(noise[1]),  # independent
        "UUP": path(-0.8 * base + 0.4 * noise[2]),  # negatively tied
    }


def test_matrix_is_symmetric_with_unit_diagonal() -> None:
    matrix = correlation_matrix(_closes(), END)
    assert list(matrix.columns) == ["SPY", "EFA", "GLD", "UUP"]
    np.testing.assert_allclose(matrix.to_numpy(), matrix.to_numpy().T)
    np.testing.assert_allclose(np.diag(matrix.to_numpy()), 1.0)


def test_window_is_three_years_ending_on_end() -> None:
    returns = daily_returns(_closes(), END)
    assert len(returns) == WINDOW_DAYS
    assert returns.index[-1] == pd.Timestamp(END)
    earlier = daily_returns(_closes(), DAYS[500].date())
    assert earlier.index[-1] == DAYS[500]
    assert len(earlier) == 500  # shorter than 3 years of history: all of it


def test_known_correlation_by_hand() -> None:
    days = pd.bdate_range("2026-01-05", periods=5)
    a = pd.Series([100, 101, 103, 102, 104], index=days, dtype=float)
    b = pd.Series([50, 50.5, 51.5, 51, 52], index=days, dtype=float)  # a / 2: identical returns
    c = pd.Series([10, 9.9, 9.7, 9.8, 9.6], index=days, dtype=float)
    matrix = correlation_matrix({"A": a, "B": b, "C": c}, days[-1].date())
    assert matrix.loc["A", "B"] == pytest.approx(1.0)
    ra, rc = a.pct_change().dropna(), c.pct_change().dropna()
    assert matrix.loc["A", "C"] == pytest.approx(float(np.corrcoef(ra, rc)[0, 1]))


def test_pairs_above_0_7_are_flagged_highest_first() -> None:
    matrix = correlation_matrix(_closes(), END)
    flagged = flag_pairs(matrix)
    assert [(p.a, p.b) for p in flagged] == [("SPY", "EFA")]
    assert flagged[0].correlation > 0.7
    assert matrix.to_numpy()[0, 3] < -0.7  # SPY/UUP: strongly negative is not flagged


def test_threshold_is_strict_and_configurable() -> None:
    matrix = pd.DataFrame([[1.0, 0.7], [0.7, 1.0]], index=["A", "B"], columns=["A", "B"])
    assert flag_pairs(matrix) == []
    assert [(p.a, p.b) for p in flag_pairs(matrix, threshold=0.5)] == [("A", "B")]


def test_missing_days_are_dropped_for_every_symbol() -> None:
    closes = _closes()
    closes["GLD"] = closes["GLD"].drop(DAYS[-3])
    returns = daily_returns(closes, END)
    assert DAYS[-3] not in returns.index
    assert not returns.isna().any().any()
