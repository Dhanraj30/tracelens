"""The TraceLens tool catalog. Every Cypher query the agent depends on lives here.

docs/QUERIES.md is generated from this module (scripts/gen_queries_md.py) so the docs can
never drift from what actually runs.
"""
from __future__ import annotations

import json
import re
from typing import Any

from .base import (
    ToolContext,
    ToolError,
    ToolResult,
    resolve_incident,
    resolve_services,
    rows_to_dicts,
    tool,
    validate_epoch,
    validate_id,
    validate_int,
    validate_name,
    validate_names,
)

AGENT_RE = re.compile(r"^[a-z][a-z0-9\-_.:]{0,63}$")  # provenance label, e.g. "engine", "mcp:langgraph"

# =======================================================================================
# 1. get_incident
# =======================================================================================
Q_GET_INCIDENT = """
MATCH (i:Incident {id: $id})-[:AFFECTS]->(s:Service)
OPTIONAL MATCH (i)-[:OBSERVED]->(e:ErrorEvent)-[:HAS_SIGNATURE]->(sig:ErrorSignature)
RETURN i.id, i.title, i.status, i.opened_at, i.symptom, s.name, s.tier, s.team,
       collect({id: e.id, at: e.at, message: e.message, count: e.count, signature: sig.key}) AS observed
"""


@tool(
    "get_incident",
    "Load an incident, the service it affects and the error events observed on it.",
    "Anchors the investigation: the symptom service and the error signatures we must explain.",
    [Q_GET_INCIDENT],
    {"incident_id": "string"},
)
def get_incident(ctx: ToolContext, incident_id: str) -> ToolResult:
    incident_id = validate_id(incident_id, "incident_id")
    resolve_incident(ctx, incident_id)
    rows, _ = ctx.graph.read(Q_GET_INCIDENT, {"id": incident_id})
    cols = ["id", "title", "status", "opened_at", "symptom", "service", "tier", "team", "observed"]
    out = rows_to_dicts(rows, cols)
    for r in out:
        r["observed"] = [o for o in r["observed"] if o.get("id")]
    ev = [out[0]["id"]] + [o["id"] for o in out[0]["observed"]] if out else []
    return ToolResult("get_incident", out, evidence_ids=ev, extra={"summary": f"{out[0]['title']} affects {out[0]['service']}" if out else "not found"})


# =======================================================================================
# 2. upstream_dependencies  (variable-length traversal; hop bound validated + inlined)
# =======================================================================================
Q_UPSTREAM = """
MATCH p = (s:Service {name: $name})-[:DEPENDS_ON*1..{depth}]->(d:Service)
RETURN d.name, d.tier, d.team, d.external, length(p) AS hops,
       [n IN nodes(p) | n.name] AS path,
       [r IN relationships(p) | r.kind] AS kinds
ORDER BY hops, d.name
"""


@tool(
    "upstream_dependencies",
    "Walk DEPENDS_ON edges from a service to everything it (transitively) relies on, up to `depth` hops.",
    "Defines the suspect space: only services on this path can be the upstream cause of the symptom.",
    [Q_UPSTREAM],
    {"service": "string", "depth": "int 1..4"},
)
def upstream_dependencies(ctx: ToolContext, service: str, depth: int = 4) -> ToolResult:
    service = validate_name(service)
    depth = validate_int(depth, "depth", 1, 4)
    resolve_services(ctx, [service])
    rows, _ = ctx.graph.read(Q_UPSTREAM.replace("{depth}", str(depth)), {"name": service})
    cols = ["service", "tier", "team", "external", "hops", "path", "kinds"]
    all_rows = rows_to_dicts(rows, cols)
    # keep the shortest path per dependency (rows are ordered by hops)
    seen: dict[str, dict[str, Any]] = {}
    for r in all_rows:
        seen.setdefault(r["service"], r)
    deps = list(seen.values())
    boundary = [r["service"] for r in deps if r.get("external")]
    return ToolResult(
        "upstream_dependencies",
        deps,
        evidence_ids=[service] + [r["service"] for r in deps],
        paths=[r["path"] for r in deps],
        extra={"root": service, "boundary_services": boundary, "summary": f"{len(deps)} upstream services within {depth} hops"},
    )


