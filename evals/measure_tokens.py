"""Approximate tokens per model request on the Scenario A replay (offline, no network).

Counts characters of the full message list sent to the model on every call and reports chars/4 as an
approximate token count (no tokenizer dependency; the ratio is stated, not hidden).
Usage: python -m evals.measure_tokens
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.environ.setdefault("GRAPH_NAME", "tracelens_test")

from langchain_core.messages import BaseMessage  # noqa: E402
from langchain_core.outputs import ChatResult  # noqa: E402

from agents.langgraph_guest.fake_model import ReplayToolCallingModel  # noqa: E402


class MeasuringReplay(ReplayToolCallingModel):
    sizes: list[int] = []

    @property
    def _llm_type(self) -> str:
        return "measuring-replay"

    def _generate(self, messages: list[BaseMessage], stop: list[str] | None = None, run_manager: Any = None, **kwargs: Any) -> ChatResult:
        total = 0
        for m in messages:
            c = m.content if isinstance(m.content, str) else json.dumps(m.content)
            total += len(c) + sum(len(json.dumps(tc)) for tc in (getattr(m, "tool_calls", None) or []))
        self.sizes.append(total)
        return super()._generate(messages, stop, run_manager, **kwargs)


def main() -> None:
    from backend.db import Graph, graph_name_for
    from backend.seed import reset_and_seed
    import backend.mcp_server as srv
    from agents.langgraph_guest.agent import run_guest
    from evals.replays import script_for

    g = Graph(graph_name_for("measure"))
    reset_and_seed(g)
    try:
        srv._ctx = None
        srv._session["trace_start"] = 0
        srv.get_graph = lambda: g
        script, _ = script_for("A")
        model = MeasuringReplay(script=script, sizes=[])
        asyncio.run(run_guest("inc-1042", server=srv.mcp, model=model, fallbacks=[], log=lambda s: None))
    finally:
        g.delete()
    sizes = model.sizes
    toks = [s // 4 for s in sizes]
    print(f"requests={len(sizes)}  chars/request={sizes}  approx_tokens/request(chars/4)={toks}")
    print(f"total approx tokens sent={sum(toks)}  max per request={max(toks)}  mean={sum(toks)//len(toks)}")


if __name__ == "__main__":
    main()
