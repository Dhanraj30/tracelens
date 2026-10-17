"""Thin FalkorDB access layer.

Every query that the agent depends on goes through `Graph.read` / `Graph.write`, which
record a `QueryRecord` (exact Cypher text, params, duration, row count). That record is
what the UI's "Show Cypher" toggle displays — the trace is a product feature, not a log.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

from falkordb import FalkorDB
from falkordb.node import Node
from falkordb.edge import Edge
from falkordb.path import Path

from .config import settings


@dataclass
class QueryRecord:
    cypher: str
    params: dict[str, Any]
    duration_ms: float
    row_count: int
    read_only: bool
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def to_plain(value: Any) -> Any:
    """Convert FalkorDB result values (Node/Edge/Path) into JSON-friendly dicts.

    Nodes become {"_id", "_labels", ...properties}; edges {"_id", "_type", "_src", "_dst", ...props};
    paths {"nodes": [...], "edges": [...]}. Everything else passes through recursively.
    """
    if isinstance(value, Node):
        d = {"_id": value.id, "_labels": list(value.labels or [])}
        d.update(value.properties or {})
        return d
    if isinstance(value, Edge):
        d = {"_id": value.id, "_type": value.relation, "_src": value.src_node, "_dst": value.dest_node}
        d.update(value.properties or {})
        return d
    if isinstance(value, Path):
        return {"nodes": [to_plain(n) for n in value.nodes()], "edges": [to_plain(e) for e in value.edges()]}
    if isinstance(value, dict):
        return {k: to_plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_plain(v) for v in value]
    return value


class Graph:
    """A named FalkorDB graph with traced read/write helpers."""

    def __init__(self, name: str | None = None, host: str | None = None, port: int | None = None):
        self.name = name or settings.graph_name
        self._db = FalkorDB(host=host or settings.falkordb_host, port=port or settings.falkordb_port)
        self._g = self._db.select_graph(self.name)
        self.records: list[QueryRecord] = []

    # -- raw helpers -----------------------------------------------------------------
    def _run(self, cypher: str, params: dict[str, Any] | None, read_only: bool) -> tuple[list[list[Any]], QueryRecord]:
        params = params or {}
        fn = self._g.ro_query if read_only else self._g.query
        start = time.perf_counter()
        try:
            res = fn(cypher, params, timeout=settings.query_timeout_ms)
        except Exception as exc:  # noqa: BLE001 - we record and re-raise
            rec = QueryRecord(cypher.strip(), params, (time.perf_counter() - start) * 1000, 0, read_only, error=str(exc))
            self.records.append(rec)
            raise
        rows = [[to_plain(v) for v in row] for row in res.result_set]
        rec = QueryRecord(cypher.strip(), params, (time.perf_counter() - start) * 1000, len(rows), read_only)
        self.records.append(rec)
        return rows, rec

    def read(self, cypher: str, params: dict[str, Any] | None = None) -> tuple[list[list[Any]], QueryRecord]:
        """Read-only query (GRAPH.RO_QUERY). FalkorDB rejects writes here — a safety net for the tool catalog."""
        return self._run(cypher, params, read_only=True)

    def write(self, cypher: str, params: dict[str, Any] | None = None) -> tuple[list[list[Any]], QueryRecord]:
        return self._run(cypher, params, read_only=False)

    # -- admin -----------------------------------------------------------------------
    def ping(self) -> bool:
        """Server reachability (works even before the graph key exists)."""
        try:
            return bool(self._db.connection.ping())
        except Exception:  # noqa: BLE001
            return False

    def exists(self) -> bool:
        return self.name in self._db.list_graphs()

    def node_count(self) -> int:
        if not self.exists():
            return 0
        rows, _ = self.read("MATCH (n) RETURN count(n)")
        return int(rows[0][0]) if rows else 0

    def delete(self) -> None:
        if self.exists():
            self._g.delete()
            self._g = self._db.select_graph(self.name)

    def existing_indexes(self) -> set[tuple[str, str]]:
        if not self.exists():
            return set()
        rows, _ = self.read("CALL db.indexes() YIELD label, properties RETURN label, properties")
        out: set[tuple[str, str]] = set()
        for label, props in rows:
            for p in props:
                out.add((label, p))
        return out

    def ensure_index(self, label: str, prop: str) -> bool:
        """CREATE INDEX is not idempotent in FalkorDB (verified: 'Attribute already indexed'), so check first."""
        if (label, prop) in self.existing_indexes():
            return False
        self.write(f"CREATE INDEX FOR (n:{label}) ON (n.{prop})")
        return True


_default: Graph | None = None


def get_graph() -> Graph:
    global _default
    if _default is None:
        _default = Graph()
    return _default
