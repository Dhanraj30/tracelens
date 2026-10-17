"""The TraceLens investigation engine: an explicit, bounded state machine.

States (in order):  RESOLVE -> SCOPE -> RECALL -> GATHER -> HYPOTHESIZE -> FALSIFY -> DECIDE -> ACT -> REMEMBER

Every conclusion is derived from tool results (graph queries / algorithms). Nothing in here
knows the ground truth; tests/scenarios.yaml is the only place it is written down.

Checks per hypothesis (a hypothesis = "this Deployment caused the symptom"):
  HARD (a failure rejects the hypothesis)
    temporal_precedence  symptom errors must start AFTER the deployment
    path                 the symptom service must reach the candidate through DEPENDS_ON (algo.SPpaths)
    origin               the earliest errors in the window must sit at/below the candidate and after it
  SOFT (count toward confidence)
    common_cause         independent siblings sharing the candidate's dependencies are healthy
    history              a resolved past incident shares an ErrorSignature with the candidate's service

Confidence = passed / total checks — a share, NOT a probability. We never say "proven".
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional

from ..db import Graph
from ..tools import ToolContext, ToolError, ToolResult
from ..tools.base import RESOLVE_INCIDENT, RESOLVE_SERVICES, validate_id, validate_name
from . import llm as llm_adapter
from .narrative import NarrativeValidator, build_narrative, fmt_ts

WINDOW_SECONDS = 3600  # look back one hour before the incident was opened
MAX_TOOL_CALLS = 14
SUPPORT_THRESHOLD = 0.6

OUTCOMES = ("root_cause_supported", "multiple_candidates", "inconclusive", "answered_from_memory", "unknown_entity")


@dataclass
class Check:
    name: str
    passed: Optional[bool]  # None = could not be evaluated (inconclusive)
    detail: str
    evidence_ids: list[str] = field(default_factory=list)
    hard: bool = False
    tool_step: Optional[int] = None

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class Hypothesis:
    id: str
    kind: str  # deployment | shared_dependency | memory
    label: str
    suspect: Optional[str]  # Deployment id / Service name
    service: str
    status: str = "pending"  # supported | rejected | inconclusive
    score: float = 0.0
    checks: list[Check] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = self.__dict__.copy()
        d["checks"] = [c.to_dict() for c in self.checks]
        return d


class Investigator:
    def __init__(self, graph: Graph, mode: str = "deterministic", verify_only: list[str] | None = None):
        """verify_only: deployment IDs proposed by an outside agent. The engine then skips memory, restricts the
        hypotheses to exactly those candidates, runs the same checks, and does NOT write anything back."""
        if mode not in ("deterministic", "llm"):
            raise ValueError("mode must be 'deterministic' or 'llm'")
        self.graph = graph
        self.mode = mode
        self.verify_only = verify_only
        self.ctx = ToolContext(graph, max_calls=MAX_TOOL_CALLS)
        self.states: list[dict[str, Any]] = []
        self.report: dict[str, Any] = {}
        self._t0 = time.perf_counter()

    # ------------------------------------------------------------------ helpers
    def _state(self, name: str, note: str = "") -> None:
        self.states.append({"state": name, "at_ms": round((time.perf_counter() - self._t0) * 1000, 1), "note": note})

    def _call(self, tool: str, purpose: str, **args: Any) -> ToolResult:
        return self.ctx.call(tool, purpose=purpose, **args)

    # ------------------------------------------------------------------ entry point
    def run(self, incident_id: str | None = None, service: str | None = None) -> dict[str, Any]:
        inv_id = f"inv-{int(time.time() * 1000)}"
        now = int(time.time())
        self.report = {
            "id": inv_id, "at": now, "mode": self.mode, "outcome": None, "incident": None, "window": None,
            "hypotheses": [], "root_cause": None, "similar_incidents": [], "action": None, "memory": None,
            "missing_data": [], "checked": [], "confidence": {"passed": 0, "total": 0, "share": 0.0,
                                                               "note": "share of graph checks passed — not a probability"},
            "narrative": "", "narrative_source": "template", "llm_fallbacks": [], "graph_highlights": {},
            "states": self.states, "trace": [], "tool_calls": 0, "resolution": None,
        }

        # ---------------- RESOLVE: nothing runs until the entity exists in the graph -----------
        self._state("RESOLVE")
        try:
            incident_id = self._resolve(incident_id, service)
        except ToolError as exc:
            self.report.update({"outcome": "unknown_entity", "error": str(exc),
                                "incident": {"id": incident_id or service, "title": "unknown", "symptom": "", "service": service or "", "opened_at": None}})
            self.report["narrative"] = build_narrative(self.report)
            return self._finish()

        try:
            self._investigate(incident_id, inv_id, now)
        except ToolError as exc:
            # bounded failure: report what we have, honestly
            self.report["outcome"] = self.report.get("outcome") or "inconclusive"
            self.report["error"] = f"{exc.kind}: {exc}"
            self.report["missing_data"].append(f"investigation stopped early ({exc.kind}): {exc}")
            self.report["narrative"] = build_narrative(self.report)
        return self._finish()

    def _resolve(self, incident_id: str | None, service: str | None) -> str:
        if incident_id:
            incident_id = validate_id(incident_id, "incident_id")
            rows, rec = self.graph.read(RESOLVE_INCIDENT, {"id": incident_id})
            self.report["resolution"] = {"kind": "incident", "value": incident_id, "found": bool(rows and int(rows[0][0]) > 0), "query": rec.to_dict()}
            if not self.report["resolution"]["found"]:
                raise ToolError(f"incident '{incident_id}' does not exist in the graph", kind="unresolved", unresolved=[incident_id])
            return incident_id
        if service:
            service = validate_name(service)
            rows, rec = self.graph.read(RESOLVE_SERVICES, {"names": [service]})
            found = bool(rows and service in rows[0][0])
            self.report["resolution"] = {"kind": "service", "value": service, "found": found, "query": rec.to_dict()}
            if not found:
                raise ToolError(f"service '{service}' does not exist in the graph", kind="unresolved", unresolved=[service])
            rows, _ = self.graph.read(
                "MATCH (i:Incident {status: 'open'})-[:AFFECTS]->(s:Service {name: $name}) RETURN i.id ORDER BY i.opened_at DESC LIMIT 1",
                {"name": service},
            )
            if not rows:
                raise ToolError(f"no open incident affects '{service}'", kind="unresolved", unresolved=[service])
            return str(rows[0][0])
        raise ToolError("incident_id or service is required", kind="validation")

    # ------------------------------------------------------------------ the state machine
    def _investigate(self, incident_id: str, inv_id: str, now: int) -> None:
        rep = self.report

        # ---------------- SCOPE ----------------------------------------------------------------
        self._state("SCOPE", "load incident, walk upstream dependencies")
        inc = self._call("get_incident", "anchor the investigation on the symptom", incident_id=incident_id).rows[0]
        affected = inc["service"]
        window = {"from": int(inc["opened_at"]) - WINDOW_SECONDS, "to": int(inc["opened_at"])}
        rep["incident"] = inc
        rep["window"] = window
        up = self._call("upstream_dependencies", f"everything {affected} relies on (suspect space)", service=affected, depth=4)
        dep_services = [r["service"] for r in up.rows]
        path_services = [affected] + dep_services
        all_paths: list[list[str]] = up.paths
        boundary = up.extra.get("boundary_services", [])
        highlights = {"visited": list(path_services), "suspects": [], "rejected": [], "root_path": [], "boundary": boundary, "healthy": []}
        rep["graph_highlights"] = highlights

        # ---------------- RECALL ---------------------------------------------------------------
        if self.verify_only is None:
            self._state("RECALL", "check memory before deriving anything")
            mem = self._call("recall", "prior supported root causes on this dependency path (other incidents)", services=path_services,
                             from_ts=window["from"], to_ts=window["to"], exclude_incident=incident_id)
            if mem.rows and self._answer_from_memory(mem.rows[0], inc, affected, window, inv_id, now, all_paths):
                return

        # ---------------- GATHER ---------------------------------------------------------------
        self._state("GATHER", "deployments + error timeline on the path, centrality weights")
        deps = self._call("deployments_in_window", "candidate changes on the dependency path", services=path_services, from_ts=window["from"], to_ts=window["to"]).rows
        timeline = self._call("error_timeline", "who failed first, and when", services=path_services, from_ts=window["from"], to_ts=window["to"])
        errors = timeline.rows
        central = self._call("critical_services", "PageRank/betweenness to weight candidates", limit=20)
        pagerank: dict[str, float] = central.extra.get("pagerank", {})

        first_symptom = min((r["first_seen"] for r in errors if r["service"] == affected), default=None)
        origin = errors[0]["service"] if errors else None
        origin_first = errors[0]["first_seen"] if errors else None
        rep["first_symptom_at"] = first_symptom
        rep["origin"] = {"service": origin, "first_seen": origin_first, "signature": errors[0]["signature"] if errors else None}
        rep["error_timeline"] = errors
        rep["deployments"] = deps

        # ---------------- HYPOTHESIZE ----------------------------------------------------------
        hyps: list[Hypothesis] = []
        off_path: list[Hypothesis] = []
        if self.verify_only is not None:
            # verifying an outside agent's claim: only its suspects, and a suspect that is not even a deployment
            # on the dependency path inside the window fails immediately
            self._state("HYPOTHESIZE", f"verifying {len(self.verify_only)} suspect(s) proposed by an outside agent")
            by_id = {d["id"]: d for d in deps}
            for sid in self.verify_only:
                if sid not in by_id:
                    h = Hypothesis(id=f"{inv_id}-h{len(off_path) + 1}", kind="deployment", label=f"{sid} (not a deployment on the dependency path in the window)",
                                   suspect=sid, service=affected)
                    h.checks.append(Check("in_window", False, f"{sid} is not a deployment to {affected} or any of its dependencies between {fmt_ts(window['from'])} and {fmt_ts(window['to'])}",
                                          [sid, affected], hard=True))
                    off_path.append(h)
            deps = [by_id[s] for s in self.verify_only if s in by_id]
        else:
            self._state("HYPOTHESIZE", f"{len(deps)} deployment candidate(s) + shared-dependency failure")
        # most central service first, so the most load-bearing candidate is examined first
        for n, d in enumerate(sorted(deps, key=lambda d: -(pagerank.get(d["service"]) or 0)), start=len(off_path) + 1):
            hyps.append(Hypothesis(
                id=f"{inv_id}-h{n}", kind="deployment", label=f"{d['id']} ({d['service']} {d['version']} at {fmt_ts(d['at'])})",
                suspect=d["id"], service=d["service"],
                details={"deployment": d, "pagerank": pagerank.get(d["service"]), "risk": max((c.get("risk") or "low" for c in d["changes"]), key=_risk_rank, default="low")},
            ))
        shared_hyp: Hypothesis | None = None
        if self.verify_only is None:
            shared_deps = self._shared_dependency_candidates(origin or affected, all_paths, dep_services, up.rows)
            shared_hyp = Hypothesis(
                id=f"{inv_id}-h{len(hyps) + 1}", kind="shared_dependency",
                label="shared dependency failure" + (f" ({', '.join(shared_deps)})" if shared_deps else ""),
                suspect=shared_deps[0] if len(shared_deps) == 1 else None, service=origin or affected, details={"dependencies": shared_deps},
            )
            hyps.append(shared_hyp)
        hyps.extend(off_path)
        highlights["suspects"] = [h.service for h in hyps if h.kind == "deployment"]

        # ---------------- FALSIFY --------------------------------------------------------------
        self._state("FALSIFY", "graph checks per hypothesis; failures become REJECTED_BY edges")
        symptom_event_ids = [eid for r in errors if r["service"] == affected for eid in r["event_ids"]]
        origin_event_ids = [eid for r in errors if r["service"] == origin for eid in r["event_ids"]]

        for h in [x for x in hyps if x.kind == "deployment" and "deployment" in x.details]:
            d = h.details["deployment"]
            # temporal precedence (hard)
            if first_symptom is None:
                h.checks.append(Check("temporal_precedence", None, f"no errors recorded on {affected} in the window", [d["id"]], hard=True))
            elif d["at"] <= first_symptom:
                h.checks.append(Check("temporal_precedence", True, f"deployed {fmt_ts(d['at'])}, {affected} errors began {fmt_ts(first_symptom)} — change precedes symptom",
                                      [d["id"]] + symptom_event_ids[:2], hard=True))
            else:
                h.checks.append(Check("temporal_precedence", False, f"{affected} errors began {fmt_ts(first_symptom)}, BEFORE this deployment at {fmt_ts(d['at'])} — the symptom predates the change",
                                      [d["id"]] + symptom_event_ids[:2], hard=True))
            # path (hard) — algo.SPpaths
            if h.service == affected:
                h.checks.append(Check("path", True, f"deployment is on the symptom service {affected} itself", [affected], hard=True))
            else:
                p = self._call("path_between", f"is {h.service} reachable from {affected}?", from_service=affected, to_service=h.service)
                if p.extra.get("connected"):
                    h.checks.append(Check("path", True, "dependency path " + " → ".join(p.rows[0]["path"]) + f" (weight {p.rows[0]['pathWeight']:g})", p.rows[0]["path"], hard=True, tool_step=self.ctx.tool_calls))
                    h.details["path"] = p.rows[0]["path"]
                else:
                    h.checks.append(Check("path", False, f"no DEPENDS_ON path from {affected} to {h.service}", [affected, h.service], hard=True, tool_step=self.ctx.tool_calls))
            # origin (hard) — derived from the traversal rows already returned
            if origin is None:
                h.checks.append(Check("origin", None, "no errors in window to locate an origin", [], hard=True))
            else:
                reach = _reaches(h.service, origin, affected, all_paths)
                after = d["at"] <= origin_first
                if reach and after:
                    h.checks.append(Check("origin", True, f"earliest errors ({errors[0]['signature']} at {fmt_ts(origin_first)}) sit at {origin}, " + ("the candidate itself" if origin == h.service else f"downstream of {h.service}") + ", after the deployment",
                                          [origin] + origin_event_ids[:1] + [d["id"]], hard=True))
                elif not reach:
                    h.checks.append(Check("origin", False, f"earliest errors sit at {origin}, which {h.service} does not reach — the failure did not start under this candidate",
                                          [origin, h.service] + origin_event_ids[:1], hard=True))
                else:
                    h.checks.append(Check("origin", False, f"earliest errors at {origin} began {fmt_ts(origin_first)}, before this deployment ({fmt_ts(d['at'])})",
                                          [origin, d["id"]] + origin_event_ids[:1], hard=True))

        # soft checks only for survivors (saves tool budget, and rejected suspects need no more evidence)
        survivors = [h for h in hyps if h.kind == "deployment" and all(c.passed for c in h.checks if c.hard)]
        sibling_results: dict[str, ToolResult] = {}
        for h in survivors:
            sib = sibling_results.get(h.service) or self._call("healthy_siblings", f"common-cause test around {h.service}", service=h.service, from_ts=window["from"], to_ts=window["to"])
            sibling_results[h.service] = sib
            exon, impl = sib.extra.get("exonerated_dependencies", []), sib.extra.get("implicated_dependencies", [])
            if not sib.rows:
                h.checks.append(Check("common_cause", None, f"{h.service} shares no dependency with another service — nothing to compare", [h.service], tool_step=self.ctx.tool_calls))
            elif impl:
                h.checks.append(Check("common_cause", False, f"independent siblings using {', '.join(impl)} also fail — points to a shared dependency, not this change",
                                      [h.service] + impl + sib.extra.get("unhealthy", []), tool_step=self.ctx.tool_calls))
            else:
                h.checks.append(Check("common_cause", True, f"independent siblings {', '.join(sib.extra.get('healthy', []))} are healthy — shared dependencies {', '.join(exon)} ruled out as common cause",
                                      [h.service] + exon + sib.extra.get("healthy", []), tool_step=self.ctx.tool_calls))
                highlights["healthy"] = sorted(set(highlights["healthy"]) | set(sib.extra.get("healthy", [])))

        signatures = sorted({r["signature"] for r in errors})
        sims: list[dict[str, Any]] = []
        if signatures:
            sims = self._call("similar_incidents", "past incidents reached through shared ErrorSignature nodes", signature_keys=signatures, services=path_services).rows
        rep["similar_incidents"] = sims
        for h in survivors:
            match = [s for s in sims if s["service"] == h.service]
            if match:
                s = match[0]
                files_now = {f for c in h.details["deployment"]["changes"] for f in (c.get("files") or [])}
                overlap = sorted(files_now & set(s.get("reverted_files") or []))
                detail = f"incident {s['id']} on {h.service} shared signature {', '.join(s['shared_signatures'])} and was resolved by {s.get('resolution_summary') or 'a recorded resolution'}"
                if overlap:
                    detail += f"; the reverted change touched the same file(s): {', '.join(overlap)}"
                h.checks.append(Check("history", True, detail, [s["id"]] + [x for x in (s.get("resolution_id"), s.get("runbook_id"), s.get("reverted_deployment")) if x], tool_step=self.ctx.tool_calls))
                h.details["similar_incident"] = s
            else:
                h.checks.append(Check("history", False, f"no resolved incident shares a signature with {h.service}", [h.service], tool_step=self.ctx.tool_calls))

        # shared-dependency hypothesis
        if shared_hyp is not None:
            self._check_shared_dependency(shared_hyp, origin, affected, errors, sibling_results, window, boundary, up.rows)

        # ---------------- DECIDE ---------------------------------------------------------------
        self._state("DECIDE", "deterministic score = passed / total checks")
        for h in hyps:
            _score(h)
        supported = [h for h in hyps if h.status == "supported"]
        rejected = [h for h in hyps if h.status == "rejected"]
        highlights["rejected"] = [h.service for h in rejected if h.kind == "deployment"]
        rep["hypotheses"] = [h.to_dict() for h in sorted(hyps, key=lambda h: (-(h.status == "supported"), -h.score))]

        if len(supported) == 1:
            rep["outcome"] = "root_cause_supported"
            best = supported[0]
            d = best.details["deployment"]
            rep["root_cause"] = {"deployment_id": d["id"], "version": d["version"], "author": d["author"], "deployment_at": d["at"], "service": best.service,
                                 "changes": d["changes"], "path": best.details.get("path") or [affected] if best.service == affected else best.details.get("path"),
                                 "hypothesis_id": best.id}
            highlights["root_path"] = rep["root_cause"]["path"] or [affected]
            rep["confidence"] = _confidence(best)
        elif len(supported) > 1:
            rep["outcome"] = "multiple_candidates"
            rep["confidence"] = _confidence(supported[0])
        else:
            rep["outcome"] = "inconclusive"
            rep["missing_data"] = self._missing_data(deps, errors, boundary, affected, origin, shared_hyp) if shared_hyp else []
        rep["checked"] = [{"step": c.step, "tool": c.tool, "summary": c.summary, "rows": c.row_count} for c in self.ctx.trace]

        # ---------------- ACT ------------------------------------------------------------------
        if rep["outcome"] == "root_cause_supported":
            self._state("ACT", "propose remediation tied to the deployment + runbook (simulated; needs approval)")
            rep["action"] = self._propose_action(inv_id, rep["root_cause"], supported[0])

        if self.verify_only is not None:
            self._state("VERIFIED", "server-side verification of an outside agent's claim — nothing written")
            rep["narrative"] = build_narrative(rep)
            return

        # ---------------- REMEMBER -------------------------------------------------------------
        self._state("REMEMBER", "write Investigation, Hypotheses and evidence edges back to the graph")
        self._remember(inv_id, incident_id, now, hyps)
        self._narrate()

    # ------------------------------------------------------------------ memory path
    def _answer_from_memory(self, m: dict[str, Any], inc: dict[str, Any], affected: str, window: dict[str, int], inv_id: str, now: int, all_paths: list[list[str]]) -> bool:
        """Reuse an earlier supported root cause if it still fits THIS incident's timeline."""
        rep = self.report
        tl = self._call("error_timeline", f"re-verify: did {affected} fail after the remembered deployment?", services=[affected], from_ts=window["from"], to_ts=window["to"])
        first_symptom = tl.rows[0]["first_seen"] if tl.rows else None
        if first_symptom is None or m["deployment_at"] > first_symptom:
            self.states[-1]["note"] += " — memory did not fit the timeline; continuing full investigation"
            return False
        rep["first_symptom_at"] = first_symptom
        path = next((p for p in all_paths if p[-1] == m["service"]), [affected, m["service"]])
        h = Hypothesis(id=f"{inv_id}-h1", kind="memory", label=f"{m['deployment_id']} ({m['service']}) — from investigation {m['investigation_id']}",
                       suspect=m["deployment_id"], service=m["service"], details={"memory": m, "path": path})
        h.checks.append(Check("memory", True, f"investigation {m['investigation_id']} by {m.get('agent') or 'engine'} (incident {m['incident_id']}) supported this deployment with score {m['score']:.2f}"
                              + ("; operator approved the remediation" if m.get("confirmed") else ""), [m["investigation_id"], m["hypothesis_id"], m["incident_id"]]))
        h.checks.append(Check("path", True, "dependency path " + " → ".join(path), path, hard=True))
        h.checks.append(Check("temporal_precedence", True, f"{affected} errors began {fmt_ts(first_symptom)}, after the deployment at {fmt_ts(m['deployment_at'])}",
                              [m["deployment_id"]] + tl.rows[0]["event_ids"][:2], hard=True, tool_step=self.ctx.tool_calls))
        _score(h)
        rep["hypotheses"] = [h.to_dict()]
        rep["outcome"] = "answered_from_memory"
        rep["memory"] = {"used": True, "investigation_id": m["investigation_id"], "incident_id": m["incident_id"], "incident_title": m["incident_title"],
                         "learned_at": m["learned_at"], "confirmed": bool(m.get("confirmed")), "action_status": m.get("action_status"),
                         "agent": m.get("agent") or "engine", "verified": bool(m.get("verified", True))}
        rep["root_cause"] = {"deployment_id": m["deployment_id"], "version": m.get("deployment_version"), "deployment_at": m["deployment_at"], "service": m["service"],
                             "changes": m.get("changes", []), "path": path, "hypothesis_id": h.id}
        rep["confidence"] = _confidence(h)
        rep["graph_highlights"].update({"suspects": [m["service"]], "root_path": path})
        rep["checked"] = [{"step": c.step, "tool": c.tool, "summary": c.summary, "rows": c.row_count} for c in self.ctx.trace]
        self._state("ACT", "re-propose the remembered remediation")
        rep["action"] = self._propose_action(inv_id, rep["root_cause"], h, runbook_id=m.get("runbook_id"))
        self._state("REMEMBER", "record this investigation (answered from memory)")
        self._remember(inv_id, inc["id"], now, [h])
        self._narrate()
        return True

    # ------------------------------------------------------------------ pieces
    def _shared_dependency_candidates(self, origin: str, all_paths: list[list[str]], dep_services: list[str], up_rows: list[dict[str, Any]]) -> list[str]:
        """Datastore/infra/external dependencies directly under the origin service (from the traversal rows)."""
        out: list[str] = []
        tiers = {r["service"]: r.get("tier") for r in up_rows}
        for p in all_paths:
            if origin in p:
                i = p.index(origin)
                if i + 1 < len(p):
                    nxt = p[i + 1]
                    if tiers.get(nxt) in ("datastore", "infra", "external") and nxt not in out:
                        out.append(nxt)
        return out

    def _check_shared_dependency(self, h: Hypothesis, origin: str | None, affected: str, errors: list[dict[str, Any]], sibling_results: dict[str, ToolResult],
                                 window: dict[str, int], boundary: list[str], up_rows: list[dict[str, Any]]) -> None:
        deps = h.details["dependencies"]
        if not deps:
            h.checks.append(Check("common_cause", None, f"{origin or affected} has no datastore/infra/external dependency on the path", [affected]))
            return
        sib = sibling_results.get(origin or affected)
        if sib is None and origin:
            sib = self._call("healthy_siblings", f"do other users of {', '.join(deps)} also fail?", service=origin, from_ts=window["from"], to_ts=window["to"])
        exon = set(sib.extra.get("exonerated_dependencies", [])) if sib else set()
        impl = set(sib.extra.get("implicated_dependencies", [])) if sib else set()
        for dep in deps:
            dep_errors = [r for r in errors if r["service"] == dep]
            dep_sibs = [r for r in (sib.rows if sib else []) if r["shared_dependency"] == dep and not r["depends_on_candidate"]]
            healthy_here = [r["sibling"] for r in dep_sibs if r["healthy"]]
            unhealthy_here = [r["sibling"] for r in dep_sibs if not r["healthy"]]
            if dep in impl:
                h.checks.append(Check("common_cause", True, f"independent siblings using {dep} are also failing ({', '.join(unhealthy_here)})", [dep] + unhealthy_here, tool_step=self.ctx.tool_calls))
            elif dep in exon:
                h.checks.append(Check("common_cause", False, f"{dep} is shared with healthy sibling(s) {', '.join(healthy_here)} — not a common cause",
                                      [dep] + healthy_here, hard=True, tool_step=self.ctx.tool_calls))
            elif dep_errors:
                h.checks.append(Check("origin", True, f"{dep} itself emitted {dep_errors[0]['signature']} at {fmt_ts(dep_errors[0]['first_seen'])}", [dep] + dep_errors[0]["event_ids"][:1]))
            elif dep in boundary:
                h.checks.append(Check("origin", None, f"{dep} is external: no error events or deployments are modeled for it, so this cannot be confirmed or ruled out", [dep]))
            else:
                h.checks.append(Check("origin", None, f"{dep} has no error events in the window and no other dependents to compare against", [dep]))

    def _missing_data(self, deps, errors, boundary, affected, origin, shared_hyp: Hypothesis) -> list[str]:
        out: list[str] = []
        if not deps:
            out.append(f"no deployments to {affected} or its dependencies in the window — widen the window or ingest config/infra changes")
        for b in boundary:
            out.append(f"error events / status for external dependency {b} (not instrumented in the graph)")
            out.append(f"change history for {b} (third-party; deployments are not modeled)")
        if not errors:
            out.append("no error events in the window — ingest logs for the affected service")
        for c in shared_hyp.checks:
            if c.passed is None and not any(b in c.evidence_ids for b in boundary) and c.detail not in out:
                out.append(c.detail)
        return out

    def _propose_action(self, inv_id: str, rc: dict[str, Any], h: Hypothesis, runbook_id: str | None = None) -> dict[str, Any]:
        sim = h.details.get("similar_incident") or {}
        rb_id = runbook_id or sim.get("runbook_id")
        steps = sim.get("runbook_steps") or []
        if rb_id and not steps:
            rows, _ = self.graph.read("MATCH (rb:Runbook {id: $id}) RETURN rb.title, rb.steps", {"id": rb_id})
            if rows:
                sim = {"runbook_title": rows[0][0]}
                steps = rows[0][1]
        summary = f"Roll back {rc['service']} deployment {rc['deployment_id']} ({rc.get('version') or ''}) to the previous version"
        if rb_id:
            summary += f", then follow runbook {rb_id}"
        return {"id": f"act-{inv_id[4:]}", "kind": "rollback", "status": "proposed", "simulated": True, "summary": summary,
                "deployment_id": rc["deployment_id"], "service": rc["service"], "runbook_id": rb_id, "runbook_title": sim.get("runbook_title"),
                "runbook_steps": steps}

    def _remember(self, inv_id: str, incident_id: str, now: int, hyps: list[Hypothesis]) -> None:
        rep = self.report
        payload = {
            "id": inv_id, "incident_id": incident_id, "at": now, "mode": self.mode, "outcome": rep["outcome"],
            "tool_calls": self.ctx.tool_calls + 1, "confidence": rep["confidence"]["share"], "agent": "engine", "verified": True,
            "trace": [{"step": c.step, "tool": c.tool, "args": c.args, "summary": c.summary, "rows": c.row_count, "ms": c.duration_ms, "purpose": c.purpose}
                      for c in self.ctx.trace],
            "hypotheses": [{"id": h.id, "status": h.status, "score": h.score, "kind": h.kind, "label": h.label, "suspect": h.suspect,
                            "checks": [{"name": c.name, "passed": c.passed, "evidence_ids": c.evidence_ids} for c in h.checks if c.passed is not None]} for h in hyps],
            "action": rep["action"],
        }
        self._call("record_investigation", "write-back: this is what the next `recall` will find", investigation=payload)

    def _narrate(self) -> None:
        rep = self.report
        rep["tool_calls"] = self.ctx.tool_calls
        template = build_narrative(rep)
        rep["narrative"], rep["narrative_source"] = template, "template"
        if self.mode != "llm":
            return
        evidence = {k: rep[k] for k in ("incident", "outcome", "root_cause", "hypotheses", "similar_incidents", "action", "memory", "missing_data", "confidence")}
        text, model, err, skipped = llm_adapter.write_narrative(evidence)
        for s in skipped:  # a provider that failed before another one succeeded
            rep["llm_fallbacks"].append({"step": "narrative", "reason": s, "fallback": "next LLM provider"})
        if text is None:
            rep["llm_fallbacks"].append({"step": "narrative", "reason": err, "fallback": "deterministic template"})
            return
        rep["llm_model"] = model
        known = {r[0] for r in self.graph.read("MATCH (s:Service) RETURN s.name")[0]}
        evidence_ids = set(_collect_ids(evidence)) | {rep["incident"]["id"], rep["incident"]["service"]}
        ok, problems = NarrativeValidator(known, evidence_ids, traversals_empty=not rep["graph_highlights"].get("visited"), outcome=rep["outcome"]).validate(text)
        if ok:
            rep["narrative"], rep["narrative_source"] = text, "llm"
        else:
            rep["llm_fallbacks"].append({"step": "narrative", "reason": "validator rejected LLM output: " + "; ".join(problems), "fallback": "deterministic template"})

    def _finish(self) -> dict[str, Any]:
        rep = self.report
        rep["trace"] = [c.to_dict() for c in self.ctx.trace]
        rep["tool_calls"] = self.ctx.tool_calls
        rep["duration_ms"] = round((time.perf_counter() - self._t0) * 1000, 1)
        return rep


