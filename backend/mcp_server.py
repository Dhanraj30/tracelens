"""TraceLens MCP server — exposes the SAME tool catalog (never raw Cypher) over MCP.

Run (stdio):   python -m backend.mcp_server
Inspect:       mcp dev backend/mcp_server.py

Every MCP tool delegates to backend/tools/catalog.py through a ToolContext, so the same
validation, entity resolution and tracing apply. An MCP client (an LLM agent, Claude Desktop,
Cursor, backend/mcp_client_demo.py or agents/langgraph_guest) can pick these tools to
investigate an incident.

Provenance + verification:
  * Every evidence tool accepts an optional `reason` — the calling model's short stated reason.
    It is recorded in the session trace labelled "model's stated reason (unverified)".
  * `record_investigation` forces the provenance label to "mcp:<agent>", attaches the session
    trace since the previous record, and SERVER-SIDE VERIFIES every proposed suspect by rerunning
    the rule-based checks (backend.agent.engine.verify_candidates). A suspect is stored as
    `supported` only if it passes; otherwise `rejected` with the failed checks.
"""
from __future__ import annotations

import json
import time
from typing import Any

from mcp.server import MCPServer

from .db import get_graph
from .tools import REGISTRY, ToolContext, ToolError

mcp = MCPServer(
    "tracelens",
    instructions=(
        "Incident root-cause tools backed by a FalkorDB dependency graph. Typical order: get_incident -> "
        "upstream_dependencies -> recall -> deployments_in_window -> error_timeline -> path_between / healthy_siblings / "
        "similar_incidents -> record_investigation. Never claim a 'proven' cause; report the best-supported explanation and "
        "what was ruled out. Times are integer epoch seconds. Pass a short `reason` with each call."
    ),
)

_ctx: ToolContext | None = None
_session: dict[str, Any] = {"trace_start": 0, "agent": "mcp:unknown"}


def ctx() -> ToolContext:
    global _ctx
    if _ctx is None:
        _ctx = ToolContext(get_graph(), max_calls=500)
    return _ctx


def session_trace() -> list[dict[str, Any]]:
    """Compact per-step summary of the calls made since the last record_investigation."""
    out = []
    for c in ctx().trace[_session["trace_start"]:]:
        out.append({"step": c.step, "tool": c.tool, "args": {k: v for k, v in c.args.items() if k != "reason"}, "summary": c.summary,
                    "rows": c.row_count, "ms": c.duration_ms, "status": c.status, "purpose": c.purpose})
    return out


def _run(name: str, reason: str = "", **args: Any) -> dict[str, Any]:
    purpose = f"model's stated reason (unverified): {reason.strip()[:200]}" if reason else "mcp"
    try:
        r = ctx().call(name, purpose=purpose, **args)
        step = ctx().trace[-1]
        return {"ok": True, **r.to_dict(), "trace": {"step": step.step, "duration_ms": step.duration_ms, "cypher": [q["cypher"] for q in step.queries]}}
    except ToolError as exc:
        return {"ok": False, "error": str(exc), "kind": exc.kind, "unresolved": exc.unresolved}


REASON_DOC = " `reason`: one short sentence on why you are calling this tool (recorded, unverified)."


@mcp.tool()
def get_incident(incident_id: str, reason: str = "") -> dict[str, Any]:
    """Load an incident, the service it affects and the error events observed on it. Start here.""" + REASON_DOC
    return _run("get_incident", reason, incident_id=incident_id)


@mcp.tool()
def upstream_dependencies(service: str, depth: int = 4, reason: str = "") -> dict[str, Any]:
    """Walk DEPENDS_ON edges from a service to everything it transitively relies on (depth 1..4). Defines the suspect space.""" + REASON_DOC
    return _run("upstream_dependencies", reason, service=service, depth=depth)


@mcp.tool()
def deployments_in_window(services: list[str], from_ts: int, to_ts: int, reason: str = "") -> dict[str, Any]:
    """Deployments (with CodeChanges) to any of the services inside [from_ts, to_ts] (epoch seconds). Each is a candidate cause.""" + REASON_DOC
    return _run("deployments_in_window", reason, services=services, from_ts=from_ts, to_ts=to_ts)


@mcp.tool()
def error_timeline(services: list[str], from_ts: int, to_ts: int, reason: str = "") -> dict[str, Any]:
    """First-seen time and volume per (service, error signature) inside the window. The first row is the error origin.""" + REASON_DOC
    return _run("error_timeline", reason, services=services, from_ts=from_ts, to_ts=to_ts)


@mcp.tool()
def healthy_siblings(service: str, from_ts: int, to_ts: int, reason: str = "") -> dict[str, Any]:
    """Common-cause test: other services sharing this service's dependencies, and whether they emitted errors in the window.""" + REASON_DOC
    return _run("healthy_siblings", reason, service=service, from_ts=from_ts, to_ts=to_ts)


@mcp.tool()
def similar_incidents(signature_keys: list[str], services: list[str], reason: str = "") -> dict[str, Any]:
    """Resolved past incidents reached through shared ErrorSignature nodes, with Resolution, Runbook and reverted Deployment.""" + REASON_DOC
    return _run("similar_incidents", reason, signature_keys=signature_keys, services=services)


