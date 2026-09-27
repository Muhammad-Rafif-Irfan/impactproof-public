#!/usr/bin/env python3
"""Self-contained public ImpactProof demo server (stdlib HTTP server)."""
from __future__ import annotations

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "demo"
SCENARIO = "premium_checkout_refund"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "graph"))
sys.path.insert(0, str(ROOT / "impactproof"))

from graph import build_graph, merge_proof_results  # noqa: E402
from proof.engine import run_scenario, load_scenario  # noqa: E402
import analyze  # noqa: E402
import regression_tools  # noqa: E402

TEMPLATE = (ROOT / "graph" / "viewer_template.html").read_text(encoding="utf-8")
LANDING_PAGE = (ROOT / "graph" / "landing.html").read_text(encoding="utf-8")
LOGO_ASSET = ROOT / "graph" / "assets" / "impactproof-logo.png"
SCENARIO_SPEC = load_scenario(SCENARIO)
LOCK = threading.RLock()
FIX_APPLIED = False
VERIFIED = False
INVESTIGATED = False
EXPLAINED = False
LATEST_STATUS = None


def investigate(on_step=None):
    if on_step:
        on_step("analyze", "running", "Analyzing bundled change")
    # Invoke the proven static analyzer against only the bundled Git checkout.
    import subprocess
    completed = subprocess.run([sys.executable, str(ROOT / "impactproof/analyze.py"), str(DEMO)], capture_output=True, text=True, timeout=30, check=True)
    analysis = json.loads(completed.stdout)
    if on_step:
        on_step("analyze", "complete", "Change detected")
        on_step("graph", "running", "Building impact graph")
    graph_data = build_graph(analysis)
    if on_step:
        on_step("graph", "complete", "Impact graph built")
        on_step("proof", "running", "Running proof scenario")
    proof = run_scenario(str(DEMO), SCENARIO_SPEC)
    merge_proof_results(graph_data, [proof], {SCENARIO: SCENARIO_SPEC})
    graph_data["investigation"] = {"status": proof["status"], "scenarios": [proof], "errors": []}
    graph_data["repo_path"] = "bundled-demo"
    if on_step:
        on_step("proof", "complete" if proof["status"] in {"PASS", "REGRESSION"} else "error", "Proof scenario executed")
    return graph_data


def initial_graph():
    return {"repo_path": "bundled-demo", "nodes": [], "edges": [], "summary": {"changed_files": 0, "affected_files": 0, "symbol_relationships": 0, "regressions": 0}}


