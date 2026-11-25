"""Offline test of the bridge's voice path: fake Telegram, fake Clara server, real local transcription."""
import io, os, sys, types, contextlib
S = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.abspath(__file__))   # folder with fake.env, probe_en.oga, silence.oga
os.environ.update(CLARA_TG_ENV=f"{S}/fake.env", CLARA_HOME=S)
sys.path.insert(0, os.path.expanduser("~/local_agentic_system/system/telegram"))
import clara_telegram as ct
sent, said = [], []
ct.tg = lambda method, **kw: {"file_path": "voice/file_1.oga"} if method == "getFile" else None
@contextlib.contextmanager
def fake_open(url, timeout=0):
    assert url.endswith("/voice/file_1.oga"), url
    yield io.BytesIO(open(f"{S}/{AUDIO}", "rb").read())
ct.tg_open = fake_open
ct.send = lambda chat, text, **kw: sent.append(text)
b = ct.Bridge.__new__(ct.Bridge)
b.chat = lambda chat_id, new=False, lokal=False: types.SimpleNamespace(question=None, sid="s1")
b.say = lambda chat_id, text: said.append(text)
for AUDIO, msg in (("probe_en.oga", {"voice": {"file_id": "x"}}), ("probe_en.oga", {"audio": {"file_id": "x"}, "caption": "please note"}),
                   ("silence.oga", {"voice": {"file_id": "x"}})):
    sent.clear(); said.clear()
    b.on_message({"chat": {"id": 1}, "from": {"id": 42}, **msg})
    print(AUDIO, list(msg)[0], "| echoed:", sent, "| to Clara:", said)
assert said == [] and "could not understand" in sent[0]
print("PASS")
