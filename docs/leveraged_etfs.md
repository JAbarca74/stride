# Leveraged ETF verification (task T21)

Rule (SPEC section 7, `config.yaml` → `leveraged_etf_filters`): a leveraged ETF is eligible only if its **30-day median bid-ask spread ≤ 0.20%** and its **net assets ≥ $50 million**. An ETF that fails is dropped and its asset trades at 1x, or at the other multiple if that one passes. Missing data counts as a fail until it is verified.

Source: the issuers' own fund pages (published under SEC rule 6c-11), read on **2026-10-19**. These figures change daily; re-check before Phase 4. IBKR-specific checks (permissions for leveraged products, fractional shares, real commission) are done in Phase 4 with the paper account.

| Asset | ETF | Multiple | Issuer | 30-day median spread | Net assets | Net expense ratio | Inception | Result |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| SPY | SSO | 2x | ProShares | 0.01% | $8.87B | 0.84% (gross 0.88%) | 2006-06-19 | **Pass** |
| SPY | UPRO | 3x | ProShares | 0.02% | $5.40B | 0.88% | 2009-06-23 | **Pass** |
| TLT | UBT | 2x | ProShares | 0.35% | $68.6M | 0.95% (gross 0.98%) | 2010-01-19 | **Fail**: spread above 0.20% |
| TLT | TMF | 3x | Direxion | 0.04% (as of 2026-10-16) | not shown on the fund page | 0.90% (gross 1.01%) | 2009-04-16 | **Pending**: net assets to verify |
| GLD | UGL | 2x | ProShares | 0.02% | $716M | 0.95% | 2008-12-01 | **Pass** |
| SLV | AGQ | 2x | ProShares | 0.06% | $1.11B | 0.95% | 2008-12-01 | **Pass** |
| IBIT | BITX | 2x | Volatility Shares | not retrieved | not retrieved | not retrieved | | **Pending**: the fund page is behind a disclaimer that was not accepted |

## Notes for the owner

- **TLT at 2x drops out** (UBT's spread is 0.35%). TLT can only use TMF at 3x, once TMF's net assets are confirmed. `config.yaml` still lists UBT, because the map is a parameter; the filter is applied when instruments are chosen.
- **Ether:** Volatility Shares also lists **ETHU (2x Ether ETF)**. SPEC section 7 says ETHA has no known liquid leveraged version, so ETHU is not in the map. Adding it is the owner's decision.
- **Fees:** `costs.leveraged_etf_fee` is one value (0.95%), but the net expense ratios above range from 0.84% to 0.95%. The synthetic series should use each ETF's own fee (owner to confirm).

## Synthetic series (SPEC section 9)

`src/stride/data/leveraged.py`:

```
r_syn(t) = m * r_underlying(t) - (m - 1) * (DTB3(t-1) + spread) / 252 - fee / 252
```

- `DTB3` comes from FRED (`FredRateSource`, no key). `rate_known_at_t_minus_1` uses the last value published before day t.
- `calibrate_spread` finds the financing spread per asset on the overlap between the real ETF and its underlying. `Calibration.passes` requires a difference of less than 2% per year.
- **Pending:** running the calibration on real prices needs Tiingo data (`DATA_API_KEY`).
