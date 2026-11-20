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
OEFFENTLICH_LESEN = tuple(os.path.expanduser(p) for p in (
    "~/blackbeard_wikki/", "~/local_agentic_system/system/skills-clara/", "~/.clara/skills/", "~/.clara-lauf-cle/skills/"))
_lock = threading.Lock()
_hub = {"zeit": 0, "ids": set()}   # model ids the hub offers now (13.11.2026: hub dropped fast/vision/light overnight -> 400)


def hub_modell(modell):
    """Model the hub really offers: an alias it dropped falls back to standard_modell (list cached 10 min)."""
    if time.time() - _hub["zeit"] > 600:
        try:
            req = urllib.request.Request(K["cle_url"].rstrip("/") + "/models",
                                         headers={"Authorization": f"Bearer {weiche.cle_key()}"})
            _hub["ids"] = {m["id"] for m in json.load(OPENER.open(req, timeout=10))["data"]}
        except Exception:  # noqa: BLE001 -- list not reachable: keep the old one, send the name as asked
            pass
        _hub["zeit"] = time.time()
    if _hub["ids"] and modell not in _hub["ids"]:
        return K["standard_modell"]
    return modell


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


LESEBEFEHLE = {"cat", "head", "tail", "ls", "grep", "sed", "echo", "wc", "sort", "uniq", "cut", "find", "awk", "tr", "nl"}


def nur_lesen_oeffentlich(cmd):
    """A shell command that only reads public files (wiki, skills): every path in it is public, every command of the
    pipeline a reading one, no redirection into a file."""
    if re.search(r"[^2]>|`|\$\(", cmd.replace("2>/dev/null", "").replace("2>&1", "")):
        return False
    pfade = re.findall(r"(?:~|/)[^\s;|&'\"<>()]+", cmd)
    pfade = [p for p in pfade if p not in ("/dev/null",)]
    if not pfade or not all(os.path.abspath(os.path.expanduser(p)).startswith(OEFFENTLICH_LESEN) for p in pfade):
        return False
    teile = [t.strip().split()[0] for t in re.split(r"[;|&]+", cmd) if t.strip()]
    return all(t in LESEBEFEHLE for t in teile)


def kuerzen(messages, zeichen):
    """Fit a conversation into a small local context: keep the system prompt, the first user message and as many of
    the latest messages as fit (a tool result is never kept without the call that produced it)."""
    sysm = [m for m in messages if m.get("role") == "system"][:1]
    erste = next((m for m in messages if m.get("role") == "user"), None)
    rest = [m for m in messages if m is not erste and m.get("role") != "system"]
    behalten, n = [], len(json.dumps(sysm + [erste]))
    for m in reversed(rest):
        g = len(json.dumps(m))
        if n + g > zeichen:
            break
        behalten.insert(0, m); n += g
    while behalten and behalten[0].get("role") == "tool":
        behalten.pop(0)                                    # drop orphan tool results at the cut
    hinweis = {"role": "user", "content": "(Earlier steps of this session were dropped to fit the local model; "
                                          "continue from the latest steps.)"}
    return sysm + ([erste] if erste else []) + [hinweis] + behalten


# 06.11.2026 (user): cohort names the researcher types are replaced by neutral short names before the Weiche decides,
# so a chat that only NAMES a project stays on Clé; the real name never leaves the workstation. Only role=user text, never tool
# output, never inside paths or file names (the lookarounds skip anything next to / . - or a letter).
try:
    KURZ = {k: v for k, v in json.load(open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                                         "kurznamen.json"))).items() if not k.startswith("_")}
except (OSError, ValueError):
    KURZ = {}
KURZ_RX = re.compile(r"(?<![\w/.-])(" + "|".join(sorted(map(re.escape, KURZ), key=len, reverse=True)) + r")(?![\w/.-])") if KURZ else None


def kurznamen(body):
    """Replace cohort names in the user's own messages (in place). Returns how many were replaced."""
    if KURZ_RX is None:
        return 0
    n = 0
    for m in body.get("messages") or []:
        if m.get("role") != "user" or not isinstance(m.get("content"), str):
            continue
        neu, k = KURZ_RX.subn(lambda x: KURZ[x.group(1)], m["content"])
        if k:
            m["content"], n = neu, n + k
    return n


# 06.11.2026 (user: chat on the local model waited minutes behind background jobs): one local request at a time
# (llama-server has one slot), and a waiting chat request goes before waiting background requests.
_lokal_frei = threading.Condition()
_lokal_belegt = [False]
_chat_wartet = [0]


