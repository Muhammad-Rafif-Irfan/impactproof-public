#!/usr/bin/env python3
"""
Tests for proof/engine.py — Phase 4A proof execution.

Run from the project root:
    python3 proof/test_proof.py

Test cases:
  - PASS scenario: correct implementation, all values match
  - REGRESSION scenario: buggy implementation, values differ
  - ERROR scenario: execution error (import fails / bad module)
  - ERROR scenario: repo_path does not exist
  - Floating-point comparison tolerance
  - mismatches list structure on REGRESSION
"""

from __future__ import annotations

import json
import os
import sys
import shutil
import subprocess
import tempfile
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent))

from proof.engine import run_scenario, _compare, _build_driver_script  # noqa: E402


# ---------------------------------------------------------------------------
# Tiny fixture repos
# ---------------------------------------------------------------------------

def _make_passing_repo(tmp: str) -> str:
    """
    Repo where calculate_total correctly applies the premium discount
    AND refund returns the correct (discounted) amount.
    => scenario should PASS.
    """
    repo = os.path.join(tmp, "passing")
    os.makedirs(repo)

    Path(repo, "discount.py").write_text(textwrap("""
        def calculate_total(price, is_premium=False):
            if is_premium:
                return price * 0.9
            return price
    """))
    Path(repo, "invoice.py").write_text(textwrap("""
        def create_invoice(original_amount, final_amount):
            return {"original_amount": original_amount, "final_amount": final_amount}
    """))
    Path(repo, "payment.py").write_text(textwrap("""
        from invoice import create_invoice
        def charge(final_amount, original_amount):
            invoice = create_invoice(original_amount=original_amount, final_amount=final_amount)
            return {"original_amount": original_amount, "final_amount": final_amount, "invoice": invoice}
    """))
    Path(repo, "checkout.py").write_text(textwrap("""
        from discount import calculate_total
        from payment import charge
        def checkout(price, is_premium=False):
            final_amount = calculate_total(price, is_premium)
            return charge(final_amount, original_amount=price)
    """))
    # Correct refund: uses final_amount
    Path(repo, "refund.py").write_text(textwrap("""
        def refund(transaction):
            return transaction["final_amount"]
    """))
    return repo


def _make_regression_repo(tmp: str) -> str:
    """
    Repo where discount works correctly but refund has the bug
    (returns original_amount instead of final_amount).
    => scenario should REGRESSION.
    """
    repo = os.path.join(tmp, "regression")
    os.makedirs(repo)

    Path(repo, "discount.py").write_text(textwrap("""
        def calculate_total(price, is_premium=False):
            if is_premium:
                return price * 0.9
            return price
    """))
    Path(repo, "invoice.py").write_text(textwrap("""
        def create_invoice(original_amount, final_amount):
            return {"original_amount": original_amount, "final_amount": final_amount}
    """))
    Path(repo, "payment.py").write_text(textwrap("""
        from invoice import create_invoice
        def charge(final_amount, original_amount):
            invoice = create_invoice(original_amount=original_amount, final_amount=final_amount)
            return {"original_amount": original_amount, "final_amount": final_amount, "invoice": invoice}
    """))
    Path(repo, "checkout.py").write_text(textwrap("""
        from discount import calculate_total
        from payment import charge
        def checkout(price, is_premium=False):
            final_amount = calculate_total(price, is_premium)
            return charge(final_amount, original_amount=price)
    """))
    # Bug: returns original_amount
    Path(repo, "refund.py").write_text(textwrap("""
        def refund(transaction):
            return transaction["original_amount"]
    """))
    return repo


def _make_broken_repo(tmp: str) -> str:
    """Repo with a syntax error — execution must return ERROR."""
    repo = os.path.join(tmp, "broken")
    os.makedirs(repo)
    Path(repo, "checkout.py").write_text("def checkout(price, is_premium=False:\n    pass\n")
    Path(repo, "refund.py").write_text("def refund(tx): return tx\n")
    return repo


def textwrap(s: str) -> str:
    """Strip leading newline + common indentation from a triple-quoted string."""
    import textwrap as tw
    return tw.dedent(s.lstrip("\n"))


# ---------------------------------------------------------------------------
# Shared scenario spec
# ---------------------------------------------------------------------------

