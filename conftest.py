"""Root pytest fixtures, shared by tests/ and agents/langgraph_guest/tests/.

Tests run against a REAL FalkorDB (docker compose up -d falkordb). They use a separate graph
key `tracelens_test` so the demo graph is never touched."""
from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

os.environ.setdefault("GRAPH_NAME", "tracelens_test")

from backend.db import Graph  # noqa: E402
from backend.seed import clear_memory, reset_and_seed  # noqa: E402

SCENARIOS = yaml.safe_load((Path(__file__).parent / "tests" / "scenarios.yaml").read_text())["scenarios"]


@pytest.fixture(scope="session")
def graph() -> Graph:
    g = Graph("tracelens_test")
    if not g.ping():
        pytest.skip("FalkorDB not reachable (docker compose up -d falkordb)")
    reset_and_seed(g)
    return g


@pytest.fixture()
def fresh_memory(graph: Graph) -> Graph:
    clear_memory(graph)
    return graph


@pytest.fixture(scope="session")
def scenarios() -> dict:
    return SCENARIOS
