#!/usr/bin/env python3
"""Weiche-Relay: one OpenAI-compatible endpoint on 127.0.0.1 for agents that may only talk to loopback (clara-agent).

Per request:
  1. fixed rule over ALL messages (data path incl. the project line, ids, cohort names, image files, markers):
     a hit sends the request to the local model (llama-server :8000, started on demand via lokal-bedarf/modellstart);
  2. else Clé: model 'auto' (or anything not a Clé alias) -> the Weiche picks the alias once per conversation from the
     first user message (System One type at p >= 0.8), and keeps it, so the hub's prefix cache stays warm.
Model name 'lokal' forces the local model. The Clé key stays in this process; the agent never sees it.

    python3 -I relay.py --port 8770
Log (no message text, only place/model/reasons): ergebnisse/weiche/relay.jsonl
"""
import argparse, hashlib, json, os, re, subprocess, sys, threading, time, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import weiche  # noqa: E402

K = weiche.KONFIG
CLE_MODELLE = sorted({a["modell"] for a in K["arten"].values()} | {"fast", "clea", "frontier", "eu", "vision", "light"})
LOG = os.path.join(weiche.ERGEBNISSE, "relay.jsonl")
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # Clé and loopback: never via proxy
_wahl = {}            # conversation+turn key -> (Clé alias, type)
_eskaliert = {}       # conversation key -> model whose last turn System One judged 'nein'
STARK = "frontier"
_lock = threading.Lock()


def text_of(messages):
    out = []
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            out.append(c)
        elif isinstance(c, list):
            out += [p.get("text", "") for p in c if isinstance(p, dict) and p.get("type") == "text"]
        for tc in m.get("tool_calls") or []:
            out.append(json.dumps(tc.get("function", {}), ensure_ascii=False))
    return "\n".join(out)


BRIEF = re.compile(r"THEMENBRIEF \([^)]*\):\n.*?\n---\nAUFTRAG:\n", re.S)


def ohne_brief(text):
    return BRIEF.sub("", text)


def hat_bild(messages):
    return any(isinstance(m.get("content"), list) and any(isinstance(p, dict) and p.get("type") == "image_url"
                                                          for p in m["content"]) for m in messages)


def gespraech(messages):
    first = next((m for m in messages if m.get("role") == "user"), {})
    sysm = next((m for m in messages if m.get("role") == "system"), {})
    return hashlib.sha256((text_of([sysm]) + "\x00" + text_of([first])).encode()).hexdigest()[:16]


def letzte_frage(messages):
    return text_of([next((m for m in reversed(messages) if m.get("role") == "user"), {})])


def route(body):
    """-> (ort, modell, gruende, turn) ; turn = {conv, frage, art} for the verdict at the end of the turn."""
    msgs = body.get("messages") or []
    wunsch = str(body.get("model") or "auto")
    # What counts where (29.10.2026, after a Telegram session flipped to local because a log line in a tool result named
    # a data path): subject/session ids and cohort names count in every text. Data paths and image files count where
    # data is ACCESSED: the project line of the system prompt and the arguments of tool calls (read, write, bash).
    # A path that is only mentioned in text (a log, a script, the brief) is structure, not content -- the same line as
    # for Claude in system/CLAUDE.md. Plain-word markers count in user and tool text outside the topic brief.
    voll = text_of(msgs)
    zugriff = "\n".join([text_of([m for m in msgs if m.get("role") == "system"])] +
                        [json.dumps(tc.get("function", {}), ensure_ascii=False)
                         for m in msgs for tc in (m.get("tool_calls") or [])])
    ids = [g for g in weiche.schutzgruende(voll, pfade=False) if g.startswith("pattern") and ".nii" not in g]
    schutz = ids
    schutz += [g for g in weiche.schutzgruende(zugriff, pfade=True)
               if g.startswith("data path") or (g.startswith("pattern") and ".nii" in g)]
    schutz += [g for g in weiche.schutzgruende(ohne_brief(text_of([m for m in msgs if m.get("role") != "system"])),
                                               pfade=False) if g.startswith("marker")]
    if schutz or wunsch == "lokal":
        return "lokal", None, schutz or ["model lokal requested"], None
    conv, frage = gespraech(msgs), letzte_frage(msgs)
    turn = {"conv": conv, "frage": frage, "art": None}
    if wunsch in CLE_MODELLE:
        return "cle", wunsch, ["model requested"], turn
    if hat_bild(msgs):
        return "cle", K["arten"]["bild"]["modell"], ["image in request"], turn
    key = conv + hashlib.sha256(frage.encode()).hexdigest()[:12]   # one decision per user turn, kept in its tool loop
    with _lock:
        if key in _wahl:
            turn["art"] = _wahl[key][1]
            return "cle", _wahl[key][0], ["same turn"], turn
        hoch = _eskaliert.pop(conv, None)
    d = weiche.entscheiden(frage, ort_vorgabe="cle", quelle="relay", protokoll=False, mit_thema=False)
    modell, gruende = d["modell"], d["gruende"]
    if hoch and modell != STARK:
        modell, gruende = STARK, gruende + [f"escalated: last turn judged 'nein' ({hoch})"]
    turn["art"] = d.get("art")
    with _lock:
        _wahl[key] = (modell, d.get("art"))
    return "cle", modell, gruende, turn


