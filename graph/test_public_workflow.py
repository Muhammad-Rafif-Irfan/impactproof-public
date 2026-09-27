"""End-to-end public workflow checks, isolated to a disposable demo checkout."""
import http.client
import json
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

import public_server


class PublicWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.original_demo = public_server.DEMO
        public_server.DEMO = Path(self.temp.name) / "demo"
        shutil.copytree(self.original_demo, public_server.DEMO, ignore=shutil.ignore_patterns("__pycache__"))
        public_server.FIX_APPLIED = False
        public_server.VERIFIED = False
        public_server.INVESTIGATED = False
        public_server.EXPLAINED = False
        public_server.LATEST_STATUS = None
        self.server = public_server.Server(("127.0.0.1", 0), public_server.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=30)

    def tearDown(self):
        self.conn.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        public_server.DEMO = self.original_demo
        public_server.FIX_APPLIED = False
        public_server.VERIFIED = False
        public_server.INVESTIGATED = False
        public_server.EXPLAINED = False
        public_server.LATEST_STATUS = None
        self.temp.cleanup()

    def get(self, path):
        self.conn.request("GET", path)
        response = self.conn.getresponse()
        return response.status, response.read().decode()

    def get_bytes(self, path):
        self.conn.request("GET", path)
        response = self.conn.getresponse()
        return response.status, response.getheader("Content-Type"), response.read()

    def post(self, path, data):
        self.conn.request("POST", path, json.dumps(data), {"Content-Type": "application/json"})
        response = self.conn.getresponse()
        return response.status, json.loads(response.read())

    def test_real_workflow_and_bounded_routes(self):
        status, landing = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("ImpactProof", landing)
        self.assertIn("Predict what your code change could break,<br><span>then prove it by execution.</span>", landing)
        self.assertIn("ImpactProof connects change impact analysis with deterministic execution evidence, helping developers predict potential blast radius, identify regressions, and verify fixes.", landing)
        self.assertIn("What are we demonstrating?", landing)
        self.assertIn("The code change", landing)
        self.assertIn("From change to evidence", landing)
        self.assertIn("Prediction <span>≠</span> Proof", landing)
        self.assertIn("discount.py</span></li><li><span>checkout.py", landing)
        self.assertIn("PREMIUM_DISCOUNT = 0.10", landing)
        self.assertIn("PREMIUM_MINIMUM_SPEND = 50.0", landing)
        self.assertIn("calculate_total", landing)
        self.assertIn("Built with IBM Bob", landing)
        self.assertIn('src="/assets/impactproof-logo.png"', landing)
        self.assertIn("href=\"/demo\"", landing)
        self.assertIn("DEMO NOW", landing)
        self.assertIn("Demo runs on a preconfigured sample repository and scenario. User-provided source code is not analyzed.", landing)
        status, content_type, logo = self.get_bytes("/assets/impactproof-logo.png")
        self.assertEqual(status, 200)
        self.assertEqual(content_type, "image/png")
        self.assertTrue(logo.startswith(b"\x89PNG\r\n\x1a\n"))
        status, page = self.get("/demo")
        self.assertEqual(status, 200)
        self.assertIn("INVESTIGATE CHANGE", page)
        self.assertIn('let GRAPH_DATA = null;', page)
        status, health = self.get("/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(health)["status"], "ok")

        self.assertFalse((public_server.DEMO / ".git/impactproof/undo_state.json").exists())
        status = subprocess.run(["git", "status", "--short"], cwd=public_server.DEMO, capture_output=True, text=True, check=True).stdout.splitlines()
        self.assertEqual(status, [" M discount.py"])
        status, _ = self.post("/api/explain", {"repo_path": "bundled-demo"})
        self.assertEqual(status, 409, "Explain must not reveal regression before investigation")
        status, _ = self.post("/api/fix", {"repo_path": "bundled-demo"})
        self.assertEqual(status, 409, "Fix must remain gated until the regression is explained")

        status, events = self.get("/api/investigate")
        self.assertEqual(status, 200)
        result = json.loads(next(line[6:] for line in events.splitlines() if line.startswith("data: {\"done\"")))
        graph = result["graph"]
        self.assertEqual(graph["investigation"]["status"], "REGRESSION")
        self.assertEqual(graph["summary"]["changed_files"], 1)
        changed_nodes = [item for item in graph["nodes"] if item["status"] == "CHANGED"]
        self.assertEqual([(item["file"], item["symbol"]) for item in changed_nodes], [("discount.py", "calculate_total")])
        regression = next((item for item in graph["nodes"] if item["id"] == "refund.py::refund"), None)
        self.assertIsNotNone(regression, "Proof-derived regression node must be present in the graph")
        self.assertEqual(regression["proof_status"], "REGRESSION")
        self.assertEqual(graph["summary"]["regressions"], 1)
        self.assertIn("selectedProvenRegression", page)
        self.assertIn('id="demo-analyze-button"', page)
        self.assertIn('fetch("/api/explain"', page)
        proof = graph["investigation"]["scenarios"][0]
        self.assertEqual(proof["actual"]["final_amount"], 90.0)
        self.assertEqual(proof["actual"]["refund_amount"], 100)

        status, explained = self.post("/api/explain", {"repo_path": "bundled-demo"})
        self.assertEqual(status, 200)
        evidence = explained["analysis"]
        self.assertEqual(evidence["proof_status"], "REGRESSION")
        self.assertEqual(evidence["expected"]["refund_amount"], 90.0)
        self.assertEqual(evidence["actual"]["refund_amount"], 100)
        self.assertEqual(evidence["mismatches"], [{"key": "refund_amount", "expected": 90.0, "actual": 100}])
        status, _ = self.post("/api/fix", {"repo_path": "../../etc"})
        self.assertEqual(status, 400)
        status, fixed = self.post("/api/fix", {"repo_path": "bundled-demo"})
        self.assertEqual(status, 200)
        self.assertEqual(fixed["fix"]["file"], "refund.py")
        self.assertFalse((public_server.DEMO / "refund.py.bak").exists())
        current = subprocess.run(["git", "status", "--short"], cwd=public_server.DEMO, capture_output=True, text=True, check=True).stdout.splitlines()
        self.assertEqual(current, [" M discount.py", " M refund.py"])
        status, verified = self.post("/api/verify-fix", {"repo_path": "bundled-demo", "scenario": "premium_checkout_refund"})
        self.assertEqual(status, 200)
        self.assertEqual(verified["verification"]["status"], "PASS")
        self.assertEqual(verified["verification"]["actual"]["refund_amount"], 90.0)
        self.assertEqual(verified["verification"]["expected"]["refund_amount"], 90.0)
        status, undone = self.post("/api/undo-fix", {})
        self.assertEqual(status, 200)
        self.assertEqual(undone["status"], "undone")
        self.assertIn('return transaction["original_amount"]', (public_server.DEMO / "refund.py").read_text())
        restored = subprocess.run(["git", "status", "--short"], cwd=public_server.DEMO, capture_output=True, text=True, check=True).stdout.splitlines()
        self.assertEqual(restored, [" M discount.py"])
        undo_state = json.loads((public_server.DEMO / ".git/impactproof/undo_state.json").read_text())
        self.assertEqual(undo_state["history"][-1]["status"], "undone")

        status, events = self.get("/api/investigate")
        self.assertEqual(status, 200)
        second = json.loads(next(line[6:] for line in events.splitlines() if line.startswith('data: {"done"')))["graph"]
        self.assertEqual(second["investigation"]["status"], "REGRESSION")
        self.assertEqual(second["summary"]["changed_files"], 1)
        self.assertTrue(any(item["id"] == "refund.py::refund" and item["proof_status"] == "REGRESSION" for item in second["nodes"]))


if __name__ == "__main__":
    unittest.main()
