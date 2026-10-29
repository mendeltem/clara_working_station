from __future__ import annotations

import fnmatch
import os
import re

from .base import Tool, ToolContext, ToolResult, clip
from .files import SKIP_DIRS, is_binary


class Search(Tool):
    name = "search"
    description = "Search file contents with a regular expression. Returns file:line: text."
    parameters = {"type": "object",
                  "properties": {"pattern": {"type": "string", "description": "regular expression"},
                                 "path": {"type": "string", "description": "folder or file, default project root"},
                                 "glob": {"type": "string", "description": "only files matching, e.g. *.py"},
                                 "max_results": {"type": "integer"},
                                 "ignore_case": {"type": "boolean"}},
                  "required": ["pattern"]}
    kind = "read"

    def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        try:
            rx = re.compile(str(args.get("pattern", "")), re.IGNORECASE if args.get("ignore_case") else 0)
        except re.error as exc:
            return ToolResult(f"bad regular expression: {exc}", is_error=True)
        root = ctx.resolve(str(args.get("path") or "."))
        glob = str(args.get("glob") or "*")
        limit = int(args.get("max_results") or 50)
        hits: list[str] = []
        files_scanned = 0
        if root.is_file():
            candidates = [str(root)]
        else:
            candidates = []
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
                for fn in sorted(filenames):
                    if fnmatch.fnmatch(fn, glob):
                        candidates.append(os.path.join(dirpath, fn))
        for fp in candidates:
            try:
                with open(fp, "rb") as fh:
                    sample = fh.read(8192)
                if is_binary(sample) or os.path.getsize(fp) > 5_000_000:
                    continue
                files_scanned += 1
                with open(fp, "r", encoding="utf-8", errors="replace") as fh:
                    for i, line in enumerate(fh, start=1):
                        if rx.search(line):
                            rel = os.path.relpath(fp, ctx.project_root)
                            hits.append(f"{rel}:{i}: {line.rstrip()[:300]}")
                            if len(hits) >= limit:
                                break
            except OSError:
                continue
            if len(hits) >= limit:
                break
        if not hits:
            return ToolResult(f"no matches in {files_scanned} files")
        suffix = f"\n(stopped at {limit} results)" if len(hits) >= limit else ""
        return ToolResult(clip("\n".join(hits)) + suffix)