def ist_chat(quelle):
    return quelle.startswith(("chat", "telegram"))


class LokalPlatz:
    def __init__(self, quelle):
        self.chat = ist_chat(quelle)

    def __enter__(self):
        with _lokal_frei:
            if self.chat:
                _chat_wartet[0] += 1
            try:
                while _lokal_belegt[0] or (not self.chat and _chat_wartet[0] > 0):
                    _lokal_frei.wait(timeout=5)
            finally:
                if self.chat:
                    _chat_wartet[0] -= 1
            _lokal_belegt[0] = True
        return self

    def __exit__(self, *exc):
        with _lokal_frei:
            _lokal_belegt[0] = False
            _lokal_frei.notify_all()


DATEN_ORTE = [os.path.expanduser(p).rstrip("/") + "/" for p in weiche.KONFIG["datenpfade"] if p.startswith(("~", "/"))]
DATEI_RX = re.compile(r"(?:[\w./~-]*/)?[\w.-]+\.(?:nii(?:\.gz)?|dcm|mgz|mgh|xlsx?)\b", re.I)


def daten_dateien(zugriff):
    """06.11.2026: reasons to stay local = image or Excel files that a tool call touches INSIDE a data folder (and not
    in a released public folder). A bare file name counts when the session's project folder is a data folder."""
    projekt = re.search(r"\[context: project=([^,\]]+)", zugriff)
    projekt = os.path.expanduser(projekt.group(1).strip()) if projekt else ""
    gruende = []
    for f in DATEI_RX.findall(zugriff):
        p = os.path.expanduser(f) if "/" in f else os.path.join(projekt, f)
        p = os.path.normpath(p) + ("/" if os.path.isdir(p) else "")
        if any(p.startswith(o) for o in DATEN_ORTE) and not weiche._pfad_oeffentlich(p):
            gruende.append(f"data file {os.path.basename(p)[:40]}")
    return list(dict.fromkeys(gruende))[:4]


def herkunft(body):
    """Who sent the request when the caller did not say: clara-agent sends tools; its project line tells Telegram
    (clara serve in ~/cle-arbeit) from the job queue (clara-lauf in a project folder)."""
    if not body.get("tools"):
        return "skript"
    m = re.search(r"\[context: project=([^,\]]+)", text_of(body.get("messages") or []))
    if not m:
        return "agent"
    return "telegram/serve" if m.group(1).rstrip("/").endswith("/cle-arbeit") else "auftrag"


def gespraech(messages):
    first = next((m for m in messages if m.get("role") == "user"), {})
    sysm = next((m for m in messages if m.get("role") == "system"), {})
    return hashlib.sha256((text_of([sysm]) + "\x00" + text_of([first])).encode()).hexdigest()[:16]


def letzte_frage(messages):
    return text_of([next((m for m in reversed(messages) if m.get("role") == "user"), {})])


# 09.11.2026 (user: "Clara should know what happens everywhere"; results questions go to the local model):
#  * the LOCAL full view of the overview board or of Clara's memory carries this marker (system/lage); a conversation
#    that contains it anywhere stays on the local model, like one that once read an image
#  * a results question about a project ("what did step 30 find?", "was hat Schritt 30 ergeben?") in the researcher's own
#    words goes local from the start, in interactive chats -- except from clara serve (Telegram): patient-derived results never go to
#    Telegram, so there Clara answers from the Clé view and says that results need a local session
LAGE_VOLL = "LAGE-VOLL (local only"
ERGEBNIS_FRAGE = re.compile(
    r"\b(what|which)\s+(did|does|do|has|have)\b(?=.{0,80}\b(step|schritt|analys\w*|cmb|pvs|gm|project|projekt)\b)"
    r".{0,80}\b(find|found|show|shows|showed|say|says|give|gave|come out|result\w*)\b"
    r"|\b(results?|findings?|ergebnis\w*|befund\w*|outcome)\b.{0,50}\b(step|schritt|analys\w*|cmb|pvs|gm|project|projekt)\b"
    r"|\b(step|schritt)\s*\d{1,3}[a-z]?\b.{0,50}\b(find|found|result\w*|ergeb\w*|ergab|herausgekommen|show\w*|zeigt\w*)\b"
    r"|\bwas\s+(hat|haben|ergab|kam|zeigt\w*)\b.{0,60}\b(ergeben|gefunden|herausgekommen|gezeigt|raus)\b", re.I | re.S)


