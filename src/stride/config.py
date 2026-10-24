"""Load and validate `config.yaml` (SPEC sections 6, 7, 8 and 11).

Every value is type- and range-checked at startup. An invalid file raises
`ConfigError`, which stops the system before it does anything else.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Annotated, Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

logger = logging.getLogger(__name__)

LIVE_CONFIRM_ENV = "LIVE_CONFIRM"
LIVE_CONFIRM_VALUE = "I_UNDERSTAND_REAL_MONEY"

Ticker = Annotated[str, Field(pattern=r"^[A-Z]{1,6}$")]
Lookback = Annotated[int, Field(ge=2)]
UnitCap = Annotated[int, Field(ge=1)]
Fraction = Annotated[float, Field(gt=0, lt=1)]
NonNegative = Annotated[float, Field(ge=0)]
# YAML dates arrive as strings ("2007-01-01"); parse them even in strict mode.
IsoDate = Annotated[date, Field(strict=False)]


class ConfigError(Exception):
    """The configuration file is missing, unreadable or out of range."""


class _Section(BaseModel):
    # strict: a YAML typo such as `max_leverage: true` must fail, not become 1.0.
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class LeveragedEtfFilters(_Section):
    max_median_spread: Fraction
    min_aum_usd: NonNegative


class LeverageSwitchConfig(_Section):
    """Hysteresis for switching between normal and leveraged ETFs (SPEC 7, step 2b)."""

    enter_k: Annotated[float, Field(gt=1)]
    exit_k: Annotated[float, Field(gt=0)]
    min_hold_days: Annotated[int, Field(ge=0)]

    @model_validator(mode="after")
    def _exit_below_enter(self) -> Self:
        if self.exit_k >= self.enter_k:
            raise ValueError("exit_k must be below enter_k")
        return self


class StrategyConfig(_Section):
    sma_filter: Lookback
    entry_breakout: Lookback
    exit_breakout: Lookback
    atr_period: Lookback
    stop_n: Annotated[float, Field(gt=0, le=5)]
    pyramid_step_n: Annotated[float, Field(gt=0, le=5)]
    max_units_per_asset: Annotated[int, Field(ge=1, le=4)]

    @model_validator(mode="after")
    def _exit_shorter_than_entry(self) -> Self:
        if self.exit_breakout >= self.entry_breakout:
            raise ValueError("exit_breakout must be shorter than entry_breakout")
        return self


class RiskConfig(_Section):
    risk_per_unit: Annotated[float, Field(gt=0, le=0.05)]
    max_units_group: UnitCap
    max_units_crypto: UnitCap
    max_units_total: UnitCap
    kelly_fraction: Annotated[float, Field(gt=0, le=1)]
    max_leverage: Annotated[float, Field(gt=0, le=4)]
    max_etf_portfolio_leverage: Annotated[float, Field(ge=1, le=3)]
    max_portfolio_vol: Fraction
    dd_delever_at: Fraction  # drawdown magnitude, e.g. 0.20 means -20%
    kill_switch_dd: Fraction  # drawdown magnitude, e.g. 0.40 means -40%
    allow_margin: bool
    margin_min_equity: NonNegative
    margin_buffer: Fraction
    min_history_days_for_kelly: Annotated[int, Field(ge=1)]
    reconciliation_tolerance: Fraction
    reconciliation_floor_usd: NonNegative

    @model_validator(mode="after")
    def _consistent_limits(self) -> Self:
        if self.max_etf_portfolio_leverage > self.max_leverage:
            raise ValueError("max_etf_portfolio_leverage must not exceed max_leverage")
        if self.dd_delever_at >= self.kill_switch_dd:
            raise ValueError("dd_delever_at must be smaller than kill_switch_dd")
        if self.max_units_group > self.max_units_total:
            raise ValueError("max_units_group must not exceed max_units_total")
        if self.max_units_crypto > self.max_units_total:
            raise ValueError("max_units_crypto must not exceed max_units_total")
        return self


class ExecutionConfig(_Section):
    rebalance_band: Fraction
    min_order_usd: NonNegative
    limit_offset: Annotated[float, Field(ge=0, lt=1)]
    max_orders_per_day: Annotated[int, Field(ge=1)]
    max_order_pct_equity: Fraction
    # Cost-aware resizing (owner request, 2026-10-23): a HOLD top-up buy whose
    # commission plus spread is above this fraction of the order is skipped.
    max_resize_cost_pct: Fraction


class CashFlowsConfig(_Section):
    initial_deposit_usd: Annotated[float, Field(gt=0)]
    monthly_deposit_usd: NonNegative
    deposit_day: Literal["first_trading_day"]


class CostsConfig(_Section):
    spread_bps: NonNegative
    spread_bps_crypto: NonNegative
    pricing: Literal["fixed", "tiered"]
    commission_per_share: NonNegative
    commission_min_per_order: NonNegative
    commission_max_pct_of_value: NonNegative
    tiered_per_share: NonNegative
    tiered_min_per_order: NonNegative
    tiered_max_pct_of_value: NonNegative
    tiered_third_party_per_share: NonNegative
    cash_interest_free_usd: NonNegative
    cash_interest_full_rate_nav: Annotated[float, Field(gt=0)]
    leveraged_etf_fee: NonNegative
    margin_spread: NonNegative


class DataQualityConfig(_Section):
    """Thresholds for the section 5 validations (data parameters, not strategy)."""

    missing_days_lookback: Annotated[int, Field(ge=1)]
    max_daily_change: Annotated[float, Field(gt=0, le=1)]
    source_close_tolerance: Fraction
    latest_bar_max_diff: Fraction

    @model_validator(mode="after")
    def _latest_not_stricter_than_sample(self) -> Self:
        if self.latest_bar_max_diff < self.source_close_tolerance:
            raise ValueError("latest_bar_max_diff must not be below source_close_tolerance")
        return self


class DataConfig(_Section):
    db_path: Annotated[str, Field(min_length=1)]
    history_start: IsoDate
    crypto_history_start: IsoDate
    quality: DataQualityConfig

    @model_validator(mode="after")
    def _crypto_not_before_history(self) -> Self:
        if self.crypto_history_start < self.history_start:
            raise ValueError("crypto_history_start must not be before history_start")
        return self


Experiment = Literal[
    "extra_universe", "ensemble", "drawdown_scaling", "leverage_rotation", "allocation"
]
BreakoutSystem = Annotated[list[Lookback], Field(min_length=2, max_length=2)]


class RotationConfig(_Section):
    """Experiment leverage_rotation (Gayed and Bilello, "Leverage for the Long Run",
    2016): hold the `multiple`x version of `underlying` while its close is above
    its `sma`-day average, and `refuge` (an ETF of the universe, or "cash") when
    it is below. `band` adds hysteresis: switch on above sma x (1 + band), off
    below sma x (1 - band)."""

    underlying: Ticker
    sma: Lookback
    band: Annotated[float, Field(ge=0, lt=0.2)]
    multiple: Literal[1, 2, 3]
    refuge: Annotated[str, Field(pattern=r"^([A-Z]{1,6}|cash)$")]


class AllocationConfig(_Section):
    """Experiment allocation: one of the pre-registered monthly strategies of
    docs/design/strategy_search.md (strategy/allocation.py). `leverage` multiplies
    the weights; above 1x it is reached with the eligible leveraged ETFs."""

    mode: Literal[
        "dual_momentum",
        "relative_strength",
        "vol_managed",
        "momentum_vol",
        "risk_parity",
        "hold",
        "sleeve",
    ]
    assets: Annotated[list[Ticker], Field(min_length=1)]
    refuge: Annotated[str, Field(pattern=r"^([A-Z]{1,6}|cash)$")]
    lookback: Lookback
    skip: Annotated[int, Field(ge=0)]
    top: Annotated[int, Field(ge=1)]
    target_vol: Fraction
    max_weight: Annotated[float, Field(gt=0, le=4)]
    vol_window: Lookback  # days of returns behind the realized volatility
    leverage: Annotated[float, Field(gt=0, le=4)]
    rebalance: Literal["monthly", "daily"]
    # sleeve (round 5, docs/design/strategy_search_round5.md): `core` holds
    # 1 - `sleeve` of equity; `assets` share `sleeve`, each in only while its
    # `signal` is on, sized by `kelly_fraction` of Kelly (0 = the full share).
    core: list[Ticker]
    sleeve: Fraction
    signal: Literal["hold", "trend", "breakout", "range", "dip"]
    trend_sma: Lookback  # trend: in while the close is above this average
    entry: Lookback  # breakout: close above the prior high; range: at the prior low
    exit: Lookback  # breakout: close below the prior low; range: at the prior high
    kelly_fraction: Annotated[float, Field(ge=0, le=1)]
    kelly_window: Lookback  # days of returns behind the Kelly estimate
    dip_drop: Fraction  # dip: buy once the close is this far below its recent high
    dip_window: Lookback  # dip: days behind that recent high
    take_profit: Fraction  # dip: sell once the close is this far above the buy close

    @model_validator(mode="after")
    def _skip_shorter(self) -> Self:
        if self.skip >= self.lookback:
            raise ValueError("allocation.skip must be shorter than allocation.lookback")
        return self


class ExperimentsConfig(_Section):
    """Strategy experiments (owner request, 2026-10-23). The validation measures
    each one with and without it; `enabled` lists those the configured run uses.
    They enter `enabled` only after they win out of sample (SPEC 9)."""

    enabled: list[Experiment]
    extra_universe: list[Ticker]
    extra_groups: dict[str, list[Ticker]]
    # Symbols and leveraged versions loaded only for the strategy search
    # (docs/design/strategy_search_round2.md); never traded by the configured run.
    search_universe: list[Ticker]
    search_leveraged_etfs: dict[Ticker, Annotated[dict[Literal[2, 3], Ticker], Field(min_length=1)]]
    breakout_systems: list[BreakoutSystem]  # [entry, exit] windows in trading days
    dd_scale_start: Fraction  # drawdown where leverage starts to shrink
    dd_scale_full: Fraction  # drawdown where it reaches the floor
    dd_scale_floor: Fraction  # share of the leverage kept at and beyond dd_scale_full
    # Days behind the peak the scaling drawdown is measured from; 0 = all-time peak
    # (round 4 of the strategy search, docs/design/strategy_search_round4.md).
    dd_scale_window: Annotated[int, Field(ge=0)]
    rotation: RotationConfig
    allocation: AllocationConfig

    @model_validator(mode="after")
    def _systems_exit_shorter(self) -> Self:
        for entry, exit_ in self.breakout_systems:
            if exit_ >= entry:
                raise ValueError("each breakout system needs exit shorter than entry")
        if self.dd_scale_full <= self.dd_scale_start:
            raise ValueError("dd_scale_full must be above dd_scale_start")
        if "ensemble" in self.enabled and len(self.breakout_systems) < 2:  # noqa: PLR2004
            raise ValueError("the ensemble needs at least two breakout systems")
        return self


class StrideConfig(_Section):
    mode: Literal["backtest", "paper", "live"]
    universe: Annotated[list[Ticker], Field(min_length=1)]
    groups: dict[str, list[Ticker]]
    leveraged_etfs: dict[Ticker, Annotated[dict[Literal[2, 3], Ticker], Field(min_length=1)]]
    leveraged_etf_filters: LeveragedEtfFilters
    leverage_switch: LeverageSwitchConfig
    strategy: StrategyConfig
    risk: RiskConfig
    execution: ExecutionConfig
    cash_flows: CashFlowsConfig
    costs: CostsConfig
    data: DataConfig
    experiments: ExperimentsConfig

    @model_validator(mode="after")
    def _consistent_symbols(self) -> Self:
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
        for symbol in self.leveraged_etfs:
            if symbol not in universe:
                raise ValueError(f"leveraged_etfs: {symbol} is not in the universe")
        return self

    @model_validator(mode="after")
    def _consistent_experiments(self) -> Self:
        universe = set(self.universe)
        extras = self.experiments.extra_universe
        if len(set(extras)) != len(extras) or universe & set(extras):
            raise ValueError("experiments.extra_universe must be new, unique symbols")
        known = universe | set(extras)
        for name, members in self.experiments.extra_groups.items():
            if any(m not in known for m in members):
                raise ValueError(f"experiments.extra_groups.{name}: unknown symbol")
        search = self.experiments.search_universe
        if len(set(search)) != len(search) or known & set(search):
            raise ValueError("experiments.search_universe must be new, unique symbols")
        if any(a not in known | set(search) for a in self.experiments.search_leveraged_etfs):
            raise ValueError("experiments.search_leveraged_etfs: unknown underlying")
        rotation = self.experiments.rotation
        if rotation.underlying not in universe:
            raise ValueError("experiments.rotation.underlying must be in the universe")
        if rotation.refuge != "cash" and rotation.refuge not in universe:
            raise ValueError("experiments.rotation.refuge must be in the universe or cash")
        if rotation.multiple > 1 and rotation.multiple not in self.leveraged_etfs.get(
            rotation.underlying, {}
        ):
            raise ValueError("experiments.rotation.multiple has no leveraged version")
        alloc = self.experiments.allocation
        if any(a not in universe for a in [*alloc.assets, *alloc.core]):
            raise ValueError("experiments.allocation.assets must be in the universe")
        if alloc.refuge != "cash" and alloc.refuge not in universe:
            raise ValueError("experiments.allocation.refuge must be in the universe or cash")
        return self

    @model_validator(mode="after")
    def _consistent_unit_caps(self) -> Self:
        if self.strategy.max_units_per_asset > self.risk.max_units_group:
            raise ValueError("max_units_per_asset must not exceed max_units_group")
        return self


def load_config(path: str | Path, env: Mapping[str, str] | None = None) -> StrideConfig:
    """Read, validate and return the configuration at `path`.

    `mode: live` is only kept when `LIVE_CONFIRM` in `env` (default: the process
    environment) is exactly `LIVE_CONFIRM_VALUE`; otherwise the system starts in
    `paper` and logs a warning (SPEC section 11).
    """
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"cannot read config file {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must contain a mapping at the top level")

    try:
        config = StrideConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"invalid configuration in {path}:\n{exc}") from exc

    env = os.environ if env is None else env
    if config.mode == "live" and env.get(LIVE_CONFIRM_ENV) != LIVE_CONFIRM_VALUE:
        logger.warning(
            "mode: live requested without a valid %s; starting in paper mode", LIVE_CONFIRM_ENV
        )
        config = config.model_copy(update={"mode": "paper"})
    return config


def with_extra_universe(config: StrideConfig) -> StrideConfig:
    """`config` with `experiments.extra_universe` added to the universe and
    `experiments.extra_groups` merged into the groups (a symbol moves to the
    group the experiment gives it). Built in memory and re-validated."""
    extra = config.experiments
    moved = {s for members in extra.extra_groups.values() for s in members}
    groups = {
        name: [s for s in members if s not in moved] for name, members in config.groups.items()
    }
    for name, members in extra.extra_groups.items():
        groups[name] = [*groups.get(name, []), *members]
    data = config.model_dump()
    data["universe"] = [*config.universe, *extra.extra_universe]
    data["groups"] = {name: members for name, members in groups.items() if members}
    data["experiments"]["extra_universe"] = []
    data["experiments"]["extra_groups"] = {}
    return StrideConfig.model_validate(data)


SEARCH_GROUP = "search"


def with_search_universe(config: StrideConfig) -> StrideConfig:
    """`with_extra_universe(config)` plus `experiments.search_universe` (in its own
    group) and `experiments.search_leveraged_etfs`: the universe the strategy
    search and its data load use. Built in memory and re-validated."""
    extended = with_extra_universe(config)
    search = config.experiments
    data = extended.model_dump()
    data["universe"] = [*extended.universe, *search.search_universe]
    if search.search_universe:
        data["groups"][SEARCH_GROUP] = list(search.search_universe)
    data["leveraged_etfs"] = extended.leveraged_etfs | search.search_leveraged_etfs
    data["experiments"]["search_universe"] = []
    data["experiments"]["search_leveraged_etfs"] = {}
    return StrideConfig.model_validate(data)


def effective_config(config: StrideConfig) -> StrideConfig:
    """The configuration a run uses: `config` plus the enabled experiments that
    change the universe (the ensemble is read by the engine itself)."""
    if "extra_universe" in config.experiments.enabled:
        return with_extra_universe(config)
    return config
