"""TraceLens API. FastAPI serves the JSON API and (in the Docker image) the built frontend."""
from __future__ import annotations

import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal, Optional

from fastapi import FastAPI, HTTPException
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

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

# In-memory store of full reports (incl. traces) for this process. The durable memory lives in the graph.
REPORTS: dict[str, dict[str, Any]] = {}
ACTIONS: dict[str, str] = {}  # action_id -> investigation_id


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


class InvestigateRequest(BaseModel):
    incident_id: Optional[str] = Field(default=None, description="e.g. inc-1042")
    service: Optional[str] = Field(default=None, description="investigate the latest open incident on this service")
    mode: Literal["deterministic", "llm"] = "deterministic"


@app.get("/api/health")
def health() -> dict[str, Any]:
    g = get_graph()
    ok = g.ping()
    nodes = g.node_count() if ok else 0
    return {
        "status": "ok" if ok else "degraded",
        "falkordb": {"reachable": ok, "host": settings.falkordb_host, "port": settings.falkordb_port},
        "graph": {"name": g.name, "nodes": nodes, "seeded": nodes > 0},
        "llm": {"configured": llm_adapter.available(), "model": settings.llm_model if llm_adapter.available() else None},
        "time": int(time.time()),
    }


@app.get("/api/incidents")
def incidents() -> list[dict[str, Any]]:
    g = get_graph()
    rows, _ = g.read(
        "MATCH (i:Incident)-[:AFFECTS]->(s:Service) "
        "OPTIONAL MATCH (inv:Investigation)-[:INVESTIGATED]->(i) "
        "RETURN i.id, i.title, i.status, i.opened_at, i.symptom, s.name, count(inv) ORDER BY i.opened_at DESC"
    )
    return [dict(zip(["id", "title", "status", "opened_at", "symptom", "service", "investigations"], r)) for r in rows]


@app.get("/api/graph")
def graph_view() -> dict[str, Any]:
    g = get_graph()
    nodes, _ = g.read("MATCH (s:Service) OPTIONAL MATCH (s)<-[:EMITTED_BY]-(e:ErrorEvent) RETURN s.name, s.tier, s.team, s.external, count(e) ORDER BY s.name")
    edges, _ = g.read("MATCH (a:Service)-[d:DEPENDS_ON]->(b:Service) RETURN a.name, b.name, d.kind, d.weight ORDER BY a.name, b.name")
    deps, _ = g.read("MATCH (d:Deployment)-[:DEPLOYED_TO]->(s:Service) RETURN d.id, d.at, d.version, s.name ORDER BY d.at")
    return {
        "services": [dict(zip(["name", "tier", "team", "external", "error_events"], r)) for r in nodes],
        "edges": [dict(zip(["source", "target", "kind", "weight"], r)) for r in edges],
        "deployments": [dict(zip(["id", "at", "version", "service"], r)) for r in deps],
    }


@app.post("/api/investigations")
def investigate(req: InvestigateRequest) -> dict[str, Any]:
    if not req.incident_id and not req.service:
        raise HTTPException(status_code=422, detail="incident_id or service is required")
    g = get_graph()
    if not g.ping():
        raise HTTPException(status_code=503, detail="FalkorDB unreachable")
    report = Investigator(g, mode=req.mode).run(incident_id=req.incident_id, service=req.service)
    REPORTS[report["id"]] = report
    if report.get("action"):
        ACTIONS[report["action"]["id"]] = report["id"]
    return report


@app.get("/api/investigations")
def list_investigations() -> list[dict[str, Any]]:
    g = get_graph()
    rows, _ = g.read(
        "MATCH (inv:Investigation)-[:INVESTIGATED]->(i:Incident) OPTIONAL MATCH (inv)-[:PROPOSED]->(a:Action) "
        "RETURN inv.id, inv.at, inv.mode, inv.outcome, inv.tool_calls, inv.confidence, i.id, i.title, a.id, a.status ORDER BY inv.at DESC"
    )
    return [dict(zip(["id", "at", "mode", "outcome", "tool_calls", "confidence", "incident_id", "incident_title", "action_id", "action_status"], r)) for r in rows]


@app.get("/api/investigations/{investigation_id}")
def get_investigation(investigation_id: str) -> dict[str, Any]:
    if investigation_id in REPORTS:
        return REPORTS[investigation_id]
    raise HTTPException(status_code=404, detail="investigation not found in this process (full traces are kept in memory; summary via GET /api/investigations)")


@app.post("/api/actions/{action_id}/approve")
def approve_action(action_id: str) -> dict[str, Any]:
    """Human approval -> the action is marked approved, then 'executed' (SIMULATED — nothing is rolled back for real)."""
    g = get_graph()
    ctx = ToolContext(g, max_calls=4)
    now = int(time.time())
    try:
        ctx.call("record_action", purpose="operator approved", action_id=action_id, status="approved", at=now)
        res = ctx.call("record_action", purpose="simulated execution", action_id=action_id, status="executed", at=now)
    except ToolError as exc:
        raise HTTPException(status_code=404 if exc.kind == "unresolved" else 400, detail=str(exc))
    action = res.rows[0]
    inv_id = ACTIONS.get(action_id)
    if inv_id and inv_id in REPORTS and REPORTS[inv_id].get("action"):
        REPORTS[inv_id]["action"]["status"] = "executed"
        REPORTS[inv_id]["action"]["executed_at"] = now
    return {"action": action, "simulated": True, "message": f"SIMULATED: rollback of {action['deployment_id']} recorded as executed. No real system was touched.",
            "trace": [c.to_dict() for c in ctx.trace]}


@app.post("/api/demo/reset")
def demo_reset(memory_only: bool = False) -> dict[str, Any]:
    g = get_graph()
    REPORTS.clear()
    ACTIONS.clear()
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
