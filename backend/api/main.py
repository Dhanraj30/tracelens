"""TraceLens API. FastAPI serves the JSON API and (in the Docker image) the built frontend."""
from __future__ import annotations

import json
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal, Optional

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..agent.engine import Investigator
from ..agent import llm as llm_adapter
from ..config import settings
from ..db import Graph, get_graph
from ..seed import clear_memory, counts, reset_and_seed, seed_if_empty
from ..tools import REGISTRY, ToolContext, ToolError
from ..tools.base import validate_id

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

# In-memory store of full reports (incl. traces) for this process, keyed by (graph key, id) so tenants never see
# each other's reports. The durable memory lives in the graph.
REPORTS: dict[tuple[str, str], dict[str, Any]] = {}
ACTIONS: dict[tuple[str, str], str] = {}  # (graph, action_id) -> investigation_id


@asynccontextmanager
async def lifespan(app: FastAPI):
    g = get_graph()
    if settings.auto_seed and g.ping():
        try:
            if seed_if_empty(g):
                print("[tracelens] demo graph was empty -> seeded")
        except Exception as exc:  # noqa: BLE001
            print(f"[tracelens] auto-seed failed: {exc}")
    yield


app = FastAPI(title="TraceLens API", version="0.1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


def tenant_graph(tenant: Optional[str] = Query(default=None, description="optional tenant slug -> graph key <GRAPH_NAME>:<tenant>, auto-seeded")) -> Graph:
    """One FalkorDB graph per tenant. Default (no tenant) is the demo graph. Memory never crosses tenants."""
    try:
        g = get_graph(tenant)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if tenant and settings.auto_seed and g.ping():
        seed_if_empty(g)
    return g


class InvestigateRequest(BaseModel):
    incident_id: Optional[str] = Field(default=None, description="e.g. inc-1042")
    service: Optional[str] = Field(default=None, description="investigate the latest open incident on this service")
    mode: Literal["deterministic", "llm"] = "deterministic"


@app.get("/api/health")
def health(g: Graph = Depends(tenant_graph)) -> dict[str, Any]:
    ok = g.ping()
    nodes = g.node_count() if ok else 0
    return {
        "status": "ok" if ok else "degraded",
        "falkordb": {"reachable": ok, "host": settings.falkordb_host, "port": settings.falkordb_port},
        "graph": {"name": g.name, "nodes": nodes, "seeded": nodes > 0, "tenant": g.name.split(":", 1)[1] if ":" in g.name else None},
        "llm": {"configured": llm_adapter.available(), "model": settings.llm_model if llm_adapter.available() else None,
                "fallback_model": settings.llm_fallback_model or None},
        "time": int(time.time()),
    }


@app.get("/api/incidents")
def incidents(g: Graph = Depends(tenant_graph)) -> list[dict[str, Any]]:
    rows, _ = g.read(
        "MATCH (i:Incident)-[:AFFECTS]->(s:Service) "
        "OPTIONAL MATCH (inv:Investigation)-[:INVESTIGATED]->(i) "
        "RETURN i.id, i.title, i.status, i.opened_at, i.symptom, s.name, count(inv) ORDER BY i.opened_at DESC"
    )
    return [dict(zip(["id", "title", "status", "opened_at", "symptom", "service", "investigations"], r)) for r in rows]


@app.get("/api/graph")
def graph_view(g: Graph = Depends(tenant_graph)) -> dict[str, Any]:
    nodes, _ = g.read("MATCH (s:Service) OPTIONAL MATCH (s)<-[:EMITTED_BY]-(e:ErrorEvent) RETURN s.name, s.tier, s.team, s.external, count(e) ORDER BY s.name")
    edges, _ = g.read("MATCH (a:Service)-[d:DEPENDS_ON]->(b:Service) RETURN a.name, b.name, d.kind, d.weight ORDER BY a.name, b.name")
    deps, _ = g.read("MATCH (d:Deployment)-[:DEPLOYED_TO]->(s:Service) RETURN d.id, d.at, d.version, s.name ORDER BY d.at")
    return {
        "services": [dict(zip(["name", "tier", "team", "external", "error_events"], r)) for r in nodes],
        "edges": [dict(zip(["source", "target", "kind", "weight"], r)) for r in edges],
        "deployments": [dict(zip(["id", "at", "version", "service"], r)) for r in deps],
    }


@app.post("/api/investigations")
def investigate(req: InvestigateRequest, g: Graph = Depends(tenant_graph)) -> dict[str, Any]:
    if not req.incident_id and not req.service:
        raise HTTPException(status_code=422, detail="incident_id or service is required")
    if not g.ping():
        raise HTTPException(status_code=503, detail="FalkorDB unreachable")
    report = Investigator(g, mode=req.mode).run(incident_id=req.incident_id, service=req.service)
    REPORTS[(g.name, report["id"])] = report
    if report.get("action"):
        ACTIONS[(g.name, report["action"]["id"])] = report["id"]
    return report


@app.get("/api/investigations")
def list_investigations(g: Graph = Depends(tenant_graph)) -> list[dict[str, Any]]:
    """Every investigation in the graph, whoever wrote it (engine or an MCP guest agent)."""
    rows, _ = g.read(
        "MATCH (inv:Investigation)-[:INVESTIGATED]->(i:Incident) OPTIONAL MATCH (inv)-[:PROPOSED]->(a:Action) "
        "OPTIONAL MATCH (inv)-[:CONSIDERED]->(h:Hypothesis) "
        "WITH inv, i, a, collect({status: h.status, label: h.label}) AS hyps "
        "RETURN inv.id, inv.at, inv.mode, inv.outcome, inv.tool_calls, inv.confidence, i.id, i.title, a.id, a.status, "
        "coalesce(inv.agent, 'engine'), coalesce(inv.verified, true), hyps ORDER BY inv.at DESC"
    )
    out = []
    for r in rows:
        d = dict(zip(["id", "at", "mode", "outcome", "tool_calls", "confidence", "incident_id", "incident_title", "action_id", "action_status", "agent", "verified", "hyps"], r))
        hyps = [h for h in d.pop("hyps") if h.get("status")]
        d["supported"] = sum(1 for h in hyps if h["status"] == "supported")
        d["rejected"] = sum(1 for h in hyps if h["status"] == "rejected")
        out.append(d)
    return out


@app.get("/api/investigations/{investigation_id}")
def get_investigation(investigation_id: str, g: Graph = Depends(tenant_graph)) -> dict[str, Any]:
    """Full in-process report when available; otherwise the durable record from the graph (agent, trace, hypotheses, evidence edges)."""
    try:
        investigation_id = validate_id(investigation_id, "investigation_id")
    except ToolError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if (g.name, investigation_id) in REPORTS:
        return REPORTS[(g.name, investigation_id)]
    rows, _ = g.read(
        "MATCH (inv:Investigation {id: $id})-[:INVESTIGATED]->(i:Incident)-[:AFFECTS]->(s:Service) OPTIONAL MATCH (inv)-[:PROPOSED]->(a:Action) "
        "RETURN inv.id, inv.at, inv.mode, inv.outcome, inv.tool_calls, inv.confidence, coalesce(inv.agent,'engine'), coalesce(inv.verified,true), inv.trace, "
        "i.id, i.title, i.symptom, i.opened_at, s.name, a.id, a.status, a.summary, a.deployment_id, a.runbook_id",
        {"id": investigation_id},
    )
    if not rows:
        raise HTTPException(status_code=404, detail="investigation not found")
    r = rows[0]
    hrows, _ = g.read(
        "MATCH (inv:Investigation {id: $id})-[:CONSIDERED]->(h:Hypothesis) OPTIONAL MATCH (h)-[e:SUPPORTED_BY|REJECTED_BY]->(n) "
        "OPTIONAL MATCH (h)-[:SUSPECTS]->(sus) "
        "RETURN h.id, h.status, h.score, h.kind, h.label, h.confirmed, collect(DISTINCT coalesce(sus.id, sus.name)), "
        "collect({type: type(e), check: e.check, target: coalesce(n.id, n.name)}), coalesce(h.proposed, true) ORDER BY h.status, h.id",
        {"id": investigation_id},
    )
    hypotheses = []
    for h in hrows:
        edges = [e for e in h[7] if e.get("type")]
        hypotheses.append({"id": h[0], "status": h[1], "score": h[2], "kind": h[3], "label": h[4], "confirmed": h[5], "suspects": [s for s in h[6] if s],
                           "supported_by": [e for e in edges if e["type"] == "SUPPORTED_BY"], "rejected_by": [e for e in edges if e["type"] == "REJECTED_BY"],
                           "proposed": h[8]})
    try:
        trace = json.loads(r[8] or "[]")
    except json.JSONDecodeError:
        trace = []
    return {"id": r[0], "at": r[1], "mode": r[2], "outcome": r[3], "tool_calls": r[4], "confidence": r[5], "agent": r[6], "verified": r[7],
            "trace": trace, "incident": {"id": r[9], "title": r[10], "symptom": r[11], "opened_at": r[12], "service": r[13]},
            "action": {"id": r[14], "status": r[15], "summary": r[16], "deployment_id": r[17], "runbook_id": r[18]} if r[14] else None,
            "hypotheses": hypotheses, "source": "graph"}


@app.post("/api/actions/{action_id}/approve")
def approve_action(action_id: str, g: Graph = Depends(tenant_graph)) -> dict[str, Any]:
    """Human approval -> the action is marked approved, then 'executed' (SIMULATED — nothing is rolled back for real)."""
    ctx = ToolContext(g, max_calls=4)
    now = int(time.time())
    try:
        ctx.call("record_action", purpose="operator approved", action_id=action_id, status="approved", at=now)
        res = ctx.call("record_action", purpose="simulated execution", action_id=action_id, status="executed", at=now)
    except ToolError as exc:
        raise HTTPException(status_code=404 if exc.kind == "unresolved" else 400, detail=str(exc))
    action = res.rows[0]
    inv_id = ACTIONS.get((g.name, action_id))
    rep = REPORTS.get((g.name, inv_id)) if inv_id else None
    if rep and rep.get("action"):
        rep["action"]["status"] = "executed"
        rep["action"]["executed_at"] = now
    return {"action": action, "simulated": True, "message": f"SIMULATED: rollback of {action['deployment_id']} recorded as executed. No real system was touched.",
            "trace": [c.to_dict() for c in ctx.trace]}


@app.post("/api/demo/reset")
def demo_reset(memory_only: bool = False, g: Graph = Depends(tenant_graph)) -> dict[str, Any]:
    for k in [k for k in REPORTS if k[0] == g.name]:  # only this tenant's cache
        REPORTS.pop(k, None)
    for k in [k for k in ACTIONS if k[0] == g.name]:
        ACTIONS.pop(k, None)
    if memory_only:
        return {"reset": "memory", "deleted": clear_memory(g), "counts": counts(g)}
    return {"reset": "graph", "counts": reset_and_seed(g)}


@app.get("/api/tools")
def tools() -> list[dict[str, Any]]:
    return [{"name": s.name, "description": s.description, "proves": s.proves, "params": s.params, "writes": s.writes, "cypher": [c.strip() for c in s.cypher]}
            for s in REGISTRY.values()]


# ---- static frontend (built into backend/static by the Dockerfile) ------------------------------
if STATIC_DIR.exists():
    app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")

    @app.get("/{full_path:path}")
    def spa(full_path: str):
        candidate = STATIC_DIR / full_path
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(STATIC_DIR / "index.html")