# =======================================================================================
# 3. deployments_in_window
# =======================================================================================
Q_DEPLOYMENTS = """
UNWIND $services AS nm
MATCH (s:Service {name: nm})<-[:DEPLOYED_TO]-(d:Deployment)
WHERE d.at >= $from AND d.at <= $to
OPTIONAL MATCH (d)-[:INCLUDES]->(c:CodeChange)
RETURN d.id, d.at, d.version, d.author, s.name,
       collect({id: c.id, summary: c.summary, files: c.files, risk: c.risk}) AS changes
ORDER BY d.at
"""


@tool(
    "deployments_in_window",
    "Deployments (with their CodeChanges) to any of the given services inside [from, to].",
    "Every deployment here is a candidate hypothesis. Anything not here cannot be 'the change that broke it'.",
    [Q_DEPLOYMENTS],
    {"services": "string[]", "from_ts": "epoch seconds", "to_ts": "epoch seconds"},
)
def deployments_in_window(ctx: ToolContext, services: list[str], from_ts: int, to_ts: int) -> ToolResult:
    services = validate_names(services)
    from_ts, to_ts = validate_epoch(from_ts, "from_ts"), validate_epoch(to_ts, "to_ts")
    if from_ts > to_ts:
        raise ToolError("from_ts must be <= to_ts")
    resolve_services(ctx, services)
    rows, _ = ctx.graph.read(Q_DEPLOYMENTS, {"services": services, "from": from_ts, "to": to_ts})
    out = rows_to_dicts(rows, ["id", "at", "version", "author", "service", "changes"])
    for r in out:
        r["changes"] = [c for c in r["changes"] if c.get("id")]
    ev = [r["id"] for r in out] + [c["id"] for r in out for c in r["changes"]]
    return ToolResult("deployments_in_window", out, evidence_ids=ev, extra={"summary": f"{len(out)} deployment(s) in window"})


# =======================================================================================
# 4. error_timeline
# =======================================================================================
Q_ERROR_TIMELINE = """
UNWIND $services AS nm
MATCH (s:Service {name: nm})<-[:EMITTED_BY]-(e:ErrorEvent)-[:HAS_SIGNATURE]->(sig:ErrorSignature)
WHERE e.at >= $from AND e.at <= $to
RETURN s.name, sig.key, min(e.at) AS first_seen, sum(e.count) AS total, collect(e.id) AS event_ids
ORDER BY first_seen
"""


@tool(
    "error_timeline",
    "First-seen time and volume per (service, error signature) inside [from, to].",
    "Establishes temporal order: which service failed first, and whether errors predate a deployment.",
    [Q_ERROR_TIMELINE],
    {"services": "string[]", "from_ts": "epoch seconds", "to_ts": "epoch seconds"},
)
def error_timeline(ctx: ToolContext, services: list[str], from_ts: int, to_ts: int) -> ToolResult:
    services = validate_names(services)
    from_ts, to_ts = validate_epoch(from_ts, "from_ts"), validate_epoch(to_ts, "to_ts")
    resolve_services(ctx, services)
    rows, _ = ctx.graph.read(Q_ERROR_TIMELINE, {"services": services, "from": from_ts, "to": to_ts})
    out = rows_to_dicts(rows, ["service", "signature", "first_seen", "total", "event_ids"])
    ev = [eid for r in out for eid in r["event_ids"]]
    origin = out[0]["service"] if out else None
    return ToolResult(
        "error_timeline", out, evidence_ids=ev,
        extra={"origin_service": origin, "summary": f"{len(out)} (service, signature) series; earliest at {origin}" if out else "no errors in window"},
    )