def urteil_im_hintergrund(turn, modell, antwort):
    """System One judges the finished turn; a 'nein' escalates the next turn of this conversation."""
    def arbeit():
        v = weiche.bewerten(turn["frage"], antwort)
        if v["urteil"] == "nein":
            with _lock:
                _eskaliert[turn["conv"]] = modell
        protokoll({"zeit": time.strftime("%FT%T"), "bewertung": v["urteil"], "p": v["p"], "art": turn["art"],
                   "modell": modell, "conv": turn["conv"]})
    threading.Thread(target=arbeit, daemon=True).start()


def antwort_aus(raw, stream):
    """Final assistant text of a response, or None while the model still calls tools."""
    try:
        if not stream:
            m = json.loads(raw)["choices"][0]
            return None if m["message"].get("tool_calls") else (m["message"].get("content") or "")
        text, tools = [], False
        for line in raw.splitlines():
            if not line.startswith(b"data: ") or line.strip() == b"data: [DONE]":
                continue
            ch = json.loads(line[6:]).get("choices") or [{}]
            delta = ch[0].get("delta") or {}
            tools = tools or bool(delta.get("tool_calls")) or ch[0].get("finish_reason") == "tool_calls"
            text.append(delta.get("content") or "")
        return None if tools else "".join(text)
    except (ValueError, KeyError, IndexError):
        return None


_lokal_zuletzt = [0.0]
LOKAL_HALTEN = 600     # s: the relay's local need ends 10 min after the last local request (then modellwache may stop)


def _bedarf_waechter():
    while True:
        time.sleep(30)
        if _lokal_zuletzt[0] and time.time() - _lokal_zuletzt[0] > LOKAL_HALTEN:
            subprocess.run([weiche.LOKAL_BEDARF, "aus", str(os.getpid())], check=False)
            _lokal_zuletzt[0] = 0.0


class _Antwort:
    """Stand-in for an already read HTTP error body."""
    def __init__(self, data):
        self.data, self.headers = data, {"Content-Type": "application/json"}

    def read(self, n=-1):
        d, self.data = self.data, b""
        return d

    read1 = read


def lokal_bereit():
    _lokal_zuletzt[0] = time.time()
    subprocess.run([weiche.LOKAL_BEDARF, "an", str(os.getpid()), "weiche-relay"], check=False)
    r = subprocess.run([weiche.MODELLSTART, "--warten", "600", "--leise"], capture_output=True) \
        if os.path.exists(weiche.MODELLSTART) else subprocess.CompletedProcess([], 0)
    if r.returncode != 0:
        raise RuntimeError("local model could not be started (modellstart)")
    return json.load(OPENER.open(K["lokal_url"] + "/models", timeout=10))["data"][0]["id"]


