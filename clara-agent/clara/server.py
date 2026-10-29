"""`clara serve`: the local API and the phone app (Clara App).

One process, standard library only. Listens on 127.0.0.1 unless --lan. Every
request needs the token from ~/.clara/serve-token. Model runs are serialised
(one llama-server slot). Approvals and questions from the agent are pushed to
the phone as events and answered through the API.
"""
from __future__ import annotations

import json
import mimetypes
import os
import queue
import secrets
import socket
import ssl
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import __version__, netguard, sandbox
from .agent import Agent, UI
from .config import ProjectSettings, Settings, find_project_root, load_settings
from .permissions import MODES, PermissionRequest, Permissions
from .providers.base import Provider, ProviderError
from .providers.mock import MockProvider
from .providers.openai_local import LocalOpenAIProvider
from .sessions import Session, list_sessions
from .tools.base import default_registry

APP_DIR = Path(__file__).parent / "app"
PERMISSION_TIMEOUT = 15 * 60


# ------------------------------------------------------------- session state

class WebUI(UI):
    """Turns agent callbacks into an ordered event log that SSE clients read."""

    def __init__(self, owner: "ServerSession"):
        self.owner = owner
        self.show_reasoning = True

    def emit(self, kind: str, **data) -> None:
        self.owner.push({"kind": kind, **data})

    def on_text(self, delta):
        self.emit("text", delta=delta)

    def on_reasoning(self, delta):
        self.emit("reasoning", delta=delta)

    def on_tool_start(self, call, summary):
        self.emit("tool_start", id=call.id, name=call.name, summary=summary, args=call.arguments)

    def on_tool_result(self, call, result, decision):
        self.emit("tool_result", id=call.id, name=call.name, text=result.text[:20000], error=result.is_error,
                  decision=decision, seconds=result.data.get("seconds"))

    def on_status(self, text):
        self.emit("status", text=text)

    def on_warning(self, text):
        self.emit("warning", text=text)

    def on_turn_end(self, text):
        self.emit("turn_end", text=text)

    def on_todo(self, items):
        self.emit("todo", items=items)

    def ask_permission(self, req: PermissionRequest) -> str:
        rid = uuid.uuid4().hex[:10]
        req.id = rid
        waiter = self.owner.pending(rid, "permission")
        preview = ""
        if req.tool == "write_file":
            preview = str(req.args.get("content", ""))[:4000]
        elif req.tool == "edit_file":
            preview = "- " + str(req.args.get("old_text", ""))[:2000] + "\n+ " + str(req.args.get("new_text", ""))[:2000]
        self.emit("permission", id=rid, tool=req.tool, summary=req.summary, warnings=req.warnings(),
                  target=req.target[:500], preview=preview)
        answer = waiter.wait(PERMISSION_TIMEOUT)
        self.emit("permission_done", id=rid, answer=answer or "deny")
        return answer or "deny"

    def ask_user(self, question: str) -> str | None:
        rid = uuid.uuid4().hex[:10]
        waiter = self.owner.pending(rid, "question")
        self.emit("question", id=rid, question=question)
        answer = waiter.wait(PERMISSION_TIMEOUT)
        self.emit("question_done", id=rid, answer=answer)
        return answer


class Waiter:
    def __init__(self, kind: str):
        self.kind = kind
        self.event = threading.Event()
        self.answer: str | None = None

    def wait(self, timeout: float) -> str | None:
        self.event.wait(timeout)
        return self.answer

    def resolve(self, answer: str) -> None:
        self.answer = answer
        self.event.set()


