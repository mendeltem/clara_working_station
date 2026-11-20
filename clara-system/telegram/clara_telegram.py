#!/usr/bin/env python3
"""Telegram bridge for clara-agent: the same bot (ClaraBot) and the same allowed users as the Hermes gateway had.
Token and users: ~/.config/clara/telegram.env (chmod 600; until 29.10.2026 in the Hermes profile).

    phone (Telegram) <-> api.telegram.org (via the Charité proxy) <-> this bridge <-> clara serve (127.0.0.1:8766)

This process is the only one that talks to Telegram. clara serve and its agent stay sandboxed on loopback
(model via the Weiche relay :8770, approvals via System One :8765). The bot token stays here.

Commands: /neu (/new)  new session      /lokal (/local)  new session on the local model (patient data)
          /lokal_an [hours]  allow the local model to start   /lokal_aus  revoke and stop it   /lokal_status
          /stop interrupt        /modus (/mode) default|accept-edits|yolo        /status
Approvals that System One escalates arrive as buttons; a question from the agent is answered by the next message.
A document you send lands in <project>/eingang/ and the agent is told about it.

    python3 -I clara_telegram.py            (systemd: clara-telegram.service)
"""
import html, json, os, re, sys, threading, time, urllib.parse, urllib.request

HOME = os.path.expanduser("~")
ENVFILE = os.environ.get("CLARA_TG_ENV", f"{HOME}/.config/clara/telegram.env")
CLARA_HOME = os.environ.get("CLARA_HOME", f"{HOME}/.clara")
SERVE = os.environ.get("CLARA_SERVE", "http://127.0.0.1:8766")
STATE = os.path.join(CLARA_HOME, "telegram.json")
PROXY = os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY") or ""   # Telegram via the proxy, if one is needed
LOG = os.path.join(CLARA_HOME, "telegram.log")


def env(name):
    for line in open(ENVFILE, encoding="utf-8"):
        if line.startswith(name + "="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


TOKEN = env("TELEGRAM_BOT_TOKEN")
ALLOWED = {u.strip() for u in env("TELEGRAM_ALLOWED_USERS").split(",") if u.strip()}
TG = f"https://api.telegram.org/bot{TOKEN}"
TG_FILE = f"https://api.telegram.org/file/bot{TOKEN}"
tg_open = urllib.request.build_opener(urllib.request.ProxyHandler({"https": PROXY, "http": PROXY} if PROXY else {})).open
lo_open = urllib.request.build_opener(urllib.request.ProxyHandler({})).open


def log(*a):
    with open(LOG, "a") as f:
        f.write(time.strftime("%F %T ") + " ".join(str(x) for x in a)[:500] + "\n")


# ---------------------------------------------------------------- Telegram

def tg(method, wait=70, **params):
    data = json.dumps(params).encode()
    req = urllib.request.Request(f"{TG}/{method}", data, {"Content-Type": "application/json"})
    for attempt in range(4):
        try:
            with tg_open(req, timeout=wait) as r:
                return json.load(r).get("result")
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:200]
            if e.code == 400 and "parse" in body and params.get("parse_mode"):
                params.pop("parse_mode")                      # markdown the bot cannot parse: send plain
                req = urllib.request.Request(f"{TG}/{method}", json.dumps(params).encode(), {"Content-Type": "application/json"})
                continue
            if e.code == 400 and "not modified" in body:
                return None
            log("tg", method, e.code, body)
            if e.code < 500 and e.code != 429:
                return None
        except Exception as e:  # noqa: BLE001 -- the line is unstable: retry
            log("tg", method, type(e).__name__, e)
        time.sleep(2 + 3 * attempt)
    return None


def send(chat, text, **kw):
    text = text.strip() or "(empty)"
    out = None
    for i in range(0, len(text), 3900):
        out = tg("sendMessage", chat_id=chat, text=text[i:i + 3900], disable_web_page_preview=True, **kw)
    return out


# ---------------------------------------------------------------- clara serve

def serve_token():
    return open(os.path.join(CLARA_HOME, "serve-token")).read().strip()


def api(method, path, body=None, timeout=30):
    req = urllib.request.Request(SERVE + path, json.dumps(body or {}).encode() if method == "POST" else None,
                                 {"Content-Type": "application/json", "Authorization": f"Bearer {serve_token()}"},
                                 method=method)
    with lo_open(req, timeout=timeout) as r:
        return json.load(r)


