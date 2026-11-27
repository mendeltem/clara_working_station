"""stimme-dienst -- Clara's voice for Telegram, kept loaded so that a spoken answer comes back in seconds (27.11.2026).

User 27.11.2026: "ich will mich mit Clara unterhalten, sprachlich ... muss sehr schnell sein"; voice chosen: Serena.

    POST /hoeren   {"datei": "<audio file>"}            -> {"text": ..., "sprache": "de"|"en", "geraet": ...}
    POST /sprechen {"text": ..., "sprache": "de"|"en"}  -> {"datei": "<ogg/opus file>", "sekunden": ..., "stimme": ...}
    GET  /zustand                                        -> what is loaded where

Everything runs on Alita, nothing leaves the machine (HF_HUB_OFFLINE=1). Listens on 127.0.0.1:8790 only.
Hearing: faster-whisper large-v3-turbo (same model as werkzeuge/hoeren), German or English only (26.11.2026: a 1-s
German message came out as Korean when the language was free). Speaking: Qwen3-TTS 0.6B CustomVoice, speaker Serena.
Measured 27.11.2026 on the A5000: hearing 10 s of speech 0.2-0.5 s, Serena ~0.6 s per second of speech, ~4 GB together.

The graphics card belongs to the 27B and to training first: the models load on the card only when at least
MIN_FREI_MIB are free; otherwise hearing runs on the processor and Piper kerstin speaks (instant, processor).
27.11.2026 (user: "kann das immer geladen sein?"): the models stay loaded (~5 GB) and load by themselves when the card
has room. Only the 27B moves them off: modellstart calls POST /entladen before it starts; while llama-server runs and
HALTEN_S after an unload nothing reloads (Piper and the processor answer meanwhile).
"""
import json, os, re, subprocess, tempfile, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
import soundfile as sf

PORT = 8790
MIN_FREI_MIB = 4500
HALTEN_S = 5 * 60          # after /entladen: no reload for this long (the 27B is loading)
MAX_ZEICHEN = 700          # a spoken answer is short; longer text is cut at a sentence end (the full text is in the chat)
QWEN = os.path.expanduser("~/.cache/huggingface/hub/models--Qwen--Qwen3-TTS-12Hz-0.6B-CustomVoice/snapshots/"
                          "85e237c12c027371202489a0ec509ded67b5e4b5")
WHISPER = "mobiuslabsgmbh/faster-whisper-large-v3-turbo"
PIPER = os.path.expanduser("~/local_agentic_system/modelle/stimme-de/piper/de_DE-kerstin-low.onnx")
FFMPEG = os.path.join(os.path.dirname(os.path.realpath(__import__("sys").executable)), "ffmpeg")
AUSGABE = tempfile.mkdtemp(prefix="clara-stimme-")

lock = threading.Lock()
m = {"whisper": None, "whisper_geraet": None, "qwen": None, "piper": None, "zuletzt": time.time(), "halten_bis": 0.0}


def log(*a):
    print(time.strftime("%F %T"), *a, flush=True)


def frei_mib():
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=10).stdout
        return int(out.split()[0])
    except Exception:  # noqa: BLE001 -- no card information: behave as if the card were full
        return 0


def karte_erlaubt():
    """The card may take the voice: the 27B is not running or starting, and no unload hold is active."""
    if time.time() < m["halten_bis"]:
        return False
    zustand = subprocess.run(["systemctl", "--user", "is-active", "llama-server"], capture_output=True, text=True).stdout.strip()
    return zustand not in ("active", "activating", "reloading")


def whisper():
    from faster_whisper import WhisperModel
    if m["whisper"] is None or (m["whisper_geraet"] == "cpu" and m["qwen"] is not None):
        if karte_erlaubt() and (frei_mib() >= MIN_FREI_MIB or m["qwen"] is not None):
            m["whisper"], m["whisper_geraet"] = WhisperModel(WHISPER, device="cuda", compute_type="float16"), "cuda"
        else:
            m["whisper"], m["whisper_geraet"] = WhisperModel(WHISPER, device="cpu", compute_type="int8", cpu_threads=8), "cpu"
        log("hearing loaded on", m["whisper_geraet"])
    return m["whisper"]


def qwen():
    """Serena on the card, or None when the card has no room (then Piper speaks)."""
    if m["qwen"] is None:
        if not karte_erlaubt() or frei_mib() < MIN_FREI_MIB:
            return None
        import torch
        from qwen_tts import Qwen3TTSModel
        m["qwen"] = Qwen3TTSModel.from_pretrained(QWEN, device_map="cuda:0", dtype=torch.bfloat16, attn_implementation="sdpa")
        log("Serena loaded on the card")
    return m["qwen"]


def piper():
    if m["piper"] is None:
        from piper import PiperVoice
        m["piper"] = PiperVoice.load(PIPER)
    return m["piper"]


def frei_machen(grund):
    """Unload hearing and Serena from the card (caller holds the lock)."""
    if m["qwen"] is not None or m["whisper_geraet"] == "cuda":
        m["qwen"], m["whisper"], m["whisper_geraet"] = None, None, None
        import gc, torch
        gc.collect(); torch.cuda.empty_cache()
        log("unloaded:", grund)


