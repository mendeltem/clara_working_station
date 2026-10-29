"""Shared fixtures: a temporary Clara home, a project folder and a fake llama-server.

The fake server speaks the wire format of llama.cpp's llama-server with
--jinja (OpenAI chat completions over SSE, reasoning_content deltas, tool_calls
deltas with index/id/function chunks, a final chunk with usage and timings,
plus /health, /props, /slots, /tokenize). It is a simulation, not the real
binary: tests against it prove Clara's handling of that format, nothing about
the model. The real-model tests live in test_real_model.py.
"""
from __future__ import annotations

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from clara import netguard  # noqa: E402


@pytest.fixture
def clara_home(tmp_path, monkeypatch):
    home = tmp_path / "clara-home"
    monkeypatch.setenv("CLARA_HOME", str(home))
    from clara.config import ensure_home
    ensure_home(home)
    return home


@pytest.fixture
def project(tmp_path):
    p = tmp_path / "proj"
    p.mkdir()
    (p / "hello.txt").write_text("The secret word is PELICAN.\nSecond line.\n", encoding="utf-8")
    (p / "src").mkdir()
    (p / "src" / "app.py").write_text("def add(a, b):\n    return a + b\n\n\ndef main():\n    print(add(1, 2))\n", encoding="utf-8")
    return p


@pytest.fixture(autouse=True)
def guard_reset():
    """Each test starts without the guard; tests that need it install it."""
    netguard.uninstall()
    yield
    netguard.uninstall()


# ------------------------------------------------------------- fake server

class FakeLlamaServer:
    """Scripted llama-server. Set .script to a list of turns before the request.

    A turn is a dict with optional keys:
      reasoning: str            -> streamed as delta.reasoning_content
      content: str              -> streamed as delta.content (may contain <think> or <tool_call> for the Ollama-style variant)
      tool_calls: [{name, arguments}] -> streamed as OpenAI tool_calls deltas (llama-server --jinja style)
      prompt_tokens: int        -> reported in usage (default: computed from the request)
      status: int, error: str   -> answer with an HTTP error instead
    """

    def __init__(self, n_ctx: int = 8192, model_id: str = "qwen3.8-27b-q4", kind: str = "llama"):
        self.n_ctx = n_ctx
        self.model_id = model_id
        self.kind = kind
        self.script: list[dict] = []
        self.requests: list[dict] = []
        self.sleeping = False
        self.lock = threading.Lock()
        server = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _json(self, code, data):
                body = json.dumps(data).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/health":
                    return self._json(200, {"status": "ok"})
                if self.path == "/props" and server.kind == "llama":
                    return self._json(200, {"default_generation_settings": {"n_ctx": server.n_ctx},
                                            "model_path": "/models/fake.gguf", "build_info": "b11874-fake",
                                            "is_sleeping": server.sleeping})
                if self.path == "/slots" and server.kind == "llama":
                    return self._json(200, [{"id": 0, "is_processing": False}])
                if self.path == "/v1/models":
                    return self._json(200, {"data": [{"id": server.model_id}]})
                if self.path == "/api/version" and server.kind == "ollama":
                    return self._json(200, {"version": "0.99-fake"})
                return self._json(404, {"error": "not found"})

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                if self.path == "/tokenize" and server.kind == "llama":
                    return self._json(200, {"tokens": list(range(len(str(body.get("content", "")).split())))})
                if self.path != "/v1/chat/completions":
                    return self._json(404, {"error": "not found"})
                with server.lock:
                    server.requests.append(body)
                    turn = server.script.pop(0) if server.script else {"content": "nothing scripted"}
                if turn.get("status"):
                    return self._json(turn["status"], {"error": {"code": turn["status"], "message": turn.get("error", "error")}})
                prompt_tokens = turn.get("prompt_tokens") or sum(len(json.dumps(m)) // 4 for m in body.get("messages", []))
                if not body.get("stream"):
                    msg = {"role": "assistant", "content": turn.get("content", "")}
                    if turn.get("reasoning"):
                        msg["reasoning_content"] = turn["reasoning"]
                    if turn.get("tool_calls"):
                        msg["tool_calls"] = [{"id": f"call{i}", "type": "function",
                                              "function": {"name": c["name"], "arguments": json.dumps(c["arguments"])}}
                                             for i, c in enumerate(turn["tool_calls"])]
                    return self._json(200, {"choices": [{"message": msg, "finish_reason": "tool_calls" if turn.get("tool_calls") else "stop"}],
                                            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": 10}})
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()

                def chunk(delta, finish=None, extra=None):
                    d = {"choices": [{"index": 0, "delta": delta, "finish_reason": finish}], "object": "chat.completion.chunk",
                         "model": server.model_id}
                    if extra:
                        d.update(extra)
                    self.wfile.write(b"data: " + json.dumps(d).encode() + b"\n\n")
                    self.wfile.flush()

                for i in range(0, len(turn.get("reasoning", "")), 5):
                    chunk({"reasoning_content": turn["reasoning"][i:i + 5]})
                content = turn.get("content", "")
                for i in range(0, len(content), 4):
                    chunk({"content": content[i:i + 4]})
                calls = turn.get("tool_calls") or []
                for idx, c in enumerate(calls):
                    args = json.dumps(c["arguments"])
                    chunk({"tool_calls": [{"index": idx, "id": f"call{idx}", "type": "function", "function": {"name": c["name"], "arguments": ""}}]})
                    for j in range(0, len(args), 6):
                        chunk({"tool_calls": [{"index": idx, "function": {"arguments": args[j:j + 6]}}]})
                chunk({}, finish="tool_calls" if calls else "stop",
                      extra={"usage": {"prompt_tokens": prompt_tokens, "completion_tokens": 20, "total_tokens": prompt_tokens + 20},
                             "timings": {"prompt_n": 12, "prompt_per_second": 400.0, "predicted_n": 20, "predicted_per_second": 30.0}})
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def fake_llama():
    s = FakeLlamaServer()
    yield s
    s.close()


@pytest.fixture
def fake_ollama():
    s = FakeLlamaServer(kind="ollama", model_id="qwen3.6:35b")
    yield s
    s.close()
