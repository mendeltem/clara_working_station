"""In-process network allowlist with an audit log.

Every socket connect, UDP send and name resolution that Clara's own process
attempts goes through here. Anything whose target is not loopback or in the
configured allowlist is refused BEFORE a packet leaves, and every attempt,
allowed or refused, is appended to ~/.clara/netaudit.log as one JSON line.

This is the first of three layers:
  1. this module (Python level, covers Clara itself and anything it imports),
  2. clara.sandbox (Landlock, kernel level, also covers child processes such
     as commands the model runs through the bash tool),
  3. your own capture (scripts/netaudit.sh, Wireshark) which trusts neither.

The guard cannot be removed by the model: it is installed at startup from the
command line, there is no tool that reaches it, and the sandbox makes the
process tree unable to open TCP connections to other ports anyway.
"""
from __future__ import annotations

import ipaddress
import json
import os
import socket
import threading
import time
import traceback
from pathlib import Path
from typing import Callable

Asker = Callable[[str, int, str], bool]


class NetworkBlocked(OSError):
    """Raised instead of opening a connection that policy does not allow."""


_lock = threading.Lock()
_state: dict = {
    "installed": False,
    "allowed_hosts": set(),
    "mode": "deny",          # "deny" | "ask"
    "asker": None,
    "audit_path": None,
    "counts": {"allowed": 0, "blocked": 0, "asked": 0},
}
_orig = {}


def _normalize(host: str) -> str:
    return host.strip().lower().split("%")[0]


def is_allowed(host: str) -> bool:
    """Loopback and configured hosts are allowed. Empty host means local bind."""
    if host in ("", None):
        return True
    h = _normalize(str(host))
    if h in ("localhost", "localhost.localdomain"):
        return True
    try:
        ip = ipaddress.ip_address(h)
        if ip.is_loopback:
            return True
        if h in _state["allowed_hosts"]:
            return True
        if ip.version == 6 and ip.ipv4_mapped is not None:
            return str(ip.ipv4_mapped) in _state["allowed_hosts"] or ip.ipv4_mapped.is_loopback
        return False
    except ValueError:
        return h in _state["allowed_hosts"]


def _caller() -> str:
    skip = ("netguard.py", "socket.py", "ssl.py", "http/client.py", "urllib/request.py",
            "urllib3", "threading.py")
    for frame in reversed(traceback.extract_stack()[:-2]):
        fn = frame.filename.replace("\\", "/")
        if not any(fn.endswith(s) or s in fn for s in skip):
            return f"{os.path.basename(fn)}:{frame.lineno} {frame.name}"
    return "?"


def _record(event: str, host: str, port, decision: str) -> None:
    line = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
        "pid": os.getpid(),
        "event": event,
        "host": str(host),
        "port": port,
        "decision": decision,
        "where": _caller(),
    }
    with _lock:
        key = "blocked" if decision.endswith("deny") else "allowed"
        _state["counts"][key] += 1
        if decision.startswith("ask"):
            _state["counts"]["asked"] += 1
        path = _state["audit_path"]
    if path:
        try:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(line, ensure_ascii=False) + "\n")
        except OSError:
            pass  # the audit log is best effort; the decision itself is not


def _decide(event: str, host: str, port) -> None:
    if is_allowed(host):
        _record(event, host, port, "allow")
        return
    asker = _state["asker"]
    if _state["mode"] == "ask" and asker is not None:
        if asker(str(host), int(port or 0), event):
            _record(event, host, port, "ask-allow")
            return
        _record(event, host, port, "ask-deny")
    else:
        _record(event, host, port, "deny")
    raise NetworkBlocked(f"clara netguard: {event} to {host}:{port} is not allowed (offline policy)")


def _addr_parts(address):
    if isinstance(address, (str, bytes)):
        return None, None          # AF_UNIX: local by definition
    if isinstance(address, tuple) and len(address) >= 2:
        return address[0], address[1]
    return None, None


