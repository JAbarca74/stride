# Leveraged ETF verification (task T21, SPEC 1.2)

**Source of truth: [`reference/etf_facts.yaml`](../reference/etf_facts.yaml).** It holds `expense_ratio`, `aum_usd`, `median_spread_30d`, `as_of` and the source URLs per instrument. This page only summarises it.

Rule (SPEC section 7, `config.yaml` → `leveraged_etf_filters`): a leveraged ETF is eligible only if its **30-day median bid-ask spread ≤ 0.20%** and its **net assets ≥ $50 million**. A missing value fails until it is filled in. An ETF that fails is dropped: the asset trades at 1x, or at the other multiple if that one passes (TLT may go directly from 1x to TMF at 3x). Switching uses the hysteresis in `leverage_switch` (step 2b).

## Pre-filled from SEC EDGAR (2026-10-19)

| Asset | ETF | Multiple | Net expense ratio | Net assets | As of | Spread (owner to fill) |
| --- | --- | --- | --- | --- | --- | --- |
| SPY | SSO | 2x | 0.84% (497K) | $8.38B (N-PORT) | 2026-05-31 | — |
| SPY | UPRO | 3x | 0.88% (497K) | $5.55B (N-PORT) | 2026-05-31 | — |
| TLT | UBT | 2x | 0.95% (497K) | $64.0M (N-PORT) | 2026-05-31 | — |
| TLT | TMF | 3x | 0.90% (497K) | $2.20B (N-PORT) | 2026-07-31 | — |
| GLD | UGL | 2x | 0.95% (10-K fee) | $641M (10-Q equity) | 2026-06-30 | — |
| SLV | AGQ | 2x | 0.95% (10-K fee) | $1.27B (10-Q equity) | 2026-06-30 | — |
| IBIT | BITX | 2x | owner | owner | — | — |
| ETHA | ETHU | 2x | owner | owner | — | — |

For reference only, and **not** entered in the facts file: on 2026-10-19 the ProShares fund pages showed 30-day median spreads of SSO 0.01%, UPRO 0.02%, UBT 0.35% (fails), UGL 0.02% and AGQ 0.06%. The Direxion page showed 0.04% for TMF. The owner collects the official values.

## Synthetic series (SPEC section 9)

`src/stride/data/leveraged.py`:

```
r_syn(t) = m * r_underlying(t) - (m - 1) * (DTB3(t-1) + spread) / 252 - fee / 252
```

- `fee` is the ETF's own net expense ratio from the facts file (`expense_ratio_for`). `costs.leveraged_etf_fee` is used only as a fallback, and doing so logs a warning.
- `DTB3` comes from FRED (`FredRateSource`, no key). `rate_known_at_t_minus_1` uses the last value published before day t.
- `calibrate_spread` finds the financing spread per asset on the overlap between the real ETF and its underlying. `Calibration.passes` requires less than 2% per year.
- **Pending:** the calibration on real prices needs Tiingo data (`DATA_API_KEY`).
