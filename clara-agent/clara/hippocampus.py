"""Long-term memory that filters like a person: the hippocampus decides what stays, sleep tidies it.

06.11.2026 (user: "create a filter like a hippocampus: what is important should be remembered, what is emotional
should be remembered"; "sleep once a week: merge duplicates, compress old entries, let unused things fade; my rules
never fade"). Long context after arXiv 2611.05306 ("You don't need long context", Belcak et al. 2026): summarise at a
threshold instead of keeping everything, and read details from files when needed.

Three layers
  persona            who Clara is (a file the user owns; not touched here)
  long-term memory   entries in <home>/hippocampus.jsonl, rendered into <home>/memory.md (read every turn)
  archive            full sessions (sessions/*.jsonl) and faded entries (hippocampus-faded.jsonl): searchable, not
                     loaded -- "I don't remember exactly, let me check"

Encoding (at every compaction and at the end of a session)
  1. the model reads the conversation and the current memories and proposes candidates, each with a kind,
     importance 1-5, emotion 0-3 and whether it will still matter in a week; it also names memories that came up
     again (recall strengthens them) and corrections to old ones
  2. a fixed gate in code, not the model's mood, decides: hard filter (paths, commands, case ids, bare numbers never
     enter), then  keep = kind in {rule, decision, promise, preference} and importance >= 3,  or  importance + emotion >= 5
     (emotion counts like the amygdala's boost); "won't matter in a week" vetoes only weak feelings (emotion < 2)
  3. a near-duplicate of an existing memory strengthens that one instead of adding a line

Sleep (weekly)
  strength = importance + emotion + 0.5 * min(uses, 6) - 0.5 * weeks since last use; rules (pinned) never fade.
  The model proposes merges of duplicates and shorter wording of old entries (applied with guards), entries with
  strength < 2 fade into the archive, and the memory is cut to the strongest CAP entries (default 150).
"""
from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path

KINDS = ("rule", "decision", "preference", "promise", "status", "lesson", "feeling", "fact")
TITLES = {"rule": "Rules (never fade)", "decision": "Decisions", "preference": "Preferences", "promise": "Promises and open points",
          "status": "Projects", "lesson": "Lessons", "feeling": "How it felt", "fact": "Other"}
CAP = 150
FADE_BELOW = 2.0
HEADER = "# Long-term memory (hippocampus) -- written by Clara; change it with `clara memory`, not by hand"

# never into long-term memory: these belong in the archive
HARD = [
    (re.compile(r"(?:^|[\s(\"'])(?:~|\.{1,2})?/[\w.\-]+/[\w.\-/]*"), "path"),
    (re.compile(r"`[^`]+`|\$\(|\s--?[a-zA-Z][\w-]*(?:=|\s|$)|\|\s*\w+|&&"), "command"),
    (re.compile(r"\bsub-(?=[A-Za-z]*\d)[A-Za-z0-9]+|\bses-(?=[A-Za-z]*\d)[A-Za-z0-9]+", re.I), "case id"),
    (re.compile(r"\.(nii|nii\.gz|dcm|mgz|mgh)\b", re.I), "image file"),
]


def hard_filter(text: str) -> str | None:
    """Reason why a text may not enter long-term memory, or None."""
    for rx, why in HARD:
        if rx.search(text):
            return why
    words = re.findall(r"[A-Za-zÄÖÜäöüß]{3,}", text)
    if len(words) < 3:
        return "no content (numbers or fragments only)"
    return None


def _words(text: str) -> set[str]:
    return {w.lower() for w in re.findall(r"[A-Za-zÄÖÜäöüß0-9]{3,}", text)}


def similar(a: str, b: str) -> float:
    wa, wb = _words(a), _words(b)
    return len(wa & wb) / max(1, len(wa | wb))


def strength(e: dict, now: float | None = None) -> float:
    if e.get("pinned"):
        return 99.0
    now = now or time.time()
    weeks = max(0.0, (now - float(e.get("last_used") or e.get("created") or now)) / (7 * 86400))
    return float(e.get("importance", 3)) + float(e.get("emotion", 0)) + 0.5 * min(int(e.get("uses", 0)), 6) - 0.5 * weeks


