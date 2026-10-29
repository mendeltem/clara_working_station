"""Terminal front end: streaming output, tool cards and permission prompts.

ANSI only, no curses, no third-party packages, so it runs over SSH and inside
tmux the same way.
"""
from __future__ import annotations

import os
import sys
import threading

from .agent import UI
from .permissions import PermissionRequest
from .toolcalls import ToolCall
from .tools.base import ToolResult

COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None and os.environ.get("TERM") != "dumb"


def c(code: str, text: str) -> str:
    return f"\x1b[{code}m{text}\x1b[0m" if COLOR else text


DIM = "2"
BOLD = "1"
CYAN = "36"
YELLOW = "33"
RED = "31"
GREEN = "32"
MAGENTA = "35"


class TerminalUI(UI):
    def __init__(self, show_reasoning: bool = True, result_lines: int = 6):
        self.show_reasoning = show_reasoning
        self.result_lines = result_lines
        self._text_open = False
        self._reasoning_open = False
        self.lock = threading.Lock()
        self.last_status = ""

    # -------------------------------------------------------------- output
    def _newline_if_open(self) -> None:
        if self._text_open or self._reasoning_open:
            sys.stdout.write("\n")
            self._text_open = False
            self._reasoning_open = False

    def on_text(self, delta: str) -> None:
        with self.lock:
            if self._reasoning_open:
                sys.stdout.write("\n")
                self._reasoning_open = False
            sys.stdout.write(delta)
            sys.stdout.flush()
            self._text_open = True

    def on_reasoning(self, delta: str) -> None:
        if not self.show_reasoning:
            return
        with self.lock:
            if not self._reasoning_open and not self._text_open:
                sys.stdout.write(c(DIM, "· thinking: "))
            sys.stdout.write(c(DIM, delta.replace("\n", "\n  ")))
            sys.stdout.flush()
            self._reasoning_open = True

    def on_tool_start(self, call: ToolCall, summary: str) -> None:
        with self.lock:
            self._newline_if_open()
            sys.stdout.write(c(CYAN, "● ") + c(BOLD, summary) + "\n")
            sys.stdout.flush()

    def on_tool_result(self, call: ToolCall, result: ToolResult, decision: str) -> None:
        with self.lock:
            lines = result.text.splitlines() or [""]
            shown = lines[: self.result_lines]
            color = RED if result.is_error else DIM
            for ln in shown:
                sys.stdout.write("  " + c(color, ln[:200]) + "\n")
            if len(lines) > len(shown):
                sys.stdout.write("  " + c(DIM, f"… {len(lines) - len(shown)} more lines") + "\n")
            secs = result.data.get("seconds")
            tail = f"{decision}" + (f" · {secs}s" if secs is not None and secs >= 1 else "")
            sys.stdout.write("  " + c(DIM, tail) + "\n")
            sys.stdout.flush()

    def on_status(self, text: str) -> None:
        self.last_status = text
        with self.lock:
            self._newline_if_open()
            sys.stdout.write(c(DIM, f"  {text}") + "\n")
            sys.stdout.flush()

    def on_warning(self, text: str) -> None:
        with self.lock:
            self._newline_if_open()
            sys.stdout.write(c(YELLOW, f"! {text}") + "\n")
            sys.stdout.flush()

    def on_turn_end(self, text: str) -> None:
        with self.lock:
            self._newline_if_open()
            sys.stdout.flush()

    def on_todo(self, items: list) -> None:
        marks = {"done": c(GREEN, "✔"), "in_progress": c(YELLOW, "▶"), "pending": c(DIM, "○")}
        with self.lock:
            self._newline_if_open()
            for it in items:
                sys.stdout.write(f"  {marks.get(it.get('status'), '○')} {it.get('text')}\n")
            sys.stdout.flush()

    # -------------------------------------------------------------- input
    def ask_permission(self, req: PermissionRequest) -> str:
        with self.lock:
            self._newline_if_open()
            warn = "".join(c(YELLOW, f"  ⚠ {w}") + "\n" for w in req.warnings())
            sys.stdout.write(c(MAGENTA, "? ") + c(BOLD, req.summary) + "\n" + warn)
            if req.tool in ("write_file",) and "content" in req.args:
                preview = str(req.args["content"]).splitlines()[:12]
                for ln in preview:
                    sys.stdout.write("    " + c(DIM, ln[:160]) + "\n")
            if req.tool == "edit_file":
                for label, key in (("- ", "old_text"), ("+ ", "new_text")):
                    for ln in str(req.args.get(key, "")).splitlines()[:8]:
                        sys.stdout.write("    " + c(RED if label == "- " else GREEN, label + ln[:160]) + "\n")
            sys.stdout.write(c(DIM, "  [y] once  [s] this session  [p] save as rule  [n] deny › "))
            sys.stdout.flush()
        try:
            answer = input().strip().lower()
        except (EOFError, KeyboardInterrupt):
            sys.stdout.write("\n")
            return "deny"
        return {"y": "allow", "yes": "allow", "s": "allow_session", "p": "allow_persist"}.get(answer, "deny")

    def ask_user(self, question: str) -> str | None:
        with self.lock:
            self._newline_if_open()
            sys.stdout.write(c(MAGENTA, "? ") + question + "\n" + c(DIM, "  › "))
            sys.stdout.flush()
        try:
            return input().strip()
        except (EOFError, KeyboardInterrupt):
            sys.stdout.write("\n")
            return None


class HeadlessUI(UI):
    """For `clara -p`: final answer on stdout, everything else on stderr."""
    def __init__(self, show_reasoning: bool = False, verbose: bool = True):
        self.show_reasoning = show_reasoning
        self.verbose = verbose
        self.text_parts: list[str] = []

    def on_text(self, delta: str) -> None:
        self.text_parts.append(delta)

    def on_tool_start(self, call: ToolCall, summary: str) -> None:
        if self.verbose:
            sys.stderr.write(f"● {summary}\n")

    def on_tool_result(self, call: ToolCall, result: ToolResult, decision: str) -> None:
        if self.verbose:
            first = (result.text.splitlines() or [""])[0]
            sys.stderr.write(f"  {decision}: {first[:160]}\n")

    def on_warning(self, text: str) -> None:
        sys.stderr.write(f"! {text}\n")

    def on_status(self, text: str) -> None:
        if self.verbose:
            sys.stderr.write(f"  {text}\n")

    def on_turn_end(self, text: str) -> None:
        pass
