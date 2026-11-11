# Monte Carlo portfolio (`paper_mc`): asset universe

Phase 1 of `docs/handoff_montecarlo.es.md`. Status: **proposal, waiting for the owner's approval**. Nothing here changes code, configs or the B and DIP instances.

## How the data was collected

`scripts/research/mc_universe.py` (drafted by the Antigravity worker, reviewed and run by Claude Code on 2026-11-11) reads 48 candidate ETFs from Alpaca with the paper keys, GET requests only:

- asset flags (`tradable`, `fractionable`, `shortable`) from the paper trading API;
- daily bars since 2016 (SIP, adjusted): 3-year annualized volatility, median daily dollar volume of the last 60 sessions, and the correlation matrix of daily log returns;
- hourly bars of the last 12 months, regular session only: median and 90th percentile of the absolute 1-hour move;
- quoted spread: median of the SIP quotes in five 1-minute windows (10:30 to 14:30 New York) of the session of 2026-11-10. It is a single session, so read it as an order of magnitude.

Outputs go to `reports/mc_universe/` (git-ignored). Checks: all 48 symbols answered every endpoint; all 48 are tradable and fractionable on Alpaca; for the symbols also stored in `data/stride.db` (Tiingo), daily returns from both sources correlate above 0.98.

Limit of the correlation matrix: it uses the dates common to all 48 symbols, and ETHA only exists since 2024-07-23, so the window is 578 sessions (about 2.3 years), not 3 years.

## Selection criteria

1. **One asset per risk source.** Of two candidates correlated above about 0.85, keep one.
2. **Enough movement to pay the costs.** The cost model is IBKR Pro Tiered with a 0.35 USD minimum per order. On a 100 USD order that is 35 bps per side, about 70 bps per round trip before spread. Assets with very low volatility (short and mid Treasuries, TIPS, currency funds) cannot clear that, so they are left out, and between two correlated candidates the more volatile one is preferred.
3. **Tight spread and deep hourly history** (Alpaca serves 1-hour bars since 2016 for all but the crypto ETFs).
4. **Every class the owner asked for is represented**: US and international equities, sectors, bonds, gold, silver, commodities, real estate, dollar, crypto.

## Proposed universe (20 assets)

| Symbol | Exposure | Vol 3y | USD M/day | Spread bps | Median 1h move bps | Hourly since | Closest in the list |
| --- | --- | ---: | ---: | ---: | ---: | --- | --- |
| QQQ | US equity, large growth | 21% | 24,229 | 0.6 | 17 | 2016-01 | IWM +0.78 |
| IWM | US equity, small caps | 21% | 6,405 | 0.7 | 17 | 2016-01 | QQQ +0.78 |
| XLE | US sector, energy | 22% | 1,975 | 1.6 | 21 | 2016-01 | USO +0.62 |
| XLV | US sector, health care | 15% | 1,403 | 1.1 | 15 | 2016-01 | VNQ +0.59 |
| XLU | US sector, utilities | 16% | 1,000 | 2.3 | 17 | 2016-01 | VNQ +0.59 |
| EFA | Developed ex-US equity | 15% | 1,236 | 1.0 | 11 | 2016-01 | IWM +0.72 |
| FXI | China equity | 27% | 557 | 2.9 | 12 | 2016-01 | EFA +0.51 |
| INDA | India equity | 15% | 232 | 1.9 | 8 | 2016-01 | EFA +0.59 |
| EWZ | Brazil equity | 28% | 1,158 | 3.1 | 22 | 2016-01 | EFA +0.48 |
| TLT | US Treasuries 20y+ | 13% | 2,916 | 1.2 | 8 | 2016-01 | VNQ +0.37 |
| GLD | Gold | 22% | 3,375 | 0.5 | 17 | 2016-01 | SLV +0.80 |
| SLV | Silver | 45% | 890 | 2.2 | 34 | 2016-01 | GLD +0.80 |
| CPER | Copper | 29% | 16 | 2.7 | 16 | 2016-01 | EFA +0.55 |
| USO | Crude oil | 38% | 737 | 2.8 | 35 | 2016-01 | XLE +0.62 |
| UNG | Natural gas | 58% | 223 | 9.8 | 40 | 2016-01 | XLE +0.16 |
| DBA | Agriculture | 14% | 30 | 3.8 | 8 | 2016-01 | EFA +0.24 |
| VNQ | US real estate | 16% | 380 | 1.0 | 15 | 2016-01 | XLV +0.59 |
| UUP | US dollar index | 7% | 41 | 3.4 | 4 | 2016-01 | EFA -0.42 |
| IBIT | Bitcoin | 48% | 1,880 | 2.4 | 30 | 2024-01 | ETHA +0.83 |
| ETHA | Ether | 70% | 593 | 2.2 | 38 | 2024-07 | IBIT +0.83 |

