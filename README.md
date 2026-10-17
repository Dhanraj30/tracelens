# TraceLens

**Symptom → root cause, traced and checked in the FalkorDB graph.**

> *TraceLens doesn't just tell you what's broken — it walks the graph to prove why, shows you the suspects it ruled out, and remembers the answer next time.*

Built for **Graph Hacks: Context for AI Agents** (WeMakeDevs × FalkorDB), 17–18 Oct 2026.

**Tracks entered:** **Track 01 – Agents That Act on Connected Data** (primary) · **Track 02 – Agent Memory and Coordination** (Scenario B below).

**Demo video:** _link goes here_ · **Live demo:** _local setup below (live deployment optional)_

---

## The problem

When checkout breaks at 14:10, everyone looks at the deploy to checkout at 14:05. It is usually the wrong suspect. The real cause is often two hops upstream — a config change on a dependency whose errors began *before* the obvious deploy. Finding that takes an engineer twenty minutes of log-diving and tribal memory ("didn't we see this pool error three weeks ago?").

## What TraceLens does

| Reference demo ("Blast Radius") | TraceLens |
|---|---|
| Walks *downstream* impact | Walks *upstream* cause — from symptom to root cause |
| Returns facts | Generates **competing hypotheses and falsifies them** with graph checks |
| Lists recent changes | **Rejects the obvious suspect** with timestamped, traversable evidence |
| — | Finds past incidents by **shared graph structure** (same `ErrorSignature` / `Service` / `CodeChange` file), not text similarity |
| Saves findings | **Learns**: confirmed root causes are written back to the graph and shorten the next investigation |
| — | **Acts**: proposes a rollback tied to a specific `Deployment` + `Runbook`, executed (simulated) only after a human click |

Every conclusion comes from a Cypher query or a graph algorithm. The agent never writes Cypher. Remove FalkorDB and nothing works.

![Scenario A](docs/screenshots/scenario-a.png)

## 30-second quick start

```bash
git clone <this repo> && cd tracelens
docker compose up --build          # FalkorDB (6379, Browser on 3000) + TraceLens (8000)
# open http://localhost:8000  →  pick "inc-1042 — Checkout requests are failing"  →  Investigate
```

The app seeds the demo graph automatically when it is empty. No API key is needed (deterministic mode).

### Dev setup (hot reload)

```bash
docker compose up -d falkordb
python -m venv .venv && .venv/Scripts/activate      # or source .venv/bin/activate
pip install -r requirements.txt
python -m backend.seed                               # reset + seed the demo graph
uvicorn backend.api.main:app --reload --port 8000
cd frontend && npm install && npm run dev            # http://localhost:5173 (proxies /api)
```

### Tests (against a real FalkorDB container, separate graph key `tracelens_test`)

```bash
PYTHONIOENCODING=utf-8 python -m pytest -q           # 43 tests: one per tool, scenario ground truth A–D, validator, unknown-entity guards,
                                                     # MCP guards, cross-agent handoff, guest fake-model replay, tenant isolation
pip install -r agents/langgraph_guest/requirements.txt   # optional: the LangGraph guest (its tests skip if not installed)
```

## The three scenarios

