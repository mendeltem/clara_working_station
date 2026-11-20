from __future__ import annotations

import fnmatch
import os
import stat
import time
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
        # 07.11.2026 (deep test: five chat turns hung 120 s in a search over ~/cle-arbeit, 3 GB of films and sessions):
        # a time budget, and the agents' own homes/media folders are skipped
        deadline = time.time() + float(os.environ.get("CLARA_SEARCH_SECONDS", "15"))
        timed_out = False
        if root.is_file():
            candidates = [str(root)]
        else:
            candidates = []
            for dirpath, dirnames, filenames in os.walk(root):
                if time.time() > deadline:
                    timed_out = True
                    break
                dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".clara-home")
                                     and d not in ("media", "audio_beats", "partial_movie_files", "sessions", "backups"))
                for fn in sorted(filenames):
                    if fnmatch.fnmatch(fn, glob):
                        candidates.append(os.path.join(dirpath, fn))
        for fp in candidates:
            if time.time() > deadline:
                timed_out = True
                break
            try:
                st = os.stat(fp)
                if not stat.S_ISREG(st.st_mode) or st.st_size > 5_000_000:
                    continue                  # 07.11.2026: a FIFO left by a work session blocked open() for ever
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
        note = (f"\n(search stopped after {os.environ.get('CLARA_SEARCH_SECONDS', '15')} s: the folder is too big -- "
                "search a narrower folder, or use work_status / status_logs for sessions)") if timed_out else ""
        if not hits:
            return ToolResult(f"no matches in {files_scanned} files" + note)
        suffix = (f"\n(stopped at {limit} results)" if len(hits) >= limit else "") + note
        return ToolResult(clip("\n".join(hits)) + suffix)
