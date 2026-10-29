"""Landlock sandbox: kernel-enforced write lock and TCP lock, no root needed.

Applied once at startup to Clara's own process. Every child process (the bash
tool, scripts the model runs, subagents) inherits it and cannot lift it.

  - Filesystem: writing, creating, deleting and renaming are only possible
    beneath the listed folders (project, ~/.clara, /tmp). Reading and executing
    stay allowed everywhere. /dev and /proc keep file-level write access so
    /dev/null, terminals and GPU nodes keep working, but nothing can be created
    there.
  - Network: TCP connect() is only possible to the listed ports (the model
    server), TCP bind() only to the listed ports (the phone app). Everything
    else fails with EACCES, also inside `curl`, `pip`, `git`, `ssh` started by
    the model. Landlock does not mediate UDP or ICMP; for those, use the
    firewall rule in scripts/nft-clara-egress.sh or watch with Wireshark.

Requires Linux with Landlock (kernel 5.13 for files, 6.7 (ABI 4) for TCP).
Pure ctypes, no packages. Based on the launcher described in the user's wiki
(landlock-write-lock-for-a-yolo-agent) and the kernel documentation
https://docs.kernel.org/userspace-api/landlock.html.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

SYS_LANDLOCK_CREATE_RULESET = 444
SYS_LANDLOCK_ADD_RULE = 445
SYS_LANDLOCK_RESTRICT_SELF = 446
PR_SET_NO_NEW_PRIVS = 38

LANDLOCK_CREATE_RULESET_VERSION = 1 << 0
LANDLOCK_RULE_PATH_BENEATH = 1
LANDLOCK_RULE_NET_PORT = 2

FS_EXECUTE = 1 << 0
FS_WRITE_FILE = 1 << 1
FS_READ_FILE = 1 << 2
FS_READ_DIR = 1 << 3
FS_REMOVE_DIR = 1 << 4
FS_REMOVE_FILE = 1 << 5
FS_MAKE_CHAR = 1 << 6
FS_MAKE_DIR = 1 << 7
FS_MAKE_REG = 1 << 8
FS_MAKE_SOCK = 1 << 9
FS_MAKE_FIFO = 1 << 10
FS_MAKE_BLOCK = 1 << 11
FS_MAKE_SYM = 1 << 12
FS_REFER = 1 << 13        # ABI 2
FS_TRUNCATE = 1 << 14     # ABI 3
FS_IOCTL_DEV = 1 << 15    # ABI 5

NET_BIND_TCP = 1 << 0     # ABI 4
NET_CONNECT_TCP = 1 << 1  # ABI 4

O_PATH = 0o10000000
O_CLOEXEC = 0o2000000
O_DIRECTORY = 0o200000


class RulesetAttr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64),
                ("handled_access_net", ctypes.c_uint64),
                ("scoped", ctypes.c_uint64)]


class PathBeneathAttr(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


class NetPortAttr(ctypes.Structure):
    _fields_ = [("allowed_access", ctypes.c_uint64), ("port", ctypes.c_uint64)]


@dataclass
class SandboxReport:
    supported: bool = False
    abi: int = 0
    applied: bool = False
    fs_locked: bool = False
    net_locked: bool = False
    write_paths: list[str] = field(default_factory=list)
    write_files: list[str] = field(default_factory=list)
    read_denied: list[str] = field(default_factory=list)
    file_only_paths: list[str] = field(default_factory=list)
    connect_ports: list[int] = field(default_factory=list)
    bind_ports: list[int] = field(default_factory=list)
    socket_sealed: bool = False
    seal_reason: str = ""
    missing_paths: list[str] = field(default_factory=list)
    reason: str = ""

    def summary(self) -> str:
        if not self.applied:
            return f"sandbox off ({self.reason or 'not applied'})"
        parts = [f"Landlock ABI {self.abi}"]
        parts.append("writes limited to " + ", ".join(self.write_paths) + (" + " + ", ".join(self.write_files) if self.write_files else ""))
        if self.read_denied:
            parts.append("reads denied under " + ", ".join(self.read_denied))
        if self.net_locked:
            parts.append(f"TCP connect only to ports {self.connect_ports or '[]'}, bind only to {self.bind_ports or '[]'}")
        else:
            parts.append("TCP not restricted (kernel ABI < 4)")
        if self.net_locked:
            parts.append("sockets: " + (self.seal_reason if self.socket_sealed else f"UDP/Unix NOT sealed ({self.seal_reason})"))
        return "; ".join(parts)


def _libc():
    libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
    libc.syscall.restype = ctypes.c_long
    libc.prctl.restype = ctypes.c_int
    return libc


def abi_version() -> int:
    """0 means Landlock is unavailable (not Linux, old kernel, disabled, or seccomp-blocked)."""
    if sys.platform != "linux":
        return 0
    try:
        libc = _libc()
        res = libc.syscall(SYS_LANDLOCK_CREATE_RULESET, None, 0, LANDLOCK_CREATE_RULESET_VERSION)
        if res < 0:
            return 0
        return int(res)
    except OSError:
        return 0


def _write_rights(abi: int) -> int:
    rights = (FS_WRITE_FILE | FS_REMOVE_DIR | FS_REMOVE_FILE | FS_MAKE_CHAR | FS_MAKE_DIR
              | FS_MAKE_REG | FS_MAKE_SOCK | FS_MAKE_FIFO | FS_MAKE_BLOCK | FS_MAKE_SYM)
    if abi >= 2:
        rights |= FS_REFER
    if abi >= 3:
        rights |= FS_TRUNCATE
    return rights


def _add_path_rule(libc, ruleset_fd: int, path: str, access: int, directory: bool) -> None:
    flags = O_PATH | O_CLOEXEC | (O_DIRECTORY if directory else 0)
    fd = os.open(path, flags)
    try:
        attr = PathBeneathAttr(allowed_access=access, parent_fd=fd)
        res = libc.syscall(SYS_LANDLOCK_ADD_RULE, ruleset_fd, LANDLOCK_RULE_PATH_BENEATH, ctypes.byref(attr), 0)
        if res != 0:
            err = ctypes.get_errno()
            raise OSError(err, f"landlock_add_rule({path}): {os.strerror(err)}")
    finally:
        os.close(fd)


def _add_port_rule(libc, ruleset_fd: int, port: int, access: int) -> None:
    attr = NetPortAttr(allowed_access=access, port=port)
    res = libc.syscall(SYS_LANDLOCK_ADD_RULE, ruleset_fd, LANDLOCK_RULE_NET_PORT, ctypes.byref(attr), 0)
    if res != 0:
        err = ctypes.get_errno()
        raise OSError(err, f"landlock_add_rule(port {port}): {os.strerror(err)}")


def read_grant_rules(denied: list[str]) -> list[tuple[str, bool]]:
    """Landlock can only grant, never carve out. To deny reading D, grant reading to
    everything that is not D and not on the way to D: every entry of every ancestor
    folder of D, except the entries that are themselves ancestors of a denied path.
    Returns (path, is_dir) pairs. Files that appear later in an ancestor folder are
    unreadable until the next start; everything beneath a granted entry is readable."""
    denied_res = []
    for d in denied:
        try:
            denied_res.append(os.path.realpath(os.path.expanduser(d)))
        except OSError:
            continue
    blocked: set[str] = set(denied_res)
    for d in denied_res:
        parent = os.path.dirname(d)
        while True:
            blocked.add(parent)   # ancestors: never granted as a whole
            if parent == "/":
                break
            parent = os.path.dirname(parent)
    grants: list[tuple[str, bool]] = []
    seen: set[str] = set()
    ancestors = sorted({a for a in blocked if a not in denied_res})
    for anc in ancestors:
        try:
            entries = sorted(os.listdir(anc))
        except OSError:
            continue
        for name in entries:
            full = os.path.join(anc, name)
            if full in blocked or full in seen:
                continue
            seen.add(full)
            grants.append((full, os.path.isdir(full)))
    return grants


def apply(write_paths, connect_ports=(), bind_ports=(), file_only_paths=("/dev", "/proc"),
          restrict_net: bool = True, write_files=(), read_deny=(), unix_sockets: str = "seqpacket") -> SandboxReport:
    """Restrict the calling thread (and everything it starts from now on).

    Call this from the main thread before any other thread exists. Raises
    nothing: the report says what was and was not applied, so the caller can
    print it (no silent fallback).
    """
    rep = SandboxReport(connect_ports=sorted({int(p) for p in connect_ports}),
                        bind_ports=sorted({int(p) for p in bind_ports}))
    abi = abi_version()
    rep.abi = abi
    if abi < 1:
        rep.reason = "Landlock unavailable on this kernel or blocked for this process"
        return rep
    rep.supported = True
    libc = _libc()
    fs_rights = _write_rights(abi)
    read_rights = FS_READ_FILE | FS_READ_DIR
    read_grants = read_grant_rules(list(read_deny)) if read_deny else []
    handled = fs_rights | (read_rights if read_deny else 0)
    attr = RulesetAttr(handled_access_fs=handled, handled_access_net=0, scoped=0)
    want_net = restrict_net and abi >= 4
    if want_net:
        attr.handled_access_net = NET_BIND_TCP | NET_CONNECT_TCP
    # struct landlock_ruleset_attr grew with the ABI: 8 bytes (handled_access_fs, ABI 1-3),
    # 16 (+ handled_access_net, ABI 4), 24 (+ scoped, ABI 6). The kernel reads at most the
    # size it knows and rejects a larger one with E2BIG, so the size must match the ABI.
    size = 8 if abi < 4 else (16 if abi < 6 else 24)
    assert size <= ctypes.sizeof(attr)
    ruleset_fd = libc.syscall(SYS_LANDLOCK_CREATE_RULESET, ctypes.byref(attr), size, 0)
    if ruleset_fd < 0:
        err = ctypes.get_errno()
        rep.reason = f"landlock_create_ruleset failed: {os.strerror(err)}"
        return rep
    try:
        dir_write = fs_rights | (read_rights if read_deny else 0)
        for p in write_paths:
            p = str(Path(p).expanduser())
            if not os.path.isdir(p):
                rep.missing_paths.append(p)
                continue
            _add_path_rule(libc, ruleset_fd, p, dir_write, directory=True)
            rep.write_paths.append(p)
        file_rights = FS_WRITE_FILE | (FS_TRUNCATE if abi >= 3 else 0)
        if read_deny:
            for path, is_dir in read_grants:
                try:
                    _add_path_rule(libc, ruleset_fd, path, read_rights if is_dir else FS_READ_FILE, directory=is_dir)
                except OSError:
                    continue   # vanished or unreadable entry: it simply stays unreadable
            rep.read_denied = [os.path.realpath(os.path.expanduser(d)) for d in read_deny]
            file_rights |= FS_READ_FILE
        for p in file_only_paths:
            if os.path.isdir(p):
                _add_path_rule(libc, ruleset_fd, p, file_rights, directory=True)
                rep.file_only_paths.append(p)
        for p in write_files:
            p = str(Path(p).expanduser())
            if not os.path.isfile(p):
                rep.missing_paths.append(p)
                continue
            _add_path_rule(libc, ruleset_fd, p, file_rights, directory=False)
            rep.write_files.append(p)
        if want_net:
            for port in rep.connect_ports:
                _add_port_rule(libc, ruleset_fd, port, NET_CONNECT_TCP)
            for port in rep.bind_ports:
                _add_port_rule(libc, ruleset_fd, port, NET_BIND_TCP)
        if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            err = ctypes.get_errno()
            rep.reason = f"prctl(PR_SET_NO_NEW_PRIVS) failed: {os.strerror(err)}"
            return rep
        if libc.syscall(SYS_LANDLOCK_RESTRICT_SELF, ruleset_fd, 0) != 0:
            err = ctypes.get_errno()
            rep.reason = f"landlock_restrict_self failed: {os.strerror(err)}"
            return rep
    except OSError as exc:
        rep.reason = str(exc)
        return rep
    finally:
        os.close(ruleset_fd)
    rep.applied = True
    rep.fs_locked = True
    rep.net_locked = want_net
    if want_net:
        # Landlock does not see UDP, ICMP or Unix sockets (DNS names can carry data; the systemd user bus can start
        # a process outside every lock): a seccomp filter leaves only TCP sockets. See clara/netseal.py.
        from . import netseal
        rep.socket_sealed, rep.seal_reason = netseal.apply(unix_sockets)
    return rep


def default_write_paths(project_root: Path, home: Path, extra=(), rules_writable: bool = True) -> list[str]:
    """Folders the locked process may write to. Not the whole of ~/.clara: config.toml,
    skills, the token and the certificates stay read-only, so the model cannot change
    Clara's own settings. The rules folder is writable only when a user can be asked
    (not under yolo), so that "save as rule" works but the model cannot grant itself rules."""
    home = home.resolve()
    paths = [str(project_root.resolve()), str(home / "sessions"), str(home / "backups"), str(home / "state"),
             str(home / "schedule-runs"), str(home / "netaudit-runs"), "/tmp", "/var/tmp", "/dev/shm"]
    if rules_writable:
        paths.append(str(home / "projects"))
    paths += [str(Path(p).expanduser().resolve()) for p in extra]
    out: list[str] = []
    for p in paths:
        if p not in out:
            out.append(p)
    return out


def default_write_files(home: Path) -> list[str]:
    """Single files under ~/.clara that stay writable: memory notes and the audit log."""
    home = home.resolve()
    return [str(home / "memory.md"), str(home / "netaudit.log")]