# =======================================================================================
# 5. healthy_siblings  (common-cause test)
# =======================================================================================
Q_SIBLINGS = """
MATCH (s:Service {name: $name})-[:DEPENDS_ON]->(dep:Service)<-[:DEPENDS_ON]-(sib:Service)
WHERE sib.name <> $name
OPTIONAL MATCH (sib)<-[:EMITTED_BY]-(e:ErrorEvent)
WHERE e.at >= $from AND e.at <= $to
OPTIONAL MATCH dp = (sib)-[:DEPENDS_ON*1..3]->(s)
RETURN dep.name AS shared_dependency, sib.name AS sibling, count(DISTINCT e) AS error_events,
       collect(DISTINCT e.id) AS event_ids, count(dp) > 0 AS depends_on_candidate
ORDER BY shared_dependency, sibling
"""


@tool(
    "healthy_siblings",
    "For each dependency shared with other services, list those sibling dependents and whether they emitted errors in the window.",
    "Common-cause test: if siblings of the suspect that use the same dependency are healthy, the shared dependency is not the cause.",
    [Q_SIBLINGS],
    {"service": "string", "from_ts": "epoch seconds", "to_ts": "epoch seconds"},
)
def healthy_siblings(ctx: ToolContext, service: str, from_ts: int, to_ts: int) -> ToolResult:
    service = validate_name(service)
    from_ts, to_ts = validate_epoch(from_ts, "from_ts"), validate_epoch(to_ts, "to_ts")
    resolve_services(ctx, [service])
    rows, _ = ctx.graph.read(Q_SIBLINGS, {"name": service, "from": from_ts, "to": to_ts})
    out = rows_to_dicts(rows, ["shared_dependency", "sibling", "error_events", "event_ids", "depends_on_candidate"])
    for r in out:
        r["event_ids"] = [e for e in r["event_ids"] if e]
        r["healthy"] = r["error_events"] == 0
    # A sibling that itself depends on the candidate is downstream of it: its errors are expected and
    # cannot exonerate or implicate the shared dependency. Only independent siblings count.
    independent = [r for r in out if not r["depends_on_candidate"]]
    healthy = [r["sibling"] for r in independent if r["healthy"]]
    unhealthy = [r["sibling"] for r in independent if not r["healthy"]]
    exonerated = sorted({r["shared_dependency"] for r in independent if r["healthy"]})
    implicated = sorted({r["shared_dependency"] for r in independent if not r["healthy"]} - set(exonerated))
    return ToolResult(
        "healthy_siblings", out,
        evidence_ids=[service] + [r["shared_dependency"] for r in out] + [r["sibling"] for r in out] + [e for r in out for e in r["event_ids"]],
        extra={"healthy": healthy, "unhealthy": unhealthy, "exonerated_dependencies": exonerated, "implicated_dependencies": implicated,
               "summary": f"{len(healthy)} healthy, {len(unhealthy)} unhealthy independent sibling(s); shared deps ruled out: {', '.join(exonerated) or 'none'}"},
    )


# =======================================================================================
# 6. similar_incidents  (structural match through shared ErrorSignature / Service nodes)
# =======================================================================================
Q_SIMILAR = """
MATCH (past:Incident {status: 'resolved'})-[:OBSERVED]->(e:ErrorEvent)-[:HAS_SIGNATURE]->(sig:ErrorSignature)
WHERE sig.key IN $signatures
MATCH (past)-[:AFFECTS]->(ps:Service)
OPTIONAL MATCH (past)-[:RESOLVED_BY]->(res:Resolution)
OPTIONAL MATCH (res)-[:FOLLOWED]->(rb:Runbook)
OPTIONAL MATCH (res)-[:REVERTED]->(rd:Deployment)-[:INCLUDES]->(cc:CodeChange)
OPTIONAL MATCH (rd)-[:DEPLOYED_TO]->(rds:Service)
RETURN past.id, past.title, past.opened_at, ps.name,
       collect(DISTINCT sig.key) AS shared_signatures,
       ps.name IN $services AS same_service,
       res.id, res.summary, rb.id, rb.title, rb.steps, rd.id,
       collect(DISTINCT cc.files) AS reverted_files,
       res.source AS resolution_source, res.agent AS resolution_agent, rds.name AS reverted_service
ORDER BY past.opened_at DESC
"""


