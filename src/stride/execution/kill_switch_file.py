"""The `KILL_SWITCH` file (SPEC section 7, step 4). Part of the kill switch.

HUMAN-REVIEWED MODULE: read the whole file before merging.

- `fire` writes the file (and an `events` row) when the decision says so. The
  caller then cancels pending orders, sends the liquidation sells for the next
  open (`risk.guard.check_liquidation`) and alerts.
- While the file exists, `is_active` is True and the system does not trade.
- No code path deletes the file. A human deletes it by hand, then runs

      python -m stride.execution.kill_switch_file reset --by "Name" --reason "..."

  which refuses while the file still exists, resets the drawdown peak and records
  who did it in `events` (SPEC 1.3).
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections.abc import Sequence
from pathlib import Path

import pandas as pd

from stride.data.store import connect, record_event, reset_twr_peak
from stride.risk.kill_switch import KillDecision

KILL_SWITCH_FILE = "KILL_SWITCH"


class KillSwitchError(Exception):
    """The kill switch is in a state that does not allow the requested action."""


def is_active(path: str | Path = KILL_SWITCH_FILE) -> bool:
    return Path(path).exists()


def fire(
    conn: sqlite3.Connection, decision: KillDecision, path: str | Path = KILL_SWITCH_FILE
) -> bool:
    """Write the file and an event if `decision.fire`. Returns True if it fired now.

    If the file already exists it is left untouched (the first reason is kept).
    """
    if not decision.fire:
        return False
    target = Path(path)
    stamp = pd.Timestamp.now(tz="UTC").isoformat()
    detail = f"{', '.join(decision.reasons)}; drawdown {decision.drawdown:.2%}"
    if target.exists():
        record_event(conn, "KILL_SWITCH_STILL_ACTIVE", detail, stamp)
        return False
    target.write_text(
        f"STRIDE kill switch fired at {stamp}\n{detail}\n"
        "Delete this file by hand only after reviewing the account, then run the reset.\n",
        encoding="utf-8",
    )
    record_event(conn, "KILL_SWITCH", detail, stamp)
    return True


def manual_reset(
    conn: sqlite3.Connection, by: str, reason: str, path: str | Path = KILL_SWITCH_FILE
) -> None:
    """Record the human reset after the file was deleted by hand."""
    if is_active(path):
        raise KillSwitchError("delete the KILL_SWITCH file by hand first")
    reset_twr_peak(conn, by, reason)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m stride.execution.kill_switch_file")
    parser.add_argument("command", choices=["status", "reset"])
    parser.add_argument("--by", default="", help="who resets (required for reset)")
    parser.add_argument("--reason", default="", help="why it is safe to trade again")
    parser.add_argument("--db", default="data/stride.db")
    parser.add_argument("--file", default=KILL_SWITCH_FILE)
    args = parser.parse_args(argv)
    if args.command == "status":
        sys.stdout.write("ACTIVE\n" if is_active(args.file) else "inactive\n")
        return 0
    if not args.by.strip() or not args.reason.strip():
        sys.stdout.write("reset needs --by and --reason\n")
        return 2
    conn = connect(args.db)
    try:
        manual_reset(conn, args.by, args.reason, args.file)
    except KillSwitchError as exc:
        sys.stdout.write(f"error: {exc}\n")
        return 1
    finally:
        conn.close()
    sys.stdout.write("reset recorded\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
