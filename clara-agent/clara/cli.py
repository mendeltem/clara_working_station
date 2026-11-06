"""Command line entry point: `clara`."""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from . import __version__, netguard, sandbox, terminal
from .agent import Agent
from .config import ProjectSettings, Settings, find_project_root, load_settings
from .permissions import MODES, Permissions
from .providers.base import Provider, ProviderError
from .providers.mock import MockProvider
from .providers.openai_local import LocalOpenAIProvider
from .sessions import Session, list_sessions
from .terminal import BOLD, CYAN, DIM, YELLOW, HeadlessUI, TerminalUI, c
from .tools.base import default_registry, session_registry

DEMO_SCRIPT = [
    "<think>The user wants to see how I work. I will list the project files first.</think>"
    "Let me look at the project.\n<tool_call>\n{\"name\": \"list_files\", \"arguments\": {\"pattern\": \"*\", \"max_results\": 20}}\n</tool_call>",
    "This is a demonstration with a scripted model (no server needed). I listed the project folder above. "
    "Start a real model server and run `clara doctor` to connect Clara to it.",
]


@dataclass
class Runtime:
    settings: Settings
    project_root: Path
    provider: Provider
    permissions: Permissions
    session: Session
    agent: Agent
    sandbox_report: sandbox.SandboxReport | None


# ------------------------------------------------------------------ parser

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="clara", description="Clara: offline coding agent for a local model server.")
    p.add_argument("--version", action="version", version=f"clara {__version__}")
    p.add_argument("-p", "--print", dest="prompt", metavar="PROMPT", help="one-shot: answer and exit")
    p.add_argument("-C", "--project", metavar="DIR", help="project folder (default: detected from cwd)")
    p.add_argument("--mode", choices=MODES, help="permission mode (default: default)")
    p.add_argument("--yolo", action="store_true", help="same as --mode yolo")
    p.add_argument("--sandbox", choices=["auto", "on", "off"], help="Landlock sandbox (default from config: auto)")
    p.add_argument("--no-record", action="store_true", help="do not write the session to disk")
    p.add_argument("--resume", metavar="ID", help="resume a session by id, or 'last'")
    p.add_argument("--url", metavar="URL", help="model server base URL (default from config)")
    p.add_argument("--model", metavar="NAME", help="model id as listed by /v1/models")
    p.add_argument("--tool-mode", choices=["native", "prompt"], help="how tools are passed to the model")
    p.add_argument("--wait", type=float, metavar="SECONDS", default=0, help="wait this long for the server to come up")
    p.add_argument("--max-turns", type=int, metavar="N")
    p.add_argument("--quiet", action="store_true", help="hide the model's reasoning")
    p.add_argument("--mock", action="store_true", help="use a scripted model (demo, no server)")
    p.add_argument("--allow-tool", action="append", default=[], metavar="RULE", help="extra allow rule for this run")
    sub = p.add_subparsers(dest="command")
    d = sub.add_parser("doctor", help="check config, server, sandbox and network guard")
    d.add_argument("--probe", action="store_true", help="send one real request (wakes a sleeping llama-server)")
    d.add_argument("--url", metavar="URL", default=argparse.SUPPRESS)
    d.add_argument("--model", metavar="NAME", default=argparse.SUPPRESS)
    a = sub.add_parser("audit", help="show the network audit log")
    a.add_argument("--last", type=int, default=30)
    a.add_argument("--clear", action="store_true")
    sub.add_parser("sessions", help="list recorded sessions")
    sub.add_parser("connect", help="attach this terminal to the main Clara in clara serve (same conversation as Telegram)")
    mp = sub.add_parser("memory", help="long-term memory (hippocampus): show, sleep, search, log")
    mp.add_argument("action", nargs="?", default="show", choices=["show", "sleep", "search", "log"])
    mp.add_argument("query", nargs="*", help="for search: words to look for among faded memories")
    s = sub.add_parser("serve", help="start the local API and the phone app")
    s.add_argument("--lan", action="store_true", help="listen on all interfaces (phone over WLAN)")
    s.add_argument("--host")
    s.add_argument("--port", type=int)
    s.add_argument("--tls", action="store_true", help="serve HTTPS with the certificate from `clara pair --tls`")
    s.add_argument("--mode", choices=MODES, default=argparse.SUPPRESS, help="permission mode for app sessions (default: default)")
    s.add_argument("--yolo", action="store_true", default=argparse.SUPPRESS, help="same as --mode yolo (applies the sandbox)")
    s.add_argument("--sandbox", choices=["auto", "on", "off"], default=argparse.SUPPRESS)
    s.add_argument("--mock", action="store_true", default=argparse.SUPPRESS, help="scripted model, no server (demo)")
    s.add_argument("--wait", type=float, default=argparse.SUPPRESS, metavar="SECONDS")
    pr = sub.add_parser("pair", help="print the URL (and token) for the phone; --tls creates a local certificate")
    pr.add_argument("--tls", action="store_true")
    pr.add_argument("--host", help="address the phone should use (default: detected LAN address)")
    pr.add_argument("--port", type=int)
    pr.add_argument("--new-token", action="store_true")
    sub.add_parser("desktop", help="open the app in a window (pywebview) or print the URL")
    sub.add_parser("sandbox-check", help="apply the sandbox to a child process and verify it holds")
    sc = sub.add_parser("schedule", help="local scheduled tasks")
    sc_sub = sc.add_subparsers(dest="schedule_command")
    sa = sc_sub.add_parser("add")
    sa.add_argument("--every", required=True, help="interval such as 30m, 2h, 1d")
    sa.add_argument("--mode", choices=MODES, default="default")
    sa.add_argument("prompt")
    sc_sub.add_parser("list")
    sr = sc_sub.add_parser("remove")
    sr.add_argument("id")
    sc_sub.add_parser("run", help="run due tasks once")
    sl = sc_sub.add_parser("loop", help="run due tasks forever")
    sl.add_argument("--interval", type=int, default=60)
    return p


