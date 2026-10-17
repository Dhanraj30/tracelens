"""TraceLens MCP server — exposes the SAME tool catalog (never raw Cypher) over MCP.

Run (stdio):   python -m backend.mcp_server
Inspect:       mcp dev backend/mcp_server.py

Every MCP tool delegates to backend/tools/catalog.py through a ToolContext, so the same
validation, entity resolution and tracing apply. An MCP client (an LLM agent, Claude Desktop,
Cursor, or backend/mcp_client_demo.py) can pick these tools to investigate an incident.
"""
from __future__ import annotations

import json
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
        "what was ruled out. Times are integer epoch seconds."
    ),
)

_ctx: ToolContext | None = None


def ctx() -> ToolContext:
    global _ctx
    if _ctx is None:
        _ctx = ToolContext(get_graph(), max_calls=200)
    return _ctx


def _run(name: str, **args: Any) -> dict[str, Any]:
    try:
        r = ctx().call(name, purpose="mcp", **args)
        step = ctx().trace[-1]
        return {"ok": True, **r.to_dict(), "trace": {"step": step.step, "duration_ms": step.duration_ms, "cypher": [q["cypher"] for q in step.queries]}}
    except ToolError as exc:
        return {"ok": False, "error": str(exc), "kind": exc.kind, "unresolved": exc.unresolved}


@mcp.tool()
def get_incident(incident_id: str) -> dict[str, Any]:
    """Load an incident, the service it affects and the error events observed on it."""
    return _run("get_incident", incident_id=incident_id)


@mcp.tool()
def upstream_dependencies(service: str, depth: int = 4) -> dict[str, Any]:
    """Walk DEPENDS_ON edges from a service to everything it transitively relies on (depth 1..4). Defines the suspect space."""
    return _run("upstream_dependencies", service=service, depth=depth)


@mcp.tool()
def deployments_in_window(services: list[str], from_ts: int, to_ts: int) -> dict[str, Any]:
    """Deployments (with CodeChanges) to any of the services inside [from_ts, to_ts] (epoch seconds)."""
    return _run("deployments_in_window", services=services, from_ts=from_ts, to_ts=to_ts)


@mcp.tool()
def error_timeline(services: list[str], from_ts: int, to_ts: int) -> dict[str, Any]:
    """First-seen time and volume per (service, error signature) inside the window. The first row is the error origin."""
    return _run("error_timeline", services=services, from_ts=from_ts, to_ts=to_ts)


@mcp.tool()
def healthy_siblings(service: str, from_ts: int, to_ts: int) -> dict[str, Any]:
    """Common-cause test: other services sharing this service's dependencies, and whether they emitted errors in the window."""
    return _run("healthy_siblings", service=service, from_ts=from_ts, to_ts=to_ts)


@mcp.tool()
def similar_incidents(signature_keys: list[str], services: list[str]) -> dict[str, Any]:
    """Resolved past incidents reached through shared ErrorSignature nodes, with Resolution, Runbook and reverted Deployment."""
    return _run("similar_incidents", signature_keys=signature_keys, services=services)


@mcp.tool()
def path_between(from_service: str, to_service: str) -> dict[str, Any]:
    """Weighted shortest DEPENDS_ON path (algo.SPpaths). Empty = not connected."""
    return _run("path_between", from_service=from_service, to_service=to_service)


@mcp.tool()
def critical_services(limit: int = 10) -> dict[str, Any]:
    """Load-bearing services by PageRank and betweenness centrality."""
    return _run("critical_services", limit=limit)


@mcp.tool()
def recall(services: list[str], from_ts: int, to_ts: int, exclude_incident: str = "") -> dict[str, Any]:
    """Memory: prior investigations (of other incidents) whose supported root cause is a deployment on these services in the window."""
    return _run("recall", services=services, from_ts=from_ts, to_ts=to_ts, exclude_incident=exclude_incident)


@mcp.tool()
def record_investigation(investigation_json: str) -> dict[str, Any]:
    """WRITE: persist an investigation. Argument is a JSON object: {id, incident_id, at, mode, outcome, hypotheses:[{id,status,score,kind,label,suspect,checks:[{name,passed,evidence_ids}]}], action?}."""
    try:
        inv = json.loads(investigation_json)
    except json.JSONDecodeError as exc:
        return {"ok": False, "error": f"invalid JSON: {exc}", "kind": "validation"}
    return _run("record_investigation", investigation=inv)


@mcp.tool()
def record_action(action_id: str, status: str, at: int) -> dict[str, Any]:
    """WRITE: set an Action's status (approved | executed | rejected). Execution is simulated."""
    return _run("record_action", action_id=action_id, status=status, at=at)


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


if __name__ == "__main__":
    mcp.run(transport="stdio")
