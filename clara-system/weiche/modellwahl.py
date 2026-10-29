#!/usr/bin/env python3
"""Which Clé model is best per Weiche task type (trivial, coding, suche, schwer, schreiben, bild).

Auto-scored tasks, synthetic only, no patient data. Result: ergebnisse/weiche/modellwahl_<date>.json plus a table
score/seconds per type x model. The mapping in weiche.json follows this table (best score, ties -> faster).

    python3 -I modellwahl.py --models fast,clea,frontier,eu,vision,light --repeats 2

Limit: 'suche' here is literature recall (DOIs) and refusing fake papers in one call; real web research with tools
is not measured.
"""
import argparse, base64, datetime, json, os, re, struct, subprocess, sys, tempfile, time, urllib.request, zlib
from concurrent.futures import ThreadPoolExecutor

BASE = "https://cle.charite.de/api"
KEYFILE = "~/.config/cle/api_key"
OUT = os.path.expanduser("~/local_agentic_system/ergebnisse/weiche")


# ---------- synthetic images (pure python PNG) ----------
def png(w, h, pixel):
    raw = b"".join(b"\x00" + bytes(c for x in range(w) for c in pixel(x, y)) for y in range(h))
    chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def bild_formen(x, y):  # 3 red circles, 2 blue squares on white
    for cx, cy in ((60, 60), (200, 90), (110, 210)):
        if (x - cx) ** 2 + (y - cy) ** 2 < 30 ** 2:
            return (220, 30, 30)
    for sx, sy in ((230, 190), (30, 140)):
        if sx <= x < sx + 45 and sy <= y < sy + 45:
            return (30, 60, 220)
    return (255, 255, 255)


def bild_balken(x, y):  # 4 grey bars, the 3rd from the left is tallest
    hoehen = (120, 80, 220, 160)
    for i, hh in enumerate(hoehen):
        if 30 + i * 70 <= x < 80 + i * 70 and y >= 260 - hh:
            return (90, 90, 90)
    return (255, 255, 255)


def bild_fleck(x, y):  # dark grey "slice" with one bright spot lower right
    if (x - 205) ** 2 + (y - 200) ** 2 < 18 ** 2:
        return (250, 250, 250)
    return (40, 40, 40) if (x - 140) ** 2 + (y - 140) ** 2 < 125 ** 2 else (0, 0, 0)


def url(pixel, n=280):
    return "data:image/png;base64," + base64.b64encode(png(n, n, pixel)).decode()


