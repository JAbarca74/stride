# Contributing to STRIDE

Thanks for your interest. STRIDE is a personal research project that trades a small real account, so changes are reviewed carefully. Issues, questions and focused pull requests are welcome.

## Before you start

- Read [`docs/SPEC.md`](docs/SPEC.md). It is the single source of truth: if code and spec disagree, the spec wins.
- For anything larger than a small fix, open an issue first so we can agree on the approach.
- Security problems go through [`SECURITY.md`](SECURITY.md), never a public issue.

## Development setup

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate   |   macOS/Linux: source .venv/bin/activate
pip install -e ".[dev,alpaca]"
pytest
ruff check . && ruff format --check .
mypy src tests
```

The tests need no network and no keys. Do not add tests that call real APIs.

## Rules for every change

1. **Never commit a credential.** Keys go in your local `.env` only (git-ignored). Use obviously fake values in tests, such as `"test-key-not-real"`.
2. **Paper or backtest only.** Never run with `mode: live` and never set `LIVE_CONFIRM` while developing.
3. **No look-ahead.** No function may read data dated after the evaluation date.
4. **Pure core.** `src/stride/strategy/` and `src/stride/risk/` have no network, file I/O or clock.
5. **Parameters live in `config.yaml`.** Do not hardcode strategy or risk numbers, and do not change their values without discussion.
6. **Tests ship with the code.** Code that moves money gets property-based tests (Hypothesis) for its invariants.
7. **English only** in code, comments, commits and pull requests.

## Protected modules

These files decide how much money is at risk and are reviewed line by line by the maintainer. Pull requests that touch them need prior agreement in an issue:

- `src/stride/risk/leverage.py`
- `src/stride/risk/guard.py`
- `src/stride/risk/kill_switch.py` and `src/stride/execution/kill_switch_file.py`
- `src/stride/execution/` broker and order code
- `.github/workflows/`

## Pull requests

- One topic per pull request, on a branch from `main`.
- Fill in the pull request template: what changed, how it was tested, open questions.
- CI (tests, ruff, mypy, secret scan, CodeQL) must be green before review.
- New dependencies need a reason in the pull request description.

By contributing you agree that your contribution is licensed under the [MIT License](LICENSE) and that you follow the [Code of Conduct](CODE_OF_CONDUCT.md).
