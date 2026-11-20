"""Stability tests (10.11.2026, from the check in ergebnisse/clara-check/2026-11-10): the job text survives summaries,
provider errors are retried by a table, a round that is summarised again and again stops, a session that was killed
between a tool call and its result loads cleanly, and the server tells a restart and a dead question apart.
Scripted model, temporary Clara home; nothing here touches the live system."""
import json
import threading
import time

import pytest

import clara.agent as agent_mod
import clara.server as srv
from clara.agent import Agent, RAILS_NOTE, SUMMARY_MARK, TASK_MARK
from clara.config import load_settings
from clara.providers.base import ProviderError
from clara.providers.mock import MockProvider
from clara.sessions import INTERRUPTED, Session

from tests.test_agent import make_agent
from tests.test_server_api import api  # noqa: F401 -- fixture

JOB = "JOB wk-test: write results/table.csv. ACCEPTANCE: file exists and contains the token KOLIBRI-7731."


class Flaky(MockProvider):
    """Raises the given errors on the first requests, then follows the script."""

    def __init__(self, errors, script):
        super().__init__(script)
        self.errors = list(errors)
        self.failed = 0

    def stream(self, messages, tools, **opts):
        if self.errors:
            self.failed += 1
            raise ProviderError(self.errors.pop(0))
        yield from super().stream(messages, tools, **opts)


def flaky_agent(project, clara_home, errors, script):
    agent, prov, ui, session = make_agent(project, clara_home, script)
    agent.provider = flaky = Flaky(errors, script)
    return agent, flaky, ui, session


# ------------------------------------------------------------------ 1 the job text survives summaries
def test_job_text_survives_three_summaries(project, clara_home):
    a, prov, ui, s = make_agent(project, clara_home, ["working on it", "summary one", "still working", "summary two", "more",
                                                      "summary three", "final"])
    a.run(JOB)
    for _ in range(3):
        a.compact()
        a.run("continue")
    sent = json.dumps(prov.calls[-1]["messages"])
    assert "KOLIBRI-7731" in sent, "the job text (acceptance criteria) is gone from what the model sees"
    first = a.messages[1]["content"]
    assert first.startswith(TASK_MARK) and first.count("KOLIBRI-7731") == 1            # kept once, not piled up
    assert "summary three" in first and "summary one" not in first                    # the newest summary replaces the old


def test_summary_request_sees_the_task_and_is_told_not_to_repeat_it(project, clara_home):
    a, prov, ui, s = make_agent(project, clara_home, ["working", "the summary"])
    a.run(JOB)
    a.compact()
    asked = prov.calls[-1]["messages"]
    assert asked[1]["content"].startswith(TASK_MARK) and "KOLIBRI-7731" in asked[1]["content"]
    assert "do not repeat it" in asked[-1]["content"]
    assert len(a.messages) == 2 and SUMMARY_MARK + "the summary" in a.messages[1]["content"]


def test_resumed_session_keeps_the_pinned_task(project, clara_home):
    a, prov, ui, s = make_agent(project, clara_home, ["working", "summary one"])
    a.run(JOB)
    a.compact()
    loaded = Session.load(s.path)
    a2 = Agent(load_settings(), MockProvider(["more work", "summary two"]), a.tools, a.permissions, loaded, ui, project)
    a2.run("continue")
    a2.compact()
    assert "KOLIBRI-7731" in a2.messages[1]["content"] and "summary two" in a2.messages[1]["content"]


def test_chat_and_old_sessions_pin_nothing(project, clara_home, monkeypatch):
    a, prov, ui, s = make_agent(project, clara_home, ["hello", "chat summary"])
    monkeypatch.setattr(a, "_is_chat", lambda: True)
    a.run("good morning")
    a.compact()
    assert a.messages[1]["content"].startswith(SUMMARY_MARK)
    b, prov, ui, s = make_agent(project, clara_home, ["ok", "new summary"])
    b.session.append({"role": "user", "content": SUMMARY_MARK + "old summary from before the change"})
    b.session.append({"role": "assistant", "content": "ok"})
    b.compact()
    assert b.messages[1]["content"].startswith(SUMMARY_MARK)


