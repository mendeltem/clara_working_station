"""The agent loop with a scripted model: tools, permissions, recording, limits."""
import json

from clara.agent import Agent, UI
from clara.config import ProjectSettings, load_settings
from clara.permissions import Permissions
from clara.providers.mock import MockProvider
from clara.sessions import Session, list_sessions
from clara.tools.base import default_registry


class RecordingUI(UI):
    def __init__(self, permission="allow", answer="yes, do it"):
        self.events = []
        self.permission = permission
        self.answer = answer

    def on_text(self, d): self.events.append(("text", d))
    def on_reasoning(self, d): self.events.append(("reasoning", d))
    def on_tool_start(self, call, summary): self.events.append(("tool_start", summary))
    def on_tool_result(self, call, result, decision): self.events.append(("tool_result", decision, result.text))
    def on_warning(self, t): self.events.append(("warning", t))
    def on_status(self, t): self.events.append(("status", t))
    def ask_permission(self, req): self.events.append(("ask", req.summary)); return self.permission
    def ask_user(self, q): self.events.append(("question", q)); return self.answer

    def text(self):
        return "".join(e[1] for e in self.events if e[0] == "text")

    def of(self, kind):
        return [e[1] for e in self.events if e[0] == kind]


def make_agent(project, clara_home, script, ui=None, mode="default", tool_mode="native", record=True, max_turns=None):
    settings = load_settings()
    ui = ui or RecordingUI()
    perms = Permissions(ProjectSettings.load(project, clara_home), mode=mode, asker=ui.ask_permission, project_root=project, home=clara_home)
    session = Session.new(settings.sessions_dir, project, record=record)
    prov = MockProvider(script)
    agent = Agent(settings, prov, default_registry(), perms, session, ui, project, tool_mode=tool_mode, max_turns=max_turns)
    return agent, prov, ui, session


def test_read_then_answer_native(project, clara_home):
    script = [{"tool_calls": [{"name": "read_file", "arguments": {"path": "hello.txt"}}]}, "The word is PELICAN."]
    agent, prov, ui, session = make_agent(project, clara_home, script)
    out = agent.run("what is the secret word?")
    assert out == "The word is PELICAN."
    roles = [m["role"] for m in session.messages]
    assert roles == ["system", "user", "assistant", "tool", "assistant"]
    assert session.messages[3]["content"].startswith("1\tThe secret word is PELICAN.")
    assert prov.calls[0]["tools"] and prov.calls[0]["messages"][0]["content"] == prov.calls[1]["messages"][0]["content"]
    assert "[context: project=" in session.messages[1]["content"]


def test_prompt_mode_wraps_results_like_qwen_template(project, clara_home):
    script = ['<tool_call>\n{"name": "read_file", "arguments": {"path": "hello.txt"}}\n</tool_call>', "done"]
    agent, prov, ui, session = make_agent(project, clara_home, script, tool_mode="prompt")
    agent.run("read it")
    assert prov.calls[0]["tools"] is None
    assert "<tools>" in session.messages[0]["content"] and '"read_file"' in session.messages[0]["content"]
    assert session.messages[2]["role"] == "assistant" and "<tool_call>" in session.messages[2]["content"]
    assert session.messages[3]["role"] == "user" and session.messages[3]["content"].startswith("<tool_response>\n1\tThe secret")


def test_write_asks_and_denial_is_reported_to_model(project, clara_home):
    script = [{"tool_calls": [{"name": "write_file", "arguments": {"path": "x.txt", "content": "hi"}}]}, "ok"]
    ui = RecordingUI(permission="deny")
    agent, prov, ui, session = make_agent(project, clara_home, script, ui=ui)
    agent.run("write x")
    assert not (project / "x.txt").exists()
    assert ("ask", "write_file(x.txt)") in ui.events
    assert session.messages[3]["content"].startswith("ERROR: not executed: denied by user")