Inside the list the average absolute pairwise correlation is 0.23 (all 48 candidates: 0.31) and the highest pair is 0.83 (IBIT and ETHA). Both crypto ETFs are kept because they are the two most volatile assets and the weights are dynamic; the design (phase 2) needs a joint cap for them, and another for GLD and SLV (0.80).

Symbols without daily history in `data/stride.db`, to be downloaded in phase 3: XLU, FXI, INDA, EWZ, UNG.

## Finding that shapes phase 2: costs against the 1-hour horizon

In the proposed list the median 1-hour move is 4 to 40 bps and the 90th percentile is 13 to 126 bps. A round trip on a 100 USD order costs about 70 bps of commission plus 0.5 to 9.8 bps of spread per side under the IBKR model. So with 1,000 USD a 1-hour forecast will almost never have an expected gain above its cost: the portfolio can *evaluate* every hour, but most trades will be justified by the 1-day and 5-day horizons. Larger and fewer positions lower the cost in bps (0.35 USD on 250 USD is 14 bps per side). The alternative is to model Alpaca's own commission (zero) for this instance; that is an owner decision because it changes what the paper result means.

UUP is the weakest member by this test (6.5% volatility): it is in the list because it is the only asset with negative correlation to most of the others, but it is expected to trade rarely.

## Candidates left out

| Symbol | Reason (correlations over the 578-session window) |
| --- | --- |
| SPY | 0.95 with QQQ, 0.85 with IWM; less volatile than both |
| DIA | 0.89 with SPY |
| XLK | 0.97 with QQQ |
| SMH | 0.91 with QQQ |
| XLF | 0.72 with IWM, 0.86 with DIA |
| XLI | 0.83 with IWM |
| XLY | 0.78 with QQQ, 0.85 with SPY |
| XLB | 0.74 with IWM |
| XLC | history only from 2018-06; 0.66 with QQQ |
| XLP | kept out to limit the number of equity names; first reserve |
| VGK | 0.98 with EFA |
| EWJ | 0.87 with EFA |
| EEM | 0.78 with EFA and 0.61 with FXI, both in the list |
| SHY | 1.6% volatility: no move can pay the costs |
| IEF | 0.91 with TLT and half its volatility |
| TIP | 0.87 with IEF; 4.4% volatility |
| LQD | 0.90 with TLT |
| HYG | 0.77 with IWM; 4.8% volatility |
| EMB | 0.84 with HYG; 7.0% volatility |
| BWX | USD 10M traded a day, 4.7 bps spread |
| GDX | 0.85 with GLD (shares of gold miners) |
| PPLT | 7.2 bps spread; 0.80 with SLV |
| DBC | 0.90 with USO |
| FXY | -0.71 with UUP; USD 9M traded a day |
| FXE | -0.96 with UUP; USD 7M traded a day |
| SH, PSQ, TBF | inverse funds: open owner decision, see below |

## Open decisions for the owner

