"""Provider fallback = restart the whole run from scratch on the next provider (never continue another model's transcript)."""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

pytest.importorskip("langchain.mcp")
pytest.importorskip("langgraph")

from langchain_core.messages import BaseMessage  # noqa: E402
from langchain_core.outputs import ChatResult  # noqa: E402

from agents.langgraph_guest.agent import run_guest  # noqa: E402
from agents.langgraph_guest.fake_model import ReplayToolCallingModel  # noqa: E402
from agents.langgraph_guest.tests.test_guest import SCRIPT_GOOD, _server  # noqa: E402


class FlakyProvider(ReplayToolCallingModel):
    """Replays the script but blows up on the Nth model call, like a 429/400 from a free-tier endpoint."""

    fail_at: int = 3
    calls: int = 0

    @property
    def _llm_type(self) -> str:
        return "flaky-primary"

    def _generate(self, messages: list[BaseMessage], stop: list[str] | None = None, run_manager: Any = None, **kwargs: Any) -> ChatResult:
        self.calls += 1
        if self.calls == self.fail_at:
            raise RuntimeError("Error code: 429 - rate limit reached (simulated primary failure mid-run)")
        return super()._generate(messages, stop, run_manager, **kwargs)


def test_fallback_restarts_from_scratch_on_next_provider(fresh_memory, monkeypatch, scenarios):
    srv = _server(fresh_memory, monkeypatch)
    primary = FlakyProvider(script=list(SCRIPT_GOOD), fail_at=3)
    fallback = ReplayToolCallingModel(script=list(SCRIPT_GOOD))
    out = asyncio.run(run_guest("inc-1042", server=srv.mcp, model=primary, fallbacks=[fallback], log=lambda s: None))
    # the primary got two tool calls in, then failed; the fallback started over and completed the whole sequence
    assert primary.calls == 3
    assert len(out["restarts"]) == 1
    note = out["restarts"][0]["note"]
    assert note.startswith("restarted on ") and "after" in note and "429" in note
    assert out["restarts"][0]["calls_before_restart"] == 2
    # the transcript that was continued is the FALLBACK's own, from scratch: the final tool sequence equals the full script
    assert [c["tool"] for c in out["tool_calls"]] == [s["tool"] for s in SCRIPT_GOOD if "tool" in s]
    assert out["recorded"]["verified_outcome"] == "root_cause_supported"
    assert out["model_used"] == "replay-fake"
    # the restart note also travels into the guest's trace list so evals/recordings can show it
    assert any(c.get("tool") == "restart" for c in out["trace_notes"])


def test_all_providers_fail_raises_last_error(fresh_memory, monkeypatch):
    srv = _server(fresh_memory, monkeypatch)
    primary = FlakyProvider(script=list(SCRIPT_GOOD), fail_at=1)
    second = FlakyProvider(script=list(SCRIPT_GOOD), fail_at=2)
    with pytest.raises(RuntimeError, match="429"):
        asyncio.run(run_guest("inc-1042", server=srv.mcp, model=primary, fallbacks=[second], log=lambda s: None))
