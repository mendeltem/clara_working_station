"""Parsing of model output: <think> blocks and Hermes/Qwen style <tool_call> blocks.

Qwen3 (and every model trained on the Hermes function-calling format) emits

    <tool_call>
    {"name": "read_file", "arguments": {"path": "x.py"}}
    </tool_call>

llama-server with --jinja parses that itself and returns OpenAI `tool_calls`;
Ollama and vLLM do the same. Servers without a parser return the raw text, and
this module turns it into the same structure, so Clara works with both.
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field

TOOL_CALL_RE = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.DOTALL)
THINK_RE = re.compile(r"<think>(.*?)</think>", re.DOTALL)
# A call that was started but never closed (model stopped early): keep it visible.
OPEN_TOOL_CALL_RE = re.compile(r"<tool_call>\s*(\{.*)$", re.DOTALL)


@dataclass
class ToolCall:
    name: str
    arguments: dict
    id: str = field(default_factory=lambda: "call_" + uuid.uuid4().hex[:12])
    raw: str = ""

    def to_openai(self) -> dict:
        return {"id": self.id, "type": "function",
                "function": {"name": self.name, "arguments": json.dumps(self.arguments, ensure_ascii=False)}}


def _loads_lenient(text: str) -> dict | None:
    """Parse JSON the way small models write it: trailing commas, single-quoted
    keys and code fences are repaired; anything else is a parse failure."""
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:json)?\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    try:
        v = json.loads(t)
        return v if isinstance(v, dict) else None
    except json.JSONDecodeError:
        pass
    t2 = re.sub(r",\s*([}\]])", r"\1", t)             # trailing commas
    t2 = re.sub(r"(?<=[{,])\s*'([^']+)'\s*:", r' "\1":', t2)  # single-quoted keys
    try:
        v = json.loads(t2)
        return v if isinstance(v, dict) else None
    except json.JSONDecodeError:
        return None


def parse_tool_call_json(text: str) -> ToolCall | None:
    obj = _loads_lenient(text)
    if not obj:
        return None
    name = obj.get("name") or (obj.get("function") or {}).get("name")
    args = obj.get("arguments")
    if args is None:
        args = obj.get("parameters")
    if args is None and "function" in obj and isinstance(obj["function"], dict):
        args = obj["function"].get("arguments")
    if isinstance(args, str):
        parsed = _loads_lenient(args)
        args = parsed if parsed is not None else {"_raw": args}
    if not name or not isinstance(name, str):
        return None
    if args is None:
        args = {}
    if not isinstance(args, dict):
        args = {"_raw": args}
    return ToolCall(name=name, arguments=args, raw=text)


def split_think(text: str) -> tuple[str, str]:
    """Return (reasoning, visible_text) for a complete message."""
    reasoning = "\n".join(m.strip() for m in THINK_RE.findall(text))
    visible = THINK_RE.sub("", text)
    # An unclosed <think> means the model was cut off while reasoning.
    if "<think>" in visible and "</think>" not in visible:
        head, _, tail = visible.partition("<think>")
        reasoning = (reasoning + "\n" + tail).strip()
        visible = head
    return reasoning.strip(), visible.strip()


def extract_tool_calls(text: str) -> tuple[list[ToolCall], str]:
    """Return (tool_calls, text_without_tool_call_blocks)."""
    calls: list[ToolCall] = []
    for m in TOOL_CALL_RE.finditer(text):
        tc = parse_tool_call_json(m.group(1))
        if tc:
            calls.append(tc)
    rest = TOOL_CALL_RE.sub("", text)
    if not calls:
        m = OPEN_TOOL_CALL_RE.search(rest)
        if m:
            tc = parse_tool_call_json(m.group(1))
            if tc:
                calls.append(tc)
                rest = rest[: m.start()]
    return calls, rest.strip()


class StreamRouter:
    """Routes streamed content into reasoning / text / held-back tool-call text.

    Feed deltas with push(); it returns a list of (kind, text) pairs to emit
    right away. Call finish() at the end to get the parsed tool calls and any
    text that was held back because it looked like the start of a tag.
    """
    OPEN_THINK = "<think>"
    CLOSE_THINK = "</think>"
    OPEN_CALL = "<tool_call>"
    CLOSE_CALL = "</tool_call>"

    def __init__(self) -> None:
        self.buf = ""
        self.mode = "text"           # text | think | call
        self.all_text = ""
        self.all_reasoning = ""
        self.call_text = ""
        self.started = False

    def push(self, delta: str) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        self.buf += delta
        while self.buf:
            if self.mode == "text":
                if not self.started:
                    stripped = self.buf.lstrip()
                    if stripped != self.buf and not stripped:
                        break      # only whitespace so far: wait
                    if self.OPEN_THINK.startswith(stripped[: len(self.OPEN_THINK)]) and stripped != self.OPEN_THINK and len(stripped) < len(self.OPEN_THINK):
                        break      # could still become <think>
                    if stripped.startswith(self.OPEN_THINK):
                        self.buf = stripped[len(self.OPEN_THINK):]
                        self.mode = "think"
                        self.started = True
                        continue
                    self.started = True
                idx = self.buf.find("<")
                if idx == -1:
                    out.append(("text", self.buf)); self.all_text += self.buf; self.buf = ""
                    break
                head, tail = self.buf[:idx], self.buf[idx:]
                if head:
                    out.append(("text", head)); self.all_text += head
                if tail.startswith(self.OPEN_CALL):
                    self.buf = tail[len(self.OPEN_CALL):]; self.mode = "call"; continue
                if tail.startswith(self.OPEN_THINK):
                    self.buf = tail[len(self.OPEN_THINK):]; self.mode = "think"; continue
                if self.OPEN_CALL.startswith(tail) or self.OPEN_THINK.startswith(tail):
                    self.buf = tail; break          # partial tag: wait for more
                out.append(("text", "<")); self.all_text += "<"; self.buf = tail[1:]
            elif self.mode == "think":
                idx = self.buf.find(self.CLOSE_THINK)
                if idx == -1:
                    keep = len(self.CLOSE_THINK) - 1
                    emit, self.buf = self.buf[:-keep] if len(self.buf) > keep else "", self.buf[-keep:] if len(self.buf) > keep else self.buf
                    if emit:
                        out.append(("reasoning", emit)); self.all_reasoning += emit
                    break
                emit = self.buf[:idx]
                if emit:
                    out.append(("reasoning", emit)); self.all_reasoning += emit
                self.buf = self.buf[idx + len(self.CLOSE_THINK):]
                self.mode = "text"
            else:  # call
                idx = self.buf.find(self.CLOSE_CALL)
                if idx == -1:
                    keep = len(self.CLOSE_CALL) - 1
                    if len(self.buf) > keep:
                        self.call_text += self.buf[:-keep]; self.buf = self.buf[-keep:]
                    break
                self.call_text += self.buf[:idx] + "\n"
                self.buf = self.buf[idx + len(self.CLOSE_CALL):]
                self.mode = "text"
        return out

    def finish(self) -> tuple[list[ToolCall], str, str]:
        """Flush. Returns (tool_calls, text, reasoning)."""
        if self.mode == "call":
            self.call_text += self.buf
        elif self.mode == "think":
            self.all_reasoning += self.buf
        else:
            self.all_text += self.buf
        self.buf = ""
        calls: list[ToolCall] = []
        for chunk in re.split(r"\n(?=\s*\{)", self.call_text.strip()):
            chunk = chunk.strip()
            if not chunk:
                continue
            tc = parse_tool_call_json(chunk)
            if tc:
                calls.append(tc)
        return calls, self.all_text.strip(), self.all_reasoning.strip()


# ------------------------------------------------------------- tool schema helpers

def openai_tool_schema(name: str, description: str, parameters: dict) -> dict:
    return {"type": "function", "function": {"name": name, "description": description, "parameters": parameters}}


def prompt_tools_section(schemas: list[dict]) -> str:
    """Tool description block in the wording of Qwen's own chat template, so a
    model that was trained on it recognises the format."""
    lines = ["# Tools", "",
             "You may call one or more functions to assist with the user query.", "",
             "You are provided with function signatures within <tools></tools> XML tags:",
             "<tools>"]
    for s in schemas:
        lines.append(json.dumps(s, ensure_ascii=False, separators=(",", ":")))
    lines += ["</tools>", "",
              "For each function call, return a json object with function name and arguments within <tool_call></tool_call> XML tags:",
              "<tool_call>",
              '{"name": <function-name>, "arguments": <args-json-object>}',
              "</tool_call>"]
    return "\n".join(lines)