# --------------------------------------------------------------- bootstrap

def _server_port(base_url: str) -> int | None:
    u = urlparse(base_url)
    if u.port:
        return u.port
    if u.scheme == "https":
        return 443
    if u.scheme == "http":
        return 80
    return None


def install_guard(settings: Settings, interactive: bool) -> None:
    host = urlparse(settings.base_url).hostname or ""
    allowed = set(settings.allowed_hosts)
    asker = None
    if interactive:
        def asker(h, port, kind):
            sys.stdout.write(c(YELLOW, f"\n! Clara wants to {kind} to {h}:{port}, which is not in the offline allowlist. Allow once? [y/N] "))
            sys.stdout.flush()
            try:
                return input().strip().lower() == "y"
            except (EOFError, KeyboardInterrupt):
                return False
    netguard.install(allowed, settings.audit_log, mode="ask" if interactive else "deny", asker=asker)
    if host and not netguard.is_allowed(host):
        print(c(YELLOW, f"! model server host {host} is not in [network].allowed_hosts; requests to it will be refused"),
              file=sys.stderr)


def make_provider(settings: Settings, args, status=None) -> Provider:
    if getattr(args, "mock", False):
        return MockProvider(list(DEMO_SCRIPT))
    prov = LocalOpenAIProvider(settings.base_url, settings.model, timeout=settings.timeout,
                               temperature=settings.temperature,
                               extra_body=quelle_body())
    if args.wait:
        prov.wait_until_up(args.wait, status_cb=status)
    if not prov.model:
        try:
            models = prov.models()
        except ProviderError as exc:
            raise SystemExit(c(YELLOW, f"cannot reach the model server at {settings.base_url}: {exc}\n"
                                       "Start it (or use --wait SECONDS, or --mock for a demo)."))
        if not models:
            raise SystemExit("the server lists no models")
        prov.model = models[0]
        if len(models) > 1:
            print(c(DIM, f"server lists {len(models)} models; using {models[0]} (set --model to choose)"), file=sys.stderr)
    return prov


def _approver(settings: Settings):
    if not settings.approver_url:
        return None
    from .approver import make_approver
    return make_approver(settings.approver_url, settings.approver_timeout)


