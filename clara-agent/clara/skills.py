"""Skills: folders with a SKILL.md, a common agent layout.

    ~/.clara/skills/<name>/SKILL.md          global
    <project>/.clara/skills/<name>/SKILL.md  per project

Only name, description and path go into the system prompt; the model reads the
file with read_file when a task matches. That keeps the prompt small.
The description goes in whole (up to 1,024 characters, the limit of the format): it says
when the skill applies, and that part usually stands at its end.
"""
from __future__ import annotations

import re
from pathlib import Path

FRONT_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
KEY_RE = re.compile(r"^([A-Za-z0-9_-]+):(.*)$")
BLOCK_RE = re.compile(r"^([>|])[+-]?\d*\s*(#.*)?$")
NESTED_RE = re.compile(r"^(-\s|-$|[A-Za-z0-9_\"'-]+:(\s|$))")
MAX_DESCRIPTION = 1024      # limit of the Agent Skills format; a longer text is cut and marked with "…"


def _unquote(v: str) -> str:
    if len(v) >= 2 and v[0] == v[-1] == '"':
        return re.sub(r"\\(.)", lambda e: {"n": "\n", "t": "\t"}.get(e.group(1), e.group(1)), v[1:-1])
    if len(v) >= 2 and v[0] == v[-1] == "'":
        return v[1:-1].replace("''", "'")
    return v


def parse_frontmatter(text: str) -> dict[str, str]:
    """Top-level keys of the block between the --- lines, without a YAML library (no dependencies on purpose).

    Understands what skill files use: plain and quoted values, block scalars (> folded, | literal) and plain
    values that continue on indented lines. Nested blocks (metadata:) give an empty value and their keys are
    not mixed into the top level. Until 24.11.2026 every line with a colon counted as a key, so a description
    written as a block showed up as ">" and nested keys could overwrite top-level ones."""
    m = FRONT_RE.match(text)
    meta: dict[str, str] = {}
    if not m:
        return meta
    lines = m.group(1).splitlines()
    i = 0
    while i < len(lines):
        km = KEY_RE.match(lines[i])
        i += 1
        if not km:
            continue
        key, value = km.group(1).strip().lower(), km.group(2).strip()
        more: list[str] = []
        while i < len(lines) and (not lines[i].strip() or lines[i][0] in " \t"):
            more.append(lines[i].strip())
            i += 1
        while more and not more[-1]:
            more.pop()
        block = BLOCK_RE.match(value)
        if block and block.group(1) == "|":
            meta[key] = "\n".join(more)
        elif block:                                  # folded: lines join with a space, an empty line stays a break
            meta[key] = "\n".join(" ".join(p.split()) for p in "\n".join(more).split("\n\n"))
        elif not value and more and NESTED_RE.match(more[0]):
            meta[key] = ""                           # nested mapping or list
        else:
            meta[key] = _unquote(" ".join([value] + [s for s in more if s]).strip())
    return meta


def discover_skills(dirs: list[Path]) -> list[dict]:
    found: dict[str, dict] = {}
    for d in dirs:
        if not d.is_dir():
            continue
        for skill_md in sorted(d.glob("*/SKILL.md")):
            try:
                text = skill_md.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            meta = parse_frontmatter(text)
            name = meta.get("name") or skill_md.parent.name
            desc = " ".join((meta.get("description") or "").split())
            if not desc:
                for line in FRONT_RE.sub("", text, count=1).splitlines():
                    s = line.strip()
                    if s and not s.startswith(("#", "---")):
                        desc = s[:160]
                        break
            if len(desc) > MAX_DESCRIPTION:
                desc = desc[:MAX_DESCRIPTION - 1].rstrip() + "…"
            found[name] = {"name": name, "description": desc, "path": str(skill_md)}
    return list(found.values())
