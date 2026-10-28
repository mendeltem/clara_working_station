# Two more charts for the README and the landing page, as self-contained SVGs.
#
#   python more_charts.py
#
# dice_vs_load.svg  Dice against reference lesion volume, per case, for the best
#                   network and the best BIANCA setting (qc-wmh-verfahren/cases.json).
# wait.svg          How long a job waits before it can start: institute cluster
#                   (whole GPU, GPU share) against the workstation. Measured values,
#                   sources in the README.

import json
import math
import os

HERE = os.path.dirname(os.path.abspath(__file__))
BG, PANEL, LINE, INK, MUTED = "#080c0f", "#0f1a20", "#22363f", "#e8f1f3", "#7d99a2"
TEAL, AMBER = "#25a294", "#c97a2a"
FONT = 'font-family="ui-monospace, SFMono-Regular, Menlo, monospace"'

# --- 1. Dice against lesion load ---------------------------------------------
cases = list(json.load(open(os.path.join(HERE, "qc-wmh-verfahren", "cases.json"))).values())
A, B = "monai-attentionunet", "bianca-n201-t90c3"
pts = [(c["sub"], c["ref_ml"], c["m"][A]["dice"], c["m"][B]["dice"]) for c in cases
       if c["ref_ml"] and A in c["m"] and B in c["m"]]
pts.sort(key=lambda p: p[1])

W, H, L, R, T, Bm = 780, 430, 70, 30, 96, 58
pw, ph = W - L - R, H - T - Bm
xmin, xmax = 0.5, 100.0
X = lambda v: L + (math.log10(v) - math.log10(xmin)) / (math.log10(xmax) - math.log10(xmin)) * pw
Y = lambda v: T + (1 - v) * ph

s = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" {FONT} role="img" aria-labelledby="lt ld">',
     f'<title id="lt">Dice against reference lesion volume, {len(pts)} public test cases</title>',
     '<desc id="ld">' + "; ".join(f"{p[0]} {p[1]} ml: {A} {p[2]:.3f}, {B} {p[3]:.3f}" for p in pts) + "</desc>",
     f'<rect width="{W}" height="{H}" rx="12" fill="{PANEL}" stroke="{LINE}"/>',
     f'<text x="24" y="34" fill="{INK}" font-size="15" font-weight="700">Dice depends on how much lesion there is</text>',
     f'<text x="24" y="56" fill="{MUTED}" font-size="11.5">One case per column, same {len(pts)} public test cases; thin line joins the two methods on one case</text>']
# legend: shape and colour together
s += [f'<circle cx="30" cy="76" r="5" fill="{TEAL}"/><text x="42" y="80" fill="{INK}" font-size="11.5">{A}</text>',
      f'<rect x="{262}" y="71" width="10" height="10" rx="2" fill="{AMBER}"/><text x="{280}" y="80" fill="{INK}" font-size="11.5">{B} (best BIANCA)</text>']
for g in [0, .2, .4, .6, .8, 1.0]:
    s.append(f'<line x1="{L}" y1="{Y(g):.1f}" x2="{W - R}" y2="{Y(g):.1f}" stroke="{LINE}"/>')
    s.append(f'<text x="{L - 8}" y="{Y(g) + 4:.1f}" fill="{MUTED}" font-size="11" text-anchor="end">{g:.1f}</text>')
for v in [0.5, 1, 2, 5, 10, 20, 50, 100]:
    s.append(f'<line x1="{X(v):.1f}" y1="{T + ph}" x2="{X(v):.1f}" y2="{T + ph + 4}" stroke="{MUTED}"/>')
    s.append(f'<text x="{X(v):.1f}" y="{T + ph + 18}" fill="{MUTED}" font-size="11" text-anchor="middle">{v:g}</text>')
s.append(f'<text x="{L + pw / 2:.1f}" y="{H - 14}" fill="{MUTED}" font-size="11" text-anchor="middle">reference lesion volume, ml (log scale)</text>')
s.append(f'<text x="18" y="{T + ph / 2:.1f}" fill="{MUTED}" font-size="11" text-anchor="middle" transform="rotate(-90 18 {T + ph / 2:.1f})">Dice</text>')
for sub, ml, da, db in pts:
    x = X(ml)
    s.append(f'<g><title>{sub}: {ml} ml reference; {A} {da:.3f}, {B} {db:.3f}</title>'
             f'<rect x="{x - 7:.1f}" y="{T}" width="14" height="{ph}" fill="transparent"/>'
             f'<line x1="{x:.1f}" y1="{Y(da):.1f}" x2="{x:.1f}" y2="{Y(db):.1f}" stroke="{MUTED}" stroke-width="1"/>'
             f'<rect x="{x - 5:.1f}" y="{Y(db) - 5:.1f}" width="10" height="10" rx="2" fill="{AMBER}" stroke="{PANEL}" stroke-width="2"/>'
             f'<circle cx="{x:.1f}" cy="{Y(da):.1f}" r="5.5" fill="{TEAL}" stroke="{PANEL}" stroke-width="2"/></g>')
