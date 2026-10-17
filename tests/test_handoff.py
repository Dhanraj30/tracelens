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
    assert {h["suspect"]: h["status"] for h in rec["hypotheses"]} == {scenarios["A"]["root_cause_deployment"]: "supported"}
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
    assert by["dep-chk-1405"]["status"] == "rejected" and "temporal_precedence" in by["dep-chk-1405"]["failed_checks"]
    assert by["dep-auth-1100"]["status"] == "rejected" and "in_window" in by["dep-auth-1100"]["failed_checks"]
    assert rec["action"] is None
    # stored as rejected -> recall must NOT surface it as memory for Scenario B
    rows, _ = fresh_memory.read("MATCH (inv:Investigation {id: $id})-[:CONSIDERED]->(h) RETURN h.status, count(h)", {"id": rec["investigation_id"]})
    assert rows == [["rejected", 2]]
    b = Investigator(fresh_memory).run(incident_id=scenarios["B"]["incident_id"])
    assert b["outcome"] == "root_cause_supported" and b["memory"] is None
