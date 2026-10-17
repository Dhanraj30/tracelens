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

## What you must be able to explain (checklist)

- [ ] Walk through Scenario A's 11 tool calls and say what each query returns.
- [ ] Explain why `dep-chk-1405` is rejected (temporal) and why `dep-cart-1320` is rejected (origin).
- [ ] Explain the common-cause test with fraud-check, and why refunds-worker is excluded from it.
- [ ] Explain how `recall` finds Scenario A's result for Scenario B, and why re-running A doesn't use memory.
- [ ] Explain why Scenario C is inconclusive and what data would resolve it.
- [ ] Explain `algo.SPpaths` arguments and what `pathWeight` means here.
- [ ] Explain the validator for LLM narratives and the fallback.
- [ ] Explain what is simulated (the rollback) and what is real (the graph writes).
