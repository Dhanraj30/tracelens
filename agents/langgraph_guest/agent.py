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
  * Groq is primary; Gemini is tried by ModelFallbackMiddleware when Groq fails (404/429/5xx).
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
EXCLUDED_TOOLS = {"investigate", "record_action", "list_catalog"}
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
        return sc.get("result", sc)
    try:
        return json.loads(msg.content if isinstance(msg.content, str) else msg.content[0]["text"])
    except Exception:  # noqa: BLE001
        return None


async def run_guest(incident_id: str, *, server: Any = None, model: Any = None, fallbacks: list[Any] | None = None,
                    log=print, save_recording: bool = False) -> dict[str, Any]:
    """Run the guest against `server` (an MCPServer instance for in-process tests, or None = stdio subprocess).

    Returns {"tool_calls": [...in order...], "recorded": <record_investigation result>, "final": str, "model_used": str, "duration_s": float}.
    """
    from langchain.agents import create_agent
    from langchain.agents.middleware import ModelCallLimitMiddleware, ModelFallbackMiddleware, ToolCallLimitMiddleware
    from langchain.mcp import MCPAdapter
    from langchain_core.messages import AIMessage, ToolMessage

    target = server if server is not None else Path(__file__).resolve().parent / "server_launcher.py"
    t0 = time.time()
    async with MCPAdapter(target) as adapter:  # keep the session open for the whole run: the server traces it as one session
        all_tools = await adapter.list_tools()
        tools = [t for t in all_tools if t.name not in EXCLUDED_TOOLS]
        log(f"[guest] MCP tools offered to the model: {', '.join(t.name for t in tools)}  (excluded: {', '.join(sorted(EXCLUDED_TOOLS))})")
        if model is None:
            model, fallbacks = build_models()
        middleware: list[Any] = [
            ToolCallLimitMiddleware(run_limit=MAX_TOOL_CALLS, exit_behavior="continue"),
            ModelCallLimitMiddleware(run_limit=MAX_MODEL_CALLS, exit_behavior="end"),
        ]
        if fallbacks:
            middleware.append(ModelFallbackMiddleware(*fallbacks))
        agent = create_agent(model, tools, system_prompt=SYSTEM_PROMPT, middleware=middleware)
        result = await agent.ainvoke(
            {"messages": [{"role": "user", "content": f"Investigate incident {incident_id}. Gather evidence with the tools, then record your conclusion."}]},
            config={"recursion_limit": 60},
        )

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
    if not isinstance(final, str):
        final = json.dumps(final)[:1000]
    out = {"incident_id": incident_id, "tool_calls": calls, "recorded": recorded, "final": final, "model_used": model_used,
           "duration_s": round(time.time() - t0, 1), "at": int(time.time())}

    log("")
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
