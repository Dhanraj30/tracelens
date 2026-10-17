# Test results (captured Sat 17 Oct 2026)

Raw logs live in this folder; rendered screenshots are in `docs/screenshots/`.

| What | Command | Result | Screenshot |
|---|---|---|---|
| Unit + scenario tests | `PYTHONIOENCODING=utf-8 python -m pytest -q` | 29 passed against a real FalkorDB container (`tracelens_test` graph) | `test-results-pytest.png` |
| LLM mode, Groq | `python llm_test.py groq` (model `openai/gpt-oss-120b`) | A/B/C all narrated by the LLM and accepted by the validator; validator rejects an invented ID, an unlisted service and "proven cause" | `test-results-llm-groq.png`, `llm-mode-scenario-a.png`, `llm-mode-scenario-b-memory.png`, `llm-mode-scenario-c-inconclusive.png` |
| LLM mode, Gemini | `python llm_test.py gemini` (model `gemini-2.5-flash`) | A and C narrated by the LLM; B hit a transient 503 and fell back to the deterministic template automatically (shown in the report as "LLM unavailable → deterministic") | `test-results-llm-gemini.png` |
| LLM provider fallback chain | `LLM_MODEL=does-not-exist python …` (primary forced to a bogus model) | Groq returns 404 → adapter moves to the fallback provider → Gemini `gemini-3.8-flash` writes a validated narrative in 4.2 s; both hops recorded in `llm_fallbacks` | `llm-fallback-chain.txt` |
| MCP tool selection | `python -m backend.mcp_client_demo inc-1042` | client lists 13 tools over stdio, selects 10 calls, reaches `root_cause_supported`; unknown incident stops after one call | `test-results-mcp.png` |
| Docker image | `docker compose build app` then run against an empty graph key | auto-seeds (80 nodes), serves the built UI, Scenario A → `dep-pay-1352` in 11 calls | see `PLAN.md` |

Notes
- `llama-3.3-70b-versatile` and `llama-3.1-8b-instant` both returned `model_not_found` on this Groq key; the default model is `openai/gpt-oss-120b`. List what your key can use with `curl https://api.groq.com/openai/v1/models -H "Authorization: Bearer $LLM_API_KEY"`.
- Reasoning models (gpt-oss, gemini-3.x) spend hidden tokens from `max_tokens`; the adapter sends `max_tokens: 4000` with `reasoning_effort: low` and treats `finish_reason=length` as a failure (fallback), so a truncated narrative is never shown. Before adding `reasoning_effort`, gemini-3.8-flash exhausted a 2000-token budget on thinking alone.
- The deterministic outcome, hypotheses and evidence are identical in both modes; only the narrative text differs.
