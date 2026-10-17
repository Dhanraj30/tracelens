# Evals — LangGraph guest agent vs deterministic engine

```
python -m evals.run_evals                                   # offline: replay/fake model, no network (pytest runs this)
RUN_LIVE_LLM=1 python -m evals.run_evals --live --runs 2 --max-requests 100 --min-interval 2.5
```

**What is measured.** Five cases (A red herring, B after A, B alone, C incomplete evidence, unknown id), each run through the
LangGraph guest over MCP *and* through the deterministic engine as a baseline. Per case: verified-correct rate, outcome match,
ruled-out correct, tool calls, LLM requests, latency, fallbacks (Groq → Gemini → template), tool calls rejected by catalog
validation, narratives rejected by the validator, proposals rejected by the server verifier.

**Isolation.** Every run and mode gets its own tenant graph `tracelens:eval-<run_id>-<mode>`, freshly seeded and deleted at the
end; memory cannot leak between runs. "B after A" deliberately reuses the graph A just wrote to.

**Ground truth.** Only `score_guest` / `score_engine` read `tests/scenarios.yaml`. The agent, its prompt and the replay scripts never do.

**Quota guards (live).** `--max-requests` is a hard cap enforced in a LangChain callback (the run stops cleanly and records
`budget`); `--min-interval` (default **12 s** in live mode) spaces model requests; the client retries a 429 once honouring
`Retry-After`. If a provider still fails, the guest **restarts the whole run from scratch on the next provider** and the
restart is recorded as `restarted on <provider> after <error>` (it never continues another model's transcript — Gemini 3.x
rejects foreign function calls). Measured on 17 Oct 2026: the Groq free tier for `openai/gpt-oss-120b` allows 8,000 tokens per
minute and 1,000 requests per day; each guest request re-sends a 3–6k-token transcript, so plan on roughly 30 s between
requests (`--min-interval 30`) for a 429-free pass.

**Scoring changes are disclosed.** When a saved run is re-scored (`--rescore <raw.json>`), the report states what changed, why,
and shows both the original and re-scored totals; both raw files are kept.

**Outputs.** `evals/results/<timestamp>-<mode>.json` (raw) and `docs/EVALS.md` (generated: tables, summary line, models, commit, failures and why).

**Rule.** Do not change agent logic or prompts to make evals pass. Evals describe behaviour; they do not tune it.
