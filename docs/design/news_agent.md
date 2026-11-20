# Design: `paper_news`, a rule-based news agent (owner request, 2026-11-19)

**Pre-registered: written before the rules ran on any past data.** Exploratory: one more strategy on data the project has already used, so any result carries selection bias.

## 1. Goal and constraints

The owner asked for an account that reads breaking news constantly and buys or sells on it, including following large investors ("a big investor put X million into something, so it is good to copy it"), **without paying for an LLM API**: it must decide by itself. So the agent is a fixed set of rules over headlines. That has one advantage over an LLM: the rules know nothing about the future, so they can be tested honestly on past news (an LLM already "knows" what happened after an old headline).

- Simulated money only (10,000 USD), its own database `data/paper_news.db`, never sends orders to a broker.
- No change to B, DIP, Kelly, MC or RISK.
- Free data only: Alpaca news (Benzinga, with the existing paper keys) and Alpaca daily bars. SEC EDGAR is left out: it refuses automated requests without a declared contact (name and e-mail), which only the owner can give.

## 2. Universe (fixed here)

40 large, liquid US stocks, each with the names a headline uses for it:

AAPL, MSFT, NVDA, AMZN, GOOGL, META, TSLA, AVGO, JPM, V, MA, UNH, XOM, JNJ, PG, HD, COST, ABBV, MRK, PEP, KO, WMT, BAC, CVX, LLY, ORCL, CRM, ADBE, AMD, NFLX, INTC, CSCO, QCOM, TXN, IBM, DIS, NKE, MCD, CAT, GS.

Known bias: these are today's large companies (survivorship bias). The backtest therefore compares against an equal-weight buy-and-hold of the same 40, not only against SPY.

## 3. Events (fixed here)

A headline counts for a stock only if the stock's name or ticker appears **as the subject of the action** (after "upgrades", "stake in", and so on, or before "insider buys"). This matters: Benzinga tags "JPMorgan Chase Downgrades Expedia" with JPM, and that headline says nothing about JPM. One event per headline at most (the first rule that matches, in this order).

| Event | Pattern (case-insensitive, on the headline) | Points |
| --- | --- | --- |
| `INSIDER_BUY` | `<name> ... insider (buys / bought / purchases)` | +2 |
| `STAKE_UP` | `(increases / raises / boosts / builds / takes / acquires / initiates / discloses / adds) ... stake in <name>` | +2 |
| `UPGRADE` | `upgrades <name> to ...`, or `<name> ... receives ... upgrade` | +1 |
| `GUIDANCE_UP` | `<name> ... (raises / boosts / lifts) ... (guidance / outlook / forecast)` | +1 |
| `BEAT` | `<name> ... (beats / tops / exceeds)` | +1 |
| `BUYBACK` | `<name> ... (buyback / repurchase)` | +1 |
| `TARGET_UP` | `raises <name> price target`, or `price target on <name> to $X from $Y` with X > Y | +0.5 |
| `TARGET_DOWN` | `lowers / cuts <name> price target`, or `... to $X from $Y` with X < Y | -0.5 |
| `DOWNGRADE` | `downgrades <name> to ...`, or `<name> ... receives ... downgrade`; to Sell / Underperform / Underweight counts double | -1 / -2 |
| `STAKE_DOWN` | `(reduces / cuts / trims / sells / exits / lowers) ... stake in <name>` | -1 |
| `MISS` | `<name> ... (misses / falls short)` | -1 |
| `GUIDANCE_DOWN` | `<name> ... (lowers / cuts / slashes / withdraws) ... (guidance / outlook / forecast)` | -1.5 |
| `REGULATOR` | `<name> ... (SEC investigation / DOJ / FBI / fraud / accounting / delays ... filing)` | -2 |

Insider **sales** and ordinary lawsuits are ignored: they are frequent and say little about large companies.

## 4. Decisions (fixed here)

For each session d (decided with news published **before 09:30 New York of d**, filled at the open of d):

