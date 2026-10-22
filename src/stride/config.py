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
    margin_min_equity: NonNegative
    margin_buffer: Fraction
    min_history_days_for_kelly: Annotated[int, Field(ge=1)]

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


class CashFlowsConfig(_Section):
    monthly_deposit_usd: NonNegative
    deposit_day: Literal["first_trading_day"]


class CostsConfig(_Section):
    spread_bps: NonNegative
    spread_bps_crypto: NonNegative
    commission_per_share: NonNegative
    commission_min_per_order: NonNegative
    commission_max_pct_of_value: NonNegative
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
