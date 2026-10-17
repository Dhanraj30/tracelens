# TraceLens — Architecture

```
┌──────────────────────────────┐   /api (?tenant=)   ┌──────────────────────────────────────────────────────┐
│  Browser (Vite + React + TS) │◄───────────────────►│  FastAPI  (backend/api/main.py)                      │
│  React Flow + dagre graph    │                     │   POST /api/investigations → Investigator            │
│  timeline · hypotheses ·     │                     │   POST /api/actions/{id}/approve → record_action      │
│  verdict · past investigations│                    │   GET  /api/investigations[/{id}] (graph-backed)     │
└──────────────────────────────┘                     │   GET  /api/graph /incidents /health /tools           │
                                                     └───────────────┬──────────────────────────────────────┘
┌──────────────────────────────┐   MCP (stdio)       ┌───────────────▼──────────────────────────────────────┐
│  Outside agents              │◄───────────────────►│  MCP server (backend/mcp_server.py)                  │
│  · mcp_client_demo (rules)   │                     │   same catalog, never raw Cypher                     │
│  · agents/langgraph_guest    │                     │   evidence tools take `reason` → "stated reason      │
│    (LangChain create_agent,  │                     │     (unverified)" in the session trace               │
│    Groq → Gemini fallback,   │                     │   record_investigation → verify_candidates():        │
│    ≤12 tool calls)           │                     │     re-runs the rule-based checks, stores suspects   │
└──────────────────────────────┘                     │     as supported ONLY if they pass; agent="mcp:…"    │
                                                     └───────────────┬──────────────────────────────────────┘
                                                                     │ Python calls
                                                     ┌───────────────▼──────────────────────────────────────┐
                                                     │  Investigator  (backend/agent/engine.py)             │
                                                     │  explicit state machine, ≤14 tool calls              │
                                                     │  RESOLVE→SCOPE→RECALL→GATHER→HYPOTHESIZE→FALSIFY     │
                                                     │  →DECIDE→ACT→REMEMBER   (verify_only = VERIFIED)     │
                                                     │  narrative: template, or LLM (Groq → Gemini →        │
                                                     │  template) gated by NarrativeValidator               │
                                                     └───────────────┬──────────────────────────────────────┘
                                                                     │ ctx.call("tool", **validated args)
                                                     ┌───────────────▼──────────────────────────────────────┐
                                                     │  Tool catalog (backend/tools/catalog.py)             │
                                                     │  11 tools · fixed Cypher · typed args ·              │
                                                     │  entity resolution first · full trace                │
                                                     │  writes: record_investigation, record_action         │
                                                     └───────────────┬──────────────────────────────────────┘
                                                                     │ GRAPH.RO_QUERY / GRAPH.QUERY
                                                     ┌───────────────▼──────────────────────────────────────┐
                                                     │  FalkorDB — one graph key per tenant                 │
                                                     │  `tracelens` (demo) · `tracelens:<tenant>` · tests   │
                                                     │  Cypher + algo.SPpaths / pageRank / betweenness      │
                                                     │  MEMORY lives here: Investigation / Hypothesis /     │
                                                     │  Action / Resolution{source:'tracelens'}             │
                                                     └──────────────────────────────────────────────────────┘
```

## Why these choices

