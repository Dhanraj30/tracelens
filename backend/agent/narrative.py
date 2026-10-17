"""Narrative generation (template) and the validator that gates any LLM-written narrative.

Language rules: never 'proven cause'. We say 'best-supported explanation' and label items
correlated / supported / ruled out. Confidence = share of checks passed, not a probability.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

ID_TOKEN = re.compile(r"\b(?:dep|inc|cc|err|rb|res|inv|act|hyp)-[A-Za-z0-9][A-Za-z0-9\-_.]*", re.IGNORECASE)
FORBIDDEN_PHRASES = ("proven cause", "proves that", "proved that", "definitively caused", "root cause is proven")


def fmt_ts(ts: int | float | None) -> str:
    if ts is None:
        return "unknown time"
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%H:%M UTC")


def fmt_date(ts: int | float | None) -> str:
    if ts is None:
        return "unknown date"
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%d %b %Y %H:%M UTC")


class NarrativeValidator:
    """Rejects narratives that name entities absent from the evidence, or that over-claim."""

    def __init__(self, known_services: set[str], evidence_ids: set[str], traversals_empty: bool):
        self.known_services = {s.lower() for s in known_services}
        self.evidence_ids = {e.lower() for e in evidence_ids}
        self.evidence_services = {e.lower() for e in evidence_ids if e.lower() in self.known_services}
        self.traversals_empty = traversals_empty

    def validate(self, text: str) -> tuple[bool, list[str]]:
        problems: list[str] = []
        low = text.lower()
        for tok in {m.group(0).lower().rstrip(".") for m in ID_TOKEN.finditer(text)}:
            if tok not in self.evidence_ids:
                problems.append(f"names an ID not in evidence: {tok}")
        for svc in self.known_services:
            if re.search(rf"(?<![\w-]){re.escape(svc)}(?![\w-])", low) and svc not in self.evidence_services:
                problems.append(f"names a service not in evidence: {svc}")
        for phrase in FORBIDDEN_PHRASES:
            if phrase in low:
                problems.append(f"over-claims: '{phrase}'")
        if self.traversals_empty and re.search(r"\b(impact(s|ed)?|affect(s|ed)?|cascad\w+)\b", low):
            problems.append("claims impact although dependency traversals returned nothing")
        return (not problems, problems)


def build_narrative(report: dict[str, Any]) -> str:
    """Deterministic narrative built only from rows the tools returned."""
    inc = report["incident"]
    out = report["outcome"]
    conf = report["confidence"]
    lines: list[str] = []
    lines.append(f"Incident {inc['id']} — {inc['title']}: {inc['symptom']} (affects {inc['service']}, opened {fmt_ts(inc['opened_at'])}).")

    if out == "unknown_entity":
        return f"Unknown entity: {report.get('error', 'the requested incident or service does not exist in the graph')}. No investigation queries were run."

    if out == "answered_from_memory":
        mem = report["memory"]
        rc = report["root_cause"]
        lines.append(
            f"Answered from memory: investigation {mem['investigation_id']} (incident {mem['incident_id']}, learned {fmt_date(mem['learned_at'])}) "
            f"already established deployment {rc['deployment_id']} to {rc['service']} as the best-supported explanation"
            + (" and an operator approved the remediation." if mem.get("confirmed") else ".")
        )
        lines.append(f"Re-verified against this incident: {inc['service']} depends on {rc['service']} and its errors began at {fmt_ts(report.get('first_symptom_at'))}, after the deployment at {fmt_ts(rc['deployment_at'])}.")
        lines.append(f"Reached in {report['tool_calls']} tool calls instead of a full investigation.")
        return " ".join(lines)

    hyps = report["hypotheses"]
    rejected = [h for h in hyps if h["status"] == "rejected"]
    supported = [h for h in hyps if h["status"] == "supported"]

    if out == "root_cause_supported":
        rc = report["root_cause"]
        h = supported[0]
        chg = "; ".join(c["summary"] for c in rc.get("changes", [])) or "no code change details"
        lines.append(
            f"Best-supported explanation: deployment {rc['deployment_id']} ({rc.get('version', '')}) to {rc['service']} at {fmt_ts(rc['deployment_at'])} — {chg}. "
            f"It passed {conf['passed']} of {conf['total']} graph checks."
        )
        lines.append(f"Dependency path from the symptom: {' → '.join(rc.get('path') or [inc['service'], rc['service']])}.")
    elif out == "multiple_candidates":
        lines.append(f"{len(supported)} candidates remain supported: " + ", ".join(h["label"] for h in supported) + ". More evidence is needed to separate them.")
    else:
        lines.append("Inconclusive: no candidate passed enough graph checks to be the best-supported explanation.")

    for h in rejected:
        failed = [c for c in h["checks"] if c["passed"] is False]
        reason = failed[0]["detail"] if failed else "failed checks"
        lines.append(f"Ruled out: {h['label']} — {reason.rstrip('.')}.")

    sims = report.get("similar_incidents") or []
    if sims:
        s = sims[0]
        lines.append(
            f"History: incident {s['id']} ({fmt_date(s['opened_at'])}) shared the {', '.join(s['shared_signatures'])} signature on {s['service']} "
            f"and was resolved by {s.get('resolution_summary', 'a recorded resolution')}" + (f" following runbook {s['runbook_id']}." if s.get("runbook_id") else ".")
        )

    act = report.get("action")
    if act:
        lines.append(f"Proposed remediation (simulated, needs approval): {act['summary']}")

    if report.get("missing_data"):
        lines.append("Missing data that would resolve this: " + "; ".join(report["missing_data"]) + ".")
    return " ".join(lines)
