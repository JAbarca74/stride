"""Parameters of the Monte Carlo forecast (docs/design/mc_forecast.md).

The defaults are the design's starting values; the instance's real values live in
its config file and are chosen by the walk-forward of phase 5.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

SLOTS_PER_DAY = 7

Generator = Literal["drift", "analog"]


@dataclass(frozen=True)
class ForecastParams:
    """How the paths are simulated (design section 3)."""

    window_days: int = 126  # history used, in trading days (63, 126 or 252)
    generator: Generator = "drift"  # G1 "drift" or G2 "analog"
    drift_shrink: float = 0.5  # lambda: share of the window drift that is kept
    vol_span_days: int = 60  # EWMA span of the volatility filter
    mean_block_days: float = 2.0  # mean length of a resampled run
    n_paths: int = 5000
    n_paths_risk: int = 1000  # joint paths for the portfolio risk cap (analog only)
    horizons: tuple[int, ...] = (1, 7, 35, 140)  # steps: 1 hour, 1, 5 and 20 days
    analog_share: float = 0.10  # share of the eligible moments used as analogs
    analog_min: int = 30

    def __post_init__(self) -> None:
        if self.window_days < 20:  # noqa: PLR2004
            raise ValueError("window_days must be at least 20")
        if not 0.0 <= self.drift_shrink <= 1.0:
            raise ValueError("drift_shrink must be in [0, 1]")
        if self.n_paths < 1 or self.n_paths_risk < 1:
            raise ValueError("path counts must be positive")
        if not self.horizons or min(self.horizons) < 1:
            raise ValueError("horizons must be positive step counts")
        if tuple(sorted(set(self.horizons))) != self.horizons:
            raise ValueError("horizons must be increasing and unique")

    @property
    def window_steps(self) -> int:
        """The window length in session slots."""
        return self.window_days * SLOTS_PER_DAY

    @property
    def vol_span_steps(self) -> int:
        """The span of the volatility filter in session slots."""
        return self.vol_span_days * SLOTS_PER_DAY

    @property
    def mean_block_steps(self) -> float:
        """The mean length of a resampled run in session slots."""
        return self.mean_block_days * SLOTS_PER_DAY


@dataclass(frozen=True)
class DecisionParams:
    """Entry and exit rule (design section 5)."""

    horizon: int = 35  # decision horizon H, in steps
    confirm_horizon: int = 7  # the 1-day confirmation
    p_enter: float = 0.60
    p_exit: float = 0.50
    k_cost: float = 1.0  # net edge must be at least k_cost times the round-trip cost
    p_confirm: float = 0.50

    def __post_init__(self) -> None:
        if not 0.0 < self.p_exit < self.p_enter < 1.0:
            raise ValueError("need 0 < p_exit < p_enter < 1 (hysteresis)")
        if self.k_cost < 0:
            raise ValueError("k_cost must not be negative")


@dataclass(frozen=True)
class WeightParams:
    """Dynamic weights and risk limits (design section 6)."""

    kappa: float = 0.25  # Kelly fraction
    max_weight: float = 0.25  # per asset
    max_total: float = 1.0  # no leverage
    groups: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    group_caps: Mapping[str, float] = field(default_factory=dict)
    risk_horizon: int = 35  # steps of the portfolio loss check
    risk_quantile: float = 0.01
    max_portfolio_loss: float = 0.08
    min_order_usd: float = 150.0

    def __post_init__(self) -> None:
        if self.kappa <= 0:
            raise ValueError("kappa must be positive")
        if not 0.0 < self.max_weight <= 1.0 or not 0.0 < self.max_total <= 1.0:
            raise ValueError("max_weight and max_total must be in (0, 1]")
        if not 0.0 < self.risk_quantile < 1.0 or self.max_portfolio_loss <= 0:
            raise ValueError("risk_quantile must be in (0, 1) and max_portfolio_loss positive")
        if self.min_order_usd < 0:
            raise ValueError("min_order_usd must not be negative")
        unknown = set(self.group_caps) - set(self.groups)
        if unknown:
            raise ValueError(f"group caps without a group: {sorted(unknown)}")
        if any(not 0.0 < cap <= 1.0 for cap in self.group_caps.values()):
            raise ValueError("group caps must be in (0, 1]")