def test_a_very_long_first_message_is_capped(project, clara_home):
    a, prov, ui, s = make_agent(project, clara_home, ["working", "the summary"])
    a.run("HEAD-TOKEN " + "x" * 80_000 + " TAIL-TOKEN")
    a.compact()
    first = a.messages[1]["content"]
    assert "HEAD-TOKEN" in first and "TAIL-TOKEN" in first and len(first) < agent_mod.PIN_MAX + 2000


# ------------------------------------------------------------------ 2 errors around the summary and the request
def test_summary_request_error_does_not_crash(project, clara_home):
    a, prov, ui, s = make_agent(project, clara_home, ["first answer"])
    a.run(JOB)
    before = list(a.messages)

    def boom(*x, **k):
        raise ProviderError("HTTP 401 unauthorized")
        yield

    prov.stream = boom
    out = a.compact()
    assert out.startswith("the model returned no summary") and a.messages == before
    assert any(e[0] == "warning" and "summary request failed" in e[1] for e in ui.events)


RAILS = "server error: {'message': 'Blocked by output rails.', 'type': 'guardrails_violation', 'code': 'content_blocked'}"


@pytest.mark.parametrize("error, times, answered, requests", [
    ("HTTP 503 from http://x/v1: busy", 1, True, 2),
    ("HTTP 429 from http://x/v1: slow down", 3, True, 4),
    ("HTTP 500 from http://x/v1: oops", 4, False, 4),                        # three more tries, then it gives up
    ("HTTP 401 from http://x/v1: unauthorized", 1, True, 2),
    ("HTTP 401 from http://x/v1: unauthorized", 2, False, 2),                # a wrong key does not heal by waiting
    ("HTTP 400 from http://x/v1: bad request", 1, False, 1),                 # never retried
    ("HTTP 500 from http://x/v1: weiche: local model could not be started", 1, False, 1),   # the relay refused on purpose
    ("cannot reach http://x/v1: Remote end closed connection without response", 2, True, 3),
    ("cannot reach http://x/v1: timed out", 2, False, 2),                    # a timeout is tried once more only
    (RAILS, 2, True, 3),
    (RAILS, 3, False, 3),
])
def test_provider_error_table(project, clara_home, monkeypatch, error, times, answered, requests):
    monkeypatch.setattr(agent_mod, "RETRY_WAITS", (0, 0, 0))
    a, prov, ui, s = flaky_agent(project, clara_home, [error] * times, ["the answer"])
    out = a.run("do it")
    assert (out == "the answer") == answered
    assert prov.failed + len(prov.calls) == requests
    if not answered:
        assert any(e[0] == "warning" and "model server" in e[1] for e in ui.events)
    if error is RAILS:
        assert sum(1 for m in a.messages if m.get("content") == RAILS_NOTE) == min(times, agent_mod.RAILS_MAX)


def test_retry_wait_ends_on_interrupt(project, clara_home, monkeypatch):
    monkeypatch.setattr(agent_mod, "RETRY_WAITS", (60, 60, 60))
    a, prov, ui, s = flaky_agent(project, clara_home, ["HTTP 503 from http://x/v1: busy"], ["never"])
    threading.Timer(0.3, a.interrupt).start()
    t0 = time.time()
    assert a.run("do it") == "" and time.time() - t0 < 10
    assert a.messages[-1]["content"] == "[interrupted by user]"


# ------------------------------------------------------------------ 3 summaries again and again
def test_compact_limit_moves_up_when_the_fixed_part_is_big(project, clara_home):
    a, prov, ui, s = make_agent(project, clara_home, [])
    a.settings.compact_at = 32000
    assert a._compact_limit() == 32000
    a._floor = 27000
    assert a._compact_limit() == 40000                  # fixed part + half would be 43000; capped a quarter above
    a.n_ctx = 40000
    assert a._compact_limit() == 32000                  # never past 80 % of a known context
    a._floor, a.n_ctx = 8000, None
    assert a._compact_limit() == 32000


