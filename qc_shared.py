# Shared pieces for the QC page generators (qc-wmh-verfahren/build_viewer.py,
# qc-unet-wmh/build_qc.py). Follows the wiki rules in knowledge/mri_qc_viewers:
# reference blue (66, 150, 255); method colours from a golden-ratio hue
# sequence that skips the blue band (hue 0.50-0.74); lesion F1 per case and
# pooled; slice marks for the slider; every number explained on hover.

import colorsys
import datetime
import json
import os

import numpy as np
from scipy import ndimage

ROOT = os.path.dirname(os.path.abspath(__file__))
REF_RGB = (66, 150, 255)
N26 = np.ones((3, 3, 3), dtype=bool)


def method_colors(names):
    """Fixed colour per method name (alphabetical order), never in the blue band."""
    out, h, phi = {}, 0.08, 0.6180339887
    for n in sorted(names):
        while 0.50 <= h <= 0.74:
            h = (h + phi) % 1.0
        r, g, b = colorsys.hsv_to_rgb(h, 0.78, 0.96)
        out[n] = [round(r * 255), round(g * 255), round(b * 255)]
        h = (h + phi) % 1.0
    return out


def lesion_counts(ref, pred):
    """TP = reference lesions touched by the prediction, FN = reference lesions not touched,
    FP = predicted lesions touching no reference. Lesions are 26-connected components."""
    lab_r, k_r = ndimage.label(ref, structure=N26)
    lab_p, k_p = ndimage.label(pred, structure=N26)
    tp = len(np.unique(lab_r[pred & (lab_r > 0)]))
    fp = k_p - len(np.unique(lab_p[ref & (lab_p > 0)]))
    fn = k_r - tp
    f1 = 2 * tp / (2 * tp + fp + fn) if (2 * tp + fp + fn) else None
    return {"tp": int(tp), "fp": int(fp), "fn": int(fn), "f1": None if f1 is None else round(f1, 4), "lesions": int(k_r)}


def axial_slices(mask):
    """Axial slice indices (third axis, RAS volumes) that contain any voxel of the mask."""
    return [int(z) for z in np.nonzero(mask.any(axis=(0, 1)))[0]]


def method_table(faelle, colors):
    """Per method: mean Dice and mean F1 over cases, pooled F1 over all lesions."""
    rows = []
    for n in sorted(colors):
        ms = [c["m"][n] for c in faelle if n in c["m"]]
        d = [m["dice"] for m in ms if m.get("dice") is not None]
        f = [m["f1"] for m in ms if m.get("f1") is not None]
        tp, fp, fn = (sum(m.get(k, 0) for m in ms) for k in ("tp", "fp", "fn"))
        rows.append({"name": n, "color": colors[n], "n": len(ms),
                     "dice": round(float(np.mean(d)), 4) if d else None,
                     "f1": round(float(np.mean(f)), 4) if f else None,
                     "f1_pooled": round(2 * tp / (2 * tp + fp + fn), 4) if (2 * tp + fp + fn) else None})
    rows.sort(key=lambda r: -(r["dice"] if r["dice"] is not None else -1))
    return rows


def write_page(out_dir, faelle, methods, meta):
    """quality_control.html from the shared template, plus index.html that forwards to it."""
    meta = dict(meta)
    meta.setdefault("built", datetime.datetime.now().strftime("%Y-%m-%d %H:%M"))
    meta["ref_rgb"] = list(REF_RGB)
    html = open(os.path.join(ROOT, "qc_viewer_template.html"), encoding="utf-8").read()
    html = (html.replace("/*FAELLE*/[]", json.dumps(faelle))
                .replace("/*METHODS*/[]", json.dumps(methods))
                .replace("/*META*/{}", json.dumps(meta)))
    with open(os.path.join(out_dir, "quality_control.html"), "w", encoding="utf-8") as fh:
        fh.write(html)
    fwd = ('<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><title>QC</title>'
           '<script>location.replace("quality_control.html"+location.hash);</script>'
           '<meta http-equiv="refresh" content="0; url=quality_control.html"></head>'
           '<body><a href="quality_control.html">Quality control</a></body></html>')
    with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8") as fh:
        fh.write(fwd)
