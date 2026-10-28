# Build the scrollable WMH QC page (wmhseg-style viewer) from NIfTI volumes.
#
#   python build_qc.py \
#     --flair '/path/{sub}/flair.nii.gz' \
#     --ref   '/path/{sub}/wmh.nii.gz' \
#     --pred  '/path/oof/{sub}_pred.nii.gz' \
#     --meta  cases.csv            # optional: columns sub,site,fold
#
# {sub} is replaced by each case id. Without --subs, the case ids are found
# by globbing the --flair pattern. Output goes next to this script unless
# --out is given: index.html (from template.html) and daten/<sub>.js.
#
# Metrics follow the official WMH Challenge evaluation: the reference is
# label 1, and predictions on label 2 voxels are deleted before counting.
# Lesions are 26-connected components. Volumes must share one voxel grid;
# nothing is resampled here, a mismatch stops the run.
#
# Only for released public datasets (wmh-challenge). The page embeds images.

import argparse
import base64
import csv
import glob
import gzip
import json
import os
import re
import shutil

import nibabel as nib
import numpy as np
from scipy import ndimage

p = argparse.ArgumentParser(description="Build the scrollable WMH QC page.")
p.add_argument("--flair", required=True, help="path pattern with {sub}")
p.add_argument("--ref", required=True, help="path pattern with {sub}; labels 0/1/2")
p.add_argument("--pred", required=True, help="path pattern with {sub}; binary or probability")
p.add_argument("--subs", nargs="*", help="case ids; default: glob the --flair pattern")
p.add_argument("--meta", help="CSV with columns sub,site,fold (optional)")
p.add_argument("--threshold", type=float, default=0.5, help="for probability maps")
p.add_argument("--label", default="U-Net", help="name of the method shown on the page")
p.add_argument("--title", default="U-Net WMH QC")
p.add_argument("--out", default=os.path.dirname(os.path.abspath(__file__)))
args = p.parse_args()

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.abspath(args.out)
DATEN = os.path.join(OUT, "daten")
os.makedirs(DATEN, exist_ok=True)
N26 = np.ones((3, 3, 3), dtype=bool)

# --- case list ---------------------------------------------------------------
if args.subs:
    subs = list(args.subs)
else:
    rx = re.compile("^" + re.escape(args.flair).replace(re.escape("{sub}"), "(.+?)") + "$")
    subs = sorted({rx.match(f).group(1) for f in glob.glob(args.flair.replace("{sub}", "*")) if rx.match(f)})
print(f"{len(subs)} cases")
if not subs:
    raise SystemExit("no cases found: check --flair or pass --subs")

meta = {}
if args.meta:
    with open(args.meta, newline="") as fh:
        for row in csv.DictReader(fh):
            meta[row["sub"]] = row

