"""The agent loop: user message -> model -> tool calls -> results -> model -> ... -> answer."""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import Settings
from .permissions import PermissionRequest, Permissions
from .prompts import build_system_prompt, context_line
from .providers.base import Event, Provider, ProviderError
from .sessions import Session
from .skills import discover_skills
from .toolcalls import ToolCall
from .tools.base import ToolContext, ToolRegistry, ToolResult


class UI:
    """Callbacks for a front end. Every method is optional; defaults do nothing."""
    def on_text(self, delta: str) -> None: ...
    def on_reasoning(self, delta: str) -> None: ...
    def on_tool_start(self, call: ToolCall, summary: str) -> None: ...
    def on_tool_result(self, call: ToolCall, result: ToolResult, decision: str) -> None: ...
    def on_status(self, text: str) -> None: ...
    def on_warning(self, text: str) -> None: ...
    def on_turn_end(self, text: str) -> None: ...
    def on_todo(self, items: list) -> None: ...

    def ask_permission(self, req: PermissionRequest) -> str:
        return "deny"

    def ask_user(self, question: str) -> str | None:
        return None


class Interrupted(Exception):
    pass


@dataclass
class TokenMeter:
    """Prompt-size bookkeeping. Starts as an estimate and calibrates itself on
    the exact prompt_tokens the server reports, so the number shown is measured
    after the first reply (and labelled as an estimate before)."""
    chars_per_token: float = 3.6
    calibrated: bool = False
    last_prompt_tokens: int | None = None
    last_completion_tokens: int | None = None
    last_timings: dict = field(default_factory=dict)

    def estimate(self, messages: list[dict], tools: list[dict] | None) -> int:
        chars = sum(len(json.dumps(m, ensure_ascii=False)) for m in messages)
        if tools:
            chars += len(json.dumps(tools))
        return int(chars / self.chars_per_token) + 4 * len(messages)

    def calibrate(self, messages: list[dict], tools: list[dict] | None, prompt_tokens: int) -> None:
        if prompt_tokens <= 0:
            return
        chars = sum(len(json.dumps(m, ensure_ascii=False)) for m in messages) + (len(json.dumps(tools)) if tools else 0)
        self.chars_per_token = max(1.0, chars / prompt_tokens)
        self.calibrated = True
        self.last_prompt_tokens = prompt_tokens