@tool(
    "similar_incidents",
    "Resolved incidents reached through shared ErrorSignature nodes (and flagged if on the same Service), with their Resolution, Runbook and reverted Deployment.",
    "History check by graph structure, not text similarity: a past incident with the same signature on the same service, resolved by a rollback, is strong evidence for the same fix.",
    [Q_SIMILAR],
    {"signature_keys": "string[]", "services": "string[]"},
)
def similar_incidents(ctx: ToolContext, signature_keys: list[str], services: list[str]) -> ToolResult:
    if not isinstance(signature_keys, list) or not signature_keys:
        raise ToolError("signature_keys must be a non-empty list")
    signature_keys = [validate_id(k, "signature") for k in signature_keys]
    services = validate_names(services)
    resolve_services(ctx, services)
    rows, _ = ctx.graph.read(Q_SIMILAR, {"signatures": signature_keys, "services": services})
    cols = ["id", "title", "opened_at", "service", "shared_signatures", "same_service", "resolution_id", "resolution_summary",
            "runbook_id", "runbook_title", "runbook_steps", "reverted_deployment", "reverted_files", "resolution_source", "resolution_agent",
            "reverted_service"]
    out = rows_to_dicts(rows, cols)
    for r in out:
        r["reverted_files"] = sorted({f for files in r["reverted_files"] if files for f in files})
        r["match_links"] = ["ErrorSignature:" + k for k in r["shared_signatures"]] + (["Service:" + r["service"]] if r["same_service"] else [])
        if r.get("reverted_service") and r["reverted_service"] in services and r["reverted_service"] != r["service"]:
            r["match_links"].append(f"Resolution→REVERTED→Deployment on {r['reverted_service']}")
        r["learned_by_tracelens"] = r.get("resolution_source") == "tracelens"
    ev = [x for r in out for x in (r["id"], r["resolution_id"], r["runbook_id"], r["reverted_deployment"]) if x]
    return ToolResult("similar_incidents", out, evidence_ids=ev, extra={"summary": f"{len(out)} similar resolved incident(s)"})


# =======================================================================================
# 7. path_between  (algo.SPpaths — weighted shortest path, sync=1 / async=2)
# =======================================================================================
Q_PATH = """
MATCH (a:Service {name: $from}), (b:Service {name: $to})
CALL algo.SPpaths({sourceNode: a, targetNode: b, relTypes: ['DEPENDS_ON'], weightProp: 'weight',
                   relDirection: 'outgoing', pathCount: 1, maxLen: 6})
YIELD path, pathWeight
RETURN [n IN nodes(path) | n.name] AS path, [r IN relationships(path) | r.kind] AS kinds, pathWeight
"""


@tool(
    "path_between",
    "Weighted shortest DEPENDS_ON path from one service to another (algo.SPpaths, sync=1, async=2).",
    "Path check: a candidate can only cause the symptom if the symptom service reaches it through dependencies. Empty result = not connected.",
    [Q_PATH],
    {"from_service": "string", "to_service": "string"},
)
def path_between(ctx: ToolContext, from_service: str, to_service: str) -> ToolResult:
    from_service, to_service = validate_name(from_service), validate_name(to_service)
    resolve_services(ctx, [from_service, to_service])
    if from_service == to_service:
        return ToolResult("path_between", [{"path": [from_service], "kinds": [], "pathWeight": 0}], evidence_ids=[from_service],
                          paths=[[from_service]], extra={"connected": True, "summary": "same service"})
    rows, _ = ctx.graph.read(Q_PATH, {"from": from_service, "to": to_service})
    out = rows_to_dicts(rows, ["path", "kinds", "pathWeight"])
    return ToolResult(
        "path_between", out, evidence_ids=[n for r in out for n in r["path"]], paths=[r["path"] for r in out],
        extra={"connected": bool(out), "summary": (" -> ".join(out[0]["path"]) + f" (weight {out[0]['pathWeight']})") if out else f"no dependency path {from_service} -> {to_service}"},
    )


