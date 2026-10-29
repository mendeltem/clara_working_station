from __future__ import annotations

import fnmatch
import os
from pathlib import Path

from .base import Tool, ToolContext, ToolResult, clip

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache",
             ".tox", "dist", "build", ".clara"}
MAX_READ_BYTES = 2_000_000


def is_binary(sample: bytes) -> bool:
    if b"\x00" in sample:
        return True
    text_chars = bytes(range(32, 127)) + b"\n\r\t\b\x0c"
    nontext = sample.translate(None, text_chars)
    return len(sample) > 0 and len(nontext) / len(sample) > 0.30


class ReadFile(Tool):
    name = "read_file"
    description = "Read a text file with line numbers. Use offset/limit for long files."
    parameters = {"type": "object",
                  "properties": {"path": {"type": "string"},
                                 "offset": {"type": "integer", "description": "first line, 1-based"},
                                 "limit": {"type": "integer", "description": "max lines, default 400"}},
                  "required": ["path"]}
    kind = "read"

    def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        p = ctx.resolve(str(args.get("path", "")))
        if not p.exists():
            return ToolResult(f"not found: {p}", is_error=True)
        if p.is_dir():
            names = sorted(os.listdir(p))[:200]
            return ToolResult(f"{p} is a directory with {len(names)} entries:\n" + "\n".join(names))
        size = p.stat().st_size
        with open(p, "rb") as fh:
            sample = fh.read(8192)
        if is_binary(sample):
            return ToolResult(f"{p}: binary file, {size} bytes (not shown)")
        offset = max(1, int(args.get("offset") or 1))
        limit = int(args.get("limit") or 400)
        if size > MAX_READ_BYTES and not args.get("offset"):
            return ToolResult(f"{p} is {size} bytes; read it in pieces with offset and limit.", is_error=True)
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        chunk = lines[offset - 1: offset - 1 + limit]
        out = "\n".join(f"{i}\t{ln}" for i, ln in enumerate(chunk, start=offset))
        if offset - 1 + limit < len(lines):
            out += f"\n... {len(lines) - (offset - 1 + limit)} more lines (total {len(lines)})"
        return ToolResult(clip(out) if out else f"{p}: empty file")


class WriteFile(Tool):
    name = "write_file"
    description = "Create or overwrite a file with the given content. Parent folders are created."
    parameters = {"type": "object",
                  "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                  "required": ["path", "content"]}
    kind = "write"

    def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        p = ctx.resolve(str(args.get("path", "")))
        content = args.get("content")
        if content is None:
            return ToolResult("missing content", is_error=True)
        content = str(content)
        existed = p.exists()
        if existed:
            ctx.backup(p)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
        except OSError as exc:
            return ToolResult(f"cannot write {p}: {exc}", is_error=True)
        verb = "overwrote" if existed else "wrote"
        return ToolResult(f"{verb} {p} ({len(content.encode('utf-8'))} bytes, {content.count(chr(10)) + 1} lines)")


class EditFile(Tool):
    name = "edit_file"
    description = ("Replace an exact text passage in a file. old_text must occur exactly once "
                   "unless replace_all is true. Include enough surrounding lines to be unique.")
    parameters = {"type": "object",
                  "properties": {"path": {"type": "string"},
                                 "old_text": {"type": "string"},
                                 "new_text": {"type": "string"},
                                 "replace_all": {"type": "boolean"}},
                  "required": ["path", "old_text", "new_text"]}
    kind = "write"

    def summary(self, args: dict) -> str:
        return f"edit_file({args.get('path', '')})"

    def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        p = ctx.resolve(str(args.get("path", "")))
        if not p.is_file():
            return ToolResult(f"not found: {p}", is_error=True)
        old = str(args.get("old_text", ""))
        new = str(args.get("new_text", ""))
        if not old:
            return ToolResult("old_text is empty", is_error=True)
        text = p.read_text(encoding="utf-8", errors="replace")
        n = text.count(old)
        if n == 0:
            return ToolResult("old_text not found in file (it must match exactly, including whitespace)", is_error=True)
        if n > 1 and not args.get("replace_all"):
            return ToolResult(f"old_text occurs {n} times; add context to make it unique or set replace_all", is_error=True)
        ctx.backup(p)
        updated = text.replace(old, new) if args.get("replace_all") else text.replace(old, new, 1)
        try:
            p.write_text(updated, encoding="utf-8")
        except OSError as exc:
            return ToolResult(f"cannot write {p}: {exc}", is_error=True)
        at = updated.find(new) if new else text.find(old)
        line = updated.count("\n", 0, max(at, 0)) + 1
        return ToolResult(f"edited {p}: {n if args.get('replace_all') else 1} replacement(s), first at line {line}")


class ListFiles(Tool):
    name = "list_files"
    description = "List files under a folder, optionally filtered by a glob such as **/*.py."
    parameters = {"type": "object",
                  "properties": {"path": {"type": "string", "description": "folder, default project root"},
                                 "pattern": {"type": "string", "description": "glob, default **/*"},
                                 "max_results": {"type": "integer"}},
                  "required": []}
    kind = "read"

    def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        root = ctx.resolve(str(args.get("path") or "."))
        if not root.is_dir():
            return ToolResult(f"not a directory: {root}", is_error=True)
        pattern = str(args.get("pattern") or "**/*")
        limit = int(args.get("max_results") or 200)
        out: list[str] = []
        total = 0
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
            rel_dir = os.path.relpath(dirpath, root)
            for fn in sorted(filenames):
                rel = fn if rel_dir == "." else os.path.join(rel_dir, fn)
                # "**/" also matches zero folders: **/*.py must find setup.py in the root, as in git and Claude Code
                tail = pattern[3:] if pattern.startswith("**/") else None
                if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(fn, pattern) or (tail and fnmatch.fnmatch(rel, tail)):
                    total += 1
                    if len(out) < limit:
                        out.append(rel)
            if "**" not in pattern and "/" not in pattern:
                pass
        if not out:
            return ToolResult(f"no files match {pattern} under {root}")
        more = f"\n... {total - len(out)} more" if total > len(out) else ""
        return ToolResult(f"{root}:\n" + "\n".join(out) + more)