@mcp.tool()
def path_between(from_service: str, to_service: str, reason: str = "") -> dict[str, Any]:
    """Weighted shortest DEPENDS_ON path (algo.SPpaths) from one service to another. Empty = not connected.""" + REASON_DOC
    return _run("path_between", reason, from_service=from_service, to_service=to_service)


@mcp.tool()
def critical_services(limit: int = 10, reason: str = "") -> dict[str, Any]:
    """Load-bearing services by PageRank and betweenness centrality.""" + REASON_DOC
    return _run("critical_services", reason, limit=limit)


@mcp.tool()
def recall(services: list[str], from_ts: int, to_ts: int, exclude_incident: str = "", reason: str = "") -> dict[str, Any]:
    """Memory: prior investigations (of other incidents) whose supported root cause is a deployment on these services in the window.""" + REASON_DOC
    return _run("recall", reason, services=services, from_ts=from_ts, to_ts=to_ts, exclude_incident=exclude_incident)


@mcp.tool()
def record_investigation(incident_id: str, outcome: str, suspects: list[str], agent: str = "unknown", summary: str = "",
                         reason: str = "") -> dict[str, Any]:
    """WRITE: persist your conclusion. `suspects` = deployment IDs you believe caused the incident (empty if inconclusive).
    The server RE-VERIFIES every suspect with its rule-based graph checks before storing it as supported; anything that fails
    is stored as rejected with the failing checks. Provenance is recorded as "mcp:<agent>". Returns the verified result.""" + REASON_DOC
    from .agent.engine import verify_candidates

    label = agent if agent.startswith("mcp:") else f"mcp:{agent}"
    g = get_graph()
    try:
        verification = verify_candidates(g, incident_id, suspects)
    except ToolError as exc:
        return {"ok": False, "error": str(exc), "kind": exc.kind, "unresolved": exc.unresolved}
    hyps = verification["hypotheses"]
    supported = [h for h in hyps if h["status"] == "supported"]
    verified_outcome = "root_cause_supported" if len(supported) == 1 else "multiple_candidates" if supported else "inconclusive"
    inv_id = f"inv-{int(time.time() * 1000)}"
    trace = session_trace()
    payload = {
        "id": inv_id, "incident_id": incident_id, "at": int(time.time()), "mode": "agent", "outcome": verified_outcome,
        "tool_calls": len(trace), "confidence": verification["confidence"], "agent": label, "verified": True,
        "trace": trace + [{"step": len(trace) + 1, "tool": "server_verification", "args": {"suspects": suspects}, "rows": len(hyps), "ms": verification["duration_ms"],
                           "summary": f"server re-ran {verification['checks_run']} checks: {len(supported)} supported, {len(hyps) - len(supported)} rejected",
                           "purpose": f"guest claimed outcome={outcome}; summary: {summary[:200]}"}],
        "hypotheses": [{"id": f"{inv_id}-h{i}", "status": h["status"], "score": h["score"], "kind": "deployment", "label": h["label"],
                        "suspect": h["suspect"], "checks": h["checks"]} for i, h in enumerate(hyps, start=1)],
        "action": verification.get("action") and dict(verification["action"], id=f"act-{inv_id[4:]}"),
    }
    res = _run("record_investigation", reason, investigation=payload)
    _session["trace_start"] = len(ctx().trace)
    if not res.get("ok", True):
        return res
    return {"ok": True, "investigation_id": inv_id, "agent": label, "claimed_outcome": outcome, "verified_outcome": verified_outcome,
            "hypotheses": [{"suspect": h["suspect"], "status": h["status"], "score": h["score"],
                            "failed_checks": [c["name"] for c in h["checks"] if c["passed"] is False]} for h in hyps],
            "action": payload["action"], "tool_calls_recorded": len(trace)}


@mcp.tool()
def record_action(action_id: str, status: str, at: int) -> dict[str, Any]:
    """WRITE: set an Action's status (approved | executed | rejected). Execution is simulated. (Humans only — not for guest agents.)"""
    return _run("record_action", "", action_id=action_id, status=status, at=at)


@mcp.tool()
def investigate(incident_id: str) -> dict[str, Any]:
    """Run the full bounded TraceLens investigation for an incident and return the report (outcome, hypotheses, root cause, trace)."""
    from .agent.engine import Investigator

    rep = Investigator(get_graph()).run(incident_id=incident_id)
    rep["trace_tools"] = [c["tool"] for c in rep["trace"]]
    del rep["trace"]  # full Cypher trace is available per-tool; keep the MCP payload small
    return rep


@mcp.tool()
def list_catalog() -> list[dict[str, Any]]:
    """Describe the tool catalog (name, what a result proves, parameters, whether it writes)."""
    return [{"name": s.name, "proves": s.proves, "params": s.params, "writes": s.writes} for s in REGISTRY.values()]


GUEST_TOOLS = ["get_incident", "upstream_dependencies", "deployments_in_window", "error_timeline", "healthy_siblings",
               "similar_incidents", "path_between", "critical_services", "recall", "record_investigation"]
"""Tools an outside LLM agent may use: evidence gathering + the verified write-back. Never investigate/record_action/list_catalog."""


if __name__ == "__main__":
    mcp.run(transport="stdio")
