"""`clara sandbox-check`: prove on this machine that the Landlock lock holds.

Starts a child Python process that applies the sandbox with one allowed folder
and one allowed TCP port, then tries to break out. Each check prints its own
result; nothing is counted as passed because something else passed.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading

CHILD = r"""
import json, os, socket, subprocess, sys
from clara import sandbox
allowed_dir, forbidden_dir, ok_port, bad_port, home, secret_dir = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4]), sys.argv[5], sys.argv[6]
rep = sandbox.apply(sandbox.default_write_paths(__import__("pathlib").Path(allowed_dir), __import__("pathlib").Path(home), rules_writable=False),
                    connect_ports=[ok_port], bind_ports=[], write_files=sandbox.default_write_files(__import__("pathlib").Path(home)),
                    read_deny=[secret_dir])
results = {"abi": rep.abi, "applied": rep.applied, "net_locked": rep.net_locked, "reason": rep.reason, "checks": {}}
def check(name, fn, expect_ok):
    try:
        fn(); outcome = "ok"
    except PermissionError:
        outcome = "denied"
    except subprocess.CalledProcessError as e:
        err = (e.stderr or b"").decode("utf-8", "replace")
        outcome = "denied" if ("Permission denied" in err or "Operation not permitted" in err) else f"error:{err.strip()[:120]}"
    except OSError as e:
        outcome = "denied" if e.errno in (1, 13) else f"error:{e}"
    results["checks"][name] = {"outcome": outcome, "passed": (outcome == "ok") == expect_ok}
if rep.applied:
    check("write inside allowed folder", lambda: open(os.path.join(allowed_dir, "a.txt"), "w").write("x"), True)
    check("create file in forbidden folder", lambda: open(os.path.join(forbidden_dir, "b.txt"), "w").write("x"), False)
    check("delete decoy in forbidden folder", lambda: os.remove(os.path.join(forbidden_dir, "decoy.txt")), False)
    check("read decoy in forbidden folder", lambda: open(os.path.join(forbidden_dir, "decoy.txt")).read(), True)
    check("child shell writes in forbidden folder", lambda: subprocess.run(
        ["bash", "-c", f"echo x > {forbidden_dir}/c.txt"], check=True, capture_output=True), False)
    check("write to /dev/null", lambda: open("/dev/null", "w").write("x"), True)
    check("append a memory note in ~/.clara", lambda: open(os.path.join(home, "memory.md"), "a").write("- note\n"), True)
    check("write a session file in ~/.clara/sessions", lambda: open(os.path.join(home, "sessions", "s.jsonl"), "w").write("x"), True)
    check("overwrite ~/.clara/config.toml", lambda: open(os.path.join(home, "config.toml"), "w").write("x"), False)
    check("plant a skill in ~/.clara/skills", lambda: os.makedirs(os.path.join(home, "skills", "evil")), False)
    check("add a rule file in ~/.clara/projects (yolo)", lambda: open(os.path.join(home, "projects", "x.json"), "w").write("{}"), False)
    check("replace the serve token", lambda: open(os.path.join(home, "serve-token"), "w").write("x"), False)
    check("read a file in the read-denied folder", lambda: open(os.path.join(secret_dir, "secret.txt")).read(), False)
    check("list the read-denied folder", lambda: os.listdir(secret_dir), False)
    check("child shell cats the read-denied file", lambda: subprocess.run(
        ["bash", "-c", f"cat {secret_dir}/secret.txt"], check=True, capture_output=True), False)
    check("read through a symlink into the read-denied folder", lambda: open(os.path.join(allowed_dir, "link", "secret.txt")).read(), False)
    check("read /etc/hostname still works", lambda: open("/etc/hostname").read() if os.path.exists("/etc/hostname") else None, True)
    check("python still runs in a child shell", lambda: subprocess.run(
        ["bash", "-c", f"{sys.executable} -c 'import json; print(1)'"], check=True, capture_output=True), True)
    check("read the project folder still works", lambda: open(os.path.join(allowed_dir, "a.txt")).read(), True)
    if rep.net_locked:
        def connect(port):
            s = socket.create_connection(("127.0.0.1", port), timeout=3); s.close()
        check("tcp connect to allowed port", lambda: connect(ok_port), True)
        check("tcp connect to other port", lambda: connect(bad_port), False)
        check("child curl to other port", lambda: subprocess.run(
            ["bash", "-c", f"exec 3<>/dev/tcp/127.0.0.1/{bad_port}"], check=True, capture_output=True), False)
        results["socket_sealed"] = rep.socket_sealed
        def udp():
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.sendto(b"x", ("127.0.0.53", 53)); s.close()
        def unix():
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); s.close()
        check("udp to the DNS stub (name lookups can carry data)", udp, False)
        check("unix socket (systemd/D-Bus/resolved)", unix, False)
        check("child dig/getent of an outside name", lambda: subprocess.run(
            ["bash", "-c", f"{sys.executable} -c 'import socket; socket.getaddrinfo(\"example.org\", 443)'"],
            check=True, capture_output=True), False)
        check("child systemd-run --user (escapes every lock)", lambda: subprocess.run(
            ["systemd-run", "--user", "--quiet", "true"],
            check=True, capture_output=True), False)
        check("socketpair for pipes still works", lambda: socket.socketpair()[0].close(), True)