- **FalkorDB does the reasoning.** Suspect space = `DEPENDS_ON*1..4` traversal; path check = `algo.SPpaths`; candidate weighting = `algo.pageRank` + `algo.betweenness`; history = traversal through shared `ErrorSignature` nodes (now including Resolutions TraceLens wrote itself); memory = traversal through `Investigation→Hypothesis→SUSPECTS→Deployment`. Remove FalkorDB and nothing works.
- **No LangGraph in the core.** The investigation is a plain Python state machine so a judge can read the whole control flow in one file and so the trace is a product feature. A LangGraph agent exists as an *outside* guest over MCP (`agents/langgraph_guest/`), which is where a framework agent belongs: it can choose tools, but it cannot change what gets stored.
- **The agent never writes Cypher.** Every query the agent reasons with is person-written and parameterized in `catalog.py`; the only other queries are fixed read-only lookups in `api/main.py` (graph/incident views) and `engine.py` (entity resolution, runbook lookup), none of which takes model text. Names are validated by regex, then resolved against the graph; the only inlined value is the hop depth, range-checked 1..4 (FalkorDB does not take `$param` as a hop bound). Read tools use `RO_QUERY`, which FalkorDB enforces as read-only. The same catalog is what MCP exposes; `record_action` and `investigate` are not exposed to outside agents.
- **Server-side verification of guests.** Any MCP client that calls `record_investigation` has its suspects re-checked by `verify_candidates()` (the engine with `verify_only=`): temporal precedence, dependency path, error origin, common cause, history. Wrong suspects are stored as `rejected` with the failing checks, and `recall` only ever reads `supported`. The guest's stated reasons are stored on the trace, labelled unverified.
- **Deterministic by default.** The LLM (optional, OpenAI-compatible, free tier, with a provider fallback chain) only writes the final narrative; the validator rejects anything naming an entity absent from the evidence, over-claiming ("proven"), or mislabelling an inconclusive outcome. On any failure it falls back to the template and says so in the UI.
- **Memory is the graph, and it learns from approvals.** `record_investigation` writes Investigation / Hypothesis (supported and rejected) / SUPPORTED_BY / REJECTED_BY / Action with provenance (`agent`). `record_action(executed)` writes a `Resolution {source:'tracelens'}` linked `REVERTED→Deployment` and `FOLLOWED→Runbook`, so `similar_incidents` finds TraceLens's own fixes a week later (Scenario D).
- **Tenants.** `?tenant=<slug>` → graph key `tracelens:<slug>`, auto-seeded; memory never crosses keys. Default behaviour unchanged.
- **Bounded.** Max 14 tool calls (engine), 12 tool / 16 model calls (guest), 5 s per query timeout, one pass; failures surface as `inconclusive` with the list of what was checked.

## Request lifecycle (Scenario A, engine)

1. `RESOLVE` — `MATCH (i:Incident {id:$id}) RETURN count(i)`; unknown → `unknown_entity`, 0 tool calls.
2. `SCOPE` — `get_incident`, `upstream_dependencies(checkout, 4)` → 9 services, all paths.
3. `RECALL` — `recall(path services, window, exclude_incident)` → empty on first run (any agent's prior supported hypothesis would be used here).
4. `GATHER` — `deployments_in_window` (3), `error_timeline` (origin = payments-api 13:58), `critical_services`.
5. `HYPOTHESIZE` — one hypothesis per deployment (ordered by PageRank of its service) + "shared dependency failure".
6. `FALSIFY` — hard checks: temporal precedence, path (`path_between` via SPpaths), origin. Soft checks on survivors: `healthy_siblings`, `similar_incidents`.
7. `DECIDE` — score = passed/total; any hard failure → rejected; exactly one supported → `root_cause_supported`.
8. `ACT` — propose rollback of the deployment + runbook from the matched past incident (simulated; needs a click).
9. `REMEMBER` — `record_investigation` (agent = engine).

## Guest lifecycle (LangGraph over MCP)

1. `MCPAdapter(server)` lists tools; `investigate`, `record_action`, `list_catalog` are removed from what the model sees.
2. `create_agent(model, tools, system_prompt, middleware=[ToolCallLimit(12), ModelCallLimit(16), ModelFallback(gemini)])`.
3. The model picks tools; each call's `reason` is recorded by the server as "model's stated reason (unverified)".
4. The model calls `record_investigation(suspects=[…])` → the server verifies → stores with `agent="mcp:langgraph"` and the session trace.
5. The dashboard engine's next `recall` may answer "learned by mcp:langgraph".

## Packaging

- `docker compose up` → FalkorDB + one app container (multi-stage Dockerfile: Node builds the frontend, Python serves API + static). The guest agent's dependencies are **not** in the image (`agents/langgraph_guest/requirements.txt` is separate).
- `AUTO_SEED=true` seeds the demo graph (and any tenant graph) when empty. `/api/health` reports node count and tenant.
- Secrets only via env; `.env.example` documents them; deterministic mode needs none.

## Out of scope

Auth, multi-tenancy beyond graph-key isolation, real observability ingestion, Slack/Kubernetes integrations, live multi-agent message passing.