LESEN = {"grep", "rg", "ls", "cat", "head", "tail", "find", "wc", "du", "df", "stat", "file", "date", "nvidia-smi", "ps",
         "awk", "sort", "uniq", "echo", "cut", "tr", "less", "pwd", "which", "readlink", "sed", "jq", "diff", "free", "uptime"}


def erklaere(ev):
    """The approval question in plain words: what kind of action, where, looking for what."""
    tool, cmd = ev.get("tool"), str(ev.get("target") or ev.get("summary") or "")
    home = os.path.expanduser("~")
    orte = []
    for p in re.findall(r"(?:~|" + re.escape(home) + r")/[\w.\-/]+", cmd):
        p = p.replace(home, "~", 1).rstrip("/")
        if p not in orte:
            orte.append(p)
    wo = (" in " + ", ".join(orte[:2])) if orte else ""
    if tool in ("write_file", "edit_file"):
        return f"✏️ Clara would like to change a file{wo}. Allow?"
    if tool != "bash":
        return f"🔐 Clara would like to use the tool {tool}{wo}. Allow?"
    ohne = re.sub(r"'[^']*'|\"[^\"]*\"", "''", cmd)          # quoted text (search patterns) is not a command
    erste = set()
    for t in re.split(r"\|\||&&|;|\|", ohne):
        w = t.split()
        while w and (w[0] == "cd" or "=" in w[0]):
            w = w[2:] if w[0] == "cd" else w[1:]
        if w:
            erste.add(os.path.basename(w[0]))
    if re.search(r"(^|\s)rm\s|\bshred\b", ohne):
        art = "🗑 delete files"
    elif re.search(r"(?<![0-9&])>(?!&)|\btee\b|\bmv\s|\bcp\s|\bmkdir\s|sed\s+-i", ohne.replace("/dev/null", "")):
        art = "✏️ change files"
    elif erste and erste <= LESEN:
        art = "🔍 look into files (read only)"
    else:
        art = "▶️ run a program"
    such = re.findall(r"grep[^'\"]*['\"]([^'\"]{2,60})['\"]", cmd)
    was = (", searching for " + " / ".join(x.replace("\\|", " | ") for x in such[:2])) if such else ""
    return f"{art.split(' ', 1)[0]} Clara would like to {art.split(' ', 1)[1]}{wo}{was}. Allow?"


def kfmt(n):
    n = int(n or 0)
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def ort_text(routes):
    namen = []
    for r in routes or []:
        ort, _, modell = (r or "").partition("/")
        namen.append({"cle": "Clé", "lokal": "local GPU"}.get(ort, ort))
    return " + ".join(dict.fromkeys(n for n in namen if n))


def fusszeile(st):
    """One quiet line under an answer: time, steps, tokens, where it ran."""
    sek = int(st.get("seconds") or 0)
    teile = [f"{sek // 60} min {sek % 60} s" if sek >= 60 else f"{sek} s"]
    if st.get("calls"):
        teile.append(f"{st['calls']} model call{'s' if st['calls'] != 1 else ''}")
        teile.append(f"{kfmt(st.get('prompt'))} read · {kfmt(st.get('completion'))} written")
    wo = ort_text(st.get("routes"))
    if wo:
        teile.append(wo)
    return " · ".join(teile)


