"""One test per catalog tool, against the real graph."""
from __future__ import annotations

import pytest

from backend.seed import t
from backend.tools import REGISTRY, ToolContext, ToolError

WIN = dict(from_ts=t(13, 10), to_ts=t(14, 10))


@pytest.fixture()
def ctx(fresh_memory):
    return ToolContext(fresh_memory, max_calls=30)


def test_registry_has_all_tools():
    assert set(REGISTRY) == {
        "get_incident", "upstream_dependencies", "deployments_in_window", "error_timeline", "healthy_siblings",
        "similar_incidents", "path_between", "critical_services", "recall", "record_investigation", "record_action",
    }
    assert {n for n, s in REGISTRY.items() if s.writes} == {"record_investigation", "record_action"}


def test_get_incident(ctx):
    r = ctx.call("get_incident", incident_id="inc-1042")
    row = r.rows[0]
    assert row["service"] == "checkout"
    assert {o["signature"] for o in row["observed"]} == {"UpstreamTimeout"}
    assert "inc-1042" in r.evidence_ids


def test_upstream_dependencies_returns_paths(ctx):
    r = ctx.call("upstream_dependencies", service="checkout", depth=4)
    names = {x["service"] for x in r.rows}
    assert {"payments-api", "postgres-payments", "fraud-check", "kafka", "cart"} <= names
    assert ["checkout", "payments-api", "postgres-payments"] in r.paths
    assert ctx.trace[-1].queries[-1]["cypher"].count("*1..4") == 1  # bound inlined after validation


def test_upstream_depth_validated(ctx):
    with pytest.raises(ToolError):
        ctx.call("upstream_dependencies", service="checkout", depth=9)
    with pytest.raises(ToolError):
        ctx.call("upstream_dependencies", service="checkout", depth="4")


def test_deployments_in_window(ctx):
    r = ctx.call("deployments_in_window", services=["checkout", "payments-api", "cart", "auth"], **WIN)
    assert [d["id"] for d in r.rows] == ["dep-cart-1320", "dep-pay-1352", "dep-chk-1405"]
    assert r.rows[1]["changes"][0]["id"] == "cc-pool-size"


def test_error_timeline_orders_by_first_seen(ctx):
    r = ctx.call("error_timeline", services=["checkout", "payments-api", "fraud-check"], **WIN)
    assert [x["service"] for x in r.rows] == ["payments-api", "checkout"]
    assert r.rows[0]["first_seen"] == t(13, 58)
    assert r.extra["origin_service"] == "payments-api"


def test_healthy_siblings_common_cause(ctx):
    r = ctx.call("healthy_siblings", service="payments-api", **WIN)
    by = {(x["shared_dependency"], x["sibling"]): x for x in r.rows}
    assert by[("postgres-payments", "fraud-check")]["healthy"] is True
    assert by[("kafka", "refunds-worker")]["depends_on_candidate"] is True  # downstream of candidate -> excluded
    assert r.extra["exonerated_dependencies"] == ["kafka", "postgres-payments"]


def test_similar_incidents_by_signature(ctx):
    r = ctx.call("similar_incidents", signature_keys=["PoolTimeoutError"], services=["payments-api"])
    assert [x["id"] for x in r.rows] == ["inc-0877"]
    assert r.rows[0]["runbook_id"] == "rb-pool-sizing"
    assert r.rows[0]["reverted_deployment"] == "dep-pay-h1"
    assert "ErrorSignature:PoolTimeoutError" in r.rows[0]["match_links"]
    assert ctx.call("similar_incidents", signature_keys=["EmptyResultSet"], services=["search"]).rows == []


def test_path_between_uses_sppaths(ctx):
    r = ctx.call("path_between", from_service="checkout", to_service="postgres-payments")
    assert r.rows[0]["path"] == ["checkout", "payments-api", "postgres-payments"]
    assert "algo.SPpaths" in ctx.trace[-1].queries[-1]["cypher"]
    assert ctx.call("path_between", from_service="cart", to_service="payments-api").extra["connected"] is False