# ---------------------------------------------------------------------- server-side verification
def verify_candidates(graph: Graph, incident_id: str, suspects: list[str]) -> dict[str, Any]:
    """Re-run the rule-based checks on deployment IDs proposed by an outside agent (MCP guest).

    Nothing is written. Returns per-suspect status/score/checks, the overall confidence of the best
    supported suspect, and the remediation the engine would propose for it.
    """
    suspects = [validate_id(s, "suspect") for s in suspects][:8]
    inv = Investigator(graph, verify_only=suspects)
    rep = inv.run(incident_id=incident_id)
    if rep["outcome"] == "unknown_entity":
        raise ToolError(rep.get("error", "unknown entity"), kind="unresolved", unresolved=[incident_id])
    hyps = [{"suspect": h["suspect"], "status": h["status"], "score": h["score"], "label": h["label"],
             "checks": [{"name": c["name"], "passed": c["passed"], "detail": c["detail"], "evidence_ids": c["evidence_ids"]} for c in h["checks"] if c["passed"] is not None]}
            for h in rep["hypotheses"] if h["kind"] == "deployment"]
    action = rep.get("action")
    if action:
        action = {k: v for k, v in action.items() if k != "id"}
    return {"hypotheses": hyps, "confidence": rep["confidence"]["share"], "checks_run": sum(len(h["checks"]) for h in hyps),
            "duration_ms": rep.get("duration_ms", 0), "action": action, "outcome": rep["outcome"], "root_cause": rep.get("root_cause")}


