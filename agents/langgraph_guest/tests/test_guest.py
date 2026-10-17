"""Guest agent tests: a fake model replays a recorded tool-call sequence against the in-process MCP server.
No network. Skipped automatically if the guest requirements are not installed."""
from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("langchain.mcp")
pytest.importorskip("langgraph")

from backend.agent.engine import Investigator  # noqa: E402
from backend.seed import t  # noqa: E402

from agents.langgraph_guest.agent import EXCLUDED_TOOLS, run_guest  # noqa: E402
from agents.langgraph_guest.fake_model import ReplayToolCallingModel  # noqa: E402

WIN = {"from_ts": t(13, 10), "to_ts": t(14, 10)}
PATH = ["checkout", "auth", "cart", "inventory", "notification", "payments-api", "fraud-check", "kafka", "postgres-payments", "redis-sessions"]

# The sequence a live gpt-oss-120b run chose (see recordings/), condensed.
SCRIPT_GOOD = [
    {"tool": "get_incident", "args": {"incident_id": "inc-1042", "reason": "anchor on the symptom"}},
    {"tool": "upstream_dependencies", "args": {"service": "checkout", "depth": 4, "reason": "define the suspect space"}},
    {"tool": "recall", "args": {"services": PATH, "exclude_incident": "inc-1042", "reason": "check memory first", **WIN}},
    {"tool": "deployments_in_window", "args": {"services": PATH, "reason": "candidate changes", **WIN}},
    {"tool": "error_timeline", "args": {"services": PATH, "reason": "who failed first", **WIN}},
    {"tool": "path_between", "args": {"from_service": "checkout", "to_service": "payments-api", "reason": "is payments-api upstream of checkout"}},
    {"tool": "healthy_siblings", "args": {"service": "payments-api", "reason": "common-cause test", **WIN}},
    {"tool": "similar_incidents", "args": {"signature_keys": ["PoolTimeoutError", "UpstreamTimeout"], "services": ["payments-api"], "reason": "seen before?"}},
    {"tool": "record_investigation", "args": {"incident_id": "inc-1042", "outcome": "root_cause_supported", "suspects": ["dep-pay-1352"], "agent": "langgraph",
                                              "summary": "payments-api deploy cut the pool; checkout deploy came after the errors", "reason": "persist"}},
    {"final": "Best-supported explanation: dep-pay-1352 on payments-api. The checkout deploy is ruled out by timing. Recorded for other agents."},
]

# A guest that blames the obvious suspect after barely looking.
SCRIPT_NAIVE = [
    {"tool": "get_incident", "args": {"incident_id": "inc-1042", "reason": "anchor"}},
    {"tool": "deployments_in_window", "args": {"services": ["checkout"], "reason": "what changed on checkout", **WIN}},
    {"tool": "record_investigation", "args": {"incident_id": "inc-1042", "outcome": "root_cause_supported", "suspects": ["dep-chk-1405"], "agent": "langgraph",
                                              "summary": "the checkout deploy at 14:05 broke checkout", "reason": "persist"}},
    {"final": "The checkout deploy caused it."},
]


def _server(fresh_memory, monkeypatch):
    import backend.mcp_server as srv

    srv._ctx = None
    srv._session["trace_start"] = 0
    monkeypatch.setattr(srv, "get_graph", lambda: fresh_memory)
    return srv


def test_guest_replay_reaches_verified_root_cause_and_hands_off(fresh_memory, monkeypatch, scenarios):
    srv = _server(fresh_memory, monkeypatch)
    out = asyncio.run(run_guest("inc-1042", server=srv.mcp, model=ReplayToolCallingModel(script=SCRIPT_GOOD), fallbacks=[], log=lambda s: None))
    assert [c["tool"] for c in out["tool_calls"]] == [s["tool"] for s in SCRIPT_GOOD if "tool" in s]
    assert all(c["reason"] for c in out["tool_calls"]) and out["tool_calls"][0]["reason_label"] == "model's stated reason (unverified)"
    assert not any(c["tool"] in EXCLUDED_TOOLS for c in out["tool_calls"])
    rec = out["recorded"]
    assert rec["ok"] and rec["agent"] == "mcp:langgraph" and rec["verified_outcome"] == "root_cause_supported"
    statuses = {h["suspect"]: h["status"] for h in rec["hypotheses"]}
    assert statuses[scenarios["A"]["root_cause_deployment"]] == "supported"
    assert all(statuses[d] == "rejected" for d in scenarios["A"]["rejected_deployments"])  # whole path verified, not just the claim
    assert rec["tool_calls_recorded"] == 8  # the server captured the guest's 8 evidence calls (+ its own verification step)
    # the guest's trace, with unverified reasons, is on the Investigation node
    rows, _ = fresh_memory.read("MATCH (inv:Investigation {id: $id}) RETURN inv.agent, inv.trace", {"id": rec["investigation_id"]})
    assert rows[0][0] == "mcp:langgraph" and "model's stated reason (unverified)" in rows[0][1]
    # handoff: the dashboard engine answers Scenario B from the guest's memory
    b = Investigator(fresh_memory).run(incident_id=scenarios["B"]["incident_id"])
    assert b["outcome"] == "answered_from_memory" and b["memory"]["agent"] == "mcp:langgraph"


def test_guest_wrong_suspect_is_stored_as_rejected(fresh_memory, monkeypatch, scenarios):
    srv = _server(fresh_memory, monkeypatch)
    out = asyncio.run(run_guest("inc-1042", server=srv.mcp, model=ReplayToolCallingModel(script=SCRIPT_NAIVE), fallbacks=[], log=lambda s: None))
    rec = out["recorded"]
    assert rec["ok"] and rec["claimed_outcome"] == "root_cause_supported" and rec["verified_outcome"] == "inconclusive"
    h = next(x for x in rec["hypotheses"] if x["suspect"] == "dep-chk-1405")
    assert h["status"] == "rejected" and h["proposed"] and "temporal_precedence" in h["failed_checks"]
    rows, _ = fresh_memory.read("MATCH (inv:Investigation {id: $id})-[:CONSIDERED]->(h)-[r:REJECTED_BY]->(n) RETURN h.status, r.check, coalesce(n.id, n.name) ORDER BY r.check, n.id",
                                {"id": rec["investigation_id"]})
    assert rows and all(r[0] == "rejected" for r in rows) and any(r[1] == "temporal_precedence" for r in rows)
    # and it is NOT usable as memory
    b = Investigator(fresh_memory).run(incident_id=scenarios["B"]["incident_id"])
    assert b["outcome"] == "root_cause_supported" and b["memory"] is None
