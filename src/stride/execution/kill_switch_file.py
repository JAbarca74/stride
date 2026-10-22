"""The `KILL_SWITCH` file (SPEC section 7, step 4). Part of the kill switch.

HUMAN-REVIEWED MODULE: read the whole file before merging.

- `fire` writes the file (and an `events` row) when the decision says so. The
  caller then cancels pending orders, sends the liquidation sells for the next
  open using the positions the broker reports (safe even if the system is out
  of sync), checked by `risk.guard.check_liquidation`, and alerts.
- While the file exists, `is_active` is True and the system does not trade.
- No code path deletes the file. A human deletes it by hand, then runs

      python -m stride.execution.kill_switch_file reset --by "Name" --reason "..."

  which refuses while the file still exists, resets the drawdown peak and records
  who did it in `events` (SPEC 1.3).

The halt (SPEC 1.4) uses the `HALT` file the same way: `halt` writes it,
`is_halted` reports it (the guard then approves no order but positions are
kept), and after a person fixes the reconciliation and deletes the file by
hand, `resolve-halt --by "Name" --reason "..."` records it. A halt does not
reset the drawdown peak.
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
HALT_FILE = "HALT"


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


def is_halted(path: str | Path = HALT_FILE) -> bool:
    return Path(path).exists()


def halt(conn: sqlite3.Connection, decision: KillDecision, path: str | Path = HALT_FILE) -> bool:
    """Write the HALT file and an event if `decision.halt`. Returns True if it halted now."""
    if not decision.halt:
        return False
    target = Path(path)
    stamp = pd.Timestamp.now(tz="UTC").isoformat()
    if target.exists():
        record_event(conn, "HALT_STILL_ACTIVE", "reconciliation breaks", stamp)
        return False
    target.write_text(
        f"STRIDE halted at {stamp}: reconciliation breaks on two consecutive days.\n"
        "No new orders; positions are kept; the -40% kill switch stays armed.\n"
        "Fix the reconciliation, delete this file by hand, then run resolve-halt.\n",
        encoding="utf-8",
    )
    record_event(conn, "HALT", "reconciliation breaks on two consecutive days", stamp)
    return True


def resolve_halt(
    conn: sqlite3.Connection, by: str, reason: str, path: str | Path = HALT_FILE
) -> None:
    """Record that a person resolved the halt after deleting the file by hand."""
    if is_halted(path):
        raise KillSwitchError("delete the HALT file by hand first")
    if not by.strip():
        raise ValueError("resolving a halt needs the name of the person doing it")
    record_event(conn, "HALT_RESOLVED", f"by {by}: {reason}")


def manual_reset(
    conn: sqlite3.Connection, by: str, reason: str, path: str | Path = KILL_SWITCH_FILE
) -> None:
    """Record the human reset after the file was deleted by hand."""
    if is_active(path):
        raise KillSwitchError("delete the KILL_SWITCH file by hand first")
    reset_twr_peak(conn, by, reason)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m stride.execution.kill_switch_file")
    parser.add_argument("command", choices=["status", "reset", "resolve-halt"])
    parser.add_argument("--by", default="", help="who resets (required for reset)")
    parser.add_argument("--reason", default="", help="why it is safe to trade again")
    parser.add_argument("--db", default="data/stride.db")
    parser.add_argument("--file", default=KILL_SWITCH_FILE)
    parser.add_argument("--halt-file", default=HALT_FILE)
    args = parser.parse_args(argv)
    if args.command == "status":
        kill = "ACTIVE" if is_active(args.file) else "inactive"
        halted = "HALTED" if is_halted(args.halt_file) else "not halted"
        sys.stdout.write(f"kill switch: {kill}; {halted}\n")
        return 0
    if not args.by.strip() or not args.reason.strip():
        sys.stdout.write(f"{args.command} needs --by and --reason\n")
        return 2
    conn = connect(args.db)
    try:
        if args.command == "reset":
            manual_reset(conn, args.by, args.reason, args.file)
        else:
            resolve_halt(conn, args.by, args.reason, args.halt_file)
    except KillSwitchError as exc:
        sys.stdout.write(f"error: {exc}\n")
        return 1
    finally:
        conn.close()
    sys.stdout.write(f"{args.command} recorded\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