# =======================================================================================
# 8. critical_services  (algo.pageRank + algo.betweenness)
# =======================================================================================
Q_PAGERANK = """
CALL algo.pageRank('Service', 'DEPENDS_ON') YIELD node, score
RETURN node.name AS service, score ORDER BY score DESC
"""
Q_BETWEENNESS = """
CALL algo.betweenness({nodeLabels: ['Service'], relationshipTypes: ['DEPENDS_ON'], samplingSeed: 7}) YIELD node, score
RETURN node.name AS service, score ORDER BY score DESC
"""


@tool(
    "critical_services",
    "Load-bearing services by PageRank (how many things depend on you, transitively) and betweenness (how often you sit on dependency paths).",
    "Weights candidates: a change on a high-centrality service has a wider blast radius, so it is examined first and its score is annotated.",
    [Q_PAGERANK, Q_BETWEENNESS],
    {"limit": "int 1..50"},
)
def critical_services(ctx: ToolContext, limit: int = 10) -> ToolResult:
    limit = validate_int(limit, "limit", 1, 50)
    pr_rows, _ = ctx.graph.read(Q_PAGERANK)
    bw_rows, _ = ctx.graph.read(Q_BETWEENNESS)
    pr = {r[0]: round(float(r[1]), 4) for r in pr_rows}
    bw = {r[0]: round(float(r[1]), 2) for r in bw_rows}
    out = [{"service": s, "pagerank": pr.get(s, 0.0), "betweenness": bw.get(s, 0.0)}
           for s in sorted(set(pr) | set(bw), key=lambda x: (-(pr.get(x) or 0), x))][:limit]
    return ToolResult("critical_services", out, evidence_ids=[r["service"] for r in out],
                      extra={"pagerank": pr, "betweenness": bw, "summary": f"top by PageRank: {out[0]['service']}" if out else "no services"})


# =======================================================================================
# 9. recall  (agent memory — reads what earlier investigations wrote back)
# =======================================================================================
Q_RECALL = """
MATCH (inv:Investigation)-[:CONSIDERED]->(h:Hypothesis {status: 'supported'})-[:SUSPECTS]->(d:Deployment)-[:DEPLOYED_TO]->(s:Service)
WHERE s.name IN $services AND d.at >= $from AND d.at <= $to
MATCH (inv)-[:INVESTIGATED]->(inc:Incident)
WHERE inc.id <> $exclude_incident
OPTIONAL MATCH (inv)-[:PROPOSED]->(a:Action)
OPTIONAL MATCH (d)-[:INCLUDES]->(cc:CodeChange)
RETURN inv.id, inv.at, inv.outcome, inc.id, inc.title, h.id, h.score, h.confirmed, d.id, d.at, d.version, s.name,
       a.id, a.status, a.kind, a.runbook_id, collect({id: cc.id, summary: cc.summary, risk: cc.risk}) AS changes,
       coalesce(inv.agent, 'engine') AS agent, coalesce(inv.verified, true) AS verified
ORDER BY inv.at DESC
"""