def apply_sandbox(settings: Settings, project_root: Path, perm_mode: str, sandbox_mode: str,
                  bind_ports=()) -> sandbox.SandboxReport | None:
    want = sandbox_mode == "on" or (sandbox_mode == "auto" and perm_mode == "yolo")
    if not want:
        return None
    port = _server_port(settings.base_url)
    connect_ports = ([port] if port else []) + list(settings.extra_connect_ports)
    aport = _server_port(settings.approver_url) if settings.approver_url else None
    if aport:
        connect_ports.append(aport)
    rep = sandbox.apply(sandbox.default_write_paths(project_root, settings.home, settings.extra_write_paths,
                                                    rules_writable=(perm_mode != "yolo")),
                        connect_ports=connect_ports, bind_ports=list(bind_ports),
                        write_files=sandbox.default_write_files(settings.home), read_deny=settings.read_deny,
                        unix_sockets=settings.unix_sockets, read_allow=settings.read_allow)
    if not rep.applied:
        print(c(YELLOW, f"! sandbox requested but not applied: {rep.reason}"), file=sys.stderr)
    if rep.missing_paths:
        print(c(DIM, f"sandbox: these folders do not exist and were skipped: {rep.missing_paths}"), file=sys.stderr)
    return rep


def bootstrap(args, interactive: bool, ui) -> Runtime:
    settings = load_settings()
    if args.url:
        settings.base_url = args.url.rstrip("/")
    if args.model:
        settings.model = args.model
    if args.tool_mode:
        settings.tool_mode = args.tool_mode
    if args.max_turns:
        settings.max_turns = args.max_turns
    if args.quiet:
        settings.show_reasoning = False
    install_guard(settings, interactive)
    project_root = Path(args.project).resolve() if args.project else find_project_root()
    perm_mode = "yolo" if args.yolo else (args.mode or "default")
    sandbox_mode = args.sandbox or settings.sandbox_mode
    rep = apply_sandbox(settings, project_root, perm_mode, sandbox_mode)
    if perm_mode == "yolo" and (rep is None or not rep.applied):
        print(c(YELLOW, "! yolo without a working sandbox: the model can write anywhere you can"), file=sys.stderr)
    provider = make_provider(settings, args, status=ui.on_status if ui else None)
    ps = ProjectSettings.load(project_root, settings.home)
    for rule in args.allow_tool:
        if rule not in ps.allow:
            ps.allow.append(rule)
    # Headless runs have no one to ask: the asker stays None so a refusal is
    # reported as "no user available", not as a denial by a user.
    perms = Permissions(ps, mode=perm_mode, approver=_approver(settings), asker=ui.ask_permission if (ui and interactive) else None,
                        project_root=project_root, home=settings.home)
    if args.resume:
        session = resume_session(settings, args.resume)
        if session.project_root and Path(session.project_root).exists():
            project_root = Path(session.project_root)
    else:
        session = Session.new(settings.sessions_dir, project_root, record=not args.no_record)
    agent = Agent(settings, provider, session_registry(settings), perms, session, ui, project_root,
                  tool_mode=settings.tool_mode)
    return Runtime(settings, project_root, provider, perms, session, agent, rep)


def resume_session(settings: Settings, which: str) -> Session:
    if which == "last":
        items = list_sessions(settings.sessions_dir, limit=1)
        if not items:
            raise SystemExit("no recorded sessions")
        return Session.load(Path(items[0]["path"]))
    path = settings.sessions_dir / f"{which}.jsonl"
    if not path.exists():
        matches = list(settings.sessions_dir.glob(f"*{which}*.jsonl"))
        if len(matches) == 1:
            path = matches[0]
        else:
            raise SystemExit(f"session {which} not found ({len(matches)} matches)")
    return Session.load(path)


# -------------------------------------------------------------------- chat

HELP = """commands
  /help            this list
  /status          model, context use, sandbox, guard
  /mode [m]        show or set permission mode: default | accept-edits | yolo
  /yolo            same as /mode yolo: every tool call runs without asking
  /think           toggle showing the model's reasoning
  /compact         summarise the conversation (memory first, then a fresh start)
  /remember        let the memory (hippocampus) take what mattered from this conversation now
  /clear           start a new session (also /neu, /new as on Telegram)
  /sessions        list recorded sessions
  /resume ID       switch to a recorded session (ID or last)
  /tools           list tools
  /skills          list skills
  /audit           last network audit entries
  /memory          show notes from earlier sessions
  /work            overview of the work sessions and the job queue
  /exit            leave (also /quit, /q or Ctrl+D)

keys
  typing a slash at the line start shows the command menu
  Tab              complete a command, its argument (mode, session id) or a file path
  Up / Down        earlier inputs (history is kept between sessions)
  Ctrl+C           interrupt the model while it works
  Ctrl+D           leave
  Ctrl-b d         detach from the tmux window; Clara keeps running (clara --an to come back)

permission prompts
  y once · s for this session · p save as rule · n deny

settings (environment, before starting)
  CLARA_THEME=plain      old colours          CLARA_BG=off         keep the terminal background
  CLARA_SLASH_MENU=off   no menu on slash     NO_COLOR=1           no colours at all

outside the session
  clara --help lists every start option; clara --liste shows all open sessions"""