class Chat:
    """One Telegram chat = one clara session. Reads the session's events and mirrors them to the chat."""

    def __init__(self, bridge, chat_id, sid):
        self.bridge, self.chat, self.sid = bridge, chat_id, sid
        self.seq = 0
        self.text = ""
        self.status_msg = None
        self.tools = []
        self.question = None
        self.stop = False
        self.ab = time.time()     # events older than this bridge's view of the session are not replayed to the chat
        try:   # approvals and questions still open from before (bridge restart): show them again
            self.offen = {p["id"] for p in api("GET", f"/api/sessions/{sid}").get("pending", [])}
        except Exception:  # noqa: BLE001
            self.offen = set()
        self.seit = None          # start of the running turn (None = idle)
        self.gezeigt = 0.0        # last time the chat saw a line from this turn
        threading.Thread(target=self.follow, daemon=True).start()
        threading.Thread(target=self.herzschlag, daemon=True).start()

    def herzschlag(self):
        """While a turn runs and the chat has seen nothing for a while: update the status line (no new messages)."""
        while not self.stop:
            time.sleep(20)
            if self.seit is None:
                continue
            jetzt = time.time()
            ruhe = jetzt - self.gezeigt
            if (self.status_msg is None and ruhe >= 90) or (self.status_msg is not None and ruhe >= 120):
                self.progress(lauf=True)

    def follow(self):
        while not self.stop:
            try:
                # 09.11.2026: after a clara-serve restart the session's event numbers start again at 0; asking
                # "since" the old, higher number then hid every new reply (15:31-19:50, nothing reached the chat).
                # Seen: the server's counter is below ours -> start at 0 again, replay only what came after the
                # last event this chat already saw.
                if api("GET", f"/api/sessions/{self.sid}").get("seq", self.seq) < self.seq:
                    log("seq reset", self.sid, self.seq)
                    self.seq, self.ab = 0, getattr(self, "last_t", self.ab) + 1.001
                ticket = api("POST", f"/api/sessions/{self.sid}/ticket")["ticket"]
                url = f"{SERVE}/api/sessions/{self.sid}/events?ticket={ticket}&since={self.seq}"
                with lo_open(urllib.request.Request(url), timeout=90) as r:
                    data = ""
                    for raw in r:
                        line = raw.decode("utf-8", "replace").rstrip("\n")
                        if line.startswith("data: "):
                            data += line[6:]
                        elif line == "" and data:
                            ev = json.loads(data)
                            data = ""
                            self.seq = max(self.seq, ev.get("seq", 0))
                            self.last_t = max(getattr(self, "last_t", 0.0), ev.get("t", 0.0))
                            if ev.get("t", self.ab) >= self.ab - 1 or \
                                    (ev.get("kind") in ("permission", "question") and ev.get("id") in self.offen):
                                self.on_event(ev)
            except Exception as e:  # noqa: BLE001
                if not self.stop:
                    log("follow", self.sid, type(e).__name__, e)
                    time.sleep(3)

    def progress(self, lauf=False):
        # 06.11.2026 (user: no long unreadable command text; show thinking and token use instead)
        sek = int(time.time() - self.seit) if self.seit else 0
        teile = [f"{sek // 60} min" if sek >= 60 else f"{sek} s"]
        if self.tools:
            teile.append(f"{len(self.tools)} step{'s' if len(self.tools) != 1 else ''}")
        u = getattr(self, "usage", {}) or {}
        if u.get("completion"):
            teile.append(f"↓ {kfmt(u['completion'])} tokens")
        wo = ort_text(u.get("routes"))
        if wo:
            teile.append(wo)
        line = "✻ Working… (" + " · ".join(teile) + ")"
        self.gezeigt = time.time()
        if self.status_msg is None:
            m = send(self.chat, line)
            self.status_msg = m and m.get("message_id")
        else:
            tg("editMessageText", chat_id=self.chat, message_id=self.status_msg, text=line[:3900])

    def on_event(self, ev):
        k = ev.get("kind")
        if k == "user":
            self.seit, self.gezeigt, self.tools, self.status_msg = time.time(), time.time(), [], None
            self.usage, self.fuss = {}, ""
        elif k == "usage":
            self.usage = ev
        elif k == "turn_stats":
            self.fuss = fusszeile(ev)
        elif k in ("idle", "turn_end"):
            self.seit = None if k == "idle" else self.seit
        if k == "text":
            self.text += ev.get("delta", "")
        elif k == "tool_start":
            self.tools.append(re.sub(r"\s+", " ", ev.get("summary") or ev.get("name", ""))[:70])
            self.progress()
        elif k == "turn_end":
            fuss = getattr(self, "fuss", "")
            send(self.chat, (ev.get("text") or self.text) + (f"\n\n— {fuss}" if fuss else ""))
            if self.status_msg:      # the working line has done its job
                tg("deleteMessage", chat_id=self.chat, message_id=self.status_msg)
            self.text, self.tools, self.status_msg, self.seit = "", [], None, None
        elif k == "permission":
            warn = ("\n⚠ " + "; ".join(ev["warnings"])) if ev.get("warnings") else ""
            prev = ("\n\n" + ev["preview"][:1500]) if ev.get("preview") else ""
            rid = ev["id"]
            kb = {"inline_keyboard": [[{"text": "✅ Yes", "callback_data": f"p:{rid}:allow"},
                                       {"text": "🔁 Session", "callback_data": f"p:{rid}:allow_session"},
                                       {"text": "❌ No", "callback_data": f"p:{rid}:deny"}]]}
            # 06.11.2026 (user: the raw command is not understandable): plain words first, the command behind a spoiler
            send(self.chat, erklaere(ev) + html.escape(warn) + "\n\n<tg-spoiler>" +
                 html.escape(str(ev.get("target") or ev.get("summary") or "")[:700]) + "</tg-spoiler>" + html.escape(prev),
                 reply_markup=kb, parse_mode="HTML")
            self.gezeigt = time.time()
        elif k == "question":
            self.question = ev["id"]
            send(self.chat, "❓ " + ev.get("question", ""))
            self.gezeigt = time.time()
        elif k == "question_done":
            # 10.11.2026 (stability check): a question that ran out (15 min) stayed "open" here for ever, and the
            # next message went to the dead question instead of to Clara
            if self.question == ev.get("id"):
                self.question = None
                if ev.get("answer") is None:
                    send(self.chat, "⌛ The question above ran out without an answer; Clara went on without it.")
        elif k == "warning":
            send(self.chat, "⚠ " + ev.get("text", ""))
            self.gezeigt = time.time()
            self.seit = None if "HTTP" in ev.get("text", "") else self.seit