def wache():
    """Every minute: keep the voice loaded when the card may take it; move off if the 27B runs anyway."""
    while True:
        try:
            with lock:
                if not karte_erlaubt():
                    if time.time() >= m["halten_bis"]:          # the 27B runs (started without /entladen): give way
                        frei_machen("the 27B is running")
                elif m["qwen"] is None or m["whisper_geraet"] != "cuda":
                    if qwen() is not None:
                        whisper()
                        log("loaded and kept ready on the card")
        except Exception as e:  # noqa: BLE001 -- try again next minute
            log("wache", type(e).__name__, str(e)[:200])
        time.sleep(60)


def hoeren(datei):
    roh = subprocess.run([FFMPEG, "-v", "error", "-i", datei, "-f", "s16le", "-ac", "1", "-ar", "16000", "-"],
                         capture_output=True, check=True).stdout
    audio = np.frombuffer(roh, np.int16).astype(np.float32) / 32768.0
    w = whisper()
    _, _, wahrsch = w.detect_language(audio, vad_filter=True)
    p = dict(wahrsch)
    sprache = "de" if p.get("de", 0) >= p.get("en", 0) else "en"
    teile, _ = w.transcribe(audio, language=sprache, beam_size=5, vad_filter=True)
    return " ".join(t.text.strip() for t in teile).strip(), sprache


def sprechbar(text):
    """Chat text -> what can be said: no footer, code, links, markdown or table pipes; cut at a sentence end."""
    text = text.split("\n\n— ")[0]
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"\S*/\S+", "", text)          # file paths are unreadable aloud ("ergebnisse/x.md"); the chat has them
    text = re.sub(r"[*_#>|]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > MAX_ZEICHEN:
        cut = max(text.rfind(z, 0, MAX_ZEICHEN) for z in ".!?")
        text = text[:cut + 1] if cut > 80 else text[:MAX_ZEICHEN]
    return text


def sprechen(text, sprache):
    text = sprechbar(text)
    if not text:
        raise ValueError("nothing to say")
    wav = os.path.join(AUSGABE, f"antwort-{time.time():.3f}.wav")
    q = qwen()
    if q is not None:
        saetze = [s for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()] or [text]
        sp = "German" if sprache == "de" else "English"
        w, sr = q.generate_custom_voice(text=saetze, language=[sp] * len(saetze), speaker=["Serena"] * len(saetze))
        pause = np.zeros(int(sr * 0.25), dtype=np.float32)
        audio = np.concatenate([np.concatenate([np.asarray(x, dtype=np.float32), pause]) for x in w])
        sf.write(wav, audio, sr)
        stimme = "Serena (card)"
    else:
        import wave
        with wave.open(wav, "wb") as f:
            piper().synthesize_wav(text, f)
        stimme = "Piper kerstin (processor; the card is full)"
    ogg = wav[:-4] + ".ogg"
    subprocess.run([FFMPEG, "-loglevel", "error", "-y", "-i", wav, "-c:a", "libopus", "-b:a", "32k",
                    "-application", "voip", ogg], check=True)
    os.unlink(wav)
    return ogg, sf.info(ogg).duration if os.path.exists(ogg) else 0.0, stimme


class Handler(BaseHTTPRequestHandler):
    def antwort(self, code, obj):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path == "/zustand":
            self.antwort(200, {"hoeren": m["whisper_geraet"], "serena": m["qwen"] is not None, "frei_mib": frei_mib()})
        else:
            self.antwort(404, {"fehler": "unknown path"})

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(n) or b"{}")
            t0 = time.time()
            with lock:
                m["zuletzt"] = time.time()
                if self.path == "/entladen":
                    m["halten_bis"] = time.time() + HALTEN_S
                    frei_machen("the 27B is about to start")
                    out = {"entladen": True, "frei_mib": frei_mib()}
                elif self.path == "/hoeren":
                    text, sprache = hoeren(req["datei"])
                    out = {"text": text, "sprache": sprache, "geraet": m["whisper_geraet"]}
                elif self.path == "/sprechen":
                    datei, sek, stimme = sprechen(str(req.get("text") or ""), req.get("sprache") or "de")
                    out = {"datei": datei, "sekunden": round(sek, 1), "stimme": stimme}
                else:
                    return self.antwort(404, {"fehler": "unknown path"})
                m["zuletzt"] = time.time()
            out["dauer_s"] = round(time.time() - t0, 2)
            log(self.path, {k: v for k, v in out.items() if k != "text"})
            self.antwort(200, out)
        except Exception as e:  # noqa: BLE001 -- the bridge falls back to text only
            log(self.path, "error", type(e).__name__, str(e)[:200])
            self.antwort(500, {"fehler": f"{type(e).__name__}: {e}"[:300]})

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    threading.Thread(target=wache, daemon=True).start()
    log("stimme-dienst on 127.0.0.1:%d, output %s" % (PORT, AUSGABE))
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