@tool(
    "recall",
    "Prior investigations of OTHER incidents whose supported root-cause hypothesis points at a deployment on one of these services inside the window.",
    "Memory: a root cause the agent already established (and a human may have confirmed) on the same dependency path can be reused instead of re-derived. Knowledge transfers across incidents; re-investigating the same incident always re-derives.",
    [Q_RECALL],
    {"services": "string[]", "from_ts": "epoch seconds", "to_ts": "epoch seconds", "exclude_incident": "string (the incident being investigated)"},
)
def recall(ctx: ToolContext, services: list[str], from_ts: int, to_ts: int, exclude_incident: str = "") -> ToolResult:
    services = validate_names(services)
    from_ts, to_ts = validate_epoch(from_ts, "from_ts"), validate_epoch(to_ts, "to_ts")
    exclude_incident = validate_id(exclude_incident, "exclude_incident") if exclude_incident else ""
    resolve_services(ctx, services)
    rows, _ = ctx.graph.read(Q_RECALL, {"services": services, "from": from_ts, "to": to_ts, "exclude_incident": exclude_incident})
    cols = ["investigation_id", "learned_at", "outcome", "incident_id", "incident_title", "hypothesis_id", "score", "confirmed",
            "deployment_id", "deployment_at", "deployment_version", "service", "action_id", "action_status", "action_kind",
            "runbook_id", "changes", "agent", "verified"]
    out = rows_to_dicts(rows, cols)
    for r in out:
        r["changes"] = [c for c in r["changes"] if c.get("id")]
    ev = [x for r in out for x in (r["investigation_id"], r["hypothesis_id"], r["deployment_id"], r["incident_id"]) if x]
    return ToolResult("recall", out, evidence_ids=ev, extra={"summary": f"{len(out)} prior investigation(s) recalled"})


# =======================================================================================
# 10. record_investigation / record_action  (the ONLY write tools)
# =======================================================================================
Q_RECORD_INVESTIGATION = """
MATCH (inc:Incident {id: $incident_id})
MERGE (inv:Investigation {id: $id})
SET inv.at = $at, inv.mode = $mode, inv.outcome = $outcome, inv.tool_calls = $tool_calls, inv.confidence = $confidence,
    inv.agent = $agent, inv.trace = $trace, inv.verified = $verified
MERGE (inv)-[:INVESTIGATED]->(inc)
"""
Q_RECORD_HYPOTHESES = """
MATCH (inv:Investigation {id: $id})
UNWIND $hyps AS h
MERGE (hy:Hypothesis {id: h.id})
SET hy.status = h.status, hy.score = h.score, hy.kind = h.kind, hy.label = h.label, hy.confirmed = false
MERGE (inv)-[:CONSIDERED]->(hy)
"""
Q_RECORD_SUSPECTS = """
UNWIND $links AS l
MATCH (hy:Hypothesis {id: l.hypothesis_id})
MATCH (n) WHERE (n:Deployment OR n:CodeChange OR n:Service) AND (n.id = l.target OR n.name = l.target)
MERGE (hy)-[:SUSPECTS]->(n)
"""
Q_RECORD_SUPPORTED = """
UNWIND $links AS l
MATCH (hy:Hypothesis {id: l.hypothesis_id})
MATCH (n) WHERE (n:ErrorEvent OR n:Deployment OR n:Incident OR n:Service) AND (n.id = l.target OR n.name = l.target)
MERGE (hy)-[r:SUPPORTED_BY {check: l.check}]->(n)
"""
Q_RECORD_REJECTED = """
UNWIND $links AS l
MATCH (hy:Hypothesis {id: l.hypothesis_id})
MATCH (n) WHERE (n:ErrorEvent OR n:Deployment OR n:Incident OR n:Service) AND (n.id = l.target OR n.name = l.target)
MERGE (hy)-[r:REJECTED_BY {check: l.check}]->(n)
"""
Q_RECORD_ACTION = """
MATCH (inv:Investigation {id: $investigation_id})
MERGE (a:Action {id: $id})
SET a.kind = $kind, a.status = 'proposed', a.summary = $summary, a.deployment_id = $deployment_id, a.runbook_id = $runbook_id, a.simulated = true
MERGE (inv)-[:PROPOSED]->(a)
WITH a
OPTIONAL MATCH (d:Deployment {id: $deployment_id})
FOREACH (_ IN CASE WHEN d IS NULL THEN [] ELSE [1] END | MERGE (a)-[:TARGETS]->(d))
WITH a
OPTIONAL MATCH (rb:Runbook {id: $runbook_id})
FOREACH (_ IN CASE WHEN rb IS NULL THEN [] ELSE [1] END | MERGE (a)-[:FOLLOWS]->(rb))
"""
Q_ACTION_STATUS = """
MATCH (inv:Investigation)-[:PROPOSED]->(a:Action {id: $id})
SET a.status = $status, a.updated_at = $at
WITH inv, a
OPTIONAL MATCH (inv)-[:CONSIDERED]->(h:Hypothesis {status: 'supported'})
SET h.confirmed = CASE WHEN $status IN ['approved', 'executed'] THEN true ELSE h.confirmed END
RETURN a.id, a.status, a.kind, a.summary, a.deployment_id, a.runbook_id, a.simulated, inv.id
"""
# Learning from approvals: an executed (simulated) action becomes a Resolution on the incident, linked to the
# deployment it reverted and the runbook it followed — exactly the structure `similar_incidents` traverses.
Q_ACTION_RESOLUTION = """
MATCH (inv:Investigation)-[:PROPOSED]->(a:Action {id: $id}), (inv)-[:INVESTIGATED]->(inc:Incident)
MERGE (res:Resolution {id: 'res-' + $id})
SET res.summary = $summary, res.at = $at, res.source = 'tracelens', res.agent = coalesce(inv.agent, 'engine'), res.investigation_id = inv.id
MERGE (inc)-[:RESOLVED_BY]->(res)
SET inc.status = 'resolved', inc.resolved_at = $at
WITH res, a, inc
OPTIONAL MATCH (d:Deployment {id: a.deployment_id})
FOREACH (_ IN CASE WHEN d IS NULL THEN [] ELSE [1] END | MERGE (res)-[:REVERTED]->(d))
WITH res, a, inc
OPTIONAL MATCH (rb:Runbook {id: a.runbook_id})
FOREACH (_ IN CASE WHEN rb IS NULL THEN [] ELSE [1] END | MERGE (res)-[:FOLLOWED]->(rb))
RETURN res.id, inc.id
"""