s.append("</svg>")
open(os.path.join(HERE, "dice_vs_load.svg"), "w", encoding="utf-8").write("\n".join(s))

small = [p for p in pts if p[1] < 5]
big = [p for p in pts if p[1] >= 5]
mean = lambda xs: sum(xs) / len(xs)
print(f"dice_vs_load.svg: {len(pts)} cases; below 5 ml n={len(small)} A {mean([p[2] for p in small]):.3f} B {mean([p[3] for p in small]):.3f}; "
      f"5 ml and more n={len(big)} A {mean([p[2] for p in big]):.3f} B {mean([p[3] for p in big]):.3f}")

# --- 2. Waiting before a job can start -----------------------------------------
rows = [
    ("Cluster · whole GPU", 9.0, "≈ 9 h until scheduled (squeue --start, 16 Oct 2026)"),
    ("Cluster · GPU share", 10 / 3600, "starts within seconds, but only small jobs fit"),
    ("Workstation", 11.4 / 3600, "no queue; idle model server wakes in 11.4 s"),
]
FOLD_H = 25 / 60          # one training fold of Clara's U-Net on the A5000: 1355-1505 s
W, L, R, T, ROW = 780, 190, 40, 96, 54
H = T + ROW * len(rows) + 56
pw = W - L - R
X = lambda v: L + v / 10 * pw
s = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" {FONT} role="img" aria-labelledby="wt wd">',
     '<title id="wt">Waiting time before a GPU job can start</title>',
     '<desc id="wd">' + "; ".join(f"{r[0]}: {r[2]}" for r in rows) + "; one training fold on the A5000 takes about 25 minutes</desc>",
     f'<rect width="{W}" height="{H}" rx="12" fill="{PANEL}" stroke="{LINE}"/>',
     f'<text x="24" y="34" fill="{INK}" font-size="15" font-weight="700">Waiting before a GPU job can start</text>',
     f'<text x="24" y="56" fill="{MUTED}" font-size="11.5">An agent needs dozens of build-train-check rounds a day; the queue is paid on every round</text>',
     f'<text x="24" y="72" fill="{MUTED}" font-size="11.5">Dashed: one training fold of Clara\'s U-Net on the A5000, ≈ 25 min, for scale</text>']
for g in range(0, 11, 2):
    s.append(f'<line x1="{X(g):.1f}" y1="{T - 6}" x2="{X(g):.1f}" y2="{T + ROW * len(rows)}" stroke="{LINE}"/>')
    s.append(f'<text x="{X(g):.1f}" y="{T + ROW * len(rows) + 18}" fill="{MUTED}" font-size="11" text-anchor="middle">{g} h</text>')
for i, (name, hours, note) in enumerate(rows):
    y = T + i * ROW + 8
    w = max(X(hours) - X(0), 3)
    col = AMBER if i == 0 else TEAL
    s.append(f'<g><title>{name}: {note}</title>'
             f'<text x="{L - 10}" y="{y + 13}" fill="{INK}" font-size="12" text-anchor="end">{name}</text>'
             f'<rect x="{X(0):.1f}" y="{y}" width="{w:.1f}" height="18" rx="3" fill="{col}"/>'
             f'<text x="{X(FOLD_H) + 10:.1f}" y="{y + 36 if i == 0 else y + 13}" fill="{MUTED}" font-size="11">{note}</text></g>')
s.append(f'<text x="{X(FOLD_H) + 6:.1f}" y="{T - 2}" fill="{INK}" font-size="11">25 min</text>')
s.insert(next(i for i, x in enumerate(s) if x.startswith('<g><title>Cluster')),
         f'<line x1="{X(FOLD_H):.1f}" y1="{T - 12}" x2="{X(FOLD_H):.1f}" y2="{T + ROW * len(rows)}" stroke="{INK}" stroke-opacity=".7" stroke-width="1.5" stroke-dasharray="4 4"/>')
s.append("</svg>")
open(os.path.join(HERE, "wait.svg"), "w", encoding="utf-8").write("\n".join(s))
print("wait.svg written")
