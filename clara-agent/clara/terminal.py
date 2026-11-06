"""Terminal front end: streaming output, tool cards and permission prompts.

ANSI only, no curses, no third-party packages, so it runs over SSH and inside
tmux the same way.
"""
from __future__ import annotations

import os
import sys
import threading

import time

from .agent import UI, kfmt, route_text, turn_summary
from .permissions import PermissionRequest
from .toolcalls import ToolCall
from .tools.base import ToolResult

COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None and os.environ.get("TERM") != "dumb"


def c(code: str, text: str) -> str:
    return f"\x1b[{code}m{text}\x1b[0m" if COLOR else text


THEME = os.environ.get("CLARA_THEME", "dark")
if THEME == "plain":
    DIM, BOLD, CYAN, YELLOW, RED, GREEN, MAGENTA = "2", "1", "36", "33", "31", "32", "35"
    ACCENT, TEXT = CYAN, "0"
    DOT, HOOK = "● ", "  "
else:
    # dark theme (06.11.2026, user: "dark"): truecolor, warm accent, grey secondary text
    DIM = "38;2;128;128;128"
    BOLD = "1;38;2;235;235;235"
    TEXT = "38;2;225;225;225"
    ACCENT = "38;2;215;119;87"
    CYAN = ACCENT
    YELLOW = "38;2;229;192;123"
    RED = "38;2;224;108;117"
    GREEN = "38;2;152;195;121"
    MAGENTA = "38;2;198;160;246"
    DOT, HOOK = "⏺ ", "  ⎿  "


def width() -> int:
    try:
        return max(40, min(os.get_terminal_size().columns, 160))
    except OSError:
        return 100


def dark_background(on: bool) -> None:
    """Paint the terminal background dark (OSC 11) while Clara runs, restore it on exit (OSC 111).
    Off with CLARA_BG=off or CLARA_THEME=plain; terminals that do not know OSC 11 ignore it."""
    if not COLOR or THEME == "plain" or os.environ.get("CLARA_BG", "on") == "off":
        return
    sys.stdout.write("\x1b]11;#1a1a1a\x07\x1b]10;#e1e1e1\x07" if on else "\x1b]111\x07\x1b]110\x07")
    sys.stdout.flush()


def rule() -> str:
    return c(DIM, "─" * width())


