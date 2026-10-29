"""Skills: folders with a SKILL.md, the same layout Claude Code and Hermes use.

    ~/.clara/skills/<name>/SKILL.md          global
    <project>/.clara/skills/<name>/SKILL.md  per project

Only name, description and path go into the system prompt; the model reads the
file with read_file when a task matches. That keeps the prompt small.
"""
from __future__ import annotations

import re
from pathlib import Path

FRONT_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def parse_frontmatter(text: str) -> dict[str, str]:
    m = FRONT_RE.match(text)
    meta: dict[str, str] = {}
    if not m:
        return meta
    for line in m.group(1).splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            meta[k.strip().lower()] = v.strip().strip('"').strip("'")
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
            desc = meta.get("description") or ""
            if not desc:
                for line in text.splitlines():
                    s = line.strip()
                    if s and not s.startswith(("#", "---")):
                        desc = s[:160]
                        break
            found[name] = {"name": name, "description": desc[:200], "path": str(skill_md)}
    return list(found.values())
