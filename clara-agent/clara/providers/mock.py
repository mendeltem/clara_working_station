"""A scripted model for tests and for `clara --mock` demonstrations.

The script is a list of turns. Each turn is either a string (the model answers
with that text) or a dict {"text": ..., "tool_calls": [{"name":..., "arguments":...}]}.
A string containing "<tool_call>" is streamed as raw text, which exercises the
same parser that handles servers without native tool calling.
"""
from __future__ import annotations

from typing import Iterator

from ..toolcalls import StreamRouter, ToolCall
from .base import Event, Provider


class MockProvider(Provider):
    name = "mock"

    def __init__(self, script: list | None = None, chunk: int = 7, n_ctx: int | None = 32768):
        self.script = list(script or [])
        self.chunk = chunk
        self.calls: list[dict] = []       # every request, for assertions
        self.n_ctx = n_ctx

    def context_size(self) -> int | None:
        return self.n_ctx

    def count_tokens(self, text: str) -> int | None:
        return None

    def stream(self, messages: list[dict], tools: list[dict] | None, **opts) -> Iterator[Event]:
        self.calls.append({"messages": [dict(m) for m in messages], "tools": tools, "opts": opts})
        if not self.script:
            turn = "I have nothing scripted for this request."
        else:
            turn = self.script.pop(0)
        if isinstance(turn, str):
            router = StreamRouter()
            for i in range(0, len(turn), self.chunk):
                for kind, text in router.push(turn[i:i + self.chunk]):
                    yield Event(kind, text)
            calls, text, reasoning = router.finish()
            for tc in calls:
                yield Event("tool_call", tool_call=tc)
            yield Event("usage", data={"usage": {"prompt_tokens": sum(len(str(m.get("content", ""))) // 4 for m in messages),
                                                 "completion_tokens": len(turn) // 4}, "timings": {}})
            yield Event("done", text=text, data={"finish_reason": "tool_calls" if calls else "stop",
                                                 "reasoning": reasoning, "native_tool_calls": False})
            return
        text = turn.get("text", "")
        if turn.get("reasoning"):
            yield Event("reasoning", turn["reasoning"])
        if text:
            yield Event("text", text)
        calls = [ToolCall(name=c["name"], arguments=dict(c.get("arguments", {}))) for c in turn.get("tool_calls", [])]
        for tc in calls:
            yield Event("tool_call", tool_call=tc)
        yield Event("usage", data={"usage": {"prompt_tokens": 100, "completion_tokens": 20}, "timings": {}})
        yield Event("done", text=text, data={"finish_reason": "tool_calls" if calls else "stop",
                                             "reasoning": turn.get("reasoning", ""), "native_tool_calls": bool(calls)})
