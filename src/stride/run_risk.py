"""Entry point of the risk-allocation paper instance (docs/design/mc_risk_paper.md).

python -m stride.run_risk --config config.paper_risk.yaml [--report | --verify]
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from stride.risk_paper.config import RiskPaperConfigError
from stride.risk_paper.job import Deps, RiskJob


def main(argv: Sequence[str] | None = None, deps: Deps | None = None) -> int:
    """One tick of the job, or its status (`--report`), or its self-check (`--verify`)."""
    parser = argparse.ArgumentParser(prog="python -m stride.run_risk")
    parser.add_argument("--config", default="config.paper_risk.yaml")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--report", action="store_true", help="print the status and stop")
    group.add_argument(
        "--verify", action="store_true", help="replay the account and compare with the record"
    )
    args = parser.parse_args(argv)

    def out(line: str) -> None:
        """Write one line to the standard output."""
        sys.stdout.write(line + "\n")

    try:
        job = RiskJob(Path(args.config), deps)
    except RiskPaperConfigError as exc:
        out(f"STRIDE RISK: invalid config: {exc}")
        return 2
    if args.report:
        return job.report(out)
    if args.verify:
        return job.verify(out)
    return job.run(out)


if __name__ == "__main__":
    raise SystemExit(main())
