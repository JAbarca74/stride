# Pre-registered strategy search, round 4: 12-month drawdown window (owner choice, 2026-10-23)

**Written before these candidates were run on the data.**

## Why

Round 3 (`spy_levered`, SPY held at 1.5x) failed. Out of sample it reached a CAGR of 14.5% and a Sharpe of 0.748. SPY reached 14.9% and 0.825 over the same days.

The year-by-year table showed the cause. The drawdown scaling measures the drawdown from the **all-time** peak of the TWR index, so after a crash the leverage stays near its floor until equity makes a new high:

- The runs start in 2007. After 2008, leverage stayed at 0.375x for years, so 2012 returned 3.4% against SPY's 16%.
- The same pattern cost part of the rebounds after 2015, 2020 and 2022.
- Without scaling, 2008 at 1.5x trips the −40% kill switch and ends the full-period run (CAGR −2%).

The owner approved a new key, `experiments.dd_scale_window`. It measures the scaling drawdown from the peak of the last N trading days (0 means the all-time peak, the old behavior and still the default). Leverage then returns at most N days after a crash. The −40% kill switch is **not** changed: it still uses the all-time peak of the TWR index.

The owner also asked to postpone moving the kill switch to −50% until this round is known.

## Candidates (fixed here)

Both are evaluated on the first trading day of each month.

| Name | Mode | Rule |
| --- | --- | --- |
| `spy_levered_12m` | `hold` on SPY | Always hold SPY. The exposure is 1.5x or 2x through SSO, with the rest in cash. |
| `qqq_levered_12m` | `hold` on QQQ | Always hold QQQ. The exposure is 1.5x or 2x through QLD, with the rest in cash. |

Settings for both:

- Leverage levels: 1.5x and 2x (`ROUND_4_LEVELS`).
- Drawdown scaling on or off, chosen in sample.
- When scaling is on, `dd_scale_window` is 252 trading days (12 months). The start, full and floor values are the configured `experiments.dd_scale_*` (−5%, −20%, 25%).

**Hindsight warning:** QQQ returned about 18% a year over 2012–2026. A QQQ candidate looks good partly for that reason. The deflated Sharpe ratio counts every trial of rounds 1 to 4.

## Test and pass criteria (unchanged)

The test is validation section 10: walk-forward with the leverage and the scaling chosen in the 5 in-sample years, plus full-period runs at each level, with and without scaling.

To pass, a candidate must meet all of these over the out-of-sample years:

- a Sharpe ratio above SPY's;
- a CAGR above SPY's;
- a deflated Sharpe ratio of at least 0.95;
- a maximum drawdown no deeper than 40%.

## Expected outcome, written down in advance

- `spy_levered_12m`:
  - out of sample, a CAGR of about 15–20%, above SPY's;
  - a Sharpe ratio close to SPY's;
  - a deflated Sharpe ratio below 0.95.
- `qqq_levered_12m`:
  - a higher CAGR, around 20–25%;
  - drawdowns near the −40% limit in 2020 and 2022;
  - the deflated Sharpe ratio is the open question.
- At 2x without scaling, the kill switch is likely to fire in 2008, 2020 or 2022.

If no candidate passes, the next step is the owner's choice: a round 5 with the kill switch at −50%, which needs the owner's explicit OK for that value.
