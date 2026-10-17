"""Tool catalog infrastructure.

Rules enforced here (these are the judging-critical guarantees):
  * The agent never writes Cypher. Each tool owns a fixed, person-written query string.
  * Arguments are typed and validated before any query runs. No free text is ever
    concatenated into Cypher; the only inlined values are integers that passed a range check
    (FalkorDB rejects parameters as variable-length hop bounds — verified on v4.22.1).
  * Entity names (services, incidents) are resolved against the graph first. Unresolved names
    stop the step with ToolError — nothing else runs.
  * Reads use GRAPH.RO_QUERY. Only `record_*` tools write.
  * Every call is traced: tool, args, exact Cypher, params, duration, row count.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Callable, Optional

from ..db import Graph, QueryRecord

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9\-_.]{0,63}$")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9\-_.:]{0,63}$")


class ToolError(Exception):
    """Validation or resolution failure. The step stops; nothing is queried past this point."""

    def __init__(self, message: str, kind: str = "validation", unresolved: list[str] | None = None):
        super().__init__(message)
        self.kind = kind
        self.unresolved = unresolved or []


@dataclass
class ToolCall:
    """One traced invocation of a catalog tool (this is what the UI timeline renders)."""

    step: int
    tool: str
    args: dict[str, Any]
    queries: list[dict[str, Any]]
    duration_ms: float
    row_count: int
    status: str  # ok | error
    error: Optional[str] = None
    summary: str = ""
    purpose: str = ""
    evidence_ids: list[str] = field(default_factory=list)  # lets the UI highlight what this step touched
    paths: list[list[str]] = field(default_factory=list)
    rows_preview: list[dict[str, Any]] = field(default_factory=list)  # first rows, for the timeline

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ToolResult:
    tool: str
    rows: list[dict[str, Any]]
    evidence_ids: list[str] = field(default_factory=list)  # node IDs (business IDs) that justify the rows
    paths: list[list[str]] = field(default_factory=list)  # service-name paths, when the tool traverses
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"tool": self.tool, "rows": self.rows, "evidence_ids": self.evidence_ids, "paths": self.paths, **self.extra}


@dataclass
class ToolSpec:
    name: str
    description: str
    proves: str  # what a result of this tool demonstrates — used for docs/QUERIES.md
    cypher: list[str]
    writes: bool
    fn: Callable[..., ToolResult]
    params: dict[str, str]  # arg name -> type description for MCP/LLM schema


REGISTRY: dict[str, ToolSpec] = {}


def tool(name: str, description: str, proves: str, cypher: list[str], params: dict[str, str], writes: bool = False):
    def deco(fn: Callable[..., ToolResult]):
        REGISTRY[name] = ToolSpec(name, description, proves, cypher, writes, fn, params)
        return fn

    return deco


class ToolContext:
    """Holds the graph connection and the trace for one investigation (or one MCP session)."""

    def __init__(self, graph: Graph, max_calls: int = 12):
        self.graph = graph
        self.trace: list[ToolCall] = []
        self.max_calls = max_calls

    @property
    def tool_calls(self) -> int:
        return len(self.trace)

    def call(self, name: str, purpose: str = "", **args: Any) -> ToolResult:
        if name not in REGISTRY:
            raise ToolError(f"unknown tool '{name}'", kind="unknown_tool")
        if len(self.trace) >= self.max_calls:
            raise ToolError(f"tool budget of {self.max_calls} calls exhausted", kind="budget")
        spec = REGISTRY[name]
        step = len(self.trace) + 1
        start_idx = len(self.graph.records)
        t0 = time.perf_counter()
        try:
            result = spec.fn(self, **args)
        except ToolError as exc:
            self._log(step, spec, args, start_idx, t0, 0, "error", str(exc), purpose)
            raise
        except Exception as exc:  # noqa: BLE001
            self._log(step, spec, args, start_idx, t0, 0, "error", f"{type(exc).__name__}: {exc}", purpose)
            raise ToolError(f"{name} failed: {exc}", kind="query") from exc
        call = self._log(step, spec, args, start_idx, t0, len(result.rows), "ok", None, purpose)
        call.summary = result.extra.get("summary", f"{len(result.rows)} row(s)")
        call.evidence_ids = list(dict.fromkeys(result.evidence_ids))
        call.paths = result.paths
        call.rows_preview = _jsonable(result.rows[:8])
        return result

    def _log(self, step, spec, args, start_idx, t0, rows, status, error, purpose) -> ToolCall:
        queries: list[QueryRecord] = self.graph.records[start_idx:]
        call = ToolCall(
            step=step,
            tool=spec.name,
            args=_jsonable(args),
            queries=[q.to_dict() for q in queries],
            duration_ms=round((time.perf_counter() - t0) * 1000, 2),
            row_count=rows,
            status=status,
            error=error,
            purpose=purpose,
        )
        self.trace.append(call)
        return call


def _jsonable(v: Any) -> Any:
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple, set)):
        return [_jsonable(x) for x in v]
    return v


# ---------------------------------------------------------------------------------------
# Validation + resolution helpers
# ---------------------------------------------------------------------------------------
def validate_name(value: Any, what: str = "service") -> str:
    if not isinstance(value, str) or not NAME_RE.match(value):
        raise ToolError(f"invalid {what} name {value!r}", kind="validation")
    return value


def validate_id(value: Any, what: str = "id") -> str:
    if not isinstance(value, str) or not ID_RE.match(value):
        raise ToolError(f"invalid {what} {value!r}", kind="validation")
    return value


def validate_int(value: Any, what: str, lo: int, hi: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolError(f"{what} must be an integer", kind="validation")
    if value < lo or value > hi:
        raise ToolError(f"{what} must be between {lo} and {hi}", kind="validation")
    return value


def validate_epoch(value: Any, what: str) -> int:
    return validate_int(value, what, 0, 4_102_444_800)  # <= year 2100


def validate_names(values: Any, what: str = "service", max_items: int = 50) -> list[str]:
    if not isinstance(values, (list, tuple)) or not values:
        raise ToolError(f"{what}s must be a non-empty list", kind="validation")
    if len(values) > max_items:
        raise ToolError(f"too many {what}s (max {max_items})", kind="validation")
    return [validate_name(v, what) for v in values]


RESOLVE_SERVICES = "UNWIND $names AS nm MATCH (s:Service {name: nm}) RETURN collect(s.name)"
RESOLVE_INCIDENT = "MATCH (i:Incident {id: $id}) RETURN count(i)"


def resolve_services(ctx: ToolContext, names: list[str]) -> list[str]:
    """Return the names that exist in the graph; raise ToolError listing any that don't."""
    rows, _ = ctx.graph.read(RESOLVE_SERVICES, {"names": names})
    found = set(rows[0][0]) if rows else set()
    missing = [n for n in names if n not in found]
    if missing:
        raise ToolError(f"unknown service(s): {', '.join(missing)}", kind="unresolved", unresolved=missing)
    return names


def resolve_incident(ctx: ToolContext, incident_id: str) -> str:
    rows, _ = ctx.graph.read(RESOLVE_INCIDENT, {"id": incident_id})
    if not rows or int(rows[0][0]) == 0:
        raise ToolError(f"unknown incident: {incident_id}", kind="unresolved", unresolved=[incident_id])
    return incident_id


def rows_to_dicts(rows: list[list[Any]], columns: list[str]) -> list[dict[str, Any]]:
    return [dict(zip(columns, r)) for r in rows]