class Bridge:
    def __init__(self):
        self.state = json.load(open(STATE)) if os.path.exists(STATE) else {}
        self.chats = {}

    def save(self):
        tmp = STATE + ".tmp"
        json.dump(self.state, open(tmp, "w"))
        os.chmod(tmp, 0o600)
        os.replace(tmp, STATE)

    def chat(self, chat_id, new=False, lokal=False):
        key = str(chat_id)
        if new or key not in self.state:
            if key in self.chats:
                self.chats.pop(key).stop = True
            sid = api("POST", "/api/sessions", {})["id"]
            self.state[key] = {"sid": sid, "lokal": lokal}
            self.save()
        if key not in self.chats:
            # 10.11.2026 (stability check): one failed look used to throw the conversation away. Now: a new session
            # only when the server answers "no such session" (404); a server that is just restarting is asked three
            # times, and if it stays away the old session is kept (follow() keeps trying by itself)
            for versuch in range(3):
                try:
                    api("GET", f"/api/sessions/{self.state[key]['sid']}")
                    break
                except urllib.error.HTTPError as e:
                    if e.code == 404:
                        self.state[key] = {"sid": api("POST", "/api/sessions", {})["id"], "lokal": False}
                        self.save()
                        break
                    log("reattach", self.state[key]["sid"], e.code)
                except Exception as e:  # noqa: BLE001
                    log("reattach", self.state[key]["sid"], type(e).__name__, e)
                time.sleep(2 + 3 * versuch)
            self.chats[key] = Chat(self, chat_id, self.state[key]["sid"])
        return self.chats[key]

    def say(self, chat_id, text):
        c = self.chat(chat_id)
        if self.state[str(chat_id)].get("lokal"):
            text = "ORT: lokal\n" + text           # the Weiche sees the marker in every request of this session
        try:
            api("POST", f"/api/sessions/{c.sid}/messages", {"text": text})
        except urllib.error.HTTPError as e:
            send(chat_id, "Clara is still working. /stop interrupts." if e.code == 409 else f"Error {e.code}")
        except urllib.error.URLError as e:       # 10.11.2026: say so instead of silence
            log("say", c.sid, e)
            send(chat_id, "Clara's server does not answer right now (restart?). Your message was not delivered; "
                          "please send it again in a minute.")

    def on_message(self, m):
        chat_id = m["chat"]["id"]
        user = str(m.get("from", {}).get("id"))
        if user not in ALLOWED:
            log("refused user", user)
            return
        text = (m.get("text") or m.get("caption") or "").strip()
        c = self.chat(chat_id)
        if m.get("document"):
            text = self.fetch_document(c, m["document"]) + ("\n" + text if text else "")
        if c.question and not text.startswith("/"):
            rid, c.question = c.question, None
            try:
                api("POST", f"/api/answers/{rid}", {"answer": text})
                return
            except urllib.error.HTTPError as e:
                # 10.11.2026: the question is gone (ran out: 410, server restarted: 404) -- the text is a normal message
                if e.code not in (404, 410):
                    raise
                log("answer to a dead question, sent as a message", rid, e.code)
        cmd = text.split()[0].lower() if text.startswith("/") else ""
        if cmd in ("/lokal_an", "/local_on", "/lokal_aus", "/local_off", "/lokal_status"):
            # 06.11.2026 (user): the local model only starts after the researcher allowed it
            import subprocess
            arg = ["an"] + text.split()[1:2] if cmd in ("/lokal_an", "/local_on") else (["aus"] if cmd in ("/lokal_aus", "/local_off") else [])
            r = subprocess.run([os.path.expanduser("~/local_agentic_system/system/werkzeuge/lokal-freigabe")] + arg,
                               capture_output=True, text=True, timeout=60)
            send(chat_id, (r.stdout or r.stderr).strip() or "done")
            return
        if cmd in ("/neu", "/new", "/start"):
            self.chat(chat_id, new=True)
            send(chat_id, "New session (Clé; local only for patient data).")
        elif cmd in ("/lokal", "/local"):
            self.chat(chat_id, new=True, lokal=True)
            send(chat_id, "New session on the local model (GPU is started on demand).")
        elif cmd == "/stop":
            api("POST", f"/api/sessions/{c.sid}/interrupt")
            send(chat_id, "Interrupted.")
        elif cmd in ("/modus", "/mode"):
            mode = (text.split() + [""])[1]
            try:
                api("POST", f"/api/sessions/{c.sid}/mode", {"mode": mode})
                send(chat_id, f"Mode {mode}.")
            except urllib.error.HTTPError:
                send(chat_id, "Mode: default | accept-edits | yolo")
        elif cmd == "/status":
            snap = api("GET", f"/api/sessions/{c.sid}")
            send(chat_id, f"Session {c.sid}, mode {snap.get('mode')}, {'working' if snap.get('busy') else 'idle'}, "
                          f"{'local' if self.state[str(chat_id)].get('lokal') else 'Clé'}, {len(snap.get('messages', []))} messages")
        elif text:
            self.say(chat_id, text)

    def fetch_document(self, c, doc):
        info = tg("getFile", file_id=doc["file_id"])
        if not info:
            return "(file could not be downloaded)"
        snap = api("GET", f"/api/sessions/{c.sid}")
        ziel = os.path.join(snap["project"], "eingang")
        os.makedirs(ziel, exist_ok=True)
        name = re.sub(r"[^A-Za-z0-9._-]+", "_", doc.get("file_name") or "datei")
        with tg_open(f"{TG_FILE}/{info['file_path']}", timeout=120) as r, open(os.path.join(ziel, name), "wb") as f:
            f.write(r.read())
        return f"[File received: eingang/{name}]"

    def on_callback(self, q):
        if str(q.get("from", {}).get("id")) not in ALLOWED:
            return
        kind, rid, answer = (q.get("data") or "::").split(":", 2)
        ok = False
        if kind == "p":
            try:
                api("POST", f"/api/permissions/{rid}", {"answer": answer})
                ok = True
            except Exception:  # noqa: BLE001
                pass
        tg("answerCallbackQuery", callback_query_id=q["id"], text="ok" if ok else "no longer open")
        msg = q.get("message") or {}
        if msg:
            tg("editMessageReplyMarkup", chat_id=msg["chat"]["id"], message_id=msg["message_id"], reply_markup={"inline_keyboard": []})

    def run(self):
        offset = self.state.get("_offset", 0)
        log("bridge up, users", len(ALLOWED))
        for key in [k for k in self.state if not k.startswith("_")]:
            try:
                self.chat(int(key))        # follow running turns again after a restart (old events are not replayed)
            except Exception as e:  # noqa: BLE001
                log("reattach", key, type(e).__name__, e)
        while True:
            ups = tg("getUpdates", wait=70, timeout=50, offset=offset, allowed_updates=["message", "callback_query"])
            for u in ups or []:
                offset = u["update_id"] + 1
                try:
                    if "message" in u:
                        self.on_message(u["message"])
                    elif "callback_query" in u:
                        self.on_callback(u["callback_query"])
                except Exception as e:  # noqa: BLE001 -- one bad update must not stop the bridge
                    log("update", type(e).__name__, e)
            if ups:
                self.state["_offset"] = offset
                self.save()
            elif ups is None:
                time.sleep(5)


if __name__ == "__main__":
    if not TOKEN or not ALLOWED:
        sys.exit(f"TELEGRAM_BOT_TOKEN / TELEGRAM_ALLOWED_USERS missing in {ENVFILE}")
    Bridge().run()