print(json.dumps(results))
"""


def _listener():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    s.listen(16)

    def accept_loop():
        while True:
            try:
                conn, _ = s.accept()
                conn.close()
            except OSError:
                return

    t = threading.Thread(target=accept_loop, daemon=True)
    t.start()
    return s, s.getsockname()[1]


def run_check(quiet: bool = False) -> int:
    # Not under /tmp: /tmp is writable by design, the check needs a folder that is not.
    base = tempfile.mkdtemp(prefix=".clara-sandbox-check-", dir=os.path.expanduser("~"))
    allowed = os.path.join(base, "allowed")
    forbidden = os.path.join(base, "forbidden")
    os.makedirs(allowed)
    os.makedirs(forbidden)
    with open(os.path.join(forbidden, "decoy.txt"), "w") as fh:
        fh.write("decoy")
    secret = os.path.join(base, "secret")
    os.makedirs(secret)
    with open(os.path.join(secret, "secret.txt"), "w") as fh:
        fh.write("secret")
    os.symlink(secret, os.path.join(allowed, "link"))
    l1, ok_port = _listener()
    l2, bad_port = _listener()
    from .config import ensure_home
    home = os.path.join(base, "home")
    ensure_home(__import__("pathlib").Path(home))
    with open(os.path.join(home, "serve-token"), "w") as fh:
        fh.write("token\n")
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in [os.path.dirname(os.path.dirname(__file__)), env.get("PYTHONPATH", "")] if p)
    proc = subprocess.run([sys.executable, "-c", CHILD, allowed, forbidden, str(ok_port), str(bad_port), home, secret],
                          capture_output=True, text=True, env=env, timeout=60)
    l1.close()
    l2.close()
    shutil.rmtree(base, ignore_errors=True)
    if proc.returncode != 0:
        print(proc.stderr.strip() or proc.stdout.strip())
        return 1
    try:
        res = json.loads(proc.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        print(proc.stdout, proc.stderr)
        return 1
    if not res["applied"]:
        print(f"sandbox not applied: {res['reason']} (Landlock ABI {res['abi']})")
        return 1
    failed = 0
    if not quiet:
        print(f"Landlock ABI {res['abi']}, TCP lock {'on' if res['net_locked'] else 'off (ABI < 4)'}")
    for name, r in res["checks"].items():
        mark = "pass" if r["passed"] else "FAIL"
        failed += 0 if r["passed"] else 1
        if not quiet:
            print(f"  {mark}  {name}: {r['outcome']}")
    if not quiet:
        print(f"{len(res['checks']) - failed} of {len(res['checks'])} checks passed")
    return 0 if failed == 0 else 1


def check_results() -> dict:
    """Same check as run_check, returned as data (for tests)."""
    # Not under /tmp: /tmp is writable by design, the check needs a folder that is not.
    base = tempfile.mkdtemp(prefix=".clara-sandbox-check-", dir=os.path.expanduser("~"))
    allowed = os.path.join(base, "allowed")
    forbidden = os.path.join(base, "forbidden")
    os.makedirs(allowed)
    os.makedirs(forbidden)
    with open(os.path.join(forbidden, "decoy.txt"), "w") as fh:
        fh.write("decoy")
    secret = os.path.join(base, "secret")
    os.makedirs(secret)
    with open(os.path.join(secret, "secret.txt"), "w") as fh:
        fh.write("secret")
    os.symlink(secret, os.path.join(allowed, "link"))
    l1, ok_port = _listener()
    l2, bad_port = _listener()
    from .config import ensure_home
    home = os.path.join(base, "home")
    ensure_home(__import__("pathlib").Path(home))
    with open(os.path.join(home, "serve-token"), "w") as fh:
        fh.write("token\n")
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in [os.path.dirname(os.path.dirname(__file__)), env.get("PYTHONPATH", "")] if p)
    proc = subprocess.run([sys.executable, "-c", CHILD, allowed, forbidden, str(ok_port), str(bad_port), home, secret],
                          capture_output=True, text=True, env=env, timeout=60)
    l1.close()
    l2.close()
    shutil.rmtree(base, ignore_errors=True)
    if proc.returncode != 0:
        return {"applied": False, "reason": proc.stderr.strip()[:500], "checks": {}}
    return json.loads(proc.stdout.strip().splitlines()[-1])
