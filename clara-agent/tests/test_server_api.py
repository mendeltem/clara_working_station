"""The phone-app API: token, sessions, streaming events, approvals and questions from the phone."""
import json
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from clara.config import load_settings
from clara.providers.mock import MockProvider
from clara.server import App, Handler, ensure_icons


@pytest.fixture
def api(project, clara_home):
    settings = load_settings()
    prov = MockProvider([])
    ensure_icons()
    app = App(settings, prov, project, token="t0k3n")
    Handler.app = app
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"

    def call(method, path, body=None, token="t0k3n", raw=False):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(base + path, data=data, method=method)
        if token:
            req.add_header("Authorization", "Bearer " + token)
        if data:
            req.add_header("Content-Type", "application/json")
        try:
            resp = urllib.request.urlopen(req, timeout=10)
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")
        payload = resp.read()
        return resp.status, (payload if raw else json.loads(payload or b"{}"))

    def events(sid, since=0, want=None, timeout=10):
        """Read SSE until an event of kind `want` (or idle) arrives. The stream needs a ticket."""
        out = []
        deadline = time.time() + timeout
        ticket = call("POST", f"/api/sessions/{sid}/ticket", {})[1]["ticket"]
        req = urllib.request.Request(f"{base}/api/sessions/{sid}/events?since={since}&ticket={ticket}")
        resp = urllib.request.urlopen(req, timeout=timeout)
        for line in resp:
            if time.time() > deadline:
                break
            line = line.decode().strip()
            if line.startswith("data:"):
                ev = json.loads(line[5:])
                out.append(ev)
                if ev["kind"] == (want or "idle") or (want and ev["kind"] == "idle"):
                    break
        resp.close()
        return out

    yield type("Api", (), {"call": staticmethod(call), "events": staticmethod(events), "prov": prov, "base": base, "app": app})
    httpd.shutdown()


def test_token_required(api):
    assert api.call("GET", "/api/status", token=None)[0] == 401
    assert api.call("GET", "/api/status", token="wrong")[0] == 401
    assert api.call("GET", "/api/status?token=t0k3n", token=None)[0] == 401     # never in the URL
    code, data = api.call("GET", "/api/status")
    assert code == 200 and data["model"] == "mock" and "sandbox" in data


def test_stream_ticket_is_single_use_and_token_in_url_is_refused(api):
    sid = api.call("POST", "/api/sessions", {})[1]["id"]
    assert api.call("GET", f"/api/sessions/{sid}/events?token=t0k3n", token=None)[0] == 401
    assert api.call("GET", f"/api/sessions/{sid}/events", token="t0k3n")[0] == 401     # bearer alone is not enough either
    ticket = api.call("POST", f"/api/sessions/{sid}/ticket", {})[1]["ticket"]
    assert api.call("GET", f"/api/sessions/other/events?ticket={ticket}", token=None)[0] in (401, 404)
    req = urllib.request.Request(f"{api.base}/api/sessions/{sid}/events?since=0&ticket={ticket}")
    resp = urllib.request.urlopen(req, timeout=5)
    assert resp.status == 200
    resp.close()
    assert api.call("GET", f"/api/sessions/{sid}/events?ticket={ticket}", token=None)[0] == 401   # used


def test_cross_site_posts_and_huge_bodies_are_refused(api):
    req = urllib.request.Request(f"{api.base}/api/sessions", data=b"{}", method="POST")
    req.add_header("Authorization", "Bearer t0k3n")
    req.add_header("Content-Type", "application/json")
    req.add_header("Origin", "http://evil.example")
    try:
        urllib.request.urlopen(req, timeout=5)
        assert False, "cross-site POST accepted"
    except urllib.error.HTTPError as exc:
        assert exc.code == 403
    req = urllib.request.Request(f"{api.base}/api/sessions", data=b"{}", method="POST")
    req.add_header("Authorization", "Bearer t0k3n")
    req.add_header("Content-Type", "application/json")
    req.add_header("Origin", "http://" + api.base.split("//")[1])
    assert urllib.request.urlopen(req, timeout=5).status == 201                      # same origin passes
    req = urllib.request.Request(f"{api.base}/api/sessions", data=b"{}", method="POST")
    req.add_header("Authorization", "Bearer t0k3n")
    req.add_header("Content-Length", str(50 * 1024 * 1024))
    try:
        urllib.request.urlopen(req, timeout=5)
        assert False, "huge body accepted"
    except urllib.error.HTTPError as exc:
        assert exc.code == 413


def test_static_app_files_served_without_token(api):
    for path, ctype in (("/", b"<!doctype html>"), ("/manifest.webmanifest", b'"name": "Clara"'), ("/sw.js", b"CACHE"),
                        ("/icon-192.png", b"\x89PNG")):
        code, body = api.call("GET", path, token=None, raw=True)
        assert code == 200 and body.startswith(ctype) or ctype in body, path