def test_critical_services_pagerank(ctx):
    r = ctx.call("critical_services", limit=3)
    assert len(r.rows) == 3
    assert r.rows[0]["service"] == "kafka"  # most depended-upon node
    assert any("algo.pageRank" in q["cypher"] for q in ctx.trace[-1].queries)
    assert any("algo.betweenness" in q["cypher"] for q in ctx.trace[-1].queries)


def test_recall_empty_without_memory(ctx):
    assert ctx.call("recall", services=["payments-api"], **WIN).rows == []


def test_record_investigation_then_recall_and_action(ctx):
    inv = {"id": "inv-t1", "incident_id": "inc-1042", "at": t(14, 30), "mode": "deterministic", "outcome": "root_cause_supported",
           "tool_calls": 9, "confidence": 1.0,
           "hypotheses": [{"id": "inv-t1-h1", "status": "supported", "score": 1.0, "kind": "deployment", "label": "x", "suspect": "dep-pay-1352",
                           "checks": [{"name": "temporal_precedence", "passed": True, "evidence_ids": ["err-chk-1401"]}]},
                          {"id": "inv-t1-h2", "status": "rejected", "score": 0.3, "kind": "deployment", "label": "y", "suspect": "dep-chk-1405",
                           "checks": [{"name": "temporal_precedence", "passed": False, "evidence_ids": ["err-chk-1401"]}]}],
           "action": {"id": "act-t1", "kind": "rollback", "summary": "roll back", "deployment_id": "dep-pay-1352", "runbook_id": "rb-pool-sizing"}}
    ctx.call("record_investigation", investigation=inv)
    edges, _ = ctx.graph.read("MATCH (h:Hypothesis)-[r]->(n) WHERE h.id STARTS WITH 'inv-t1' RETURN h.id, type(r), coalesce(n.id, n.name) ORDER BY h.id, type(r)")
    assert ["inv-t1-h1", "SUPPORTED_BY", "err-chk-1401"] in edges
    assert ["inv-t1-h2", "REJECTED_BY", "err-chk-1401"] in edges
    assert ["inv-t1-h1", "SUSPECTS", "dep-pay-1352"] in edges
    rec = ctx.call("recall", services=["payments-api"], **WIN).rows
    assert rec[0]["deployment_id"] == "dep-pay-1352" and rec[0]["confirmed"] is False and rec[0]["runbook_id"] == "rb-pool-sizing"
    a = ctx.call("record_action", action_id="act-t1", status="approved", at=t(14, 40)).rows[0]
    assert a["status"] == "approved" and a["simulated"] is True
    assert ctx.call("recall", services=["payments-api"], **WIN).rows[0]["confirmed"] is True


def test_unknown_names_run_no_tool_query(ctx):
    for call in (dict(name="upstream_dependencies", service="warehouse-robot", depth=2),
                 dict(name="get_incident", incident_id="inc-9999"),
                 dict(name="deployments_in_window", services=["checkout", "nope"], **WIN)):
        name = call.pop("name")
        with pytest.raises(ToolError) as ei:
            ctx.call(name, **call)
        assert ei.value.kind == "unresolved"
        # only the resolution lookup ran, never the tool's own query
        assert len(ctx.trace[-1].queries) == 1
        assert "MATCH (s:Service {name: nm})" in ctx.trace[-1].queries[0]["cypher"] or "MATCH (i:Incident {id: $id}) RETURN count(i)" in ctx.trace[-1].queries[0]["cypher"]


def test_injection_like_name_rejected_before_any_query(ctx):
    with pytest.raises(ToolError) as ei:
        ctx.call("upstream_dependencies", service="checkout') DETACH DELETE (n", depth=2)
    assert ei.value.kind == "validation"
    assert ctx.trace[-1].queries == []


def test_every_call_is_traced_with_cypher(ctx):
    ctx.call("get_incident", incident_id="inc-1042")
    c = ctx.trace[-1]
    assert c.tool == "get_incident" and c.status == "ok" and c.duration_ms >= 0
    assert any("MATCH (i:Incident {id: $id})-[:AFFECTS]" in q["cypher"] for q in c.queries)
    assert all(q["read_only"] for q in c.queries)
