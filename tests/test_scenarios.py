"""Scenario tests: outcome and root-cause IDs must match tests/scenarios.yaml (the only place ground truth lives)."""
from __future__ import annotations

from backend.agent.engine import Investigator


def _run(graph, **kw):
    return Investigator(graph, mode="deterministic").run(**kw)


def test_scenario_a_red_herring(fresh_memory, scenarios):
    s = scenarios["A"]
    r = _run(fresh_memory, incident_id=s["incident_id"])
    assert r["outcome"] == s["expected_outcome"]
    assert r["root_cause"]["deployment_id"] == s["root_cause_deployment"]
    assert r["root_cause"]["service"] == s["root_cause_service"]
    assert s["root_cause_code_change"] in [c["id"] for c in r["root_cause"]["changes"]]
    by_suspect = {h["suspect"]: h for h in r["hypotheses"]}
    for dep_id, check in s["rejected_checks"].items():
        h = by_suspect[dep_id]
        assert h["status"] == "rejected", dep_id
        failed = {c["name"] for c in h["checks"] if c["passed"] is False}
        assert check in failed, (dep_id, failed)
    assert any(x["id"] == s["similar_incident"] for x in r["similar_incidents"])
    assert r["action"]["kind"] == s["remediation_kind"]
    assert r["action"]["deployment_id"] == s["root_cause_deployment"]
    assert r["action"]["runbook_id"] == s["runbook"]
    assert r["action"]["simulated"] is True and r["action"]["status"] == "proposed"
    # the obvious suspect is visibly struck through in the graph highlights
    assert "checkout" in r["graph_highlights"]["rejected"]
    assert r["graph_highlights"]["root_path"] == ["checkout", "payments-api"]
    # every step is traced with real Cypher; no step wrote except the final record
    assert r["tool_calls"] <= 12
    writes = [c for c in r["trace"] if any(not q["read_only"] for q in c["queries"])]
    assert [c["tool"] for c in writes] == ["record_investigation"]
    assert "proven" not in r["narrative"].lower()


def test_scenario_b_answered_from_memory(fresh_memory, scenarios):
    a = _run(fresh_memory, incident_id=scenarios["A"]["incident_id"])
    assert a["outcome"] == "root_cause_supported"
    s = scenarios["B"]
    r = _run(fresh_memory, incident_id=s["incident_id"])
    assert r["outcome"] == s["expected_outcome"]
    assert r["root_cause"]["deployment_id"] == s["root_cause_deployment"]
    assert r["memory"]["used"] is True
    assert r["memory"]["investigation_id"] == a["id"]  # memory came from the agent's own write-back
    assert r["tool_calls"] <= s["max_tool_calls"]
    assert r["tool_calls"] < a["tool_calls"]
    assert "answered from memory" in r["narrative"].lower()


def test_rerunning_same_incident_does_not_answer_from_its_own_memory(fresh_memory, scenarios):
    first = _run(fresh_memory, incident_id=scenarios["A"]["incident_id"])
    second = _run(fresh_memory, incident_id=scenarios["A"]["incident_id"])
    assert first["outcome"] == second["outcome"] == "root_cause_supported"
    assert second["memory"] is None and second["tool_calls"] == first["tool_calls"]


def test_scenario_b_without_memory_is_a_full_investigation(fresh_memory, scenarios):
    r = _run(fresh_memory, incident_id=scenarios["B"]["incident_id"])
    assert r["outcome"] == "root_cause_supported"  # payments-api deploy is still derivable the long way
    assert r["memory"] is None
    assert r["tool_calls"] > scenarios["B"]["max_tool_calls"] - 2


def test_scenario_c_inconclusive(fresh_memory, scenarios):
    s = scenarios["C"]
    r = _run(fresh_memory, incident_id=s["incident_id"])
    assert r["outcome"] == s["expected_outcome"]
    assert r["root_cause"] is None and r["action"] is None
    assert s["boundary_service"] in r["graph_highlights"]["boundary"]
    joined = " ".join(r["missing_data"]).lower()
    for m in s["missing_data_mentions"]:
        assert m in joined
    assert len(r["checked"]) >= 5  # it lists what it actually checked
    assert all(h["status"] != "supported" for h in r["hypotheses"])


def _approve(graph, action_id: str) -> dict:
    from backend.seed import t
    from backend.tools import ToolContext

    ctx = ToolContext(graph, max_calls=4)
    ctx.call("record_action", action_id=action_id, status="approved", at=t(14, 40))
    return ctx.call("record_action", action_id=action_id, status="executed", at=t(14, 41)).rows[0]