class TerminalUI(UI):
    def __init__(self, show_reasoning: bool = True, result_lines: int = 6):
        self.show_reasoning = show_reasoning
        self.result_lines = result_lines
        self._text_open = False
        self._reasoning_open = False
        self.lock = threading.Lock()
        self.last_status = ""
        # live "Working…" line (06.11.2026, user: plain text and token use instead of cryptic status lines)
        self._busy = False
        self._paused = False
        self._spin_shown = False
        self._last_out = 0.0
        self._t0 = 0.0
        self._stats: dict = {}
        self._tty_saved = None
        self._spinner: threading.Thread | None = None

    # -------------------------------------------------------------- output
    # -------------------------------------------------------------- working line
    def _clear_spin(self) -> None:
        self._last_out = time.time()
        if self._spin_shown:
            sys.stdout.write("\r\x1b[2K")
            self._spin_shown = False

    def _spin_text(self) -> str:
        secs = int(time.time() - self._t0)
        dur = f"{secs // 60}m {secs % 60:02d}s" if secs >= 60 else f"{secs}s"
        st = self._stats
        parts = [dur]
        if st.get("completion"):
            parts.append(f"↓ {kfmt(st['completion'])} tokens")
        if st.get("context"):
            parts.append(f"context {kfmt(st['context'])}")
        where = route_text(st.get("routes", []))
        if where:
            parts.append(where)
        rest = " (" + " · ".join(parts) + " · Ctrl+C to interrupt)"
        rest = rest[: max(10, width() - 12)]          # cut the plain text, never the colour codes
        return c(ACCENT, "✻ ") + c(TEXT, "Working…") + c(DIM, rest)

    def _spin_loop(self) -> None:
        frames = "✻✳✶✢✶✳"
        i = 0
        while self._busy:
            time.sleep(0.25)
            with self.lock:
                if (not self._busy or self._paused or self._text_open or self._reasoning_open
                        or time.time() - self._last_out < 0.6):
                    continue
                i += 1
                line = self._spin_text().replace("✻", frames[i % len(frames)], 1)
                sys.stdout.write("\r\x1b[2K" + line)
                sys.stdout.flush()
                self._spin_shown = True

    def _echo(self, on: bool) -> None:
        """Keys typed while Clara works are not echoed (they used to clutter the screen)."""
        try:
            import termios
            fd = sys.stdin.fileno()
            if not os.isatty(fd):
                return
            if not on:
                self._tty_saved = termios.tcgetattr(fd)
                new = termios.tcgetattr(fd)
                new[3] &= ~termios.ECHO
                termios.tcsetattr(fd, termios.TCSANOW, new)
            elif self._tty_saved is not None:
                termios.tcsetattr(fd, termios.TCSANOW, self._tty_saved)
                self._tty_saved = None
        except Exception:
            pass

    def begin_turn(self) -> None:
        self._t0 = self._last_out = time.time()
        self._stats = {}
        self._busy = True
        self._echo(False)
        if COLOR and THEME != "plain":
            self._spinner = threading.Thread(target=self._spin_loop, daemon=True)
            self._spinner.start()

    def end_turn(self) -> None:
        self._busy = False
        if self._spinner is not None:
            self._spinner.join(timeout=1)
            self._spinner = None
        with self.lock:
            self._clear_spin()
            sys.stdout.flush()
        self._echo(True)

    def on_usage(self, stats: dict) -> None:
        self._stats = stats
        if THEME == "plain" or not COLOR:
            UI.on_usage(self, stats)

    def on_turn_stats(self, stats: dict) -> None:
        with self.lock:
            self._newline_if_open()
            sys.stdout.write(c(DIM, "✻ " + turn_summary(stats)) + "\n")
            sys.stdout.flush()

    def _newline_if_open(self) -> None:
        self._clear_spin()
        if self._text_open or self._reasoning_open:
            sys.stdout.write("\n")
            self._text_open = False
            self._reasoning_open = False

    def on_text(self, delta: str) -> None:
        with self.lock:
            self._clear_spin()
            if self._reasoning_open:
                sys.stdout.write("\n")
                self._reasoning_open = False
            if not self._text_open:
                sys.stdout.write(c(ACCENT if THEME != "plain" else "0", DOT if THEME != "plain" else ""))
            sys.stdout.write(c(TEXT, delta) if THEME != "plain" else delta)
            sys.stdout.flush()
            self._text_open = True

    def on_reasoning(self, delta: str) -> None:
        if not self.show_reasoning:
            return
        with self.lock:
            self._clear_spin()
            if not self._reasoning_open and not self._text_open:
                sys.stdout.write(c(DIM, "✻ thinking… " if THEME != "plain" else "· thinking: "))
            sys.stdout.write(c(DIM + (";3" if THEME != "plain" else ""), delta.replace("\n", "\n  ")))
            sys.stdout.flush()
            self._reasoning_open = True

    def on_tool_start(self, call: ToolCall, summary: str) -> None:
        with self.lock:
            self._newline_if_open()
            sys.stdout.write(c(ACCENT, DOT) + c(BOLD, summary) + "\n")
            sys.stdout.flush()

    def on_tool_result(self, call: ToolCall, result: ToolResult, decision: str) -> None:
        with self.lock:
            lines = result.text.splitlines() or [""]
            shown = lines[: self.result_lines]
            color = RED if result.is_error else DIM
            pad = " " * len(HOOK)
            for i, ln in enumerate(shown):
                sys.stdout.write((c(DIM, HOOK) if i == 0 else pad) + c(color, ln[:200]) + "\n")
            if len(lines) > len(shown):
                sys.stdout.write(pad + c(DIM, f"… {len(lines) - len(shown)} more lines") + "\n")
            secs = result.data.get("seconds")
            word = {"ok": "", "error": "failed", "denied": "denied", "unknown": "unknown tool",
                    "bad-args": "invalid arguments"}.get(decision, decision)
            took = f"took {secs:.0f}s" if secs is not None and secs >= 1 else ""
            tail = " · ".join(x for x in (word, took) if x)
            if tail:
                sys.stdout.write(pad + c(RED if word else DIM, tail) + "\n")
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
    def _ask_permission_inner(self, req: PermissionRequest) -> str:
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

    def _ask_user_inner(self, question: str) -> str | None:
        with self.lock:
            self._newline_if_open()
            sys.stdout.write(c(MAGENTA, "? ") + question + "\n" + c(DIM, "  › "))
            sys.stdout.flush()
        try:
            return input().strip()
        except (EOFError, KeyboardInterrupt):
            sys.stdout.write("\n")
            return None


    def ask_permission(self, req: PermissionRequest) -> str:
        self._paused = True
        self._echo(True)
        try:
            return self._ask_permission_inner(req)
        finally:
            self._paused = False
            if self._busy:
                self._echo(False)
            self._last_out = time.time()

    def ask_user(self, question: str) -> str | None:
        self._paused = True
        self._echo(True)
        try:
            return self._ask_user_inner(question)
        finally:
            self._paused = False
            if self._busy:
                self._echo(False)
            self._last_out = time.time()


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