def test_message_streams_and_tool_cards_appear(api, project):
    api.prov.script[:] = [{"tool_calls": [{"name": "read_file", "arguments": {"path": "hello.txt"}}]}, "PELICAN it is."]
    code, data = api.call("POST", "/api/sessions", {})
    assert code == 201
    sid = data["id"]
    code, _ = api.call("POST", f"/api/sessions/{sid}/messages", {"text": "secret word?"})
    assert code == 202
    evs = api.events(sid)
    kinds = [e["kind"] for e in evs]
    assert kinds[0] == "user" and "tool_start" in kinds and "tool_result" in kinds and kinds[-1] == "idle"
    assert "".join(e.get("delta", "") for e in evs if e["kind"] == "text") == "PELICAN it is."
    code, snap = api.call("GET", f"/api/sessions/{sid}")
    assert snap["messages"][-1]["content"] == "PELICAN it is." and snap["busy"] is False
    code, lst = api.call("GET", "/api/sessions")
    assert any(s["id"] == sid for s in lst["sessions"])


def test_approval_from_the_phone_gates_a_write(api, project):
    api.prov.script[:] = [{"tool_calls": [{"name": "write_file", "arguments": {"path": "phone.txt", "content": "from phone"}}]}, "written"]
    sid = api.call("POST", "/api/sessions", {})[1]["id"]
    api.call("POST", f"/api/sessions/{sid}/messages", {"text": "write it"})
    evs = api.events(sid, want="permission")
    perm = [e for e in evs if e["kind"] == "permission"][0]
    assert perm["tool"] == "write_file" and perm["preview"] == "from phone"
    assert not (project / "phone.txt").exists()                     # the agent waits
    snap = api.call("GET", f"/api/sessions/{sid}")[1]
    assert snap["busy"] and snap["pending"][0]["kind"] == "permission"
    assert api.call("POST", f"/api/permissions/{perm['id']}", {"answer": "nonsense"})[0] == 400
    assert api.call("POST", f"/api/permissions/{perm['id']}", {"answer": "allow"})[0] == 200
    evs = api.events(sid, since=perm["seq"])
    assert (project / "phone.txt").read_text() == "from phone"
    assert evs[-1]["kind"] == "idle"
    assert api.call("POST", f"/api/permissions/{perm['id']}", {"answer": "allow"})[0] == 404   # resolved once


def test_denial_from_the_phone(api, project):
    api.prov.script[:] = [{"tool_calls": [{"name": "bash", "arguments": {"command": "touch nope.txt"}}]}, "ok"]
    sid = api.call("POST", "/api/sessions", {})[1]["id"]
    api.call("POST", f"/api/sessions/{sid}/messages", {"text": "touch"})
    perm = [e for e in api.events(sid, want="permission") if e["kind"] == "permission"][0]
    api.call("POST", f"/api/permissions/{perm['id']}", {"answer": "deny"})
    evs = api.events(sid, since=perm["seq"])
    assert not (project / "nope.txt").exists()
    result = [e for e in evs if e["kind"] == "tool_result"][0]
    assert result["decision"] == "denied"


def test_question_answered_from_the_phone(api):
    api.prov.script[:] = [{"tool_calls": [{"name": "ask_user", "arguments": {"question": "Which file?"}}]}, "thanks"]
    sid = api.call("POST", "/api/sessions", {})[1]["id"]
    api.call("POST", f"/api/sessions/{sid}/messages", {"text": "edit"})
    q = [e for e in api.events(sid, want="question") if e["kind"] == "question"][0]
    assert q["question"] == "Which file?"
    api.call("POST", f"/api/answers/{q['id']}", {"answer": "app.py"})
    api.events(sid, since=q["seq"])
    snap = api.call("GET", f"/api/sessions/{sid}")[1]
    assert any("User answered: app.py" in m["content"] for m in snap["messages"] if m["role"] == "tool")


def test_busy_session_rejects_second_message_and_mode_switch_works(api):
    api.prov.script[:] = [{"tool_calls": [{"name": "write_file", "arguments": {"path": "a.txt", "content": "1"}}]}, "ok", "ok2"]
    sid = api.call("POST", "/api/sessions", {})[1]["id"]
    api.call("POST", f"/api/sessions/{sid}/messages", {"text": "go"})
    perm = [e for e in api.events(sid, want="permission") if e["kind"] == "permission"][0]
    assert api.call("POST", f"/api/sessions/{sid}/messages", {"text": "again"})[0] == 409
    assert api.call("POST", f"/api/sessions/{sid}/mode", {"mode": "nope"})[0] == 400
    assert api.call("POST", f"/api/sessions/{sid}/mode", {"mode": "accept-edits"})[1]["mode"] == "accept-edits"
    api.call("POST", f"/api/permissions/{perm['id']}", {"answer": "allow"})
    api.events(sid, since=perm["seq"])
    assert api.call("GET", f"/api/sessions/{sid}")[1]["mode"] == "accept-edits"


def test_interrupt_endpoint(api):
    api.prov.script[:] = [{"tool_calls": [{"name": "bash", "arguments": {"command": "sleep 5"}}]}, "late"]
    sid = api.call("POST", "/api/sessions", {})[1]["id"]
    api.call("POST", f"/api/sessions/{sid}/messages", {"text": "sleep"})
    perm = [e for e in api.events(sid, want="permission") if e["kind"] == "permission"][0]
    assert api.call("POST", f"/api/sessions/{sid}/interrupt", {})[0] == 200
    api.call("POST", f"/api/permissions/{perm['id']}", {"answer": "deny"})
    evs = api.events(sid, since=perm["seq"])
    assert evs[-1]["kind"] == "idle"
    snap = api.call("GET", f"/api/sessions/{sid}")[1]
    assert snap["messages"][-1]["content"] == "[interrupted by user]"
