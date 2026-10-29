from __future__ import annotations

import time

from .base import Tool, ToolContext, ToolResult, clip


class AskUser(Tool):
    name = "ask_user"
    description = "Ask the user one short question when the task is ambiguous. Prefer this over guessing."
    parameters = {"type": "object", "properties": {"question": {"type": "string"}}, "required": ["question"]}
    kind = "meta"

    def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        q = str(args.get("question", "")).strip()
        if not q:
            return ToolResult("empty question", is_error=True)
        if ctx.ask_user is None:
            return ToolResult("No user is available to answer right now. State your assumption explicitly and continue.")
        answer = ctx.ask_user(q)
        if answer is None:
            return ToolResult("The user did not answer. State your assumption explicitly and continue.")
        return ToolResult(f"User answered: {answer}")


class Remember(Tool):
    name = "remember"
    description = ("Save a short durable note for future sessions (a decision, a path, a preference). "
                   "scope 'project' (default) or 'global'.")
    parameters = {"type": "object",
                  "properties": {"note": {"type": "string"}, "scope": {"type": "string", "enum": ["project", "global"]}},
                  "required": ["note"]}
    kind = "write"

    def summary(self, args: dict) -> str:
        return f"remember({str(args.get('note', ''))[:100]})"

    def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        note = " ".join(str(args.get("note", "")).split())
        if not note:
            return ToolResult("empty note", is_error=True)
        scope = args.get("scope") or "project"
        path = ctx.home / "memory.md" if scope == "global" else ctx.project_root / ".clara" / "memory.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"- {stamp}: {note}\n")
        return ToolResult(f"saved to {path}")


class Todo(Tool):
    name = "todo"
    description = "Keep a short task list for a multi-step job. Pass the complete list each time."
    parameters = {"type": "object",
                  "properties": {"items": {"type": "array",
                                           "items": {"type": "object",
                                                     "properties": {"text": {"type": "string"},
                                                                    "status": {"type": "string",
                                                                               "enum": ["pending", "in_progress", "done"]}},
                                                     "required": ["text", "status"]}}},
                  "required": ["items"]}
    kind = "meta"

    def summary(self, args: dict) -> str:
        items = args.get("items") or []
        done = sum(1 for i in items if isinstance(i, dict) and i.get("status") == "done")
        return f"todo({done}/{len(items)} done)"

    def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        items = args.get("items")
        if not isinstance(items, list):
            return ToolResult("items must be a list", is_error=True)
        clean = []
        for it in items:
            if isinstance(it, dict) and it.get("text"):
                clean.append({"text": str(it["text"]), "status": str(it.get("status", "pending"))})
        ctx.todo_state[:] = clean
        marks = {"done": "[x]", "in_progress": "[>]", "pending": "[ ]"}
        return ToolResult("\n".join(f"{marks.get(i['status'], '[ ]')} {i['text']}" for i in clean) or "(empty list)")


class Delegate(Tool):
    name = "delegate"
    description = ("Run a sub-task in a fresh context with read-only tools and get back only its answer. "
                   "Use for research that would flood this conversation (reading many files).")
    parameters = {"type": "object",
                  "properties": {"task": {"type": "string", "description": "complete, self-contained instructions"},
                                 "tools": {"type": "string", "enum": ["read", "all"],
                                           "description": "read (default) or all"}},
                  "required": ["task"]}
    kind = "meta"

    def summary(self, args: dict) -> str:
        return f"delegate({str(args.get('task', ''))[:100]})"

    def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        if ctx.delegate is None:
            return ToolResult("delegation is not available in this context", is_error=True)
        task = str(args.get("task", "")).strip()
        if not task:
            return ToolResult("empty task", is_error=True)
        answer = ctx.delegate(task, str(args.get("tools") or "read"))
        return ToolResult(clip(answer))