# ---------------------------------------------------------------------- scoring helpers
def _risk_rank(r: str) -> int:
    return {"low": 0, "medium": 1, "high": 2}.get(r, 0)


def _reaches(candidate: str, origin: str, affected: str, all_paths: list[list[str]]) -> bool:
    """Is `origin` at or downstream of `candidate`, judging by the traversal paths from the symptom service?"""
    if candidate == origin:
        return True
    if candidate == affected:
        return any(origin in p for p in all_paths) or origin == affected
    return any(candidate in p and origin in p and p.index(candidate) < p.index(origin) for p in all_paths)


def _score(h: Hypothesis) -> None:
    evaluated = [c for c in h.checks if c.passed is not None]
    passed = [c for c in evaluated if c.passed]
    hard_fail = any(c.hard and c.passed is False for c in h.checks)
    h.score = round(len(passed) / len(evaluated), 3) if evaluated else 0.0
    if hard_fail:
        h.status = "rejected"
    elif h.kind == "shared_dependency":
        h.status = "supported" if evaluated and h.score >= SUPPORT_THRESHOLD and any(c.passed for c in evaluated) else "inconclusive"
    elif evaluated and h.score >= SUPPORT_THRESHOLD and len(evaluated) >= 3:
        h.status = "supported"
    else:
        h.status = "inconclusive"


def _confidence(h: Hypothesis) -> dict[str, Any]:
    evaluated = [c for c in h.checks if c.passed is not None]
    passed = [c for c in evaluated if c.passed]
    return {"passed": len(passed), "total": len(evaluated), "share": round(len(passed) / len(evaluated), 3) if evaluated else 0.0,
            "breakdown": [{"check": c.name, "passed": c.passed, "hard": c.hard} for c in h.checks],
            "note": "share of graph checks passed — not a probability"}


def _collect_ids(obj: Any) -> list[str]:
    out: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, str) and k in ("id", "deployment_id", "service", "incident_id", "runbook_id", "resolution_id", "investigation_id",
                                             "hypothesis_id", "suspect", "reverted_deployment", "action_id"):
                out.append(v)
            else:
                out.extend(_collect_ids(v))
    elif isinstance(obj, list):
        for v in obj:
            if isinstance(v, str):
                out.append(v)
            else:
                out.extend(_collect_ids(v))
    return out