def gate(c: dict) -> tuple[bool, str]:
    """The fixed rule: (keep?, reason)."""
    text = " ".join(str(c.get("text", "")).split())
    why = hard_filter(text)
    if why:
        return False, f"filtered: {why}"
    kind = c.get("kind") if c.get("kind") in KINDS else "fact"
    imp = max(1, min(5, int(c.get("importance") or 1)))
    emo = max(0, min(3, int(c.get("emotion") or 0)))
    if c.get("matters_in_a_week") is False and emo < 2:
        return False, "will not matter in a week"           # a strong feeling is kept anyway (amygdala)
    if kind in ("rule", "decision", "promise", "preference") and imp >= 3:
        return True, f"{kind}, importance {imp}"
    if imp + emo >= 5:
        return True, f"importance {imp} + emotion {emo}"
    return False, f"too weak (importance {imp}, emotion {emo})"


def _json_block(text: str) -> dict:
    """The first JSON object in a model reply (models like to wrap it in prose or code fences)."""
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    start = text.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except ValueError:
                        break
        start = text.find("{", start + 1)
    return {}


def _ask(provider, system: str, user: str) -> str:
    out = ""
    for ev in provider.stream([{"role": "system", "content": system}, {"role": "user", "content": user}], None):
        if ev.kind == "done":
            out = ev.text
    return out


def transcript(messages: list[dict], max_chars: int = 40000) -> str:
    """Readable conversation for the model: tool output shortened, newest part kept if it is too long."""
    lines = []
    for m in messages:
        role = m.get("role")
        if role == "system":
            continue
        content = m.get("content")
        if isinstance(content, list):
            content = " ".join(str(x.get("text", "")) for x in content if isinstance(x, dict))
        content = str(content or "")
        if role == "tool":
            content = content[:300] + (" ..." if len(content) > 300 else "")
            lines.append(f"[tool result] {content}")
        elif role == "assistant":
            calls = m.get("tool_calls") or []
            names = ", ".join(c.get("function", {}).get("name", "?") for c in calls)
            lines.append(f"Clara: {content}" + (f" [used {names}]" if names else ""))
        else:
            lines.append(f"Researcher: {content}")
    text = "\n".join(lines)
    return text[-max_chars:]


ENCODE_SYSTEM = """You are Clara's hippocampus. You decide what Clara will still remember next week, like a person:
the gist, the decisions, the feelings -- not the details.

Return ONLY a JSON object:
{"new": [{"text": "...", "kind": "rule|decision|preference|promise|status|lesson|feeling|fact",
          "importance": 1-5, "emotion": 0-3, "matters_in_a_week": true|false}],
 "recalled": ["id", ...],
 "corrected": [{"id": "...", "text": "..."}]}

What deserves a memory:
- rule: something the researcher always wants or never wants (these never fade); "from now on ...", "always ...",
  "never ..." are rules
- decision: what was decided, with the reason in a few words if one was given
- preference: how the researcher likes things (explanations, tone, tools)
- promise: what Clara or the researcher promised to do, or an open point
- status: a project started, stuck, finished or dropped
- lesson: something went wrong and what was learned
- feeling: how the researcher felt (annoyed, relieved, happy) and about what
Write each memory as one short, self-contained sentence in plain English, as Clara would remember it
("The researcher was annoyed that the watches sent so many messages; they now come once a day").
NEVER put file paths, commands, code, case ids, image file names or bare numbers into a memory -- they stay in the
archive. Small talk, routine steps and intermediate results are not memories.
importance: 5 = shapes future work, 3 = useful, 1 = trivia. emotion: 0 = neutral, 3 = strong feeling.
"recalled": ids of existing memories that this conversation used or confirmed.
"corrected": an existing memory that this conversation shows to be wrong or outdated, with the corrected sentence.
If nothing is worth remembering, return {"new": [], "recalled": [], "corrected": []}."""

SLEEP_SYSTEM = """You are Clara, tidying your long-term memory in your sleep. Return ONLY a JSON object:
{"merge": [{"ids": ["id1", "id2", ...], "text": "one sentence that keeps what matters of all of them"}],
 "shorten": [{"id": "...", "text": "a shorter sentence with the same meaning"}]}
merge: memories that say the same thing or are steps of one story (keep the newest state).
shorten: older entries that are long-winded. Never change the meaning, never add facts, never add paths, commands,
ids or numbers. Leave everything else alone. If nothing needs tidying, return {"merge": [], "shorten": []}."""