class Agent:
    def __init__(self, settings: Settings, provider: Provider, tools: ToolRegistry, permissions: Permissions,
                 session: Session, ui: UI | None, project_root: Path, tool_mode: str = "native",
                 max_turns: int | None = None, depth: int = 0):
        self.settings = settings
        self.provider = provider
        self.tools = tools
        self.permissions = permissions
        self.session = session
        self.ui = ui or UI()
        self.project_root = project_root
        self.tool_mode = tool_mode
        self.max_turns = max_turns or settings.max_turns
        self.depth = depth
        self.meter = TokenMeter()
        self._stop = threading.Event()
        self.n_ctx: int | None = None
        self.ctx = ToolContext(project_root=project_root, home=settings.home, session_id=session.id,
                               ask_user=self.ui.ask_user, status=self.ui.on_status,
                               delegate=self._delegate if depth == 0 else None)
        skills = discover_skills([settings.skills_dir, project_root / ".clara" / "skills"])
        self.system_prompt = build_system_prompt(project_root, settings.home, skills, self.tools.schemas(),
                                                 prompt_mode_tools=(tool_mode == "prompt"))
        if not self.session.messages:
            self.session.append({"role": "system", "content": self.system_prompt})
        elif self.session.messages[0].get("role") != "system":
            self.session.messages.insert(0, {"role": "system", "content": self.system_prompt})

    # ----------------------------------------------------------------- control
    def interrupt(self) -> None:
        self._stop.set()

    @property
    def messages(self) -> list[dict]:
        return self.session.messages

    def _tools_param(self) -> list[dict] | None:
        return self.tools.schemas() if self.tool_mode == "native" else None

    # ------------------------------------------------------------------ run
    def run(self, user_text: str) -> str:
        """One full agentic turn. Returns the final assistant text."""
        self._stop.clear()
        user_turns = sum(1 for m in self.messages if m.get("role") == "user")
        content = user_text if user_turns else context_line(self.project_root) + "\n\n" + user_text
        self.session.append({"role": "user", "content": content})
        final_text = ""
        for turn in range(self.max_turns):
            if self._stop.is_set():
                self.session.append({"role": "assistant", "content": "[interrupted by user]"})
                self.ui.on_turn_end("")
                return ""
            self._check_budget()
            try:
                text, reasoning, calls, native = self._request()
            except ProviderError as exc:
                self.ui.on_warning(f"model server: {exc}")
                self.session.note("error", text=str(exc))
                return ""
            except Interrupted:
                self.session.append({"role": "assistant", "content": "[interrupted by user]"})
                self.ui.on_turn_end("")
                return ""
            if not calls:
                if not text.strip() and not getattr(self, "_leer_versucht", False):
                    # an empty final answer (reasoning used the whole budget, or a small fallback model gave up) is
                    # not a result: ask once more, then say so instead of ending the turn in silence
                    self._leer_versucht = True
                    self.ui.on_warning("the model returned an empty answer; asking once more")
                    self.session.append({"role": "user", "content": "(Your last reply was empty. Continue the task: "
                                                                    "use a tool or give your answer now.)"})
                    continue
                if not text.strip():
                    text = "[no answer: the model returned an empty reply twice; try again or start a new session]"
                    self.ui.on_warning(text)
                self._leer_versucht = False
                self.session.append({"role": "assistant", "content": text})
                final_text = text
                break
            self._append_assistant_with_calls(text, calls, native)
            for call in calls:
                if self._stop.is_set():
                    self._append_tool_result(call, ToolResult("[interrupted by user]", is_error=True), native)
                    self.ui.on_turn_end("")
                    return ""
                result, decision = self._execute(call)
                self._append_tool_result(call, result, native)
        else:
            self.ui.on_warning(f"stopped after {self.max_turns} model turns (max_turns); say 'continue' to go on")
        self.ui.on_turn_end(final_text)
        return final_text

    def _request(self) -> tuple[str, str, list[ToolCall], bool]:
        tools = self._tools_param()
        opts = {}
        if self.settings.temperature is not None:
            opts["temperature"] = self.settings.temperature
        calls: list[ToolCall] = []
        text = ""
        reasoning = ""
        native = False
        for ev in self.provider.stream(self.messages, tools, **opts):
            if self._stop.is_set():
                raise Interrupted()
            if ev.kind == "text":
                self.ui.on_text(ev.text)
            elif ev.kind == "reasoning":
                if self.settings.show_reasoning:
                    self.ui.on_reasoning(ev.text)
            elif ev.kind == "tool_call" and ev.tool_call is not None:
                calls.append(ev.tool_call)
            elif ev.kind == "usage":
                self._note_usage(ev, tools)
            elif ev.kind == "done":
                text = ev.text
                reasoning = ev.data.get("reasoning", "")
                native = bool(ev.data.get("native_tool_calls"))
                if ev.data.get("finish_reason") == "length":
                    self.ui.on_warning("the model hit max_tokens before finishing")
        return text, reasoning, calls, native

    def _note_usage(self, ev: Event, tools) -> None:
        usage = ev.data.get("usage") or {}
        timings = ev.data.get("timings") or {}
        pt = usage.get("prompt_tokens")
        if isinstance(pt, int) and pt > 0:
            est = self.meter.estimate(self.messages, tools)
            self.meter.calibrate(self.messages, tools, pt)
            # Ollama keeps ~num_ctx/2 tokens from the end and says nothing. The
            # server's own count is the only place where that shows.
            if est > 2000 and pt < 0.6 * est:
                self.ui.on_warning(f"server counted {pt} prompt tokens but the conversation is about {est}: "
                                   "the server truncated the context silently (Ollama behaviour). "
                                   "Raise num_ctx or use /compact.")
        self.meter.last_completion_tokens = usage.get("completion_tokens")
        if timings:
            self.meter.last_timings = timings
            cached = timings.get("prompt_n")
            speed = timings.get("predicted_per_second")
            parts = []
            if pt:
                parts.append(f"prompt {pt} tok")
            if isinstance(cached, (int, float)) and pt:
                parts.append(f"{int(cached)} evaluated, {max(0, pt - int(cached))} from cache")
            if isinstance(speed, (int, float)):
                parts.append(f"{speed:.1f} tok/s")
            if parts:
                self.ui.on_status(" · ".join(parts))

    def _check_budget(self) -> None:
        if self.n_ctx is None:
            try:
                self.n_ctx = self.provider.context_size()
            except Exception:
                self.n_ctx = None
        est = self.meter.estimate(self.messages, self._tools_param())
        label = "measured" if self.meter.calibrated else "estimated"
        if self.n_ctx:
            if est > self.settings.context_warn_fraction * self.n_ctx:
                self.ui.on_warning(f"prompt is about {est} tokens ({label}) of a {self.n_ctx} token context. "
                                   "llama-server will refuse with HTTP 400 beyond the limit; Ollama would truncate "
                                   "silently. /compact summarises (and flushes the prefix cache), /clear starts fresh.")
        elif est > 24_000 and not getattr(self, "_warned_unknown_ctx", False):
            self._warned_unknown_ctx = True
            self.ui.on_warning(f"prompt is about {est} tokens ({label}); the server did not report its context size")

    # ----------------------------------------------------------- tool handling
    def _execute(self, call: ToolCall) -> tuple[ToolResult, str]:
        tool = self.tools.get(call.name)
        self.ui.on_tool_start(call, tool.summary(call.arguments) if tool else f"{call.name}(?)")
        if tool is None:
            res = ToolResult(f"unknown tool {call.name}; available: {', '.join(self.tools.names())}", is_error=True)
            self.ui.on_tool_result(call, res, "unknown")
            return res, "unknown"
        if "_raw" in call.arguments and len(call.arguments) == 1:
            res = ToolResult(f"arguments for {call.name} were not valid JSON: {str(call.arguments['_raw'])[:200]}", is_error=True)
            self.ui.on_tool_result(call, res, "bad-args")
            return res, "bad-args"
        decision = self.permissions.decide(tool, call.arguments)
        if not decision.allow:
            res = ToolResult(f"not executed: {decision.reason}", is_error=True)
            self.ui.on_tool_result(call, res, "denied")
            return res, "denied"
        started = time.time()
        try:
            res = tool.run(call.arguments, self.ctx)
        except Exception as exc:  # a tool bug must not kill the session
            res = ToolResult(f"tool {call.name} failed: {type(exc).__name__}: {exc}", is_error=True)
        res.data["seconds"] = round(time.time() - started, 2)
        if tool.name == "todo":
            self.ui.on_todo(list(self.ctx.todo_state))
        self.ui.on_tool_result(call, res, "ok" if not res.is_error else "error")
        return res, "ok"

    def _append_assistant_with_calls(self, text: str, calls: list[ToolCall], native: bool) -> None:
        if self.tool_mode == "native" or native:
            msg: dict = {"role": "assistant", "content": text or "", "tool_calls": [c.to_openai() for c in calls]}
        else:
            blocks = "".join("\n<tool_call>\n" + json.dumps({"name": c.name, "arguments": c.arguments}, ensure_ascii=False)
                             + "\n</tool_call>" for c in calls)
            msg = {"role": "assistant", "content": (text or "") + blocks}
        self.session.append(msg)

    def _append_tool_result(self, call: ToolCall, result: ToolResult, native: bool) -> None:
        content = result.text if not result.is_error else f"ERROR: {result.text}"
        if self.tool_mode == "native" or native:
            self.session.append({"role": "tool", "tool_call_id": call.id, "name": call.name, "content": content})
        else:
            self.session.append({"role": "user", "content": f"<tool_response>\n{content}\n</tool_response>"})

    # ---------------------------------------------------------------- extras
    def _delegate(self, task: str, tools: str) -> str:
        names = ["read_file", "list_files", "search"] if tools != "all" else [n for n in self.tools.names() if n != "delegate"]
        child_tools = self.tools.subset(names)
        child_session = Session.new(None, self.project_root, record=False)
        child = Agent(self.settings, self.provider, child_tools, self.permissions, child_session, _QuietUI(self.ui),
                      self.project_root, tool_mode=self.tool_mode, max_turns=min(20, self.max_turns), depth=self.depth + 1)
        self.ui.on_status(f"delegate: {task[:80]}")
        answer = child.run(task)
        self.session.note("delegate", task=task, answer=answer[:4000])
        return answer or "(the sub-agent returned no text)"

    def compact(self) -> str:
        """Explicit summary of the conversation so far. Flushes the server's prefix cache."""
        convo = [m for m in self.messages if m.get("role") != "system"]
        if not convo:
            return "nothing to compact"
        ask = {"role": "user", "content": "Summarise this conversation for yourself in at most 300 words: the task, "
                                          "what was done, decisions, open points, and important file paths. Plain text."}
        summary = ""
        for ev in self.provider.stream([self.messages[0]] + convo + [ask], None):
            if ev.kind == "done":
                summary = ev.text
        if not summary:
            return "the model returned no summary; nothing changed"
        new_msgs = [self.messages[0], {"role": "user", "content": "[summary of the earlier conversation]\n" + summary}]
        self.session.replace_messages(new_msgs, "compact")
        return summary


class _QuietUI(UI):
    """Sub-agents show only status lines through the parent UI."""
    def __init__(self, parent: UI):
        self.parent = parent

    def on_tool_start(self, call, summary):
        self.parent.on_status("  ↳ " + summary)

    def ask_permission(self, req):
        return self.parent.ask_permission(req)

    def ask_user(self, question):
        return self.parent.ask_user(question)

    def on_warning(self, text):
        self.parent.on_warning(text)