def test_write_allowed_and_bash_runs(project, clara_home):
    script = [{"tool_calls": [{"name": "write_file", "arguments": {"path": "x.txt", "content": "hi"}}]},
              {"tool_calls": [{"name": "bash", "arguments": {"command": "cat x.txt"}}]}, "done"]
    agent, prov, ui, session = make_agent(project, clara_home, script)
    agent.run("write and cat")
    assert (project / "x.txt").read_text() == "hi"
    assert any(e[0] == "tool_result" and "hi" in e[2] for e in ui.events)


def test_unknown_tool_and_bad_arguments_are_reported(project, clara_home):
    script = [{"tool_calls": [{"name": "teleport", "arguments": {}}]},
              '<tool_call>\n{"name": "read_file", "arguments": "not json"}\n</tool_call>', "end"]
    agent, prov, ui, session = make_agent(project, clara_home, script)
    agent.run("x")
    assert "unknown tool teleport" in session.messages[3]["content"]
    assert any("not valid JSON" in m.get("content", "") for m in session.messages)


def test_max_turns_stops_a_loop(project, clara_home):
    script = [{"tool_calls": [{"name": "list_files", "arguments": {}}]}] * 10
    agent, prov, ui, session = make_agent(project, clara_home, script, max_turns=3)
    agent.run("loop")
    assert len(prov.calls) == 3
    assert any("max_turns" in t for t in ui.of("warning"))


def test_ask_user_tool_reaches_the_ui(project, clara_home):
    script = [{"tool_calls": [{"name": "ask_user", "arguments": {"question": "Which port?"}}]}, "ok 8080"]
    agent, prov, ui, session = make_agent(project, clara_home, script)
    agent.run("change the port")
    assert ("question", "Which port?") in ui.events
    assert "User answered: yes, do it" in session.messages[3]["content"]


def test_delegate_runs_a_child_with_read_only_tools(project, clara_home):
    script = [{"tool_calls": [{"name": "delegate", "arguments": {"task": "find the secret word"}}]},
              {"tool_calls": [{"name": "read_file", "arguments": {"path": "hello.txt"}}]},   # child turn 1
              "child says: PELICAN",                                                              # child turn 2
              "parent: PELICAN"]
    agent, prov, ui, session = make_agent(project, clara_home, script)
    out = agent.run("delegate it")
    assert out == "parent: PELICAN"
    child_call = prov.calls[1]
    assert [t["function"]["name"] for t in child_call["tools"]] == ["read_file", "list_files", "search"]
    assert "child says: PELICAN" in session.messages[3]["content"]


def test_no_record_writes_nothing(project, clara_home):
    agent, prov, ui, session = make_agent(project, clara_home, ["hello"], record=False)
    agent.run("hi")
    assert session.path is None and not list((clara_home / "sessions").glob("*.jsonl"))


def test_record_resume_and_compact(project, clara_home):
    agent, prov, ui, session = make_agent(project, clara_home, ["first answer"])
    agent.run("first question")
    items = list_sessions(clara_home / "sessions")
    assert items and items[0]["title"].endswith("first question") and items[0]["messages"] == 3
    loaded = Session.load(session.path)
    assert [m["role"] for m in loaded.messages] == ["system", "user", "assistant"]
    settings = load_settings()
    prov2 = MockProvider(["summary of everything"])
    agent2 = Agent(settings, prov2, default_registry(), agent.permissions, loaded, RecordingUI(), project)
    assert agent2.compact() == "summary of everything"
    assert len(agent2.messages) == 2 and "summary of everything" in agent2.messages[1]["content"]
    lines = [json.loads(l) for l in session.path.read_text().splitlines()]
    assert lines[-1]["kind"] == "compact"           # append-only file, compaction is a note


def test_interrupt_stops_the_turn(project, clara_home):
    script = [{"tool_calls": [{"name": "list_files", "arguments": {}}]}] * 5
    agent, prov, ui, session = make_agent(project, clara_home, script)
    original = agent._execute

    def execute_then_interrupt(call):
        res = original(call)
        agent.interrupt()
        return res

    agent._execute = execute_then_interrupt
    agent.run("go")
    assert len(prov.calls) == 1
    assert session.messages[-1]["content"] == "[interrupted by user]"