class ServerSession:
    def __init__(self, app: "App", session: Session, project_root: Path, mode: str):
        self.app = app
        self.session = session
        self.project_root = project_root
        self.events: list[dict] = []
        self.seq = 0
        self.lock = threading.Lock()
        self.cv = threading.Condition(self.lock)
        self.busy = False
        self.waiters: dict[str, Waiter] = {}
        self.ui = WebUI(self)
        ps = ProjectSettings.load(project_root, app.settings.home)
        from .cli import _approver
        self.permissions = Permissions(ps, mode=mode, approver=_approver(app.settings), asker=self.ui.ask_permission, project_root=project_root,
                                       home=app.settings.home)
        self.agent = Agent(app.settings, app.provider, default_registry(), self.permissions, session, self.ui,
                           project_root, tool_mode=app.settings.tool_mode)

    def push(self, ev: dict) -> None:
        with self.cv:
            self.seq += 1
            ev["seq"] = self.seq
            ev["t"] = time.time()
            self.events.append(ev)
            if len(self.events) > 5000:
                self.events = self.events[-4000:]
            self.cv.notify_all()

    def pending(self, rid: str, kind: str) -> Waiter:
        w = Waiter(kind)
        with self.lock:
            self.waiters[rid] = w
        return w

    def resolve(self, rid: str, answer: str) -> bool:
        with self.lock:
            w = self.waiters.pop(rid, None)
        if w is None:
            return False
        w.resolve(answer)
        return True

    def cancel_pending(self) -> int:
        """Deny every open approval and skip every open question (used by interrupt)."""
        with self.lock:
            waiters = list(self.waiters.items())
            self.waiters.clear()
        for rid, w in waiters:
            w.resolve("deny" if w.kind == "permission" else "")
        return len(waiters)

    def start(self, text: str) -> bool:
        with self.lock:
            if self.busy:
                return False
            self.busy = True
        self.push({"kind": "user", "text": text})

        def work():
            try:
                with self.app.model_lock:
                    self.agent.run(text)
            except Exception as exc:
                self.push({"kind": "warning", "text": f"{type(exc).__name__}: {exc}"})
            finally:
                with self.lock:
                    self.busy = False
                self.push({"kind": "idle"})

        threading.Thread(target=work, daemon=True).start()
        return True

    def since(self, seq: int, timeout: float) -> list[dict]:
        with self.cv:
            if not any(e["seq"] > seq for e in self.events):
                self.cv.wait(timeout)
            out = [e for e in self.events if e["seq"] > seq]
            if self.events and seq and seq < self.events[0]["seq"] - 1:
                # Older events were trimmed: tell the client instead of returning a silent gap.
                out = [{"kind": "gap", "seq": seq, "t": time.time()}] + out
            return out

    def snapshot(self) -> dict:
        msgs = []
        for m in self.session.messages:
            role = m.get("role")
            if role == "system":
                continue
            entry = {"role": role, "content": m.get("content", "")}
            if m.get("tool_calls"):
                entry["tool_calls"] = m["tool_calls"]
            if role == "tool":
                entry["name"] = m.get("name")
            msgs.append(entry)
        with self.lock:
            pending = [{"id": k, "kind": w.kind} for k, w in self.waiters.items()]
            busy = self.busy
        return {"id": self.session.id, "project": str(self.project_root), "messages": msgs, "busy": busy,
                "mode": self.permissions.mode, "pending": pending, "todo": list(self.agent.ctx.todo_state),
                "seq": self.seq, "title": self.session.title}


# -------------------------------------------------------------------- app

TICKET_TTL = 120
MAX_BODY = 4 * 1024 * 1024


class App:
    def __init__(self, settings: Settings, provider: Provider, project_root: Path, token: str,
                 sandbox_report=None, default_mode: str = "default"):
        self.settings = settings
        self.provider = provider
        self.project_root = project_root
        self.token = token
        self.sessions: dict[str, ServerSession] = {}
        self.model_lock = threading.Lock()
        self.lock = threading.Lock()
        self.sandbox_report = sandbox_report
        self.default_mode = default_mode
        self.started = time.time()
        self.tickets: dict[str, tuple[str, float]] = {}   # ticket -> (session id, expiry)

    def new_ticket(self, sid: str) -> str:
        """Short-lived single-use ticket for the event stream, so the bearer token
        never travels in a URL (EventSource cannot send headers)."""
        tok = secrets.token_urlsafe(24)
        now = time.time()
        with self.lock:
            self.tickets = {t: v for t, v in self.tickets.items() if v[1] > now}
            self.tickets[tok] = (sid, now + TICKET_TTL)
        return tok

    def take_ticket(self, tok: str, sid: str) -> bool:
        with self.lock:
            entry = self.tickets.get(tok)
            if not entry or entry[0] != sid or entry[1] <= time.time():
                return False
            del self.tickets[tok]
        return True

    def new_session(self, project: str | None = None, record: bool = True) -> ServerSession:
        root = Path(project).resolve() if project else self.project_root
        session = Session.new(self.settings.sessions_dir, root, record=record)
        ss = ServerSession(self, session, root, self.default_mode)
        with self.lock:
            self.sessions[session.id] = ss
        return ss

    def get(self, sid: str) -> ServerSession | None:
        with self.lock:
            ss = self.sessions.get(sid)
        if ss is not None:
            return ss
        path = self.settings.sessions_dir / f"{sid}.jsonl"
        if path.exists():
            session = Session.load(path)
            root = Path(session.project_root) if session.project_root and Path(session.project_root).exists() else self.project_root
            ss = ServerSession(self, session, root, self.default_mode)
            with self.lock:
                self.sessions[sid] = ss
            return ss
        return None

    def status(self) -> dict:
        info = {}
        if isinstance(self.provider, LocalOpenAIProvider):
            try:
                info = self.provider.detect()
            except Exception as exc:
                info = {"error": str(exc)}
        return {"version": __version__, "model": getattr(self.provider, "model", self.provider.name),
                "base_url": self.settings.base_url, "server": info, "project": str(self.project_root),
                "sandbox": self.sandbox_report.summary() if self.sandbox_report else "off",
                "guard": netguard.counts(), "allowed_hosts": sorted(netguard.allowed_hosts()),
                "uptime": round(time.time() - self.started), "default_mode": self.default_mode,
                "sessions": sum(1 for s in self.sessions.values())}


