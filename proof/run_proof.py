#!/usr/bin/env python3
"""
ImpactProof Phase 4A — Proof CLI runner.

Usage:
    python3 proof/run_proof.py <repo_path> <scenario_name>

Example:
    python3 proof/run_proof.py path/to/bundled-demo premium_checkout_refund

Prints the proof result as formatted JSON to stdout.
Exits 0 on PASS, 1 on REGRESSION or ERROR.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent))   # so proof/engine.py is importable

from proof.engine import run_named_scenario  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(
        description="ImpactProof proof runner — execute a scenario against a repository"
    )
    parser.add_argument("repo_path", help="Path to the Git repository to run against")
    parser.add_argument("scenario", help="Scenario name (e.g. premium_checkout_refund)")
    args = parser.parse_args()

    result = run_named_scenario(args.repo_path, args.scenario)
    print(json.dumps(result, indent=2))

    if result["status"] == "PASS":
        sys.exit(0)
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
