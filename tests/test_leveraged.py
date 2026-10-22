from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from stride.config import load_config
from stride.data.leveraged import (
    EtfFacts,
    annualized_return,
    calibrate_spread,
    expense_ratio_for,
    load_etf_facts,
    passes_filters,
    rate_known_at_t_minus_1,
    synthetic_prices,
    synthetic_returns,
    tracking_diff_per_year,
)
from stride.data.sources import FredRateSource, SourceError

ROOT = Path(__file__).resolve().parents[1]
FILTERS = load_config(ROOT / "config.yaml", env={}).leveraged_etf_filters
DAYS = pd.bdate_range("2022-01-03", periods=520)


def _underlying(seed: int = 7) -> pd.Series:
    rng = np.random.default_rng(seed)
    returns = rng.normal(0.0004, 0.01, len(DAYS))
    return pd.Series(100 * np.cumprod(1 + returns), index=DAYS)


def _rate(value: float = 0.04) -> pd.Series:
    return pd.Series(value, index=DAYS)


# Filters and issuer facts (SPEC section 7).


def test_reference_filters_are_loaded_from_config() -> None:
    assert FILTERS.max_median_spread == 0.002
    assert FILTERS.min_aum_usd == 50_000_000


def _facts(spread: float | None, aum: float | None, fee: float | None = 0.0095) -> EtfFacts:
    return EtfFacts(
        ticker="X",
        underlying="Y",
        multiple=2,
        expense_ratio=fee,
        aum_usd=aum,
        median_spread_30d=spread,
        as_of=None,
        source=[],
    )


@pytest.mark.parametrize(
    ("spread", "aum", "expected"),
    [
        (0.0001, 8.9e9, True),  # SSO-like
        (0.0035, 68.6e6, False),  # UBT-like: spread too wide
        (0.0004, 40e6, False),  # too small
        (0.002, 50e6, True),  # exactly at both limits
        (0.0004, None, False),  # assets unknown
        (None, 8.9e9, False),  # spread unknown
    ],
)
def test_filters(spread: float | None, aum: float | None, expected: bool) -> None:
    assert passes_filters(_facts(spread, aum), FILTERS) is expected


def test_reference_facts_file_loads_and_covers_every_mapped_etf() -> None:
    facts = load_etf_facts()
    config = load_config(ROOT / "config.yaml", env={})
    mapped = {etf for versions in config.leveraged_etfs.values() for etf in versions.values()}
    assert mapped <= set(facts)
    for ticker, fact in facts.items():
        assert fact.underlying in config.leveraged_etfs
        assert config.leveraged_etfs[fact.underlying][fact.multiple] == ticker


def test_reference_facts_are_complete_and_only_ubt_fails_the_filters() -> None:
    facts = load_etf_facts()
    config = load_config(ROOT / "config.yaml", env={})
    assert facts["ETHU"].underlying == "ETHA"
    for fact in facts.values():
        assert fact.expense_ratio is not None
        assert fact.aum_usd is not None
        assert fact.median_spread_30d is not None
    failing = {t for t, f in facts.items() if not passes_filters(f, config.leveraged_etf_filters)}
    assert failing == {"UBT"}  # 0.35% median spread, above the 0.20% cap