def quelle_body() -> dict:
    """06.11.2026: who is asking (CLARA_QUELLE, e.g. chat/terminal, arbeit/<name>) -- the Weiche relay reads x_quelle
    to put chat requests ahead of background work on the local model."""
    q = os.environ.get("CLARA_QUELLE", "")
    return {"x_quelle": q[:40]} if q else {}


def banner(rt: Runtime) -> None:
    prov = rt.provider
    model = getattr(prov, "model", prov.name)
    if terminal.THEME != "plain":
        return banner_dark(rt)
    print(c(BOLD, f"Clara {__version__}") + c(DIM, " · offline agent · /help for commands"))
    line = f"model    {model} @ {rt.settings.base_url}"
    if isinstance(prov, LocalOpenAIProvider):
        info = prov.detect()
        extra = []
        if info.get("kind") != "unknown":
            extra.append(info["kind"])
        if info.get("n_ctx"):
            extra.append(f"{info['n_ctx']} ctx")
        if info.get("sleeping"):
            extra.append("sleeping")
        if info.get("slots_busy"):
            extra.append(f"{info['slots_busy']}/{info['slots_total']} slots busy")
        if extra:
            line += " (" + ", ".join(extra) + ")"
    print(c(DIM, line))
    sb = rt.sandbox_report.summary() if rt.sandbox_report else "sandbox off"
    print(c(DIM, f"mode     {rt.permissions.mode} · {sb}"))
    rec = "recorded" if rt.session.path else "not recorded"
    print(c(DIM, f"project  {rt.project_root} · session {rt.session.id} ({rec})"))
    g = netguard.counts()
    print(c(DIM, f"guard    allowed hosts {sorted(netguard.allowed_hosts())} · audit {rt.settings.audit_log}"))


def banner_dark(rt: Runtime) -> None:
    """Welcome box. No model call: the server is only asked for its model list."""
    prov = rt.provider
    model = getattr(prov, "model", prov.name)
    w = min(terminal.width(), 100)
    home = os.path.expanduser("~")
    proj = str(rt.project_root).replace(home, "~", 1)
    sb = rt.sandbox_report.summary() if rt.sandbox_report else "sandbox off"
    rows = [(c(terminal.ACCENT, "✻ ") + c(terminal.BOLD, f"Clara {__version__}") + c(DIM, " · welcome back"), 2 + 6 + len(__version__) + 15),
            ("", 0),
            (c(DIM, "  /help for commands · Ctrl+C interrupts · Ctrl+D leaves"), 56),
            ("", 0),
            (c(DIM, "  cwd:   ") + proj, 9 + len(proj)),
            (c(DIM, "  model: ") + f"{model}" + c(DIM, f" @ {rt.settings.base_url}"), 9 + len(str(model)) + 3 + len(rt.settings.base_url)),
            (c(DIM, f"  mode:  {rt.permissions.mode} · {sb}"), 9 + len(f"{rt.permissions.mode} · {sb}"))]
    print(c(terminal.ACCENT, "╭" + "─" * (w - 2) + "╮"))
    for text, n in rows:
        if n > w - 4:   # too long for the box: cut the plain part, colours are lost, still readable
            import re as _re
            plain = _re.sub(r"\x1b\[[0-9;]*m", "", text)[: w - 5] + "…"
            text, n = plain, len(plain)
        print(c(terminal.ACCENT, "│") + " " + text + " " * max(0, w - 3 - n) + c(terminal.ACCENT, "│"))
    print(c(terminal.ACCENT, "╰" + "─" * (w - 2) + "╯"))