# ---------------------------------------------------------------- handler

class Handler(BaseHTTPRequestHandler):
    app: App = None  # type: ignore
    protocol_version = "HTTP/1.1"
    server_version = "clara/" + __version__

    def log_message(self, fmt, *args):  # quiet by default; audit log is elsewhere
        if os.environ.get("CLARA_SERVE_LOG"):
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # ---- helpers
    def _json(self, code: int, data) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _static(self, name: str) -> None:
        path = (APP_DIR / name).resolve()
        if not path.is_file() or APP_DIR.resolve() not in path.parents:
            self._json(404, {"error": "not found"})
            return
        data = path.read_bytes()
        ctype = {".webmanifest": "application/manifest+json", ".js": "application/javascript",
                 ".html": "text/html; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png"}.get(
            path.suffix, mimetypes.guess_type(name)[0] or "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        if name == "sw.js":
            self.send_header("Service-Worker-Allowed", "/")
        self.end_headers()
        self.wfile.write(data)

    def _authed(self) -> bool:
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer ") and secrets.compare_digest(auth[7:].strip(), self.app.token):
            return True
        self._json(401, {"error": "missing or wrong token; run `clara pair` on the computer"})
        return False

    def _same_origin(self) -> bool:
        """A browser sends Origin on cross-site requests; only our own page may change state."""
        origin = self.headers.get("Origin")
        if not origin:
            return True
        host = self.headers.get("Host", "")
        return urlparse(origin).netloc == host

    def _body(self) -> dict | None:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0:
            return {}
        if n > MAX_BODY:
            self._json(413, {"error": f"body larger than {MAX_BODY} bytes"})
            return None
        try:
            return json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        except json.JSONDecodeError:
            return {}

    # ---- routes
    def do_GET(self):
        u = urlparse(self.path)
        parts = [p for p in u.path.split("/") if p]
        if not parts or parts == ["index.html"]:
            return self._static("index.html")
        if len(parts) == 1 and parts[0] in ("manifest.webmanifest", "sw.js", "icon.svg", "icon-192.png", "icon-512.png", "app.js", "app.css"):
            return self._static(parts[0])
        if parts[0] != "api":
            return self._json(404, {"error": "not found"})
        if len(parts) == 4 and parts[1] == "sessions" and parts[3] == "events":
            # The stream authenticates with a ticket from POST /api/sessions/{id}/ticket.
            q = parse_qs(u.query)
            ticket = (q.get("ticket") or [""])[0]
            if not self.app.take_ticket(ticket, parts[2]):
                return self._json(401, {"error": "stream ticket missing, used or expired"})
            ss = self.app.get(parts[2])
            if ss is None:
                return self._json(404, {"error": "no such session"})
            return self._sse(ss, u)
        if not self._authed():
            return
        if parts[1:] == ["status"]:
            return self._json(200, self.app.status())
        if parts[1:] == ["sessions"]:
            items = list_sessions(self.app.settings.sessions_dir, limit=40)
            with self.app.lock:
                active = {sid: s.busy for sid, s in self.app.sessions.items()}
            for it in items:
                it["busy"] = active.get(it["id"], False)
            return self._json(200, {"sessions": items})
        if parts[1:] == ["audit"]:
            return self._json(200, {"entries": netguard.read_audit(self.app.settings.audit_log, 50)})
        if len(parts) >= 3 and parts[1] == "sessions":
            ss = self.app.get(parts[2])
            if ss is None:
                return self._json(404, {"error": "no such session"})
            if len(parts) == 3:
                return self._json(200, ss.snapshot())
        return self._json(404, {"error": "not found"})

    def do_POST(self):
        u = urlparse(self.path)
        parts = [p for p in u.path.split("/") if p]
        if not parts or parts[0] != "api":
            return self._json(404, {"error": "not found"})
        if not self._same_origin():
            return self._json(403, {"error": "cross-site request refused"})
        if not self._authed():
            return
        body = self._body()
        if body is None:
            return
        if len(parts) == 4 and parts[1] == "sessions" and parts[3] == "ticket":
            if self.app.get(parts[2]) is None:
                return self._json(404, {"error": "no such session"})
            return self._json(200, {"ticket": self.app.new_ticket(parts[2]), "ttl": TICKET_TTL})
        if parts[1:] == ["sessions"]:
            ss = self.app.new_session(body.get("project"), record=not body.get("no_record"))
            return self._json(201, {"id": ss.session.id})
        if len(parts) >= 4 and parts[1] == "sessions":
            ss = self.app.get(parts[2])
            if ss is None:
                return self._json(404, {"error": "no such session"})
            action = parts[3]
            if action == "messages":
                text = str(body.get("text", "")).strip()
                if not text:
                    return self._json(400, {"error": "empty text"})
                if not ss.start(text):
                    return self._json(409, {"error": "session is busy"})
                return self._json(202, {"ok": True})
            if action == "interrupt":
                ss.agent.interrupt()
                cancelled = ss.cancel_pending()
                return self._json(200, {"ok": True, "cancelled": cancelled})
            if action == "mode":
                mode = str(body.get("mode", ""))
                if mode not in MODES:
                    return self._json(400, {"error": f"mode must be one of {MODES}"})
                ss.permissions.set_mode(mode)
                ss.push({"kind": "mode", "mode": mode})
                return self._json(200, {"ok": True, "mode": mode})
            if action == "compact":
                if ss.busy:
                    return self._json(409, {"error": "session is busy"})
                with self.app.model_lock:
                    summary = ss.agent.compact()
                ss.push({"kind": "status", "text": "compacted"})
                return self._json(200, {"summary": summary})
        if len(parts) == 3 and parts[1] in ("permissions", "answers"):
            rid = parts[2]
            answer = str(body.get("answer", ""))
            if parts[1] == "permissions" and answer not in ("allow", "deny", "allow_session", "allow_persist"):
                return self._json(400, {"error": "answer must be allow, deny, allow_session or allow_persist"})
            with self.app.lock:
                sessions = list(self.app.sessions.values())
            for ss in sessions:
                if ss.resolve(rid, answer):
                    return self._json(200, {"ok": True})
            return self._json(404, {"error": "no such pending request"})
        return self._json(404, {"error": "not found"})

    def _sse(self, ss: ServerSession, u) -> None:
        q = parse_qs(u.query)
        try:
            since = int((q.get("since") or [self.headers.get("Last-Event-ID") or "0"])[0])
        except ValueError:
            since = 0
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            deadline = time.time() + 55
            while time.time() < deadline:
                events = ss.since(since, timeout=15)
                if not events:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    continue
                for ev in events:
                    since = ev["seq"]
                    payload = json.dumps(ev, ensure_ascii=False)
                    self.wfile.write(f"id: {ev['seq']}\ndata: {payload}\n\n".encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return


# -------------------------------------------------------------- utilities

def token_path(settings: Settings) -> Path:
    return settings.home / "serve-token"


def load_or_create_token(settings: Settings, renew: bool = False) -> str:
    p = token_path(settings)
    if p.exists() and not renew:
        return p.read_text(encoding="utf-8").strip()
    tok = secrets.token_hex(16)
    p.write_text(tok + "\n", encoding="utf-8")
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    return tok


def lan_address() -> str | None:
    """The IPv4 address of the interface with the default route, read from
    /proc/net/route and an ioctl: no socket is connected and no packet is sent,
    so this works under the network guard and the sandbox. None when unknown."""
    try:
        import fcntl
        import struct
        iface = None
        with open("/proc/net/route", encoding="utf-8") as fh:
            for line in fh.read().splitlines()[1:]:
                parts = line.split()
                if len(parts) >= 4 and parts[1] == "00000000" and int(parts[3], 16) & 2:
                    iface = parts[0]
                    break
        if not iface:
            return None
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            packed = fcntl.ioctl(s.fileno(), 0x8915, struct.pack("256s", iface.encode()[:15]))  # SIOCGIFADDR
        finally:
            s.close()
        return socket.inet_ntoa(packed[20:24])
    except (OSError, ValueError, ImportError):
        return None


def ensure_tls(settings: Settings, host: str) -> tuple[Path, Path, Path]:
    """Create a local CA and a server certificate with openssl (one time). Returns (ca, cert, key)."""
    d = settings.home / "tls"
    d.mkdir(parents=True, exist_ok=True)
    ca_key, ca_crt = d / "clara-ca.key", d / "clara-ca.crt"
    key, crt, csr = d / "server.key", d / "server.crt", d / "server.csr"
    def run(cmd):
        try:
            subprocess.run(cmd, check=True, capture_output=True)
        except FileNotFoundError:
            raise SystemExit("openssl is not installed; install it or run without --tls")
        except subprocess.CalledProcessError as exc:
            raise SystemExit(f"openssl failed: {exc.stderr.decode('utf-8', 'replace').strip()[-400:]}")

    if not ca_crt.exists():
        run(["openssl", "req", "-x509", "-newkey", "rsa:3072", "-nodes", "-keyout", str(ca_key),
             "-out", str(ca_crt), "-days", "3650", "-subj", "/CN=Clara local CA"])
    ext = d / "san.cnf"
    san = f"subjectAltName=DNS:localhost,IP:127.0.0.1,IP:{host}\n"
    # The server certificate is re-issued when the address changes (WLAN gave a new IP).
    if not crt.exists() or not ext.exists() or ext.read_text(encoding="utf-8") != san:
        ext.write_text(san, encoding="utf-8")
        run(["openssl", "req", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(csr),
             "-subj", "/CN=clara"])
        run(["openssl", "x509", "-req", "-in", str(csr), "-CA", str(ca_crt), "-CAkey", str(ca_key),
             "-CAcreateserial", "-out", str(crt), "-days", "825", "-extfile", str(ext)])
    return ca_crt, crt, key


def ensure_icons() -> None:
    """Android needs PNG icons for installation; make plain ones once (pure Python)."""
    import struct
    import zlib
    for size in (192, 512):
        out = APP_DIR / f"icon-{size}.png"
        if out.exists():
            continue
        rows = []
        cx = cy = size / 2
        r_outer = size * 0.46
        r_inner = size * 0.30
        for y in range(size):
            row = bytearray([0])
            for x in range(size):
                d = ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5
                if d < r_inner and abs(x - cx) < size * 0.12 and x > cx:
                    row += bytes((20, 24, 32))          # a gap: the letter C
                elif d < r_inner and d > r_inner * 0.6:
                    row += bytes((120, 200, 255))
                elif d < r_outer:
                    row += bytes((20, 24, 32))
                else:
                    row += bytes((20, 24, 32))
            rows.append(bytes(row))
        raw = b"".join(rows)

        def chunk(tag, data):
            return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)) \
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")
        out.write_bytes(png)