PREMIUM_SCENARIO = {
    "scenario": "premium_checkout_refund",
    "description": "Test scenario",
    "steps": [
        {
            "id": "checkout_step",
            "module": "checkout",
            "function": "checkout",
            "args": [100],
            "kwargs": {"is_premium": True},
            "capture": "transaction",
        },
        {
            "id": "refund_step",
            "module": "refund",
            "function": "refund",
            "args_from_capture": ["transaction"],
            "capture": "refund_amount",
        },
    ],
    "expected": {
        "final_amount": 90.0,
        "refund_amount": 90.0,
    },
    "extract": {
        "final_amount": {"from_capture": "transaction", "key": "final_amount"},
        "refund_amount": {"from_capture": "refund_amount", "direct": True},
    },
}


# ---------------------------------------------------------------------------
# Tests — _compare (unit)
# ---------------------------------------------------------------------------

def test_compare_pass() -> None:
    status, mismatches = _compare({"a": 10, "b": 2.5}, {"a": 10, "b": 2.5})
    assert status == "PASS", f"Expected PASS, got {status}"
    assert mismatches == []
    print("✅ test_compare_pass passed.")


def test_compare_regression() -> None:
    status, mismatches = _compare({"a": 90.0}, {"a": 100})
    assert status == "REGRESSION"
    assert len(mismatches) == 1
    assert mismatches[0]["key"] == "a"
    assert mismatches[0]["expected"] == 90.0
    assert mismatches[0]["actual"] == 100
    print("✅ test_compare_regression passed.")


def test_compare_float_tolerance() -> None:
    """Values within 1e-9 should be considered equal."""
    status, _ = _compare({"x": 90.0}, {"x": 90.0 + 1e-12})
    assert status == "PASS", f"Expected PASS for near-equal floats, got {status}"
    print("✅ test_compare_float_tolerance passed.")


def test_compare_multiple_mismatches() -> None:
    status, mismatches = _compare({"a": 1, "b": 2}, {"a": 9, "b": 9})
    assert status == "REGRESSION"
    assert len(mismatches) == 2
    print("✅ test_compare_multiple_mismatches passed.")


# ---------------------------------------------------------------------------
# Tests — driver script generation (unit)
# ---------------------------------------------------------------------------

def test_driver_contains_module_imports() -> None:
    script = _build_driver_script(PREMIUM_SCENARIO)
    assert "from checkout import checkout" in script
    assert "from refund import refund" in script
    print("✅ test_driver_contains_module_imports passed.")


def test_driver_uses_python_bool() -> None:
    """is_premium=True must appear as Python True, not JSON true."""
    script = _build_driver_script(PREMIUM_SCENARIO)
    assert "is_premium=True" in script, f"Expected Python True in driver:\n{script}"
    print("✅ test_driver_uses_python_bool passed.")


def test_driver_has_json_output() -> None:
    script = _build_driver_script(PREMIUM_SCENARIO)
    assert "print(json.dumps(output))" in script
    print("✅ test_driver_has_json_output passed.")


def _scenario_with_extraction(output_key: str, key_path: str) -> dict:
    return {
        **PREMIUM_SCENARIO,
        "expected": {**PREMIUM_SCENARIO["expected"], output_key: 90.0},
        "extract": {
            **PREMIUM_SCENARIO["extract"],
            output_key: {"from_capture": "transaction", "key": key_path},
        },
    }


def test_single_level_extraction_still_works() -> None:
    tmp = tempfile.mkdtemp(prefix="impactproof_proof_")
    try:
        result = run_scenario(_make_passing_repo(tmp), PREMIUM_SCENARIO)
        assert result["status"] == "PASS", result
        assert result["actual"]["final_amount"] == 90.0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_nested_dictionary_extraction_works() -> None:
    tmp = tempfile.mkdtemp(prefix="impactproof_proof_")
    try:
        scenario = _scenario_with_extraction("invoice_final_amount", "invoice.final_amount")
        result = run_scenario(_make_passing_repo(tmp), scenario)
        assert result["status"] == "PASS", result
        assert result["actual"]["invoice_final_amount"] == 90.0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_missing_nested_dictionary_key_returns_error() -> None:
    tmp = tempfile.mkdtemp(prefix="impactproof_proof_")
    try:
        scenario = _scenario_with_extraction("missing_value", "invoice.missing.amount")
        result = run_scenario(_make_passing_repo(tmp), scenario)
        assert result["status"] == "ERROR", result
        assert result["actual"] is None
        assert result["error"]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# Tests — run_scenario integration
# ---------------------------------------------------------------------------

