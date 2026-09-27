#!/usr/bin/env python3
"""
ImpactProof Phase 3 / 4B — Graph data transformer.

Takes the JSON output of analyze.py and produces a graph structure:

    {
      "nodes": [
        {
          "id": "checkout.py::checkout",
          "file": "checkout.py",
          "symbol": "checkout",       # None for file-only nodes
          "status": "DIRECT",          # CHANGED | DIRECT | INDIRECT | REGRESSION
          "label": "checkout\ncheckout.py",
          "proof_status": null,        # Phase 4B: "PASS" | "REGRESSION" | "ERROR" | null
          "proof_evidence": null       # Phase 4B: proof result dict | null
        },
        ...
      ],
      "edges": [
        {
          "source": "discount.py::calculate_total",
          "target": "checkout.py::checkout",
          "type": "calls_or_uses",
          "reason": "..."
        },
        ...
      ],
      "summary": { ... }
    }

All nodes and edges are derived exclusively from analyze_change() output.
No additional inference is performed here.
Proof evidence is merged from proof/engine.py results — nodes are only
marked REGRESSION when actual proof execution returns REGRESSION.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Node ID helpers
# ---------------------------------------------------------------------------

def _node_id(file: str, symbol: str | None = None) -> str:
    if symbol:
        return f"{file}::{symbol}"
    return file


def _short_label(file: str, symbol: str | None = None) -> str:
    basename = Path(file).name
    if symbol:
        return f"{symbol}\n{basename}"
    return basename


# ---------------------------------------------------------------------------
# Core transformer
# ---------------------------------------------------------------------------

def build_graph(analysis: dict) -> dict:
    """
    Transform the output of analyze.py into a graph dict with nodes and edges.

    The source-of-truth is the analysis JSON — no additional edges are inferred.
    Nodes are created at symbol level when symbol_relationships provide symbol
    information; otherwise at file level.
    """
    nodes: dict[str, dict] = {}   # id -> node
    edges: list[dict] = []

    def add_node(file: str, symbol: str | None, status: str) -> str:
        nid = _node_id(file, symbol)
        if nid not in nodes:
            nodes[nid] = {
                "id": nid,
                "file": file,
                "symbol": symbol,
                "status": status,
                "label": _short_label(file, symbol),
                "proof_status": None,
                "proof_evidence": None,
            }
        else:
            # Upgrade status priority: CHANGED > DIRECT > INDIRECT
            priority = {"CHANGED": 0, "DIRECT": 1, "INDIRECT": 2}
            existing = nodes[nid]["status"]
            if priority[status] < priority[existing]:
                nodes[nid]["status"] = status
        return nid

    # 1. Changed files — extract symbols from changed_files array
    changed_files_set: set[str] = set()
    for cf in analysis.get("changed_files", []):
        f = cf["file"]
        changed_files_set.add(f)
        syms = cf.get("symbols", [])
        if syms:
            for sym in syms:
                add_node(f, sym, "CHANGED")
        else:
            add_node(f, None, "CHANGED")

    # 2. Directly affected files
    directly_affected_set = set(analysis.get("directly_affected", []))
    for f in analysis.get("directly_affected", []):
        add_node(f, None, "DIRECT")

    # 3. Indirectly affected files
    for f in analysis.get("indirectly_affected", []):
        # Don't downgrade a DIRECT node
        add_node(f, None, "INDIRECT")

    # 4. Symbol relationships — these define symbol-level nodes and edges
    seen_edges: set[tuple[str, str]] = set()
    for rel in analysis.get("symbol_relationships", []):
        frm = rel["from"]
        to = rel["to"]

        from_file = frm["file"]
        from_sym = frm.get("symbol")
        to_file = to["file"]
        to_sym = to.get("symbol")

        # Determine status for each endpoint
        def _status(f: str) -> str:
            if f in changed_files_set:
                return "CHANGED"
            if f in directly_affected_set:
                return "DIRECT"
            return "INDIRECT"

        from_id = add_node(from_file, from_sym, _status(from_file))
        to_id = add_node(to_file, to_sym, _status(to_file))

        ekey = (from_id, to_id)
        if ekey not in seen_edges:
            seen_edges.add(ekey)
            edges.append({
                "source": from_id,
                "target": to_id,
                "type": rel.get("type", "calls_or_uses"),
                "reason": rel.get("reason", ""),
            })

    # 5. File-level import edges (from relationships array) — add file nodes and
    #    edges only if both endpoints already exist OR if either is in changed set.
    #    We only add these for pairs not already covered by symbol-level edges.
    for rel in analysis.get("relationships", []):
        frm_file = rel["from"]
        to_file = rel["to"]

        def _fstatus(f: str) -> str:
            if f in changed_files_set:
                return "CHANGED"
            if f in directly_affected_set:
                return "DIRECT"
            return "INDIRECT"

        from_id = _node_id(frm_file, None)
        to_id = _node_id(to_file, None)

        # Only add file-level edges when neither endpoint has a symbol-level node
        # to avoid duplicate/confusing edges at different granularities.
        has_sym_from = any(n["file"] == frm_file and n["symbol"] for n in nodes.values())
        has_sym_to = any(n["file"] == to_file and n["symbol"] for n in nodes.values())
        if has_sym_from or has_sym_to:
            continue

        add_node(frm_file, None, _fstatus(frm_file))
        add_node(to_file, None, _fstatus(to_file))

        ekey = (from_id, to_id)
        if ekey not in seen_edges:
            seen_edges.add(ekey)
            edges.append({
                "source": from_id,
                "target": to_id,
                "type": "import",
                "reason": rel.get("reason", ""),
            })

    return {
        "nodes": list(nodes.values()),
        "edges": edges,
        "summary": analysis.get("summary", {}),
        "repo_path": analysis.get("repo_path", ""),
        "error": analysis.get("error"),
    }


# ---------------------------------------------------------------------------
# Phase 4B — Proof result merger
# ---------------------------------------------------------------------------

def _find_regression_nodes(
    nodes: list[dict],
    proof_result: dict,
) -> list[str]:
    """
    Identify node IDs that are directly associated with the failing behavior
    in a REGRESSION proof result.

    Strategy:
    - The scenario's extract spec tells us which capture/key maps to which
      output field.  When a mismatch occurs on a field, we look at the step
      that produced that capture and use the module+function as the target node.
    - We only match nodes that already exist in the graph (no new nodes invented).
    - Returns a list of node IDs to be marked REGRESSION.
    """
    if proof_result.get("status") != "REGRESSION":
        return []

    mismatches = proof_result.get("mismatches", [])
    scenario = proof_result.get("_scenario_spec")  # attached by merge caller

    if not scenario or not mismatches:
        return []

    mismatch_keys = {m["key"] for m in mismatches}

    # Build capture -> step mapping
    capture_to_step: dict[str, dict] = {}
    for step in scenario.get("steps", []):
        capture_to_step[step["capture"]] = step

    # For each mismatching extract key, find the producing step
    node_ids: list[str] = []
    extract = scenario.get("extract", {})
    node_by_id = {n["id"]: n for n in nodes}

    for key in mismatch_keys:
        spec = extract.get(key, {})
        capture_name = spec.get("from_capture", key)
        step = capture_to_step.get(capture_name)
        if not step:
            continue
        module = step.get("module", "")
        function = step.get("function", "")
        # Try symbol-level node first: "module.py::function"
        candidate_id = _node_id(f"{module}.py", function)
        if candidate_id in node_by_id:
            node_ids.append(candidate_id)
        else:
            # Fall back to file-level node
            file_id = _node_id(f"{module}.py")
            if file_id in node_by_id:
                node_ids.append(file_id)

    return node_ids


def merge_proof_results(
    graph_data: dict,
    proof_results: list[dict],
    scenario_specs: dict[str, dict] | None = None,
) -> dict:
    """
    Merge proof execution results into a graph data dict.

    For each proof result:
    - If REGRESSION: find the graph node(s) that own the failing symbol,
      set proof_status="REGRESSION" on them, and attach proof_evidence.
    - If PASS: set proof_status="PASS" on directly targeted nodes (no
      visual change to impact status, just evidence).
    - If ERROR: set proof_status="ERROR" on targeted nodes.

    The visual ``status`` field (CHANGED/DIRECT/INDIRECT) is never
    downgraded by proof results.  REGRESSION is only a proof_status.

    No new nodes or edges are created.

    Returns the mutated graph_data (also modifies in-place for efficiency).
    """
    nodes = graph_data.get("nodes", [])
    node_by_id = {n["id"]: n for n in nodes}
    summary = graph_data.get("summary", {})
    regression_count = 0

    for proof in proof_results:
        # Attach scenario spec so _find_regression_nodes can use it
        scenario_name = proof.get("scenario", "")
        spec = (scenario_specs or {}).get(scenario_name)
        proof_with_spec = {**proof, "_scenario_spec": spec}

        status = proof.get("status")

        if status == "REGRESSION":
            target_ids = _find_regression_nodes(nodes, proof_with_spec)
            for nid in target_ids:
                node = node_by_id.get(nid)
                if node:
                    node["proof_status"] = "REGRESSION"
                    node["proof_evidence"] = {
                        k: v for k, v in proof.items()
                        if k != "_scenario_spec"
                    }
                    regression_count += 1

        elif status == "PASS":
            # Record PASS evidence on nodes targeted by the scenario
            if spec:
                for step in spec.get("steps", []):
                    module = step.get("module", "")
                    function = step.get("function", "")
                    candidate_id = _node_id(f"{module}.py", function)
                    node = node_by_id.get(candidate_id)
                    if node and node.get("proof_status") is None:
                        node["proof_status"] = "PASS"
                        node["proof_evidence"] = {
                            k: v for k, v in proof.items()
                            if k != "_scenario_spec"
                        }

        elif status == "ERROR":
            if spec:
                for step in spec.get("steps", []):
                    module = step.get("module", "")
                    function = step.get("function", "")
                    candidate_id = _node_id(f"{module}.py", function)
                    node = node_by_id.get(candidate_id)
                    if node:
                        node["proof_status"] = "ERROR"
                        node["proof_evidence"] = {
                            k: v for k, v in proof.items()
                            if k != "_scenario_spec"
                        }

    summary["regressions"] = regression_count
    graph_data["summary"] = summary
    return graph_data