# -------------------------------------------------------------- commands

def serve_command(args) -> int:
    settings = load_settings()
    if getattr(args, "url", None):
        settings.base_url = args.url.rstrip("/")
    if getattr(args, "model", None):
        settings.model = args.model
    if args.command == "pair":
        host = args.host or lan_address() or "127.0.0.1"
        port = args.port or settings.serve_port
        token = load_or_create_token(settings, renew=args.new_token)
        scheme = "http"
        if args.tls:
            ca, crt, key = ensure_tls(settings, host)
            scheme = "https"
            print(f"local CA certificate: {ca}")
            print("Install it on the phone once (Android: Settings > Security > Encryption & credentials > Install a certificate > CA certificate).")
        url = f"{scheme}://{host}:{port}/#token={token}"
        print(f"\nOpen this on the phone (same WLAN):\n\n  {url}\n")
        print(f"token file: {token_path(settings)}  (`clara pair --new-token` rotates it)")
        try:
            subprocess.run(["qrencode", "-t", "ANSIUTF8", url], check=True)
        except (OSError, subprocess.CalledProcessError):
            print("(install `qrencode` to get a QR code here)")
        print(f"\nThen start the server: clara serve --lan{' --tls' if args.tls else ''}")
        return 0
    if args.command == "desktop":
        token = load_or_create_token(settings)
        url = f"http://127.0.0.1:{settings.serve_port}/#token={token}"
        try:
            import webview  # type: ignore
        except ImportError:
            print(f"pywebview is not installed (pip install 'clara-agent[desktop]'). Open this in your browser while `clara serve` runs:\n  {url}")
            return 0
        webview.create_window("Clara", url, width=1100, height=800)
        webview.start()
        return 0
    # serve
    host = args.host or ("0.0.0.0" if args.lan else settings.serve_host)
    port = args.port or settings.serve_port
    netguard.install(set(settings.allowed_hosts), settings.audit_log, mode="deny")
    project_root = Path(args.project).resolve() if getattr(args, "project", None) else find_project_root()
    perm_mode = "yolo" if getattr(args, "yolo", False) else (getattr(args, "mode", None) or "default")
    sandbox_mode = getattr(args, "sandbox", None) or settings.sandbox_mode
    # Everything that writes outside the sandbox's folders happens before the lock:
    # token, icons (inside the package folder), certificates.
    token = load_or_create_token(settings)
    ensure_icons()
    tls_files = None
    if args.tls:
        tls_host = lan_address()
        if host == "0.0.0.0" and not tls_host:
            print("! could not determine the WLAN address for the certificate; pass --host ADDRESS", file=sys.stderr)
        tls_files = ensure_tls(settings, tls_host or "127.0.0.1")
    rep = None
    if sandbox_mode == "on" or (sandbox_mode == "auto" and perm_mode == "yolo"):
        from .cli import _server_port
        mport = _server_port(settings.base_url)
        rep = sandbox.apply(sandbox.default_write_paths(project_root, settings.home, settings.extra_write_paths,
                                                        rules_writable=(perm_mode != "yolo")),
                            connect_ports=([mport] if mport else []) + list(settings.extra_connect_ports)
                            + ([_server_port(settings.approver_url)] if settings.approver_url else []), bind_ports=[port],
                            write_files=sandbox.default_write_files(settings.home), read_deny=settings.read_deny,
                        unix_sockets=settings.unix_sockets)
        if not rep.applied:
            print(f"! sandbox requested but not applied: {rep.reason}", file=sys.stderr)
    if getattr(args, "mock", False):
        from .cli import DEMO_SCRIPT
        provider: Provider = MockProvider(list(DEMO_SCRIPT) * 50)
    else:
        provider = LocalOpenAIProvider(settings.base_url, settings.model, timeout=settings.timeout,
                                       temperature=settings.temperature)
        if getattr(args, "wait", 0):
            provider.wait_until_up(args.wait, status_cb=lambda t: print(t, file=sys.stderr))
        if not provider.model:
            try:
                models = provider.models()
                provider.model = models[0] if models else ""
            except ProviderError as exc:
                print(f"! model server not reachable yet ({exc}); the app will show the error until it is", file=sys.stderr)
    app = App(settings, provider, project_root, token, sandbox_report=rep, default_mode=perm_mode)
    Handler.app = app
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    scheme = "http"
    if tls_files is not None:
        ca, crt, key = tls_files
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(crt), str(key))
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
        scheme = "https"
    shown = (lan_address() or "<this computer's WLAN address>") if host == "0.0.0.0" else host
    print(f"Clara {__version__} serving on {scheme}://{shown}:{port}  (project {project_root}, mode {perm_mode}, "
          f"{rep.summary() if rep else 'sandbox off'})")
    print(f"phone: {scheme}://{shown}:{port}/#token={token}")
    if host == "0.0.0.0":
        print("listening on all interfaces: only devices on your network that know the token can use it")
    print("Ctrl+C stops the server")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0
