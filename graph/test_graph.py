#!/usr/bin/env python3
"""
Tests for graph.py — the Phase 3 graph data transformer.

Run from the project root:
    python graph/test_graph.py

These tests use static fixture data (no git repo, no subprocess) so they
are fast and fully deterministic.
"""

from __future__ import annotations

import json
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
from graph import build_graph, _node_id, _short_label


# ---------------------------------------------------------------------------
# Shared fixture — mirrors the impactproof_demo blast radius
# ---------------------------------------------------------------------------

SAMPLE_ANALYSIS = {
    "repo_path": "/repo",
    "changed_files": [
        {"file": "discount.py", "symbols": ["calculate_total"]}
    ],
    "directly_affected": ["checkout.py"],
    "indirectly_affected": ["demo.py", "tests/test_checkout.py"],
    "relationships": [
        {
            "from": "discount.py",
            "to": "checkout.py",
            "type": "import",
            "reason": "checkout.py imports calculate_total from discount.py",
        },
        {
            "from": "checkout.py",
            "to": "demo.py",
            "type": "import",
            "reason": "demo.py imports checkout from checkout.py",
        },
    ],
    "symbol_relationships": [
        {
            "from": {"file": "checkout.py", "symbol": "checkout"},
            "to":   {"file": "discount.py", "symbol": "calculate_total"},
            "type": "calls_or_uses",
            "reason": "checkout.py.checkout uses calculate_total from discount.py",
        },
        {
            "from": {"file": "demo.py", "symbol": "main"},
            "to":   {"file": "checkout.py", "symbol": "checkout"},
            "type": "calls_or_uses",
            "reason": "demo.py.main uses checkout from checkout.py",
        },
    ],
    "summary": {
        "changed_files": 1,
        "affected_files": 3,
        "symbol_relationships": 2,
    },
}

EMPTY_ANALYSIS = {
    "repo_path": "/repo",
    "changed_files": [],
    "directly_affected": [],
    "indirectly_affected": [],
    "relationships": [],
    "symbol_relationships": [],
    "summary": {"changed_files": 0, "affected_files": 0, "symbol_relationships": 0},
}

ERROR_ANALYSIS = {
    "repo_path": "/nonexistent",
    "error": "repo_path does not exist: /nonexistent",
    "changed_files": [],
    "directly_affected": [],
    "indirectly_affected": [],
    "relationships": [],
    "symbol_relationships": [],
    "summary": {"changed_files": 0, "affected_files": 0},
}


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _nodes_by_id(graph: dict) -> dict:
    return {n["id"]: n for n in graph["nodes"]}


# ---------------------------------------------------------------------------
# Tests — node IDs and labels
# ---------------------------------------------------------------------------

def test_node_id_with_symbol() -> None:
    assert _node_id("discount.py", "calculate_total") == "discount.py::calculate_total"
    print("✅ test_node_id_with_symbol passed.")


def test_node_id_without_symbol() -> None:
    assert _node_id("discount.py") == "discount.py"
    assert _node_id("discount.py", None) == "discount.py"
    print("✅ test_node_id_without_symbol passed.")


def test_short_label_with_symbol() -> None:
    label = _short_label("discount.py", "calculate_total")
    assert "calculate_total" in label
    assert "discount.py" in label
    print("✅ test_short_label_with_symbol passed.")


def test_short_label_without_symbol() -> None:
    label = _short_label("tests/test_checkout.py")
    assert "test_checkout.py" in label
    print("✅ test_short_label_without_symbol passed.")


# ---------------------------------------------------------------------------
# Tests — build_graph structure
# ---------------------------------------------------------------------------

def test_output_has_required_keys() -> None:
    g = build_graph(SAMPLE_ANALYSIS)
    assert "nodes" in g, "missing nodes"
    assert "edges" in g, "missing edges"
    assert "summary" in g, "missing summary"
    assert "repo_path" in g, "missing repo_path"
    print("✅ test_output_has_required_keys passed.")


def test_changed_symbol_node_status() -> None:
    """calculate_total from discount.py must have status CHANGED."""
    g = build_graph(SAMPLE_ANALYSIS)
    nodes = _nodes_by_id(g)
    nid = "discount.py::calculate_total"
    assert nid in nodes, f"Expected node {nid}, got: {list(nodes)}"
    assert nodes[nid]["status"] == "CHANGED", f"Expected CHANGED, got: {nodes[nid]['status']}"
    print("✅ test_changed_symbol_node_status passed.")


def test_direct_symbol_node_status() -> None:
    """checkout.py::checkout is called from the blast radius and directly affected."""
    g = build_graph(SAMPLE_ANALYSIS)
    nodes = _nodes_by_id(g)
    nid = "checkout.py::checkout"
    assert nid in nodes, f"Expected node {nid}"
    assert nodes[nid]["status"] == "DIRECT", f"Expected DIRECT, got: {nodes[nid]['status']}"
    print("✅ test_direct_symbol_node_status passed.")


def test_indirect_symbol_node_status() -> None:
    """demo.py::main is indirectly affected."""
    g = build_graph(SAMPLE_ANALYSIS)
    nodes = _nodes_by_id(g)
    nid = "demo.py::main"
    assert nid in nodes, f"Expected node {nid}"
    assert nodes[nid]["status"] == "INDIRECT", f"Expected INDIRECT, got: {nodes[nid]['status']}"
    print("✅ test_indirect_symbol_node_status passed.")


