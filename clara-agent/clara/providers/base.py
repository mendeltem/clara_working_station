from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator

from ..toolcalls import ToolCall


class ProviderError(RuntimeError):
    """The model server answered with an error or could not be reached."""


@dataclass
class Event:
    kind: str                      # text | reasoning | tool_call | usage | status | done
    text: str = ""
    tool_call: ToolCall | None = None
    data: dict = field(default_factory=dict)


class Provider:
    """One chat request as a stream of events. Implementations are stateless."""
    name = "base"
    supports_native_tools = True

    def stream(self, messages: list[dict], tools: list[dict] | None, **opts) -> Iterator[Event]:
        raise NotImplementedError

    def context_size(self) -> int | None:
        """Server context window in tokens, or None when the server does not say.
        Callers must treat None as unknown, never as a number."""
        return None

    def count_tokens(self, text: str) -> int | None:
        """Exact token count when the server offers /tokenize, else None."""
        return None
