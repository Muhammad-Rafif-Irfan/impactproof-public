#!/usr/bin/env python3
"""
ImpactProof Phase 4A — Proof engine.

Executes a scenario against a target repository by generating a
minimal Python driver script and running it in a subprocess with
the repo on sys.path. Compares actual vs expected values
deterministically and returns structured JSON.

No LLM inference. No hard-coded actual values.
The repo source files are never modified.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import traceback
from pathlib import Path

HERE = Path(__file__).parent
SCENARIOS_DIR = HERE / "scenarios"


# ---------------------------------------------------------------------------
# Driver script generation
# ---------------------------------------------------------------------------

def _build_driver_script(scenario: dict) -> str:
    """
    Generate a self-contained Python script that:
      1. Executes the scenario steps using the repo's own modules.
      2. Extracts the values defined in scenario["extract"].
      3. Prints a single JSON object to stdout.

    The script is run as a subprocess inside the repo directory,
    so `import checkout` etc. resolve against the repo's files.
    """
    lines: list[str] = [
        "import json, sys",
        "",
        "captures = {}",
        "",
    ]

    for step in scenario["steps"]:
        mod = step["module"]
        fn = step["function"]
        capture = step["capture"]

        lines.append(f"from {mod} import {fn} as _fn_{step['id']}")
        lines.append("")

        # Build args
        if "args_from_capture" in step:
            # Args come from previously captured values
            cap_refs = step["args_from_capture"]
            args_expr = ", ".join(f"captures[{repr(c)}]" for c in cap_refs)
        else:
            # Use repr() so Python booleans/None render as True/False/None
            args_list = step.get("args", [])
            kwargs_dict = step.get("kwargs", {})
            args_parts = [repr(a) for a in args_list]
            kwargs_parts = [f"{k}={repr(v)}" for k, v in kwargs_dict.items()]
            args_expr = ", ".join(args_parts + kwargs_parts)

        lines.append(f"_result_{step['id']} = _fn_{step['id']}({args_expr})")
        lines.append(f"captures[{repr(capture)}] = _result_{step['id']}")
        lines.append("")

    # Extract values
    if any(not spec.get("direct") for spec in scenario["extract"].values()):
        lines.extend([
            "def _extract_dictionary_path(value, path):",
            "    for part in path:",
            "        if not isinstance(value, dict):",
            "            raise TypeError('Extract path traverses a non-dictionary value')",
            "        value = value[part]",
            "    return value",
            "",
        ])
    lines.append("output = {}")
    for key, spec in scenario["extract"].items():
        if spec.get("direct"):
            cap = spec["from_capture"] if "from_capture" in spec else spec.get("capture", key)
            lines.append(f"output[{repr(key)}] = captures[{repr(spec.get('from_capture', key))}]")
        else:
            cap = spec["from_capture"]
            dict_key = spec["key"]
            if not isinstance(dict_key, str) or not dict_key or any(not part for part in dict_key.split(".")):
                raise ValueError("Extract key must be a non-empty dot-separated dictionary path.")
            key_path = dict_key.split(".")
            path_literal = repr(key_path)
            lines.append(
                f"output[{repr(key)}] = _extract_dictionary_path(captures[{repr(cap)}], {path_literal})"
            )

    lines.append("")
    lines.append("print(json.dumps(output))")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------

def _compare(expected: dict, actual: dict) -> tuple[str, list[dict]]:
    """
    Compare expected vs actual values.
    Returns (status, mismatches).
    status is "PASS" or "REGRESSION".
    mismatches is a list of {key, expected, actual} dicts for differing values.
    """
    mismatches: list[dict] = []
    for key, exp_val in expected.items():
        act_val = actual.get(key)
        # Numeric comparison with a tiny tolerance for float representation
        if isinstance(exp_val, (int, float)) and isinstance(act_val, (int, float)):
            if abs(float(exp_val) - float(act_val)) > 1e-9:
                mismatches.append({"key": key, "expected": exp_val, "actual": act_val})
        else:
            if exp_val != act_val:
                mismatches.append({"key": key, "expected": exp_val, "actual": act_val})

    status = "PASS" if not mismatches else "REGRESSION"
    return status, mismatches


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_scenario(repo_path: str, scenario: dict) -> dict:
    """
    Execute a scenario against repo_path and return a proof result dict:

    {
      "scenario": str,
      "input": dict,
      "expected": dict,
      "actual": dict | None,
      "status": "PASS" | "REGRESSION" | "ERROR",
      "mismatches": [...],   # present on REGRESSION
      "error": str | None,   # present on ERROR
    }
    """
    repo_path = os.path.abspath(repo_path)
    scenario_name = scenario.get("scenario", "unknown")
    expected = scenario.get("expected", {})

    # Build human-readable input summary from the first step
    first_step = scenario["steps"][0] if scenario["steps"] else {}
    input_summary = {
        **({"args": first_step.get("args", [])} if first_step.get("args") else {}),
        **(first_step.get("kwargs", {})),
    }

    base_result = {
        "scenario": scenario_name,
        "description": scenario.get("description", ""),
        "input": input_summary,
        "expected": expected,
        "actual": None,
        "status": "ERROR",
        "mismatches": [],
        "error": None,
    }

    # Generate driver script
    try:
        driver = _build_driver_script(scenario)
    except Exception as exc:
        base_result["error"] = f"Driver generation failed: {exc}"
        return base_result

    # Run driver in a subprocess inside the repo directory
    try:
        result = subprocess.run(
            [sys.executable, "-c", driver],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        base_result["error"] = "Execution timed out after 30 seconds."
        return base_result
    except Exception as exc:
        base_result["error"] = f"Subprocess error: {exc}"
        return base_result

    if result.returncode != 0:
        stderr = result.stderr.strip()
        base_result["error"] = f"Execution failed (exit {result.returncode}):\n{stderr}"
        return base_result

    # Parse actual output
    stdout = result.stdout.strip()
    try:
        actual = json.loads(stdout)
    except json.JSONDecodeError as exc:
        base_result["error"] = f"Driver produced non-JSON output: {exc}\n{stdout}"
        return base_result

    # Compare
    status, mismatches = _compare(expected, actual)

    base_result.update({
        "actual": actual,
        "status": status,
        "mismatches": mismatches,
        "error": None,
    })
    return base_result


def load_scenario(name: str, scenarios_dir: Path | None = None) -> dict:
    """Load a scenario by name from the scenarios directory."""
    d = scenarios_dir or SCENARIOS_DIR
    path = d / f"{name}.json"
    if not path.exists():
        raise FileNotFoundError(f"Scenario not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def run_named_scenario(repo_path: str, scenario_name: str) -> dict:
    """Convenience: load a scenario by name and run it."""
    scenario = load_scenario(scenario_name)
    return run_scenario(repo_path, scenario)
