"""Rules first, then the user. The model never decides its own permissions.

Modes
  default       read tools run, writes and shell commands ask
  accept-edits  writes inside the project run, shell commands ask
  yolo          everything runs (use with the Landlock sandbox)

Rules (~/.clara/projects/<project>.json, like Claude Code's allow/deny lists,
but kept outside the project so a sandboxed model cannot edit them):
  "bash(git status*)"      allow a command pattern
  "write_file(src/**)"     allow writes under src/
  "read_file(/media/**)"   in the deny list: never read patient data folders
A deny rule always wins. A rule pattern is matched with fnmatch against the
tool's first argument (command or path).
"""
from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .config import ProjectSettings
from .tools.base import Tool
from .tools.shell import looks_networked

MODES = ("default", "accept-edits", "yolo")
RULE_RE = re.compile(r"^\s*([a-zA-Z_][\w]*)\s*(?:\((.*)\))?\s*$", re.DOTALL)


@dataclass
class PermissionRequest:
    tool: str
    kind: str
    summary: str
    args: dict
    target: str = ""
    networked: bool = False
    outside_project: bool = False
    protected: bool = False
    id: str = ""

    def warnings(self) -> list[str]:
        w = []
        if self.networked:
            w.append("this command normally uses the network")
        if self.outside_project:
            w.append("target is outside the project folder")
        if self.protected:
            w.append("this changes Clara's own instructions or settings")
        return w


@dataclass
class Decision:
    allow: bool
    reason: str
    asked: bool = False


Asker = Callable[[PermissionRequest], str]   # returns allow | deny | allow_session | allow_persist


def parse_rule(rule: str) -> tuple[str, str] | None:
    m = RULE_RE.match(rule)
    if not m:
        return None
    return m.group(1), (m.group(2) if m.group(2) is not None else "*")


SHELL_COMPOUND = re.compile(r"[;&|`\n<>]|\$\(")
PROTECTED_NAMES = ("CLARA.md", "AGENTS.md")


def rule_matches(rule: str, tool: str, target: str) -> bool:
    parsed = parse_rule(rule)
    if not parsed:
        return False
    name, pattern = parsed
    if name != tool:
        return False
    if pattern in ("", "*"):
        return tool != "bash" or not SHELL_COMPOUND.search(target)
    if tool == "bash" and SHELL_COMPOUND.search(target):
        # A saved rule such as bash(pytest -q*) must not cover "pytest -q; rm -rf ~".
        # Compound commands, pipes, redirections and substitutions always ask.
        return False
    return fnmatch.fnmatch(target, pattern) or fnmatch.fnmatch(target, pattern + "*")


def deny_rule_prefix(rule: str) -> str | None:
    """The literal path before the first wildcard of a path-shaped deny rule, e.g. /media for read_file(/media/**)."""
    parsed = parse_rule(rule)
    if not parsed:
        return None
    _, pattern = parsed
    literal = re.split(r"[*?\[]", pattern)[0].rstrip("/")
    return literal if literal.startswith(("/", "~")) and len(literal) > 1 else None


def resolve_target_path(target: str, project_root: Path | None) -> Path | None:
    if not target or "\n" in target:
        return None
    p = Path(target).expanduser()
    if not p.is_absolute():
        p = (project_root or Path.cwd()) / p
    try:
        return p.resolve()
    except OSError:
        return None


def target_of(tool: Tool, args: dict) -> str:
    keys = list(tool.parameters.get("properties", {}).keys())
    if tool.name == "bash":
        return str(args.get("command", ""))
    if "path" in args:
        return str(args.get("path", ""))
    if keys:
        v = args.get(keys[0])
        return str(v) if v is not None else ""
    return ""


