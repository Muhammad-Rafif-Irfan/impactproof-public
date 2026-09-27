#!/usr/bin/env python3
"""
Tests for Phase 4B — proof result merging into graph data.

Run from the project root:
    python3 graph/test_graph_proof.py

Covers:
  - REGRESSION proof marks correct node with proof_status=REGRESSION
  - PASS proof sets proof_status=PASS (no impact status change)
  - ERROR proof sets proof_status=ERROR
  - PASS proof does NOT mark any node as REGRESSION
  - Proof does not create new nodes
  - Proof does not create new edges
  - summary.regressions count is correct
  - Node with no matching proof has proof_status=None
  - Only the node that owns the failing symbol is marked, not all nodes
"""

from __future__ import annotations

import json
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from graph import build_graph, merge_proof_results, _node_id


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

# Analysis output that mirrors impactproof_demo
SAMPLE_ANALYSIS = {
    "repo_path": "/repo",
    "changed_files": [
        {"file": "discount.py", "symbols": ["calculate_total"]}
    ],
    "directly_affected": ["checkout.py"],
    "indirectly_affected": ["demo.py", "refund.py", "tests/test_refund.py"],
    "relationships": [],
    "symbol_relationships": [
        {
            "from": {"file": "checkout.py", "symbol": "checkout"},
            "to":   {"file": "discount.py", "symbol": "calculate_total"},
            "type": "calls_or_uses",
            "reason": "checkout.py.checkout uses calculate_total from discount.py",
        },
        {
            "from": {"file": "tests/test_refund.py", "symbol": "test_regular_refund"},
            "to":   {"file": "refund.py",  "symbol": "refund"},
            "type": "calls_or_uses",
            "reason": "tests/test_refund.py.test_regular_refund uses refund from refund.py",
        },
    ],
    "summary": {
        "changed_files": 1,
        "affected_files": 4,
        "symbol_relationships": 2,
    },
}

# The scenario spec for premium_checkout_refund
SCENARIO_SPEC = {
    "scenario": "premium_checkout_refund",
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
    "expected": {"final_amount": 90.0, "refund_amount": 90.0},
    "extract": {
        "final_amount": {"from_capture": "transaction", "key": "final_amount"},
        "refund_amount": {"from_capture": "refund_amount", "direct": True},
    },
}

REGRESSION_PROOF = {
    "scenario": "premium_checkout_refund",
    "input": {"args": [100], "is_premium": True},
    "expected": {"final_amount": 90.0, "refund_amount": 90.0},
    "actual": {"final_amount": 90.0, "refund_amount": 100},
    "status": "REGRESSION",
    "mismatches": [{"key": "refund_amount", "expected": 90.0, "actual": 100}],
    "error": None,
}

PASS_PROOF = {
    "scenario": "premium_checkout_refund",
    "input": {"args": [100], "is_premium": True},
    "expected": {"final_amount": 90.0, "refund_amount": 90.0},
    "actual": {"final_amount": 90.0, "refund_amount": 90.0},
    "status": "PASS",
    "mismatches": [],
    "error": None,
}

ERROR_PROOF = {
    "scenario": "premium_checkout_refund",
    "input": {"args": [100], "is_premium": True},
    "expected": {"final_amount": 90.0, "refund_amount": 90.0},
    "actual": None,
    "status": "ERROR",
    "mismatches": [],
    "error": "SyntaxError in checkout.py",
}

SCENARIO_SPECS = {"premium_checkout_refund": SCENARIO_SPEC}


def _fresh_graph() -> dict:
    """Build a fresh graph from SAMPLE_ANALYSIS for each test."""
    return build_graph(SAMPLE_ANALYSIS)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_regression_marks_correct_node() -> None:
    """REGRESSION proof must mark refund.py::refund with proof_status=REGRESSION."""
    g = _fresh_graph()
    merge_proof_results(g, [REGRESSION_PROOF], SCENARIO_SPECS)

    nodes = {n["id"]: n for n in g["nodes"]}
    refund_node = nodes.get("refund.py::refund")
    assert refund_node is not None, "refund.py::refund node must exist"
    assert refund_node["proof_status"] == "REGRESSION", \
        f"Expected REGRESSION, got: {refund_node['proof_status']}"
    print("✅ test_regression_marks_correct_node passed.")


