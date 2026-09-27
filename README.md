# ImpactProof Public Demo

A standalone, deterministic public demo packaged from the existing ImpactProof implementation. It demonstrates a bundled Git change and target repository through **Investigate → Predict → Prove → Regression → Explain → Fix → Verify**. IBM Bob, MCP, credentials, and third-party Python packages are not required.

The root page introduces ImpactProof. Click **DEMO NOW** to open the existing idle dashboard, then click **INVESTIGATE CHANGE** to run the bundled static analyzer and the `premium_checkout_refund` scenario. The scenario executes the bundled premium $100 purchase, observes the $90 charged amount, and detects the pre-fix $100 refund. Explain presents the proof output; Apply Fix changes only `demo/refund.py` from `original_amount` to `final_amount`; Verify reruns the scenario and reports PASS only when execution passes. Undo is available after successful verification.

## Run locally

```sh
cd impactproof-public
python3 graph/public_server.py
```

Open <http://localhost:8000>. The server binds to `0.0.0.0` and reads `PORT` (default `8000`). No environment variables are required.

## Docker

```sh
docker build -t impactproof-public .
docker run --rm -p 8000:8000 impactproof-public
```

Set `-e PORT=...` and map that container port if needed. The container runs as a non-root user. The demo repository is bundled in the image and is the only target exposed by the UI; the browser cannot supply paths or commands. Demo file changes are container-local and reset when the container is recreated.

## Package contents

The package reuses the existing analyzer, graph transformer, D3 dashboard template, proof engine, named scenario, and safe change/undo helpers. Its public server adapts the local dashboard to bind for deployment, adds `/healthz`, fixes the target to the bundled demo, and uses a bounded deterministic explanation and a fixed allowlisted patch. Bob bridges, MCP entrypoints, Bob integration bridges, credentials/configuration, Bob IDE setup, unrelated docs, Node dependencies, and extra demo repository files are excluded.

The dashboard retains the existing D3 CDN reference, so a browser needs access to jsDelivr to render the graph. The application server and proof workflow itself have no network dependency.
