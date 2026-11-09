# Jana Sponsored Study Places

A workshop organizer has $36 for three learners. Who will turn up? Jana lets the organizer offer everyone a place, then charges $12 for each eligible learner who claims one. Each funded learner can activate one month of Jana online Study and use their own worksheet for practice.

[Watch the PayPal AI Hackathon demo on YouTube](https://youtu.be/kshmninfwaY)

## Run the workshop

Use Node 24.21, pnpm 10.34.6, and uv 0.10.x with Python 3.12. From the repository root on macOS or Linux, run:

```sh
pnpm install --frozen-lockfile --ignore-scripts
VITE_JANA_HOSTED=1 pnpm build
uv sync --locked --all-packages --no-default-groups --group dev
uv run --no-sync python demo/workshop.py
```

1. Open **http://localhost:8474/signin**. Use **nia@example.test** and the temporary password printed in your terminal.
2. Open Nia’s profile, choose **Study**, and open **Chemistry workshop: catalysts and equilibrium**.
3. Choose **Use a sponsored place**. The local coordinator closes claims after a few seconds. Choose **Activate my online month** when the offer is funded.
4. Open the saved quiz. On the catalyst question, try an answer, check it, and open the citation. The highlighted worksheet sentence explains why a catalyst speeds both directions while the equilibrium constant stays the same. Return to the question and check your revised answer.

The local rehearsal runs the Jana React client, personal Study core, payment ledger, and signed month activation. A simulated wallet stands in for PayPal, and the quiz and chat replay saved Gemini output from the sample worksheet. No PayPal or model account is needed. Stop the server with Ctrl+C. Restarting creates a new disposable workshop.

## What PayPal and Gemini do

- **PayPal Sandbox Orders v2** creates one order with an authorization ceiling of $36. The buyer approves it on PayPal. Jana retrieves the order and verifies its merchant and enrolled buyer.
- **PayPal Payments v2** settles the frozen number of eligible claims. Two claims mean one final $24 capture. Zero claims mean a void and $0 captured. Verified webhook events trigger reconciliation. PayPal request IDs and a durable SQLite ledger let the server recover when a response is lost.
- **Gemini 3.8 Flash on Google Vertex AI** generates a quiz or answer from the passages a learner chooses to share. Jana checks quotation offsets and links the answer back to the worksheet. Online generation requires a separately configured gateway. The local replay uses the included chemistry passage and preserves its actual answer and citations.

The film combines the app rehearsal, separately verified PayPal Sandbox settlements, and previously generated Gemini material. The two-claim and zero-claim receipts describe two different workshops.

## Verify the payment cases

After the Python setup above:

```sh
uv run --no-sync pytest -q services/agent-core/tests/test_sponsored_study.py
```

The eight tests cover zero, one, two, and three claims, withdrawing and retrying, a lost capture response, wrong-buyer or mismatched payment data, eligibility changes, signed activation and webhook reconciliation. They use a simulated wallet.

### Optional live PayPal Sandbox checks

Use your own PayPal developer Sandbox app, its US business seller and a separate US personal test buyer. The checker uses only the PayPal Sandbox API. In a private directory outside the repository, save an owner-readable-only TOML file with your Sandbox app’s `client_id` and `client_secret`. In a second local file named `pilot.toml` set `credentials_file` to the absolute path of that first file, and set `merchant` and `payer` under `[enrollment]` to the two Sandbox account IDs. Apply `chmod 600` to both files.

From the repo root, replace both paths below with your private paths. Choose a new run directory the first time. Reuse it for all steps and retries.

```sh
uv run --no-sync python -m jana_tools.sponsored_check start --pilot /path/to/pilot.toml --run /path/to/private-run
uv run --no-sync python -m jana_tools.sponsored_check serve --pilot /path/to/pilot.toml --run /path/to/private-run
```

Leave the second command running. Approve the two printed links as your Sandbox buyer. In another terminal run:

```sh
uv run --no-sync python -m jana_tools.sponsored_check finish --pilot /path/to/pilot.toml --run /path/to/private-run
```

A completed run checks the $24 final capture, the zero-claim void, and the two signed single-learner entitlements. It writes a redacted verification record in the private run directory. Only test funds move. The browser approval and live credential setup are optional for the local judge walkthrough.

## Source

| Component | Where to look |
| --- | --- |
| PayPal requests, idempotency and durable settlement | `scripts/jana_tools/sponsored_places.py` |
| Sponsor and learner routes, verified webhooks and activation | `scripts/jana_tools/hosted_sponsorship.py` |
| Adult workshop and single-model gateway | `scripts/jana_tools/adult_workshop.py` |
| Learner interface and shared client | `apps/desktop/src`, `packages/client-core`, `packages/ui` |
| Study storage, output validation and provider adapter | `services/agent-core/agent_core`, `services/cloud-gateway/cloud_gateway` |
| Rehearsal launcher and sample worksheet | `demo` |

This public source edition includes the code and runtime assets for the hackathon workshop. Jana is licensed under Apache-2.0. Third-party components retain their own licenses as listed in THIRD_PARTY_NOTICES.