def test_regression_does_not_change_impact_status() -> None:
    """proof_status=REGRESSION must not change the node's impact status field."""
    g = _fresh_graph()
    merge_proof_results(g, [REGRESSION_PROOF], SCENARIO_SPECS)
    nodes = {n["id"]: n for n in g["nodes"]}
    refund_node = nodes["refund.py::refund"]
    # refund.py is INDIRECT in the impact graph — proof must not change that
    assert refund_node["status"] == "INDIRECT", \
        f"Impact status must remain INDIRECT, got: {refund_node['status']}"
    print("✅ test_regression_does_not_change_impact_status passed.")


def test_regression_attaches_evidence() -> None:
    """The regression node must carry the full proof_evidence dict."""
    g = _fresh_graph()
    merge_proof_results(g, [REGRESSION_PROOF], SCENARIO_SPECS)
    nodes = {n["id"]: n for n in g["nodes"]}
    refund_node = nodes["refund.py::refund"]
    ev = refund_node["proof_evidence"]
    assert ev is not None
    assert ev["status"] == "REGRESSION"
    assert ev["actual"]["refund_amount"] == 100
    assert ev["expected"]["refund_amount"] == 90.0
    assert len(ev["mismatches"]) == 1
    print("✅ test_regression_attaches_evidence passed.")


def test_pass_proof_does_not_mark_regression() -> None:
    """A PASS proof must not set proof_status=REGRESSION on any node."""
    g = _fresh_graph()
    merge_proof_results(g, [PASS_PROOF], SCENARIO_SPECS)
    for node in g["nodes"]:
        assert node["proof_status"] != "REGRESSION", \
            f"PASS proof must not mark any node REGRESSION: {node['id']}"
    print("✅ test_pass_proof_does_not_mark_regression passed.")


def test_pass_proof_marks_checkout_pass() -> None:
    """A PASS proof marks the checkout step's node as PASS."""
    g = _fresh_graph()
    merge_proof_results(g, [PASS_PROOF], SCENARIO_SPECS)
    nodes = {n["id"]: n for n in g["nodes"]}
    checkout_node = nodes.get("checkout.py::checkout")
    assert checkout_node is not None
    assert checkout_node["proof_status"] == "PASS", \
        f"Expected PASS, got: {checkout_node['proof_status']}"
    print("✅ test_pass_proof_marks_checkout_pass passed.")


def test_error_proof_sets_error_status() -> None:
    """An ERROR proof must set proof_status=ERROR on targeted nodes."""
    g = _fresh_graph()
    merge_proof_results(g, [ERROR_PROOF], SCENARIO_SPECS)
    nodes = {n["id"]: n for n in g["nodes"]}
    checkout_node = nodes.get("checkout.py::checkout")
    assert checkout_node is not None
    assert checkout_node["proof_status"] == "ERROR", \
        f"Expected ERROR, got: {checkout_node['proof_status']}"
    print("✅ test_error_proof_sets_error_status passed.")


def test_error_proof_does_not_mark_regression() -> None:
    """An ERROR proof must not mark any node as REGRESSION."""
    g = _fresh_graph()
    merge_proof_results(g, [ERROR_PROOF], SCENARIO_SPECS)
    for node in g["nodes"]:
        assert node["proof_status"] != "REGRESSION", \
            f"ERROR proof must not set REGRESSION: {node['id']}"
    print("✅ test_error_proof_does_not_mark_regression passed.")