@tool(
    "record_investigation",
    "Write back the Investigation, its Hypotheses (supported AND rejected), SUSPECTS edges and SUPPORTED_BY/REJECTED_BY evidence edges, plus the proposed Action.",
    "This is the memory. The next investigation's `recall` reads exactly these nodes — no seed data involved.",
    [Q_RECORD_INVESTIGATION, Q_RECORD_HYPOTHESES, Q_RECORD_SUSPECTS, Q_RECORD_SUPPORTED, Q_RECORD_REJECTED, Q_RECORD_ACTION],
    {"investigation": "object"},
    writes=True,
)
def record_investigation(ctx: ToolContext, investigation: dict[str, Any]) -> ToolResult:
    inv = investigation
    inv_id = validate_id(inv["id"], "investigation id")
    incident_id = validate_id(inv["incident_id"], "incident_id")
    resolve_incident(ctx, incident_id)
    g = ctx.graph
    agent = str(inv.get("agent") or "engine")
    if not AGENT_RE.match(agent):
        raise ToolError(f"invalid agent name {agent!r}", kind="validation")
    trace = inv.get("trace") or []
    trace_json = json.dumps(trace, default=str)[:60000]  # compact per-step summary, kept on the node for the history view
    g.write(Q_RECORD_INVESTIGATION, {"id": inv_id, "incident_id": incident_id, "at": int(inv["at"]), "mode": str(inv["mode"]),
                                     "outcome": str(inv["outcome"]), "tool_calls": int(inv.get("tool_calls", 0)),
                                     "confidence": float(inv.get("confidence") or 0.0), "agent": agent, "trace": trace_json,
                                     "verified": bool(inv.get("verified", agent == "engine"))})
    hyps = [{"id": validate_id(h["id"], "hypothesis id"), "status": h["status"], "score": float(h.get("score") or 0.0),
             "kind": h["kind"], "label": h["label"]} for h in inv.get("hypotheses", [])]
    if hyps:
        g.write(Q_RECORD_HYPOTHESES, {"id": inv_id, "hyps": hyps})
        suspects = [{"hypothesis_id": h["id"], "target": validate_id(h["suspect"], "suspect")} for h in inv["hypotheses"] if h.get("suspect")]
        if suspects:
            g.write(Q_RECORD_SUSPECTS, {"links": suspects})
        supported, rejected = [], []
        for h in inv["hypotheses"]:
            for chk in h.get("checks", []):
                for ev in chk.get("evidence_ids", []):
                    link = {"hypothesis_id": h["id"], "target": validate_id(ev, "evidence id"), "check": chk["name"]}
                    (supported if chk["passed"] else rejected).append(link)
        if supported:
            g.write(Q_RECORD_SUPPORTED, {"links": supported})
        if rejected:
            g.write(Q_RECORD_REJECTED, {"links": rejected})
    action = inv.get("action")
    if action:
        g.write(Q_RECORD_ACTION, {"investigation_id": inv_id, "id": validate_id(action["id"], "action id"), "kind": action["kind"],
                                  "summary": action["summary"], "deployment_id": action.get("deployment_id") or "",
                                  "runbook_id": action.get("runbook_id") or ""})
    return ToolResult("record_investigation", [{"investigation_id": inv_id, "hypotheses": len(hyps), "action": bool(action)}],
                      evidence_ids=[inv_id], extra={"summary": f"wrote {inv_id} with {len(hyps)} hypotheses"})


