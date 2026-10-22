"""Anti-look-ahead test (SPEC sections 5 and 9, task T14).

For many cut-off days t, fake future prices are injected after t (crashes, spikes,
noise, gaps) and every signal, indicator and data check dated on or before t must
stay exactly the same. Uses the production windows from `config.yaml`.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from stride.config import load_config
from stride.data.calendar import trading_days
from stride.data.leveraged import rate_known_at_t_minus_1
from stride.data.quality import validate
from stride.data.sources import splice_spot_with_etf, to_contract
from stride.indicators import compute_indicators, ewma_volatility
from stride.strategy.signals import generate_signals

ROOT = Path(__file__).resolve().parents[1]
CONFIG = load_config(ROOT / "config.yaml", env={})
DAYS = trading_days(date(2023, 1, 3), date(2026, 10, 16))  # ~950 sessions
NOW = pd.Timestamp("2026-10-19 22:00", tz="UTC")


def _prices(seed: int, drift: float = 0.0006) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 100 * np.cumprod(1 + rng.normal(drift, 0.015, len(DAYS)))
    spread = np.abs(rng.normal(0, 0.006, len(DAYS)))
    return pd.DataFrame(
        {
            "open": close * (1 + rng.normal(0, 0.003, len(DAYS))),
            "high": close * (1 + spread + 0.004),
            "low": close * (1 - spread - 0.004),
            "close": close,
            "volume": 1_000,
        },
        index=pd.DatetimeIndex(DAYS, name="date"),
    )


Tamper = Callable[[pd.DataFrame], pd.DataFrame]


def _crash(future: pd.DataFrame) -> pd.DataFrame:
    return future * 0.3


def _spike(future: pd.DataFrame) -> pd.DataFrame:
    return future * 4.0


def _noise(future: pd.DataFrame) -> pd.DataFrame:
    rng = np.random.default_rng(99)
    return future * rng.uniform(0.5, 1.5, size=future.shape)


def _gap(future: pd.DataFrame) -> pd.DataFrame:
    return future.iloc[::2]  # half the future bars missing


TAMPERS = {"crash": _crash, "spike": _spike, "noise": _noise, "gap": _gap}
CUTS = [300, 450, 600, 800]  # positions of day t in DAYS


def _tampered(prices: pd.DataFrame, cut: int, tamper: Tamper) -> pd.DataFrame:
    return pd.concat([prices.iloc[: cut + 1], tamper(prices.iloc[cut + 1 :])])


@pytest.mark.parametrize("cut", CUTS)
@pytest.mark.parametrize("name", list(TAMPERS))
def test_fake_future_prices_change_no_past_signal(cut: int, name: str) -> None:
    prices = _prices(seed=cut)
    fake = _tampered(prices, cut, TAMPERS[name])
    past = slice(None, DAYS[cut])
    original = generate_signals(prices, CONFIG.strategy).loc[past]
    assert (original["target_units"] > 0).any() or cut == CUTS[0]  # signals do fire
    pd.testing.assert_frame_equal(original, generate_signals(fake, CONFIG.strategy).loc[past])


@pytest.mark.parametrize("cut", CUTS)
def test_fake_future_prices_change_no_past_indicator(cut: int) -> None:
    prices = _prices(seed=7)
    fake = _tampered(prices, cut, _noise)
    past = slice(None, DAYS[cut])
    pd.testing.assert_frame_equal(
        compute_indicators(prices, CONFIG.strategy).loc[past],
        compute_indicators(fake, CONFIG.strategy).loc[past],
    )
    pd.testing.assert_series_equal(
        ewma_volatility(prices["close"], 60).loc[past],
        ewma_volatility(fake["close"], 60).loc[past],
    )


@pytest.mark.parametrize("cut", CUTS)
def test_data_checks_for_day_t_ignore_later_bars(cut: int) -> None:
    prices = _prices(seed=3)
    t = DAYS[cut].date()
    clean = validate("SPY", prices.loc[: DAYS[cut]], t, CONFIG.data.quality)
    with_future = validate("SPY", _tampered(prices, cut, _spike).loc[: DAYS[cut]], t,
                           CONFIG.data.quality)  # fmt: skip
    assert clean == with_future
    assert all(r.passed for r in clean)


def test_risk_free_rate_for_day_t_never_uses_day_t_or_later() -> None:
    rate = pd.Series(np.linspace(0.01, 0.05, len(DAYS)), index=DAYS)
    fake = rate.copy()
    fake.iloc[500:] = 0.99
    pd.testing.assert_series_equal(
        rate_known_at_t_minus_1(rate, DAYS[:501]), rate_known_at_t_minus_1(fake, DAYS[:501])
    )


def test_crypto_splice_before_the_switch_ignores_later_spot_bars() -> None:
    spot = to_contract(_prices(seed=1).reset_index(), "BTC-USD", "coinbase", NOW.to_pydatetime())
    etf = to_contract(_prices(seed=2).iloc[700:].reset_index(), "IBIT", "tiingo",
                      NOW.to_pydatetime())  # fmt: skip
    spliced = splice_spot_with_etf(spot, etf, "IBIT", DAYS)
    fake_spot = spot.copy()
    fake_spot.loc[DAYS[701] :, ["open", "high", "low", "close"]] *= 5  # after the switch
    refake = splice_spot_with_etf(fake_spot, etf, "IBIT", DAYS)
    pd.testing.assert_frame_equal(spliced.loc[: DAYS[699]], refake.loc[: DAYS[699]])
