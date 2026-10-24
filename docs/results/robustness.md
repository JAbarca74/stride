# Robustness of crypto_dip (docs/design/dip_robustness.md)

**Verdict: NOT ROBUST**

## 1. Parameters moved by -20% and +20% (walk-forward, out-of-sample only)

| run | cagr | spy_cagr | sharpe | spy_sharpe | max_drawdown | beats_spy |
| --- | --- | --- | --- | --- | --- | --- |
| as registered | 0.224 | 0.149 | 1.304 | 0.825 | 0.288 | True |
| dip_drop -20% (0.2) | 0.228 | 0.149 | 1.267 | 0.825 | 0.324 | True |
| dip_drop +20% (0.3) | 0.166 | 0.149 | 1.043 | 0.825 | 0.283 | True |
| dip_window -20% (72) | 0.230 | 0.149 | 1.325 | 0.825 | 0.287 | True |
| dip_window +20% (108) | 0.223 | 0.149 | 1.291 | 0.825 | 0.293 | True |
| take_profit -20% (0.24) | 0.206 | 0.149 | 1.227 | 0.825 | 0.288 | True |
| take_profit +20% (0.36) | 0.220 | 0.149 | 1.274 | 0.825 | 0.289 | True |

## 2. Extreme years left out (good and bad alike)

| run | cagr | spy_cagr | sharpe | spy_sharpe | max_drawdown | beats_spy |
| --- | --- | --- | --- | --- | --- | --- |
| all years | 0.224 | 0.149 | 1.304 | 0.825 | 0.288 | True |
| without the best (2017) and the worst (2022) year | 0.182 | 0.175 | 1.118 | 0.967 | 0.288 | True |
| without the crypto bubbles (2017, 2021) | 0.116 | 0.134 | 0.724 | 0.707 | 0.288 | False |
| without the crashes (2018, 2020, 2022) | 0.300 | 0.199 | 1.786 | 1.315 | 0.214 | True |
| without all of them | 0.166 | 0.188 | 1.139 | 1.187 | 0.214 | False |

## 3. One continuous account from each start year (drawdown scaling on)

| run | cagr | spy_cagr | sharpe | spy_sharpe | max_drawdown | beats_spy | kill_switch |
| --- | --- | --- | --- | --- | --- | --- | --- |
| continuous from 2012 | 0.203 | 0.148 | 1.266 | 0.819 | 0.226 | True | False |
| continuous from 2015 | 0.209 | 0.136 | 1.198 | 0.697 | 0.243 | True | False |
| continuous from 2018 | 0.075 | 0.143 | 0.406 | 0.659 | 0.258 | False | False |
