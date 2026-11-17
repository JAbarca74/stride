"""Manual smoke test against a real IB Gateway PAPER account (design doc section 8).

Skipped unless `STRIDE_IBKR_PAPER_SMOKE=1` and the `IBKR_*` values are in `.env`.
It connects read-only, so it can never place an order: it checks the gates against
the owner's Gateway and reads the account and positions.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(
    os.environ.get("STRIDE_IBKR_PAPER_SMOKE") != "1",
    reason="needs IB Gateway paper running (set STRIDE_IBKR_PAPER_SMOKE=1)",
)


def test_read_only_paper_connection() -> None:  # pragma: no cover - manual only
    from stride.config import load_config  # noqa: PLC0415
    from stride.execution.ibkr_broker import IBKRBroker  # noqa: PLC0415
    from stride.settings import load_secrets  # noqa: PLC0415

    config = load_config(ROOT / "config.paper_ibkr.yaml")
    with IBKRBroker(config, load_secrets(ROOT / ".env"), lambda _: 1.0, readonly=True) as broker:
        account = broker.get_account()
        assert account.equity > 0
        broker.get_positions()
        assert broker.is_fractionable("SPY") in {True, False}
