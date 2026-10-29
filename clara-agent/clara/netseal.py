"""seccomp layer under Landlock: the locked process tree may only create TCP sockets.

Landlock limits TCP connect/bind by port but does not see UDP, ICMP or Unix sockets. That leaves DNS
(a query to 127.0.0.53 travels on to the network and can carry data in the name) and Unix sockets
(systemd-resolved varlink, the D-Bus/systemd user bus: `systemd-run` starts a process outside every lock).
This filter, applied after Landlock (no root, NO_NEW_PRIVS is already set), refuses with EACCES:
  socket(AF_INET/AF_INET6, anything but SOCK_STREAM)    UDP, ICMP, raw
  socket(AF_UNIX, STREAM/DGRAM)                         resolved, D-Bus, tmux server, systemd, journald
                                                        (SEQPACKET stays: libcuda fails without it; the system
                                                        services an agent could misuse do not listen on it)
  socket(any other family except AF_NETLINK)            packet, bluetooth, vsock ...
  io_uring_setup                                        io_uring can create sockets without socket()
socketpair() stays allowed (pipes between a shell and its children). Non-x86_64 syscall ABIs are refused.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import errno
import platform
import struct

AUDIT_ARCH_X86_64 = 0xC000003E
NR_SOCKET = 41
NR_IO_URING_SETUP = 425
X32_BIT = 0x40000000
AF_UNIX, AF_INET, AF_INET6, AF_NETLINK = 1, 2, 10, 16
SOCK_STREAM, SOCK_SEQPACKET, SOCK_TYPE_MASK = 1, 5, 0xF

SECCOMP_RET_ALLOW = 0x7FFF0000
SECCOMP_RET_ERRNO = 0x00050000
SECCOMP_SET_MODE_FILTER = 1
SYS_SECCOMP = 317
PR_SET_NO_NEW_PRIVS = 38

# BPF opcodes
LD_W_ABS = 0x20       # BPF_LD | BPF_W | BPF_ABS
JEQ_K = 0x15          # BPF_JMP | BPF_JEQ | BPF_K
JSET_K = 0x45         # BPF_JMP | BPF_JSET | BPF_K
AND_K = 0x54          # BPF_ALU | BPF_AND | BPF_K
RET_K = 0x06          # BPF_RET | BPF_K

OFF_NR, OFF_ARCH, OFF_ARG0, OFF_ARG1 = 0, 4, 16, 24   # struct seccomp_data, low 32 bits of args (little endian)


def _ins(code, jt, jf, k):
    return struct.pack("HBBI", code, jt, jf, k & 0xFFFFFFFF)


def program(unix: str = "seqpacket") -> bytes:
    """unix="seqpacket": Unix sockets only of type SEQPACKET (libcuda's MPS probe). unix="allow": all Unix sockets
    (torch DataLoader workers pass file descriptors over Unix stream sockets); UDP/ICMP/raw stay refused either way."""
    deny = SECCOMP_RET_ERRNO | errno.EACCES
    head = [
        _ins(LD_W_ABS, 0, 0, OFF_ARCH),
        _ins(JEQ_K, 1, 0, AUDIT_ARCH_X86_64),
        _ins(RET_K, 0, 0, deny),                         # other syscall ABI
        _ins(LD_W_ABS, 0, 0, OFF_NR),
        _ins(JSET_K, 0, 1, X32_BIT),
        _ins(RET_K, 0, 0, deny),                         # x32 ABI
        _ins(JEQ_K, 0, 1, NR_IO_URING_SETUP),
        _ins(RET_K, 0, 0, deny),
        _ins(JEQ_K, 1, 0, NR_SOCKET),
        _ins(RET_K, 0, 0, SECCOMP_RET_ALLOW),            # every other syscall
        _ins(LD_W_ABS, 0, 0, OFF_ARG0),                  # socket(domain, type, proto)
        _ins(JEQ_K, 0, 1, AF_NETLINK),
        _ins(RET_K, 0, 0, SECCOMP_RET_ALLOW),
    ]
    if unix == "allow":
        uni = [_ins(JEQ_K, 0, 1, AF_UNIX), _ins(RET_K, 0, 0, SECCOMP_RET_ALLOW)]
    else:
        uni = [
            _ins(JEQ_K, 0, 4, AF_UNIX),
            _ins(LD_W_ABS, 0, 0, OFF_ARG1),
            _ins(AND_K, 0, 0, SOCK_TYPE_MASK),
            _ins(JEQ_K, 1, 0, SOCK_SEQPACKET),
            _ins(RET_K, 0, 0, deny),                     # Unix stream/datagram
            _ins(RET_K, 0, 0, SECCOMP_RET_ALLOW),
        ]
        # fix the jump: after AF_UNIX matched we load arg1 and test SEQPACKET; on mismatch of AF_UNIX skip the block
        uni[0] = _ins(JEQ_K, 0, 5, AF_UNIX)
    inet = [
        _ins(LD_W_ABS, 0, 0, OFF_ARG0),
        _ins(JEQ_K, 1, 0, AF_INET),
        _ins(JEQ_K, 0, 4, AF_INET6),
        _ins(LD_W_ABS, 0, 0, OFF_ARG1),
        _ins(AND_K, 0, 0, SOCK_TYPE_MASK),
        _ins(JEQ_K, 0, 1, SOCK_STREAM),
        _ins(RET_K, 0, 0, SECCOMP_RET_ALLOW),            # TCP: Landlock decides the port
        _ins(RET_K, 0, 0, deny),                         # UDP, raw, other families
    ]
    return b"".join(head + uni + inet)


class SockFprog(ctypes.Structure):
    _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.c_void_p)]


def supported() -> bool:
    return platform.system() == "Linux" and platform.machine() == "x86_64"


def apply(unix: str = "seqpacket") -> tuple[bool, str]:
    """Install the filter for this process and all children. Returns (applied, reason)."""
    if not supported():
        return False, f"seccomp layer only built for Linux x86_64, not {platform.system()} {platform.machine()}"
    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        return False, f"prctl(NO_NEW_PRIVS): {errno.errorcode.get(ctypes.get_errno())}"
    code = program(unix)
    buf = ctypes.create_string_buffer(code, len(code))
    prog = SockFprog(len(code) // 8, ctypes.cast(buf, ctypes.c_void_p))
    rc = libc.syscall(SYS_SECCOMP, SECCOMP_SET_MODE_FILTER, 0, ctypes.byref(prog))
    if rc != 0:
        return False, f"seccomp: {errno.errorcode.get(ctypes.get_errno())}"
    if unix == "allow":
        return True, "TCP and Unix sockets only (UDP, ICMP, raw and io_uring refused; Unix left open for training)"
    return True, "only TCP sockets (UDP, ICMP, raw, Unix stream/datagram sockets and io_uring refused)"
