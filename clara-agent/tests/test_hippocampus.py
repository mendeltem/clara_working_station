"""The hippocampus keeps what matters (decisions, rules, feelings), lets details go, and sleep fades the unused."""
import json
import time

from clara.hippocampus import Hippocampus, gate, hard_filter, strength
from clara.providers.base import Event


class Scripted:
    """Stand-in model: returns the next scripted reply as the final text."""
    def __init__(self, *replies):
        self.replies = list(replies)
        self.asked = []

    def stream(self, messages, tools, **opts):
        self.asked.append(messages)
        yield Event("done", text=self.replies.pop(0))


CONVO = [{"role": "system", "content": "sys"},
         {"role": "user", "content": "The watches send far too many messages, I only want them once a day in the morning."},
         {"role": "assistant", "content": "Done: they are collected and come with the morning show."}]


def test_hard_filter_blocks_details():
    assert hard_filter("results are in ~/data/work/arena/out.csv") == "path"
    assert hard_filter("run it with `python 01_count.py --force`") == "command"
    assert hard_filter("sub-101 has twelve microbleeds") == "case id"
    assert hard_filter("0.77 / 0.81") is not None
    assert hard_filter("The researcher wants the watches only once a day") is None


def test_gate_importance_and_emotion():
    assert gate({"text": "Tests only ever run on public challenge data", "kind": "rule", "importance": 5})[0]
    assert not gate({"text": "Clara listed the folder contents once", "kind": "fact", "importance": 1, "emotion": 0})[0]
    # emotion lifts a middling memory over the line, like the amygdala
    assert gate({"text": "The researcher was really happy with the dark terminal", "kind": "feeling",
                 "importance": 3, "emotion": 2})[0]
    assert not gate({"text": "The researcher was happy with the dark terminal", "kind": "feeling",
                     "importance": 3, "emotion": 1})[0]
    assert not gate({"text": "Something about the queue state today", "kind": "decision", "importance": 4,
                     "matters_in_a_week": False})[0]
    # live test 06.11.: a lasting preference and a strong feeling were lost; both must stay
    assert gate({"text": "The researcher wants every test result as a short table", "kind": "preference", "importance": 4})[0]
    assert gate({"text": "The researcher was frustrated by the flood of watch messages", "kind": "feeling",
                 "importance": 3, "emotion": 3, "matters_in_a_week": False})[0]


def test_encode_keeps_filters_and_strengthens(tmp_path):
    hc = Hippocampus(tmp_path)
    reply = json.dumps({"new": [
        {"text": "The researcher wants the watch messages only once a day, in the morning show", "kind": "decision",
         "importance": 4, "emotion": 1},
        {"text": "The collection file is ~/system/logs/melden-sammlung.log", "kind": "fact", "importance": 3},
        {"text": "The researcher was annoyed by the many watch messages", "kind": "feeling", "importance": 3, "emotion": 3},
        {"text": "Always test on public data only", "kind": "rule", "importance": 5}]})
    got = hc.encode(Scripted(reply), CONVO)
    assert got == {"kept": 3, "rejected": 1, "strengthened": 0}
    entries = hc.load()
    assert {e["kind"] for e in entries} == {"decision", "feeling", "rule"}
    assert [e for e in entries if e["kind"] == "rule"][0]["pinned"]
    md = (tmp_path / "memory.md").read_text()
    assert md.startswith("# Long-term memory (hippocampus)") and "## Rules (never fade)" in md and "~/" not in md
    # the same thing said again strengthens the old memory instead of adding a line
    again = json.dumps({"new": [{"text": "The researcher wants watch messages only once a day in the morning show",
                                 "kind": "decision", "importance": 4}], "recalled": [], "corrected": []})
    got = hc.encode(Scripted(again), CONVO)
    assert got["kept"] == 0 and got["strengthened"] == 1 and len(hc.load()) == 3


def test_sleep_fades_unused_but_never_rules(tmp_path):
    hc = Hippocampus(tmp_path)
    old = time.time() - 30 * 7 * 86400     # thirty weeks without use
    entries = [
        {"id": "r1", "text": "Never delete the researcher's files", "kind": "rule", "importance": 5, "emotion": 0,
         "pinned": True, "created": old, "last_used": old, "uses": 0},
        {"id": "f1", "text": "A training run was slow one afternoon", "kind": "fact", "importance": 3, "emotion": 0,
         "pinned": False, "created": old, "last_used": old, "uses": 0},
        {"id": "d1", "text": "The CMB arena waits until the paper is out", "kind": "decision", "importance": 4,
         "emotion": 0, "pinned": False, "created": time.time(), "last_used": time.time(), "uses": 2}]
    hc.save(entries)
    res = hc.sleep(None)
    left = {e["id"] for e in hc.load()}
    assert left == {"r1", "d1"} and res["faded"] == 1
    assert hc.search_faded("training run slow")[0]["id"] == "f1"      # faded, not deleted: it can be looked up


def test_sleep_merges_duplicates(tmp_path):
    hc = Hippocampus(tmp_path)
    now = time.time()
    base = {"kind": "preference", "importance": 3, "emotion": 1, "pinned": False, "created": now, "last_used": now, "uses": 1}
    hc.save([dict(base, id="a", text="The researcher likes a dark terminal"),
             dict(base, id="b", text="The researcher prefers dark colours in the terminal")])
    reply = json.dumps({"merge": [{"ids": ["a", "b"], "text": "The researcher likes a dark terminal"}], "shorten": []})
    res = hc.sleep(Scripted(reply))
    entries = hc.load()
    assert res["merged"] == 1 and len(entries) == 1 and entries[0]["uses"] == 2


def test_strength_rules_infinite():
    assert strength({"pinned": True}) > 50
    assert abs(strength({"importance": 2, "emotion": 0, "uses": 0, "last_used": time.time()}) - 2.0) < 1e-6
