"""LangGraph guest agent — LLM tool selection over MCP.

What this is: a SEPARATE agent, built with LangChain's `create_agent` (LangGraph runtime), that
connects to the TraceLens MCP server, sees only the evidence-gathering tools plus
`record_investigation`, and decides for itself which tools to call. It never sees Cypher and
never gets `investigate`, `record_action` or `list_catalog`.

What the server does with its conclusion: `record_investigation` re-runs the rule-based checks
on every suspect the guest proposes and stores a hypothesis as `supported` only if it passes.
The guest's own trace (with its stated reasons, labelled unverified) is attached to the
Investigation node so the dashboard's history view can show it.

Quota guards:
  * pytest uses a fake chat model that replays a recorded tool-call sequence (no network).
  * A live run happens only with RUN_LIVE_LLM=1 (`python -m agents.langgraph_guest.agent inc-1042`).
  * Groq is primary; if it fails (404/429/5xx) the WHOLE run restarts from scratch on Gemini. We never continue
    another model's transcript (Gemini 3.x rejects foreign function calls: thought_signature). Restarts are recorded.
  * Hard caps: 12 tool calls, 16 model calls, recursion limit 60.

Run:  RUN_LIVE_LLM=1 python -m agents.langgraph_guest.agent inc-1042
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

GUEST_NAME = "langgraph"
EXCLUDED_TOOLS = {"investigate", "record_action", "list_catalog", "session_note"}  # session_note is harness-only, never the model's
MAX_TOOL_CALLS = 12
MAX_MODEL_CALLS = 16
RECORDINGS = Path(__file__).resolve().parent / "recordings"
ROOT = Path(__file__).resolve().parents[2]

SYSTEM_PROMPT = f"""You are TraceLens-Guest, an SRE agent investigating a production incident using ONLY the tools provided.
The tools query a FalkorDB dependency graph (services, DEPENDS_ON edges, deployments, error events, past incidents).
Goal: find the deployment that best explains the incident, and the suspects that should be ruled out.

Method (keep it tight, you have at most {MAX_TOOL_CALLS} tool calls):
1. get_incident -> note the affected service and opened_at (epoch seconds).
2. upstream_dependencies(affected, 4) -> the suspect space is the affected service plus everything it depends on.
3. recall(path services, opened_at-3600, opened_at, exclude_incident=<id>) -> if memory already holds a supported root cause, you may stop early.
4. deployments_in_window(path services, opened_at-3600, opened_at) and error_timeline(same) -> candidates and who failed first.
5. For promising candidates: path_between(affected, candidate_service), healthy_siblings(candidate_service, window), similar_incidents(signatures, [candidate_service]).
   A deployment that happened AFTER the affected service's first error cannot be the cause. A candidate whose service does not reach the earliest-error service is not the cause.
6. FINISH by calling record_investigation(incident_id, outcome, suspects=[deployment ids you believe caused it], agent="{GUEST_NAME}", summary=<one paragraph>).
   The server re-verifies your suspects with its own graph checks; a wrong suspect will be stored as rejected, so be careful, not confident.