def html_page():
    data = initial_graph()
    return (TEMPLATE.replace("{{GRAPH_DATA}}", "null")
            .replace("{{INVESTIGATE_DISABLED}}", "")
            .replace("{{INVESTIGATE_HINT}}", ""))


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print("[impactproof-public] " + (fmt % args), file=sys.stderr)

    def _json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _same_origin(self):
        origin = self.headers.get("Origin")
        if origin is None:
            return True
        parsed = urlsplit(origin)
        return parsed.netloc == self.headers.get("Host", "") and parsed.scheme in {"http", "https"}

    def _body(self):
        if not self._same_origin() or self.headers.get("Content-Type", "").split(";", 1)[0].lower() != "application/json":
            raise ValueError("Invalid request")
        length = int(self.headers.get("Content-Length", "-1"))
        if length < 0 or length > 4096:
            raise ValueError("Invalid request")
        value = json.loads(self.rfile.read(length))
        if not isinstance(value, dict):
            raise ValueError("Invalid request")
        return value

    def do_GET(self):
        if self.path == "/healthz":
            return self._json(200, {"status": "ok"})
        if self.path == "/api/investigate":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            def emit(item):
                self.wfile.write(("data: " + json.dumps(item) + "\n\n").encode())
                self.wfile.flush()
            try:
                with LOCK:
                    global VERIFIED, INVESTIGATED, EXPLAINED, LATEST_STATUS
                    VERIFIED = False
                    INVESTIGATED = False
                    EXPLAINED = False
                    LATEST_STATUS = None
                    data = investigate(lambda *args: emit({"step": args[0], "state": args[1], "message": args[2]}))
                    LATEST_STATUS = data["investigation"]["status"]
                    INVESTIGATED = True
                emit({"done": True, "graph": data})
            except Exception as exc:
                emit({"error": str(exc)[:300]})
            return
        if self.path == "/assets/impactproof-logo.png":
            body = LOGO_ASSET.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "public, max-age=3600")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/":
            body = LANDING_PAGE.encode()
        elif self.path in {"/demo", "/demo/"}:
            body = html_page().encode()
        else:
            return self.send_error(404)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        try:
            body = self._body()
        except (ValueError, json.JSONDecodeError):
            return self._json(400, {"status": "error", "message": "Invalid request"})
        global FIX_APPLIED, VERIFIED, EXPLAINED
        with LOCK:
            if self.path == "/api/explain":
                if body not in ({}, {"repo_path": "bundled-demo"}):
                    return self._json(400, {"status": "error"})
                if not INVESTIGATED or LATEST_STATUS != "REGRESSION":
                    return self._json(409, {"status": "error", "message": "Investigate and prove a regression before Explain."})
                try:
                    evidence = regression_tools.cmd_explain(str(DEMO), SCENARIO)
                    proof = evidence
                    if proof.get("proof_status") != "REGRESSION":
                        return self._json(409, {"status": "error", "message": "No regression evidence is available."})
                    mismatches = proof.get("mismatches", [])
                    actual = proof.get("actual") or {}
                    analysis = {"status": "complete", "proof_status": "REGRESSION",
                        "root_cause": "refund.py::refund returns transaction['original_amount']; the executed premium checkout charged $90, but the refund returns $100.",
                        "demo_message": "The proof confirms the refund uses the pre-discount amount. A minimal correction is to return final_amount, which is the amount actually paid.",
                        "mismatches": mismatches, "expected": proof.get("expected", {}), "actual": actual,
                        "changed_files": evidence.get("changed_files", []), "regression_nodes": evidence.get("regression_nodes", [])}
                    EXPLAINED = True
                    return self._json(200, {"status": "success", "analysis": analysis})
                except Exception:
                    return self._json(500, {"status": "error", "message": "Deterministic explanation failed."})
            if self.path == "/api/fix":
                if body not in ({}, {"repo_path": "bundled-demo"}):
                    return self._json(400, {"status": "error"})
                if not INVESTIGATED or LATEST_STATUS != "REGRESSION" or not EXPLAINED:
                    return self._json(409, {"status": "error", "message": "Explain the proven regression before applying the demo fix."})
                if FIX_APPLIED:
                    return self._json(409, {"status": "error", "message": "The demo fix is already applied."})
                refund = DEMO / "refund.py"
                current = refund.read_text(encoding="utf-8")
                expected = 'return transaction["original_amount"]'
                replacement = 'return transaction["final_amount"]'
                if current.count(expected) != 1:
                    return self._json(409, {"status": "error", "message": "Bundled demo source changed; refusing the fix."})
                result = regression_tools.cmd_apply_fix(str(DEMO), "refund.py", current.replace(expected, replacement))
                if result.get("status") != "applied":
                    return self._json(500, {"status": "error", "message": "Fix could not be safely applied."})
                (DEMO / "refund.py.bak").unlink(missing_ok=True)
                result["backup"] = None
                FIX_APPLIED, VERIFIED = True, False
                result["description"] = "Return the amount actually charged for the purchase."
                return self._json(200, {"status": "success", "fix": result})
            if self.path == "/api/verify-fix":
                if body not in ({}, {"repo_path": "bundled-demo", "scenario": SCENARIO}):
                    return self._json(400, {"status": "error"})
                if not FIX_APPLIED or not EXPLAINED:
                    return self._json(409, {"status": "error", "message": "Explain and apply the demo fix before verification."})
                proof = regression_tools.cmd_verify(str(DEMO), SCENARIO)
                VERIFIED = proof.get("status") == "PASS"
                return self._json(200, {"status": "success", "verification": proof})
            if self.path == "/api/undo-fix":
                if body:
                    return self._json(400, {"status": "error"})
                if not FIX_APPLIED or not VERIFIED:
                    return self._json(409, {"status": "error", "error": "Verify the applied demo fix before undoing it."})
                result = regression_tools.cmd_undo_fix(str(DEMO))
                if result.get("status") == "undone":
                    FIX_APPLIED, VERIFIED = False, False
                return self._json(200, result)
        return self.send_error(404)


def main():
    port = int(os.environ.get("PORT", "8000"))
    server = Server(("0.0.0.0", port), Handler)
    print(f"ImpactProof public demo listening on 0.0.0.0:{server.server_port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