def eigene_worte(text):
    """The researcher's own words of a user turn: without the context line and the [background ...] notes the agent
    puts in front (the status brief names steps and files; it must not count as a question)."""
    m = re.search(r"\[context: project=[^\]]*\]", text)
    if m:
        text = text[m.end():]
    while True:
        neu = re.sub(r"^\s*\[background[^\]]*\]", "", text, count=1, flags=re.S)
        if neu == text:
            return text.strip()
        text = neu


def route(body, quelle=""):
    """-> (ort, modell, gruende, turn) ; turn = {conv, frage, art} for the verdict at the end of the turn."""
    msgs = body.get("messages") or []
    roh = msgs
    wunsch = str(body.get("model") or "auto")
    # What counts where (29.10.2026, after a Telegram session flipped to local because a log line in a tool result named
    # a data path): subject/session ids and cohort names count in every text. Data paths and image files count where
    # data is ACCESSED: the project line of the system prompt and the arguments of tool calls (read, write, bash).
    # A path that is only mentioned in text (a log, a script, the brief) is structure, not content -- the same line as
    # in the setup notes. Plain-word markers count in user and tool text outside the topic brief.
    # Reading a public file (the wiki on GitHub, Clara's own skills) is not data: drop those tool results before the
    # checks (29.10.2026, the wiki areas name cohorts and every planning turn went to the GPU)
    aufrufe = {tc.get("id"): tc.get("function", {}) for m in msgs for tc in (m.get("tool_calls") or [])}
    def oeffentlich_gelesen(m):
        f = aufrufe.get(m.get("tool_call_id"), {})
        try:
            pfad = json.loads(f.get("arguments") or "{}").get("path", "")
        except ValueError:
            pfad = ""
        if f.get("name") in ("read_file", "list_files", "search"):
            return bool(pfad) and os.path.abspath(os.path.expanduser(pfad)).startswith(OEFFENTLICH_LESEN)
        if f.get("name") == "bash":
            try:
                cmd = json.loads(f.get("arguments") or "{}").get("command", "")
            except ValueError:
                return False
            return nur_lesen_oeffentlich(cmd)
        return False
    msgs = [m for m in msgs if not (m.get("role") == "tool" and oeffentlich_gelesen(m))]
    voll = text_of(msgs)
    # data ACCESS: the project line clara-agent puts into the conversation, and the arguments of tool calls; not the
    # rest of the system prompt (skill descriptions mention file types, the memory explains the rules)
    projekt = " ".join(re.findall(r"\[context: project=[^\]]*\]", voll))
    zugriff = "\n".join([projekt] + [json.dumps(f, ensure_ascii=False) for f in aufrufe.values()])
    # 06.11.2026 (user: "the rules must be much simpler"): only REAL patient data counts --
    #   case ids anywhere in the conversation (the researcher's text or tool output; public ids excepted), and
    #   image files a tool call reads from a non-public folder. A data path or a cohort name alone is no reason any more.
    # The conversation carries every earlier tool result, so a session that once got real data stays local.
    # 06.11.2026, later (user: "pseudonymised data like sub-01 is also allowed"): pseudonymous case ids are no reason
    # either; only image files read from a non-public folder (and the explicit markers below) keep a request local.
    # The exit guard to the internet stays strict (ids included).
    schutz = daten_dateien(zugriff)
    # plain-word markers are the user's (or the job's) own flag: only in user messages, outside the topic brief;
    # our code and docs quote "Patientendaten" as rule text, so tool output does not count here
    schutz += [g for g in weiche.schutzgruende(ohne_brief(text_of([m for m in msgs if m.get("role") == "user"])),
                                               pfade=False) if g.startswith("marker")]
    if any(LAGE_VOLL in (m.get("content") if isinstance(m.get("content"), str) else json.dumps(m.get("content") or ""))
           for m in roh):
        schutz.append("local overview or memory in the conversation")
    # only interactive chats outside Telegram (clara-lauf sets x_quelle chat/terminal); jobs and work sessions keep
    # their own place ("what does the frame show?" in a film job is no results question)
    kanal = quelle or herkunft(body)
    if kanal.startswith("chat") and ERGEBNIS_FRAGE.search(eigene_worte(letzte_frage(roh))):
        schutz.append("results question about a project")
    if schutz or wunsch == "lokal":
        return "lokal", None, schutz or ["model lokal requested"], None
    conv, frage = gespraech(msgs), letzte_frage(msgs)
    turn = {"conv": conv, "frage": frage, "art": None}
    if wunsch in CLE_MODELLE:
        return "cle", wunsch, ["model requested"], turn
    if hat_bild(msgs):
        return "cle", K["arten"]["bild"]["modell"], ["image in request"], turn
    if K.get("modellwahl", "system1") == "fest":
        return "cle", K["standard_modell"], ["fixed model (modellwahl fest)"], turn
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


