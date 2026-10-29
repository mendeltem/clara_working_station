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
    def load(cls, path: Path) -> "Session":
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
            elif t == "message":
                s.messages.append(rec["message"])
                if not s.title and rec["message"].get("role") == "user":
                    s.title = title_from(rec["message"].get("content", ""))
        return s

    def _write(self, rec: dict) -> None:
        if self.path is None:
            return
        rec.setdefault("t", time.time())
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def append(self, message: dict) -> None:
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
