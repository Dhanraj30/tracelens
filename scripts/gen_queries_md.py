"""Generate docs/QUERIES.md from the tool catalog (single source of truth).

Usage:  python -m scripts.gen_queries_md
"""
from __future__ import annotations

from pathlib import Path

from backend.tools import REGISTRY
from backend.tools.base import RESOLVE_INCIDENT, RESOLVE_SERVICES

OUT = Path(__file__).resolve().parent.parent / "docs" / "QUERIES.md"

HEADER = """# TraceLens — Cypher queries and graph algorithms

> **Generated from `backend/tools/catalog.py` by `scripts/gen_queries_md.py`. Do not edit by hand.**
>
> The agent never writes Cypher. Every query below is person-written, parameterized (`$param`), and
> wrapped in a tool whose arguments are typed and validated. The only values ever inlined are integers
> that passed a range check (FalkorDB does not accept a parameter as a variable-length hop bound).
> Reads run through `GRAPH.RO_QUERY`; only the two `record_*` tools write.

## Entity resolution (runs before any tool query)

Every service / incident name is resolved against the graph first. If it does not resolve, the step
stops and **no tool query runs**.

```cypher
{resolve_services}
```

```cypher
{resolve_incident}
```

## Tool catalog

| # | Tool | Writes | What a result proves |
|---|------|--------|----------------------|
{table}

"""


def main() -> None:
    table = "\n".join(
        f"| {i} | `{s.name}` | {'yes' if s.writes else 'no'} | {s.proves} |" for i, s in enumerate(REGISTRY.values(), start=1)
    )
    parts = [HEADER.format(resolve_services=RESOLVE_SERVICES.strip(), resolve_incident=RESOLVE_INCIDENT.strip(), table=table)]
    for i, s in enumerate(REGISTRY.values(), start=1):
        parts.append(f"### {i}. `{s.name}`\n\n{s.description}\n\n**Proves:** {s.proves}\n\n**Arguments:** " + ", ".join(f"`{k}` ({v})" for k, v in s.params.items()) + "\n")
        for q in s.cypher:
            parts.append("```cypher\n" + q.strip() + "\n```\n")
    algos = [
        ("`algo.SPpaths`", "`path_between`", "weighted shortest path (weightProp `weight`: sync=1, async=2), `relDirection: 'outgoing'`, `maxLen: 6`"),
        ("`algo.pageRank`", "`critical_services`", "`('Service','DEPENDS_ON')` — which services most things transitively depend on"),
        ("`algo.betweenness`", "`critical_services`", "`{nodeLabels:['Service'], relationshipTypes:['DEPENDS_ON'], samplingSeed: 7}` — which services sit on the most dependency paths"),
    ]
    parts.append("## Graph algorithms used\n\n| Procedure | Tool | How |\n|---|---|---|\n" + "\n".join(f"| {a} | {b} | {c} |" for a, b, c in algos) + "\n")
    parts.append("Signatures verified against https://docs.falkordb.com/algorithms on FalkorDB v4.22.1 (2026-10-17).\n")
    OUT.write_text("\n".join(parts), encoding="utf-8")
    print(f"wrote {OUT} ({len(REGISTRY)} tools)")


if __name__ == "__main__":
    main()
