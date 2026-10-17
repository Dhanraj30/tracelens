"""Run a REPLAYED guest investigation (no LLM) into a tenant graph — used by the Playwright e2e tests.

Usage: python -m evals.e2e_guest --tenant e2e --incident inc-1042 [--naive]
  --naive  replays a guest that blames the checkout deploy, so the server rejects its proposal.
Provenance is "mcp:langgraph-replay" (TRACELENS_REPLAY=1) so the dashboard labels it as a replay.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.environ["TRACELENS_REPLAY"] = "1"

from backend.db import get_graph  # noqa: E402
from backend.seed import seed_if_empty  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tenant", required=True)
    ap.add_argument("--incident", default="inc-1042")
    ap.add_argument("--naive", action="store_true")
    args = ap.parse_args()

    import backend.mcp_server as srv
    from agents.langgraph_guest.agent import run_guest
    from agents.langgraph_guest.fake_model import ReplayToolCallingModel
    from evals.replays import script_for

    g = get_graph(args.tenant)
    seed_if_empty(g)
    srv._ctx = None
    srv._session["trace_start"] = 0
    srv.get_graph = lambda: g
    if args.naive:
        from agents.langgraph_guest.tests.test_guest import SCRIPT_NAIVE as script
        source = "naive replay"
    else:
        script, source = script_for("A" if args.incident == "inc-1042" else "C")
    out = asyncio.run(run_guest(args.incident, server=srv.mcp, model=ReplayToolCallingModel(script=list(script)), fallbacks=[], log=lambda s: None))
    rec = out.get("recorded") or {}
    print(json.dumps({"tenant": g.name, "source": source, "tool_calls": len(out["tool_calls"]), "recorded_id": rec.get("investigation_id"),
                      "agent": rec.get("agent"), "verified_outcome": rec.get("verified_outcome"),
                      "hypotheses": rec.get("hypotheses")}))


if __name__ == "__main__":
    main()
