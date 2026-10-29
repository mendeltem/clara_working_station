"""End to end over HTTP against the simulated llama-server (format of llama.cpp --jinja).

What these tests prove: Clara's client handles the server's wire format
(streamed reasoning_content, tool_calls deltas, usage/timings, errors) and
keeps the request prefix stable. What they do not prove: anything about a
real model. See test_real_model.py for that.
"""
import json

from clara import netguard
from clara.agent import Agent
from clara.config import ProjectSettings, load_settings
from clara.permissions import Permissions
from clara.providers.openai_local import LocalOpenAIProvider
from clara.sessions import Session
from clara.tools.base import default_registry
from tests.test_agent import RecordingUI


def make(project, clara_home, server, tool_mode="native", ui=None):
    netguard.install({"127.0.0.1"}, clara_home / "netaudit.log")
    settings = load_settings()
    settings.base_url = server.base_url
    prov = LocalOpenAIProvider(server.base_url, model="", timeout=30)
    prov.model = prov.models()[0]
    ui = ui or RecordingUI()
    perms = Permissions(ProjectSettings.load(project, clara_home), mode="accept-edits", asker=ui.ask_permission,
                        project_root=project, home=clara_home)
    session = Session.new(settings.sessions_dir, project)
    return Agent(settings, prov, default_registry(), perms, session, ui, project, tool_mode=tool_mode), prov, ui, session


def test_detect_reports_llama_server_fields(fake_llama):
    prov = LocalOpenAIProvider(fake_llama.base_url)
    info = prov.detect()
    assert info["kind"] == "llama-server" and info["models"] == ["qwen3.8-27b-q4"]
    assert info["n_ctx"] == 8192 and info["sleeping"] is False and info["slots_total"] == 1
    assert prov.count_tokens("one two three") == 3


def test_detect_reports_ollama(fake_ollama):
    info = LocalOpenAIProvider(fake_ollama.base_url).detect()
    assert info["kind"] == "ollama" and info["n_ctx"] is None


def test_native_tool_calls_streamed_as_deltas(project, clara_home, fake_llama):
    fake_llama.script = [
        {"reasoning": "I should read the file.", "content": "", "tool_calls": [{"name": "read_file", "arguments": {"path": "hello.txt"}}]},
        {"reasoning": "Now answer.", "content": "The word is **PELICAN**."},
    ]
    agent, prov, ui, session = make(project, clara_home, fake_llama)
    out = agent.run("secret word?")
    assert out == "The word is **PELICAN**."
    assert "".join(ui.of("reasoning")) == "I should read the file.Now answer."
    assert session.messages[2]["tool_calls"][0]["function"]["name"] == "read_file"
    assert session.messages[2]["tool_calls"][0]["id"] == "call0"
    assert session.messages[3] == {"role": "tool", "tool_call_id": "call0", "name": "read_file",
                                   "content": "1\tThe secret word is PELICAN.\n2\tSecond line."}
    # status line from timings: measured numbers, not guesses
    assert any("tok/s" in t for t in ui.of("status"))
    assert agent.meter.calibrated and agent.meter.last_prompt_tokens > 0


def test_requests_are_append_only_with_a_stable_prefix(project, clara_home, fake_llama):
    fake_llama.script = [
        {"tool_calls": [{"name": "list_files", "arguments": {"pattern": "*.py"}}]},
        {"tool_calls": [{"name": "read_file", "arguments": {"path": "src/app.py"}}]},
        {"content": "done"},
    ]
    agent, prov, ui, session = make(project, clara_home, fake_llama)
    agent.run("look around")
    reqs = fake_llama.requests
    assert len(reqs) == 3
    for a, b in zip(reqs, reqs[1:]):
        assert b["messages"][: len(a["messages"])] == a["messages"], "history was rewritten between requests"
        assert a["tools"] == b["tools"]
    assert reqs[0]["messages"][0]["role"] == "system" and "date" not in reqs[0]["messages"][0]["content"]
    assert reqs[0]["stream"] is True and reqs[0]["stream_options"] == {"include_usage": True}
    assert "temperature" not in reqs[0]            # sampling left to the server unless configured


def test_text_tool_calls_and_think_tags_from_a_server_without_parser(project, clara_home, fake_llama):
    fake_llama.script = [
        {"content": '<think>\nread it\n</think>\nSure.\n<tool_call>\n{"name": "read_file", "arguments": {"path": "hello.txt"}}\n</tool_call>'},
        {"content": "PELICAN"},
    ]
    agent, prov, ui, session = make(project, clara_home, fake_llama)
    out = agent.run("word?")
    assert out == "PELICAN"
    assert "".join(ui.of("reasoning")).strip() == "read it"
    assert ui.text().strip().startswith("Sure.")
    assert session.messages[2]["tool_calls"][0]["function"]["name"] == "read_file"
    assert "<tool_call>" not in session.messages[2]["content"]


def test_silent_truncation_by_the_server_is_reported(project, clara_home, fake_ollama):
    big = "x " * 6000
    (project / "big.txt").write_text(big)
    fake_ollama.script = [
        {"tool_calls": [{"name": "read_file", "arguments": {"path": "big.txt", "limit": 1}}]},
        {"content": "ok", "prompt_tokens": 400},    # far below the ~3000+ tokens the conversation has
    ]
    agent, prov, ui, session = make(project, clara_home, fake_ollama)
    agent.run("read big.txt " * 300)
    warnings = ui.of("warning")
    assert any("truncated the context silently" in w for w in warnings), warnings


def test_server_error_is_surfaced_not_swallowed(project, clara_home, fake_llama):
    fake_llama.script = [{"status": 400, "error": "the request exceeds the available context size, try increasing it"}]
    agent, prov, ui, session = make(project, clara_home, fake_llama)
    out = agent.run("hi")
    assert out == ""
    warnings = ui.of("warning")
    assert warnings and "HTTP 400" in warnings[0] and "context size" in warnings[0]


def test_context_warning_near_the_limit(project, clara_home, fake_llama):
    fake_llama.n_ctx = 1200
    fake_llama.script = [{"content": "short"}]
    agent, prov, ui, session = make(project, clara_home, fake_llama)
    agent.run("w " * 1500)
    assert any("llama-server will refuse" in t for t in ui.of("warning"))


def test_non_streaming_path(project, clara_home, fake_llama):
    fake_llama.script = [{"reasoning": "r", "content": "plain", "tool_calls": [{"name": "list_files", "arguments": {}}]}]
    prov = LocalOpenAIProvider(fake_llama.base_url, model="qwen3.8-27b-q4", timeout=10)
    events = list(prov.stream([{"role": "user", "content": "x"}], default_registry().schemas(), stream=False))
    kinds = [e.kind for e in events]
    assert "tool_call" in kinds and "reasoning" in kinds and events[-1].kind == "done" and events[-1].text == "plain"


def test_audit_log_only_shows_the_model_server(clara_home, fake_llama, project):
    fake_llama.script = [{"content": "hi"}]
    agent, prov, ui, session = make(project, clara_home, fake_llama)
    agent.run("hello")
    entries = [json.loads(l) for l in (clara_home / "netaudit.log").read_text().splitlines()]
    assert entries and {(e["host"], e["port"], e["decision"]) for e in entries} == {("127.0.0.1", fake_llama.port, "allow")}
