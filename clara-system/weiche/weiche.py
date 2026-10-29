"""Weiche: which LLM a Clara job uses, which topic brief it gets, and a quick verdict on a result (29.10.2026).

User 29.10.: Clara uses the Charité API Clé by default; the local GPU model only when it is needed (patient data).
System One (H2O-Lightning-4B, local, CPU or GPU) picks the task type -> Clé model and the topic brief (themen/<x>.md,
passed as hermes skill thema-<x>), and gives a first yes/no on whether a result answers the job.

Order of a decision (entscheiden):
  1. fixed rules: data paths, patient ids, cohort names, image files or a marker ("hoher Datenschutz", "ORT: lokal")
     -> ort=lokal. Nothing about such a job is sent to Clé, and System One only sees it locally anyway.
  2. explicit header lines in the job text: "MODELL: <alias>", "THEMA: <name>", "ORT: cle"
  3. System One: task type (weiche.json "arten") and topic, each only at p >= schwelle; below -> standard_modell / none.
  System One down -> standard_modell, no topic (the job still runs).

CLI (werkzeuge/weiche):
  weiche entscheiden "text" | --datei F [--json]     decision as JSON
  weiche bewerten --auftrag F --ergebnis F           System One verdict: does the result answer the job (p)
  weiche frag [--ort cle|lokal|auto] [-m M] "prompt" one chat completion through the chosen endpoint
  weiche messen [--force true]                       routing accuracy on the labelled cases (ergebnisse/weiche)
Log: one JSON line per decision in ~/local_agentic_system/ergebnisse/weiche/log.jsonl (private: job texts).
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

HIER = os.path.dirname(os.path.abspath(__file__))
SYSTEM = os.path.dirname(HIER)
# weiche.json is the local configuration (cohort names, data folders: never published); weiche.example.json the template
KONFIG = json.load(open(os.path.join(HIER, "weiche.json" if os.path.exists(os.path.join(HIER, "weiche.json"))
                                     else "weiche.example.json")))
THEMEN = os.path.join(SYSTEM, "themen")
ERGEBNISSE = os.path.expanduser(os.environ.get("WEICHE_ERGEBNISSE", "~/local_agentic_system/ergebnisse/weiche"))
LOG = os.path.join(ERGEBNISSE, "log.jsonl")
LOKAL_BEDARF = os.path.join(SYSTEM, "werkzeuge", "lokal-bedarf")
# starts the local model server when needed (here: werkzeuge/modellstart, which checks free VRAM); without it the
# relay assumes the local server is already up
MODELLSTART = KONFIG.get("lokal_start") or os.path.join(SYSTEM, "werkzeuge", "modellstart")

ID_MUSTER = [re.compile(m) for m in KONFIG["id_muster"]]
KOPF = re.compile(r"^\s*(MODELL|THEMA|ORT)\s*:\s*(\S+)\s*$", re.M | re.I)


def _post(url, body, timeout=30, headers=None):
    req = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json", **(headers or {})})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # Clé and loopback: never via proxy
    with opener.open(req, timeout=timeout) as r:
        return json.loads(r.read())


# --- 1. fixed rules -------------------------------------------------------------------------------------------------

def schutzgruende(text, pfade=True):
    """Reasons why a text must stay on the local model (empty list = may go to Clé).
    pfade=True for agent jobs (an agent in a data folder reads the data); a plain completion (frag) only sends its
    text, so there a path name alone is no reason -- ids, cohort names, image files and markers still are."""
    gruende = []
    low = text.lower()
    gruende += [f"marker '{m}'" for m in KONFIG["lokal_marker"] if m.lower() in low]
    home = os.path.expanduser("~")
    for p in (KONFIG["datenpfade"] if pfade else []):
        if p.replace("~", home) in text or p in text:
            gruende.append(f"data path {p}")
    gruende += [f"pattern {rx.pattern[:24]}" for rx in ID_MUSTER if rx.search(text)]
    return gruende


# --- 3. System One ----------------------------------------------------------------------------------------------------

def systemone(state, frage, optionen, timeout=30):
    """One choice question to System One -> (label, p) or (None, 0.0) if it is not reachable."""
    try:
        r = _post(KONFIG["systemone_url"], {"state": state[:6000], "questions": {
            "q": {"type": "choice", "instructions": frage, "criteria": optionen}}}, timeout=timeout)
        a = r["answers"]["q"]
        label = a.get("choice")
        return label, float(a.get("probabilities", {}).get(label, 0.0))   # same p as the approval gate
    except Exception as e:                                       # noqa: BLE001 -- System One down must not stop a job
        return None, 0.0


def themenliste(ohne_kohorten):
    """{name: first heading line} of system/themen/*.md; for Clé only briefs without cohort knowledge."""
    out = {}
    for f in sorted(os.listdir(THEMEN)):
        if not f.endswith(".md") or f.startswith("README"):
            continue
        text = open(os.path.join(THEMEN, f), encoding="utf-8").read()
        if ohne_kohorten and any(rx.search(text) for rx in ID_MUSTER):    # cohort knowledge; paths are fine
            continue
        kopf = next((l.lstrip("# ").strip() for l in text.splitlines() if l.strip()), f)
        out[f[:-3]] = kopf[:100]
    return out


def entscheiden(text, ort_vorgabe=None, quelle="", protokoll=True, mit_thema=True):
    t0 = time.time()
    d = {"ort": "cle", "modell": KONFIG["standard_modell"], "thema": None, "art": None, "p_art": 0.0, "p_thema": 0.0,
         "gruende": [], "quelle": quelle}
    schutz = schutzgruende(text)
    kopf = {k.upper(): v for k, v in KOPF.findall(text)}
    if schutz or ort_vorgabe == "lokal" or kopf.get("ORT", "").lower() == "lokal":
        d.update(ort="lokal", modell="lokal")
        d["gruende"] = schutz or ["ORT lokal requested"]
    elif ort_vorgabe == "cle" or kopf.get("ORT", "").lower() == "cle":
        d["gruende"].append("ORT cle requested")
    arten = KONFIG["arten"]
    if d["ort"] == "cle" and "MODELL" not in kopf:
        frage = "Which kind of work is this job for an AI agent? " + "; ".join(
            f"{k} = {v['beschreibung']}" for k, v in arten.items())
        art, p = systemone(text, frage, list(arten))
        d["art"], d["p_art"] = art, round(p, 3)
        if art in arten and p >= KONFIG["schwelle"]:
            d["modell"] = arten[art]["modell"]
            d["gruende"].append(f"type {art} p={p:.2f}")
        else:
            d["gruende"].append(f"type unsure ({art} p={p:.2f}) -> standard {KONFIG['standard_modell']}")
    if "MODELL" in kopf and d["ort"] == "cle":
        d["modell"] = kopf["MODELL"]
    # topic brief: local jobs may get any brief, Clé jobs only briefs without cohort knowledge
    themen = themenliste(ohne_kohorten=d["ort"] == "cle")
    if "THEMA" in kopf:
        d["thema"] = kopf["THEMA"] if kopf["THEMA"] in themen else None
    elif themen and mit_thema:
        frage = "Which topic brief helps an AI agent most with this job? none = no brief fits. " + "; ".join(
            f"{k} = {v}" for k, v in themen.items())
        th, p = systemone(text, frage, ["none"] + list(themen))
        d["p_thema"] = round(p, 3)
        if th in themen and p >= KONFIG["schwelle"]:
            d["thema"] = th
    d["ms"] = round((time.time() - t0) * 1000)
    if protokoll:
        os.makedirs(ERGEBNISSE, exist_ok=True)
        with open(LOG, "a") as f:
            f.write(json.dumps({"zeit": time.strftime("%Y-%m-%dT%H:%M:%S"), "text": text[:2000], **d},
                               ensure_ascii=False) + "\n")
    return d


def bewerten(auftrag, ergebnis):
    """System One's first verdict: does the result answer the job? -> {"urteil": ja|nein|unsicher, "p": ...}"""
    state = f"JOB:\n{auftrag[:3000]}\n\nRESULT:\n{ergebnis[-3000:]}"
    label, p = systemone(state, "Does the RESULT fully answer the JOB, with the requested files or numbers present?",
                         ["yes", "no"])
    urteil = label if label and p >= KONFIG["schwelle"] else "unsicher"
    return {"urteil": {"yes": "ja", "no": "nein"}.get(urteil, urteil), "label": label, "p": round(p, 3)}


# --- endpoints --------------------------------------------------------------------------------------------------------

def cle_key():
    return open(os.path.expanduser(KONFIG["cle_keydatei"])).read().strip()


def frag(messages, ort="auto", modell=None, max_tokens=4000, timeout=600, **extra):
    """One chat completion. ort=auto decides by the fixed rules on the message text (no System One call)."""
    text = "\n".join(m.get("content", "") if isinstance(m.get("content"), str) else "" for m in messages)
    if ort == "auto":
        ort = "lokal" if schutzgruende(text, pfade=False) else "cle"
    if ort == "cle":
        url, hdr = KONFIG["cle_url"].rstrip("/") + "/chat/completions", {"Authorization": f"Bearer {cle_key()}"}
        modell = modell or KONFIG["standard_modell"]
        max_tokens = max(max_tokens, 8000)     # frontier/fast think first: below ~6000 the answer can come back empty
        extra.pop("chat_template_kwargs", None)
    else:
        # local GPU only on demand: register the need (modellwache keeps the server up while a need lives), start
        subprocess.run([LOKAL_BEDARF, "an", str(os.getpid()), "weiche-frag"], check=False)
        if os.path.exists(MODELLSTART) and subprocess.run([MODELLSTART, "--warten", "600", "--leise"],
                                                          capture_output=True).returncode != 0:
            raise RuntimeError("local model could not be started (modellstart)")
        url, hdr = KONFIG["lokal_url"].rstrip("/") + "/chat/completions", {}
        modell = modell or json.load(urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
            KONFIG["lokal_url"] + "/models", timeout=10))["data"][0]["id"]
    body = {"model": modell, "messages": messages, "max_tokens": max_tokens, **extra}
    r = _post(url, body, timeout=timeout, headers=hdr)
    return r["choices"][0]["message"].get("content") or "", {"ort": ort, "modell": modell, "usage": r.get("usage")}


# --- measurement ------------------------------------------------------------------------------------------------------

def messen(force=False):
    """Routing accuracy of System One on the labelled cases ergebnisse/weiche/faelle.jsonl {"text", "art"}."""
    faelle = os.path.join(ERGEBNISSE, "faelle.jsonl")
    aus = os.path.join(ERGEBNISSE, "messung.json")
    if os.path.exists(aus) and not force:
        print(open(aus).read()); return
    rows = [json.loads(l) for l in open(faelle) if l.strip()]
    treffer, abgedeckt, falsch_sicher, details = 0, 0, 0, []
    for r in rows:
        d = entscheiden(r["text"], ort_vorgabe="cle", quelle="messen", protokoll=False, mit_thema=False)
        sicher = d["p_art"] >= KONFIG["schwelle"]
        ok = d["art"] == r["art"]
        treffer += ok; abgedeckt += sicher; falsch_sicher += sicher and not ok
        details.append({"soll": r["art"], "ist": d["art"], "p": d["p_art"], "thema": d["thema"]})
    n = len(rows)
    out = {"n": n, "genauigkeit": round(treffer / n, 3), "abdeckung": round(abgedeckt / n, 3),
           "sicher_aber_falsch": falsch_sicher, "schwelle": KONFIG["schwelle"], "details": details}
    json.dump(out, open(aus, "w"), indent=1, ensure_ascii=False)
    print(json.dumps({k: v for k, v in out.items() if k != "details"}, ensure_ascii=False))


def main():
    ap = argparse.ArgumentParser(prog="weiche", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("entscheiden"); e.add_argument("text", nargs="?"); e.add_argument("--datei")
    e.add_argument("--ort", choices=("auto", "cle", "lokal"), default="auto"); e.add_argument("--quelle", default="cli")
    e.add_argument("--feld", help="print only this field (ort, modell, thema)")
    b = sub.add_parser("bewerten"); b.add_argument("--auftrag", required=True); b.add_argument("--ergebnis", required=True)
    f = sub.add_parser("frag"); f.add_argument("prompt"); f.add_argument("--ort", default="auto"); f.add_argument("-m")
    m = sub.add_parser("messen"); m.add_argument("--force", default="false")
    a = ap.parse_args()
    if a.cmd == "entscheiden":
        text = open(a.datei, encoding="utf-8", errors="replace").read() if a.datei else (a.text or sys.stdin.read())
        d = entscheiden(text, None if a.ort == "auto" else a.ort, quelle=a.quelle)
        print(d.get(a.feld) or "" if a.feld else json.dumps(d, ensure_ascii=False))
    elif a.cmd == "bewerten":
        lies = lambda p: open(p, encoding="utf-8", errors="replace").read() if os.path.exists(p) else p
        print(json.dumps(bewerten(lies(a.auftrag), lies(a.ergebnis)), ensure_ascii=False))
    elif a.cmd == "frag":
        text, info = frag([{"role": "user", "content": a.prompt}], ort=a.ort, modell=a.m)
        print(text); print(json.dumps(info), file=sys.stderr)
    elif a.cmd == "messen":
        messen(a.force.lower() == "true")


if __name__ == "__main__":
    main()
