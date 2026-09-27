# Public demo checks

Run the copied graph and proof checks directly:

```sh
python3 graph/test_graph.py
python3 graph/test_graph_proof.py
python3 graph/test_graph_explorer.py
python3 proof/test_proof.py
python3 graph/test_public_workflow.py
```

The public workflow test starts a local HTTP server and exercises the idle dashboard, health endpoint, investigation, observed regression, Explain, path rejection, Apply Fix, actual verification, and Undo in a temporary copy of the bundled demo repository.
