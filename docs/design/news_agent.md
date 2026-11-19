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
