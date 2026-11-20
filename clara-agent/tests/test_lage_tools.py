"""09.11.2026: status brief at the start of every conversation context, and the lage / erinnern tools by place."""
import json
import time

from clara.agent import Agent
from clara.config import load_settings
from clara.providers.mock import MockProvider
from clara.sessions import Session
from clara.tools.base import ToolContext, default_registry
from clara.tools.lage_tools import Erinnern, Lage
from tests.test_agent import RecordingUI, make_agent

import os

import pytest

from clara.tools import lage_tools

# the overview code is a separate part of the workstation (CLARA_LAGE_CODE); without it these tests are skipped
pytestmark = pytest.mark.skipif(not os.path.isfile(os.path.join(lage_tools.CODE, "brief.py")),
                                reason="needs the overview code (CLARA_LAGE_CODE)")

NOW = time.strftime("%Y-%m-%d %H:%M")


def board(tmp_path, monkeypatch, kanal="telegram"):
    d = tmp_path / "lage"
    d.mkdir()
    cle = {"ok": True, "filter": "test", "built": NOW, "dropped": 0, "items": [
        {"project": "PVS", "state": "completed", "source": "file", "time": NOW, "trust": 3, "stale": False,
         "kind": "step", "step": "30", "step_name": "where does mood come from", "file": "30_summary.md"},
        {"project": "CMB", "state": "working", "source": "process", "time": NOW, "trust": 3, "stale": False,
         "kind": "session", "title": "Moderation"}]}
    full = {"built": NOW, "items": [dict(cle["items"][0], what="step 30: SECRET-RESULT-0.42", evidence="/x/30_summary.md", id="a"),
                                    dict(cle["items"][1], what="terminal session", evidence="", id="b")], "sources": {}}
    (d / "board_cle.json").write_text(json.dumps(cle))
    (d / "board.json").write_text(json.dumps(full))
    monkeypatch.setenv("LAGE_DIR", str(d))
    monkeypatch.setenv("LAGE_AUDIT_TOOL", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("LAGE_EMBED_URL", "http://127.0.0.1:9/none")
    monkeypatch.setenv("CLARA_LAGE_BRIEF", "1")
    monkeypatch.setenv("CLARA_KANAL", kanal)
    return d


def test_brief_on_the_first_turn_only_and_after_compaction(project, clara_home, tmp_path, monkeypatch):
    board(tmp_path, monkeypatch)
    agent, prov, ui, session = make_agent(project, clara_home, ["first", "second"])
    agent.run("status?")
    first = [m for m in session.messages if m["role"] == "user"][0]["content"]
    assert first.startswith("[background status brief") and "Board as of" in first and "step 30" in first
    assert "SECRET-RESULT" not in first and "(source: file, trust 3)" in first
    agent.run("and now?")
    second = [m for m in session.messages if m["role"] == "user"][1]["content"]
    assert "status brief" not in second
    # a resumed session in a new process (server restart) gets the brief again on its first turn
    loaded = Session.load(session.path)
    a2 = Agent(load_settings(), MockProvider(["third"]), default_registry(), agent.permissions, loaded, RecordingUI(), project)
    a2.run("status after restart?")
    assert "status brief" in [m for m in loaded.messages if m["role"] == "user"][-1]["content"]
    # after a compaction the summary carries a fresh brief
    a3 = Agent(load_settings(), MockProvider(["the summary"]), default_registry(), agent.permissions, Session.load(session.path),
               RecordingUI(), project)
    a3.compact()
    assert "status brief" in a3.messages[1]["content"] and "the summary" in a3.messages[1]["content"]
    audit = (tmp_path / "audit.jsonl").read_text()
    assert "start brief" in audit                     # every Clé-bound text logged first


def test_tools_give_the_cle_view_on_telegram_and_the_full_view_only_locally(tmp_path, monkeypatch):
    board(tmp_path, monkeypatch, kanal="telegram")
    ctx = ToolContext(project_root=tmp_path, home=tmp_path, session_id="t")
    out = Lage().run({"project": "PVS"}, ctx).text
    assert "Clé view" in out and "where does mood come from" in out and "SECRET" not in out and "LAGE-VOLL" not in out
    rec = Erinnern().run({"project": "PVS", "question": "what did step 30 find?"}, ctx).text
    assert "LAGE-VOLL" not in rec and "local session" in rec and "SECRET" not in rec
    monkeypatch.setenv("CLARA_KANAL", "lokal")
    full = Lage().run({"project": "PVS"}, ctx).text
    assert "LAGE-VOLL" in full and "SECRET-RESULT" in full     # the marker keeps the conversation on the local model
    rec = Erinnern().run({"project": "PVS", "question": "what did step 30 find?"}, ctx).text
    assert "LAGE-VOLL" in rec and ("no record" in rec or "PROJECT PAGE" in rec)


def test_brief_can_be_switched_off(project, clara_home, tmp_path, monkeypatch):
    board(tmp_path, monkeypatch)
    monkeypatch.setenv("CLARA_LAGE_BRIEF", "0")
    agent, prov, ui, session = make_agent(project, clara_home, ["x"])
    agent.run("hi")
    assert "status brief" not in [m for m in session.messages if m["role"] == "user"][0]["content"]