def work_overview() -> list[str]:
    """Lines for the work sessions (clara-arbeit) and the job queue (dauerlauf), newest first. Read-only, files only."""
    from .tools.work import BASES, _status
    rows = []
    for place in ("cle", "lokal"):
        base = Path(os.path.expanduser(BASES[place]))
        if not base.is_dir():
            continue
        for d in base.iterdir():
            if d.is_dir() and (d / "AUFTRAG.txt").exists():
                st = _status(d)
                start = st.get("started") or (d / "AUFTRAG.txt").stat().st_mtime
                end = st.get("finished") or time.time()
                mins = int(end - start) // 60
                dur = f"{mins // 60}h {mins % 60:02d}m" if mins >= 60 else f"{mins}m"
                rows.append((start, st.get("state", "?"), d.name, dur, "Clé" if place == "cle" else "local GPU"))
    rows.sort(reverse=True)
    out = []
    marks = {"running": "▶", "done": "✔", "waiting to start": "…"}
    for _, state, name, dur, where in rows[:12]:
        mark = marks.get(state, "✖" if state in ("failed", "timed out") else "■")
        out.append(f"  {mark} {name:<28} {state:<18} {dur:>7}  {where}")
    if len(rows) > 12:
        out.append(f"  … {len(rows) - 12} older (work_status lists all)")
    queue = Path(os.path.expanduser("~/data/work/auftraege"))
    if queue.is_dir():
        n = {k: len(list((queue / k).glob("*.txt"))) for k in ("laeuft", "offen", "steckengeblieben")}
        out.append(f"  queue: {n['laeuft']} running · {n['offen']} waiting · {n['steckengeblieben']} stuck")
    return out


def print_work_overview(title: bool = True) -> None:
    lines = work_overview()
    if not lines:
        return
    if title:
        print(c(terminal.ACCENT, "  Work sessions"))
    for ln in lines:
        color = terminal.TEXT if "▶" in ln else DIM
        print(c(color, ln))


