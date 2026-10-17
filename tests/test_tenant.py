"""Tenant isolation: one FalkorDB graph key per tenant; memory written in tenant A is invisible in tenant B."""
from __future__ import annotations

import pytest

from backend.agent.engine import Investigator
from backend.db import Graph, graph_name_for
from backend.seed import reset_and_seed


def test_tenant_slug_validation():
    assert graph_name_for(None).endswith("tracelens_test") or graph_name_for(None)
    assert graph_name_for("acme-eu").endswith(":acme-eu")
    for bad in ("Acme", "a b", "x" * 33, "../etc", "a;b", ""):
        if bad == "":
            assert graph_name_for(bad) == graph_name_for(None)  # empty = default tenant
            continue
        with pytest.raises(ValueError):
            graph_name_for(bad)


def test_memory_written_in_tenant_a_is_invisible_in_tenant_b(graph, scenarios):
    ga, gb = Graph(graph_name_for("tenant-a")), Graph(graph_name_for("tenant-b"))
    try:
        reset_and_seed(ga)
        reset_and_seed(gb)
        a = Investigator(ga).run(incident_id=scenarios["A"]["incident_id"])
        assert a["outcome"] == "root_cause_supported"
        # same incident B: tenant A answers from memory, tenant B must do the full investigation
        b_in_a = Investigator(ga).run(incident_id=scenarios["B"]["incident_id"])
        b_in_b = Investigator(gb).run(incident_id=scenarios["B"]["incident_id"])
        assert b_in_a["outcome"] == "answered_from_memory" and b_in_a["memory"]["investigation_id"] == a["id"]
        assert b_in_b["outcome"] == "root_cause_supported" and b_in_b["memory"] is None
        rows, _ = gb.read("MATCH (inv:Investigation) RETURN count(inv)")
        assert int(rows[0][0]) == 1  # only B's own write-back; nothing leaked from tenant A
        rows, _ = ga.read("MATCH (inv:Investigation) RETURN count(inv)")
        assert int(rows[0][0]) == 2
    finally:
        ga.delete()
        gb.delete()