def test_no_new_nodes_created() -> None:
    """merge_proof_results must not add new nodes."""
    g = _fresh_graph()
    node_count_before = len(g["nodes"])
    merge_proof_results(g, [REGRESSION_PROOF], SCENARIO_SPECS)
    assert len(g["nodes"]) == node_count_before, \
        f"Node count changed: {node_count_before} → {len(g['nodes'])}"
    print("✅ test_no_new_nodes_created passed.")


def test_no_new_edges_created() -> None:
    """merge_proof_results must not add new edges."""
    g = _fresh_graph()
    edge_count_before = len(g["edges"])
    merge_proof_results(g, [REGRESSION_PROOF], SCENARIO_SPECS)
    assert len(g["edges"]) == edge_count_before, \
        f"Edge count changed: {edge_count_before} → {len(g['edges'])}"
    print("✅ test_no_new_edges_created passed.")


def test_summary_regressions_count() -> None:
    """summary.regressions must equal the number of regression-marked nodes."""
    g = _fresh_graph()
    merge_proof_results(g, [REGRESSION_PROOF], SCENARIO_SPECS)
    regression_nodes = [n for n in g["nodes"] if n["proof_status"] == "REGRESSION"]
    assert g["summary"].get("regressions") == len(regression_nodes), \
        f"summary.regressions={g['summary'].get('regressions')} but {len(regression_nodes)} regression node(s)"
    print("✅ test_summary_regressions_count passed.")


def test_unproven_nodes_have_none_proof_status() -> None:
    """Nodes not targeted by any proof must have proof_status=None."""
    g = _fresh_graph()
    merge_proof_results(g, [REGRESSION_PROOF], SCENARIO_SPECS)
    nodes = {n["id"]: n for n in g["nodes"]}
    # discount.py::calculate_total is CHANGED but not the regression node
    changed_node = nodes.get("discount.py::calculate_total")
    assert changed_node is not None
    assert changed_node["proof_status"] is None, \
        f"Changed node should have proof_status=None: {changed_node['proof_status']}"
    print("✅ test_unproven_nodes_have_none_proof_status passed.")


def test_only_failing_symbol_node_is_regression() -> None:
    """
    Only the node for the function that produced the mismatch should be REGRESSION.
    Other nodes must not be.
    """
    g = _fresh_graph()
    merge_proof_results(g, [REGRESSION_PROOF], SCENARIO_SPECS)
    nodes = {n["id"]: n for n in g["nodes"]}

    # refund.py::refund — the step that produced the mismatch — must be REGRESSION
    assert nodes["refund.py::refund"]["proof_status"] == "REGRESSION"

    # checkout.py::checkout produced final_amount (which passed) — must NOT be REGRESSION
    checkout = nodes.get("checkout.py::checkout")
    if checkout:
        assert checkout["proof_status"] != "REGRESSION", \
            "checkout node must not be marked REGRESSION when only refund failed"

    print("✅ test_only_failing_symbol_node_is_regression passed.")


def test_no_proof_leaves_all_none() -> None:
    """When no proof results are provided, all proof_status fields must be None."""
    g = _fresh_graph()
    merge_proof_results(g, [], SCENARIO_SPECS)
    for node in g["nodes"]:
        assert node["proof_status"] is None, \
            f"Unexpected proof_status on {node['id']}: {node['proof_status']}"
    print("✅ test_no_proof_leaves_all_none passed.")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    test_regression_marks_correct_node()
    test_regression_does_not_change_impact_status()
    test_regression_attaches_evidence()
    test_pass_proof_does_not_mark_regression()
    test_pass_proof_marks_checkout_pass()
    test_error_proof_sets_error_status()
    test_error_proof_does_not_mark_regression()
    test_no_new_nodes_created()
    test_no_new_edges_created()
    test_summary_regressions_count()
    test_unproven_nodes_have_none_proof_status()
    test_only_failing_symbol_node_is_regression()
    test_no_proof_leaves_all_none()
    print("\n🎉 All Phase 4B graph proof tests passed.")
