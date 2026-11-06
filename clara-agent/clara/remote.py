"""`clara connect`: the terminal as a window onto the SAME Clara the phone talks to (06.11.2026, user: "clara in the
terminal should be the same Clara as on Telegram, same conversation, same memory").

It does not run an agent itself: it attaches to the session of the main conversation in `clara serve` (the one the
Telegram bridge uses), shows its events with the normal terminal look, and sends what you type. Messages written on
the phone appear here too (marked 📱). Several terminals may be connected at once.
  - a line typed while Clara still answers is queued and sent when she is done
  - an approval or a question is answered with the next line (y / s / n, or the answer text)
  - /work   overview of the work sessions      /stop   interrupt Clara      /exit   leave (Clara keeps running)
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import terminal
from .terminal import DIM, TerminalUI, c
from .tools.base import ToolResult

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class Remote:
    def __init__(self, base: str, home: Path):
        self.base = base.rstrip("/")
        self.home = home
        self.token = (home / "serve-token").read_text().strip()
        self.ui = TerminalUI(show_reasoning=False)
        self.sid = ""
        self.busy = False
        self.pending: dict | None = None          # permission or question waiting for the next line
        self.queue: list[str] = []
        self.mine: set[str] = set()                # texts sent from this terminal (not echoed as 📱)
        self.lock = threading.Lock()
        self.stop = False

    # ------------------------------------------------------------------ http
    def api(self, method: str, path: str, body: dict | None = None, timeout: float = 30):
        req = urllib.request.Request(self.base + path, json.dumps(body or {}).encode() if method == "POST" else None,
                                     {"Content-Type": "application/json", "Authorization": f"Bearer {self.token}"},
                                     method=method)
        with OPENER.open(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")

    def main_session(self) -> str:
        """The Telegram chat's session = the main conversation; otherwise the newest session; otherwise a new one."""
        state = self.home / "telegram.json"
        try:
            st = json.loads(state.read_text())
            for k, v in st.items():
                if not k.startswith("_") and isinstance(v, dict) and v.get("sid"):
                    self.api("GET", f"/api/sessions/{v['sid']}")
                    return v["sid"]
        except (OSError, ValueError, urllib.error.URLError):
            pass
        try:
            items = self.api("GET", "/api/sessions").get("sessions") or []
            if items:
                return items[0]["id"]
        except (urllib.error.URLError, KeyError, IndexError):
            pass
        return self.api("POST", "/api/sessions", {})["id"]

    # ------------------------------------------------------------------ events
    def follow(self) -> None:
        seq = int(self.api("GET", f"/api/sessions/{self.sid}").get("seq") or 0)
        while not self.stop:
            try:
                ticket = self.api("POST", f"/api/sessions/{self.sid}/ticket")["ticket"]
                with OPENER.open(f"{self.base}/api/sessions/{self.sid}/events?ticket={ticket}&since={seq}", timeout=3600) as r:
                    for raw in r:
                        line = raw.decode("utf-8", "replace").strip()
                        if not line.startswith("data:"):
                            continue
                        ev = json.loads(line[5:])
                        seq = max(seq, int(ev.get("seq") or 0))
                        self.on_event(ev)
            except Exception:  # noqa: BLE001 -- reconnect quietly (serve restart, network)
                if not self.stop:
                    time.sleep(2)

    def on_event(self, ev: dict) -> None:
        k, ui = ev.get("kind"), self.ui
        if k == "user":
            text = ev.get("text", "")
            if text in self.mine:
                self.mine.discard(text)
            else:
                print(c(DIM, "\n📱 you (phone): ") + text)
            self.busy = True
            ui.begin_turn()
        elif k == "text":
            ui.on_text(ev.get("delta", ""))
        elif k == "tool_start":
            ui.on_tool_start(None, ev.get("summary") or ev.get("name", ""))
        elif k == "tool_result":
            res = ToolResult(ev.get("text", ""), is_error=bool(ev.get("error")))
            res.data["seconds"] = ev.get("seconds")
            ui.on_tool_result(None, res, ev.get("decision", "ok"))
        elif k == "usage":
            ui.on_usage(ev)
        elif k == "turn_stats":
            ui.on_turn_stats(ev)
        elif k == "warning":
            ui.on_warning(ev.get("text", ""))
        elif k == "permission":
            self.pending = {"kind": "permissions", "id": ev["id"]}
            ui._clear_spin()
            print("\n" + c(terminal.MAGENTA, "? ") + c(terminal.BOLD, f"{ev.get('tool')}: {ev.get('target') or ev.get('summary')}"))
            print(c(DIM, "  answer with the next line: y once · s this session · n deny"))
        elif k == "question":
            self.pending = {"kind": "answers", "id": ev["id"]}
            ui._clear_spin()
            print("\n" + c(terminal.MAGENTA, "? ") + ev.get("question", "") + c(DIM, "  (answer with the next line)"))
        elif k == "turn_end":
            ui.on_turn_end(ev.get("text", ""))
            ui.end_turn()
            self.busy = False
            self.flush_queue()
            print(c(terminal.ACCENT, "❯ "), end="", flush=True)

    def flush_queue(self) -> None:
        with self.lock:
            if self.queue and not self.busy:
                text = self.queue.pop(0)
                self.send(text)

    def send(self, text: str) -> None:
        self.mine.add(text)
        try:
            self.api("POST", f"/api/sessions/{self.sid}/messages", {"text": text})
            self.busy = True
        except urllib.error.HTTPError as e:
            if e.code == 409:
                self.queue.append(text)
                print(c(DIM, "  Clara is still answering; your message goes out as soon as she is done."))
            else:
                print(c(terminal.YELLOW, f"! could not send: HTTP {e.code}"))

    # ------------------------------------------------------------------ loop
    def run(self) -> int:
        self.sid = self.main_session()
        terminal.dark_background(True)
        w = min(terminal.width(), 100)
        print(c(terminal.ACCENT, "╭" + "─" * (w - 2) + "╮"))
        for t in ("✻ Clara · connected to the main conversation (the same as on the phone)",
                  "  /work work sessions · /stop interrupt · /exit leave (Clara keeps running)"):
            print(c(terminal.ACCENT, "│ ") + t[: w - 4].ljust(w - 4) + c(terminal.ACCENT, " │"))
        print(c(terminal.ACCENT, "╰" + "─" * (w - 2) + "╯"))
        try:
            from .cli import print_work_overview
            print_work_overview()
        except Exception:  # noqa: BLE001
            pass
        threading.Thread(target=self.follow, daemon=True).start()
        try:
            while True:
                try:
                    line = input("\n" + c(terminal.ACCENT, "❯ ") if terminal.COLOR else "\n❯ ").strip()
                except KeyboardInterrupt:
                    print(c(DIM, "\n(/exit leaves; Clara keeps running)"))
                    continue
                if not line:
                    continue
                if self.pending:
                    p, self.pending = self.pending, None
                    ans = line
                    if p["kind"] == "permissions":
                        ans = {"y": "allow", "yes": "allow", "j": "allow", "s": "allow_session"}.get(line.lower(), "deny")
                    try:
                        self.api("POST", f"/api/{p['kind']}/{p['id']}", {"answer": ans})
                    except urllib.error.URLError as e:
                        print(c(terminal.YELLOW, f"! answer not delivered: {e}"))
                    continue
                if line in ("/exit", "/quit", "/q"):
                    break
                if line == "/stop":
                    self.api("POST", f"/api/sessions/{self.sid}/interrupt")
                    continue
                if line == "/work":
                    from .cli import print_work_overview
                    print_work_overview(title=False)
                    continue
                if line in ("/help", "/?"):
                    print(c(DIM, __doc__.split("\n\n", 1)[1]))
                    continue
                with self.lock:
                    if self.busy:
                        self.queue.append(line)
                        print(c(DIM, "  queued: Clara is still answering; it goes out as soon as she is done."))
                    else:
                        self.send(line)
        except EOFError:
            pass
        finally:
            self.stop = True
            terminal.dark_background(False)
        return 0


def connect_command(args) -> int:
    home = Path(os.environ.get("CLARA_SERVE_HOME", os.path.expanduser("~/.clara")))
    base = os.environ.get("CLARA_SERVE", "http://127.0.0.1:8766")
    try:
        return Remote(base, home).run()
    except (OSError, urllib.error.URLError) as e:
        print(f"clara connect: clara serve not reachable at {base} ({e}). Start it: systemctl --user start clara-serve",
              file=sys.stderr)
        return 3
