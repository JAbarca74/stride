"""Entry point of the news agent paper account (docs/design/news_agent.md).

python -m stride.run_news --config config.paper_news.yaml [--report]
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from stride.config import ConfigError
from stride.news.job import Deps, NewsJob


def main(argv: Sequence[str] | None = None, deps: Deps | None = None) -> int:
    """One tick of the job, or its status (`--report`)."""
    parser = argparse.ArgumentParser(prog="python -m stride.run_news")
    parser.add_argument("--config", default="config.paper_news.yaml")
    parser.add_argument("--report", action="store_true", help="print the status and stop")
    args = parser.parse_args(argv)

    def out(line: str) -> None:
        sys.stdout.write(line + "\n")

    try:
        job = NewsJob(Path(args.config), deps)
    except ConfigError as exc:
        out(f"STRIDE NEWS: invalid config: {exc}")
        return 2
    return job.report(out) if args.report else job.run(out)


if __name__ == "__main__":
    raise SystemExit(main())