def test_summary_storm_gets_a_note_and_then_stops_the_round(project, clara_home, monkeypatch):
    monkeypatch.setenv("CLARA_MAX_COMPACTS", "2")
    look = lambda i: {"tool_calls": [{"name": "read_file", "arguments": {"path": f"f{i}.txt"}}]}
    # compact_at = 1: a summary is due at every step. Step 0: the only message is the job (nothing to summarise, 5 steps
    # pause); step 6: summary 1, then one more look; step 7: summary 2, one step after the first -> note, then stop
    script = [look(i) for i in range(6)] + ["summary one", look(6), "summary two", "HAND-OVER\nOPEN: table.csv", "never asked"]
    a, prov, ui, s = make_agent(project, clara_home, script)
    a.settings.compact_at = 1
    out = a.run(JOB)
    assert out.startswith("[stopped: 2 summaries in this round") and out.endswith("HAND-OVER\nOPEN: table.csv")
    assert "this is summary 2 of this round" in a.messages[1]["content"] and "KOLIBRI-7731" in a.messages[1]["content"]
    assert prov.script == ["never asked"]
    notes = [json.loads(l) for l in s.path.read_text().splitlines()]
    assert any(n.get("kind") == "summary_storm" for n in notes)


# ------------------------------------------------------------------ 4 a turn killed between tool call and result
def dangling(s):
    s.append({"role": "assistant", "content": "", "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}},
        {"id": "c2", "type": "function", "function": {"name": "bash", "arguments": "{}"}}]})
    s.append({"role": "tool", "tool_call_id": "c1", "name": "read_file", "content": "PELICAN"})


def test_load_closes_a_tool_call_without_result(project, clara_home):
    a, prov, ui, s = make_agent(project, clara_home, ["ok"])
    a.run("hi")
    dangling(s)
    s.append({"role": "user", "content": "are you there?"})
    loaded = Session.load(s.path)
    roles = [(m["role"], m.get("tool_call_id")) for m in loaded.messages[-4:]]
    assert roles == [("assistant", None), ("tool", "c1"), ("tool", "c2"), ("user", None)]
    assert loaded.messages[-2]["content"] == INTERRUPTED and loaded.messages[-2]["name"] == "bash"
    assert [m.get("tool_call_id") for m in Session.load(s.path, full=True).messages[-3:]] == [None, "c1", None]   # raw file as it is
    assert Session.load(s.path).messages == loaded.messages                 # the same on every load


def test_resumed_session_with_open_call_takes_the_next_message(project, clara_home):
    a, prov, ui, s = make_agent(project, clara_home, ["ok"])
    a.run("hi")
    dangling(s)
    prov2 = MockProvider(["back again"])
    a2 = Agent(load_settings(), prov2, a.tools, a.permissions, Session.load(s.path), ui, project)
    assert a2.run("continue") == "back again"
    sent = prov2.calls[-1]["messages"]
    ids = {c["id"] for m in sent for c in (m.get("tool_calls") or [])}
    assert ids == {"c1", "c2"} and ids <= {m.get("tool_call_id") for m in sent if m.get("role") == "tool"}


# ------------------------------------------------------------------ 5 server: dead questions and restarts
def test_timed_out_question_is_not_left_pending(project, clara_home, monkeypatch):
    monkeypatch.setattr(srv, "PERMISSION_TIMEOUT", 0.2)
    app = srv.App(load_settings(), MockProvider([]), project, token="t")
    ss = app.new_session(str(project))
    assert ss.ui.ask_user("which one?") is None    # nobody answers
    assert ss.snapshot()["pending"] == [], "a dead question is still offered to the phone"


def test_answer_to_a_dead_question_is_410_and_unknown_is_404(api, monkeypatch):  # noqa: F811
    monkeypatch.setattr(srv, "PERMISSION_TIMEOUT", 0.2)
    sid = api.call("POST", "/api/sessions", {})[1]["id"]
    ss = api.app.get(sid)
    assert ss.ui.ask_user("which one?") is None
    rid = [e for e in ss.events if e["kind"] == "question"][-1]["id"]
    code, data = api.call("POST", f"/api/answers/{rid}", {"answer": "the second"})
    assert code == 410 and data.get("expired") is True
    assert api.call("POST", "/api/answers/nosuchid00", {"answer": "x"})[0] == 404


