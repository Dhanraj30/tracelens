"""Cross-agent handoff: an MCP client investigates Scenario A and writes back (server-verified);
the dashboard engine then answers Scenario B from THAT memory, with provenance."""
from __future__ import annotations

import asyncio

from mcp import Client

from backend.agent.engine import Investigator
from backend.mcp_client_demo import investigate_via_mcp


def _guest_server(fresh_memory, monkeypatch):
    import backend.mcp_server as srv

    srv._ctx = None
    srv._session["trace_start"] = 0
    monkeypatch.setattr(srv, "get_graph", lambda: fresh_memory)
    return srv


def test_mcp_client_investigation_is_recalled_by_the_engine(fresh_memory, monkeypatch, scenarios):
    srv = _guest_server(fresh_memory, monkeypatch)

    async def run():
        async with Client(srv.mcp) as client:
            return await investigate_via_mcp(client, scenarios["A"]["incident_id"], log=lambda s: None, agent_name="client-demo")

    res = asyncio.run(run())
    assert res["outcome"] == "root_cause_supported"
    rec = res["recorded"]
    assert rec["ok"] and rec["agent"] == "mcp:client-demo" and rec["verified_outcome"] == "root_cause_supported"
    statuses = {h["suspect"]: h["status"] for h in rec["hypotheses"]}
    assert statuses == {scenarios["A"]["root_cause_deployment"]: "supported", "dep-chk-1405": "rejected", "dep-cart-1320": "rejected"}
    assert rec["tool_calls_recorded"] >= 8  # the guest's own trace was captured by the server

    rows, _ = fresh_memory.read("MATCH (inv:Investigation {id: $id}) RETURN inv.agent, inv.verified, inv.trace", {"id": rec["investigation_id"]})
    assert rows[0][0] == "mcp:client-demo" and rows[0][1] is True and "server_verification" in rows[0][2]

    # dashboard engine, different agent, same graph -> answered from the guest's memory
    b = Investigator(fresh_memory).run(incident_id=scenarios["B"]["incident_id"])
    assert b["outcome"] == "answered_from_memory"
    assert b["memory"]["agent"] == "mcp:client-demo"
    assert b["memory"]["investigation_id"] == rec["investigation_id"]
    assert b["root_cause"]["deployment_id"] == scenarios["B"]["root_cause_deployment"]
    assert "by mcp:client-demo" in b["narrative"]


def test_wrong_suspect_from_guest_is_stored_as_rejected(fresh_memory, monkeypatch, scenarios):
    srv = _guest_server(fresh_memory, monkeypatch)

    async def run():
        async with Client(srv.mcp) as client:
            from backend.mcp_client_demo import data

            # a guest that confidently blames the obvious (wrong) suspect, plus an id that is not even on the path
            return data(await client.call_tool("record_investigation", {
                "incident_id": scenarios["A"]["incident_id"], "outcome": "root_cause_supported",
                "suspects": ["dep-chk-1405", "dep-auth-1100"], "agent": "naive-guest", "summary": "the checkout deploy did it",
            }))

    rec = asyncio.run(run())
    assert rec["ok"] and rec["claimed_outcome"] == "root_cause_supported" and rec["verified_outcome"] == "inconclusive"
    by = {h["suspect"]: h for h in rec["hypotheses"]}
    assert by["dep-chk-1405"]["status"] == "rejected" and "temporal_precedence" in by["dep-chk-1405"]["failed_checks"] and by["dep-chk-1405"]["proposed"]
    assert by["dep-auth-1100"]["status"] == "rejected" and "in_window" in by["dep-auth-1100"]["failed_checks"]
    # the server also checked the rest of the path: the real cause passes but was never proposed, so it is NOT memory
    assert by["dep-pay-1352"]["status"] == "supported" and by["dep-pay-1352"]["proposed"] is False
    assert by["dep-cart-1320"]["status"] == "rejected" and "origin" in by["dep-cart-1320"]["failed_checks"]
    assert rec["action"] is None and rec["candidates_checked"] == 4
    rows, _ = fresh_memory.read("MATCH (inv:Investigation {id: $id})-[:CONSIDERED]->(h) RETURN h.status, h.proposed, count(h) ORDER BY h.status, h.proposed",
                                {"id": rec["investigation_id"]})
    assert rows == [["rejected", False, 1], ["rejected", True, 2], ["supported", False, 1]]
    b = Investigator(fresh_memory).run(incident_id=scenarios["B"]["incident_id"])
    assert b["outcome"] == "root_cause_supported" and b["memory"] is None  # an unclaimed candidate never becomes memory


def test_server_rules_out_the_whole_path_even_when_guest_only_names_the_right_suspect(fresh_memory, monkeypatch, scenarios):
    """Criterion for 'ruled out': the checkout deploy must be STORED as rejected even if the guest never mentioned it."""
    srv = _guest_server(fresh_memory, monkeypatch)

    async def run():
        async with Client(srv.mcp) as client:
            from backend.mcp_client_demo import data

            return data(await client.call_tool("record_investigation", {
                "incident_id": scenarios["A"]["incident_id"], "outcome": "root_cause_supported",
                "suspects": [scenarios["A"]["root_cause_deployment"]], "agent": "focused-guest", "summary": "pool change",
            }))

    rec = asyncio.run(run())
    assert rec["verified_outcome"] == "root_cause_supported" and rec["candidates_checked"] == 3
    by = {h["suspect"]: h for h in rec["hypotheses"]}
    assert by[scenarios["A"]["root_cause_deployment"]]["status"] == "supported" and by[scenarios["A"]["root_cause_deployment"]]["proposed"]
    for dep_id, check in scenarios["A"]["rejected_checks"].items():
        assert by[dep_id]["status"] == "rejected" and by[dep_id]["proposed"] is False and check in by[dep_id]["failed_checks"]
    rows, _ = fresh_memory.read(
        "MATCH (inv:Investigation {id: $id})-[:CONSIDERED]->(h:Hypothesis {status:'rejected'})-[r:REJECTED_BY]->(n) "
        "RETURN h.label, collect(DISTINCT r.check) ORDER BY h.label", {"id": rec["investigation_id"]})
    assert len(rows) == 2 and all(r[1] for r in rows)
    assert rec["action"]["deployment_id"] == scenarios["A"]["root_cause_deployment"]
