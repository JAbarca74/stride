"""Configuration of the risk-allocation paper instance (`config.paper_risk.yaml`).

A small standalone file, like `config.paper_mc.yaml`: the instance has nothing to do
with the daily strategy. Costs (commission, spread) are taken from `config.yaml`, so
the instance pays what B and DIP pay. Every number here is a parameter; none is
hardcoded in the job (CLAUDE.md, rule 5).
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from stride.forecast.daily_risk import DailyRiskParams
from stride.forecast.params import WeightParams
from stride.forecast.risk_walkforward import ALLOCATION_HORIZON
from stride.forecast.risk_weights import RiskRule

Ticker = Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9.\-]{0,9}$")]
Fraction = Annotated[float, Field(gt=0.0, lt=1.0)]

CALENDAR_PER_SESSION = 365.0 / 252.0
HISTORY_MARGIN_DAYS = 30


class RiskPaperConfigError(Exception):
    """The file is missing, unreadable or invalid."""


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AllocationSection(_Section):
    """How the weights are set (`RiskRule`)."""

    allocator: Literal["EW", "IV", "ERC"]
    brake: Literal["none", "trend", "dd"]
    trend_days: Annotated[int, Field(ge=2)]
    dd_floor: Fraction


class SimulationSection(_Section):
    """How the daily futures are simulated (`DailyRiskParams`)."""

    window_days: Annotated[int, Field(ge=20)]
    vol_span_days: Annotated[int, Field(ge=2)]
    mean_block_days: Annotated[float, Field(ge=1.0)]
    n_paths: Annotated[int, Field(ge=100)]
    seed: int


class LimitsSection(_Section):
    """Risk limits (`WeightParams`)."""

    max_weight: Annotated[float, Field(gt=0, le=1.0)]
    max_total: Annotated[float, Field(gt=0, le=1.0)]
    risk_horizon: Annotated[int, Field(ge=1)]  # sessions of the portfolio loss check
    risk_quantile: Fraction
    max_portfolio_loss: Annotated[float, Field(gt=0)]
    min_order_usd: Annotated[float, Field(ge=0)]


class RiskPaperConfig(_Section):
    """`enabled: false` makes every tick of the job a no-op (the owner flips it)."""

    enabled: bool
    mode: Literal["paper"]  # this instance never trades real money
    capital_usd: Annotated[float, Field(gt=0)]
    kill_switch_dd: Fraction
    db_path: str
    universe: Annotated[list[Ticker], Field(min_length=2)]
    groups: dict[str, list[Ticker]]
    group_caps: dict[str, Annotated[float, Field(gt=0, le=1.0)]]
    allocation: AllocationSection
    simulation: SimulationSection
    limits: LimitsSection
    history_days: Annotated[int, Field(ge=60)]  # calendar days of daily bars kept before the start
    min_fresh_share: Fraction  # share of the held universe that must have the newest bar
    sanity_move: Fraction  # a one-day move above this is treated as bad data

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        universe = set(self.universe)
        if len(universe) != len(self.universe):
            raise ValueError("universe contains duplicate symbols")
        seen: set[str] = set()
        for name, members in self.groups.items():
            for symbol in members:
                if symbol not in universe:
                    raise ValueError(f"group {name!r}: {symbol} is not in the universe")
                if symbol in seen:
                    raise ValueError(f"{symbol} belongs to more than one group")
                seen.add(symbol)
        if set(self.group_caps) - set(self.groups):
            raise ValueError("group_caps names a group that does not exist")
        longest = max(self.simulation.window_days + 1, self.allocation.trend_days)
        if self.history_days < longest * CALENDAR_PER_SESSION + HISTORY_MARGIN_DAYS:
            raise ValueError("history_days must cover the simulation window and the trend average")
        return self

    def risk_params(self) -> DailyRiskParams:
        """The simulation parameters (the loss-check horizon and the 20-session one)."""
        section = self.simulation
        horizons = tuple(sorted({self.limits.risk_horizon, ALLOCATION_HORIZON}))
        return DailyRiskParams(
            window_days=section.window_days,
            vol_span_days=section.vol_span_days,
            mean_block_days=section.mean_block_days,
            n_paths=section.n_paths,
            horizons=horizons,
        )

    def rule(self) -> RiskRule:
        """The variant: allocator, brake and limits."""
        limits = WeightParams(
            **self.limits.model_dump(),
            groups={name: tuple(members) for name, members in self.groups.items()},
            group_caps=dict(self.group_caps),
        )
        section = self.allocation
        return RiskRule(
            f"{section.allocator}_{section.brake}",
            section.allocator,
            section.brake,
            limits,
            trend_days=section.trend_days,
            dd_floor=section.dd_floor,
        )


def load_risk_config(path: str | Path) -> RiskPaperConfig:
    """Read and validate the instance file."""
    target = Path(path)
    try:
        raw = yaml.safe_load(target.read_text(encoding="utf-8"))
        return RiskPaperConfig.model_validate(raw)
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        raise RiskPaperConfigError(f"{target}: {exc}") from None