# ---------- tasks: (id, type, prompt or (text, image), scorer) ----------
NUM = " Answer with the number only."
CODE = " Output only one python code block, no explanation."
UNK = " If you are not certain that this paper exists, answer exactly UNKNOWN."
TASKS = [
    ("tr_mult", "trivial", "What is 17 * 23?" + NUM, ("number", 391, 0)),
    ("tr_iso", "trivial", "Write the date 29.10.2026 in ISO 8601 format. Output only the date.", ("regex", r"^\s*2026-10-29\s*$")),
    ("tr_wmh", "trivial", "What does the abbreviation WMH stand for in neuroimaging? Output only the expansion.",
     ("contains", ["white matter hyperintens"])),
    ("tr_wc", "trivial", "Give the shell command that prints the number of lines of the file x.txt. Output only the command.",
     ("contains", ["wc -l"])),
    ("tr_unit", "trivial", "How many millilitres are 27000 cubic millimetres?" + NUM, ("number", 27, 0.01)),

    ("co_merge", "coding", "Write a Python function merge_intervals(iv) that takes a list of [start, end] pairs (unsorted, "
     "closed intervals) and returns the merged, sorted list of [start, end] lists. Touching intervals ([1,2],[2,3]) merge." + CODE,
     ("code", "assert merge_intervals([])==[]\nassert merge_intervals([[5,6],[1,3],[2,4]])==[[1,4],[5,6]]\n"
              "assert merge_intervals([[1,2],[2,3]])==[[1,3]]\nassert merge_intervals([[1,10],[2,3]])==[[1,10]]\n")),
    ("co_dice", "coding", "Write a Python function dice(a, b) for two sets of voxel coordinates (tuples). It returns the Dice "
     "coefficient as float; if both sets are empty it returns 1.0." + CODE,
     ("code", "assert dice(set(),set())==1.0\nassert dice({(0,0,0)},set())==0.0\n"
              "assert abs(dice({(1,1,1),(1,1,2)},{(1,1,2),(3,3,3)})-0.5)<1e-9\n")),
    ("co_bids", "coding", "Write a Python function parse_bids(name) that parses a BIDS-like file name such as "
     "'sub-01_ses-02_acq-x_T1w.nii.gz' into a dict of its key-value entities plus 'suffix' and 'ext'. Example result: "
     "{'sub':'01','ses':'02','acq':'x','suffix':'T1w','ext':'.nii.gz'}. ext is everything from the first dot." + CODE,
     ("code", "assert parse_bids('sub-01_ses-02_acq-x_T1w.nii.gz')=={'sub':'01','ses':'02','acq':'x','suffix':'T1w','ext':'.nii.gz'}\n"
              "assert parse_bids('sub-A7_FLAIR.json')=={'sub':'A7','suffix':'FLAIR','ext':'.json'}\n")),
    ("co_fix", "coding", "This function should count 4-connected components of 1s in a 2D list grid but has bugs. Return the "
     "corrected function with the same name and signature.\n\n```python\ndef count_components(grid):\n    seen = set()\n"
     "    n = 0\n    for i in range(len(grid)):\n        for j in range(len(grid[0])):\n            if grid[i][j] == 1:\n"
     "                n += 1\n                stack = [(i, j)]\n                while stack:\n                    a, b = stack.pop()\n"
     "                    for da, db in ((1,0),(0,1)):\n                        x, y = a+da, b+db\n"
     "                        if x < len(grid) and y < len(grid[0]) and grid[x][y] == 1:\n"
     "                            stack.append((x, y))\n    return n\n```" + CODE,
     ("code", "assert count_components([[1,1,0],[0,1,0],[1,0,1]])==3\nassert count_components([[0]])==0\n"
              "assert count_components([[1,0,1],[1,1,1]])==1\nassert count_components([[0,1],[1,0]])==2\n"
              "assert count_components([[1,1,1],[1,0,1],[1,1,1]])==1\n")),
    ("co_median", "coding", "Write a Python function rolling_median(xs, w) returning a list of medians of each full window "
     "of length w (len(xs)-w+1 values; empty list if w > len(xs)). For even w the median is the mean of the two middle "
     "values, returned as float." + CODE,
     ("code", "assert rolling_median([1,3,2,5],3)==[2,3]\nassert rolling_median([1,2],3)==[]\n"
              "assert rolling_median([4,1,3,2],2)==[2.5,2.0,2.5]\n")),

    ("su_strive", "suche", "Give the DOI of the 2013 Lancet Neurology position paper by Wardlaw et al. defining the STRIVE "
     "neuroimaging standards for small vessel disease. Output only the DOI." + UNK, ("contains", ["10.1016/s1474-4422(13)70124-8"])),
    ("su_nnunet", "suche", "Give the DOI of the 2021 Nature Methods paper by Isensee et al. introducing nnU-Net. Output only "
     "the DOI." + UNK, ("contains", ["10.1038/s41592-020-01008-z"])),
    ("su_fazekas", "suche", "Give the DOI of the 1987 AJR paper by Fazekas et al. 'MR signal abnormalities at 1.5 T in "
     "Alzheimer's dementia and normal aging'. Output only the DOI." + UNK, ("contains", ["10.2214/ajr.149.2.351"])),
    ("su_greenberg", "suche", "Give the DOI of the 2009 Lancet Neurology review by Greenberg et al. 'Cerebral microbleeds: a "
     "guide to detection and interpretation'. Output only the DOI." + UNK, ("contains", ["10.1016/s1474-4422(09)70013-4"])),
    ("su_fake1", "suche", "Give the DOI of the 2018 NeuroImage paper by Hallström-Ngata and Petrov titled 'Ultrasparse "
     "transformer priors for perivascular space counting at 3T'. Output only the DOI." + UNK, ("unknown",)),
    ("su_fake2", "suche", "Give the DOI of the 2022 Stroke paper by Okonkwo-Baptiste et al. titled 'The Berlin Lacune "
     "Persistence Score'. Output only the DOI." + UNK, ("unknown",)),

    ("sw_ppv", "schwer", "A test has sensitivity 90% and specificity 95%; prevalence is 2%. What is the positive predictive "
     "value? Answer as a decimal rounded to 3 places only.", ("number", 0.269, 0.0015)),
    ("sw_bh", "schwer", "Eight p-values: 0.001, 0.008, 0.039, 0.041, 0.042, 0.060, 0.074, 0.205. Applying the "
     "Benjamini-Hochberg procedure at q = 0.05, how many hypotheses are rejected?" + NUM, ("number", 2, 0)),
    ("sw_sched", "schwer", "Six jobs take 7, 5, 4, 4, 3 and 3 hours. They run on 2 identical GPUs, each job on one GPU, no "
     "splitting. What is the minimal time until all jobs are done, in hours?" + NUM, ("number", 13, 0)),
    ("sw_jacc", "schwer", "Two masks have a Jaccard index of 0.25. What is their Dice coefficient? Answer as a decimal only.",
     ("number", 0.4, 0.001)),
    ("sw_n", "schwer", "Two-sample two-sided t-test, effect size Cohen's d = 0.5, alpha = 0.05, power = 0.80, equal groups. "
     "How many subjects per group are needed (round up)?" + NUM, ("number", 63.5, 0.6)),
    ("sw_vol", "schwer", "A lesion mask has 12500 voxels of size 1.2 x 1.2 x 1.5 mm. The image is then resampled to 1 mm "
     "isotropic and the mask is resampled with nearest neighbour; assume the volume is preserved. How many voxels does the "
     "mask have now?" + NUM, ("number", 27000, 0)),

    ("sc_abstract", "schreiben", "Schreibe einen deutschen Absatz (80 bis 120 Wörter) für einen Laien über die automatische "
     "Erkennung von Mikroblutungen im MRT. Das Wort 'Suszeptibilität' muss vorkommen. Keine Aufzählung, keine Überschrift, "
     "nur der Absatz.", ("prosa", {"min": 80, "max": 120, "muss": ["suszeptibilität"], "sprache": "de", "absaetze": 1})),
    ("sc_mail", "schreiben", "Write a short e-mail to a colleague asking to move our meeting to Thursday afternoon. Exactly 3 "
     "paragraphs (greeting line counts as part of the first), at most 90 words, no exclamation marks, sign as 'Uchral'.",
     ("prosa", {"min": 20, "max": 90, "muss": ["thursday", "uchral"], "verboten": ["!"], "sprache": "en", "absaetze": 3})),
    ("sc_leicht", "schreiben", "Schreibe diesen Satz in Leichter Sprache neu, in mindestens 3 Sätzen mit höchstens 10 Wörtern "
     "je Satz, nur der neue Text: 'Aufgrund der erhöhten Auslastung des Rechenclusters verzögert sich die Auswertung der "
     "Bilddaten voraussichtlich um mehrere Tage, weshalb wir um Ihr Verständnis bitten.'",
     ("leicht", {"saetze": 3, "max_woerter": 10})),
    ("sc_titel", "schreiben", "Give exactly 5 title suggestions for a methods paper on automated perivascular space "
     "segmentation. Numbered '1.' to '5.', one per line, each at most 10 words, nothing else.", ("titel", 5, 10)),
    ("sc_uebers", "schreiben", "Translate into English, output only the translation: 'Die Läsionsvolumina wurden für das "
     "intrakranielle Volumen korrigiert und mittels linearer gemischter Modelle mit dem Scanner als Zufallseffekt analysiert.'",
     ("contains", ["intracranial volume", "linear mixed", "random effect", "scanner"])),

    ("bi_kreise", "bild", ("How many red circles are in this image?" + NUM, bild_formen), ("number", 3, 0)),
    ("bi_quadrate", "bild", ("How many blue squares are in this image?" + NUM, bild_formen), ("number", 2, 0)),
    ("bi_balken", "bild", ("Which bar is the tallest, counting from the left (1 to 4)?" + NUM, bild_balken), ("number", 3, 0)),
    ("bi_fleck", "bild", ("In which quadrant of the image is the bright spot? Answer with one of: upper-left, upper-right, "
                          "lower-left, lower-right.", bild_fleck), ("contains", ["lower-right"])),
]