def protokoll(rec):
    os.makedirs(weiche.ERGEBNISSE, exist_ok=True)
    with open(LOG, "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    os.chmod(LOG, 0o600)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"     # body ends when the connection closes: simple streaming pass-through

    def log_message(self, *_):
        pass

    def _json(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        p = self.path.rstrip("/")
        if p in ("/v1/models", "/models"):
            ids = ["auto", "lokal"] + CLE_MODELLE
            return self._json(200, {"object": "list", "data": [{"id": i, "object": "model", "owned_by": "weiche"} for i in ids]})
        if p in ("/health", "/v1/health"):
            return self._json(200, {"status": "ok"})
        return self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path.rstrip("/") not in ("/v1/chat/completions", "/chat/completions"):
            return self._json(404, {"error": "not found"})
        n = int(self.headers.get("Content-Length") or 0)
        if n > 64 * 1024 * 1024:
            return self._json(413, {"error": "body too large"})
        try:
            body = json.loads(self.rfile.read(n))
        except ValueError:
            return self._json(400, {"error": "invalid json"})
        t0 = time.time()
        try:
            ort, modell, gruende, turn = route(body)
            if ort == "lokal":
                body["model"] = lokal_bereit()
                url, hdr = K["lokal_url"].rstrip("/") + "/chat/completions", {}
            else:
                body["model"] = modell
                body["max_tokens"] = max(int(body.get("max_tokens") or 0), 8000)
                body.pop("chat_template_kwargs", None)
                url, hdr = K["cle_url"].rstrip("/") + "/chat/completions", {"Authorization": f"Bearer {weiche.cle_key()}"}
        except Exception as e:  # noqa: BLE001 -- a broken Weiche never sends to Clé
            protokoll({"zeit": time.strftime("%FT%T"), "fehler": str(e)[:200]})
            return self._json(503, {"error": f"weiche: {e}"})
        req = urllib.request.Request(url, json.dumps(body).encode(),
                                     {"Content-Type": "application/json", **hdr}, method="POST")
        status = 200
        try:
            up = OPENER.open(req, timeout=900)
        except urllib.error.HTTPError as e:
            up, status = e, e.code
            if ort == "lokal" and status == 400:
                fehler = e.read().decode("utf-8", "replace")
                if "exceed" in fehler and "context" in fehler:
                    protokoll({"zeit": time.strftime("%FT%T"), "ort": ort, "modell": body.get("model"), "status": 400,
                               "fehler": "local context too small for this conversation"})
                    return self._json(400, {"error": {"message":
                        "Diese Sitzung muss jetzt lokal weiterlaufen (Datenbezug), aber das lokale Modell "
                        f"({body.get('model')}) fasst den bisherigen Verlauf nicht: {fehler[:160]}. "
                        "Neue Sitzung starten (Telegram: /neu oder /lokal) oder warten, bis die GPU frei ist."}})
                up = _Antwort(fehler.encode())
        except Exception as e:  # noqa: BLE001
            protokoll({"zeit": time.strftime("%FT%T"), "ort": ort, "modell": body.get("model"), "fehler": str(e)[:200]})
            return self._json(502, {"error": f"upstream {ort}: {e}"})
        self.send_response(status)
        self.send_header("Content-Type", up.headers.get("Content-Type", "application/json"))
        self.send_header("X-Weiche", f"{ort}/{body.get('model')}")
        self.end_headers()
        roh = []
        try:
            while True:
                chunk = up.read1(65536) if hasattr(up, "read1") else up.read(65536)
                if not chunk:
                    break
                roh.append(chunk)
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        if ort == "cle" and status == 200 and turn:
            antwort = antwort_aus(b"".join(roh), bool(body.get("stream")))
            if antwort:
                urteil_im_hintergrund(turn, body.get("model"), antwort)
        protokoll({"zeit": time.strftime("%FT%T"), "ort": ort, "modell": body.get("model"), "status": status,
                   "stream": bool(body.get("stream")), "art": (turn or {}).get("art"), "gruende": gruende[:4], "s": round(time.time() - t0, 1)})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8770)
    a = ap.parse_args()
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    srv.daemon_threads = True
    threading.Thread(target=_bedarf_waechter, daemon=True).start()
    print(f"weiche-relay on 127.0.0.1:{a.port}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
