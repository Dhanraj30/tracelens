"""MCP client demo: an agent that investigates an incident by SELECTING TraceLens tools over MCP.

It launches backend/mcp_server.py as a stdio subprocess, lists the tools, and then drives
the investigation by choosing which tool to call next based on the previous result
(no Cypher anywhere on the client side). At the end it writes its conclusion back with
`record_investigation`; the SERVER re-verifies the suspects before storing them, and tags the
Investigation with provenance agent="mcp:client-demo". A later dashboard investigation can then
answer from this memory — a cross-agent handoff through shared graph state.

Run:  python -m backend.mcp_client_demo [incident_id]
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from mcp import Client

try:
    from mcp import StdioServerParameters
except ImportError:  # older layouts
    from mcp.client.stdio import StdioServerParameters  # type: ignore

AGENT_NAME = "client-demo"


def ts(v: int | None) -> str:
    return "?" if v is None else datetime.fromtimestamp(v, tz=timezone.utc).strftime("%H:%M")


def data(result: Any) -> dict[str, Any]:
    """Unwrap a CallToolResult into the tool's dict."""
    sc = getattr(result, "structured_content", None) or getattr(result, "structuredContent", None)
    if isinstance(sc, dict):
        inner = sc.get("result", sc)
        if isinstance(inner, str):  # tools return compact JSON text; the SDK wraps it as {"result": "<json>"}
            try:
                return json.loads(inner)
            except json.JSONDecodeError:
                return {"text": inner}
        return inner
    for c in getattr(result, "content", []) or []:
        text = getattr(c, "text", None)
        if text:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                pass
    return {}


def fmt_args(args: dict[str, Any]) -> str:
    return ", ".join(f"{k}=[{len(v)}]" if isinstance(v, list) else f"{k}={v}" for k, v in args.items() if k != "reason")


async def investigate_via_mcp(client: Client, incident_id: str, log: Callable[[str], None] = print, record: bool = True,
                              agent_name: str = AGENT_NAME) -> dict[str, Any]:
    """Drive one investigation through MCP tools. Returns {outcome, verdicts, recorded}."""

    async def call(name: str, why: str, **args: Any) -> dict[str, Any]:
        log(f"→ {name}({fmt_args(args)})   # {why}")
        d = data(await client.call_tool(name, dict(args, reason=why)))
        if not d.get("ok", True):
            log(f"   ✗ {d.get('error')}")
        else:
            log(f"   ✓ {d.get('summary') or str(len(d.get('rows', []))) + ' rows'}")
        return d

    # 1. anchor on the incident
    inc = await call("get_incident", "anchor on the symptom", incident_id=incident_id)
    if not inc.get("ok", True):
        log("\nOutcome: unknown_entity — no further tools were selected.")
        return {"outcome": "unknown_entity", "verdicts": [], "recorded": None}
    row = inc["rows"][0]
    affected, opened = row["service"], int(row["opened_at"])
    win = {"from_ts": opened - 3600, "to_ts": opened}

    # 2. suspect space
    up = await call("upstream_dependencies", f"what does {affected} rely on?", service=affected, depth=4)
    path_services = [affected] + [r["service"] for r in up["rows"]]

    # 3. memory first
    mem = await call("recall", "do we already know?", services=path_services, exclude_incident=incident_id, **win)
    if mem["rows"]:
        m = mem["rows"][0]
        log(f"\nOutcome: answered_from_memory — {m['deployment_id']} on {m['service']} (investigation {m['investigation_id']} by {m.get('agent')}, confirmed={m['confirmed']})")
        return {"outcome": "answered_from_memory", "verdicts": [(m["deployment_id"], "supported", "from memory")], "recorded": None}

    # 4. evidence
    deps = await call("deployments_in_window", "candidate changes", services=path_services, **win)
    errs = await call("error_timeline", "who failed first?", services=path_services, **win)
    if not deps["rows"]:
        log("\nOutcome: inconclusive — no deployments on the dependency path in the window.")
        recorded = None
        if record:
            recorded = await call("record_investigation", "persist the inconclusive result", incident_id=incident_id, outcome="inconclusive",
                                  suspects=[], agent=agent_name, summary="no deployments on the dependency path in the window")
        return {"outcome": "inconclusive", "verdicts": [], "recorded": recorded}
    origin = errs["rows"][0]["service"] if errs["rows"] else None
    first_symptom = min((r["first_seen"] for r in errs["rows"] if r["service"] == affected), default=None)

    # 5. falsify each candidate — the client decides which tool to call next from what it has seen
    verdicts: list[tuple[str, str, str]] = []
    for d in deps["rows"]:
        if first_symptom is not None and d["at"] > first_symptom:
            verdicts.append((d["id"], "ruled out", f"{affected} errors began {ts(first_symptom)}, before this deploy at {ts(d['at'])}"))
            continue
        if d["service"] != affected:
            p = await call("path_between", f"is {d['service']} upstream of {affected}?", from_service=affected, to_service=d["service"])
            if not p.get("connected"):
                verdicts.append((d["id"], "ruled out", f"no dependency path {affected} → {d['service']}"))
                continue
        if origin and d["service"] != origin:
            p2 = await call("path_between", f"does {d['service']} reach the error origin {origin}?", from_service=d["service"], to_service=origin)
            if not p2.get("connected"):
                verdicts.append((d["id"], "ruled out", f"earliest errors sit at {origin}, which {d['service']} does not reach"))
                continue
        sib = await call("healthy_siblings", f"common-cause test around {d['service']}", service=d["service"], **win)
        sims = await call("similar_incidents", "seen this before?", signature_keys=sorted({r["signature"] for r in errs["rows"]}), services=[d["service"]])
        note = f"origin {origin} reachable; shared deps ruled out: {', '.join(sib.get('exonerated_dependencies', [])) or 'n/a'}"
        if sims["rows"]:
            s = sims["rows"][0]
            note += f"; history {s['id']} → runbook {s.get('runbook_id')}"
        verdicts.append((d["id"], "supported", note))

    log("\nVerdicts (best-supported explanation, not proof):")
    for dep_id, status, why in verdicts:
        log(f"  [{status:9}] {dep_id} — {why}")
    supported = [v for v in verdicts if v[1] == "supported"]
    outcome = "root_cause_supported" if len(supported) == 1 else "multiple_candidates" if supported else "inconclusive"
    log(f"\nOutcome (client's claim): {outcome}")

    # 6. write back — the server re-verifies before storing
    recorded = None
    if record:
        recorded = await call("record_investigation", "persist my conclusion for other agents", incident_id=incident_id, outcome=outcome,
                              suspects=[v[0] for v in supported], agent=agent_name, summary="; ".join(f"{v[0]}: {v[2]}" for v in verdicts)[:400])
        if recorded.get("ok"):
            log(f"   server verified -> {recorded['verified_outcome']} | " + ", ".join(f"{h['suspect']}={h['status']}" for h in recorded["hypotheses"])
                + f" | stored as {recorded['investigation_id']} by {recorded['agent']}")
    return {"outcome": outcome, "verdicts": verdicts, "recorded": recorded}


async def main(incident_id: str) -> None:
    params = StdioServerParameters(command=sys.executable, args=["-m", "backend.mcp_server"])
    async with Client(params) as client:
        tools = await client.list_tools()
        names = [t.name for t in tools.tools]
        print(f"[mcp] connected to '{client.server_info.name if client.server_info else 'tracelens'}' — {len(names)} tools: {', '.join(names)}\n")
        await investigate_via_mcp(client, incident_id)


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "inc-1042"))
