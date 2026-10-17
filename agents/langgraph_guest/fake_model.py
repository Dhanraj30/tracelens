"""A chat model that replays a recorded tool-call sequence. No network. Used by the guest tests."""
from __future__ import annotations

from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult


class ReplayToolCallingModel(BaseChatModel):
    """Each model call emits the next scripted step: either an AIMessage with tool_calls, or a final text."""

    script: list[dict[str, Any]]  # [{"tool": name, "args": {...}}, ..., {"final": "text"}]
    cursor: int = 0

    @property
    def _llm_type(self) -> str:
        return "replay-tool-calling"

    def bind_tools(self, tools: Any, **kwargs: Any) -> "ReplayToolCallingModel":  # create_agent requires this
        return self

    def _generate(self, messages: list[BaseMessage], stop: list[str] | None = None, run_manager: Any = None, **kwargs: Any) -> ChatResult:
        if self.cursor >= len(self.script):
            msg = AIMessage(content="(script exhausted)")
        else:
            step = self.script[self.cursor]
            self.cursor += 1
            if "final" in step:
                msg = AIMessage(content=step["final"])
            else:
                msg = AIMessage(content="", tool_calls=[{"name": step["tool"], "args": step["args"], "id": f"call_{self.cursor}", "type": "tool_call"}])
        msg.response_metadata = {"model_name": "replay-fake"}
        return ChatResult(generations=[ChatGeneration(message=msg)])