def strip_fence(t):
    m = re.search(r"```(?:python|py)?\s*\n(.*?)```", t, re.S)
    return (m.group(1) if m else t).strip()


DE = {"der", "die", "das", "und", "ist", "mit", "von", "im", "werden", "eine", "nicht", "auf", "für"}
EN = {"the", "and", "is", "to", "of", "on", "for", "we", "our", "you", "could", "would", "with"}


def woerter(t):
    return re.findall(r"[A-Za-zÄÖÜäöüß'-]+", t)


def score(spec, text):
    kind, low = spec[0], text.lower()
    if kind == "number":
        m = re.findall(r"-?\d+(?:[.,]\d+)?", re.sub(r"\b([1-9]\d{0,2})((?:,\d{3})+)\b", lambda g: g.group(1) + g.group(2).replace(",", ""), text))
        if not m:
            return 0.0
        v = float(m[0].replace(",", "."))
        return float(abs(v - spec[1]) <= spec[2])
    if kind == "regex":
        return float(bool(re.match(spec[1], text)))
    if kind == "contains":
        return sum(w in low for w in spec[1]) / len(spec[1])
    if kind == "unknown":
        return float("unknown" in low and "10." not in low)
    if kind == "code":
        with tempfile.TemporaryDirectory() as d:  # model output runs isolated: own empty dir, -I, 10 s
            p = os.path.join(d, "t.py")
            open(p, "w").write(strip_fence(text) + "\n" + spec[1])
            try:
                return float(subprocess.run([sys.executable, "-I", p], cwd=d, capture_output=True, timeout=10).returncode == 0)
            except subprocess.TimeoutExpired:
                return 0.0
    if kind == "prosa":
        c, w = spec[1], woerter(text)
        sprache = DE if c["sprache"] == "de" else EN
        teile = [
            c["min"] <= len(w) <= c["max"],
            all(m in low for m in c.get("muss", [])),
            not any(v in text for v in c.get("verboten", [])),
            sum(x.lower() in sprache for x in w) >= 3,
            len([p for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]) == c["absaetze"],
        ]
        return sum(teile) / len(teile)
    if kind == "leicht":
        saetze = [s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if woerter(s)]
        ok_len = all(len(woerter(s)) <= spec[1]["max_woerter"] for s in saetze)
        return (float(len(saetze) >= spec[1]["saetze"]) + float(ok_len) + float("tage" in low)) / 3
    if kind == "titel":
        z = [l for l in text.strip().splitlines() if l.strip()]
        ok = len(z) == spec[1] and all(re.match(rf"\s*{i + 1}\.\s", l) for i, l in enumerate(z))
        kurz = all(len(woerter(re.sub(r"^\s*\d\.\s*", "", l))) <= spec[2] for l in z)
        return (float(ok) + float(kurz)) / 2
    raise ValueError(kind)


