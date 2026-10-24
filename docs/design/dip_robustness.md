# Robustness check of `crypto_dip` (owner request, 2026-10-24)

**Written before the check was run.**

## Why

In round 5, `crypto_dip` (an 80% SPY core plus a 20% crypto sleeve, using the owner's buy-the-dip rule) returned 22.4% a year out of sample, with a Sharpe of 1.30 (`docs/design/strategy_search_round5.md`). It missed only the deflated Sharpe ratio: 0.905 against 0.95. Before paper trading, this check asks whether that result is:

- a lucky choice of the three parameters (25% drop, 90-day high, 30% target);
- driven by a few extreme years.

The owner asked that extreme years be removed symmetrically: if the 2017 bubble is removed, the crashes are removed too. The 2008 crash is already outside the out-of-sample period, which runs from 2012 to 2026.

## Tests (`python -m stride.backtest.robustness`, written to `docs/results/robustness.md`)

Every test compares the candidate with SPY over the same days.

1. **Parameters.** This repeats the walk-forward of validation section 9: the drawdown scaling is chosen in sample, at 1x. It is run for the registered rule and for each dip parameter moved by −20% and by +20%, one at a time:
   - `dip_drop` 0.20 and 0.30;
   - `dip_window` 72 and 108;
   - `take_profit` 0.24 and 0.36.

   That makes 7 runs.
2. **Extreme years left out.** These runs take the registered rule's out-of-sample returns and drop the same years from both series:
   - the best year and the worst year;
   - the crypto bubbles (2017, 2021);
   - the crashes (2018, 2020, 2022);
   - all five of those years.
3. **Start years.** One continuous account from 1 January 2012, 2015 and 2018 to the last price, with the drawdown scaling on (window as configured). Each run reports whether the kill switch fired.

## Verdict: ROBUST only if all of these hold

- Every parameter variant has an out-of-sample Sharpe **and** CAGR above SPY's.
- Without the best and the worst year, and without all five extreme years, the Sharpe is above SPY's.
- Every continuous account has a CAGR above SPY's over the same days.

If the rule is ROBUST, it goes to paper trading (T17, plus `execution/orders.py`, which needs the owner's approval). If it is NOT ROBUST, the result is reported as it is, and the parameters are **not** retuned to make it pass.

## Expected outcome, written down in advance

- **Parameter variants:** they likely stay above SPY on Sharpe. The rule is simple, and the crypto sleeve's returns dominate.
- **Without the bubbles:** the CAGR drops a lot, probably below SPY's.
- **Without all extreme years:** the Sharpe may stay close to SPY's.
- **Start years:**
  - 2018 starts with a crypto crash, so it is the hardest;
  - 2012 and 2015 likely beat SPY.

## Result (prices to 2026-10-23, commit 1745078): NOT ROBUST

| Test | Outcome |
| --- | --- |
| Parameters ±20% | **All 7 runs beat SPY** on Sharpe and CAGR (Sharpe 1.04–1.33, CAGR 16.6–23.0%). The rule is not a lucky choice of its parameters. |
| Without the best (2017) and the worst (2022) year | Beats SPY (Sharpe 1.12 against 0.97, CAGR 18.2% against 17.5%). |
| Without the crypto bubbles (2017, 2021) | Fails: CAGR 11.6% against 13.4%. The Sharpe is still slightly above (0.72 against 0.71). |
| Without the crashes (2018, 2020, 2022) | Beats SPY (CAGR 30.0% against 19.9%). |
| Without all five years | Fails: Sharpe 1.14 against 1.19, CAGR 16.6% against 18.8%. |
| Continuous from 2012 / 2015 | Beats SPY: 20.3% against 14.8%, and 20.9% against 13.6%. |
| Continuous from 2018 | **Fails: 7.5% against 14.3%.** The account starts in a crypto crash and waits through it. |

The rule is not fragile in its parameters. Its edge over SPY, however, depends on the crypto bubble years and on the start date. Without 2017 and 2021, or starting in 2018, it does not beat SPY. As pre-registered, the parameters are not retuned.