- **Score** of a stock = sum of its event points over the last 5 sessions.
- **Buy** a stock not held when its score is at least +2, while fewer than 10 are held: 10% of equity each, from cash only (no leverage). Highest score first; ties by ticker.
- **Market brake:** no new buys while SPY's previous close is below its 200-day average (the trend brake that did the work in the RISK study).
- **Sell** a held stock when its score is -1 or lower, after 20 sessions held, or when its previous close is 10% or more below the entry price.
- Costs: IBKR Pro Tiered (0.0035 USD per share, 0.35 USD minimum, 1% maximum, plus 0.003 USD per share of third-party fees) and 5 basis points of spread per side, as the other accounts.
- Kill switch: if equity falls 20% below its peak, sell everything and stop.

## 5. Running it

`python -m stride.run_news --config config.paper_news.yaml`, in the GitHub Actions loop after the other accounts, never failing the run:

- **Every tick (5 minutes):** download the news published since the last stored one and record its events. A strong event (2 points or more, or -2 or less) on a universe stock sends one Telegram message right away, in plain Spanish, saying what happens at the next open.
- **Once per session, after 18:00 New York:** download the day's bars and process the session (orders at its open, values at its close), then send the daily summary.

## 6. Backtest (before any paper result)

Same code, news from Alpaca since 2021-01-01:

- 2021-2023: does it work at all;
- 2024 to today: holdout, read once.

Nothing is tuned after reading either period. Benchmarks: SPY and the equal-weight buy-and-hold of the 40 stocks.

## 7. How it is judged (forward paper, at 60 sessions)

All four must hold:

1. no kill switch;
2. net return above SPY over the same sessions;
3. costs below 2% of capital;
4. at least 10 round trips (fewer cannot be judged).

If they fail it is stopped (`enabled: false`). Its rules are not tuned while it runs: tuning restarts the experiment.

## 8. Amendments before any backtest run (2026-11-19)

- **History.** Alpaca's free news history starts on 2026-04-29: requests for March 2024, June 2025 or March 2026 return no article. The 2021-2023 / 2024-today split of section 6 is therefore impossible. The backtest covers 2026-05-06 (once 5 sessions of news exist) to the last stored bar, one window of about 140 sessions: far too short to judge an edge. It is a sanity check (do the rules fire, do the trades and costs look right), not evidence. The forward paper test of section 7 is the real test.
- **Precision of the rules**, found on 600 recent headlines before any backtest and fixed in the code: beats and misses need an earnings word (`Q1`-`Q4`, earnings, EPS, revenue, sales, results) or an expectations word (estimate, expectations, consensus); guidance must be the company's own (not "forecast for Apple" by a bank); a regulator event needs the company to face it (faces, under, charged, accused); tickers match in capitals only (`COST` is not "cost", `CAT` is not "cat").

## 9. Sanity backtest result (run once, 2026-11-19, `scripts/research/news_backtest.py`)

2026-05-06 to 2026-11-19, 138 sessions, local copy of Alpaca news (13,139 articles, 832 events) and bars:

| | CAGR | Sharpe | Max drawdown | Total |
| --- | --- | --- | --- | --- |
| news agent | -21.2% | -1.50 | 17.4% | -12.2% |
| SPY | -3.6% | -0.18 | 8.1% | -2.0% |
| equal-weight 40 stocks | -1.0% | -0.01 | 6.7% | -0.5% |

- The mechanics work: 56 buys, sells by time (26), stop (13; gaps at the open made several stops much worse than -10%) and score (7); costs 85 USD, 0.85% of capital.
- Figures after the review fixes of PR #129 (exact score window in the first sessions, last known close for a missing mark). The first run, before those fixes, gave -9.1%; the fixes are corrections of the code to the design, not tuning.
- The rules lost money in this window. Most buys reached +2 through several price-target raises (0.5 each), which tend to come after a stock already rose; stake and insider events were rare (17 and 0).
- 138 sessions is far too short to conclude anything either way. Nothing was tuned after reading it: changing the rules now would fit them to these 7 months. The forward paper test (section 7) decides.