@dataclass
class Permissions:
    project: ProjectSettings
    mode: str = "default"
    asker: Asker | None = None
    session_allow: list[str] = field(default_factory=list)
    project_root: Path | None = None
    home: Path | None = None
    log: list[dict] = field(default_factory=list)
    approver: Callable | None = None

    def set_mode(self, mode: str) -> None:
        if mode not in MODES:
            raise ValueError(f"unknown mode {mode}; use one of {MODES}")
        self.mode = mode

    def build_request(self, tool: Tool, args: dict) -> PermissionRequest:
        target = target_of(tool, args)
        req = PermissionRequest(tool=tool.name, kind=tool.kind, summary=tool.summary(args), args=args, target=target)
        if tool.name == "bash":
            req.networked = looks_networked(target)
        if tool.kind == "write" and tool.name != "remember" and self.project_root is not None and target:
            p = resolve_target_path(target, self.project_root)
            if p is None:
                req.outside_project = True
            else:
                inside = p == self.project_root or self.project_root in p.parents
                req.outside_project = not inside
                # Clara's own files: instructions the model would read back as authority,
                # or settings under ~/.clara. Never auto-allowed, whatever the mode below yolo.
                in_home = self.home is not None and (p == self.home or self.home in p.parents)
                if in_home or p.name in PROTECTED_NAMES or ".clara" in p.parts:
                    req.protected = True
        if tool.name == "remember":
            req.protected = True   # becomes part of the next system prompt
        return req

    def _denied_by_rule(self, req: PermissionRequest) -> str | None:
        """Deny rules match the literal target, its resolved path, and (for shell
        commands and searches) any mention of the protected folder."""
        resolved = resolve_target_path(req.target, self.project_root) if req.tool != "bash" else None
        for rule in self.project.deny:
            if rule_matches(rule, req.tool, req.target):
                return rule
            if resolved is not None and rule_matches(rule, req.tool, str(resolved)):
                return rule
            prefix = deny_rule_prefix(rule)
            if prefix is None:
                continue
            prefix = str(Path(prefix).expanduser())
            if resolved is not None and (str(resolved) == prefix or str(resolved).startswith(prefix + "/")):
                return rule
            if req.tool in ("bash", "search", "list_files", "delegate") and prefix in req.target:
                return rule
        return None

    def decide(self, tool: Tool, args: dict) -> Decision:
        req = self.build_request(tool, args)
        denied = self._denied_by_rule(req)
        if denied:
            return self._log(Decision(False, f"denied by rule {denied}"), req)
        if tool.kind in ("read", "meta"):
            return self._log(Decision(True, "read-only tool"), req)
        if not req.protected:
            for rule in list(self.project.allow) + list(self.session_allow):
                if rule_matches(rule, req.tool, req.target):
                    return self._log(Decision(True, f"allowed by rule {rule}"), req)
        if self.mode == "yolo":
            return self._log(Decision(True, "yolo mode"), req)
        if self.mode == "accept-edits" and tool.kind == "write" and not req.outside_project and not req.protected:
            return self._log(Decision(True, "accept-edits mode"), req)
        if self.approver is not None and not req.protected:
            verdict = self.approver(req, self.project_root)
            if verdict == "APPROVE":
                return self._log(Decision(True, "approved by the local gate"), req)
            if verdict == "DENY":
                return self._log(Decision(False, "denied by the local gate"), req)
        if self.asker is None:
            return self._log(Decision(False, "needs approval and no user is available"), req)
        answer = self.asker(req)
        if answer == "allow_session":
            self.session_allow.append(self._rule_for(req))
            return self._log(Decision(True, "allowed by user for this session", asked=True), req)
        if answer == "allow_persist":
            rule = self._rule_for(req)
            if rule not in self.project.allow:
                self.project.allow.append(rule)
            try:
                self.project.save()
            except OSError as exc:
                return self._log(Decision(True, f"allowed; rule {rule} kept for this session only, not saved: {exc}", asked=True), req)
            return self._log(Decision(True, f"allowed and saved as rule {rule}", asked=True), req)
        if answer == "allow":
            return self._log(Decision(True, "allowed by user", asked=True), req)
        return self._log(Decision(False, "denied by user", asked=True), req)

    @staticmethod
    def _rule_for(req: PermissionRequest) -> str:
        """The rule a 'this session' / 'save as rule' answer creates. For a shell
        command: the first two words plus *, which rule_matches only applies to
        simple commands (no ; && || | redirection or substitution)."""
        if req.tool == "bash":
            first = req.target.strip().split()
            prefix = " ".join(first[:2]) if first else ""
            return f"bash({prefix}*)" if prefix else "bash(*)"
        return f"{req.tool}(*)"

    def _log(self, d: Decision, req: PermissionRequest) -> Decision:
        self.log.append({"tool": req.tool, "target": req.target[:200], "allow": d.allow, "reason": d.reason})
        return d
