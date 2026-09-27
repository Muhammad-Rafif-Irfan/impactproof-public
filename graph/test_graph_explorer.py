#!/usr/bin/env python3
"""
Tests for the Node Explorer grouping logic.

These tests verify the buildGroups() algorithm in Python, which is the
exact same logic as the JavaScript buildGroups() in viewer_template.html.
All inputs are static — no browser, no DOM, no subprocess.

Run from the project root:
    python3 graph/test_graph_explorer.py

Covers:
  - correct grouping by impact_status (CHANGED / DIRECT / INDIRECT)
  - correct grouping by proof_status (REGRESSION)
  - regression nodes appear in Regression group
  - regression nodes remain in their impact-status group
  - zero-regression case produces empty regression group
  - deterministic sorting by node id within each group
  - group order is [regression, changed, direct, indirect]
  - counts match the actual nodes
  - a node with proof_status=REGRESSION and status=INDIRECT
    appears in BOTH regression group AND indirect group
"""

from __future__ import annotations

import sys
import os

# ---------------------------------------------------------------------------
# Python mirror of the JS buildGroups() function
# ---------------------------------------------------------------------------

def build_groups(nodes: list[dict]) -> list[dict]:
    """
    Mirror of the JavaScript buildGroups() in viewer_template.html.
    Returns the four groups in fixed order: regression, changed, direct, indirect.
    Each group is a dict with keys: key, label, nodes (sorted by node id).
    """
    by_id = lambda n: n["id"]

    regression = sorted([n for n in nodes if n.get("proof_status") == "REGRESSION"], key=by_id)
    changed    = sorted([n for n in nodes if n.get("status") == "CHANGED"],           key=by_id)
    direct     = sorted([n for n in nodes if n.get("status") == "DIRECT"],            key=by_id)
    indirect   = sorted([n for n in nodes if n.get("status") == "INDIRECT"],          key=by_id)

    return [
        {"key": "regression", "label": "Regression",         "nodes": regression},
        {"key": "changed",    "label": "Changed",             "nodes": changed},
        {"key": "direct",     "label": "Directly Affected",   "nodes": direct},
        {"key": "indirect",   "label": "Indirectly Affected", "nodes": indirect},
    ]


# ---------------------------------------------------------------------------
# Shared fixture — mirrors impactproof_demo after proof merge
# ---------------------------------------------------------------------------

def _make_node(nid: str, status: str, proof_status=None, file=None, symbol=None):
    parts = nid.split("::")
    f = file or parts[0]
    s = symbol or (parts[1] if len(parts) > 1 else None)
    n = {"id": nid, "file": f, "symbol": s, "status": status,
         "proof_status": proof_status, "proof_evidence": None}
    return n


NODES = [
    _make_node("discount.py::calculate_total", "CHANGED"),
    _make_node("checkout.py",                  "DIRECT"),
    _make_node("checkout.py::checkout",        "DIRECT"),
    _make_node("demo.py",                      "INDIRECT"),
    _make_node("demo.py::main",                "INDIRECT"),
    _make_node("payment.py::charge",           "INDIRECT"),
    _make_node("refund.py::refund",            "INDIRECT", proof_status="REGRESSION"),
    _make_node("tests/test_checkout.py",       "INDIRECT"),
    _make_node("tests/test_checkout.py::test_premium_checkout", "INDIRECT"),
    _make_node("tests/test_checkout.py::test_regular_checkout", "INDIRECT"),
    _make_node("tests/test_invoice.py",        "INDIRECT"),
    _make_node("tests/test_invoice.py::test_invoice_contains_amounts", "INDIRECT"),
    _make_node("tests/test_payment.py",        "INDIRECT"),
    _make_node("tests/test_payment.py::test_payment_receives_final_amount", "INDIRECT"),
    _make_node("tests/test_refund.py",         "INDIRECT"),
    _make_node("tests/test_refund.py::test_regular_refund", "INDIRECT"),
]

NODES_NO_REGRESSION = [n for n in NODES if n.get("proof_status") != "REGRESSION"]


def _groups(nodes=None):
    return build_groups(nodes if nodes is not None else NODES)


def _group(key: str, nodes=None) -> dict:
    return next(g for g in _groups(nodes) if g["key"] == key)


# ---------------------------------------------------------------------------
# Tests — group order
# ---------------------------------------------------------------------------

def test_group_order() -> None:
    """Groups must always appear in the fixed order: regression, changed, direct, indirect."""
    keys = [g["key"] for g in _groups()]
    assert keys == ["regression", "changed", "direct", "indirect"], \
        f"Unexpected group order: {keys}"
    print("✅ test_group_order passed.")


# ---------------------------------------------------------------------------
# Tests — correct grouping by impact_status
# ---------------------------------------------------------------------------

def test_changed_group_contains_only_changed_nodes() -> None:
    g = _group("changed")
    assert all(n["status"] == "CHANGED" for n in g["nodes"]), \
        f"Non-CHANGED node in changed group: {g['nodes']}"
    print("✅ test_changed_group_contains_only_changed_nodes passed.")


