# EXPLAIN IT — notes for talking to judges

Plain-language answers to "why did you do it this way?". For the concepts themselves (agent, RAG, LangGraph, memory, MCP) see `docs/CONCEPTS.md`. Every query named here is in `docs/QUERIES.md`
(generated from `backend/tools/catalog.py`).

## The one-sentence pitch

TraceLens doesn't just tell you what's broken — it walks the graph to prove *why*, shows you the suspects it ruled out, and remembers the answer next time.

## How it is different from the Blast Radius reference demo

Blast Radius goes **downstream**: "X is down, what else breaks?" TraceLens goes **upstream**: "X is failing — which change upstream explains it?" It generates competing hypotheses and tries to *falsify* each with graph checks, keeps the rejected ones with evidence, finds history by shared graph structure, and writes its conclusion back so the next investigation is shorter.

## Where FalkorDB does the work (if you remove it, nothing works)

| Question | Graph mechanism |
|---|---|
| What could possibly be the cause? | `MATCH p=(s:Service {name:$name})-[:DEPENDS_ON*1..4]->(d)` — the suspect space *is* the traversal |
| Is this candidate actually connected to the symptom? | `CALL algo.SPpaths({... weightProp:'weight', relDirection:'outgoing'})` — weighted shortest path (sync edges cost 1, async 2) |
| Which candidate is most load-bearing? | `CALL algo.pageRank('Service','DEPENDS_ON')` and `algo.betweenness` — order of examination + annotation |
| Is the shared database the real culprit? | `(s)-[:DEPENDS_ON]->(dep)<-[:DEPENDS_ON]-(sib)` + errors on `sib` — the common-cause test |
| Have we seen this before? | `(past:Incident)-[:OBSERVED]->(:ErrorEvent)-[:HAS_SIGNATURE]->(sig)` where `sig.key IN $signatures` — structure, not text |
| Do we already know the answer? | `(inv:Investigation)-[:CONSIDERED]->(h {status:'supported'})-[:SUSPECTS]->(d:Deployment)-[:DEPLOYED_TO]->(s)` — memory is a traversal through nodes the agent wrote |

## The five checks (and how to explain each in one breath)

1. **Temporal precedence (hard).** "If the symptom started before the deploy, the deploy can't have caused it." Checkout errors 14:01 < checkout deploy 14:05 → ruled out.
2. **Dependency path (hard).** "The symptom service has to reach the candidate through DEPENDS_ON edges, or it can't be upstream of it." Done with `algo.SPpaths`.
3. **Error origin (hard).** "The earliest error in the window tells you where the failure started. The candidate must sit at or above that point, and must have deployed before it." Earliest error = payments-api 13:58; cart never reaches payments-api → cart ruled out.
4. **Common cause (soft).** "If other services that share the candidate's dependency are healthy, the shared dependency is not the problem." fraud-check uses the same Postgres and is fine → Postgres exonerated. Siblings that themselves depend on the candidate (refunds-worker → payments-api) are excluded — their errors are expected.
5. **History (soft).** "A resolved incident with the same error signature on the same service, fixed by a rollback that touched the same file, is strong evidence for the same fix."

**Hard** = a single failure rules the hypothesis out. **Soft** = counts toward confidence.

## Scoring (deterministic, explainable)

- score = passed / evaluated checks. Checks that could not be evaluated (`?`) don't count either way.
- Any hard failure → **ruled out**. No hard failure, ≥3 checks evaluated and score ≥ 0.6 → **supported**. Else **inconclusive**.
- Exactly one supported → `root_cause_supported`; more than one → `multiple_candidates`; none → `inconclusive`.
- **Confidence is the share of checks passed. It is not a probability.** The UI says so next to the number.
- We never say "proven". We say "best-supported explanation", and label items *supported / ruled out / inconclusive*.

## Why the agent never writes Cypher

- Every query is person-written and parameterized. The LLM (if enabled) can only pick tools and write the final narrative.
- Names are regex-validated (`^[a-z0-9][a-z0-9\-_.]{0,63}$`) and then **resolved against the graph**; unresolved → the step stops, no query runs (the unknown-entity test proves it: 0 tool calls).
- The one inlined value is the hop bound, an integer range-checked to 1..4, because FalkorDB rejects `$param` as a variable-length bound (we verified this on v4.22.1).
- Reads go through `GRAPH.RO_QUERY`, which FalkorDB enforces as read-only — a second safety net.

