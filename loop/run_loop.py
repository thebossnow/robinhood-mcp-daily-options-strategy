#!/usr/bin/env python3
"""Orchestrator for the autoresearch loop.

HONEST STATUS: the proposal step is NOT implemented. What works today:

    python loop/run_loop.py verify    # data-readiness + baseline metrics
                                      # (delegates to loop/evaluate.py)

The full loop (propose variant → pytest → evaluate.evaluate() → log to
state.json → open PR for human review) is specified in loop/program.md and
must not be wired to an LLM until the data gates in evaluate.py pass.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("verify", help="Run the verifier dry-run on current data")
    sub.add_parser("propose", help="NOT IMPLEMENTED — see loop/program.md")
    args = ap.parse_args()

    if args.cmd == "verify":
        from loop.evaluate import main as evaluate_main
        return evaluate_main()

    print(
        "The proposal step is intentionally not implemented. Implementing it "
        "before the data gates pass (>=30 scan days, >=40 settled trades) "
        "would only automate overfitting. See loop/program.md."
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