# --- per case ----------------------------------------------------------------
faelle = []
for sub in subs:
    fl_img = nib.as_closest_canonical(nib.load(args.flair.format(sub=sub)))
    rf_img = nib.as_closest_canonical(nib.load(args.ref.format(sub=sub)))
    pr_img = nib.as_closest_canonical(nib.load(args.pred.format(sub=sub)))
    fl = np.asarray(fl_img.dataobj, dtype=np.float32)
    rf = np.rint(np.asarray(rf_img.dataobj)).astype(np.int16)
    pr = np.asarray(pr_img.dataobj, dtype=np.float32)
    if not (fl.shape == rf.shape == pr.shape):
        raise SystemExit(f"{sub}: shapes differ {fl.shape} {rf.shape} {pr.shape}; resample first")
    if not np.allclose(fl_img.affine, rf_img.affine, atol=1e-3) or not np.allclose(fl_img.affine, pr_img.affine, atol=1e-3):
        raise SystemExit(f"{sub}: affines differ; volumes are not on one grid")

    ref1 = rf == 1
    ref2 = rf == 2
    is_binary = np.array_equal(pr, np.rint(pr))           # 0/1 mask, else probability map
    pred = pr > 0 if is_binary else pr >= args.threshold
    pred_m = pred & ~ref2                                  # official rule: delete prediction on label 2

    vox_ml = float(np.prod(fl_img.header.get_zooms()[:3])) / 1000.0
    n_ref, n_pred = int(ref1.sum()), int(pred_m.sum())
    tp = int((ref1 & pred_m).sum())
    dice = 2 * tp / (n_ref + n_pred) if (n_ref + n_pred) else None
    sens = tp / n_ref if n_ref else None
    prec = tp / n_pred if n_pred else None

    lab_r, k_r = ndimage.label(ref1, structure=N26)
    lab_p, k_p = ndimage.label(pred_m, structure=N26)
    found = len(np.unique(lab_r[pred_m & (lab_r > 0)]))     # reference lesions touched by the prediction
    true_p = len(np.unique(lab_p[ref1 & (lab_p > 0)]))      # predicted lesions touching the reference
    lrec = found / k_r if k_r else None
    lprec = true_p / k_p if k_p else None
    lf1 = 2 * lrec * lprec / (lrec + lprec) if lrec and lprec else (0.0 if k_r and k_p else None)

    # crop to the non-zero FLAIR box (brain-extracted input), 4 voxel margin
    nz = np.argwhere(fl > 0) if (fl > 0).any() else np.argwhere(np.ones_like(fl, dtype=bool))
    lo = np.maximum(nz.min(0) - 4, 0)
    hi = np.minimum(nz.max(0) + 5, fl.shape)
    sl = tuple(slice(a, b) for a, b in zip(lo, hi))
    aff = fl_img.affine.copy()
    aff[:3, 3] = fl_img.affine[:3, :3] @ lo + fl_img.affine[:3, 3]

    # 8-bit window, 1st to 99th percentile of the brain
    c = fl[sl]
    v = c[c > 0] if (c > 0).any() else c.ravel()
    a, b = np.percentile(v, [1, 99])
    u8 = np.clip((c - a) / max(b - a, 1e-6) * 255, 0, 255).astype(np.uint8)

    # start at the axial slice with the most reference lesion (else prediction, else centre)
    src = ref1[sl] if ref1.any() else (pred_m[sl] if pred_m.any() else None)
    if src is not None:
        z = int(np.argmax(src.sum(axis=(0, 1))))
        xy = np.argwhere(src[:, :, z]).mean(0)
        vox = np.array([xy[0], xy[1], z, 1.0])
    else:
        vox = np.array([*(np.array(u8.shape) / 2), 1.0])
    mm0 = [round(float(x), 2) for x in (aff @ vox)[:3]]

    def packed(arr):
        img = nib.Nifti1Image(arr.astype(np.uint8), aff)
        img.header.set_xyzt_units("mm")
        return base64.b64encode(gzip.compress(img.to_bytes(), 6)).decode("ascii")

    daten = {"flair": packed(u8), "ref": packed(ref1[sl]), "pred": packed(pred[sl])}
    js = f'window.QC_DATEN=window.QC_DATEN||{{}};window.QC_DATEN["{sub}"]={json.dumps(daten)};'
    with open(os.path.join(DATEN, f"{sub}.js"), "w") as fh:
        fh.write(js)

    r3 = lambda x: None if x is None else round(x, 4)
    m = meta.get(sub, {})
    faelle.append({
        "sub": sub, "site": m.get("site", ""), "fold": m.get("fold", ""),
        "dice": r3(dice), "sens": r3(sens), "prec": r3(prec),
        "ref_ml": round(n_ref * vox_ml, 2), "pred_ml": round(n_pred * vox_ml, 2),
        "lesions": int(k_r), "lrec": r3(lrec), "lf1": r3(lf1),
        "mm0": mm0, "slices": int(u8.shape[2]), "kb": round(len(js) / 1024),
    })
    print(f"  {sub}  Dice {dice if dice is None else round(dice, 3)}  ref {n_ref * vox_ml:.2f} ml  {len(js) // 1024} kB")

# --- summary and page ----------------------------------------------------------
def mean(key):
    xs = [f[key] for f in faelle if f[key] is not None]
    return round(float(np.mean(xs)), 4) if xs else None

dices = [f["dice"] for f in faelle if f["dice"] is not None]
zahlen = {
    "n": len(faelle), "dice": mean("dice"), "dice_median": round(float(np.median(dices)), 4) if dices else None,
    "sens": mean("sens"), "prec": mean("prec"), "lrec": mean("lrec"), "lf1": mean("lf1"),
    "below_05": sum(d < 0.5 for d in dices), "threshold": args.threshold,
}
faelle.sort(key=lambda f: (f["dice"] is None, f["dice"] if f["dice"] is not None else 0))

with open(os.path.join(HERE, "template.html"), encoding="utf-8") as fh:
    html = fh.read()
html = (html.replace("__TITLE__", args.title).replace("__LABEL__", args.label)
            .replace("/*FAELLE*/[]", json.dumps(faelle)).replace("/*ZAHLEN*/{}", json.dumps(zahlen)))
with open(os.path.join(OUT, "index.html"), "w", encoding="utf-8") as fh:
    fh.write(html)
with open(os.path.join(OUT, "uebersicht.json"), "w") as fh:
    json.dump({"zahlen": zahlen, "faelle": faelle}, fh)
if os.path.abspath(OUT) != HERE:
    shutil.copy(os.path.join(HERE, "niivue.umd.js"), os.path.join(OUT, "niivue.umd.js"))

print(f"done: {len(faelle)} cases, mean Dice {zahlen['dice']}, page {os.path.join(OUT, 'index.html')}")
