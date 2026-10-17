"""The MCP server exposes the catalog (not raw Cypher) and enforces the same guards, in-process."""
from __future__ import annotations

import asyncio
import json

from mcp import Client


def _data(result):
    from backend.mcp_client_demo import data

    return data(result)


def test_mcp_lists_catalog_and_guards_unknown_names(fresh_memory, monkeypatch):
    import backend.mcp_server as srv

    srv._ctx = None
    monkeypatch.setattr(srv, "get_graph", lambda: fresh_memory)

    async def run():
        async with Client(srv.mcp) as client:
            tools = {t.name for t in (await client.list_tools()).tools}
            assert {"get_incident", "upstream_dependencies", "path_between", "recall", "record_investigation", "investigate"} <= tools
            assert not any("cypher" in t.lower() or "query" in t.lower() for t in tools)  # no raw-query tool
            ok = _data(await client.call_tool("upstream_dependencies", {"service": "checkout", "depth": 2}))
            assert ok["ok"] and {r["service"] for r in ok["rows"]} >= {"payments-api", "cart"}
            bad = _data(await client.call_tool("upstream_dependencies", {"service": "warehouse-robot", "depth": 2}))
            assert bad["ok"] is False and bad["kind"] == "unresolved"
            path = _data(await client.call_tool("path_between", {"from_service": "cart", "to_service": "payments-api"}))
            assert path["connected"] is False

    asyncio.run(run())
