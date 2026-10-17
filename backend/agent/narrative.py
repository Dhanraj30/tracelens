"""Narrative generation (template) and the validator that gates any LLM-written narrative.

Language rules: never 'proven cause'. We say 'best-supported explanation' and label items
correlated / supported / ruled out. Confidence = share of checks passed, not a probability.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

ID_TOKEN = re.compile(r"\b(?:dep|inc|cc|err|rb|res|inv|act|hyp)-[A-Za-z0-9][A-Za-z0-9\-_.]*", re.IGNORECASE)
OVERCLAIM = re.compile(r"\bprov(?:en|es|e|ed)\b|\bproof\b|\bdefinitively\b|\bcertainly caused\b")
CLAIM_WORDS = re.compile(r"best[- ]supported|\bsupported\b|\bcause[sd]?\b|\bculprit\b|\bresponsible\b")
EXCUSE_WORDS = re.compile(r"ruled out|rejected|not the cause|cannot be the cause|excluded|eliminated")


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

    def __init__(self, known_services: set[str], evidence_ids: set[str], traversals_empty: bool, outcome: str | None = None,
                 root_cause_id: str | None = None, rejected_ids: set[str] | None = None):
        self.known_services = {s.lower() for s in known_services}
        self.evidence_ids = {e.lower() for e in evidence_ids}
        self.evidence_services = {e.lower() for e in evidence_ids if e.lower() in self.known_services}
        self.traversals_empty = traversals_empty
        self.outcome = outcome
        self.root_cause_id = (root_cause_id or "").lower() or None
        self.rejected_ids = {r.lower() for r in (rejected_ids or set())}

    def validate(self, text: str) -> tuple[bool, list[str]]:
        problems: list[str] = []
        low = text.lower()
        for tok in {m.group(0).lower().rstrip(".") for m in ID_TOKEN.finditer(text)}:
            if tok not in self.evidence_ids:
                problems.append(f"names an ID not in evidence: {tok}")
        for svc in self.known_services:
            if re.search(rf"(?<![\w-]){re.escape(svc)}(?![\w-])", low) and svc not in self.evidence_services:
                problems.append(f"names a service not in evidence: {svc}")
        m = OVERCLAIM.search(low)
        if m:
            problems.append(f"over-claims: '{m.group(0)}'")
        if self.traversals_empty and re.search(r"\b(impact(s|ed)?|affect(s|ed)?|cascad\w+)\b", low):
            problems.append("claims impact although dependency traversals returned nothing")
        # attribution: the narrative must agree with the verdict, not merely mention known entities
        if self.outcome == "root_cause_supported" and self.root_cause_id and self.root_cause_id not in low:
            problems.append(f"outcome is root_cause_supported but the narrative never names the supported deployment {self.root_cause_id}")
        for sentence in re.split(r"(?<=[.!?])\s+", low):
            for rid in self.rejected_ids:
                if rid in sentence and CLAIM_WORDS.search(sentence) and not EXCUSE_WORDS.search(sentence):
                    problems.append(f"presents the ruled-out deployment {rid} as a cause: '{sentence[:120]}'")
        if self.outcome == "inconclusive":
            # an inconclusive investigation must say so, and must not promote any hypothesis to "supported"
            if "inconclusive" not in low:
                problems.append("outcome is inconclusive but the narrative never says so")
            if re.search(r"(?<!best[- ])\bsupported\b", low):
                problems.append("outcome is inconclusive but the narrative labels something 'supported'")
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
            f"Answered from memory: investigation {mem['investigation_id']} by {mem.get('agent') or 'engine'} (incident {mem['incident_id']}, learned {fmt_date(mem['learned_at'])}) "
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
