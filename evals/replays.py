"""Recorded tool-call sequences replayed by the fake model in OFFLINE mode.

Scenario A replays the real live run (agents/langgraph_guest/recordings/live-inc-1042-*.json)
when present. The other cases are hand-recorded sequences a competent agent would take with the
same tools. None of these sequences contain ground truth from tests/scenarios.yaml — they contain
tool calls; the SERVER verifies whatever they propose, exactly as in a live run.
"""
from __future__ import annotations

import glob
import json
from pathlib import Path
from typing import Any

from backend.seed import t

ROOT = Path(__file__).resolve().parents[1]

PATH_CHECKOUT = ["checkout", "auth", "cart", "inventory", "notification", "payments-api", "fraud-check", "kafka", "postgres-payments", "redis-sessions"]
PATH_REFUNDS = ["refunds-worker", "kafka", "payments-api", "fraud-check", "postgres-payments"]
PATH_SEARCH = ["search", "vendor-search-index"]
WIN_A = {"from_ts": t(13, 10), "to_ts": t(14, 10)}
WIN_B = {"from_ts": t(13, 25), "to_ts": t(14, 25)}
WIN_C = {"from_ts": t(14, 30), "to_ts": t(15, 30)}


def _live_recording_script(incident_id: str) -> list[dict[str, Any]] | None:
    files = sorted(glob.glob(str(ROOT / "agents" / "langgraph_guest" / "recordings" / f"live-{incident_id}-*.json")))
    if not files:
        return None
    rec = json.loads(Path(files[-1]).read_text(encoding="utf-8"))
    script = [{"tool": c["tool"], "args": dict(c["args"], reason=c.get("reason", ""))} for c in rec["tool_calls"]]
    script.append({"final": rec.get("final") or "done"})
    return script


def script_for(case: str) -> tuple[list[dict[str, Any]], str]:
    """Returns (script, source) — source says where the sequence came from."""
    if case == "A":
        live = _live_recording_script("inc-1042")
        if live:
            return live, "replay of live run (agents/langgraph_guest/recordings)"
        return [
            {"tool": "get_incident", "args": {"incident_id": "inc-1042", "reason": "anchor"}},
            {"tool": "upstream_dependencies", "args": {"service": "checkout", "depth": 4, "reason": "suspect space"}},
            {"tool": "recall", "args": {"services": PATH_CHECKOUT, "exclude_incident": "inc-1042", "reason": "memory", **WIN_A}},
            {"tool": "deployments_in_window", "args": {"services": PATH_CHECKOUT, "reason": "candidates", **WIN_A}},
            {"tool": "error_timeline", "args": {"services": PATH_CHECKOUT, "reason": "who failed first", **WIN_A}},
            {"tool": "healthy_siblings", "args": {"service": "payments-api", "reason": "common cause", **WIN_A}},
            {"tool": "record_investigation", "args": {"incident_id": "inc-1042", "outcome": "root_cause_supported", "suspects": ["dep-pay-1352"], "agent": "langgraph",
                                                      "summary": "payments-api pool change preceded checkout errors", "reason": "persist"}},
            {"final": "Best-supported explanation: dep-pay-1352 on payments-api."},
        ], "hand-recorded"
    if case == "B_after_A":
        return [
            {"tool": "get_incident", "args": {"incident_id": "inc-1043", "reason": "anchor"}},
            {"tool": "upstream_dependencies", "args": {"service": "refunds-worker", "depth": 4, "reason": "suspect space"}},
            {"tool": "recall", "args": {"services": PATH_REFUNDS, "exclude_incident": "inc-1043", "reason": "do we already know?", **WIN_B}},
            {"tool": "record_investigation", "args": {"incident_id": "inc-1043", "outcome": "answered_from_memory", "suspects": ["dep-pay-1352"], "agent": "langgraph",
                                                      "summary": "memory holds a supported root cause on payments-api inside the window", "reason": "persist"}},
            {"final": "Answered from memory: dep-pay-1352 on payments-api, established by an earlier investigation."},
        ], "hand-recorded"
    if case == "B_alone":
        return [
            {"tool": "get_incident", "args": {"incident_id": "inc-1043", "reason": "anchor"}},
            {"tool": "upstream_dependencies", "args": {"service": "refunds-worker", "depth": 4, "reason": "suspect space"}},
            {"tool": "recall", "args": {"services": PATH_REFUNDS, "exclude_incident": "inc-1043", "reason": "memory", **WIN_B}},
            {"tool": "deployments_in_window", "args": {"services": PATH_REFUNDS, "reason": "candidates", **WIN_B}},
            {"tool": "error_timeline", "args": {"services": PATH_REFUNDS, "reason": "who failed first", **WIN_B}},
            {"tool": "path_between", "args": {"from_service": "refunds-worker", "to_service": "payments-api", "reason": "is it upstream"}},
            {"tool": "healthy_siblings", "args": {"service": "payments-api", "reason": "common cause", **WIN_B}},
            {"tool": "similar_incidents", "args": {"signature_keys": ["PoolTimeoutError", "UpstreamTimeout"], "services": ["payments-api"], "reason": "history"}},
            {"tool": "record_investigation", "args": {"incident_id": "inc-1043", "outcome": "root_cause_supported", "suspects": ["dep-pay-1352"], "agent": "langgraph",
                                                      "summary": "payments-api pool change preceded refunds-worker timeouts", "reason": "persist"}},
            {"final": "Best-supported explanation: dep-pay-1352 on payments-api."},
        ], "hand-recorded"
    if case == "C":
        return [
            {"tool": "get_incident", "args": {"incident_id": "inc-1044", "reason": "anchor"}},
            {"tool": "upstream_dependencies", "args": {"service": "search", "depth": 4, "reason": "suspect space"}},
            {"tool": "recall", "args": {"services": PATH_SEARCH, "exclude_incident": "inc-1044", "reason": "memory", **WIN_C}},
            {"tool": "deployments_in_window", "args": {"services": PATH_SEARCH, "reason": "candidates", **WIN_C}},
            {"tool": "error_timeline", "args": {"services": PATH_SEARCH, "reason": "who failed first", **WIN_C}},
            {"tool": "record_investigation", "args": {"incident_id": "inc-1044", "outcome": "inconclusive", "suspects": [], "agent": "langgraph",
                                                      "summary": "no deployments on the path; the vendor is external with no data", "reason": "persist"}},
            {"final": "Inconclusive: no deployments in the window and vendor-search-index is external with no modeled data."},
        ], "hand-recorded"
    if case == "unknown":
        return [
            {"tool": "get_incident", "args": {"incident_id": "inc-9999", "reason": "anchor"}},
            {"final": "Unknown incident inc-9999: it does not exist in the graph, so no investigation was run."},
        ], "hand-recorded"
    raise KeyError(case)