| | Incident | What the agent must do | Outcome |
|---|---|---|---|
| **A** | `inc-1042` Checkout requests are failing | Reject the checkout deploy (errors began 14:01, deploy 14:05), reject the cart deploy (earliest errors sit at payments-api, which cart never reaches), support the payments-api deploy that cut the DB pool 50 → 5, exonerate the shared database via a healthy sibling, match incident `inc-0877` through a shared `ErrorSignature`, propose rollback + runbook | `root_cause_supported`, 5/5 checks, 11 tool calls |
| **B** | `inc-1043` Refunds failing (run after A) | `recall` the Investigation A wrote back, re-verify the timeline for refunds-worker, answer with "learned <date>" | `answered_from_memory`, 5 tool calls |
| **C** | `inc-1044` Search results are empty | No deployments in window; the path leaves the modeled graph at an external vendor. Say so, list what was checked and what data would resolve it | `inconclusive` |
| **D** | `inc-1051` Checkout failing again, a week later (run after approving A's rollback) | The payments-api deploy is a week old, so no recall; instead the history check finds the `Resolution` TraceLens itself wrote on approval, through the shared `ErrorSignature`, and reuses its runbook. The auth deploy is ruled out by origin | `root_cause_supported` with "learned by TraceLens" history |
| — | `inc-9999` / `warehouse-robot` | Name does not resolve → stop | `unknown_entity`, **0 tool calls** |

Cross-agent variants: run Scenario A from the MCP client (`python -m backend.mcp_client_demo inc-1042`) or the LangGraph guest (`RUN_LIVE_LLM=1 python -m agents.langgraph_guest.agent inc-1042`), then Scenario B in the dashboard answers "learned by mcp:client-demo / mcp:langgraph". The server re-verifies every suspect a guest proposes before storing it.

Ground truth lives only in `tests/scenarios.yaml`; the agent reaches it through queries.

## Graph data model

```
(:Service {name, tier, team, external})-[:DEPENDS_ON {kind: 'sync'|'async', weight: 1|2}]->(:Service)
(:Deployment {id, at, version, author})-[:DEPLOYED_TO]->(:Service)
(:Deployment)-[:INCLUDES]->(:CodeChange {id, summary, files, risk})
(:ErrorEvent {id, at, message, count})-[:EMITTED_BY]->(:Service)
(:ErrorEvent)-[:HAS_SIGNATURE]->(:ErrorSignature {key})          // shared node → similarity by traversal
(:Incident {id, title, status, opened_at, symptom})-[:AFFECTS]->(:Service)
(:Incident)-[:OBSERVED]->(:ErrorEvent)
(:Incident)-[:RESOLVED_BY]->(:Resolution {id, summary, at})-[:FOLLOWED]->(:Runbook {id, title, steps})
(:Resolution)-[:REVERTED]->(:Deployment)
(:Runbook)-[:APPLIES_TO]->(:Service)

// written by the agent itself (memory + audit trail)
(:Investigation {id, at, mode, outcome, tool_calls, confidence})-[:INVESTIGATED]->(:Incident)
(:Investigation)-[:CONSIDERED]->(:Hypothesis {id, status, score, kind, label, confirmed})
(:Hypothesis)-[:SUSPECTS]->(:Deployment|:CodeChange|:Service)
(:Hypothesis)-[:SUPPORTED_BY|REJECTED_BY {check}]->(:ErrorEvent|:Deployment|:Incident|:Service)
(:Investigation)-[:PROPOSED]->(:Action {id, kind, status, simulated})-[:TARGETS]->(:Deployment)
(:Action)-[:FOLLOWS]->(:Runbook)
```

Timestamps are integer epoch seconds. `weight` (sync 1 / async 2) is what `algo.SPpaths` minimizes. Full detail, topology and timeline: [`docs/GRAPH_MODEL.md`](docs/GRAPH_MODEL.md).

## Architecture

```
React + React Flow  ──/api──►  FastAPI  ──►  Investigator (bounded state machine)  ──►  Tool catalog (11 tools, fixed Cypher)  ──►  FalkorDB
                                                RESOLVE→SCOPE→RECALL→GATHER→HYPOTHESIZE→FALSIFY→DECIDE→ACT→REMEMBER
```

- **Backend:** Python 3.11+, FastAPI, official `falkordb` client. **No LangGraph** — the state machine is one readable file.
- **Tool catalog** (`backend/tools/catalog.py`): typed, validated arguments; every service/incident name resolved against the graph before any query; reads via `GRAPH.RO_QUERY`; every call traced (tool, args, exact Cypher, params, ms, rows). [`docs/QUERIES.md`](docs/QUERIES.md) is generated from it.
- **Algorithms:** `algo.SPpaths` (weighted path check), `algo.pageRank` + `algo.betweenness` (criticality weighting).
- **Frontend:** Vite + React + TypeScript + Tailwind, React Flow + dagre; the traversal animates step by step; "Show Cypher" on every step.
- **MCP server** (`backend/mcp_server.py`): exposes the same catalog (not raw Cypher) over stdio. Evidence tools take a `reason` that is recorded as "model's stated reason (unverified)"; `record_investigation` **re-verifies** proposed suspects with the engine's checks before storing them, tagged `agent="mcp:<name>"`.
- **LangGraph guest agent** (`agents/langgraph_guest/`, own requirements, never imported by the core): LangChain `create_agent` over `MCPAdapter`; the model (Groq `openai/gpt-oss-120b`, Gemini fallback) chooses the tools, capped at 12 calls; tests replay a recorded sequence through a fake model; live runs need `RUN_LIVE_LLM=1`. One live run chose 7 tools and reached the same verdict as deterministic mode ([log](docs/test-results/langgraph-guest-live.txt)).

More: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) · concepts used and not used (RAG, LangGraph, memory, MCP): [`docs/CONCEPTS.md`](docs/CONCEPTS.md).

## Agent workflow and scoring

Per candidate deployment: **temporal precedence** (hard), **dependency path** via SPpaths (hard), **error origin** (hard), **common cause** via healthy siblings (soft), **history** via shared ErrorSignature (soft). Any hard failure → *ruled out*. Score = passed / evaluated. Exactly one supported → `root_cause_supported`.

**Confidence means "share of checks passed" — not a probability.** We never say "proven cause"; we say *best-supported explanation* and label items *supported / ruled out / inconclusive*. Rejected hypotheses are kept, with the evidence that rejected them.

## Evals

`python -m evals.run_evals` scores the LangGraph guest (LLM tool selection over MCP) and the deterministic engine on five cases (A red herring, B after A, B alone, C incomplete evidence, unknown id), each in a freshly seeded, isolated tenant graph; only the scorer reads the ground truth. **Live results (17 Oct 2026, Groq `openai/gpt-oss-120b`, one run): LLM agent 3/5 verified correct, deterministic engine 5/5** — the two guest failures were a tokens-per-minute 429 and a mid-run Gemini fallback that Gemini rejects, both since fixed (restart-from-scratch fallback, native Gemini); full tables, disclosure of a scorer change, and failures in [`docs/EVALS.md`](docs/EVALS.md). The offline replay mode (`docs/EVALS-offline.md`, runs in pytest) tests the plumbing — MCP server, validation, server verifier — not the LLM.

