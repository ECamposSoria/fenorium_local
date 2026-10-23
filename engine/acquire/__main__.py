"""Run one acquisition job, or report on all of them.

    python -m engine.acquire run current-prices --state /state/acquire.db \
        --publish-dir /publish --input universe=/curve/universe.json
    python -m engine.acquire status --state /state/acquire.db

After every run the status of every job is written beside the artifacts as
`acquisition-status.json`, which the platform serves. The exit status is 1 only when the run
failed; a run skipped because another holds the lease exits 0.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
import math
import os
from pathlib import Path
import sys

from .jobs import JOBS
from .runner import RUN_BUDGET_SECONDS, STATUS_FILE, Context, run, status_document, write_atomic
from .state import StateStore


def _inputs(pairs: list[str]) -> dict[str, Path]:
    inputs = {}
    for pair in pairs:
        name, _, path = pair.partition("=")
        if not name or not path:
            raise SystemExit(f"--input expects NAME=PATH, got {pair!r}")
        inputs[name] = Path(path)
    return inputs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m engine.acquire")
    commands = parser.add_subparsers(dest="command", required=True)
    run_parser = commands.add_parser("run")
    run_parser.add_argument("job", choices=sorted(JOBS))
    run_parser.add_argument("--state", type=Path, required=True)
    run_parser.add_argument("--publish-dir", type=Path, required=True)
    run_parser.add_argument("--input", action="append", default=[], metavar="NAME=PATH")
    status_parser = commands.add_parser("status")
    status_parser.add_argument("--state", type=Path, required=True)
    args = parser.parse_args(argv)

    store = StateStore(args.state)
    try:
        if args.command == "status":
            print(json.dumps(status_document(store, now=datetime.now(UTC)), indent=2, sort_keys=True))
            return 0
        job = JOBS[args.job]()
        raw_budget = os.environ.get("FENORIUM_ACQUIRE_BUDGET_SECONDS")
        budget_seconds = RUN_BUDGET_SECONDS
        if raw_budget is not None:
            try:
                budget_seconds = float(raw_budget)
            except ValueError:
                parser.error("FENORIUM_ACQUIRE_BUDGET_SECONDS must be a positive number")
            if not math.isfinite(budget_seconds) or budget_seconds <= 0:
                parser.error("FENORIUM_ACQUIRE_BUDGET_SECONDS must be a positive number")
        outcome = run(job, store, Context(publish_dir=args.publish_dir, inputs=_inputs(args.input),
                                          budget_seconds=budget_seconds))
        write_atomic(args.publish_dir / STATUS_FILE, status_document(store, now=datetime.now(UTC)))
    finally:
        store.close()
    print(f"[acquire] {outcome.job} {outcome.status} attempted={outcome.attempted} "
          f"succeeded={outcome.succeeded} failed={outcome.failed} reused={outcome.reused} "
          f"published={str(outcome.published).lower()}" + (f" error={outcome.error}" if outcome.error else ""),
          flush=True)
    return outcome.exit_code


if __name__ == "__main__":
    sys.exit(main())
