# CONCEPTS — what TraceLens uses, what it deliberately does not, and why

Plain-language notes on the AI/agent ideas behind TraceLens. Each section says: *what the concept is*, *whether we use it*, *where it lives in the code*, and *why we chose that*. Read this with `docs/EXPLAIN_IT.md` (the judge Q&A) and `docs/ARCHITECTURE.md`.

---

## 1. Agent

**What it is.** A program that pursues a goal by repeatedly choosing an action (usually a tool call), observing the result, and deciding what to do next, instead of running one fixed query.

**Do we use it?** Yes. The `Investigator` in `backend/agent/engine.py` is the agent. Its goal is "explain this incident". Its actions are calls into the tool catalog. It observes rows and paths coming back from FalkorDB, forms hypotheses, tests them, decides, acts, and remembers.

**Why this shape.** Judges need to see *why* a conclusion was reached. So the agent is a **bounded, explicit state machine** with nine named states (RESOLVE → SCOPE → RECALL → GATHER → HYPOTHESIZE → FALSIFY → DECIDE → ACT → REMEMBER), a hard cap of 14 tool calls, and a trace of every call. Nothing is hidden inside a framework.

---

## 2. Tool calling / tool catalog

**What it is.** Giving an agent a fixed set of typed functions ("tools") it may invoke, instead of letting it do anything. In LLM products this is "function calling".

**Do we use it?** Yes, and it is the backbone. `backend/tools/catalog.py` defines 11 tools (`get_incident`, `upstream_dependencies`, `deployments_in_window`, `error_timeline`, `healthy_siblings`, `similar_incidents`, `path_between`, `critical_services`, `recall`, `record_investigation`, `record_action`). Each tool owns one person-written Cypher query. Arguments are typed and validated; service and incident names are resolved against the graph before any query runs; reads go through `GRAPH.RO_QUERY`.

**Why.** The agent never writes Cypher. That is the safety and explainability guarantee: there is no way for an LLM to inject a query, and every query in `docs/QUERIES.md` is exactly what ran. The same catalog is exposed over MCP (section 8), so the *same* guarantees hold for any outside agent.

---

## 3. RAG (Retrieval-Augmented Generation)

**What it is.** Before an LLM answers, retrieve relevant documents (classically by embedding text into vectors and finding nearest neighbours) and put them in the prompt so the answer is grounded.

**Do we use it?** We use the *idea* (ground the answer in retrieved evidence) but **not vector/text RAG**. TraceLens does **graph retrieval**: the evidence is retrieved by traversals (`DEPENDS_ON*1..4`, shared `ErrorSignature` nodes, `algo.SPpaths`) and only then handed to the narrative step. This is sometimes called **GraphRAG**, but with a twist: we don't just retrieve, we *reason* in the graph (hypothesis checks are graph queries), and the LLM is optional.

**Why not vectors.** "Similar incident" by text similarity would match on wording. Matching through a shared `ErrorSignature` node on the same `Service`, resolved by a rollback that touched the same `CodeChange` file, is structural and exact. It is also explainable: you can click the path. There is no embedding model, no vector index, and nothing to go stale.

**Where.** `similar_incidents` and `recall` in `catalog.py`; the evidence object built in `engine.py::_narrate`.

---

## 4. LangChain / LangGraph

**What they are.** Popular Python frameworks. LangChain wraps LLM calls, prompts and tools; LangGraph lets you define an agent as a graph of nodes with state passing between them.

**Do we use them?** **Not in the core.** The investigation engine is ~500 lines of plain Python with explicit states and a `Check` / `Hypothesis` dataclass model, and `backend/` never imports LangChain or LangGraph.

**But there is a LangGraph agent in the repo**: `agents/langgraph_guest/`. It is an *outside* agent built with LangChain's `create_agent` (LangGraph runtime) that connects to TraceLens **only through MCP**. It sees the evidence tools plus `record_investigation`, never `investigate` / `record_action` / `list_catalog`, and never any Cypher. The model (Groq `openai/gpt-oss-120b`, Gemini fallback) chooses which tools to call and in what order; every call carries its stated reason, recorded as "model's stated reason (unverified)". When it calls `record_investigation`, the **server re-runs the rule-based checks** on the suspects it proposes and stores a hypothesis as `supported` only if the checks pass; a wrong suspect is stored as `rejected` with the failing checks. Its trace lands on the Investigation node with provenance `agent = "mcp:langgraph"`, so the dashboard's "Past investigations" tab shows it, and a later engine run can answer from its memory ("learned by mcp:langgraph").

Live run (17 Oct 2026, gpt-oss-120b): it picked 7 tools — get_incident, upstream_dependencies, recall, deployments_in_window, error_timeline, healthy_siblings, record_investigation — skipped path_between and similar_incidents, proposed `dep-pay-1352`, and the server verified it 5/5. Same verdict as deterministic mode. (Log: `docs/test-results/langgraph-guest-live.txt`; recording: `agents/langgraph_guest/recordings/`.)