class Hippocampus:
    def __init__(self, home: Path, cap: int = CAP):
        self.home = Path(home)
        self.store = self.home / "hippocampus.jsonl"
        self.faded = self.home / "hippocampus-faded.jsonl"
        self.log = self.home / "hippocampus.log"
        self.memory_md = self.home / "memory.md"
        self.cap = cap

    # ------------------------------------------------------------------ storage
    def load(self) -> list[dict]:
        if not self.store.is_file():
            return self._import_old()
        out = []
        for ln in self.store.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                out.append(json.loads(ln))
            except ValueError:
                continue
        return out

    def save(self, entries: list[dict]) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        tmp = self.store.with_suffix(".tmp")
        tmp.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries), encoding="utf-8")
        tmp.replace(self.store)
        self.render(entries)

    def _note(self, text: str) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        with open(self.log, "a", encoding="utf-8") as fh:
            fh.write(time.strftime("%F %T ") + text + "\n")

    def _import_old(self) -> list[dict]:
        """First run: the old memory.md lines become ordinary memories (the file is kept as a backup)."""
        if not self.memory_md.is_file():
            return []
        text = self.memory_md.read_text(encoding="utf-8", errors="replace")
        if text.startswith(HEADER):
            return []
        backup = self.memory_md.with_name("memory.md.vor-hippocampus")
        if not backup.exists():
            backup.write_text(text, encoding="utf-8")
        now = time.time()
        out = []
        for ln in text.splitlines():
            s = re.sub(r"^\s*[-*]\s*(\d{4}-\d{2}-\d{2}:\s*)?", "", ln).strip()
            if len(s) < 12 or s.startswith("#"):
                continue
            out.append({"id": uuid.uuid4().hex[:8], "text": s[:400], "kind": "fact", "importance": 3, "emotion": 0,
                        "pinned": False, "created": now, "last_used": now, "uses": 0, "source": "old memory.md"})
        self._note(f"imported {len(out)} lines from the old memory.md (backup {backup.name})")
        return out

    def render(self, entries: list[dict]) -> None:
        """memory.md for the system prompt: grouped, strongest first, at most cap lines."""
        keep = sorted(entries, key=lambda e: -strength(e))[: self.cap]
        lines = [HEADER, ""]
        for kind in KINDS:
            group = [e for e in keep if e.get("kind") == kind]
            if group:
                lines.append(f"## {TITLES[kind]}")
                lines += [f"- {e['text']}" for e in group]
                lines.append("")
        self.memory_md.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")

    # ------------------------------------------------------------------ encoding
    def encode(self, provider, messages: list[dict]) -> dict:
        """Decide what of this conversation stays. Returns counts for the UI."""
        convo = transcript(messages)
        if len(convo) < 80:
            return {"kept": 0, "rejected": 0, "strengthened": 0}
        entries = self.load()
        known = "\n".join(f"{e['id']}: {e['text']}" for e in sorted(entries, key=lambda e: -strength(e))[:200]) or "(none yet)"
        reply = _ask(provider, ENCODE_SYSTEM, f"Current memories:\n{known}\n\nConversation:\n{convo}")
        data = _json_block(reply)
        now = time.time()
        by_id = {e["id"]: e for e in entries}
        kept = rejected = strengthened = 0
        for rid in data.get("recalled") or []:
            e = by_id.get(str(rid))
            if e:
                e["uses"] = int(e.get("uses", 0)) + 1
                e["last_used"] = now
                strengthened += 1
        for cor in data.get("corrected") or []:
            e = by_id.get(str(cor.get("id")))
            text = " ".join(str(cor.get("text", "")).split())
            if e and text and not hard_filter(text):
                self._note(f"corrected {e['id']}: {e['text']!r} -> {text!r}")
                e["text"], e["last_used"] = text[:400], now
        for c in data.get("new") or []:
            ok, why = gate(c)
            text = " ".join(str(c.get("text", "")).split())[:400]
            if not ok:
                rejected += 1
                self._note(f"rejected ({why}): {text[:160]}")
                continue
            twin = max(entries, key=lambda e: similar(e["text"], text), default=None)
            if twin is not None and similar(twin["text"], text) >= 0.6:
                twin["uses"] = int(twin.get("uses", 0)) + 1
                twin["last_used"] = now
                twin["importance"] = max(int(twin.get("importance", 3)), int(c.get("importance") or 1))
                twin["emotion"] = max(int(twin.get("emotion", 0)), int(c.get("emotion") or 0))
                strengthened += 1
                continue
            kind = c.get("kind") if c.get("kind") in KINDS else "fact"
            e = {"id": uuid.uuid4().hex[:8], "text": text, "kind": kind,
                 "importance": max(1, min(5, int(c.get("importance") or 1))), "emotion": max(0, min(3, int(c.get("emotion") or 0))),
                 "pinned": kind == "rule", "created": now, "last_used": now, "uses": 0, "source": "encode"}
            entries.append(e)
            by_id[e["id"]] = e
            kept += 1
            self._note(f"kept ({why}): {text[:160]}")
        self.save(entries)
        return {"kept": kept, "rejected": rejected, "strengthened": strengthened}

    # ------------------------------------------------------------------ sleep
    def sleep(self, provider=None, now: float | None = None) -> dict:
        """Weekly tidy-up: merge and shorten (model, optional), fade the weak, cut to cap. Rules never fade."""
        now = now or time.time()
        entries = self.load()
        by_id = {e["id"]: e for e in entries}
        merged = shortened = 0
        if provider is not None and len(entries) > 1:
            listing = "\n".join(f"{e['id']} [{e['kind']}{', rule' if e.get('pinned') else ''}]: {e['text']}" for e in entries)
            data = _json_block(_ask(provider, SLEEP_SYSTEM, listing[-60000:]))
            for m in data.get("merge") or []:
                ids = [str(i) for i in m.get("ids") or [] if str(i) in by_id]
                text = " ".join(str(m.get("text", "")).split())[:400]
                if len(ids) < 2 or not text or hard_filter(text):
                    continue
                group = [by_id[i] for i in ids]
                if len({bool(g.get("pinned")) for g in group}) > 1:
                    continue                                   # a rule only merges with rules
                base = max(group, key=lambda g: (bool(g.get("pinned")), int(g.get("importance", 3))))
                new = dict(base, id=uuid.uuid4().hex[:8], text=text,
                           importance=max(int(g.get("importance", 3)) for g in group),
                           emotion=max(int(g.get("emotion", 0)) for g in group),
                           uses=sum(int(g.get("uses", 0)) for g in group),
                           last_used=max(float(g.get("last_used") or 0) for g in group),
                           created=min(float(g.get("created") or now) for g in group), source="sleep merge")
                for g in group:
                    by_id.pop(g["id"], None)
                by_id[new["id"]] = new
                merged += 1
                self._note(f"merged {ids} -> {text[:160]}")
            for sh in data.get("shorten") or []:
                e = by_id.get(str(sh.get("id")))
                text = " ".join(str(sh.get("text", "")).split())[:400]
                if e and text and not e.get("pinned") and len(text) < len(e["text"]) and not hard_filter(text):
                    e["text"] = text
                    shortened += 1
        entries = list(by_id.values())
        weak = [e for e in entries if strength(e, now) < FADE_BELOW]
        alive = sorted([e for e in entries if strength(e, now) >= FADE_BELOW], key=lambda e: -strength(e, now))
        over = alive[self.cap:]
        alive = alive[: self.cap]
        faded = weak + over
        if faded:
            with open(self.faded, "a", encoding="utf-8") as fh:
                for e in faded:
                    fh.write(json.dumps(dict(e, faded=now), ensure_ascii=False) + "\n")
                    self._note(f"faded (strength {strength(e, now):.1f}): {e['text'][:160]}")
        self.save(alive)
        res = {"merged": merged, "shortened": shortened, "faded": len(faded), "kept": len(alive)}
        self._note(f"sleep: {res}")
        return res

    # ------------------------------------------------------------------ archive search
    def search_faded(self, query: str, limit: int = 5) -> list[dict]:
        if not self.faded.is_file():
            return []
        rows = []
        for ln in self.faded.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                rows.append(json.loads(ln))
            except ValueError:
                continue
        return sorted(rows, key=lambda e: -similar(e.get("text", ""), query))[:limit]