def run_turn(rt: Runtime, text: str) -> None:
    """Run the agent in a worker thread so Ctrl+C can interrupt it."""
    result: dict = {}
    done = threading.Event()

    def work():
        try:
            result["text"] = rt.agent.run(text)
        except Exception as exc:  # keep the REPL alive
            result["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            done.set()

    t = threading.Thread(target=work, daemon=True)
    begin, end = getattr(rt.agent.ui, "begin_turn", None), getattr(rt.agent.ui, "end_turn", None)
    if begin:
        begin()
    t.start()
    while not done.is_set():
        try:
            done.wait(0.2)
        except KeyboardInterrupt:
            rt.agent.interrupt()
            print(c(YELLOW, "\n! interrupting (press Enter if a prompt is waiting)"))
    if end:
        end()
    if "error" in result:
        print(c(YELLOW, f"! {result['error']}"))


def slash(rt: Runtime, ui: TerminalUI, line: str) -> Runtime | None:
    cmd, _, arg = line.strip().partition(" ")
    arg = arg.strip()
    s = rt.settings
    if cmd in ("/exit", "/quit", "/q"):
        raise EOFError
    if cmd == "/help":
        print(HELP)
    elif cmd == "/status":
        m = rt.agent.meter
        est = m.estimate(rt.agent.messages, rt.agent._tools_param())
        label = "measured" if m.calibrated else "estimated"
        ctx = rt.agent.n_ctx or rt.provider.context_size()
        print(f"model     {getattr(rt.provider, 'model', rt.provider.name)} @ {s.base_url}")
        print(f"context   ~{est} tokens ({label})" + (f" of {ctx}" if ctx else " (server context unknown)"))
        print(f"messages  {len(rt.agent.messages)} · tool mode {rt.agent.tool_mode} · mode {rt.permissions.mode}")
        print(f"sandbox   {rt.sandbox_report.summary() if rt.sandbox_report else 'off'}")
        print(f"guard     {netguard.counts()} · allowed {sorted(netguard.allowed_hosts())}")
        if m.last_timings:
            print(f"last call {json.dumps({k: v for k, v in m.last_timings.items() if k in ('prompt_n', 'prompt_per_second', 'predicted_n', 'predicted_per_second')})}")
    elif cmd == "/mode":
        if arg:
            try:
                rt.permissions.set_mode(arg)
            except ValueError as exc:
                print(c(YELLOW, str(exc)))
            if arg == "yolo" and (rt.sandbox_report is None or not rt.sandbox_report.applied):
                print(c(YELLOW, "! yolo without sandbox; restart with --yolo to apply Landlock (it cannot be applied after threads exist)"))
        print(f"mode {rt.permissions.mode}")
    elif cmd == "/yolo":
        return slash(rt, ui, "/mode yolo")
    elif cmd == "/think":
        s.show_reasoning = not s.show_reasoning
        ui.show_reasoning = s.show_reasoning
        print(f"reasoning {'shown' if s.show_reasoning else 'hidden'}")
    elif cmd == "/compact":
        print(c(DIM, "compacting…"))
        got = rt.agent.remember()
        if got:
            print(c(DIM, f"memory: {got['kept']} new, {got['strengthened']} strengthened, {got['rejected']} let go"))
        print(rt.agent.compact())
    elif cmd == "/work":
        print_work_overview(title=False) if work_overview() else print(c(DIM, "no work sessions yet"))
    elif cmd == "/remember":
        got = rt.agent.remember()
        print(c(DIM, "memory is off ([memory] hippocampus = true in config.toml)") if got is None else
              c(DIM, f"memory: {got['kept']} new, {got['strengthened']} strengthened, {got['rejected']} let go"))
    elif cmd in ("/clear", "/neu", "/new"):
        session = Session.new(s.sessions_dir, rt.project_root, record=rt.session.path is not None)
        rt.session = session
        rt.agent = Agent(s, rt.provider, rt.agent.tools, rt.permissions, session, ui, rt.project_root,
                         tool_mode=rt.agent.tool_mode)
        print(f"new session {session.id}")
    elif cmd == "/sessions":
        for it in list_sessions(s.sessions_dir):
            print(f"{it['id']}  {it['messages']:3d} msgs  {it['title']}")
    elif cmd == "/resume":
        if not arg:
            print("usage: /resume ID|last")
        else:
            session = resume_session(s, arg)
            rt.session = session
            rt.agent = Agent(s, rt.provider, rt.agent.tools, rt.permissions, session, ui, rt.project_root,
                             tool_mode=rt.agent.tool_mode)
            print(f"resumed {session.id} ({len(session.messages)} messages)")
    elif cmd == "/tools":
        for t in rt.agent.tools.tools.values():
            print(f"{t.name:12s} {t.kind:6s} {t.description}")
    elif cmd == "/skills":
        from .skills import discover_skills
        found = discover_skills([s.skills_dir, rt.project_root / ".clara" / "skills"])
        print("\n".join(f"{f['name']}: {f['description']} ({f['path']})" for f in found) or "no skills found")
    elif cmd == "/audit":
        for e in netguard.read_audit(s.audit_log, 20):
            print(f"{e['ts']} {e['decision']:9s} {e['event']:7s} {e['host']}:{e['port']}  {e['where']}")
    elif cmd == "/memory":
        from .prompts import read_memory
        print(read_memory([s.memory_file, rt.project_root / ".clara" / "memory.md"]) or "no notes yet")
    else:
        print(c(YELLOW, f"unknown command {cmd}; /help lists commands"))
    return None


def chat(args) -> int:
    ui = TerminalUI(show_reasoning=not args.quiet)
    terminal.dark_background(True)
    import atexit
    atexit.register(terminal.dark_background, False)
    rt = bootstrap(args, interactive=True, ui=ui)
    banner(rt)
    if os.environ.get("CLARA_WORK_TOOLS") == "1" or (rt.settings.raw.get("work") or {}).get("tools"):
        print_work_overview()           # 06.11.2026: the chat Clara opens with the overview of her work sessions
    histfile = rt.settings.history_file
    try:
        import readline  # noqa: F401
        try:
            readline.read_history_file(histfile)
        except (OSError, ValueError):
            pass
        readline.set_history_length(2000)
        # 06.11.2026: "/" shows the command menu, Tab completes commands, /mode, /resume and paths
        from . import completion
        prompt = ("\x01\x1b[" + terminal.ACCENT + "m\x02❯ \x01\x1b[0m\x02") if terminal.COLOR else "❯ "
        completion.install(completion.Completer(
            completion.commands_from_help(HELP), prompt, dim=terminal.DIM, accent=terminal.ACCENT,
            sessions=lambda: [it["id"] for it in list_sessions(rt.settings.sessions_dir)]),
            auto_slash=sys.stdin.isatty())
    except ImportError:
        readline = None
    try:
        while True:
            try:
                if terminal.THEME != "plain":
                    print("\n" + terminal.rule())
                    line = input("\x01" + "\x1b[" + terminal.ACCENT + "m\x02❯ \x01\x1b[0m\x02" if terminal.COLOR else "❯ ")
                    print(terminal.rule())
                else:
                    line = input(c(CYAN, "\nclara ❯ "))
            except KeyboardInterrupt:
                print(c(DIM, "\n(Ctrl+D or /exit to leave)"))
                continue
            except EOFError:
                print()
                break
            if not line.strip():
                continue
            if line.startswith("/"):
                try:
                    slash(rt, ui, line)
                except EOFError:
                    break
                continue
            run_turn(rt, line)
    finally:
        if rt.settings.hippocampus and any(m.get("role") == "user" for m in rt.agent.messages):
            print(c(DIM, "✻ remembering what mattered…"))
            got = rt.agent.remember()
            if got:
                print(c(DIM, f"  {got['kept']} new, {got['strengthened']} strengthened, {got['rejected']} let go"))
        if readline is not None:
            try:
                readline.write_history_file(histfile)
            except OSError as exc:
                print(c(DIM, f"(input history not saved: {exc})"))
    return 0


def one_shot(args) -> int:
    ui = HeadlessUI(show_reasoning=False, verbose=True)
    rt = bootstrap(args, interactive=False, ui=ui)
    text = rt.agent.run(args.prompt)
    if text.startswith("[no answer:"):
        sys.stderr.write("clara: " + text + "\n")   # no result is a failure, also for a supervisor that only sees the exit code
        return 1
    if text:
        sys.stdout.write(text.rstrip() + "\n")
        return 0
    sys.stderr.write("clara: no answer\n")
    return 1


# ------------------------------------------------------------------ doctor

def doctor(args) -> int:
    settings = load_settings()
    if args.url:
        settings.base_url = args.url.rstrip("/")
    if args.model:
        settings.model = args.model
    install_guard(settings, interactive=False)
    ok = True
    print(c(BOLD, f"Clara {__version__} doctor"))
    print(f"home        {settings.home}  (config: {settings.home / 'config.toml'})")
    print(f"python      {sys.version.split()[0]} on {sys.platform}")
    print(f"netguard    installed, allowed hosts {sorted(netguard.allowed_hosts())}, audit {settings.audit_log}")
    abi = sandbox.abi_version()
    if abi >= 4:
        print(f"sandbox     Landlock ABI {abi}: file write lock and TCP lock available")
    elif abi >= 1:
        print(c(YELLOW, f"sandbox     Landlock ABI {abi}: file write lock only, no TCP lock (kernel < 6.7)"))
    else:
        print(c(YELLOW, "sandbox     Landlock not available (not Linux, kernel too old, or disabled)"))
    prov = LocalOpenAIProvider(settings.base_url, settings.model, timeout=settings.timeout)
    info = prov.detect()
    if "error" in info:
        ok = False
        print(c(YELLOW, f"server      {settings.base_url}: {info['error']}"))
        print(c(DIM, "            start llama-server (port 8000), Ollama (11434) or LM Studio (1234), or set [model].base_url"))
    else:
        print(f"server      {settings.base_url}: {info['kind']}" + (f", {info.get('build')}" if info.get("build") else ""))
        print(f"models      {info['models']}")
        chosen = settings.model or (info["models"][0] if info["models"] else "")
        if settings.model and settings.model not in info["models"]:
            ok = False
            print(c(YELLOW, f"model       {settings.model} is not in the server's list"))
        else:
            print(f"model       {chosen or '(none)'}" + ("" if settings.model else "  (first listed; set [model].model to pin it)"))
        if info.get("n_ctx"):
            print(f"context     {info['n_ctx']} tokens (from /props)")
        elif info["kind"] == "ollama":
            print(c(YELLOW, "context     unknown: Ollama truncates prompts above num_ctx silently; set a large num_ctx in the Modelfile"))
        else:
            print(c(DIM, "context     the server does not report its context size"))
        if info.get("sleeping") is not None:
            print(f"state       {'sleeping (will reload on first request)' if info['sleeping'] else 'loaded'}")
        if info.get("slots_total"):
            print(f"slots       {info['slots_busy']} of {info['slots_total']} busy")
        if args.probe and chosen:
            prov.model = chosen
            print(c(DIM, "probe       sending one request with a tool definition (this wakes the server)…"))
            t0 = time.time()
            tools = default_registry(include=["list_files"]).schemas()
            msgs = [{"role": "system", "content": "You are a tool-using assistant."},
                    {"role": "user", "content": "Call the list_files tool for the current folder."}]
            calls, text = [], ""
            try:
                for ev in prov.stream(msgs, tools, max_tokens=512):
                    if ev.kind == "tool_call":
                        calls.append(ev.tool_call)
                    if ev.kind == "done":
                        text = ev.text
                        native = ev.data.get("native_tool_calls")
                dt = time.time() - t0
                if calls:
                    how = "native tool_calls" if native else "<tool_call> text (parsed by Clara)"
                    print(f"probe       tool call received as {how} in {dt:.1f} s: {calls[0].name}({calls[0].arguments})")
                else:
                    ok = False
                    print(c(YELLOW, f"probe       no tool call in {dt:.1f} s; answer was: {text[:200]!r}"))
                    print(c(DIM, "            llama-server needs --jinja for tools; or set [model].tool_mode = \"prompt\""))
                n = prov.count_tokens("hello world, this is a token count test")
                if n is not None:
                    from .prompts import CORE
                    core_tokens = prov.count_tokens(CORE)
                    print(f"tokens      core system prompt = {core_tokens} tokens (measured via /tokenize)")
            except ProviderError as exc:
                ok = False
                print(c(YELLOW, f"probe       failed: {exc}"))
    print(c(BOLD, "result      " + ("ok" if ok else "problems found")))
    return 0 if ok else 1


def audit(args) -> int:
    settings = load_settings()
    if args.clear:
        try:
            settings.audit_log.unlink()
        except FileNotFoundError:
            pass
        print("audit log cleared")
        return 0
    entries = netguard.read_audit(settings.audit_log, args.last)
    if not entries:
        print(f"no entries in {settings.audit_log}")
        return 0
    for e in entries:
        print(f"{e['ts']} {e['decision']:9s} {e['event']:7s} {e['host']}:{e['port']}  {e['where']}")
    return 0


def memory_cmd(args) -> int:
    """clara memory [show|sleep|search WORDS|log]"""
    from .hippocampus import Hippocampus, strength
    settings = load_settings()
    hc = Hippocampus(settings.home, cap=settings.memory_cap)
    if args.action == "sleep":
        prov = LocalOpenAIProvider(settings.base_url, settings.model, timeout=settings.timeout)
        print(hc.sleep(prov))
        return 0
    if args.action == "search":
        for e in hc.search_faded(" ".join(args.query)):
            print(f"{time.strftime('%F', time.localtime(e.get('faded', 0)))}  {e['text']}")
        return 0
    if args.action == "log":
        print(hc.log.read_text(encoding="utf-8")[-6000:] if hc.log.is_file() else "no memory log yet")
        return 0
    entries = sorted(hc.load(), key=lambda e: -strength(e))
    for e in entries:
        pin = "rule" if e.get("pinned") else f"{strength(e):4.1f}"
        print(f"{pin:>5}  {e['kind']:<10} {e['text']}")
    print(f"{len(entries)} memories (cap {settings.memory_cap}) in {hc.store}")
    return 0


def sessions_cmd(args) -> int:
    settings = load_settings()
    items = list_sessions(settings.sessions_dir, limit=50)
    if not items:
        print(f"no sessions in {settings.sessions_dir}")
    for it in items:
        print(f"{it['id']}  {it['messages']:3d} msgs  {it['project']}  {it['title']}")
    return 0


def sandbox_check(args) -> int:
    from .sandboxcheck import run_check
    return run_check()


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "doctor":
            return doctor(args)
        if args.command == "audit":
            return audit(args)
        if args.command == "sessions":
            return sessions_cmd(args)
        if args.command == "memory":
            return memory_cmd(args)
        if args.command == "connect":
            from .remote import connect_command
            return connect_command(args)
        if args.command == "sandbox-check":
            return sandbox_check(args)
        if args.command in ("serve", "pair", "desktop"):
            from .server import serve_command
            return serve_command(args)
        if args.command == "schedule":
            from .scheduler import schedule_command
            return schedule_command(args)
        if args.prompt:
            return one_shot(args)
        return chat(args)
    except SystemExit as exc:
        if isinstance(exc.code, str):
            print(exc.code, file=sys.stderr)
            return 1
        raise
    except KeyboardInterrupt:
        print()
        return 130


if __name__ == "__main__":
    sys.exit(main())