1. Approve or edit the list of 20.
2. Bearish signal: cash only (recommended to start: an inverse fund is the mirror of an asset the simulation already covers, and SH and TBF are thin) or also SH and PSQ.
3. Cost model for `paper_mc`: IBKR Pro Tiered as in B and DIP (recommended, conservative) or Alpaca's zero commission.

## Appendix: all 48 candidates

| Symbol | Class | Vol 3y % | USD M/day | Spread bps | Median 1h bps | P90 1h bps | Fractionable | Shortable | Hourly since | In stride.db since |
| --- | --- | ---: | ---: | ---: | ---: | ---: | --- | --- | --- | --- |
| SPY | US equity broad | 15.4 | 31,321 | 0.27 | 11.7 | 37.2 | yes | yes | 2016-01-01 | 2007-01-03 |
| QQQ | US equity broad | 20.9 | 24,229 | 0.59 | 17.0 | 52.4 | yes | yes | 2016-01-01 | 2007-01-03 |
| IWM | US equity broad | 21.1 | 6,405 | 0.72 | 17.4 | 56.2 | yes | yes | 2016-01-01 | 2007-01-03 |
| DIA | US equity broad | 13.9 | 1,647 | 0.77 | 11.7 | 35.3 | yes | yes | 2016-01-01 |  |
| XLE | US sectors | 22.2 | 1,975 | 1.61 | 20.7 | 63.4 | yes | yes | 2016-01-01 | 2007-01-03 |
| XLF | US sectors | 16.2 | 1,812 | 1.72 | 14.4 | 40.9 | yes | yes | 2016-01-01 | 2007-01-03 |
| XLI | US sectors | 16.9 | 1,298 | 1.20 | 16.7 | 49.2 | yes | yes | 2016-01-04 | 2007-01-03 |
| XLK | US sectors | 25.9 | 1,396 | 1.18 | 22.2 | 70.6 | yes | yes | 2016-01-04 | 2007-01-03 |
| XLV | US sectors | 15.2 | 1,403 | 1.06 | 15.3 | 41.9 | yes | yes | 2016-01-04 | 2007-01-03 |
| XLU | US sectors | 16.0 | 1,000 | 2.30 | 17.4 | 49.9 | yes | yes | 2016-01-04 |  |
| XLP | US sectors | 13.4 | 961 | 1.10 | 14.4 | 38.9 | yes | yes | 2016-01-04 |  |
| XLY | US sectors | 20.9 | 734 | 0.83 | 17.0 | 51.5 | yes | yes | 2016-01-04 |  |
| XLB | US sectors | 17.4 | 568 | 1.90 | 17.2 | 50.8 | yes | yes | 2016-01-04 |  |
| XLC | US sectors | 17.0 | 631 | 0.93 | 15.3 | 45.0 | yes | yes | 2018-06-19 |  |
| SMH | US sectors | 38.3 | 3,985 | 3.35 | 31.3 | 95.4 | yes | yes | 2016-01-04 |  |
| EFA | International equity | 15.3 | 1,236 | 0.96 | 11.2 | 35.8 | yes | yes | 2016-01-04 | 2007-01-03 |
| EEM | International equity | 20.5 | 1,342 | 1.65 | 16.0 | 52.5 | yes | yes | 2016-01-04 | 2007-01-03 |
| VGK | International equity | 15.6 | 179 | 1.12 | 11.0 | 35.3 | yes | yes | 2016-01-04 |  |
| EWJ | International equity | 19.8 | 343 | 1.11 | 13.0 | 42.3 | yes | yes | 2016-01-04 |  |
| FXI | International equity | 27.0 | 557 | 2.91 | 11.6 | 34.6 | yes | yes | 2016-01-04 |  |
| INDA | International equity | 14.9 | 232 | 1.93 | 7.9 | 26.7 | yes | yes | 2016-01-04 |  |
| EWZ | International equity | 27.8 | 1,158 | 3.11 | 22.1 | 62.6 | yes | yes | 2016-01-04 |  |
| SHY | Bonds | 1.6 | 330 | 1.23 | 1.2 | 3.7 | yes | yes | 2016-01-04 |  |
| IEF | Bonds | 6.1 | 870 | 1.10 | 3.3 | 12.3 | yes | yes | 2016-01-04 | 2007-01-03 |
| TLT | Bonds | 12.7 | 2,916 | 1.24 | 7.7 | 23.6 | yes | yes | 2016-01-01 | 2007-01-03 |
| TIP | Bonds | 4.4 | 204 | 0.95 | 2.8 | 9.3 | yes | yes | 2016-01-04 |  |
| LQD | Bonds | 6.8 | 3,190 | 0.96 | 4.8 | 15.0 | yes | yes | 2016-01-04 |  |
| HYG | Bonds | 4.8 | 3,435 | 1.30 | 3.8 | 10.4 | yes | yes | 2016-01-04 |  |
| EMB | Bonds | 7.0 | 696 | 1.09 | 4.3 | 13.0 | yes | yes | 2016-01-04 |  |
| BWX | Bonds | 8.4 | 10 | 4.68 | 4.7 | 17.8 | yes | no | 2016-01-04 | 2007-10-11 |
| GLD | Metals | 22.0 | 3,375 | 0.54 | 16.7 | 53.8 | yes | yes | 2016-01-01 | 2007-01-03 |
| SLV | Metals | 44.8 | 890 | 2.16 | 33.7 | 115.6 | yes | yes | 2016-01-04 | 2007-01-03 |
| GDX | Metals | 41.0 | 1,950 | 2.28 | 39.2 | 118.0 | yes | yes | 2016-01-01 |  |
| CPER | Metals | 29.1 | 16 | 2.66 | 16.0 | 49.6 | yes | yes | 2016-01-04 | 2011-11-15 |
| PPLT | Metals | 37.1 | 34 | 7.19 | 31.1 | 93.8 | yes | yes | 2016-01-04 |  |
| DBC | Commodities | 17.3 | 23 | 3.27 | 15.1 | 46.7 | yes | yes | 2016-01-04 | 2007-01-03 |
| DBA | Commodities | 14.1 | 30 | 3.82 | 7.9 | 26.8 | yes | yes | 2016-01-04 | 2007-01-05 |
| USO | Commodities | 37.9 | 737 | 2.83 | 34.8 | 111.6 | yes | yes | 2016-01-01 | 2007-01-03 |
| UNG | Commodities | 58.4 | 223 | 9.76 | 40.5 | 122.6 | yes | yes | 2016-01-04 |  |
| VNQ | Real estate | 16.4 | 380 | 1.03 | 15.2 | 41.8 | yes | yes | 2016-01-04 | 2007-01-03 |
| UUP | Currencies | 6.5 | 41 | 3.43 | 3.7 | 12.8 | yes | yes | 2016-01-04 | 2007-03-01 |
| FXY | Currencies | 9.9 | 9 | 1.73 | 4.2 | 13.6 | yes | yes | 2016-01-04 | 2007-02-13 |
| FXE | Currencies | 6.7 | 7 | 0.97 | 3.8 | 13.0 | yes | no | 2016-01-04 |  |
| IBIT | Crypto ETFs | 48.5 | 1,880 | 2.41 | 30.3 | 94.7 | yes | yes | 2024-01-11 | 2024-01-11 |
| ETHA | Crypto ETFs | 69.5 | 593 | 2.18 | 38.4 | 126.4 | yes | yes | 2024-07-23 | 2024-07-23 |
| SH | Inverse | 15.1 | 299 | 3.01 | 11.5 | 36.9 | yes | no | 2016-06-27 |  |
| PSQ | Inverse | 21.0 | 438 | 3.67 | 16.9 | 52.9 | yes | yes | 2016-01-04 |  |
| TBF | Inverse | 12.6 | 5 | 3.88 | 8.4 | 24.7 | yes | no | 2016-01-04 |  |
