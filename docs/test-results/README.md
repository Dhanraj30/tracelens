# Test results (captured Sat 17 Oct 2026)

Raw logs live in this folder; rendered screenshots are in `docs/screenshots/`.

| What | Command | Result | Screenshot |
|---|---|---|---|
| Unit + scenario tests | `PYTHONIOENCODING=utf-8 python -m pytest -q` | 43 passed against a real FalkorDB container (`tracelens_test` graph): tools, scenarios A–D, validator, MCP guards, cross-agent handoff, guest fake-model replay, tenant isolation | `test-results-pytest.png` |
| LLM mode, Groq | `python llm_test.py groq` (model `openai/gpt-oss-120b`) | A/B/C all narrated by the LLM and accepted by the validator; validator rejects an invented ID, an unlisted service and "proven cause" | `test-results-llm-groq.png`, `llm-mode-scenario-a.png`, `llm-mode-scenario-b-memory.png`, `llm-mode-scenario-c-inconclusive.png` |
| LLM mode, Gemini | `python llm_test.py gemini` (model `gemini-2.5-flash`) | A and C narrated by the LLM; B hit a transient 503 and fell back to the deterministic template automatically (shown in the report as "LLM unavailable → deterministic") | `test-results-llm-gemini.png` |
| LLM provider fallback chain | `LLM_MODEL=does-not-exist python …` (primary forced to a bogus model) | Groq returns 404 → adapter moves to the fallback provider → Gemini `gemini-3.5-flash-lite` writes a validated narrative in 4.5 s; both hops recorded in `llm_fallbacks` (gemini-3.8-flash also worked, 4.2 s, but the lite model uses fewer tokens) | `llm-fallback-chain.txt` |
| LangGraph guest, LLM tool selection over MCP (live) | `RUN_LIVE_LLM=1 python -m agents.langgraph_guest.agent inc-1042` | gpt-oss-120b chose 7 tools in this order: get_incident → upstream_dependencies → recall → deployments_in_window → error_timeline → healthy_siblings → record_investigation (skipped path_between, similar_incidents). Proposed `dep-pay-1352`; server re-verified → `root_cause_supported` (5/5 checks), stored as `mcp:langgraph`. Same verdict as deterministic mode. 133.7 s on the Groq free tier | `langgraph-guest-live.txt`, recording in `agents/langgraph_guest/recordings/` |
| LangGraph guest, fake model (no network) | `pytest agents/langgraph_guest/tests` | replayed sequence reaches the verified root cause and hands off to Scenario B; a naive guest blaming `dep-chk-1405` is stored as rejected (temporal_precedence) | part of the 43-test suite |
| Evals, live run 1 | `RUN_LIVE_LLM=1 python -m evals.run_evals --live --runs 1` | guest 3/5 after re-score (disclosed), baseline 5/5, 28 requests; failures = Groq 429 (8k TPM) + mid-run Gemini fallback rejected (thought_signature) | `evals-live-run1.txt`, `docs/EVALS.md`, `evals/results/*-live*.json` |
| Gemini fallback, OpenAI-compatible path | Gemini-only guest run on `inc-9999` | FAILS even alone: HTTP 400 missing thought_signature (ChatOpenAI drops Gemini's signatures) | `gemini-only-guest.txt` |
| Gemini fallback, native integration | same run through `langchain-google-genai` | works: 3 tool calls, signatures preserved, 9.9 s (the model also tried `critical_services` and a `record_investigation` the server rejected — a behaviour difference, not an error) | `gemini-native-guest.txt` |
| Groq limits probe | one 1-token request, response headers | 1,000 requests/day (982 left at 20:07 IST), 8,000 tokens/minute; no daily-token header exposed | `groq-limits-*.txt` |
| MCP tool selection (rule-based client) | `python -m backend.mcp_client_demo inc-1042` | client lists 12 tools over stdio (admin tools investigate/record_action are not exposed by default), selects 10 calls, reaches `root_cause_supported`; unknown incident stops after one call | `test-results-mcp.png` |
| Docker image | `docker compose build app` then run against an empty graph key | auto-seeds (80 nodes), serves the built UI, Scenario A → `dep-pay-1352` in 11 calls | see `PLAN.md` |

Notes
- `llama-3.3-70b-versatile` and `llama-3.1-8b-instant` both returned `model_not_found` on this Groq key; the default model is `openai/gpt-oss-120b`. List what your key can use with `curl https://api.groq.com/openai/v1/models -H "Authorization: Bearer $LLM_API_KEY"`.
- Reasoning models (gpt-oss, gemini-3.x) spend hidden tokens from `max_tokens`; the adapter sends `max_tokens: 4000` with `reasoning_effort: low` and treats `finish_reason=length` as a failure (fallback), so a truncated narrative is never shown. Before adding `reasoning_effort`, gemini-3.8-flash exhausted a 2000-token budget on thinking alone.
- The deterministic outcome, hypotheses and evidence are identical in both modes; only the narrative text differs.
