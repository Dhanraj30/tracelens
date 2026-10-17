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
