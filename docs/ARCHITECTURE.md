# TraceLens — Architecture

```
┌──────────────────────────────┐        ┌────────────────────────────────────────────┐
│  Browser (Vite + React + TS) │  /api  │  FastAPI  (backend/api/main.py)            │
│  React Flow + dagre graph    │◄──────►│   POST /api/investigations → Investigator  │
│  timeline · hypotheses ·     │        │   POST /api/actions/{id}/approve           │
│  verdict · approve button    │        │   GET  /api/graph /incidents /health /tools │
└──────────────────────────────┘        └───────────────┬────────────────────────────┘
                                                        │ Python calls
                                        ┌───────────────▼────────────────────────────┐
                                        │  Investigator  (backend/agent/engine.py)   │
                                        │  explicit state machine, ≤14 tool calls    │
                                        │  RESOLVE→SCOPE→RECALL→GATHER→HYPOTHESIZE   │
                                        │  →FALSIFY→DECIDE→ACT→REMEMBER              │
                                        └───────────────┬────────────────────────────┘
                                                        │ ctx.call("tool", **validated args)
                                        ┌───────────────▼────────────────────────────┐
                                        │  Tool catalog (backend/tools/catalog.py)   │
                                        │  11 tools · fixed Cypher · typed args ·    │
                                        │  entity resolution first · full trace      │
                                        └───────────────┬────────────────────────────┘
                                                        │ GRAPH.RO_QUERY / GRAPH.QUERY
                                        ┌───────────────▼────────────────────────────┐
                                        │  FalkorDB  (docker, graph `tracelens`)     │
                                        │  Cypher + algo.SPpaths/pageRank/betweenness │
                                        └────────────────────────────────────────────┘
```

## Why these choices

- **FalkorDB does the reasoning.** Suspect space = `DEPENDS_ON*1..4` traversal; path check = `algo.SPpaths`; candidate weighting = `algo.pageRank` + `algo.betweenness`; history = traversal through shared `ErrorSignature` nodes; memory = traversal through `Investigation→Hypothesis→SUSPECTS→Deployment`. Remove FalkorDB and nothing works.
- **No LangGraph.** The investigation is a plain Python state machine so a judge can read the whole control flow in one file and so the trace is a product feature.
- **The agent never writes Cypher.** Every query is person-written and parameterized in `catalog.py`. Names are validated by regex, then resolved against the graph; integers are range-checked before being inlined (FalkorDB does not take `$param` as a hop bound). Read tools use `RO_QUERY`, which FalkorDB enforces as read-only.
- **Deterministic by default.** The LLM (optional, OpenAI-compatible, free tier) only writes the final narrative; a validator rejects anything that names an entity absent from the evidence or over-claims ("proven"). On any failure it falls back to the template and says so in the UI.
- **Memory is the graph.** `record_investigation` writes Investigation / Hypothesis (supported and rejected) / SUPPORTED_BY / REJECTED_BY / Action. `recall` reads exactly these nodes. Scenario B therefore depends on Scenario A having actually run — not on seed data.
- **Bounded.** Max 14 tool calls, 5 s per query timeout, one pass; failures surface as `inconclusive` with the list of what was checked.

## Request lifecycle (Scenario A)

1. `RESOLVE` — `MATCH (i:Incident {id:$id}) RETURN count(i)`; unknown → `unknown_entity`, 0 tool calls.
2. `SCOPE` — `get_incident`, `upstream_dependencies(checkout, 4)` → 9 services, all paths.
3. `RECALL` — `recall(path services, window)` → empty on first run.
4. `GATHER` — `deployments_in_window` (3), `error_timeline` (origin = payments-api 13:58), `critical_services`.
5. `HYPOTHESIZE` — one hypothesis per deployment (ordered by PageRank of its service) + "shared dependency failure".
6. `FALSIFY` — hard checks: temporal precedence, path (`path_between` via SPpaths), origin. Soft checks on survivors: `healthy_siblings`, `similar_incidents`.
7. `DECIDE` — score = passed/total; any hard failure → rejected; exactly one supported → `root_cause_supported`.
8. `ACT` — propose rollback of the deployment + runbook from the matched past incident (simulated; needs a click).
9. `REMEMBER` — `record_investigation` (the only write).

## Packaging

- `docker compose up` → FalkorDB + one app container (multi-stage Dockerfile: Node builds the frontend, Python serves API + static).
- `AUTO_SEED=true` seeds the demo graph when empty (needed for FalkorDB Cloud Free, which can reset). `/api/health` reports node count.
- Secrets only via env; `.env.example` documents them; deterministic mode needs none.

## Out of scope

Auth, multi-tenancy, real observability ingestion, Slack/Kubernetes integrations, multi-agent orchestration.