## Why memory is real (Track 02)

- Scenario B only answers from memory **after** Scenario A has run in this graph. Reset memory and B becomes a full investigation (there is a test for this).
- `record_investigation` writes Investigation → Hypothesis (supported *and* rejected) → SUPPORTED_BY / REJECTED_BY edges to the actual ErrorEvent / Deployment / Incident / Service nodes. That is an audit trail you can query in FalkorDB Browser.
- Approving the simulated action sets `Hypothesis.confirmed = true`; later recalls show "operator confirmed".
- Memory transfers across incidents only: `recall` excludes the incident being investigated, so re-running A always re-derives.

## Why no LangGraph

Nine explicit states in one file (`engine.py`), a hard cap of 14 tool calls, and a trace that is the product. A judge can read the control flow top to bottom. LangGraph would hide the logic and add learning cost during a one-weekend build.

## LLM mode honestly described

Optional, OpenAI-compatible, free tier (Groq by default). It only writes the narrative. A validator rejects any narrative that names an ID or service not present in the evidence, says "proven", or claims impact when traversals were empty; on rejection or any error (no key, timeout, 429) the deterministic template is used and the UI shows "LLM unavailable → deterministic". The deployed demo defaults to deterministic so judges can't exhaust the quota. The LLM does **not** choose tools in this build — tool order is fixed by the state machine (a deliberate scope cut; the catalog + validation layer is built so that it could).

## Design decisions you may be asked about

- **Integer epoch seconds** for every timestamp: trivial comparisons, no temporal-type surprises across clients.
- **Shared `ErrorSignature` node** instead of a string property: "similar incidents" becomes a 2-hop traversal.
- **`weight` on `DEPENDS_ON`** (sync 1 / async 2): gives `algo.SPpaths` something to minimize, preferring synchronous call chains, which propagate failures immediately.
- **Window = 60 minutes before `opened_at`.** Simple, explainable; "widen the window" is one of the missing-data suggestions when nothing is found.
- **Rejected hypotheses are kept** in the graph on purpose. The next person (or agent) can see what was already excluded and why.
- **Deterministic seed with fixed IDs/timestamps**, idempotent via MERGE. The ground truth lives only in `tests/scenarios.yaml`.
- **Simulated execution** of the remediation: a hackathon demo must not pretend to roll back production. The Action node is real; the rollback is not.

## The Saturday-evening additions (steps 1–5) and how to explain them

### Cross-agent handoff with provenance
- Every `Investigation` carries `agent` (`engine`, `mcp:client-demo`, `mcp:langgraph`), `verified`, and a compact `trace`.
- Memory is shared through the graph, not through messages: an MCP client writes, the dashboard engine recalls. The badge reads "learned by mcp:langgraph at <time>".
- **Server-side verification:** when any MCP client calls `record_investigation` with suspects, the server runs `verify_candidates()`, which is the engine with `verify_only=[...]`: same temporal / path / origin / common-cause / history checks, nothing written until they are done. A suspect that fails is stored as `rejected` with the failing checks, and `recall` reads only `supported`. A guest cannot poison memory.
- A guest's `reason` strings are stored on the trace labelled "model's stated reason (unverified)". They are explanation, never evidence.

### LangGraph guest (LLM tool selection over MCP)
- Lives in `agents/langgraph_guest/`; the core never imports LangChain. It uses `langchain.agents.create_agent` (LangGraph runtime) and `langchain.mcp.MCPAdapter`.
- It sees 10 tools (evidence + `record_investigation`), never `investigate`, `record_action`, `list_catalog`, never Cypher.
- Caps: 12 tool calls, 16 model calls (middleware), Groq → Gemini fallback (middleware).
- Live run on 17 Oct: gpt-oss-120b chose get_incident → upstream_dependencies → recall → deployments_in_window → error_timeline → healthy_siblings → record_investigation; it skipped path_between and similar_incidents. The server still ran all five checks and verified `dep-pay-1352` 5/5. **The LLM chose the path; the graph decided the verdict.**
- Tests replay that sequence through a fake chat model (no network); a second test has a naive guest blame `dep-chk-1405` and proves it is stored as rejected.