def verbrauch_aus(raw, stream):
    """(prompt_tokens, completion_tokens, cached_tokens) the server reported, or (None, None, None)."""
    try:
        if not stream:
            u = json.loads(raw).get("usage") or {}
        else:
            u = {}
            for line in raw.splitlines():
                if line.startswith(b"data: ") and b'"usage"' in line:
                    u = json.loads(line[6:]).get("usage") or u
        det = u.get("prompt_tokens_details") or {}
        return u.get("prompt_tokens"), u.get("completion_tokens"), det.get("cached_tokens")
    except (ValueError, AttributeError):
        return None, None, None


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


class StartRueckstau:
    """When the local model cannot start: wait longer before each new try, slow down callers, tell the user once.
    Night 3 to 4 Nov: every request failed at once, a job restarted every 6 s, 1390 sessions, 2.5 h, no alarm."""
    WARTE_MAX = 1800       # s between start tries, at most
    HALTEN = 60            # s a caller waits for its refusal, so a retry loop runs at most once a minute
    ALARM_NACH = 3         # failed tries before the alarm
    ALARM_WIEDER = 6 * 3600

    def __init__(self, melden, uhr=time.time, schlafen=time.sleep):
        self.melden, self.uhr, self.schlafen = melden, uhr, schlafen
        self.lock = threading.Lock()
        self.fehler, self.frei_ab, self.gemeldet = 0, 0.0, 0.0

    def pruefen(self):
        """Raises during the wait, after holding the caller."""
        rest = self.frei_ab - self.uhr()
        if rest > 0:
            self.schlafen(min(self.HALTEN, rest))
            raise RuntimeError(f"local model could not be started ({self.fehler} tries); next try in {int(rest)} s")

    def misslungen(self, rc):
        with self.lock:
            self.fehler += 1
            self.frei_ab = self.uhr() + min(self.WARTE_MAX, 60 * 2 ** (self.fehler - 1))
            if self.fehler >= self.ALARM_NACH and self.uhr() - self.gemeldet > self.ALARM_WIEDER:
                self.gemeldet = self.uhr()
                self.melden(f"Local model cannot start: {self.fehler} tries failed (modellstart rc {rc}). Local requests are refused. "
                            "Check: systemctl --user status llama-server")

    def gelungen(self):
        with self.lock:
            if self.gemeldet:
                self.melden(f"Local model is back after {self.fehler} failed tries.")
            self.fehler, self.frei_ab, self.gemeldet = 0, 0.0, 0.0


def _melden(text):
    try:
        subprocess.run([MELDEN, text], capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        protokoll({"zeit": time.strftime("%FT%T"), "fehler": f"alarm not sent: {e}"[:200]})


MELDEN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "werkzeuge", "melden")
RUECKSTAU = StartRueckstau(_melden)


F_FREIGABE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lokal-freigabe.bis")