def test_facts_file_rejects_unknown_fields(tmp_path: Path) -> None:
    path = tmp_path / "facts.yaml"
    path.write_text(
        "SSO: {underlying: SPY, multiple: 2, expense_ratio: 0.0084, aum_usd: 1,"
        " median_spread_30d: null, as_of: null, source: [], typo: 1}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="typo"):
        load_etf_facts(path)


def test_expense_ratio_comes_from_facts() -> None:
    facts = {"X": _facts(0.001, 1e9, fee=0.0084)}
    assert expense_ratio_for("X", facts, fallback=0.0095) == 0.0084


@pytest.mark.parametrize("facts", [{}, {"X": _facts(0.001, 1e9, fee=None)}])
def test_missing_expense_ratio_falls_back_with_warning(
    facts: dict[str, EtfFacts], caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level("WARNING", logger="stride.data.leveraged"):
        assert expense_ratio_for("X", facts, fallback=0.0095) == 0.0095
    assert "using fallback" in caplog.text


# Risk-free rate known at t-1.


def test_rate_uses_previous_day_value() -> None:
    rate = pd.Series(
        [0.01, 0.02, 0.03], index=pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"])
    )
    days = pd.to_datetime(["2024-01-03", "2024-01-04", "2024-01-05"])
    assert rate_known_at_t_minus_1(rate, pd.DatetimeIndex(days)).tolist() == [0.01, 0.02, 0.03]


def test_rate_carries_forward_over_holidays() -> None:
    rate = pd.Series([0.05], index=pd.to_datetime(["2024-11-27"]))
    days = pd.DatetimeIndex(pd.to_datetime(["2024-11-29", "2024-12-02"]))  # after Thanksgiving
    assert rate_known_at_t_minus_1(rate, days).tolist() == [0.05, 0.05]


def test_rate_published_on_day_t_is_not_used_on_day_t() -> None:
    rate = pd.Series([0.01, 0.99], index=pd.to_datetime(["2024-01-02", "2024-01-03"]))
    known = rate_known_at_t_minus_1(rate, pd.DatetimeIndex(pd.to_datetime(["2024-01-03"])))
    assert known.iloc[0] == 0.01


# Synthetic returns and prices.


def test_synthetic_return_formula() -> None:
    close = pd.Series([100.0, 101.0, 99.99], index=DAYS[:3])
    rate = pd.Series(0.05, index=DAYS[:3])
    result = synthetic_returns(close, 3, rate, spread=0.01, annual_fee=0.0095)
    expected_first = 3 * 0.01 - 2 * (0.05 + 0.01) / 252 - 0.0095 / 252
    assert result.iloc[0] == pytest.approx(expected_first)
    assert result.iloc[1] == pytest.approx(3 * (99.99 / 101 - 1) - 2 * 0.06 / 252 - 0.0095 / 252)


def test_one_times_has_no_financing_cost() -> None:
    close = _underlying()
    result = synthetic_returns(close, 1, _rate(0.05), spread=0.02, annual_fee=0.0)
    np.testing.assert_allclose(result.to_numpy(), close.pct_change().iloc[1:].to_numpy())


def test_missing_rate_is_rejected() -> None:
    with pytest.raises(ValueError, match="risk-free rate missing"):
        synthetic_returns(_underlying(), 2, _rate()[:-5], spread=0.0, annual_fee=0.0)


def test_synthetic_prices_are_anchored_to_the_real_etf() -> None:
    returns = synthetic_returns(_underlying(), 2, _rate(), spread=0.0, annual_fee=0.0095)
    prices = synthetic_prices(returns, DAYS[0], DAYS[300], anchor_close=42.0)
    assert prices[DAYS[300]] == pytest.approx(42.0)
    np.testing.assert_allclose(prices.pct_change().iloc[1:].to_numpy(), returns.to_numpy())


def test_synthetic_prices_reject_unknown_anchor() -> None:
    returns = synthetic_returns(_underlying(), 2, _rate(), spread=0.0, annual_fee=0.0)
    with pytest.raises(ValueError, match="anchor date"):
        synthetic_prices(returns, DAYS[0], pd.Timestamp("2030-01-01"), 1.0)


# Calibration (SPEC section 9: < 2% per year).


def _real_etf(spread: float, extra_drag: float = 0.0) -> pd.Series:
    returns = synthetic_returns(_underlying(), 2, _rate(), spread, 0.0095)
    returns = returns - extra_drag / 252
    real = synthetic_prices(returns, DAYS[0], DAYS[0], 50.0)
    return real[real.index >= DAYS[200]]  # the ETF launches later than the underlying


def test_calibration_recovers_the_true_spread() -> None:
    calibration = calibrate_spread(_underlying(), _real_etf(0.0125), 2, _rate(), 0.0095)
    assert calibration.spread == pytest.approx(0.0125, abs=1e-6)
    assert calibration.tracking_diff_per_year < 1e-6
    assert calibration.passes


def test_calibration_fails_when_no_spread_explains_the_gap() -> None:
    calibration = calibrate_spread(
        _underlying(), _real_etf(0.0, extra_drag=0.5), 2, _rate(), 0.0095
    )
    assert not calibration.passes
    assert calibration.tracking_diff_per_year > 0.02


def test_tracking_diff_and_annualized_return() -> None:
    flat = pd.Series(0.0, index=DAYS[:252])
    one_pct = pd.Series(0.01 / 252, index=DAYS[:252])
    assert annualized_return(flat) == 0.0
    assert tracking_diff_per_year(one_pct, flat) == pytest.approx(0.01005, abs=1e-4)


# FRED DTB3 source.


class TextFake:
    def __init__(self, status: int, text: str) -> None:
        self.status_code = status
        self._text = text
        self.calls: list[Mapping[str, str]] = []

    @property
    def text(self) -> str:
        return self._text

    def get(self, url: str, *, params: Mapping[str, str], timeout: float) -> TextFake:
        self.calls.append(params)
        return self


def test_fred_rate_is_a_fraction_without_holidays() -> None:
    fake = TextFake(200, "observation_date,DTB3\n2007-01-01,\n2007-01-02,4.94\n2007-01-03,4.92\n")
    rate = FredRateSource(session=fake).fetch_rate()
    assert fake.calls == [{"id": "DTB3"}]
    assert list(rate.index) == [pd.Timestamp("2007-01-02"), pd.Timestamp("2007-01-03")]
    assert rate.tolist() == pytest.approx([0.0494, 0.0492])


def test_fred_http_error_is_a_source_error() -> None:
    with pytest.raises(SourceError, match="HTTP 500"):
        FredRateSource(session=TextFake(500, "")).fetch_rate()
