"""Optional local approval service, asked after the rules and the mode and before the user.

Speaks the Hermes smart-approval format (one <command> block in a chat completion, reply APPROVE, DENY or ESCALATE),
so the same local gate serves Hermes and Clara. Anything unexpected counts as ESCALATE: the user decides.
Only loopback URLs are accepted; the network guard would refuse anything else anyway.
"""
from __future__ import annotations

import json
import urllib.request
from urllib.parse import urlparse

VERDICTS = ("APPROVE", "DENY", "ESCALATE")


def command_text(req) -> str:
    if req.tool == "bash":
        return req.target or req.summary
    return f"{req.tool} {req.target}".strip() or req.summary


def make_approver(url: str, timeout: float = 90.0):
    host = (urlparse(url).hostname or "").lower()
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError(f"approver_url must be loopback, got {host!r}")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def approve(req, project_root=None) -> str:
        where = f"inside its own project folder {project_root}" if project_root else "on the user's computer"
        warn = req.warnings()
        flags = (f"{req.tool} run by a coding agent {where}; "
                 + ("warnings: " + "; ".join(warn) if warn else "no dangerous pattern matched"))
        msg = f"<command>\n{command_text(req)}\n</command>\nflagged as: {flags}"
        body = json.dumps({"model": "approval", "messages": [{"role": "user", "content": msg}], "max_tokens": 8}).encode()
        try:
            r = urllib.request.Request(url, body, {"Content-Type": "application/json"}, method="POST")
            with opener.open(r, timeout=timeout) as resp:
                word = (json.load(resp)["choices"][0]["message"].get("content") or "").strip().upper()
        except Exception:  # noqa: BLE001 -- gate down: the user decides
            return "ESCALATE"
        return next((v for v in VERDICTS if word.startswith(v)), "ESCALATE")

    return approve
