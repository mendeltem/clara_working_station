"""Configuration: where files live and what the defaults are.

Global state lives in ~/.clara (override with CLARA_HOME). Project state lives in
<project>/.clara. Nothing is ever written anywhere else unless a tool call that
the user approved writes it.
"""
from __future__ import annotations

import json
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_TOML = """# Clara configuration. Everything here is optional.

[model]
# OpenAI-compatible server on your own machine. llama-server: 8000 (your service),
# Ollama: 11434, LM Studio: 1234, vLLM: 8000. Clara refuses anything that is not
# in [network].allowed_hosts.
base_url = "http://127.0.0.1:8000/v1"
# Empty = take the first model that /v1/models reports.
model = ""
# "native": send tools in the request (llama-server --jinja, Ollama, vLLM).
# "prompt": describe tools in the system prompt and parse <tool_call> text; works
# with every server, used automatically when the server ignores the tools field.
tool_mode = "native"
# Seconds to wait for the first byte. Long on purpose: a sleeping llama-server
# needs ~11 s to reload a 27B model, and a single slot can be busy.
timeout = 600
# Leave sampling to the server unless you measured a gain.
# temperature = 0.6

[network]
# Hosts Clara's own process may ever connect to. Everything else is refused
# before a packet leaves and written to ~/.clara/netaudit.log.
allowed_hosts = ["127.0.0.1", "::1", "localhost"]

[sandbox]
# Landlock (Linux, no root). "auto": on when --yolo is used, off otherwise.
# "on": always. "off": never.
mode = "auto"
# Extra writable folders besides the project folder and Clara's session folders.
extra_write_paths = []
# Extra loopback TCP ports the sandboxed tree may connect to (e.g. a local research gateway).
extra_connect_ports = []
# Unix sockets under the sandbox: "seqpacket" (strict; CUDA works, torch DataLoader workers do not) or "allow"
# (training; leaves the systemd/D-Bus/resolved route open). UDP, ICMP and raw sockets are refused in both.
unix_sockets = "seqpacket"
# Folders the sandboxed process may not read at all (patient data, private keys).
# Enforced by the kernel for every child process, so `cat` and symlinks cannot
# get around it. Takes effect when the sandbox is on.
read_deny = []

[permissions]
# Optional local approval service (Hermes smart-approval format: one <command> block, reply APPROVE / DENY /
# ESCALATE). Asked after the rules and the mode, before the user; ESCALATE falls through to the user.
# approver_url = "http://127.0.0.1:8765/v1/chat/completions"
approver_timeout = 90

[agent]
max_turns = 60
# Warn when the estimated prompt exceeds this share of the server context.
context_warn_fraction = 0.85
show_reasoning = true

[serve]
# Phone app. Bind to loopback unless you run `clara serve --lan`.
host = "127.0.0.1"
port = 8765
# true: one session at a time uses the model (single-slot llama-server). false: sessions run side by side
# (a relay or an API that serves parallel requests).
serialize = true
"""

READ_ONLY_TOOLS = ("read_file", "list_files", "search", "ask_user", "todo")


def clara_home() -> Path:
    return Path(os.environ.get("CLARA_HOME") or Path.home() / ".clara")


@dataclass
class Settings:
    base_url: str = "http://127.0.0.1:8000/v1"
    model: str = ""
    tool_mode: str = "native"
    timeout: float = 600.0
    temperature: float | None = None
    allowed_hosts: list[str] = field(default_factory=lambda: ["127.0.0.1", "::1", "localhost"])
    sandbox_mode: str = "auto"
    extra_write_paths: list[str] = field(default_factory=list)
    extra_connect_ports: list[int] = field(default_factory=list)
    unix_sockets: str = "seqpacket"
    approver_url: str = ""
    approver_timeout: float = 90.0
    read_deny: list[str] = field(default_factory=list)
    max_turns: int = 60
    context_warn_fraction: float = 0.85
    show_reasoning: bool = True
    serve_host: str = "127.0.0.1"
    serve_port: int = 8765
    serve_serialize: bool = True
    home: Path = field(default_factory=clara_home)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def sessions_dir(self) -> Path:
        return self.home / "sessions"

    @property
    def skills_dir(self) -> Path:
        return self.home / "skills"

    @property
    def memory_file(self) -> Path:
        return self.home / "memory.md"

    @property
    def audit_log(self) -> Path:
        return self.home / "netaudit.log"

    @property
    def schedules_file(self) -> Path:
        return self.home / "schedules.json"

    @property
    def backups_dir(self) -> Path:
        return self.home / "backups"

    @property
    def history_file(self) -> Path:
        # readline writes a temporary file next to the history and renames it,
        # so the history lives in a folder that is writable under the sandbox.
        return self.home / "state" / "history"


