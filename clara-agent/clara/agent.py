"""The agent loop: user message -> model -> tool calls -> results -> model -> ... -> answer."""
from __future__ import annotations

import json
import os
import re
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


def kfmt(n) -> str:
    n = int(n or 0)
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def route_text(routes) -> str:
    """'cle/frontier' -> 'Clé (frontier)', 'lokal/Qwen...' -> 'local GPU (Qwen...)'."""
    out = []
    for r in routes:
        place, _, model = (r or "").partition("/")
        if not place:
            continue
        name = {"cle": "Clé", "lokal": "local GPU"}.get(place, place)
        out.append(f"{name} ({model})" if model else name)
    return " + ".join(dict.fromkeys(out))


def usage_line(st: dict) -> str:
    """Plain words: 'context 13.0k tokens · 2.4k new, 10.6k from cache · wrote 450 · local GPU (Qwen)'."""
    parts = []
    if st.get("context"):
        parts.append(f"context {kfmt(st['context'])} tokens")
    if st.get("last_cached") is not None and st.get("context"):
        parts.append(f"{kfmt(st['context'] - st['last_cached'])} new, {kfmt(st['last_cached'])} from cache")
    if st.get("completion"):
        parts.append(f"wrote {kfmt(st['completion'])}")
    if st.get("speed"):
        parts.append(f"{st['speed']:.0f} tokens/s")
    where = route_text(st.get("routes", []))
    if where:
        parts.append(where)
    return " · ".join(parts)


def turn_summary(st: dict) -> str:
    secs = int(st.get("seconds", 0))
    dur = f"{secs // 60}m {secs % 60:02d}s" if secs >= 60 else f"{secs}s"
    calls = st.get("calls", 0)
    parts = [f"Done in {dur}"]
    if calls:
        parts.append(f"{calls} model call{'s' if calls != 1 else ''}")
        parts.append(f"read {kfmt(st.get('prompt', 0))} tokens ({kfmt(st.get('cached', 0))} from cache)")
        parts.append(f"wrote {kfmt(st.get('completion', 0))}")
    if st.get("context"):
        parts.append(f"context now {kfmt(st['context'])}")
    where = route_text(st.get("routes", []))
    if where:
        parts.append("on " + where)
    return " · ".join(parts)


class UI:
    """Callbacks for a front end. Every method is optional; defaults do nothing."""
    def on_text(self, delta: str) -> None: ...
    def on_reasoning(self, delta: str) -> None: ...
    def on_tool_start(self, call: ToolCall, summary: str) -> None: ...
    def on_tool_result(self, call: ToolCall, result: ToolResult, decision: str) -> None: ...
    def on_status(self, text: str) -> None: ...

    def on_usage(self, stats: dict) -> None:
        """Running numbers of the current turn (see Agent._note_usage). Default: one plain status line."""
        self.on_status(usage_line(stats))

    def on_turn_stats(self, stats: dict) -> None:
        """Called once at the end of a turn with the totals of that turn."""
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