def test_scenario_d_learns_from_the_approved_fix(fresh_memory, scenarios):
    """A week later the same shape of failure: history must include the Resolution TraceLens wrote on approval."""
    s = scenarios["D"]
    a = _run(fresh_memory, incident_id=scenarios["A"]["incident_id"])
    executed = _approve(fresh_memory, a["action"]["id"])
    assert executed["status"] == "executed" and executed["resolution_id"] == f"res-{a['action']['id']}" and executed["incident_id"] == "inc-1042"
    rows, _ = fresh_memory.read(
        "MATCH (i:Incident {id:'inc-1042'})-[:RESOLVED_BY]->(r:Resolution)-[:REVERTED]->(d:Deployment), (r)-[:FOLLOWED]->(rb:Runbook) "
        "RETURN i.status, r.source, d.id, rb.id")
    assert rows == [["resolved", "tracelens", scenarios["A"]["root_cause_deployment"], scenarios["A"]["runbook"]]]

    d = _run(fresh_memory, incident_id=s["incident_id"])
    assert d["outcome"] == s["expected_outcome"]
    assert d["memory"] is None  # a week old -> outside the window: this is history, not recall
    assert d["root_cause"]["deployment_id"] == s["root_cause_deployment"]
    by_suspect = {h["suspect"]: h for h in d["hypotheses"]}
    for dep_id, check in s["rejected_checks"].items():
        assert by_suspect[dep_id]["status"] == "rejected" and check in {c["name"] for c in by_suspect[dep_id]["checks"] if c["passed"] is False}
    sims = {x["id"]: x for x in d["similar_incidents"]}
    assert set(s["similar_incidents_must_include"]) <= set(sims)
    learned = sims[s["learned_resolution_on"]]
    assert learned["learned_by_tracelens"] is True and learned["resolution_id"] == executed["resolution_id"]
    assert learned["reverted_deployment"] == scenarios["A"]["root_cause_deployment"] and learned["runbook_id"] == scenarios["A"]["runbook"]
    assert "payments/db/pool.py" in learned["reverted_files"]  # same file as dep-pay-d's change: structural match
    history = next(c for c in by_suspect[s["root_cause_deployment"]]["checks"] if c["name"] == "history")
    assert history["passed"] is True and "inc-1042" in history["detail"] and "inc-1042" in history["evidence_ids"]
    assert d["action"]["runbook_id"] == scenarios["A"]["runbook"]  # the runbook came through the learned resolution


def test_scenario_d_without_approval_has_no_learned_resolution(fresh_memory, scenarios):
    s = scenarios["D"]
    _run(fresh_memory, incident_id=scenarios["A"]["incident_id"])  # investigated but never approved
    d = _run(fresh_memory, incident_id=s["incident_id"])
    assert d["outcome"] == s["expected_outcome"] and d["root_cause"]["deployment_id"] == s["root_cause_deployment"]
    assert [x["id"] for x in d["similar_incidents"]] == ["inc-0877"]  # only the seeded history


def test_unknown_incident_runs_no_tool(fresh_memory, scenarios):
    s = scenarios["unknown_incident"]
    r = _run(fresh_memory, incident_id=s["incident_id"])
    assert r["outcome"] == s["expected_outcome"]
    assert r["tool_calls"] == s["expected_tool_calls"] and r["trace"] == []
    assert r["resolution"]["found"] is False


def test_unknown_service_runs_no_tool(fresh_memory, scenarios):
    s = scenarios["unknown_service"]
    r = _run(fresh_memory, service=s["service"])
    assert r["outcome"] == s["expected_outcome"]
    assert r["tool_calls"] == 0 and r["trace"] == []


def test_investigation_is_written_back(fresh_memory, scenarios):
    r = _run(fresh_memory, incident_id=scenarios["A"]["incident_id"])
    rows, _ = fresh_memory.read(
        "MATCH (inv:Investigation {id: $id})-[:CONSIDERED]->(h:Hypothesis) RETURN h.status, count(h) ORDER BY h.status", {"id": r["id"]}
    )
    statuses = dict(rows)
    assert statuses.get("supported") == 1 and statuses.get("rejected", 0) >= 2
    rows, _ = fresh_memory.read("MATCH (:Investigation {id: $id})-[:PROPOSED]->(a:Action)-[:TARGETS]->(d:Deployment) RETURN a.status, d.id", {"id": r["id"]})
    assert rows == [["proposed", scenarios["A"]["root_cause_deployment"]]]