**Why this split.**
- The core stays explainable: a judge asking "what happens after the temporal check fails?" gets an answer by pointing at one `if`, and the trace is a product feature rather than a debugging artifact.
- The guest demonstrates the thing Track 01 asks for, *tool selection through MCP*, with a real framework and a real LLM, while the **verdict never depends on the LLM**: the graph checks decide what gets stored.
- Quota is guarded: tests replay a recorded tool-call sequence through a fake chat model (no network); a live run requires `RUN_LIVE_LLM=1`; hard caps of 12 tool calls and 16 model calls.

If someone asks "isn't the engine just LangGraph by hand?": yes, intentionally, a nine-node graph with fixed edges is simpler to read as a function than as a framework object. And when a framework agent *is* useful, it plugs in from the outside through MCP without changing the core.

---

## 5. Memory (agent memory, Track 02)

**What it is.** Letting an agent keep information across runs so later tasks are faster or better. In chatbots this is "long-term memory"; in agent frameworks it is often a vector store of past interactions.

**Do we use it?** Yes, and **the memory is the graph itself**, not a separate store.
- After every investigation, `record_investigation` writes `Investigation`, `Hypothesis` (supported **and** rejected), `SUPPORTED_BY` / `REJECTED_BY` edges to the actual evidence nodes, and the proposed `Action`.
- The next investigation starts with `recall`: a traversal `Investigation → Hypothesis{status:'supported'} → SUSPECTS → Deployment → DEPLOYED_TO → Service` restricted to the new incident's dependency path and time window, excluding the incident being investigated.
- Approving the simulated remediation sets `Hypothesis.confirmed = true`, so memory can distinguish "the agent thought so" from "an operator confirmed it".

**Why this design.**
- It is queryable and auditable in FalkorDB Browser. A vector memory would give you "something similar happened"; the graph gives you "this deployment, on this service, confirmed by a human, with these rejected alternatives".
- Scenario B proves it: refunds fail 20 minutes after checkout; `recall` finds Scenario A's write-back, re-verifies the timeline for refunds-worker, and answers in 5 tool calls instead of 11. Reset memory and it goes back to the long way (there is a test for this).
- Keeping **rejected** hypotheses is deliberate: the next investigator (human or agent) can see what was already excluded and why.

**What it is not.** It is not conversational memory and not a cache of LLM text. No LLM output is ever written to the graph.

---

## 6. Context (what the model, or the deterministic engine, gets to see)

**What it is.** The information available at decision time. For an LLM that is the prompt; for our engine it is the rows returned so far.

**How we manage it.**
- The engine never loads the whole graph. Each state asks exactly one question (`upstream_dependencies` defines the suspect space; `deployments_in_window` and `error_timeline` are scoped to those services and a 60-minute window).
- Soft checks (`healthy_siblings`, `similar_incidents`) run only for hypotheses that survived the hard checks, which keeps the call budget and the evidence small.
- When the LLM is on, it receives one JSON object (`incident`, `outcome`, `root_cause`, `hypotheses`, `similar_incidents`, `action`, `memory`, `missing_data`, `confidence`), truncated to 12k characters. That is the only context it gets; it cannot call tools or see the graph.

**Why.** Small, structured context is cheaper, faster, and far easier to validate (section 7).

---

## 7. LLM usage, grounding and the validator

**What it is.** Using a large language model for the parts it is good at (fluent explanation) while preventing hallucination.

**Do we use it?** Optionally, and only for the final narrative. Default mode is **deterministic**: a template fills the narrative from rows. **LLM mode** sends the evidence JSON to any OpenAI-compatible endpoint (Groq `openai/gpt-oss-120b` primary, Gemini `gemini-3.5-flash-lite` fallback, then the template).

**Guardrails (`backend/agent/narrative.py::NarrativeValidator`).** The narrative is rejected and replaced by the template if it:
- names any ID (`dep-…`, `inc-…`, `cc-…`, `err-…`, `rb-…`) that is not in the evidence,
- names any service that is not in the evidence,
- says "proven", "proves", "definitively caused",
- claims impact when the dependency traversal returned nothing,
- is for an *inconclusive* outcome but never says "inconclusive" or calls something "supported".

Every fallback hop is recorded in the report and shown in the UI ("LLM provider failed → tried next provider", "LLM unavailable → deterministic").

**Why this split.** The evidence, hypotheses, scores and outcome are identical in both modes, so tests and the demo do not depend on an external API. The LLM adds readability without being able to change the answer.

**What the LLM does not do (in this build).** It does not choose tools and does not write queries. Tool order is fixed by the state machine. The catalog and validation layer were built so that an LLM tool-chooser could be added later (the MCP demo shows a client choosing tools), but we cut it to stay explainable and within quota.