@tool(
    "record_action",
    "Update an Action's status (proposed -> approved -> executed). Execution is SIMULATED. Approval marks the supported hypothesis as human-confirmed; "
    "execution also writes a Resolution on the incident (RESOLVED_BY / REVERTED / FOLLOWED) and marks the incident resolved.",
    "Closes the loop: a human-approved action is recorded on the graph and becomes stronger memory. The Resolution it writes is reachable by "
    "`similar_incidents` through the shared ErrorSignature, so TraceLens learns its own fixes.",
    [Q_ACTION_STATUS, Q_ACTION_RESOLUTION],
    {"action_id": "string", "status": "'approved' | 'executed' | 'rejected'", "at": "epoch seconds"},
    writes=True,
)
def record_action(ctx: ToolContext, action_id: str, status: str, at: int) -> ToolResult:
    action_id = validate_id(action_id, "action_id")
    if status not in ("approved", "executed", "rejected"):
        raise ToolError("status must be approved, executed or rejected")
    at = validate_epoch(at, "at")
    rows, _ = ctx.graph.write(Q_ACTION_STATUS, {"id": action_id, "status": status, "at": at})
    if not rows:
        raise ToolError(f"unknown action: {action_id}", kind="unresolved", unresolved=[action_id])
    out = rows_to_dicts(rows, ["id", "status", "kind", "summary", "deployment_id", "runbook_id", "simulated", "investigation_id"])
    extra: dict[str, Any] = {"summary": f"{action_id} -> {status}"}
    if status == "executed":
        a = out[0]
        summary = f"{a['summary']} (simulated execution approved by operator; TraceLens investigation {a['investigation_id']})"
        rrows, _ = ctx.graph.write(Q_ACTION_RESOLUTION, {"id": action_id, "summary": summary, "at": at})
        if rrows:
            out[0]["resolution_id"], out[0]["incident_id"] = rrows[0][0], rrows[0][1]
            extra["summary"] += f"; wrote {rrows[0][0]} on {rrows[0][1]} (now resolved)"
    return ToolResult("record_action", out, evidence_ids=[action_id] + [out[0].get("resolution_id")] if out[0].get("resolution_id") else [action_id], extra=extra)