LOOP_WARN = 3    # the same call with the same result this many times in a row: the next result says so
LOOP_STOP = 6    # ... this many times: the turn ends with a visible note (a model in this state fills its context, Apex 04.11.)
SLEEP_CALL = re.compile(r"\bsleep\b")   # waiting on purpose repeats itself and is not a loop


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
        self.turn = {"t0": time.time(), "calls": 0, "prompt": 0, "cached": 0, "completion": 0, "routes": [],
                     "context": 0, "last_cached": None, "speed": None}
        self._stop.clear()
        user_turns = sum(1 for m in self.messages if m.get("role") == "user")
        content = user_text if user_turns else context_line(self.project_root) + "\n\n" + user_text
        if os.environ.get("CLARA_WORK_TOOLS") == "1" or (getattr(self.settings, "raw", {}) or {}).get("work", {}).get("tools"):
            try:
                from .tools.work import overview_line
                line = overview_line()
            except Exception:  # noqa: BLE001 -- the overview must never break a turn
                line = ""
            if line:
                content = (f"[background for you, not written by the researcher -- mention only if asked or if something "
                           f"failed: your work sessions now: {line}]\n\n") + content
        self.session.append({"role": "user", "content": content})
        final_text = ""
        self._repeat_key, self._repeats, self._context_full = None, 0, None
        for turn in range(self.max_turns):
            if self._stop.is_set():
                self.session.append({"role": "assistant", "content": "[interrupted by user]"})
                self.ui.on_turn_end("")
                return ""
            self._check_budget()
            if self.settings.compact_at and (self.meter.last_prompt_tokens or 0) >= self.settings.compact_at:
                self.auto_compact()
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
                if not text.strip() and self._context_full:
                    text = (f"[no answer: the context is full ({self._context_full} tokens in the prompt), so the reply was cut off and "
                            "the model could not continue; start a new session or split the job]")
                    self.ui.on_warning(text)
                elif not text.strip():
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
                self._count_repeat(call, result)
                self._append_tool_result(call, result, native)
            if self._repeats >= LOOP_STOP:
                final_text = (f"[no answer: stopped, the model made the same tool call {self._repeats} times in a row and got the same "
                              "result each time; start a new session or change the job]")
                self.ui.on_warning(final_text)
                self.session.append({"role": "assistant", "content": final_text})
                break
        else:
            self.ui.on_warning(f"stopped after {self.max_turns} model turns (max_turns); say 'continue' to go on")
        self._turn_stats()
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
                    # this agent sends no max_tokens: on the local server only the context can cut a reply this short
                    # (the relay gives the hub route a limit of 24000)
                    done_tokens = self.meter.last_completion_tokens
                    if self.meter.last_prompt_tokens and (done_tokens is None or done_tokens < 20_000):
                        self._context_full = self.meter.last_prompt_tokens
                        self.ui.on_warning(f"the context is full ({self._context_full} tokens in the prompt): the reply was cut off")
                    else:
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
        st = getattr(self, "turn", None)
        if st is None:
            return
        details = usage.get("prompt_tokens_details") or {}
        cached = details.get("cached_tokens")
        if cached is None and isinstance(timings.get("prompt_n"), (int, float)) and isinstance(pt, int):
            cached = max(0, pt - int(timings["prompt_n"]))     # llama.cpp: prompt_n = tokens it had to evaluate
        st["calls"] += 1
        st["prompt"] += pt if isinstance(pt, int) else 0
        st["cached"] += int(cached or 0)
        st["completion"] += int(usage.get("completion_tokens") or 0)
        st["context"] = pt if isinstance(pt, int) else st["context"]
        st["last_cached"] = cached
        st["speed"] = timings.get("predicted_per_second")
        if ev.data.get("route") and ev.data["route"] not in st["routes"]:
            st["routes"].append(ev.data["route"])
        self.ui.on_usage(dict(st))

    def _turn_stats(self) -> None:
        st = getattr(self, "turn", None)
        if st is None:
            return
        st["seconds"] = time.time() - st["t0"]
        tot = self.session_totals = getattr(self, "session_totals", {"turns": 0, "calls": 0, "prompt": 0, "cached": 0, "completion": 0})
        tot["turns"] += 1
        for k in ("calls", "prompt", "cached", "completion"):
            tot[k] += st[k]
        self.ui.on_turn_stats(dict(st))

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

    def _count_repeat(self, call: ToolCall, result: ToolResult) -> None:
        """How many times in a row this exact call came back with this exact result. Waiting with sleep does not count."""
        args = json.dumps(call.arguments, sort_keys=True, ensure_ascii=False)
        if SLEEP_CALL.search(args):
            self._repeat_key, self._repeats = None, 0
            return
        key = (call.name, args, result.text)
        self._repeats = self._repeats + 1 if key == self._repeat_key else 1
        self._repeat_key = key

    def _loop_note(self) -> str:
        if not LOOP_WARN <= self._repeats < LOOP_STOP:
            return ""
        return (f"\n\n[REPEAT: you have made this same call {self._repeats} times and got the same result. Repeating it will not help. "
                f"Do the next step or give your answer; at {LOOP_STOP} repeats the job is stopped.]")

    def _deadline_note(self) -> str:
        """The supervisor sets CLARA_SOFT_DEADLINE (epoch seconds) before its hard limit. Past it, the next tool result says so, at most every 5 minutes."""
        try:
            soft = float(os.environ.get("CLARA_SOFT_DEADLINE", ""))
        except ValueError:
            return ""
        now = time.time()
        if now < soft or now - getattr(self, "_deadline_said", 0.0) < 300:
            return ""
        self._deadline_said = now
        return ("\n\n[TIME LIMIT: the supervisor will stop this job soon. Stop starting new work. Save your results now, "
                "write the report or output the job asks for, say what is unfinished, and end with your final answer.]")

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
        content += self._loop_note() + self._deadline_note()
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

    def remember(self) -> dict | None:
        """Hand the conversation to the hippocampus (if switched on). Never raises: memory must not break a turn."""
        if not self.settings.hippocampus or os.environ.get("CLARA_NO_HIPPOCAMPUS") == "1":
            return None         # 06.11.2026: work sessions and jobs have no long-term memory (only the chat Clara)
        try:
            from .hippocampus import Hippocampus
            return Hippocampus(self.settings.home, cap=self.settings.memory_cap).encode(self.provider, self.messages)
        except Exception as exc:  # noqa: BLE001
            self.ui.on_warning(f"memory not updated: {type(exc).__name__}: {exc}")
            return None

    def auto_compact(self) -> None:
        """The context reached [memory].compact_at: remember what matters, then go on with a summary (arXiv 2611.05306)."""
        size = self.meter.last_prompt_tokens or 0
        got = self.remember()
        # the researcher's newest message stays the open request, it must not vanish into the summary
        last = self.messages[-1] if self.messages and self.messages[-1].get("role") == "user" else None
        summary = self.compact(keep_last=last is not None)
        if last is not None and not summary.startswith(("nothing", "the model returned no summary")):
            self.session.append(last)
        self.meter.last_prompt_tokens = 0
        note = f"context reached {size // 1000}k tokens: summarised and started fresh"
        if got:
            note += f" · memory: {got['kept']} new, {got['strengthened']} strengthened, {got['rejected']} let go"
        if summary.startswith(("nothing", "the model returned no summary")):
            note = f"context reached {size // 1000}k tokens, but the summary failed: {summary}"
        self.ui.on_status(note)

    def compact(self, keep_last: bool = False) -> str:
        """Explicit summary of the conversation so far. Flushes the server's prefix cache.
        keep_last: leave the newest message out of the summary (the caller appends it again)."""
        convo = [m for m in self.messages if m.get("role") != "system"]
        if keep_last and convo:
            convo = convo[:-1]
        if not convo:
            return "nothing to compact"
        ask = {"role": "user", "content": "Summarise this conversation for yourself so that you can continue without it, "
                                          "in at most 400 words: the researcher's task in their own words, what was done, "
                                          "the current state, decisions, open points and the next step, and the file paths "
                                          "you still need. Plain text."}
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
