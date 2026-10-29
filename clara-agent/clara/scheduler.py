"""Local scheduled tasks, Hermes-style background routines without any network.

Tasks live in ~/.clara/schedules.json. `clara schedule loop` runs due tasks
with `clara -p` in a child process (so each run gets its own sandbox and
session) and appends the answer to ~/.clara/schedule-runs/<id>.log.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

from .config import load_settings

INTERVAL_RE = re.compile(r"^(\d+)\s*([mhd])$")


def parse_interval(text: str) -> int:
    m = INTERVAL_RE.match(text.strip().lower())
    if not m:
        raise ValueError("interval must look like 30m, 2h or 1d")
    n, unit = int(m.group(1)), m.group(2)
    return n * {"m": 60, "h": 3600, "d": 86400}[unit]


def load_tasks(path: Path) -> list[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []


def save_tasks(path: Path, tasks: list[dict]) -> None:
    path.write_text(json.dumps(tasks, indent=2) + "\n", encoding="utf-8")


def run_due(settings, now: float | None = None, runner=None) -> list[dict]:
    now = now or time.time()
    tasks = load_tasks(settings.schedules_file)
    ran = []
    for t in tasks:
        if now >= t.get("next_run", 0):
            t["last_run"] = now
            t["next_run"] = now + t["interval"]
            ran.append(t)
            (runner or _run_task)(settings, t)
    if ran:
        save_tasks(settings.schedules_file, tasks)
    return ran


def _run_task(settings, task: dict) -> None:
    log_dir = settings.home / "schedule-runs"
    log_dir.mkdir(parents=True, exist_ok=True)
    # Unattended runs always get the sandbox, whatever the mode.
    cmd = [sys.executable, "-m", "clara.cli", "-p", task["prompt"], "--mode", task.get("mode", "default"),
           "--no-record", "--sandbox", "on"]
    if task.get("project"):
        cmd += ["-C", task["project"]]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=task.get("timeout", 3600))
    with open(log_dir / f"{task['id']}.log", "a", encoding="utf-8") as fh:
        fh.write(f"=== {time.strftime('%Y-%m-%d %H:%M:%S')} exit {proc.returncode}\n{proc.stdout}\n{proc.stderr}\n")


def schedule_command(args) -> int:
    settings = load_settings()
    path = settings.schedules_file
    tasks = load_tasks(path)
    sub = args.schedule_command
    if sub == "add":
        try:
            interval = parse_interval(args.every)
        except ValueError as exc:
            print(exc, file=sys.stderr)
            return 2
        t = {"id": uuid.uuid4().hex[:8], "prompt": args.prompt, "interval": interval, "mode": args.mode,
             "project": str(Path.cwd()), "created": time.time(), "next_run": time.time() + interval}
        tasks.append(t)
        save_tasks(path, tasks)
        print(f"added {t['id']}: every {args.every}: {args.prompt[:80]}")
        return 0
    if sub == "list" or sub is None:
        if not tasks:
            print("no scheduled tasks")
        for t in tasks:
            nxt = time.strftime("%Y-%m-%d %H:%M", time.localtime(t["next_run"]))
            print(f"{t['id']}  every {t['interval']}s  mode {t['mode']}  next {nxt}  {t['prompt'][:60]}")
        return 0
    if sub == "remove":
        keep = [t for t in tasks if t["id"] != args.id]
        if len(keep) == len(tasks):
            print(f"no task {args.id}")
            return 1
        save_tasks(path, keep)
        print(f"removed {args.id}")
        return 0
    if sub == "run":
        ran = run_due(settings)
        print(f"ran {len(ran)} task(s)")
        return 0
    if sub == "loop":
        print(f"scheduler loop every {args.interval} s; Ctrl+C to stop")
        while True:
            ran = run_due(settings)
            if ran:
                print(f"{time.strftime('%H:%M:%S')} ran {len(ran)} task(s)")
            time.sleep(args.interval)
    return 2
