# Mean Dice per method on the QC cases, as a self-contained SVG (results.svg).
#
#   python results_chart.py
#
# Reads qc-wmh-verfahren/cases.json (written by build_viewer.py). Bars are the
# mean Dice over the same cases; whiskers are a 95 % bootstrap interval over
# cases (10,000 resamples, seed 2026). The dashed line is the 2017 challenge
# winner on all 110 test cases, which are not the same cases as these.

import json
import os

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
cases = json.load(open(os.path.join(HERE, "qc-wmh-verfahren", "cases.json")))
WINNER = 0.80                     # sysu_media, WMH Segmentation Challenge 2017, 110 test cases

names = sorted({m for c in cases.values() for m in c["m"]})
# identical results under two names are shown once
ALIAS = {}
for a in names:
    for b in names:
        if a < b and a not in ALIAS and all(c["m"].get(a, {}).get("dice") == c["m"].get(b, {}).get("dice") for c in cases.values()):
            ALIAS[b] = a
LABEL = {a: a + " = " + b.replace(a + "-", "") for b, a in ALIAS.items()}
names = [n for n in names if n not in ALIAS]
rng = np.random.default_rng(2026)
rows = []
for n in names:
    d = np.array([c["m"][n]["dice"] for c in cases.values() if n in c["m"] and c["m"][n]["dice"] is not None])
    boot = rng.choice(d, size=(10000, len(d)), replace=True).mean(axis=1)
    lo, hi = np.percentile(boot, [2.5, 97.5])
    rows.append({"name": LABEL.get(n, n), "mean": float(d.mean()), "lo": float(lo), "hi": float(hi), "n": int(len(d))})
rows.sort(key=lambda r: -r["mean"])
for r in rows:
    print(f"{r['name']:24s} {r['mean']:.3f}  [{r['lo']:.3f}; {r['hi']:.3f}]  n={r['n']}")

# --- layout -------------------------------------------------------------------
BG, PANEL, LINE, INK, MUTED = "#080c0f", "#0f1a20", "#22363f", "#e8f1f3", "#7d99a2"
BAR, REF = "#25a294", "#f0a830"
W, LEFT, RIGHT, TOP, ROW, BAR_H = 780, 230, 74, 92, 30, 16
H = TOP + ROW * len(rows) + 64
plot_w = W - LEFT - RIGHT
x = lambda v: LEFT + v * plot_w

svg = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" '
       f'font-family="ui-monospace, SFMono-Regular, Menlo, monospace" role="img" '
       f'aria-labelledby="t d">',
       f'<title id="t">Mean Dice per method on {rows[0]["n"]} public WMH Challenge test cases</title>',
       '<desc id="d">' + "; ".join(f"{r['name']} {r['mean']:.3f}" for r in rows) + f"; 2017 winner {WINNER:.2f} on all 110 test cases</desc>",
       f'<rect width="{W}" height="{H}" rx="12" fill="{PANEL}" stroke="{LINE}"/>',
       f'<text x="24" y="34" fill="{INK}" font-size="15" font-weight="700">Mean Dice per method · {rows[0]["n"]} public test cases</text>',
       f'<text x="24" y="56" fill="{MUTED}" font-size="11.5">Bar: mean over the same cases · whisker: 95 % bootstrap interval over cases</text>',
       f'<text x="24" y="72" fill="{MUTED}" font-size="11.5">Dashed: 2017 challenge winner on all 110 test cases, not the same cases</text>']

# grid and axis
for g in [0, .2, .4, .6, .8, 1.0]:
    svg.append(f'<line x1="{x(g):.1f}" y1="{TOP - 6}" x2="{x(g):.1f}" y2="{TOP + ROW * len(rows)}" stroke="{LINE}" stroke-width="1"/>')
    svg.append(f'<text x="{x(g):.1f}" y="{TOP + ROW * len(rows) + 18}" fill="{MUTED}" font-size="11" text-anchor="middle">{g:.1f}</text>')
svg.append(f'<text x="{x(.5):.1f}" y="{TOP + ROW * len(rows) + 38}" fill="{MUTED}" font-size="11" text-anchor="middle">Dice</text>')

# bars: 4 px rounded at the data end, square at the baseline
for i, r in enumerate(rows):
    y = TOP + i * ROW + (ROW - BAR_H) / 2
    w = x(r["mean"]) - x(0)
    rr = 4
    path = (f"M{x(0):.1f},{y:.1f} H{x(0) + w - rr:.1f} Q{x(0) + w:.1f},{y:.1f} {x(0) + w:.1f},{y + rr:.1f} "
            f"V{y + BAR_H - rr:.1f} Q{x(0) + w:.1f},{y + BAR_H:.1f} {x(0) + w - rr:.1f},{y + BAR_H:.1f} H{x(0):.1f} Z")
    cy = y + BAR_H / 2
    tip = f"{r['name']}: mean Dice {r['mean']:.3f}, 95 % interval {r['lo']:.3f} to {r['hi']:.3f}, {r['n']} cases"
    svg.append(f'<g><title>{tip}</title>'
               f'<rect x="0" y="{TOP + i * ROW}" width="{W}" height="{ROW}" fill="transparent"/>'
               f'<text x="{LEFT - 10}" y="{cy + 4:.1f}" fill="{INK}" font-size="12" text-anchor="end">{r["name"]}</text>'
               f'<path d="{path}" fill="{BAR}"/>'
               f'<line x1="{x(r["lo"]):.1f}" y1="{cy:.1f}" x2="{x(r["hi"]):.1f}" y2="{cy:.1f}" stroke="{INK}" stroke-width="1.5" stroke-opacity=".75"/>'
               f'<line x1="{x(r["lo"]):.1f}" y1="{cy - 4:.1f}" x2="{x(r["lo"]):.1f}" y2="{cy + 4:.1f}" stroke="{INK}" stroke-width="1.5" stroke-opacity=".75"/>'
               f'<line x1="{x(r["hi"]):.1f}" y1="{cy - 4:.1f}" x2="{x(r["hi"]):.1f}" y2="{cy + 4:.1f}" stroke="{INK}" stroke-width="1.5" stroke-opacity=".75"/>'
               f'<text x="{W - 22}" y="{cy + 4:.1f}" fill="{INK}" font-size="11.5" text-anchor="end">{r["mean"]:.3f}</text>'
               f'</g>')

# reference line
svg.append(f'<line x1="{x(WINNER):.1f}" y1="{TOP - 10}" x2="{x(WINNER):.1f}" y2="{TOP + ROW * len(rows)}" stroke="{REF}" stroke-width="2" stroke-dasharray="5 4"/>')
svg.append(f'<text x="{x(WINNER) + 6:.1f}" y="{TOP - 12}" fill="{INK}" font-size="11">2017 winner 0.80</text>')
svg.append("</svg>")

with open(os.path.join(HERE, "results.svg"), "w", encoding="utf-8") as fh:
    fh.write("\n".join(svg))
print("results.svg written")
