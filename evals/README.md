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
`budget`); `--min-interval` spaces model requests for free-tier RPM limits; the OpenAI-compatible client retries a 429 once
honouring `Retry-After`, after which the guest's `ModelFallbackMiddleware` switches to Gemini and the switch is recorded as a fallback.

**Outputs.** `evals/results/<timestamp>-<mode>.json` (raw) and `docs/EVALS.md` (generated: tables, summary line, models, commit, failures and why).

**Rule.** Do not change agent logic or prompts to make evals pass. Evals describe behaviour; they do not tune it.
