# TraceLens — demo video script (target 2:45, hard max 3:00)

Record at 1920×1080, browser zoom 100%. Before recording:

```
docker compose up -d falkordb && python -m backend.seed
uvicorn backend.api.main:app --port 8000 ; cd frontend && npm run dev
# pre-bake the guest run so the video never waits on an LLM (about 2 minutes live):
RUN_LIVE_LLM=1 python -m agents.langgraph_guest.agent inc-1042      # or the quota-free replay:
python -m evals.e2e_guest --tenant demo --incident inc-1042          # provenance mcp:langgraph-replay
python -m evals.e2e_guest --tenant demo --incident inc-1042 --naive  # a bad proposal the server rejects
```

Open `http://localhost:5173/?tenant=demo`. Keep the cursor slow. Say the bold lines.

| t | Beat | On screen | Say |
|---|---|---|---|
| 0:00 | Problem (10 s) | Idle dashboard, dependency graph | **"When checkout breaks, everyone looks at the last deploy to checkout. It's usually wrong. TraceLens walks the dependency graph in FalkorDB to find the upstream cause, and shows the suspects it ruled out."** |
| 0:10 | Scenario A (35 s) | Select `inc-1042`, Investigate; traversal animates; open "Show Cypher" on step 2; switch to Hypotheses | **"Every step is a real Cypher query. Three deployments in the window. The checkout deploy at 14:05 is the obvious suspect, but checkout's errors began at 14:01, so it's ruled out. Cart never reaches payments-api, ruled out. The payments-api deploy that cut the DB pool from 50 to 5 passes all five checks, including the common-cause test: fraud-check shares the same database and is healthy."** |
| 0:45 | Guest agent picks tools (25 s) | Past investigations tab → entry `mcp:langgraph` (or `-replay`) → expand: tool order, italic "model's stated reason (unverified)", green `server_verification` step | **"An outside LangGraph agent did the same job over MCP. It chose these seven tools itself; its reasons are recorded but marked unverified. The server re-ran every graph check on every deployment on the path before storing anything."** (If you used the replay, say so: *"this one is a replayed recording of the live run."*) |
| 1:10 | Server rejects a bad proposal (15 s) | Same tab → the naive guest entry: `REJECTED` dep-chk-1405 with `temporal_precedence`, and `checked by server, not proposed` on the real cause | **"Here a guest blamed the checkout deploy. The server stored it as rejected with the failed check. A guest can contribute to memory; it can't corrupt it."** |
| 1:25 | Memory handoff (20 s) | Select `inc-1043 — Refunds failing`, Investigate; violet banner "learned by mcp:langgraph" | **"Refunds fail. My engine recalls the guest's verified investigation, re-checks refunds-worker's timeline, and answers in five tool calls instead of eleven. The memory is graph nodes, written by one agent, read by another."** |
| 1:45 | Approval → learned fix (25 s) | Back on `inc-1042`: click **Approve rollback (simulated)**; then select `inc-1051 — a week later`, Investigate; similar-incidents card shows **🧠 learned by TraceLens** on inc-1042 | **"The remediation is tied to that deployment and runbook. Approving it, simulated, writes a Resolution to the graph. A week later the same signature appears, and the history check cites the fix it learned from my click."** |
| 2:10 | Tenant isolation (10 s) | Change the URL to `?tenant=other`, run `inc-1043`: no memory banner, header badge `tenant: other` | **"Each tenant is its own FalkorDB graph. Memory never crosses."** |
| 2:20 | Scenario C, honest (10 s) | `inc-1044`: amber "Inconclusive", checked list, missing data | **"And when the evidence isn't there, it says so, and what data would resolve it."** |
| 2:30 | FalkorDB Browser (15 s) | `http://localhost:3000`, graph `tracelens:demo`; run the query below; **delete one DEPENDS_ON edge** in the browser, re-run inc-1042 in the dashboard | **"This is the real graph. If I delete the checkout → payments-api edge here… the path check fails and TraceLens no longer blames payments-api. Remove FalkorDB and nothing works."** |
| 2:45 | Close (5 s) | Dashboard | **"TraceLens: symptom to root cause, traced through the graph, checked before it's trusted, verified before it's remembered. Built on FalkorDB."** |

## FalkorDB Browser queries

```cypher
// the memory: investigations, hypotheses (incl. rejected), evidence edges
MATCH (inv:Investigation)-[:CONSIDERED]->(h:Hypothesis)-[r]->(n) RETURN inv, h, r, n

// the edge-delete trick (then re-run inc-1042 in the dashboard; restore with "reset demo")
MATCH (:Service {name:'checkout'})-[e:DEPENDS_ON]->(:Service {name:'payments-api'}) DELETE e
```

After the trick, click **reset demo** (or re-run `python -m backend.seed`) to restore the edge.

## Handy URLs

- Auto-run with replay: `http://localhost:5173/?tenant=demo&incident=inc-1042`
- Jump to verdict: `…&instant=1`; hypotheses tab: `…&tab=hypotheses`; history: `…&tab=history`; pause mid-replay: `…&step=5`