def chat(key, model, prompt, max_tokens, timeout):
    if isinstance(prompt, tuple):
        content = [{"type": "text", "text": prompt[0]}, {"type": "image_url", "image_url": {"url": url(prompt[1])}}]
    else:
        content = prompt
    body = json.dumps({"model": model, "messages": [{"role": "user", "content": content}], "max_tokens": max_tokens}).encode()
    req = urllib.request.Request(BASE + "/chat/completions", data=body, method="POST",
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.load(r)
    msg = d["choices"][0]["message"]
    text = re.sub(r"<think>.*?</think>", "", msg.get("content") or "", flags=re.S).strip()
    return text, time.time() - t0, (d.get("usage") or {}).get("completion_tokens")


def lauf_modell(key, model, repeats, max_tokens, timeout):
    rows = []
    for tid, art, prompt, spec in TASKS:
        for rep in range(repeats):
            try:
                text, dt, tok = chat(key, model, prompt, max_tokens, timeout)
                s, err = score(spec, text), ""
            except Exception as e:
                text, dt, tok, s, err = "", 0.0, None, 0.0, f"{type(e).__name__}: {e}"[:200]
            rows.append({"model": model, "task": tid, "art": art, "repeat": rep, "score": round(s, 3), "seconds": round(dt, 2),
                         "completion_tokens": tok, "answer": text[:800], "error": err})
            print(f"{model:9s} {tid:13s} r{rep} {s:.2f} {dt:6.1f}s {err[:60]}", flush=True)
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default="fast,clea,frontier,eu,vision,light")
    ap.add_argument("--repeats", type=int, default=2)
    ap.add_argument("--max-tokens", type=int, default=8000)
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--only", default="", help="comma list of task types")
    a = ap.parse_args()
    if a.only:
        TASKS[:] = [t for t in TASKS if t[1] in a.only.split(",")]
    key = open(os.path.expanduser(KEYFILE)).read().strip()
    models = a.models.split(",")
    with ThreadPoolExecutor(len(models)) as ex:
        rows = [r for part in ex.map(lambda m: lauf_modell(key, m, a.repeats, a.max_tokens, a.timeout), models) for r in part]
    os.makedirs(OUT, exist_ok=True)
    out = os.path.join(OUT, f"modellwahl_{datetime.datetime.now():%Y%m%d_%H%M}.json")
    json.dump(rows, open(out, "w"), indent=1, ensure_ascii=False)
    arten = sorted({t[1] for t in TASKS}, key=[t[1] for t in TASKS].index)
    print(f"\n{out}\nscore (mean seconds) per type x model")
    print("type       " + "".join(f"{m:>16s}" for m in models) + "   best")
    for art in arten:
        zeile, best = f"{art:10s} ", None
        for m in models:
            r = [x for x in rows if x["model"] == m and x["art"] == art]
            sc = sum(x["score"] for x in r) / len(r)
            sec = sum(x["seconds"] for x in r) / len(r)
            zeile += f"{sc:9.2f} ({sec:4.1f})"
            if best is None or (sc, -sec) > (best[1], -best[2]):
                best = (m, sc, sec)
        print(zeile + f"   {best[0]}")


if __name__ == "__main__":
    main()
