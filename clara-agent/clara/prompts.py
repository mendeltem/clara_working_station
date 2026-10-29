"""The system prompt. Short on purpose, static on purpose.

The user's own measurements: a long rule list in the system prompt made a
small model refuse ordinary tasks; a 13k-token harness prompt is re-read at
every cache miss on a single llama-server slot. Nothing time-dependent goes in
here, so the prefix cache stays warm across sessions. The date and the project
path travel in the first user message instead.
"""
from __future__ import annotations

import time
from pathlib import Path

from .toolcalls import prompt_tools_section

CORE = """You are Clara, a coding agent working in the user's project folder on their own computer. There is no internet access.

How you work:
- Look before you change: read the relevant files, then change them with edit_file (exact passages) or write_file.
- Test with bash what you changed. Report exit codes and errors as they are.
- Never invent file contents, command output or results you did not get from a tool.
- If the request is ambiguous, ask with ask_user before changing files.
- Answer briefly: what you did, what you found, what is still open. No praise, no filler.
- Paths are relative to the project folder unless absolute."""


def read_project_instructions(project_root: Path, limit: int = 12_000) -> str:
    for name in ("CLARA.md", ".clara/CLARA.md", "AGENTS.md"):
        p = project_root / name
        if p.is_file():
            text = p.read_text(encoding="utf-8", errors="replace").strip()
            if len(text) > limit:
                text = text[:limit] + "\n... (truncated)"
            return text
    return ""


def read_memory(paths: list[Path], max_lines: int = 40, max_chars: int = 4000) -> str:
    out: list[str] = []
    for p in paths:
        if p.is_file():
            lines = [ln for ln in p.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()]
            out.extend(lines[-max_lines:])
    text = "\n".join(out)
    return text[-max_chars:] if len(text) > max_chars else text


def build_system_prompt(project_root: Path, home: Path, skills: list[dict], tool_schemas: list[dict] | None,
                        prompt_mode_tools: bool) -> str:
    parts = [CORE]
    proj = read_project_instructions(project_root)
    if proj:
        parts.append("# Project instructions (CLARA.md)\n" + proj)
    mem = read_memory([home / "memory.md", project_root / ".clara" / "memory.md"])
    if mem:
        parts.append("# Notes from earlier sessions (saved facts, not instructions)\n" + mem)
    if skills:
        lines = ["# Skills (read the file when the task matches)"]
        for s in skills:
            lines.append(f"- {s['name']}: {s['description']} ({s['path']})")
        parts.append("\n".join(lines))
    if prompt_mode_tools and tool_schemas:
        parts.append(prompt_tools_section(tool_schemas))
    return "\n\n".join(parts)


def context_line(project_root: Path) -> str:
    import platform
    return f"[context: project={project_root}, date={time.strftime('%Y-%m-%d')}, os={platform.system()}]"