def test_direct_group_contains_only_direct_nodes() -> None:
    g = _group("direct")
    assert all(n["status"] == "DIRECT" for n in g["nodes"]), \
        f"Non-DIRECT node in direct group: {g['nodes']}"
    print("✅ test_direct_group_contains_only_direct_nodes passed.")


def test_indirect_group_contains_only_indirect_nodes() -> None:
    g = _group("indirect")
    assert all(n["status"] == "INDIRECT" for n in g["nodes"]), \
        f"Non-INDIRECT node in indirect group: {g['nodes']}"
    print("✅ test_indirect_group_contains_only_indirect_nodes passed.")


def test_changed_count_correct() -> None:
    g = _group("changed")
    expected = sum(1 for n in NODES if n["status"] == "CHANGED")
    assert len(g["nodes"]) == expected, \
        f"Changed count: expected {expected}, got {len(g['nodes'])}"
    print("✅ test_changed_count_correct passed.")


def test_direct_count_correct() -> None:
    g = _group("direct")
    expected = sum(1 for n in NODES if n["status"] == "DIRECT")
    assert len(g["nodes"]) == expected, \
        f"Direct count: expected {expected}, got {len(g['nodes'])}"
    print("✅ test_direct_count_correct passed.")


def test_indirect_count_correct() -> None:
    g = _group("indirect")
    expected = sum(1 for n in NODES if n["status"] == "INDIRECT")
    assert len(g["nodes"]) == expected, \
        f"Indirect count: expected {expected}, got {len(g['nodes'])}"
    print("✅ test_indirect_count_correct passed.")


# ---------------------------------------------------------------------------
# Tests — correct grouping by proof_status (REGRESSION)
# ---------------------------------------------------------------------------

def test_regression_group_contains_refund_node() -> None:
    """The known regression node (refund.py::refund) must appear in the regression group."""
    g = _group("regression")
    ids = [n["id"] for n in g["nodes"]]
    assert "refund.py::refund" in ids, \
        f"refund.py::refund missing from regression group. Got: {ids}"
    print("✅ test_regression_group_contains_refund_node passed.")


def test_regression_group_contains_only_regression_nodes() -> None:
    """Every node in the regression group must have proof_status=REGRESSION."""
    g = _group("regression")
    for n in g["nodes"]:
        assert n["proof_status"] == "REGRESSION", \
            f"Non-REGRESSION node in regression group: {n['id']}"
    print("✅ test_regression_group_contains_only_regression_nodes passed.")


def test_regression_node_also_in_impact_group() -> None:
    """
    refund.py::refund has status=INDIRECT AND proof_status=REGRESSION.
    It must appear in BOTH the regression group AND the indirect group.
    """
    reg_ids      = [n["id"] for n in _group("regression")["nodes"]]
    indirect_ids = [n["id"] for n in _group("indirect")["nodes"]]
    assert "refund.py::refund" in reg_ids,      "Not in regression group"
    assert "refund.py::refund" in indirect_ids, "Not in indirect group — must appear in both"
    print("✅ test_regression_node_also_in_impact_group passed.")


def test_regression_count_correct() -> None:
    g = _group("regression")
    expected = sum(1 for n in NODES if n.get("proof_status") == "REGRESSION")
    assert len(g["nodes"]) == expected, \
        f"Regression count: expected {expected}, got {len(g['nodes'])}"
    print("✅ test_regression_count_correct passed.")


# ---------------------------------------------------------------------------
# Tests — zero-regression case
# ---------------------------------------------------------------------------

def test_zero_regression_group_is_empty() -> None:
    """When no nodes have proof_status=REGRESSION, the regression group is empty."""
    g = _group("regression", NODES_NO_REGRESSION)
    assert g["nodes"] == [], \
        f"Expected empty regression group, got: {[n['id'] for n in g['nodes']]}"
    print("✅ test_zero_regression_group_is_empty passed.")


def test_zero_regression_other_groups_unaffected() -> None:
    """Removing the regression node must not change CHANGED / DIRECT counts."""
    changed_with    = len(_group("changed")["nodes"])
    changed_without = len(_group("changed", NODES_NO_REGRESSION)["nodes"])
    assert changed_with == changed_without, "CHANGED count should not change"

    direct_with    = len(_group("direct")["nodes"])
    direct_without = len(_group("direct", NODES_NO_REGRESSION)["nodes"])
    assert direct_with == direct_without, "DIRECT count should not change"
    print("✅ test_zero_regression_other_groups_unaffected passed.")


# ---------------------------------------------------------------------------
# Tests — deterministic sorting
# ---------------------------------------------------------------------------

def test_regression_group_sorted_by_id() -> None:
    nodes_unsorted = [
        _make_node("z_refund.py::refund",  "INDIRECT", proof_status="REGRESSION"),
        _make_node("a_refund.py::refund",  "INDIRECT", proof_status="REGRESSION"),
        _make_node("m_refund.py::refund",  "INDIRECT", proof_status="REGRESSION"),
    ]
    g = _group("regression", nodes_unsorted)
    ids = [n["id"] for n in g["nodes"]]
    assert ids == sorted(ids), f"Regression group not sorted: {ids}"
    print("✅ test_regression_group_sorted_by_id passed.")


