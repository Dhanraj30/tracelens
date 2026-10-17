"""Offline eval harness runs in pytest: replay model, no network, isolated tenant graphs, generated report."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("langchain.mcp")
pytest.importorskip("langgraph")

from backend.db import Graph  # noqa: E402
from evals import run_evals  # noqa: E402


def test_offline_evals_all_correct_and_isolated(graph, tmp_path):
    out = run_evals.run(live=False, runs=1, max_requests=50, results_dir=tmp_path / "results", docs_path=tmp_path / "EVALS.md", log=lambda s: None)
    summ = out["summary"]
    assert summ["agent"]["total"] == 5 and summ["agent"]["verified_correct"] == 5, [r["failure_reasons"] for r in out["results"] if r["mode"] == "agent"]
    assert summ["baseline"]["total"] == 5 and summ["baseline"]["verified_correct"] == 5
    by = {(r["mode"], r["case"]): r for r in out["results"]}
    assert by[("agent", "A")]["ruled_out_how"]["dep-chk-1405"] in ("never_proposed", "proposed_and_rejected")
    assert by[("agent", "A")]["source"].startswith("replay of live run")
    assert by[("agent", "B_after_A")]["memory_used"] is True and by[("agent", "B_alone")]["memory_used"] is False
    assert by[("agent", "unknown")]["tool_calls"] == 1 and by[("agent", "unknown")]["recorded_id"] is None
    assert by[("baseline", "B_after_A")]["memory_used"] is True and by[("baseline", "B_alone")]["memory_used"] is False
    assert out["meta"]["llm_requests_used"] > 0  # the fake model's calls are metered like real ones
    # eval tenant graphs were deleted
    names = set(Graph("tracelens_test")._db.list_graphs())
    assert not any(n.startswith("tracelens_test:eval-") for n in names)
    # report + raw written
    md = (tmp_path / "EVALS.md").read_text(encoding="utf-8")
    assert "LLM agent: 5/5 verified correct; deterministic baseline: 5/5" in md and "Failures and why" in md
    raw = json.loads(Path(out["raw_path"]).read_text(encoding="utf-8"))
    assert raw["meta"]["mode"] == "offline" and len(raw["results"]) == 10


def test_budget_stops_cleanly(graph, tmp_path):
    out = run_evals.run(live=False, runs=1, max_requests=3, results_dir=tmp_path / "results", docs_path=tmp_path / "EVALS.md", log=lambda s: None)
    agent_rows = [r for r in out["results"] if r["mode"] == "agent"]
    assert any(str(r.get("error", "")).startswith("budget") for r in agent_rows)
    assert out["meta"]["llm_requests_used"] <= 3
    assert not any(n.startswith("tracelens_test:eval-") for n in Graph("tracelens_test")._db.list_graphs())
