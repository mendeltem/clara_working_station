from __future__ import annotations

import os
import re
import signal
import subprocess
import sys

from .base import Tool, ToolContext, ToolResult, clip

# Commands that normally need the network. They are named in the permission
# prompt; with the Landlock sandbox on they fail at the kernel anyway.
NETWORK_COMMANDS = re.compile(
    r"(^|[\s;&|(])(curl|wget|pip3?|pipx|uv|npm|npx|pnpm|yarn|conda|mamba|apt(-get)?|snap|"
    r"ssh|scp|sftp|rsync|nc|ncat|netcat|telnet|ftp|docker|podman|ollama|gh|huggingface-cli|hf|"
    r"git\s+(clone|fetch|pull|push|ls-remote|submodule)|wget2|aria2c|nmap|ping|dig|nslookup|host)\b")


def looks_networked(command: str) -> bool:
    return bool(NETWORK_COMMANDS.search(command))


class Bash(Tool):
    name = "bash"
    description = "Run a shell command in the project folder and return its output and exit code."
    parameters = {"type": "object",
                  "properties": {"command": {"type": "string"},
                                 "timeout": {"type": "integer", "description": "seconds, default 120"}},
                  "required": ["command"]}
    kind = "exec"

    def summary(self, args: dict) -> str:
        cmd = str(args.get("command", "")).replace("\n", "; ")
        return f"bash({cmd[:160]})"

    def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        command = str(args.get("command", "")).strip()
        if not command:
            return ToolResult("empty command", is_error=True)
        timeout = int(args.get("timeout") or ctx.shell_timeout)
        env = dict(os.environ)
        env["CLARA"] = "1"
        env["PYTHONUNBUFFERED"] = "1"
        env.setdefault("GIT_TERMINAL_PROMPT", "0")
        if sys.platform == "win32":
            argv = ["powershell", "-NoProfile", "-NonInteractive", "-Command", command]
            kwargs = {}
        else:
            argv = ["bash", "-c", command]
            kwargs = {"start_new_session": True}
        try:
            # stdin from /dev/null: a command that waits for input must fail, not hang.
            proc = subprocess.Popen(argv, cwd=str(ctx.project_root), env=env,
                                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, **kwargs)
        except OSError as exc:
            return ToolResult(f"cannot start shell: {exc}", is_error=True)
        try:
            out, _ = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            if sys.platform != "win32":
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    pass
            proc.kill()
            out, _ = proc.communicate()
            text = out.decode("utf-8", "replace")
            return ToolResult(clip(text) + f"\n[killed after {timeout} s timeout]", is_error=True)
        text = out.decode("utf-8", "replace")
        code = proc.returncode
        body = clip(text.rstrip()) if text.strip() else "(no output)"
        return ToolResult(f"{body}\n[exit code {code}]", is_error=code != 0, data={"exit_code": code})
