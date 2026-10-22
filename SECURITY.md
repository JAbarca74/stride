# Security policy

STRIDE can place real orders with a broker, so security reports are taken seriously.

## Reporting a vulnerability

**Do not open a public issue, discussion or pull request for a security problem.**

Report it privately through GitHub: go to the **Security** tab of this repository and choose **Report a vulnerability**. Include:

- what is affected (file, function, configuration),
- how to reproduce it,
- what an attacker could do with it.

You should get an answer within 7 days. Please give us a reasonable time to fix the problem before you disclose it.

## Supported versions

Only the latest commit on `main` is supported.

## Scope

In scope, for example:

- a way for an order to reach the broker without passing the risk manager (`src/stride/risk/guard.py`);
- a way to run with `mode: live` without `LIVE_CONFIRM`, or to bypass the kill switch;
- a key, token or account identifier that appears in a log, an error message, a report or the repository;
- injection through data downloaded from a price source or written to the SQLite database;
- a GitHub Actions workflow that exposes secrets or can be abused from a fork.

Out of scope: losses from the trading strategy itself, and problems in third-party services (brokers, data providers) that STRIDE only calls.

## If you find a leaked credential

If you see anything that looks like a real API key or token in this repository or its history, report it privately as above. Do not use it.

## How the project protects credentials

- Keys live only in a local `.env` file, which is git-ignored. `.env.example` holds names, never values.
- Keys are read as `SecretStr` and are never printed, logged or placed in URLs that are logged.
- Only data-only and paper-trading keys are used during development. Alpaca keys that are not paper keys are refused.
- Live trading requires `mode: live` **and** `LIVE_CONFIRM=I_UNDERSTAND_REAL_MONEY`.
- CI runs with a read-only token, scans every push for secrets and never receives broker credentials.
