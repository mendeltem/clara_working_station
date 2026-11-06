"""Work sessions: the chat Clara hands heavy jobs to independent sessions and stays free to talk.

06.11.2026 (user: "Clara on Telegram or the terminal should answer fast, and another, independent session should do
the heavy work"). The tools only write and read files; a small service outside the sandbox (clara-arbeit) starts one
agent process per job folder, so this process never starts anything itself.

  <base>/<name>/AUFTRAG.txt   the job text          <base>/<name>/START   request: start it (content = place)
  <base>/<name>/status.json   written by the service: state, place, started, finished, rc
  <base>/<name>/answer.txt    the work session's final answer     <base>/<name>/STOP   request: stop it

Bases (config [work] or the defaults): Clé jobs under ~/cle-arbeit/arbeit, local jobs (patient data) under
~/data/work/arbeit, so the Weiche keeps everything about them on the local GPU.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from .base import Tool, ToolContext, ToolResult

BASES = {"cle": os.environ.get("CLARA_WORK_CLE", "~/cle-arbeit/arbeit"),
         "lokal": os.environ.get("CLARA_WORK_LOKAL", "~/data/work/arbeit")}


def _base(place: str) -> Path:
    return Path(os.path.expanduser(BASES["lokal" if place == "lokal" else "cle"]))


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s[:48] or time.strftime("job-%m%d-%H%M")


def _find(name: str) -> Path | None:
    slug = _slug(name)
    for place in ("cle", "lokal"):
        p = _base(place) / slug
        if p.is_dir():
            return p
    return None


def _status(d: Path) -> dict:
    try:
        st = json.loads((d / "status.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        st = {"state": "waiting to start" if (d / "START").exists() else "unknown"}
    st["name"] = d.name
    return st


class StartWork(Tool):
    name = "start_work"
    description = ("Give a long job to a background work session; you stay free to talk. place lokal if patient data. "
                   "The task must be self-contained.")
    parameters = {"type": "object",
                  "properties": {"name": {"type": "string"}, "task": {"type": "string"},
                                 "place": {"type": "string", "enum": ["cle", "lokal"]}},
                  "required": ["name", "task"]}
    kind = "write"

    def summary(self, args: dict) -> str:
        return f"start_work({args.get('name', '')}, {args.get('place') or 'cle'})"

    def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        place = "lokal" if args.get("place") == "lokal" else "cle"
        task = str(args.get("task") or "").strip()
        if len(task) < 20:
            return ToolResult("the task is too short; write it so that someone without this conversation can do it", is_error=True)
        d = _base(place) / _slug(str(args.get("name") or ""))
        if d.exists() and _status(d).get("state") in ("running", "waiting to start"):
            return ToolResult(f"a work session '{d.name}' is already running; ask work_status or pick another name", is_error=True)
        try:
            d.mkdir(parents=True, exist_ok=True)
            (d / "AUFTRAG.txt").write_text(task + "\n", encoding="utf-8")
            for f in ("status.json", "STOP", "answer.txt"):
                (d / f).unlink(missing_ok=True)
            (d / "START").write_text(place + "\n", encoding="utf-8")
        except OSError as exc:
            return ToolResult(f"could not create the work session: {exc}", is_error=True)
        return ToolResult(f"work session '{d.name}' requested on {'the local GPU' if place == 'lokal' else 'Clé'}; "
                          "it starts within a few seconds. Ask work_status for progress.")


class WorkStatus(Tool):
    name = "work_status"
    description = "State of a work session (by name) or a list of all. Read only."
    parameters = {"type": "object", "properties": {"name": {"type": "string"}}}
    kind = "read"       # 06.11.2026: reading the state needs no approval (it made every status question slow)

    def summary(self, args: dict) -> str:
        return f"work_status({args.get('name') or 'all'})"

    def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        name = str(args.get("name") or "").strip()
        if not name:
            rows = []
            for place in ("cle", "lokal"):
                b = _base(place)
                if b.is_dir():
                    for d in sorted(b.iterdir()):
                        if d.is_dir() and (d / "AUFTRAG.txt").exists():
                            st = _status(d)
                            rows.append(f"{d.name}: {st.get('state')} ({place})")
            return ToolResult("\n".join(rows) or "no work sessions yet")
        d = _find(name)
        if d is None:
            return ToolResult(f"no work session called '{name}'", is_error=True)
        st = _status(d)
        lines = [f"{d.name}: {st.get('state')}"]
        if st.get("started"):
            end = st.get("finished") or time.time()
            lines.append(f"running time {int(end - st['started']) // 60} min")
        if st.get("rc") is not None:
            lines.append(f"exit code {st['rc']}")
        trace = d / "trace.txt"
        if st.get("state") == "running" and trace.is_file():
            tail = [ln for ln in trace.read_text(encoding="utf-8", errors="replace").splitlines() if ln.startswith("●")][-3:]
            if tail:
                lines.append("last steps: " + " | ".join(t[2:120] for t in tail))
        ans = d / "answer.txt"
        if ans.is_file() and ans.stat().st_size:
            lines.append("answer (end): " + ans.read_text(encoding="utf-8", errors="replace")[-1500:])
        return ToolResult("\n".join(lines))


class StopWork(Tool):
    name = "stop_work"
    description = "Stop a work session (only if the researcher asks)."
    parameters = {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}
    kind = "write"

    def summary(self, args: dict) -> str:
        return f"stop_work({args.get('name', '')})"

    def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        d = _find(str(args.get("name") or ""))
        if d is None:
            return ToolResult("no such work session", is_error=True)
        (d / "STOP").write_text("stop\n", encoding="utf-8")
        return ToolResult(f"stop requested for '{d.name}'")


def overview_line(limit: int = 8) -> str:
    """One compact line for the chat Clara's every turn (06.11.2026, user: she must know what the other agents do).
    Only names, states, running time and place -- no answers, no paths."""
    rows = []
    for place in ("cle", "lokal"):
        base = _base(place)
        if not base.is_dir():
            continue
        for d in base.iterdir():
            if d.is_dir() and (d / "AUFTRAG.txt").exists():
                st = _status(d)
                start = st.get("started") or (d / "AUFTRAG.txt").stat().st_mtime
                if st.get("state") not in ("running", "waiting to start") and time.time() - start > 86400:
                    continue                            # finished more than a day ago: not news any more
                mins = int((st.get("finished") or time.time()) - start) // 60
                rows.append((start, f"{d.name}: {st.get('state')} ({'Clé' if place == 'cle' else 'local'}, {mins} min)"))
    rows.sort(reverse=True)
    return "; ".join(r for _, r in rows[:limit])