def ensure_home(home: Path | None = None) -> Path:
    home = home or clara_home()
    for sub in ("sessions", "skills", "backups", "tls", "projects", "schedule-runs", "netaudit-runs", "state"):
        (home / sub).mkdir(parents=True, exist_ok=True)
    # Files the sandbox grants write access to must exist before the lock is applied.
    for name in ("memory.md", "netaudit.log"):
        f = home / name
        if not f.exists():
            f.touch()
    cfg = home / "config.toml"
    if not cfg.exists():
        cfg.write_text(DEFAULT_CONFIG_TOML, encoding="utf-8")
    return home


def project_slug(root: Path) -> str:
    """Stable file name for a project: readable tail plus a short hash of the full path."""
    import hashlib
    full = str(Path(root).resolve())
    tail = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(full).name).strip("-") or "project"
    return f"{tail}-{hashlib.sha256(full.encode()).hexdigest()[:10]}"


def load_settings(home: Path | None = None) -> Settings:
    home = ensure_home(home)
    raw: dict[str, Any] = {}
    cfg = home / "config.toml"
    try:
        raw = tomllib.loads(cfg.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        # No silent fallback: say that the file was ignored and why.
        import sys
        print(f"clara: config {cfg} not readable ({exc}); using built-in defaults", file=sys.stderr)
    model = raw.get("model", {})
    net = raw.get("network", {})
    sandbox = raw.get("sandbox", {})
    agent = raw.get("agent", {})
    serve = raw.get("serve", {})
    s = Settings(home=home, raw=raw)
    s.base_url = str(model.get("base_url", s.base_url)).rstrip("/")
    s.model = str(model.get("model", s.model))
    s.tool_mode = str(model.get("tool_mode", s.tool_mode))
    s.timeout = float(model.get("timeout", s.timeout))
    s.temperature = model.get("temperature", None)
    s.allowed_hosts = list(net.get("allowed_hosts", s.allowed_hosts))
    s.sandbox_mode = str(sandbox.get("mode", s.sandbox_mode))
    s.extra_write_paths = list(sandbox.get("extra_write_paths", []))
    s.read_deny = list(sandbox.get("read_deny", []))
    s.extra_connect_ports = [int(x) for x in sandbox.get("extra_connect_ports", [])]
    s.unix_sockets = str(sandbox.get("unix_sockets", s.unix_sockets))
    perm = raw.get("permissions", {})
    s.approver_url = str(perm.get("approver_url", ""))
    s.approver_timeout = float(perm.get("approver_timeout", s.approver_timeout))
    s.max_turns = int(agent.get("max_turns", s.max_turns))
    s.context_warn_fraction = float(agent.get("context_warn_fraction", s.context_warn_fraction))
    s.show_reasoning = bool(agent.get("show_reasoning", s.show_reasoning))
    s.serve_host = str(serve.get("host", s.serve_host))
    s.serve_port = int(serve.get("port", s.serve_port))
    s.serve_serialize = bool(serve.get("serialize", s.serve_serialize))
    env_url = os.environ.get("CLARA_BASE_URL")
    if env_url:
        s.base_url = env_url.rstrip("/")
    env_model = os.environ.get("CLARA_MODEL")
    if env_model:
        s.model = env_model
    return s


# ---------------------------------------------------------------- project settings

@dataclass
class ProjectSettings:
    """Per-project permission rules.

    Stored in ~/.clara/projects/<slug>.json, outside the project folder on
    purpose: with the sandbox on, the model can write anywhere in the project
    but not here, so it cannot grant itself rules for the next session.
    """
    root: Path
    home: Path = field(default_factory=clara_home)
    allow: list[str] = field(default_factory=list)
    deny: list[str] = field(default_factory=list)

    @property
    def path(self) -> Path:
        return self.home / "projects" / f"{project_slug(self.root)}.json"

    @classmethod
    def load(cls, root: Path, home: Path | None = None) -> "ProjectSettings":
        ps = cls(root=root, home=home or clara_home())
        try:
            data = json.loads(ps.path.read_text(encoding="utf-8"))
            ps.allow = list(data.get("allow", []))
            ps.deny = list(data.get("deny", []))
        except FileNotFoundError:
            pass
        except (OSError, json.JSONDecodeError) as exc:
            import sys
            print(f"clara: {ps.path} unreadable ({exc}); starting with no rules", file=sys.stderr)
        return ps

    def save(self) -> None:
        """Raises OSError when the file cannot be written (e.g. under the sandbox)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"project": str(self.root), "allow": self.allow, "deny": self.deny}, indent=2) + "\n",
                             encoding="utf-8")


def find_project_root(start: Path | None = None) -> Path:
    """The project is the first ancestor with .clara/, CLARA.md or .git, else cwd."""
    cur = (start or Path.cwd()).resolve()
    own_home = clara_home().resolve()
    for p in [cur, *cur.parents]:
        # ~/.clara is Clara's own home, not a project marker: without this check every folder under $HOME
        # resolved to $HOME, and --yolo made the whole home directory writable
        marker = (p / ".clara").is_dir() and (p / ".clara").resolve() != own_home
        if p == Path.home().resolve() and not (p / "CLARA.md").is_file():
            break
        if marker or (p / "CLARA.md").is_file() or (p / ".git").exists():
            return p
    return cur
