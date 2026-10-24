# STRIDE Configuration Reference (`config.yaml`)

This document provides a comprehensive reference for every configuration parameter in [`config.yaml`](../config.yaml). The configuration values are frozen before live execution; modifying them restarts experimental evaluation.

For the formal mathematical specifications and architectural contracts, refer to [`docs/SPEC.md`](SPEC.md).

---

## Table of Contents

- [General & Mode](#general--mode)
- [Asset Universe & Groups](#asset-universe--groups)
- [Leveraged ETFs & Filters](#leveraged-etfs--filters)
- [Leverage Switching & Hysteresis](#leverage-switching--hysteresis)
- [Strategy Parameters](#strategy-parameters)
- [Risk Management & Circuit Breakers](#risk-management--circuit-breakers)
- [Execution & Broker Gateway](#execution--broker-gateway)
- [Cash Flows & Deposits](#cash-flows--deposits)
- [Transaction Costs & Fees](#transaction-costs--fees)
- [Market Data & Quality Checks](#market-data--quality-checks)
- [Research Experiments](#research-experiments)

---

## General & Mode

| Parameter | Default | Unit | Description | SPEC Section |
| :-- | :-- | :-- | :-- | :-- |
| `mode` | `paper` | string | Execution mode: `backtest` (SimBroker on historical data), `paper` (Alpaca paper trading), or `live` (real capital execution). | [SPEC §8 & §11](SPEC.md#8-daily-job-orchestration-and-paper-deployment) |

---

## Asset Universe & Groups

| Parameter | Default | Unit | Description | SPEC Section |
| :-- | :-- | :-- | :-- | :-- |
| `universe` | `[SPY, EFA, IEF, TLT, GLD, SLV, DBC, UUP, IBIT, ETHA]` | list of tickers | Core trading universe of 10 ETFs spanning 8 macro asset classes (equities, treasuries, metals, commodities, currencies, crypto). | [SPEC §4 & §6](SPEC.md#4-universe-selection-and-asset-coverage) |
| `groups.equities` | `[SPY, EFA]` | list of tickers | Correlated equity asset cluster sharing joint unit risk limits. | [SPEC §6 & §7](SPEC.md#6-core-trading-rules) |
| `groups.bonds` | `[IEF, TLT]` | list of tickers | Correlated fixed-income asset cluster sharing joint unit risk limits. | [SPEC §6 & §7](SPEC.md#6-core-trading-rules) |
| `groups.metals` | `[GLD, SLV]` | list of tickers | Correlated precious metals cluster sharing joint unit risk limits. | [SPEC §6 & §7](SPEC.md#6-core-trading-rules) |
| `groups.crypto` | `[IBIT, ETHA]` | list of tickers | Correlated digital asset cluster sharing joint unit risk limits. | [SPEC §6 & §7](SPEC.md#6-core-trading-rules) |

---

## Leveraged ETFs & Filters

| Parameter | Default | Unit | Description | SPEC Section |
| :-- | :-- | :-- | :-- | :-- |
| `leveraged_etfs` | *(mapping)* | mapping | Mapping of underlying 1x ETFs to candidate 2x and 3x leveraged versions (e.g. `SPY: {2: SSO, 3: UPRO}`, `TLT: {2: UBT, 3: TMF}`). Exposure is achieved without borrowing margin. | [SPEC §7](SPEC.md#7-risk-management-sizing-and-portfolio-construction) |
| `leveraged_etf_filters.max_median_spread` | `0.002` | fraction (0.20%) | Maximum 30-day median bid-ask spread permitted under SEC Rule 6c-11 disclosures for a leveraged ETF to be eligible for trading. | [SPEC §7](SPEC.md#7-risk-management-sizing-and-portfolio-construction) |
| `leveraged_etf_filters.min_aum_usd` | `50000000` | USD ($50M) | Minimum Assets Under Management (AUM) required to eliminate illiquid or closure-risk leveraged funds. | [SPEC §7](SPEC.md#7-risk-management-sizing-and-portfolio-construction) |

---

## Leverage Switching & Hysteresis

| Parameter | Default | Unit | Description | SPEC Section |
| :-- | :-- | :-- | :-- | :-- |
| `leverage_switch.enter_k` | `1.25` | multiplier | Effective exposure threshold above which the system switches from 1x ETF to 2x/3x leveraged ETF. | [SPEC §7 (Step 2b)](SPEC.md#7-risk-management-sizing-and-portfolio-construction) |
| `leverage_switch.exit_k` | `0.95` | multiplier | Effective exposure threshold below which the system rotates back from leveraged ETF to 1x ETF. | [SPEC §7 (Step 2b)](SPEC.md#7-risk-management-sizing-and-portfolio-construction) |
| `leverage_switch.min_hold_days` | `20` | trading days | Minimum holding lock duration on switched instruments to prevent rapid whipsaw churn and excess spread cost. | [SPEC §7 (Step 2b)](SPEC.md#7-risk-management-sizing-and-portfolio-construction) |

---

## Strategy Parameters

| Parameter | Default | Unit | Description | SPEC Section |
| :-- | :-- | :-- | :-- | :-- |
| `strategy.sma_filter` | `200` | trading days | Paul Tudor Jones 200-day Simple Moving Average trend regime filter. Long positions are prohibited when price is below SMA 200. | [SPEC §6](SPEC.md#6-core-trading-rules) |
| `strategy.entry_breakout` | `55` | trading days | Turtle System 2 entry breakout channel length. Generates buy signal when closing price exceeds the rolling 55-day high (excluding today). | [SPEC §6](SPEC.md#6-core-trading-rules) |
| `strategy.exit_breakout` | `20` | trading days | Turtle System 2 exit channel length. Triggers complete position liquidation when closing price falls below the rolling 20-day low (excluding today). | [SPEC §6](SPEC.md#6-core-trading-rules) |
| `strategy.atr_period` | `20` | trading days | Lookback window for Wilder's Average True Range ($N$), used for volatility-adjusted unit sizing and stop placement. | [SPEC §6](SPEC.md#6-core-trading-rules) |
| `strategy.stop_n` | `2.0` | multiples of $N$ | Ed Seykota volatility stop placed $2.0 \times N$ below entry price or highest close, trailing the trade upward. | [SPEC §6](SPEC.md#6-core-trading-rules) |
| `strategy.pyramid_step_n` | `0.5` | multiples of $N$ | Price advance required above previous unit entry price before adding another unit (up to unit limits). | [SPEC §6](SPEC.md#6-core-trading-rules) |
| `strategy.max_units_per_asset` | `4` | unit count | Maximum number of pyramid units permitted in a single instrument. | [SPEC §6 & §7](SPEC.md#6-core-trading-rules) |

---

## Risk Management & Circuit Breakers

| Parameter | Default | Unit | Description | SPEC Section |
| :-- | :-- | :-- | :-- | :-- |
| `risk.risk_per_unit` | `0.01` | fraction of equity (1%) | Volatility dollar risk budgeted per unit: each unit is sized so that $1N$ True Range movement equals 1% of portfolio equity. | [SPEC §7](SPEC.md#7-risk-management-sizing-and-portfolio-construction) |
| `risk.max_units_group` | `6` | unit count | Exposure ceiling across all instruments in a single correlated asset cluster (equities, bonds, metals). | [SPEC §7](SPEC.md#7-risk-management-sizing-and-portfolio-construction) |
| `risk.max_units_crypto` | `4` | unit count | Dedicated exposure cap for the crypto asset cluster (`IBIT`, `ETHA`). | [SPEC §7](SPEC.md#7-risk-management-sizing-and-portfolio-construction) |
| `risk.max_units_total` | `12` | unit count | Portfolio-wide gross exposure ceiling on aggregate units held concurrently across all 10 assets. | [SPEC §7](SPEC.md#7-risk-management-sizing-and-portfolio-construction) |
| `risk.kelly_fraction` | `1.0` | multiplier | Fraction of unconstrained Kelly criterion leverage to apply (e.g. 1.0 = Full Kelly bound, 0.5 = Half Kelly). | [SPEC §7](SPEC.md#7-risk-management-sizing-and-portfolio-construction) |
| `risk.max_leverage` | `4.0` | multiplier | Hard mathematical ceiling on portfolio effective leverage regardless of other metrics. | [SPEC §7](SPEC.md#7-risk-management-sizing-and-portfolio-construction) |
| `risk.max_etf_portfolio_leverage` | `2.5` | multiplier | Upper limit on portfolio gross exposure attainable via leveraged ETF combinations. | [SPEC §7](SPEC.md#7-risk-management-sizing-and-portfolio-construction) |
| `risk.max_portfolio_vol` | `0.60` | annualized fraction (60%) | Ceiling on forecast annualized portfolio volatility computed via EWMA cross-asset covariance. | [SPEC §7](SPEC.md#7-risk-management-sizing-and-portfolio-construction) |
| `risk.dd_delever_at` | `0.20` | drawdown fraction (-20%) | **De-Leverage Circuit Breaker**: When portfolio drawdown from all-time high exceeds 20%, effective leverage is automatically halved until a new high is formed. | [SPEC §7 & §8](SPEC.md#7-risk-management-sizing-and-portfolio-construction) |
| `risk.kill_switch_dd` | `0.40` | drawdown fraction (-40%) | **Emergency Kill Switch**: When drawdown reaches 40%, the system cancels all pending orders and liquidates all broker positions at the next open. | [SPEC §8](SPEC.md#8-daily-job-orchestration-and-paper-deployment) |
| `risk.allow_margin` | `false` | boolean | Disables broker cash borrowing. Exposure comes strictly from structured leveraged ETFs, capping loss at deposited equity. | [SPEC §1.4 & §7](SPEC.md#14-architectural-invariants) |
| `risk.margin_min_equity` | `2000` | USD ($2,000) | Minimum regulatory equity threshold required if broker margin borrowing were permitted. | [SPEC §7](SPEC.md#7-risk-management-sizing-and-portfolio-construction) |
| `risk.margin_buffer` | `0.25` | fraction (25%) | Additional equity buffer above broker maintenance margin requirements. | [SPEC §7](SPEC.md#7-risk-management-sizing-and-portfolio-construction) |
| `risk.min_history_days_for_kelly` | `250` | trading days | Minimum historical sessions required before enabling dynamic Kelly leverage calculations. | [SPEC §7](SPEC.md#7-risk-management-sizing-and-portfolio-construction) |
| `risk.reconciliation_tolerance` | `0.01` | fraction (1%) | Percentage discrepancy allowed between local accounting ledger and broker-reported positions before raising break alert. | [SPEC §1.3 & §8](SPEC.md#13-reconciliation-and-broker-ledger-truth) |
| `risk.reconciliation_floor_usd` | `1.0` | USD ($1.00) | Minimum dollar threshold below which small fractional position mismatches are ignored. | [SPEC §1.3 & §8](SPEC.md#13-reconciliation-and-broker-ledger-truth) |

---

## Execution & Broker Gateway

| Parameter | Default | Unit | Description | SPEC Section |
| :-- | :-- | :-- | :-- | :-- |
| `execution.rebalance_band` | `0.20` | fraction (20%) | Threshold deviation between target weight and actual weight required to trigger a position resize order. | [SPEC §8](SPEC.md#8-daily-job-orchestration-and-paper-deployment) |
| `execution.min_order_usd` | `1.0` | USD ($1.00) | Minimum notional dollar amount required to submit an order to the broker. | [SPEC §8](SPEC.md#8-daily-job-orchestration-and-paper-deployment) |
| `execution.limit_offset` | `0.005` | fraction (0.50%) | Pricing offset from prior close used for non-market execution protection. | [SPEC §8](SPEC.md#8-daily-job-orchestration-and-paper-deployment) |
| `execution.max_orders_per_day` | `12` | order count | Safety rate limit on total broker orders submitted within a single trading day. | [SPEC §8](SPEC.md#8-daily-job-orchestration-and-paper-deployment) |
| `execution.max_order_pct_equity` | `0.60` | fraction (60%) | Maximum proportion of account equity that can be committed in a single individual order. | [SPEC §8](SPEC.md#8-daily-job-orchestration-and-paper-deployment) |
| `execution.max_resize_cost_pct` | `0.005` | fraction (0.50%) | Suppresses routine HOLD resize top-up orders if estimated transaction costs exceed 0.5% of trade value. | [SPEC §8](SPEC.md#8-daily-job-orchestration-and-paper-deployment) |
| `execution.broker` | `alpaca` | string | Paper broker backend implementation: `alpaca` (Alpaca paper API) or `shadow` (internal SimBroker on stored prices). | [SPEC §8](SPEC.md#8-daily-job-orchestration-and-paper-deployment) |
| `execution.alpaca_key_env` | `ALPACA` | string | Environment variable prefix in `.env` used to resolve Alpaca paper credentials (`ALPACA` or `ALPACA_DIP`). | [SPEC §8](SPEC.md#8-daily-job-orchestration-and-paper-deployment) |

---

## Cash Flows & Deposits

| Parameter | Default | Unit | Description | SPEC Section |
| :-- | :-- | :-- | :-- | :-- |
| `cash_flows.initial_deposit_usd` | `100` | USD ($100) | Starting capital deposit for paper and live forward-testing experiment. | [SPEC §2](SPEC.md#2-experimental-design-and-hypotheses) |
| `cash_flows.monthly_deposit_usd` | `20` | USD ($20/month) | Scheduled monthly capital contribution to simulate systematic personal retail savings. | [SPEC §2](SPEC.md#2-experimental-design-and-hypotheses) |
| `cash_flows.deposit_day` | `first_trading_day` | calendar rule | Specifies when monthly deposits are credited to portfolio equity (first NYSE session of the month). | [SPEC §2](SPEC.md#2-experimental-design-and-hypotheses) |

---

## Transaction Costs & Fees

| Parameter | Default | Unit | Description | SPEC Section |
| :-- | :-- | :-- | :-- | :-- |
| `costs.spread_bps` | `5` | basis points (0.05%) | Modeled half-spread execution cost on standard liquid ETFs. | [SPEC §3 & §8](SPEC.md#3-transaction-costs-and-slippage-model) |
| `costs.spread_bps_crypto` | `10` | basis points (0.10%) | Modeled half-spread cost for crypto ETFs (`IBIT`, `ETHA`). | [SPEC §3 & §8](SPEC.md#3-transaction-costs-and-slippage-model) |
| `costs.pricing` | `tiered` | string | Interactive Brokers commission pricing model (`fixed` or `tiered`). | [SPEC §3](SPEC.md#3-transaction-costs-and-slippage-model) |
| `costs.commission_per_share` | `0.005` | USD/share ($0.005) | IBKR Pro Fixed commission rate per share traded. | [SPEC §3](SPEC.md#3-transaction-costs-and-slippage-model) |
| `costs.commission_min_per_order` | `1.00` | USD ($1.00) | Minimum commission fee per order under Fixed pricing schedule. | [SPEC §3](SPEC.md#3-transaction-costs-and-slippage-model) |
| `costs.commission_max_pct_of_value` | `0.01` | fraction (1.0%) | Maximum commission cap as a percentage of trade value under Fixed pricing. | [SPEC §3](SPEC.md#3-transaction-costs-and-slippage-model) |
| `costs.tiered_per_share` | `0.0035` | USD/share ($0.0035) | IBKR Pro Tiered base commission rate per share for low-volume accounts. | [SPEC §3](SPEC.md#3-transaction-costs-and-slippage-model) |
| `costs.tiered_min_per_order` | `0.35` | USD ($0.35) | Minimum commission fee per order under Tiered pricing schedule. | [SPEC §3](SPEC.md#3-transaction-costs-and-slippage-model) |
| `costs.tiered_max_pct_of_value` | `0.01` | fraction (1.0%) | Maximum commission cap as a percentage of trade value under Tiered pricing. | [SPEC §3](SPEC.md#3-transaction-costs-and-slippage-model) |
| `costs.tiered_third_party_per_share` | `0.003` | USD/share ($0.003) | Estimated pass-through exchange, clearing, and regulatory fees under Tiered pricing. | [SPEC §3](SPEC.md#3-transaction-costs-and-slippage-model) |
| `costs.cash_interest_free_usd` | `10000` | USD ($10,000) | Broker uninvested cash tier below which 0% interest is credited. | [SPEC §3](SPEC.md#3-transaction-costs-and-slippage-model) |
| `costs.cash_interest_full_rate_nav` | `100000` | USD ($100,000) | Net Asset Value (NAV) threshold required before cash earns 100% of the benchmark interest rate. | [SPEC §3](SPEC.md#3-transaction-costs-and-slippage-model) |
| `costs.leveraged_etf_fee` | `0.0095` | annual fraction (0.95%/yr) | Estimated daily expense ratio and management fee drag on leveraged ETFs. | [SPEC §3 & §7](SPEC.md#3-transaction-costs-and-slippage-model) |
| `costs.margin_spread` | `0.015` | annual fraction (1.50%) | Modeled borrowing interest rate spread over benchmark rate (dormant while `allow_margin: false`). | [SPEC §3](SPEC.md#3-transaction-costs-and-slippage-model) |

---

## Market Data & Quality Checks

| Parameter | Default | Unit | Description | SPEC Section |
| :-- | :-- | :-- | :-- | :-- |
| `data.db_path` | `data/stride.db` | filesystem path | SQLite database path storing historical daily bars, equity index, runs, and event audit trails. | [SPEC §5](SPEC.md#5-data-integrity-and-validation-protocol) |
| `data.history_start` | `"2007-01-01"` | ISO date string | Earliest date ingested for standard liquid ETF series. | [SPEC §4 & §5](SPEC.md#4-universe-selection-and-asset-coverage) |
| `data.crypto_history_start` | `"2015-01-01"` | ISO date string | Earliest date ingested for cryptocurrency ETF / underlying series. | [SPEC §4 & §5](SPEC.md#4-universe-selection-and-asset-coverage) |
| `data.quality.missing_days_lookback` | `300` | trading days | Rolling window of NYSE trading sessions scanned for unrecorded missing trading days (Validation 1). | [SPEC §5](SPEC.md#5-data-integrity-and-validation-protocol) |
| `data.quality.max_daily_change` | `0.25` | fraction (25%) | Unflagged single-day close-to-close return threshold. Larger moves trigger data validation flags (Validation 2). | [SPEC §5](SPEC.md#5-data-integrity-and-validation-protocol) |
| `data.quality.source_close_tolerance` | `0.005` | fraction (0.50%) | Discrepancy tolerance between primary (Tiingo) and secondary sources on historical samples (Validation 4). | [SPEC §5](SPEC.md#5-data-integrity-and-validation-protocol) |
| `data.quality.latest_bar_max_diff` | `0.02` | fraction (2.0%) | Maximum allowable discrepancy on today's latest bar between data sources before blocking that symbol for the session (Validation 4). | [SPEC §5](SPEC.md#5-data-integrity-and-validation-protocol) |

---

## Research Experiments

| Parameter | Default | Unit | Description | SPEC Section |
| :-- | :-- | :-- | :-- | :-- |
| `experiments.enabled` | `[]` | list of strings | List of pre-registered research experiments actively enabled in production. Experiments only enter if they outperform out of sample. | [SPEC §9](SPEC.md#9-backtest-protocol-and-robustness-framework) |
| `experiments.extra_universe` | `[EEM, VNQ, BWX, USO, CPER, DBA, FXY]` | list of tickers | Expanded multi-asset universe evaluated during out-of-sample robustness testing. | [SPEC §9](SPEC.md#9-backtest-protocol-and-robustness-framework) |
| `experiments.extra_groups` | *(mapping)* | mapping | Group clusters for the extended research asset universe. | [SPEC §9](SPEC.md#9-backtest-protocol-and-robustness-framework) |
| `experiments.search_universe` | `[QQQ, IWM, XLK, XLV, XLF, XLE, XLI]` | list of tickers | Equity sector and index ETF universe evaluated in pre-registered strategy searches. | [SPEC §9](SPEC.md#9-backtest-protocol-and-robustness-framework) |
| `experiments.search_leveraged_etfs` | `QQQ: {2: QLD, 3: TQQQ}` | mapping | Candidate leveraged ETFs for strategy searches. | [SPEC §9](SPEC.md#9-backtest-protocol-and-robustness-framework) |
| `experiments.breakout_systems` | `[[20, 10], [55, 20], [100, 40]]` | list of pairs | Alternative breakout channel parameters evaluated in sensitivity analysis. | [SPEC §9](SPEC.md#9-backtest-protocol-and-robustness-framework) |
| `experiments.dd_scale_start` | `0.05` | drawdown fraction (-5%) | Drawdown threshold where linear drawdown scaling begins reducing leverage. | [SPEC §9](SPEC.md#9-backtest-protocol-and-robustness-framework) |
| `experiments.dd_scale_full` | `0.20` | drawdown fraction (-20%) | Drawdown threshold where linear drawdown scaling reaches the minimum floor. | [SPEC §9](SPEC.md#9-backtest-protocol-and-robustness-framework) |
| `experiments.dd_scale_floor` | `0.25` | fraction (25%) | Minimum scaling multiplier applied to portfolio leverage during drawdown scaling. | [SPEC §9](SPEC.md#9-backtest-protocol-and-robustness-framework) |
| `experiments.dd_scale_window` | `0` | trading days | Rolling lookback peak window for drawdown scaling (0 = all-time peak). | [SPEC §9](SPEC.md#9-backtest-protocol-and-robustness-framework) |
| `experiments.rotation` | *(mapping)* | mapping | Parameters for Gayed & Bilello (2016) leverage rotation strategy (`underlying: SPY`, `sma: 200`, `multiple: 3`, `refuge: IEF`). | [SPEC §9](SPEC.md#9-backtest-protocol-and-robustness-framework) |
| `experiments.allocation` | *(mapping)* | mapping | Parameters for systematic allocation searches (dual momentum, volatility managed, risk parity, crypto sleeve). | [SPEC §9](SPEC.md#9-backtest-protocol-and-robustness-framework) |