---

## 8. MCP (Model Context Protocol)

**What it is.** An open protocol for exposing tools, resources and prompts to AI clients (Claude Desktop, Cursor, custom agents) over stdio or HTTP.

**Do we use it?** Yes. `backend/mcp_server.py` exposes the same 11 catalog tools plus `investigate` and `list_catalog`, using the official MCP Python SDK. `backend/mcp_client_demo.py` is a client that launches the server as a subprocess, lists the tools, and **selects** them step by step to solve Scenario A (10 calls), stopping after one call for an unknown incident.

**Why.** Track 01 explicitly values "tool selection through MCP". Exposing the catalog, not raw Cypher, means an outside agent inherits the same validation, resolution and tracing.

---

## 9. Hypothesis-driven reasoning / falsification

**What it is.** Instead of asking "what is the cause?", generate candidate explanations and try to *disprove* each with evidence; keep the survivor.

**Do we use it?** Yes, it is the core idea. One hypothesis per deployment on the dependency path, plus "shared dependency failure". Five checks per hypothesis: temporal precedence, dependency path, error origin (hard: one failure rules it out), common cause, history (soft: count toward confidence). Score = passed / evaluated. Exactly one survivor → `root_cause_supported`.

**Why.** It is how a good on-call engineer thinks, it produces the "ruled out" list judges have not seen in the reference demo, and every check is a graph query, so FalkorDB is doing the reasoning, not an LLM.

---

## 10. Graph algorithms

**What they are.** Procedures that compute over the whole graph structure rather than matching a pattern.

**Which we use and for what.**
- `algo.SPpaths` (weighted shortest path, sync=1 / async=2): the *path check*: is the candidate actually upstream of the symptom, and how directly?
- `algo.pageRank` over `DEPENDS_ON`: which services most things depend on; candidates on load-bearing services are examined first and annotated.
- `algo.betweenness`: which services sit on the most dependency paths (shown alongside PageRank).

**Why.** They turn "connected?" and "important?" into numbers the engine can order by, and they are things a relational database cannot do in one call.

---

## 11. Deterministic seed and ground truth

**What it is.** Fixed demo data with known answers, so behaviour is testable.

**How.** `backend/seed.py` builds the same graph every time (fixed IDs, fixed timestamps relative to one base day, idempotent via `MERGE`). The expected outcomes live **only** in `tests/scenarios.yaml`; the agent never reads them. `pytest` runs against a real FalkorDB container (29 tests).

**Why.** "Do not fake queries, invent evidence, or hardcode root causes" is a rule of the brief and of good engineering. If the engine reaches the ground truth it must have done so through queries.

---

## 12. Human-in-the-loop action

**What it is.** The agent proposes an action; a person approves; only then is it executed.

**How.** `ACT` proposes a rollback tied to a specific `Deployment` and `Runbook`. The dashboard has an **Approve rollback (simulated)** button. Clicking it calls `record_action` twice (approved → executed), writes the `Action` node and marks the hypothesis confirmed. **Nothing real is rolled back.**

**Why.** An incident agent that acts unilaterally is a liability; one that writes a proposal to the graph and waits for a click is a teammate. "Simulated" is stated everywhere so no one mistakes the demo for production automation.

---

## Quick reference

| Concept | Used? | Where | One-line why |
|---|---|---|---|
| Agent | Yes | `agent/engine.py` | bounded, explicit state machine, trace is the product |
| Tool calling | Yes | `tools/catalog.py` | agent never writes Cypher |
| RAG (vector) | No | — | structure beats text similarity; nothing to embed or go stale |
| Graph retrieval (GraphRAG-style) | Yes | `similar_incidents`, `recall`, traversals | evidence is paths and nodes you can click |
| LangChain / LangGraph | No | — | readability and explainability for judges; nothing needed dynamic orchestration |
| Memory | Yes | `record_investigation`, `recall` | memory is graph nodes the agent wrote; Scenario B |
| Context management | Yes | scoped queries, evidence JSON ≤ 12k chars | small, structured, validatable |
| LLM | Optional | `agent/llm.py`, `narrative.py` | narrative only, validated, provider fallback chain, template fallback |
| MCP | Yes | `mcp_server.py`, `mcp_client_demo.py` | same catalog exposed to outside agents |
| Hypothesis falsification | Yes | `engine.py` FALSIFY/DECIDE | the "ruled out" list with evidence |
| Graph algorithms | Yes | `path_between`, `critical_services` | SPpaths, pageRank, betweenness |
| Deterministic seed + ground truth | Yes | `seed.py`, `tests/scenarios.yaml` | provable, testable, no hardcoding |
| Human-in-the-loop | Yes | `/api/actions/{id}/approve` | simulated execution after a click |