def lokal_bereit(auto=False):
    """auto=True only when the Weiche itself saw a data file (NIfTI/DICOM/Excel from a data folder): then the local
    model may start by itself with the timer. A request that merely ASKS for the local model (model "lokal", old queue
    jobs) still needs the researcher's approval (06.11.2026, user: "it must not just start like that")."""
    _lokal_zuletzt[0] = time.time()
    RUECKSTAU.pruefen()
    subprocess.run([weiche.LOKAL_BEDARF, "an", str(os.getpid()), "weiche-relay"], check=False)
    os.environ["LOKAL_WER"] = "a Clara work session"
    env = dict(os.environ, LOKAL_AUTO="1") if auto else {k: v for k, v in os.environ.items() if k != "LOKAL_AUTO"}
    r = subprocess.run([weiche.MODELLSTART, "--warten", "600", "--leise"], capture_output=True, env=env) \
        if os.path.exists(weiche.MODELLSTART) else subprocess.CompletedProcess([], 0)
    if r.returncode == 5:
        # 07.11.2026 (night report, user OK): no approval is not a failed start -- no backoff, no false alarm. The
        # request waits up to 10 min for the approval (the gate asked on Telegram), then it ends with a clear message.
        frist = time.time() + 600
        while time.time() < frist:
            time.sleep(30)
            if os.path.exists(F_FREIGABE) and int(open(F_FREIGABE).read().strip() or 0) > time.time():
                r = subprocess.run([weiche.MODELLSTART, "--warten", "600", "--leise"], capture_output=True, env=env)
                break
    if r.returncode == 5:
        raise RuntimeError("the local model is off and needs the researcher's approval (patient data): "
                           "a question went to Telegram -- allow with /lokal_an there or `lokal-freigabe an` on the workstation, "
                           "then ask again")
    if r.returncode != 0:
        RUECKSTAU.misslungen(r.returncode)
        raise RuntimeError(f"local model could not be started (modellstart rc {r.returncode})")
    RUECKSTAU.gelungen()
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
        self._platz = None
        try:
            return self._do_post()
        finally:
            if self._platz is not None:          # the local slot is free again, also after errors and early returns
                self._platz.__exit__(None, None, None)

    def _do_post(self):
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
        quelle = str(body.pop("x_quelle", "") or self.headers.get("X-Clara-Quelle", "") or herkunft(body))[:40]
        ersetzt = kurznamen(body)
        try:
            ort, modell, gruende, turn = route(body, quelle)
            if ersetzt:
                gruende = list(gruende) + [f"{ersetzt} cohort name(s) shortened"]
            if ort == "lokal":
                body["model"] = lokal_bereit(auto=any(str(g).startswith("data file") for g in gruende))
                url, hdr = K["lokal_url"].rstrip("/") + "/chat/completions", {}
            else:
                body["model"] = hub_modell(modell)
                if body["model"] != modell:
                    gruende = list(gruende) + [f"{modell} not on the hub, sent as {body['model']}"]
                body["max_tokens"] = max(int(body.get("max_tokens") or 0), 24000)   # reasoning counts against it (test run 9)
                if body.get("stream"):
                    body["stream_options"] = {"include_usage": True}   # the hub reports tokens in the last chunk
                body.pop("chat_template_kwargs", None)
                url, hdr = K["cle_url"].rstrip("/") + "/chat/completions", {"Authorization": f"Bearer {weiche.cle_key()}"}
        except Exception as e:  # noqa: BLE001 -- a broken Weiche never sends to Clé
            protokoll({"zeit": time.strftime("%FT%T"), "fehler": str(e)[:200]})
            return self._json(503, {"error": f"weiche: {e}"})
        req = urllib.request.Request(url, json.dumps(body).encode(),
                                     {"Content-Type": "application/json", **hdr}, method="POST")
        if ort == "lokal":
            self._platz = LokalPlatz(quelle).__enter__()     # chat goes before waiting background requests
        status = 200
        try:
            up = OPENER.open(req, timeout=900)
        except urllib.error.HTTPError as e:
            up, status = e, e.code
            if ort == "lokal" and status == 400:
                fehler = e.read().decode("utf-8", "replace")
                ctx = re.search(r'"n_ctx":\s*(\d+)', fehler)
                if "exceed" in fehler and "context" in fehler and ctx and not body.get("_gekuerzt"):
                    # one retry with a trimmed conversation (~3 characters per token, room for the answer)
                    body["messages"] = kuerzen(body["messages"], int(ctx.group(1)) * 2)
                    body["_gekuerzt"] = True
                    gek = dict(body); gek.pop("_gekuerzt")
                    try:
                        up = OPENER.open(urllib.request.Request(url, json.dumps(gek).encode(), {"Content-Type": "application/json", **hdr},
                                                                method="POST"), timeout=900)
                        status = 200
                        protokoll({"zeit": time.strftime("%FT%T"), "ort": ort, "modell": body.get("model"),
                                   "hinweis": f"conversation trimmed to {len(gek['messages'])} messages for n_ctx {ctx.group(1)}"})
                    except urllib.error.HTTPError as e2:
                        fehler, status, up = e2.read().decode("utf-8", "replace"), e2.code, None
                if up is not None and status == 200:
                    pass
                elif "exceed" in fehler and "context" in fehler:
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
        pt, ct, cache = verbrauch_aus(b"".join(roh), bool(body.get("stream")))
        protokoll({"zeit": time.strftime("%FT%T"), "quelle": quelle, "ort": ort, "modell": body.get("model"), "status": status,
                   "prompt_tokens": pt, "completion_tokens": ct, "cached_tokens": cache,
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