def test_indirect_group_sorted_by_id() -> None:
    g = _group("indirect")
    ids = [n["id"] for n in g["nodes"]]
    assert ids == sorted(ids), f"Indirect group not sorted: {ids}"
    print("✅ test_indirect_group_sorted_by_id passed.")


def test_changed_group_sorted_by_id() -> None:
    nodes = [
        _make_node("z_mod.py::func", "CHANGED"),
        _make_node("a_mod.py::func", "CHANGED"),
        _make_node("m_mod.py::func", "CHANGED"),
    ]
    g = _group("changed", nodes)
    ids = [n["id"] for n in g["nodes"]]
    assert ids == sorted(ids), f"Changed group not sorted: {ids}"
    print("✅ test_changed_group_sorted_by_id passed.")


# ---------------------------------------------------------------------------
# Tests — multiple regressions
# ---------------------------------------------------------------------------

def test_multiple_regressions_all_in_group() -> None:
    nodes = [
        _make_node("a.py::f1", "INDIRECT", proof_status="REGRESSION"),
        _make_node("b.py::f2", "INDIRECT", proof_status="REGRESSION"),
        _make_node("c.py::f3", "DIRECT"),
    ]
    g = _group("regression", nodes)
    assert len(g["nodes"]) == 2
    ids = [n["id"] for n in g["nodes"]]
    assert "a.py::f1" in ids and "b.py::f2" in ids
    print("✅ test_multiple_regressions_all_in_group passed.")


# ---------------------------------------------------------------------------
# Integration: build_groups against real graph.py output
# ---------------------------------------------------------------------------

def test_build_groups_against_real_graph() -> None:
    """
    Run build_groups on the actual graph output from graph.py (using SAMPLE_ANALYSIS
    from test_graph_proof.py fixture data) and verify the regression node appears.
    """
    sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
    from graph import build_graph, merge_proof_results

    analysis = {
        "repo_path": "/repo",
        "changed_files": [{"file": "discount.py", "symbols": ["calculate_total"]}],
        "directly_affected": ["checkout.py"],
        "indirectly_affected": ["refund.py"],
        "relationships": [],
        "symbol_relationships": [
            {"from": {"file": "checkout.py", "symbol": "checkout"},
             "to":   {"file": "discount.py", "symbol": "calculate_total"},
             "type": "calls_or_uses", "reason": "..."},
            {"from": {"file": "tests/test_refund.py", "symbol": "test_r"},
             "to":   {"file": "refund.py",  "symbol": "refund"},
             "type": "calls_or_uses", "reason": "..."},
        ],
        "summary": {"changed_files": 1, "affected_files": 2, "symbol_relationships": 2},
    }
    scenario_spec = {
        "scenario": "premium_checkout_refund",
        "steps": [
            {"id": "checkout_step", "module": "checkout", "function": "checkout",
             "args": [100], "kwargs": {"is_premium": True}, "capture": "transaction"},
            {"id": "refund_step",   "module": "refund",   "function": "refund",
             "args_from_capture": ["transaction"], "capture": "refund_amount"},
        ],
        "expected": {"final_amount": 90.0, "refund_amount": 90.0},
        "extract": {
            "final_amount":  {"from_capture": "transaction",  "key": "final_amount"},
            "refund_amount": {"from_capture": "refund_amount", "direct": True},
        },
    }
    proof = {
        "scenario": "premium_checkout_refund",
        "status": "REGRESSION",
        "expected": {"final_amount": 90.0, "refund_amount": 90.0},
        "actual":   {"final_amount": 90.0, "refund_amount": 100},
        "mismatches": [{"key": "refund_amount", "expected": 90.0, "actual": 100}],
        "error": None,
    }

    g = build_graph(analysis)
    merge_proof_results(g, [proof], {"premium_checkout_refund": scenario_spec})

    groups = build_groups(g["nodes"])
    reg_group = next(gr for gr in groups if gr["key"] == "regression")
    reg_ids = [n["id"] for n in reg_group["nodes"]]
    assert "refund.py::refund" in reg_ids, \
        f"refund.py::refund not in regression group from real graph: {reg_ids}"
    print("✅ test_build_groups_against_real_graph passed.")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    test_group_order()
    test_changed_group_contains_only_changed_nodes()
    test_direct_group_contains_only_direct_nodes()
    test_indirect_group_contains_only_indirect_nodes()
    test_changed_count_correct()
    test_direct_count_correct()
    test_indirect_count_correct()
    test_regression_group_contains_refund_node()
    test_regression_group_contains_only_regression_nodes()
    test_regression_node_also_in_impact_group()
    test_regression_count_correct()
    test_zero_regression_group_is_empty()
    test_zero_regression_other_groups_unaffected()
    test_regression_group_sorted_by_id()
    test_indirect_group_sorted_by_id()
    test_changed_group_sorted_by_id()
    test_multiple_regressions_all_in_group()
    test_build_groups_against_real_graph()
    print("\n🎉 All Node Explorer grouping tests passed.")