def test_edges_from_symbol_relationships() -> None:
    """Edges must be created for each symbol_relationship."""
    g = build_graph(SAMPLE_ANALYSIS)
    edge_keys = {(e["source"], e["target"]) for e in g["edges"]}
    assert ("checkout.py::checkout", "discount.py::calculate_total") in edge_keys, \
        f"Missing checkout→discount edge. Edges: {edge_keys}"
    assert ("demo.py::main", "checkout.py::checkout") in edge_keys, \
        f"Missing demo→checkout edge. Edges: {edge_keys}"
    print("✅ test_edges_from_symbol_relationships passed.")


def test_no_duplicate_edges() -> None:
    """No (source, target) pair should appear more than once."""
    g = build_graph(SAMPLE_ANALYSIS)
    edge_keys = [(e["source"], e["target"]) for e in g["edges"]]
    assert len(edge_keys) == len(set(edge_keys)), \
        f"Duplicate edges found: {edge_keys}"
    print("✅ test_no_duplicate_edges passed.")


def test_node_has_file_and_symbol_fields() -> None:
    """Every node must have 'file', 'symbol', 'status', 'label', 'id'."""
    g = build_graph(SAMPLE_ANALYSIS)
    for node in g["nodes"]:
        for key in ("id", "file", "status", "label"):
            assert key in node, f"Node missing key '{key}': {node}"
        assert "symbol" in node, f"Node missing 'symbol' key: {node}"
    print("✅ test_node_has_file_and_symbol_fields passed.")


def test_status_priority_changed_wins() -> None:
    """If a file appears as both changed and direct, CHANGED must win."""
    analysis = {
        **SAMPLE_ANALYSIS,
        # discount.py is changed AND also appears in directly_affected
        "directly_affected": ["checkout.py", "discount.py"],
    }
    g = build_graph(analysis)
    nodes = _nodes_by_id(g)
    # The symbol node for calculate_total should still be CHANGED
    nid = "discount.py::calculate_total"
    assert nodes[nid]["status"] == "CHANGED", \
        f"CHANGED should win over DIRECT: {nodes[nid]['status']}"
    print("✅ test_status_priority_changed_wins passed.")


def test_empty_analysis_produces_empty_graph() -> None:
    """An empty analysis should produce zero nodes and edges."""
    g = build_graph(EMPTY_ANALYSIS)
    assert g["nodes"] == [], f"Expected empty nodes, got: {g['nodes']}"
    assert g["edges"] == [], f"Expected empty edges, got: {g['edges']}"
    print("✅ test_empty_analysis_produces_empty_graph passed.")


def test_error_analysis_handled_gracefully() -> None:
    """An error analysis (invalid repo) must still return a valid graph structure."""
    g = build_graph(ERROR_ANALYSIS)
    assert "nodes" in g
    assert "edges" in g
    assert g.get("error") == ERROR_ANALYSIS["error"]
    print("✅ test_error_analysis_handled_gracefully passed.")


def test_edge_type_preserved() -> None:
    """Edge type from symbol_relationships must be preserved in graph edges."""
    g = build_graph(SAMPLE_ANALYSIS)
    for edge in g["edges"]:
        assert edge["type"] in ("calls_or_uses", "import"), \
            f"Unexpected edge type: {edge['type']}"
    print("✅ test_edge_type_preserved passed.")


def test_reason_preserved_in_edge() -> None:
    """Edge reason strings must be carried through from the analysis."""
    g = build_graph(SAMPLE_ANALYSIS)
    edge = next(
        (e for e in g["edges"]
         if e["source"] == "checkout.py::checkout"
         and e["target"] == "discount.py::calculate_total"),
        None
    )
    assert edge is not None
    assert "calculate_total" in edge["reason"], \
        f"Expected reason to mention calculate_total: {edge['reason']}"
    print("✅ test_reason_preserved_in_edge passed.")


def test_file_only_nodes_have_none_symbol() -> None:
    """
    When a changed file has no symbols listed, its node should exist with symbol=None.
    """
    analysis = {
        **SAMPLE_ANALYSIS,
        "changed_files": [{"file": "discount.py", "symbols": []}],
        "symbol_relationships": [],
        "relationships": [],
    }
    g = build_graph(analysis)
    nodes = _nodes_by_id(g)
    assert "discount.py" in nodes, "Expected file-level node for discount.py"
    assert nodes["discount.py"]["symbol"] is None
    print("✅ test_file_only_nodes_have_none_symbol passed.")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    test_node_id_with_symbol()
    test_node_id_without_symbol()
    test_short_label_with_symbol()
    test_short_label_without_symbol()
    test_output_has_required_keys()
    test_changed_symbol_node_status()
    test_direct_symbol_node_status()
    test_indirect_symbol_node_status()
    test_edges_from_symbol_relationships()
    test_no_duplicate_edges()
    test_node_has_file_and_symbol_fields()
    test_status_priority_changed_wins()
    test_empty_analysis_produces_empty_graph()
    test_error_analysis_handled_gracefully()
    test_edge_type_preserved()
    test_reason_preserved_in_edge()
    test_file_only_nodes_have_none_symbol()
    print("\n🎉 All graph transformer tests passed.")
