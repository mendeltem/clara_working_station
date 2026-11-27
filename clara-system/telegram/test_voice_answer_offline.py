"""Offline test of the spoken answer (27.11.2026): fake Telegram and fake Clara, REAL stimme-dienst on :8790.
A voice message must reach Clara with the talk hint, and her turn_end must bring a text AND a voice message.
    python3 -I test_voice_answer_offline.py <folder with fake.env and a German or English .oga/.wav probe>
"""
import io, os, sys, time, types, contextlib
S = sys.argv[1]
PROBE = sys.argv[2] if len(sys.argv) > 2 else "probe_en.oga"
os.environ.update(CLARA_TG_ENV=f"{S}/fake.env", CLARA_HOME=S)
sys.path.insert(0, os.path.expanduser("~/local_agentic_system/system/telegram"))
import clara_telegram as ct
sent, said, voices = [], [], []
ct.tg = lambda method, **kw: {"file_path": "voice/file_1.oga"} if method == "getFile" else None
@contextlib.contextmanager
def fake_open(url, timeout=0):
    yield io.BytesIO(open(PROBE if os.path.isabs(PROBE) else f"{S}/{PROBE}", "rb").read())
ct.tg_open = fake_open
ct.send = lambda chat, text, **kw: sent.append(text)
ct.send_voice = lambda chat, datei: voices.append((datei, os.path.getsize(datei))) or True
chat = ct.Chat.__new__(ct.Chat)
chat.chat, chat.question, chat.sid, chat.text, chat.status_msg, chat.tools, chat.seit = 1, None, "s1", "", None, [], None
b = ct.Bridge.__new__(ct.Bridge)
b.chat = lambda chat_id, new=False, lokal=False: chat
b.say = lambda chat_id, text: said.append(text)

t0 = time.time()
b.on_message({"chat": {"id": 1}, "from": {"id": 42}, "voice": {"file_id": "x"}})
t_hoeren = time.time() - t0
print("to Clara:", said, f"(heard in {t_hoeren:.1f} s, language {chat.sprache})")
assert said and said[0].startswith(ct.SPRECH_HINWEIS), "talk hint missing"
t1 = time.time()
chat.on_event({"kind": "turn_end", "text": "Mir geht es gut, danke. Ich teste gerade meine neue Stimme."})
for _ in range(120):
    if voices:
        break
    time.sleep(0.25)
print("text sent:", sent, "| voice:", voices, f"(voice {time.time() - t1:.1f} s after the text)")
assert sent and voices and voices[0][1] > 1000 and chat.sprache is None
# a typed message afterwards must NOT be answered by voice
said.clear(); voices.clear(); sent.clear()
chat.on_event({"kind": "turn_end", "text": "Nur Text."})
time.sleep(1)
assert sent == ["Nur Text."] and voices == [], (sent, voices)
print("PASS")