def test_passing_scenario() -> None:
    """Correct implementation → PASS."""
    tmp = tempfile.mkdtemp(prefix="impactproof_proof_")
    try:
        repo = _make_passing_repo(tmp)
        result = run_scenario(repo, PREMIUM_SCENARIO)
        print("\n=== test_passing_scenario ===")
        print(json.dumps(result, indent=2))
        assert result["status"] == "PASS", f"Expected PASS, got: {result['status']}"
        assert result["error"] is None
        assert result["mismatches"] == []
        assert result["actual"]["final_amount"] == 90.0
        assert result["actual"]["refund_amount"] == 90.0
        print("✅ test_passing_scenario passed.")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_regression_scenario() -> None:
    """Buggy refund → REGRESSION with correct mismatch on refund_amount."""
    tmp = tempfile.mkdtemp(prefix="impactproof_proof_")
    try:
        repo = _make_regression_repo(tmp)
        result = run_scenario(repo, PREMIUM_SCENARIO)
        print("\n=== test_regression_scenario ===")
        print(json.dumps(result, indent=2))
        assert result["status"] == "REGRESSION", f"Expected REGRESSION, got: {result['status']}"
        assert result["error"] is None
        assert any(m["key"] == "refund_amount" for m in result["mismatches"]), \
            f"Expected refund_amount in mismatches: {result['mismatches']}"
        # final_amount should match
        assert result["actual"]["final_amount"] == 90.0
        # refund_amount should be the bug value (100)
        assert result["actual"]["refund_amount"] == 100
        print("✅ test_regression_scenario passed.")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_error_scenario_broken_module() -> None:
    """Syntax error in repo → ERROR (not PASS or REGRESSION)."""
    tmp = tempfile.mkdtemp(prefix="impactproof_proof_")
    try:
        repo = _make_broken_repo(tmp)
        result = run_scenario(repo, PREMIUM_SCENARIO)
        print("\n=== test_error_scenario_broken_module ===")
        print(json.dumps(result, indent=2))
        assert result["status"] == "ERROR", f"Expected ERROR, got: {result['status']}"
        assert result["error"] is not None and len(result["error"]) > 0
        assert result["actual"] is None
        print("✅ test_error_scenario_broken_module passed.")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_error_scenario_bad_repo_path() -> None:
    """Non-existent repo → ERROR."""
    result = run_scenario("/tmp/impactproof_nonexistent_proof_dir_xyz", PREMIUM_SCENARIO)
    print("\n=== test_error_scenario_bad_repo_path ===")
    print(json.dumps(result, indent=2))
    assert result["status"] == "ERROR", f"Expected ERROR, got: {result['status']}"
    assert result["error"] is not None
    print("✅ test_error_scenario_bad_repo_path passed.")


def test_result_has_all_required_keys() -> None:
    """Result dict must always contain the required top-level keys."""
    tmp = tempfile.mkdtemp(prefix="impactproof_proof_")
    try:
        repo = _make_passing_repo(tmp)
        result = run_scenario(repo, PREMIUM_SCENARIO)
        for key in ("scenario", "description", "input", "expected", "actual", "status", "mismatches", "error"):
            assert key in result, f"Missing key: {key}"
        print("✅ test_result_has_all_required_keys passed.")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_expected_values_not_mutated() -> None:
    """run_scenario must not mutate the scenario dict's expected values."""
    tmp = tempfile.mkdtemp(prefix="impactproof_proof_")
    try:
        repo = _make_regression_repo(tmp)
        original_expected = dict(PREMIUM_SCENARIO["expected"])
        run_scenario(repo, PREMIUM_SCENARIO)
        assert PREMIUM_SCENARIO["expected"] == original_expected, \
            "run_scenario mutated scenario[expected]"
        print("✅ test_expected_values_not_mutated passed.")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Unit tests
    test_compare_pass()
    test_compare_regression()
    test_compare_float_tolerance()
    test_compare_multiple_mismatches()
    test_driver_contains_module_imports()
    test_driver_uses_python_bool()
    test_driver_has_json_output()
    test_single_level_extraction_still_works()
    test_nested_dictionary_extraction_works()
    test_missing_nested_dictionary_key_returns_error()
    # Integration tests
    test_passing_scenario()
    test_regression_scenario()
    test_error_scenario_broken_module()
    test_error_scenario_bad_repo_path()
    test_result_has_all_required_keys()
    test_expected_values_not_mutated()
    print("\n🎉 All proof engine tests passed.")
