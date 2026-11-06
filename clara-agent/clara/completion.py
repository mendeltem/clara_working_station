"""Suggestions while typing, standard library only (GNU readline).

06.11.2026 (user: "autofill suggestions should come"): typing "/" at the start of the line lists the slash
commands with a short description; Tab completes commands, their arguments (/mode, /resume)
and file paths. A "/" inside a path lists nothing, so typing ~/data/work stays quiet.
"""
from __future__ import annotations

import glob
import os
import re
import sys

ANSI = re.compile(r"\x1b\[[0-9;]*m|[\x01\x02]")


def commands_from_help(help_text: str) -> dict[str, str]:
    """'/name  description' pairs from the HELP text (two per line are allowed)."""
    out: dict[str, str] = {}
    for line in help_text.splitlines():
        for m in re.finditer(r"^\s+(/[a-z]+)(?: \[?[a-zA-Z|]+\]?)?\s{2,}(.+?)\s*$", line):
            out[m.group(1)] = m.group(2).strip()
    out.setdefault("/exit", "leave")
    return out


class Completer:
    def __init__(self, commands: dict[str, str], prompt: str, dim: str = "2", accent: str = "36",
                 sessions=lambda: [], modes=("default", "accept-edits", "yolo")):
        self.commands = commands
        self.prompt = prompt
        self.dim, self.accent = dim, accent
        self.sessions = sessions
        self.modes = modes
        self.matches: list[str] = []

    # readline calls this with state 0, 1, 2 ... until it returns None
    def complete(self, text: str, state: int):
        if state == 0:
            self.matches = self._matches(text)
        return self.matches[state] if state < len(self.matches) else None

    def _matches(self, text: str) -> list[str]:
        import readline
        line = readline.get_line_buffer()
        begin = readline.get_begidx()
        words = line[:begin].split()
        if begin == 0 and text.startswith("/"):
            if "/" in text[1:]:
                return []                                   # "/home/..." typed as a message: a path, not a command
            return sorted(c for c in self.commands if c.startswith(text))
        if words and words[0] == "/mode" and len(words) == 1:
            return [m for m in self.modes if m.startswith(text)]
        if words and words[0] == "/resume" and len(words) == 1:
            return [s for s in ["last", *self.sessions()] if s.startswith(text)]
        if not text or text.endswith("/"):
            return []                                       # the "/" key lists nothing inside a path
        found = glob.glob(os.path.expanduser(text) + "*")[:200]
        home = os.path.expanduser("~")
        res = []
        for f in sorted(found):
            shown = f.replace(home, "~", 1) if text.startswith("~") else f
            res.append(shown + ("/" if os.path.isdir(f) else ""))
        return res

    def display(self, substitution: str, matches: list[str], longest: int) -> None:
        """Show commands with their description, then redraw the prompt and the typed text."""
        import readline
        out = ["\n"]
        w = max((len(m) for m in matches), default=0) + 3
        for m in matches[:40]:
            desc = self.commands.get(m, "")
            out.append(f"  \x1b[{self.accent}m{m.ljust(w)}\x1b[0m\x1b[{self.dim}m{desc}\x1b[0m\n")
        if len(matches) > 40:
            out.append(f"  \x1b[{self.dim}m… {len(matches) - 40} more\x1b[0m\n")
        sys.stdout.write("".join(out))
        sys.stdout.write(ANSI.sub("", self.prompt) if os.environ.get("NO_COLOR") else self.prompt.replace("\x01", "").replace("\x02", ""))
        sys.stdout.write(readline.get_line_buffer())
        sys.stdout.flush()


def install(completer: Completer, auto_slash: bool = True) -> None:
    import readline
    readline.set_completer(completer.complete)
    readline.set_completer_delims(" \t\n")
    readline.set_completion_display_matches_hook(completer.display)
    readline.parse_and_bind("tab: complete")
    readline.parse_and_bind("set show-all-if-ambiguous on")
    readline.parse_and_bind("set completion-ignore-case on")
    readline.parse_and_bind("set bell-style none")
    if auto_slash and os.environ.get("CLARA_SLASH_MENU", "on") != "off":
        # "/" types itself and then lists the possible completions (M-?): at the line start that is the command menu
        readline.parse_and_bind(r'"/": "\C-v/\e?"')
