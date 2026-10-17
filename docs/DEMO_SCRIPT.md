# TraceLens — demo video script (2:30 target, max 3:00)

Record at 1920×1080. Browser zoom 100%. Dark theme is built in. Before recording: `docker compose up -d falkordb`,
seed, start API + Vite, open http://localhost:5173, click **reset demo** (clears memory so Scenario A is a full run).

Keep the cursor slow. Say the bold lines; the rest is stage direction.

| t | Beat | On screen | Say |
|---|---|---|---|
| 0:00 | Problem (10 s) | Idle dashboard, dependency graph visible | **"When checkout breaks, the first thing everyone looks at is the last deploy to checkout. It is usually wrong. TraceLens walks the dependency graph in FalkorDB to find the upstream cause — and shows you the suspects it ruled out."** |
| 0:10 | Pick Scenario A (10 s) | Select `inc-1042 — Checkout requests are failing`, press **Investigate** | **"Incident 1042: checkout error rate 38%. Deterministic mode, no LLM needed."** |
| 0:20 | Traversal animates (40 s) | Timeline fills step by step; graph lights up: checkout → 9 upstream services; three deploy chips appear; then two get struck through | **"Every step is a real Cypher query — here's `upstream_dependencies`, a variable-length traversal; click Show Cypher."** (open it on step 2) **"Three deployments in the window. The checkout deploy at 14:05 is the obvious suspect — but checkout's errors started at 14:01. The graph says the symptom predates the change, so it's ruled out. Cart's deploy is ruled out too: the earliest errors sit at payments-api, and cart never reaches payments-api."** |
| 1:00 | Root cause path + history + runbook (30 s) | Switch to **Root cause & remediation**; red path checkout → payments-api; 5/5 checks; similar incident inc-0877 with `ErrorSignature` / `Service` / `CodeChange file` chips; runbook steps | **"Best-supported explanation: the payments-api deploy at 13:52 that cut the Postgres pool from 50 to 5. Five of five graph checks — including a common-cause test: fraud-check shares the same database and is healthy, so the database isn't the problem. And it found incident 0877 three weeks ago through a shared ErrorSignature node — same service, same file — resolved by a rollback and this runbook."** |
| 1:30 | Approve (10 s) | Click **Approve rollback (simulated)** → status executed | **"The remediation is tied to that exact deployment and runbook. It's simulated, and it needs a human click. That click is written to the graph as an Action."** |
| 1:40 | Scenario B from memory (20 s) | Select `inc-1043 — Refunds failing`, Investigate; violet "Answered from memory — learned …" banner; 5 tool calls | **"Now refunds fail. TraceLens recalls the investigation it just wrote, checks refunds-worker depends on payments-api and that its errors started after the deploy — and answers in five tool calls instead of eleven, with 'operator confirmed'. That memory is graph nodes the agent wrote itself."** |
| 2:00 | Scenario C honest (15 s) | Select `inc-1044 — Search results are empty`, Investigate; amber "Inconclusive"; the checked list + missing data | **"And when the evidence isn't there, it says so. Search depends on an external vendor with no deployments or error data modeled. Inconclusive — here's exactly what it checked and what data would resolve it."** |
| 2:15 | FalkorDB Browser (10 s) | Tab to http://localhost:3000, run `MATCH (i:Investigation)-[:CONSIDERED]->(h:Hypothesis)-[r]->(n) RETURN i,h,r,n` | **"This is the real graph: investigations, hypotheses — including the rejected ones — and the evidence edges that support or reject them."** |
| 2:25 | Close (5 s) | Back to dashboard | **"TraceLens: symptom to root cause, proven by traversal, remembered for next time. Built on FalkorDB."** |

## Optional beats if you have 30 s to spare (Track 02 / MCP)

- **Guest handoff (15 s):** before recording, run `RUN_LIVE_LLM=1 python -m agents.langgraph_guest.agent inc-1042` (or the quota-free `python -m backend.mcp_client_demo inc-1042`). Then in the dashboard open **Past investigations**: the entry reads `mcp:langgraph · server-verified`, and its trace shows the model's stated reasons in italics. Run `inc-1043`: the banner says **learned by mcp:langgraph**. Say: *"An outside LangGraph agent chose the tools over MCP; the server re-checked its answer before storing it; my engine reused it."*
- **Learning from approval (15 s):** after approving A's rollback, pick `inc-1051 — Checkout failing again after pool change`. The similar-incidents card shows **🧠 learned by TraceLens** on inc-1042. Say: *"A week later the same signature; the fix it learned from my click is now history it can cite."*

## Handy URLs for recording

- Full replay of A: `http://localhost:5173/?incident=inc-1042`
- Jump to the verdict: `http://localhost:5173/?incident=inc-1042&instant=1`
- Hypotheses tab: `http://localhost:5173/?incident=inc-1042&instant=1&tab=hypotheses`
- Pause at a step: `http://localhost:5173/?incident=inc-1042&step=5`

## FalkorDB Browser queries worth showing

```cypher
// the dependency graph
MATCH (a:Service)-[d:DEPENDS_ON]->(b:Service) RETURN a, d, b

// what the agent learned
MATCH (inv:Investigation)-[:CONSIDERED]->(h:Hypothesis)-[r]->(n) RETURN inv, h, r, n

// why checkout's deploy was rejected
MATCH (h:Hypothesis)-[r:REJECTED_BY]->(n) WHERE h.label CONTAINS 'dep-chk-1405' RETURN h, r, n
```