## Memory (Track 02), in the judges' vocabulary

- **Episodic:** `Investigation → Hypothesis → SUPPORTED_BY / REJECTED_BY` with time and provenance (`agent = engine | mcp:<name>`). Read by `recall`; shown in the "Past investigations" tab.
- **Semantic:** the entity graph, plus the `Resolution` TraceLens writes when an operator approves an action (`RESOLVED_BY / REVERTED / FOLLOWED`). Found later by `similar_incidents` through the shared `ErrorSignature` (Scenario D, a week later).
- **Procedural:** `Runbook` steps, reused through `Action -[:FOLLOWS]-> Runbook` and carried into new Resolutions.
- **Cross-agent handoff:** an MCP client or the LangGraph guest writes an investigation; the server re-verifies its suspects with the graph checks before storing them; the dashboard engine then answers Scenario B "learned by mcp:langgraph".
- **Per-tenant memory:** `?tenant=<slug>` selects graph key `tracelens:<slug>` (auto-seeded); memory written in one tenant is invisible in another.
- **Honest limits:** no conversational memory, no inferred facts beyond approvals, no high-concurrency demo. Details in [`docs/CONCEPTS.md`](docs/CONCEPTS.md).

## Deterministic vs LLM mode

- **Deterministic (default, what the demo and tests use):** no key needed. Narrative is a template built from the returned rows.
- **LLM (optional toggle):** any OpenAI-compatible endpoint, free tier only (Groq `openai/gpt-oss-120b` by default, with Gemini `gemini-3.5-flash-lite` as an automatic fallback provider; Ollama works too, see `.env.example`). The LLM only writes the narrative. Order on failure: primary provider → fallback provider → deterministic template, every hop shown in the UI. A validator rejects any narrative naming an entity not in the evidence, saying "proven", or claiming impact when traversals were empty; on rejection, timeout, 429 or missing key it falls back to the template and the UI shows "LLM unavailable → deterministic". The LLM never writes Cypher.

## API

`GET /api/health` · `GET /api/incidents` · `GET /api/graph` · `POST /api/investigations {incident_id | service, mode}` · `GET /api/investigations[/{id}]` (every agent's investigations, graph-backed) · `POST /api/actions/{id}/approve` (simulated; writes a learned Resolution) · `POST /api/demo/reset[?memory_only=true]` · `GET /api/tools`. Every endpoint accepts `?tenant=<slug>`.

## Limitations (honest list)

- Demo data is synthetic and deterministic (14 services); there is no real log/deploy ingestion.
- The remediation is **simulated**: an `Action` node is written, nothing is rolled back.
- The investigation window is a fixed 60 minutes before `opened_at`.
- In the core engine the LLM writes the narrative only; tool order is fixed by the state machine. LLM tool selection exists in the LangGraph guest over MCP, and its conclusions are only stored after server-side verification.
- Full reports (with Cypher text) are held in process memory; the durable record in the graph keeps a compact trace per step.
- Tenant isolation is one graph key per tenant with no authentication; it is a demonstration of FalkorDB multi-graph, not an access-control system.
- The LangGraph guest is slow on free tiers (about two minutes for a run) and is not part of the Docker image.
- FalkorDB does not accept a parameter as a variable-length hop bound, so the depth is validated (1..4) and inlined.

## AI-assistance disclosure

This project was built during the event with **Claude Code (Anthropic, model Claude Fable 5.1)** as a pair programmer. The AI wrote most of the first drafts of the code, queries, tests and docs under the author's direction; the author defined the product, the graph model, the scenarios and the check/scoring rules, reviewed every query against FalkorDB, ran the tests and the demo, and can explain every design decision (see [`docs/EXPLAIN_IT.md`](docs/EXPLAIN_IT.md)). No code was copied from prior projects. FalkorDB behaviour (algorithm signatures, parameter limits) was verified empirically against v4.22.1 during the build.

## Repository map

```
backend/            db.py · seed.py · config.py · tools/ (catalog) · agent/ (engine, narrative, llm) · api/ · mcp_server.py · mcp_client_demo.py
agents/langgraph_guest/   outside LangGraph agent over MCP (own requirements, fake-model tests, live recordings)
frontend/           Vite + React + TS dashboard
tests/              scenarios.yaml (ground truth) · test_tools · test_scenarios · test_validator · test_mcp · test_handoff · test_tenant
docs/               CONCEPTS · GRAPH_MODEL · ARCHITECTURE · QUERIES (generated) · DEMO_SCRIPT · EXPLAIN_IT · BLOG_DRAFT
docker-compose.yml  FalkorDB (pinned digest) + app · Dockerfile (multi-stage)
```
