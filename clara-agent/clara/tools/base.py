from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..toolcalls import openai_tool_schema

MAX_OUTPUT_CHARS = 30_000


@dataclass
class ToolResult:
    text: str
    is_error: bool = False
    data: dict = field(default_factory=dict)


@dataclass
class ToolContext:
    project_root: Path
    home: Path
    session_id: str
    ask_user: Callable[[str], str | None] | None = None
    status: Callable[[str], None] | None = None
    delegate: Callable[[str, str], str] | None = None
    todo_state: list = field(default_factory=list)
    backups_done: set = field(default_factory=set)
    shell_timeout: int = 120

    def resolve(self, path: str) -> Path:
        p = Path(os.path.expanduser(path))
        if not p.is_absolute():
            p = self.project_root / p
        return p.resolve()

    def backup(self, path: Path) -> Path | None:
        """Copy a file once per session before its first modification."""
        key = str(path)
        if key in self.backups_done or not path.is_file():
            return None
        dest_dir = self.home / "backups" / self.session_id
        dest_dir.mkdir(parents=True, exist_ok=True)
        rel = key.lstrip("/").replace("\\", "/").replace("/", "__")
        dest = dest_dir / rel
        try:
            dest.write_bytes(path.read_bytes())
        except OSError:
            return None
        self.backups_done.add(key)
        return dest


def clip(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    head = text[: limit // 2]
    tail = text[-limit // 2:]
    return f"{head}\n... [{len(text) - limit} characters omitted] ...\n{tail}"


class Tool:
    name: str = ""
    description: str = ""
    parameters: dict = {"type": "object", "properties": {}}
    kind: str = "read"        # read | write | exec | meta

    def schema(self) -> dict:
        return openai_tool_schema(self.name, self.description, self.parameters)

    def summary(self, args: dict) -> str:
        """One line for the screen and the permission prompt."""
        keys = list(self.parameters.get("properties", {}).keys())
        first = args.get(keys[0]) if keys else None
        s = str(first) if first is not None else ""
        s = s.replace("\n", " ")
        return f"{self.name}({s[:120]})"

    def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        raise NotImplementedError


class ToolRegistry:
    def __init__(self, tools: list[Tool] | None = None):
        self.tools: dict[str, Tool] = {}
        for t in tools or []:
            self.add(t)

    def add(self, tool: Tool) -> None:
        self.tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self.tools.get(name)

    def names(self) -> list[str]:
        return list(self.tools)

    def schemas(self) -> list[dict]:
        return [t.schema() for t in self.tools.values()]

    def subset(self, names) -> "ToolRegistry":
        return ToolRegistry([t for n, t in self.tools.items() if n in set(names)])


def default_registry(include: list[str] | None = None, exclude: list[str] | None = None) -> ToolRegistry:
    from .files import ReadFile, WriteFile, EditFile, ListFiles
    from .search import Search
    from .shell import Bash
    from .misc import AskUser, Remember, Todo, Delegate
    tools: list[Tool] = [ReadFile(), WriteFile(), EditFile(), ListFiles(), Search(), Bash(),
                         AskUser(), Remember(), Todo(), Delegate()]
    if include:
        tools = [t for t in tools if t.name in set(include)]
    if exclude:
        tools = [t for t in tools if t.name not in set(exclude)]
    return ToolRegistry(tools)


def arg_str(args: dict, key: str, default: Any = None) -> Any:
    v = args.get(key, default)
    return v if v is not None else default
