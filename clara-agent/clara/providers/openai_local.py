"""OpenAI-compatible client for a local server (llama-server, Ollama, LM Studio, vLLM).

Standard library only. Proxies from the environment are ignored on purpose so a
request to 127.0.0.1 can never be routed through an HTTP proxy. The netguard
still checks the host of every connection.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Iterator

from ..toolcalls import StreamRouter, ToolCall, _loads_lenient
from .base import Event, Provider, ProviderError


def _opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


class LocalOpenAIProvider(Provider):
    name = "openai-local"

    def __init__(self, base_url: str, model: str = "", timeout: float = 600.0,
                 temperature: float | None = None, extra_body: dict | None = None):
        self.base_url = base_url.rstrip("/")
        self.root = self.base_url[:-3] if self.base_url.endswith("/v1") else self.base_url
        self.model = model
        self.timeout = timeout
        self.temperature = temperature
        self.extra_body = extra_body or {}
        self._props_cache: dict | None = None

    # ------------------------------------------------------------- transport
    def _request(self, method: str, url: str, body: dict | None = None, timeout: float | None = None,
                 stream: bool = False):
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            resp = _opener().open(req, timeout=timeout or self.timeout)
        except urllib.error.HTTPError as exc:
            snippet = ""
            try:
                snippet = exc.read(2000).decode("utf-8", "replace")
            except Exception:
                pass
            raise ProviderError(f"HTTP {exc.code} from {url}: {snippet.strip() or exc.reason}") from None
        except urllib.error.URLError as exc:
            raise ProviderError(f"cannot reach {url}: {exc.reason}") from None
        except OSError as exc:
            raise ProviderError(f"cannot reach {url}: {exc}") from None
        if stream:
            return resp
        try:
            raw = resp.read()
        finally:
            resp.close()
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            raise ProviderError(f"non-JSON answer from {url}: {raw[:200]!r}")

    def get_json(self, path: str, timeout: float = 10.0):
        url = path if path.startswith("http") else self.root + path
        return self._request("GET", url, timeout=timeout)

    # ----------------------------------------------------- server introspection
    def models(self) -> list[str]:
        data = self.get_json(self.base_url + "/models")
        items = data.get("data", []) if isinstance(data, dict) else []
        return [m.get("id", "") for m in items if isinstance(m, dict)]

    def health(self) -> dict:
        """llama-server /health (served while the model sleeps, does not wake it)."""
        try:
            return self.get_json("/health", timeout=5)
        except ProviderError as exc:
            return {"status": "error", "error": str(exc)}

    def props(self) -> dict | None:
        try:
            p = self.get_json("/props", timeout=5)
            if isinstance(p, dict):
                self._props_cache = p
                return p
        except ProviderError:
            pass
        return None

    def slots(self) -> list[dict] | None:
        try:
            s = self.get_json("/slots", timeout=5)
            return s if isinstance(s, list) else None
        except ProviderError:
            return None

    def context_size(self) -> int | None:
        p = self._props_cache or self.props()
        if not p:
            return None
        gen = p.get("default_generation_settings") or {}
        n = gen.get("n_ctx") or p.get("n_ctx")
        return int(n) if n else None

    def count_tokens(self, text: str) -> int | None:
        try:
            data = self._request("POST", self.root + "/tokenize", {"content": text}, timeout=30)
        except ProviderError:
            return None
        toks = data.get("tokens") if isinstance(data, dict) else None
        return len(toks) if isinstance(toks, list) else None

    def detect(self) -> dict:
        """What is listening at base_url. Everything here is read-only and, for
        llama-server, served during sleep (no model wake-up)."""
        info: dict = {"base_url": self.base_url, "kind": "unknown", "models": [], "n_ctx": None}
        try:
            info["models"] = self.models()
            info["kind"] = "openai-compatible"
        except ProviderError as exc:
            info["error"] = str(exc)
            return info
        p = self.props()
        if p:
            info["kind"] = "llama-server"
            info["n_ctx"] = self.context_size()
            info["sleeping"] = p.get("is_sleeping")
            info["model_path"] = p.get("model_path")
            info["build"] = p.get("build_info")
            slots = self.slots()
            if slots is not None:
                info["slots_busy"] = sum(1 for s in slots if s.get("is_processing"))
                info["slots_total"] = len(slots)
        else:
            try:
                v = self.get_json("/api/version", timeout=5)
                if isinstance(v, dict) and "version" in v:
                    info["kind"] = "ollama"
                    info["ollama_version"] = v["version"]
            except ProviderError:
                pass
        return info

    def wait_until_up(self, seconds: float, status_cb=None) -> bool:
        deadline = time.time() + seconds
        while True:
            try:
                self.models()
                return True
            except ProviderError as exc:
                if status_cb:
                    status_cb(f"waiting for model server: {exc}")
            if time.time() >= deadline:
                return False
            time.sleep(min(5.0, max(0.5, deadline - time.time())))

    # ---------------------------------------------------------------- chat
    def _body(self, messages: list[dict], tools: list[dict] | None, stream: bool, opts: dict) -> dict:
        body: dict = {"model": self.model, "messages": messages, "stream": stream}
        if stream:
            body["stream_options"] = {"include_usage": True}
        if tools:
            body["tools"] = tools
        temp = opts.get("temperature", self.temperature)
        if temp is not None:
            body["temperature"] = temp
        if opts.get("max_tokens"):
            body["max_tokens"] = int(opts["max_tokens"])
        body.update(self.extra_body)
        return body

    def stream(self, messages: list[dict], tools: list[dict] | None, **opts) -> Iterator[Event]:
        if not self.model:
            raise ProviderError("no model name set; run `clara doctor` or set [model].model")
        if opts.get("stream") is False:
            yield from self._complete(messages, tools, opts)
            return
        body = self._body(messages, tools, True, opts)
        resp = self._request("POST", self.base_url + "/chat/completions", body, stream=True)
        router = StreamRouter()
        native: dict[int, dict] = {}
        finish_reason = None
        usage: dict = {}
        timings: dict = {}
        try:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line or not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                if "error" in chunk and not chunk.get("choices"):
                    raise ProviderError(f"server error: {chunk['error']}")
                if chunk.get("usage"):
                    usage = chunk["usage"]
                if chunk.get("timings"):
                    timings = chunk["timings"]
                for choice in chunk.get("choices") or []:
                    delta = choice.get("delta") or {}
                    if choice.get("finish_reason"):
                        finish_reason = choice["finish_reason"]
                    r = delta.get("reasoning_content") or delta.get("reasoning")
                    if r:
                        router.all_reasoning += r
                        yield Event("reasoning", r)
                    c = delta.get("content")
                    if c:
                        for kind, text in router.push(c):
                            yield Event(kind, text)
                    for tc in delta.get("tool_calls") or []:
                        idx = int(tc.get("index", len(native)))
                        slot = native.setdefault(idx, {"id": tc.get("id") or "", "name": "", "arguments": ""})
                        if tc.get("id"):
                            slot["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            slot["name"] += fn["name"]
                        if fn.get("arguments"):
                            slot["arguments"] += fn["arguments"]
        finally:
            resp.close()
        parsed_calls, text, reasoning = router.finish()
        calls: list[ToolCall] = []
        for idx in sorted(native):
            slot = native[idx]
            args = _loads_lenient(slot["arguments"]) if slot["arguments"].strip() else {}
            if args is None:
                args = {"_raw": slot["arguments"]}
            tc = ToolCall(name=slot["name"], arguments=args, raw=slot["arguments"])
            if slot["id"]:
                tc.id = slot["id"]
            calls.append(tc)
        calls.extend(parsed_calls)
        for tc in calls:
            yield Event("tool_call", tool_call=tc)
        if usage or timings:
            yield Event("usage", data={"usage": usage, "timings": timings})
        yield Event("done", text=text, data={"finish_reason": finish_reason, "reasoning": reasoning,
                                             "native_tool_calls": bool(native)})

    def _complete(self, messages: list[dict], tools: list[dict] | None, opts: dict) -> Iterator[Event]:
        body = self._body(messages, tools, False, opts)
        data = self._request("POST", self.base_url + "/chat/completions", body)
        choices = data.get("choices") or []
        if not choices:
            raise ProviderError(f"no choices in answer: {str(data)[:300]}")
        msg = choices[0].get("message") or {}
        content = msg.get("content") or ""
        router = StreamRouter()
        for kind, text in router.push(content):
            yield Event(kind, text)
        r = msg.get("reasoning_content") or msg.get("reasoning")
        if r:
            router.all_reasoning += r
            yield Event("reasoning", r)
        parsed_calls, text, reasoning = router.finish()
        calls: list[ToolCall] = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            args = fn.get("arguments")
            if isinstance(args, str):
                args = _loads_lenient(args) or {"_raw": args}
            call = ToolCall(name=fn.get("name", ""), arguments=args or {}, raw=str(fn.get("arguments", "")))
            if tc.get("id"):
                call.id = tc["id"]
            calls.append(call)
        calls.extend(parsed_calls)
        for tc in calls:
            yield Event("tool_call", tool_call=tc)
        if data.get("usage") or data.get("timings"):
            yield Event("usage", data={"usage": data.get("usage") or {}, "timings": data.get("timings") or {}})
        yield Event("done", text=text, data={"finish_reason": choices[0].get("finish_reason"), "reasoning": reasoning,
                                             "native_tool_calls": bool(msg.get("tool_calls"))})
