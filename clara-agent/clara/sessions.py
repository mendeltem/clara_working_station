"""Session transcripts: one JSONL file per session in ~/.clara/sessions.

Append only (the prefix cache on the server likes that too). `clara --no-record`
creates a session with path=None and writes nothing to disk.
"""
from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

CONTEXT_PREFIX = re.compile(r"^\[context:[^\]]*\]\s*")


def title_from(content) -> str:
    text = CONTEXT_PREFIX.sub("", str(content)).replace("\n", " ").strip()
    return text[:80]


@dataclass
class Session:
    id: str
    path: Path | None
    project_root: str
    created: float = field(default_factory=time.time)
    messages: list[dict] = field(default_factory=list)   # OpenAI-format chat messages
    title: str = ""

    @classmethod
    def new(cls, sessions_dir: Path | None, project_root: Path, record: bool = True) -> "Session":
        sid = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        path = (sessions_dir / f"{sid}.jsonl") if (record and sessions_dir is not None) else None
        s = cls(id=sid, path=path, project_root=str(project_root))
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            s._write({"type": "meta", "session": sid, "project": str(project_root), "created": s.created})
        return s

    @classmethod
    def load(cls, path: Path, full: bool = False) -> "Session":
        """09.11.2026 (night check: a resumed work session reloaded 378k tokens instead of ~8k): a compact note
        replaces the messages loaded so far with its summary, as it did in the running session. full=True keeps the
        old behaviour (every raw message), for readers that count positions in the whole file (hippocampus catch-up)."""
        s = cls(id=path.stem, path=path, project_root="")
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = rec.get("type")
            if t == "meta":
                s.project_root = rec.get("project", "")
                s.created = rec.get("created", s.created)
                s.title = rec.get("title", s.title)
            elif t == "note" and rec.get("kind") == "compact" and not full and isinstance(rec.get("messages"), list) \
                    and rec["messages"]:
                s.messages = list(rec["messages"])          # a note without messages falls back to the old behaviour
            elif t == "message":
                s.messages.append(rec["message"])
                if not s.title and rec["message"].get("role") == "user":
                    s.title = title_from(rec["message"].get("content", ""))
        if not full:
            s.messages = close_open_calls(s.messages)
        return s

    def _write(self, rec: dict) -> None:
        if self.path is None:
            return
        rec.setdefault("t", time.time())
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(clean_text(json.dumps(rec, ensure_ascii=False)) + "\n")

    def append(self, message: dict) -> None:
        # 10.11.2026: a job text cut in the middle of a character and handed over as an argument arrives with lone
        # surrogates; writing it ended the round with UnicodeEncodeError before the first request (wk11a round 2)
        if isinstance(message.get("content"), str):
            message["content"] = clean_text(message["content"])
        self.messages.append(message)
        if message.get("role") == "user" and not self.title:
            self.title = title_from(message.get("content", ""))
        self._write({"type": "message", "message": message})

    def note(self, kind: str, **data) -> None:
        self._write({"type": "note", "kind": kind, **data})

    def replace_messages(self, messages: list[dict], reason: str) -> None:
        """Used by /compact. Recorded as a note so the file stays append-only."""
        self.messages = list(messages)
        self._write({"type": "note", "kind": "compact", "reason": reason, "messages": messages})


INTERRUPTED = ("[interrupted: this tool call did not finish (the process was stopped or restarted before the result "
               "came back); run it again if it is still needed]")


def clean_text(text: str) -> str:
    """Text that can be written and sent as UTF-8: lone surrogates (bytes that were not UTF-8) become U+FFFD."""
    try:
        text.encode("utf-8")
        return text
    except UnicodeEncodeError:
        return text.encode("utf-8", "surrogatepass").decode("utf-8", "replace")


def close_open_calls(messages: list[dict]) -> list[dict]:
    """10.11.2026 (stability check): a turn that was killed between a tool call and its result leaves an assistant
    message with tool_calls and no tool message for them; OpenAI-style servers reject the next request. Every such
    call gets a result that says so, right behind the results that did arrive. In memory only (the file stays
    append-only); the same file gives the same list on every load."""
    out, i, n = [], 0, len(messages)
    while i < n:
        m = messages[i]
        out.append(m)
        i += 1
        calls = m.get("tool_calls") if m.get("role") == "assistant" else None
        if not calls:
            continue
        have = set()
        while i < n and messages[i].get("role") == "tool":
            have.add(messages[i].get("tool_call_id"))
            out.append(messages[i])
            i += 1
        for c in calls:
            if isinstance(c, dict) and c.get("id") not in have:
                out.append({"role": "tool", "tool_call_id": c.get("id"),
                            "name": (c.get("function") or {}).get("name", ""), "content": INTERRUPTED})
    return out


def list_sessions(sessions_dir: Path, limit: int = 20) -> list[dict]:
    out = []
    if not sessions_dir.is_dir():
        return out
    for p in sorted(sessions_dir.glob("*.jsonl"), reverse=True)[:limit]:
        try:
            s = Session.load(p)
        except OSError:
            continue
        out.append({"id": s.id, "path": str(p), "project": s.project_root, "title": s.title,
                    "messages": len(s.messages), "created": s.created})
    return out