Always pass a short `reason` with every tool call. Never say "proven"; say "best-supported explanation". If evidence is missing, outcome="inconclusive" with suspects=[].
After record_investigation returns, reply with a 3-sentence summary and stop."""


def _env() -> dict[str, str]:
    vals = dict(dotenv_values(ROOT / ".env"))
    vals.update({k: v for k, v in os.environ.items() if k.startswith("LLM_")})
    return vals


def build_models() -> tuple[Any, list[Any]]:
    """Primary chat model + fallbacks, from the same env vars the core adapter uses. Groq/Gemini are OpenAI-compatible."""
    from langchain_openai import ChatOpenAI

    env = _env()
    primary = ChatOpenAI(model=env["LLM_MODEL"], api_key=env["LLM_API_KEY"], base_url=env["LLM_BASE_URL"], temperature=0.1,
                         max_tokens=4000, timeout=float(env.get("LLM_TIMEOUT_S", "30")), max_retries=1, extra_body={"reasoning_effort": "low"})
    fallbacks: list[Any] = []
    if env.get("LLM_FALLBACK_MODEL") and env.get("LLM_FALLBACK_API_KEY"):
        if "generativelanguage.googleapis.com" in (env.get("LLM_FALLBACK_BASE_URL") or ""):
            # Gemini 3.x function calling needs its thought signatures echoed back. The OpenAI-compatible path through
            # ChatOpenAI drops them (verified 17 Oct 2026: HTTP 400 "missing thought_signature" even on a Gemini-only run),
            # so the fallback uses LangChain's native integration, which preserves them.
            from langchain_google_genai import ChatGoogleGenerativeAI

            kwargs: dict[str, Any] = dict(model=env["LLM_FALLBACK_MODEL"], google_api_key=env["LLM_FALLBACK_API_KEY"], temperature=0.1,
                                          max_output_tokens=4000, timeout=float(env.get("LLM_TIMEOUT_S", "30")), max_retries=1)
            try:
                fallbacks.append(ChatGoogleGenerativeAI(**kwargs, thinking_level="low"))
            except Exception:  # noqa: BLE001 - older integration without thinking_level
                fallbacks.append(ChatGoogleGenerativeAI(**kwargs))
        else:
            fallbacks.append(ChatOpenAI(model=env["LLM_FALLBACK_MODEL"], api_key=env["LLM_FALLBACK_API_KEY"], base_url=env["LLM_FALLBACK_BASE_URL"],
                                        temperature=0.1, max_tokens=4000, timeout=float(env.get("LLM_TIMEOUT_S", "30")), max_retries=1,
                                        extra_body={"reasoning_effort": "low"}))
    return primary, fallbacks


def _structured(msg: Any) -> dict[str, Any] | None:
    art = getattr(msg, "artifact", None)
    if isinstance(art, dict):
        sc = art.get("structured_content")
    else:
        sc = getattr(art, "structured_content", None)
    if isinstance(sc, dict):
        inner = sc.get("result", sc)
        if isinstance(inner, dict):
            return inner
        if isinstance(inner, str):
            try:
                return json.loads(inner)
            except json.JSONDecodeError:
                return None
    try:
        return json.loads(msg.content if isinstance(msg.content, str) else msg.content[0]["text"])
    except Exception:  # noqa: BLE001
        return None


async def run_guest(incident_id: str, *, server: Any = None, model: Any = None, fallbacks: list[Any] | None = None,
                    log=print, save_recording: bool = False, callbacks: list[Any] | None = None) -> dict[str, Any]:
    """Run the guest against `server` (an MCPServer instance for in-process tests, or None = stdio subprocess).

    Returns {"tool_calls": [...in order...], "recorded": <record_investigation result>, "final": str, "model_used": str, "duration_s": float}.
    """
    from langchain.agents import create_agent
    from langchain.agents.middleware import ModelCallLimitMiddleware, ToolCallLimitMiddleware
    from langchain.mcp import MCPAdapter
    from langchain_core.callbacks import BaseCallbackHandler
    from langchain_core.messages import AIMessage, ToolMessage

    class _AttemptCounter(BaseCallbackHandler):
        """Counts tool calls completed in one attempt, so a restart can say how far the failed provider got."""

        def __init__(self) -> None:
            self.tool_calls = 0

        def on_tool_start(self, serialized, input_str, **kwargs):  # noqa: ANN001
            self.tool_calls += 1

    target = server if server is not None else Path(__file__).resolve().parent / "server_launcher.py"
    t0 = time.time()
    restarts: list[dict[str, Any]] = []
    trace_notes: list[dict[str, Any]] = []
    async with MCPAdapter(target) as adapter:  # keep the session open for the whole run: the server traces it as one session
        all_tools = await adapter.list_tools()
        tools = [t for t in all_tools if t.name not in EXCLUDED_TOOLS]
        note_tool = next((t for t in all_tools if t.name == "session_note"), None)
        log(f"[guest] MCP tools offered to the model: {', '.join(t.name for t in tools)}  (excluded: {', '.join(sorted(EXCLUDED_TOOLS))})")
        if model is None:
            model, fallbacks = build_models()
        providers = [model] + list(fallbacks or [])
        result = None
        last_exc: BaseException | None = None
        for i, provider in enumerate(providers):
            # Provider fallback = RESTART THE WHOLE RUN on the next provider. We never continue another model's transcript:
            # Gemini 3.x rejects function calls it did not make (thought_signature), and mixing providers mid-run is unsound anyway.
            middleware: list[Any] = [
                ToolCallLimitMiddleware(run_limit=MAX_TOOL_CALLS, exit_behavior="continue"),
                ModelCallLimitMiddleware(run_limit=MAX_MODEL_CALLS, exit_behavior="end"),
            ]
            agent = create_agent(provider, tools, system_prompt=SYSTEM_PROMPT, middleware=middleware)
            counter = _AttemptCounter()
            try:
                result = await agent.ainvoke(
                    {"messages": [{"role": "user", "content": f"Investigate incident {incident_id}. Gather evidence with the tools, then record your conclusion."}]},
                    config={"recursion_limit": 60, "callbacks": [counter] + list(callbacks or [])},
                )
                break
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if type(exc).__name__ == "BudgetExceeded" or i == len(providers) - 1:
                    raise
                nxt = providers[i + 1]
                nxt_label = getattr(nxt, "model_name", None) or getattr(nxt, "model", None) or getattr(nxt, "_llm_type", "fallback")
                cur_label = getattr(provider, "model_name", None) or getattr(provider, "model", None) or getattr(provider, "_llm_type", "primary")
                note = f"restarted on {nxt_label} after {cur_label} failed: {type(exc).__name__}: {str(exc)[:160]}"
                restarts.append({"note": note, "failed_provider": str(cur_label), "next_provider": str(nxt_label), "calls_before_restart": counter.tool_calls, "error": str(exc)[:400]})
                trace_notes.append({"tool": "restart", "note": note})
                log(f"[guest] {note} (the failed attempt had completed {counter.tool_calls} tool call(s); starting over from scratch)")
                if note_tool is not None:  # harness-side note into the server session trace, so the dashboard history shows the restart
                    try:
                        await note_tool.ainvoke({"note": note})
                    except Exception as note_exc:  # noqa: BLE001
                        log(f"[guest] could not record session note: {note_exc}")
        if result is None:  # pragma: no cover - defensive; the loop either breaks or raises
            raise last_exc or RuntimeError("no provider produced a result")

    # ---- read the transcript: tool calls in order, with the model's stated reasons ----
    calls: list[dict[str, Any]] = []
    recorded: dict[str, Any] | None = None
    model_used = "unknown"
    pending: dict[str, dict[str, Any]] = {}
    for msg in result["messages"]:
        if isinstance(msg, AIMessage):
            meta = getattr(msg, "response_metadata", {}) or {}
            model_used = meta.get("model_name") or meta.get("model") or model_used
            for tc in msg.tool_calls or []:
                args = dict(tc.get("args") or {})
                entry = {"n": len(calls) + 1, "tool": tc["name"], "args": {k: v for k, v in args.items() if k != "reason"},
                         "reason": args.get("reason", ""), "reason_label": "model's stated reason (unverified)", "result": None}
                calls.append(entry)
                pending[tc.get("id") or str(len(calls))] = entry
        elif isinstance(msg, ToolMessage):
            entry = pending.get(getattr(msg, "tool_call_id", "") or "")
            d = _structured(msg)
            if entry is not None:
                entry["result"] = (d.get("summary") if isinstance(d, dict) and d.get("summary") else
                                   (d.get("error") if isinstance(d, dict) and d.get("error") else str(msg.content)[:160]))
                if entry["tool"] == "record_investigation" and isinstance(d, dict):
                    recorded = d
    final = result["messages"][-1].content if result["messages"] else ""
    if not isinstance(final, str):  # Gemini returns content blocks; keep the text, drop signatures/thoughts
        final = " ".join(b.get("text", "") for b in final if isinstance(b, dict) and b.get("type") == "text").strip() or json.dumps(final)[:1000]
    out = {"incident_id": incident_id, "tool_calls": calls, "recorded": recorded, "final": final, "model_used": model_used,
           "duration_s": round(time.time() - t0, 1), "at": int(time.time()), "restarts": restarts, "trace_notes": trace_notes}

    log("")
    for r in restarts:
        log(f" !! {r['note']}")
    for c in calls:
        log(f"{c['n']:>2}. {c['tool']}({', '.join(f'{k}=[{len(v)}]' if isinstance(v, list) else f'{k}={v}' for k, v in c['args'].items())})")
        if c["reason"]:
            log(f"      {c['reason_label']}: {c['reason']}")
        log(f"      -> {c['result']}")
    if recorded:
        log(f"\n[server] claimed={recorded.get('claimed_outcome')} verified={recorded.get('verified_outcome')} | "
            + ", ".join(f"{h['suspect']}={h['status']}" + (f" (failed: {', '.join(h['failed_checks'])})" if h.get("failed_checks") else "") for h in recorded.get("hypotheses", []))
            + f" | stored as {recorded.get('investigation_id')} by {recorded.get('agent')}")
    else:
        log("\n[server] the guest never called record_investigation — nothing was stored")
    log(f"\n[guest] model={model_used} tool_calls={len(calls)} duration={out['duration_s']}s\n{final}")

    if save_recording:
        RECORDINGS.mkdir(exist_ok=True)
        stamp = datetime.fromtimestamp(out["at"], tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        p = RECORDINGS / f"live-{incident_id}-{stamp}.json"
        p.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
        log(f"[guest] recording saved: {p}")
    return out


def main() -> None:
    incident = sys.argv[1] if len(sys.argv) > 1 else "inc-1042"
    if os.environ.get("RUN_LIVE_LLM") != "1":
        print("Refusing to call a live LLM: set RUN_LIVE_LLM=1 to run (quota guard). Tests use a fake model.")
        sys.exit(2)
    print(f"[guest] live run start {datetime.now().strftime('%H:%M:%S')} incident={incident}")
    asyncio.run(run_guest(incident, save_recording=True))


if __name__ == "__main__":
    main()