### Learning from approvals (Scenario D)
- Approving the simulated rollback does three things: `Action.status=executed`, `Hypothesis.confirmed=true`, and a new `Resolution {source:'tracelens'}` with `RESOLVED_BY` from the incident, `REVERTED` to the deployment, `FOLLOWED` to the runbook; the incident becomes `resolved`.
- That is exactly the shape `similar_incidents` traverses, so a week later (`inc-1051`) the history check cites `inc-1042` with "learned by TraceLens", and the runbook flows into the new proposal.
- Why not recall? `dep-pay-1352` is outside D's 60-minute window. Recall is episodic ("we saw this deploy"); the learned Resolution is semantic ("this signature on this service was fixed by rolling back a pool change"). Be ready to say that distinction out loud.
- The history check matches a past incident to a candidate if the past incident affected the same service **or** its resolution reverted a deployment to that service; strongest file overlap wins.

### Three kinds of memory
- Episodic = Investigation/Hypothesis with time and agent. Semantic = entity graph + learned Resolutions. Procedural = Runbooks reused through Action → Resolution. Say plainly what is thin: no conversational memory, no inferred facts beyond approvals, no high-concurrency demo.

### Tenants
- `?tenant=acme-eu` → graph key `tracelens:acme-eu`, slug-validated, auto-seeded. Memory written in one key is invisible in another (test). It is FalkorDB multi-graph, not auth.

### Evals (`evals/run_evals.py`, report in `docs/EVALS.md`)
- Five cases × two modes (guest, baseline), each run in its own freshly seeded tenant graph that is deleted afterwards. "B after A" is the only case that intentionally reuses a graph.
- "Verified correct" for the guest means the **server's** re-verification of what the guest proposed matches the truth; the guest never sees the truth. For A it also requires that the checkout deploy is not supported, and the report says whether the guest never proposed it or proposed it and got rejected.
- Offline mode = replay through a fake model (no network): it proves the harness, the MCP server, validation and verification, not the LLM. Live mode = real models under a request budget and spacing. Be ready to say which numbers come from which mode.
- The honest sentence: the deterministic engine is the product; the guest is the demonstration that an LLM can choose tools over MCP without being able to corrupt the verdict. The eval tables show both.
- Live run 1 result and what it taught: guest 3/5 (re-scored from 2/5, disclosed in EVALS.md), baseline 5/5. Failures were Groq's 8k tokens-per-minute limit and a mid-run fallback to Gemini that Gemini 3.x rejects (thought signatures). Fixes: restart-from-scratch fallback, native Gemini integration, wider spacing. Say this plainly if asked "did the LLM agent always get it right?" — no, and here is why, and the verdict was never corrupted because the server verifies.

## What you must be able to explain (checklist)
- [ ] How a guest agent's conclusion gets into memory, and why a wrong one cannot.
- [ ] Which 7 tools the LangGraph guest chose live, which 2 it skipped, and why the verdict still matched.
- [ ] What approving an action writes to the graph, and how Scenario D finds it a week later (history, not recall).
- [ ] Episodic vs semantic vs procedural, mapped to node labels.
- [ ] What `?tenant=` does and does not do.
- [ ] What the eval numbers mean, offline vs live, and why only the scorer reads the ground truth.

- [ ] Walk through Scenario A's 11 tool calls and say what each query returns.
- [ ] Explain why `dep-chk-1405` is rejected (temporal) and why `dep-cart-1320` is rejected (origin).
- [ ] Explain the common-cause test with fraud-check, and why refunds-worker is excluded from it.
- [ ] Explain how `recall` finds Scenario A's result for Scenario B, and why re-running A doesn't use memory.
- [ ] Explain why Scenario C is inconclusive and what data would resolve it.
- [ ] Explain `algo.SPpaths` arguments and what `pathWeight` means here.
- [ ] Explain the validator for LLM narratives and the fallback.
- [ ] Explain what is simulated (the rollback) and what is real (the graph writes).