def test_event_numbers_keep_growing_across_a_restart(project, clara_home):
    st = load_settings()
    app1 = srv.App(st, MockProvider(["hello"]), project, token="t")
    ss = app1.new_session(str(project))
    ss.start("hi")
    for _ in range(100):
        if not ss.snapshot()["busy"]:
            break
        time.sleep(0.05)
    snap1 = ss.snapshot()
    time.sleep(0.01)
    app2 = srv.App(st, MockProvider(["again"]), project, token="t")           # "restart": the session comes back from disk
    ss2 = app2.get(snap1["id"])
    snap2 = ss2.snapshot()
    assert snap2["seq"] >= snap1["seq"] and snap2["boot"] != snap1["boot"]
    ss2.start("still there?")
    got = ss2.since(snap1["seq"], timeout=5)                                # a client that remembers the old number
    assert got and any(e["kind"] == "user" for e in got)


# ---------------------------------------------------------------- text that is not UTF-8 (wk11a round 2, 10.11.2026)

def test_a_prompt_with_broken_bytes_does_not_end_the_round(project, clara_home):
    """A text cut in the middle of a character arrives from the command line with lone surrogates."""
    broken = b"critic point: the lower margin is +0.\xe2\x80".decode("utf-8", "surrogateescape")
    agent, prov, ui, session = make_agent(project, clara_home, ["done"])
    assert agent.run(broken) == "done"
    first = next(m["content"] for m in session.messages if m["role"] == "user")
    first.encode("utf-8")                                   # can be sent
    assert "critic point" in first and "\ufffd" in first
    again = Session.load(session.path)                      # and was written
    assert any("critic point" in str(m.get("content")) for m in again.messages)


# ------------------------------------------------------------------ 7 a round that ends without an answer leaves a hand-over
def test_step_limit_in_a_job_asks_for_a_hand_over(project, clara_home):
    look = {"tool_calls": [{"name": "list_files", "arguments": {}}]}
    a, prov, ui, s = make_agent(project, clara_home, [look, look, look, "HAND-OVER\nDONE: nothing\nOPEN: results/table.csv"],
                                max_turns=3)
    out = a.run(JOB)
    assert out.startswith("[no answer: stopped after 3 model steps") and "OPEN: results/table.csv" in out
    last = prov.calls[-1]
    assert last["tools"] is None and "this round ends here" in last["messages"][-1]["content"]
    assert "KOLIBRI-7731" in json.dumps(last["messages"])                 # the model writes it with the job text in view
    assert a.messages[-1]["role"] == "assistant" and "HAND-OVER" in a.messages[-1]["content"]


def test_the_chat_gets_no_hand_over_request(project, clara_home, monkeypatch):
    look = {"tool_calls": [{"name": "list_files", "arguments": {}}]}
    a, prov, ui, s = make_agent(project, clara_home, [look, look, look, "never asked"], max_turns=3)
    monkeypatch.setattr(a, "_is_chat", lambda: True)
    assert a.run("good morning") == ""
    assert prov.script == ["never asked"] and any("max_turns" in t for t in ui.of("warning"))


def test_a_failed_hand_over_request_changes_nothing(project, clara_home):
    look = {"tool_calls": [{"name": "list_files", "arguments": {}}]}
    a, prov, ui, s = make_agent(project, clara_home, [look, look, look], max_turns=3)
    real = prov.stream

    def stream(messages, tools, **opts):
        if tools is None:
            raise ProviderError("HTTP 503 from the model server")
        yield from real(messages, tools, **opts)

    prov.stream = stream
    assert a.run(JOB) == ""
    assert any(n.get("kind") == "error" and "hand-over" in n.get("text", "")
               for n in (json.loads(l) for l in s.path.read_text().splitlines()))


def test_one_shot_writes_the_last_words_to_the_hand_over_file(project, clara_home, tmp_path):
    from tests.test_cli_and_misc import run_cli
    out = tmp_path / "handover.txt"
    r = run_cli(["--mock", "-p", "show me the project"], project, {"CLARA_HOME": str(clara_home), "CLARA_HANDOVER_FILE": str(out)})
    assert r.returncode == 0 and out.read_text().strip() == r.stdout.strip() != ""