def _guarded_connect(self, address):
    host, port = _addr_parts(address)
    if host is not None:
        _decide("connect", host, port)
    return _orig["connect"](self, address)


def _guarded_connect_ex(self, address):
    host, port = _addr_parts(address)
    if host is not None:
        _decide("connect", host, port)
    return _orig["connect_ex"](self, address)


def _guarded_sendto(self, data, *args):
    address = args[-1] if args else None
    host, port = _addr_parts(address)
    if host is not None:
        _decide("sendto", host, port)
    return _orig["sendto"](self, data, *args)


def _guarded_sendmsg(self, buffers, *args):
    # sendmsg(buffers[, ancdata[, flags[, address]]]): only an explicit address matters here
    address = args[2] if len(args) >= 3 else None
    host, port = _addr_parts(address)
    if host is not None:
        _decide("sendmsg", host, port)
    return _orig["sendmsg"](self, buffers, *args)


def _guarded_resolver(name):
    orig = _orig[name]

    def guarded(host, *args, **kwargs):
        if host is not None and not is_allowed(str(host)):
            _decide("resolve", str(host), None)
        return orig(host, *args, **kwargs)
    return guarded


def _guarded_bind(self, address):
    host, port = _addr_parts(address)
    _record("bind", host if host is not None else "", port, "allow")
    return _orig["bind"](self, address)


def _guarded_getaddrinfo(host, port, *args, **kwargs):
    if host is not None and not is_allowed(str(host)):
        # Resolving a name is itself a packet (DNS). Refuse before it happens.
        _decide("resolve", str(host), port)
    return _orig["getaddrinfo"](host, port, *args, **kwargs)


def install(allowed_hosts, audit_path: Path | str | None, mode: str = "deny", asker: Asker | None = None) -> None:
    """Install the guard. Safe to call twice; the second call updates policy."""
    with _lock:
        _state["allowed_hosts"] = {_normalize(h) for h in allowed_hosts}
        _state["mode"] = mode
        _state["asker"] = asker
        _state["audit_path"] = str(audit_path) if audit_path else None
        if _state["installed"]:
            return
        _orig["connect"] = socket.socket.connect
        _orig["connect_ex"] = socket.socket.connect_ex
        _orig["sendto"] = socket.socket.sendto
        _orig["sendmsg"] = socket.socket.sendmsg
        _orig["bind"] = socket.socket.bind
        _orig["getaddrinfo"] = socket.getaddrinfo
        for name in ("gethostbyname", "gethostbyname_ex", "gethostbyaddr"):
            _orig[name] = getattr(socket, name)
        socket.socket.connect = _guarded_connect
        socket.socket.connect_ex = _guarded_connect_ex
        socket.socket.sendto = _guarded_sendto
        socket.socket.sendmsg = _guarded_sendmsg
        socket.socket.bind = _guarded_bind
        socket.getaddrinfo = _guarded_getaddrinfo
        for name in ("gethostbyname", "gethostbyname_ex", "gethostbyaddr"):
            setattr(socket, name, _guarded_resolver(name))
        _state["installed"] = True


def uninstall() -> None:
    """Only used by tests."""
    with _lock:
        if not _state["installed"]:
            return
        socket.socket.connect = _orig["connect"]
        socket.socket.connect_ex = _orig["connect_ex"]
        socket.socket.sendto = _orig["sendto"]
        socket.socket.sendmsg = _orig["sendmsg"]
        socket.socket.bind = _orig["bind"]
        socket.getaddrinfo = _orig["getaddrinfo"]
        for name in ("gethostbyname", "gethostbyname_ex", "gethostbyaddr"):
            setattr(socket, name, _orig[name])
        _state["installed"] = False


def is_installed() -> bool:
    return bool(_state["installed"])


def counts() -> dict:
    with _lock:
        return dict(_state["counts"])


def allowed_hosts() -> set[str]:
    return set(_state["allowed_hosts"])


def read_audit(path: Path | str, last: int = 50) -> list[dict]:
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    out = []
    for ln in lines[-last:]:
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            continue
    return out
