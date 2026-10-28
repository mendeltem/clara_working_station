# Build the QC page for one segmentation run (quality_control.html) from NIfTI volumes.
#
#   python build_qc.py \
#     --flair '/path/{sub}/flair.nii.gz' \
#     --ref   '/path/{sub}/wmh.nii.gz' \
#     --pred  '/path/oof/{sub}_pred.nii.gz'
#
# {sub} is replaced by each case id. Without --subs, the case ids are found by
# globbing the --flair pattern. Output goes next to this script unless --out is
# given: quality_control.html (shared template ../qc_viewer_template.html),
# index.html forwarding to it, and daten/<sub>.js.
#
# Counting follows the official WMH Challenge evaluation: reference = label 1,
# predictions on label 2 are deleted before counting, lesions are 26-connected
# (fully connected). Volumes must share one voxel grid; nothing is resampled.
#
# Only for released public datasets (wmh-challenge). The page embeds images.

import argparse
import base64
import glob
import gzip
import json
import os
import re
import shutil
import sys

import nibabel as nib
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import qc_shared  # noqa: E402

p = argparse.ArgumentParser(description="Build the QC page for one segmentation run.")
p.add_argument("--flair", required=True, help="path pattern with {sub}")
p.add_argument("--ref", required=True, help="path pattern with {sub}; labels 0/1/2")
p.add_argument("--pred", required=True, help="path pattern with {sub}; binary or probability")
p.add_argument("--subs", nargs="*", help="case ids; default: glob the --flair pattern")
p.add_argument("--threshold", type=float, default=0.5, help="for probability maps")
p.add_argument("--label", default="U-Net", help="name of the run shown on the page")
p.add_argument("--describe", default="Clara's own 2D U-Net in plain PyTorch, five-fold cross-validation; each case is predicted by the fold that did not see it.",
               help="one sentence on the run for 'What do the models mean?'")
p.add_argument("--title", default="U-Net WMH QC")
p.add_argument("--out", default=HERE)
args = p.parse_args()

OUT = os.path.abspath(args.out)
DATEN = os.path.join(OUT, "daten")
os.makedirs(DATEN, exist_ok=True)

if args.subs:
    subs = list(args.subs)
else:
    rx = re.compile("^" + re.escape(args.flair).replace(re.escape("{sub}"), "(.+?)") + "$")
    subs = sorted({rx.match(f).group(1) for f in glob.glob(args.flair.replace("{sub}", "*")) if rx.match(f)})
print(f"{len(subs)} cases")
if not subs:
    raise SystemExit("no cases found: check --flair or pass --subs")

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
    if not (np.allclose(fl_img.affine, rf_img.affine, atol=1e-3) and np.allclose(fl_img.affine, pr_img.affine, atol=1e-3)):
        raise SystemExit(f"{sub}: affines differ; volumes are not on one grid")

    ref1, ref2 = rf == 1, rf == 2
    is_binary = np.array_equal(pr, np.rint(pr))
    pred = pr > 0 if is_binary else pr >= args.threshold
    pred_m = pred & ~ref2                                   # official rule: delete prediction on label 2

    vox_ml = float(np.prod(fl_img.header.get_zooms()[:3])) / 1000.0
    n_ref, n_pred = int(ref1.sum()), int(pred_m.sum())
    tp_v = int((ref1 & pred_m).sum())
    counts = qc_shared.lesion_counts(ref1, pred_m)
    n_les = counts.pop("lesions")

    # crop to the non-zero FLAIR box, 4 voxel margin; 8-bit window 1st-99th percentile
    nz = np.argwhere(fl > 0) if (fl > 0).any() else np.argwhere(np.ones_like(fl, dtype=bool))
    lo = np.maximum(nz.min(0) - 4, 0)
    hi = np.minimum(nz.max(0) + 5, fl.shape)
    sl = tuple(slice(a, b) for a, b in zip(lo, hi))
    aff = fl_img.affine.copy()
    aff[:3, 3] = fl_img.affine[:3, :3] @ lo + fl_img.affine[:3, 3]
    c = fl[sl]
    v = c[c > 0] if (c > 0).any() else c.ravel()
    a, b = np.percentile(v, [1, 99])
    u8 = np.clip((c - a) / max(b - a, 1e-6) * 255, 0, 255).astype(np.uint8)
    r_c, p_c = ref1[sl], pred[sl]

    def packed(arr):
        img = nib.Nifti1Image(arr.astype(np.uint8), aff)
        img.header.set_xyzt_units("mm")
        return base64.b64encode(gzip.compress(img.to_bytes(), 6)).decode("ascii")

    daten = {"flair": packed(u8), "ref": packed(r_c), "masks": {args.label: packed(p_c)}}
    with open(os.path.join(DATEN, f"{sub}.js"), "w") as fh:
        fh.write(f'window.QC_DATEN=window.QC_DATEN||{{}};window.QC_DATEN["{sub}"]={json.dumps(daten)};')

    r4 = lambda x: None if x is None else round(x, 4)
    z = fl_img.header.get_zooms()[:3]
    faelle.append({
        "sub": sub, "form": "×".join(map(str, u8.shape)), "voxel": "×".join(f"{x:g}" for x in z),
        "ref_ml": round(n_ref * vox_ml, 2), "ref_lesions": n_les,
        "z0": int(np.argmax(r_c.sum(axis=(0, 1)))) if r_c.any() else u8.shape[2] // 2,
        "ref_slices": qc_shared.axial_slices(r_c), "slices": {args.label: qc_shared.axial_slices(p_c)},
        "m": {args.label: {"dice": r4(2 * tp_v / (n_ref + n_pred)) if (n_ref + n_pred) else None,
                           "sens": r4(tp_v / n_ref) if n_ref else None, "prec": r4(tp_v / n_pred) if n_pred else None,
                           "ml": round(n_pred * vox_ml, 2), **counts}},
    })
    print(f"  {sub}  F1 {counts['f1']}  ref {n_ref * vox_ml:.2f} ml")

colors = qc_shared.method_colors([args.label])
methods = qc_shared.method_table(faelle, colors)
n = len(faelle)
meta = {
    "title": args.title,
    "generator": "qc-unet-wmh/build_qc.py",
    "wiki": "https://github.com/mendeltem/blackbeard_wikki/tree/main/knowledge/mri_qc_viewers",
    "methods_text": {args.label: args.describe},
    "hover": {
        "dice": "Dice = 2|P∩R| / (|P| + |R|) over voxels, R = reference label 1, predictions on label 2 deleted (official challenge rule).",
        "f1_case": "Lesion F1 = 2TP / (2TP + FP + FN). Lesions are 26-connected components (scipy.ndimage.label, as SetFullyConnected(True) in the challenge evaluation.py). TP = reference lesions touched by the prediction, FN = reference lesions not touched, FP = predicted lesions touching no reference; predictions on label 2 deleted first.",
        "f1_head": f"Mean: average of the per-case lesion F1 over the {n} cases. Pooled: 2·ΣTP / (2·ΣTP + ΣFP + ΣFN) over all lesions of all cases.",
        "ref_ml": "Reference WMH volume in ml: voxels of label 1 times voxel volume.",
        "click": "Lesion = 26-connected component; matched by touching (any shared voxel).",
    },
    "text": {
        "what": f"Two linked axial views of the same FLAIR, same slice: the challenge reference on the left (blue), the {args.label} prediction on the right as an outline. {n} public cases of the WMH Segmentation Challenge 2017. The slider bar marks slices with reference (red) and with prediction (green).",
        "numbers": ["Dice and lesion F1 follow the official WMH Challenge evaluation: reference = label 1, predictions on label 2 deleted, lesions fully connected.",
                    "Mean versus pooled: the mean averages per-case F1; the pooled value counts all lesions together."],
        "data": 'Only the public <a href="https://wmh.isi.uu.nl/">WMH Segmentation Challenge</a> data: Kuijf, H. J. et al., IEEE TMI 38(11), 2019, '
                '<a href="https://doi.org/10.1109/TMI.2019.2905770">doi:10.1109/TMI.2019.2905770</a>; data <a href="https://doi.org/10.34894/AECRSD">doi:10.34894/AECRSD</a>, '
                'licence CC BY-NC 4.0. Images are modified (brain-extracted, cropped, 8 bit) for viewing, not for computing. Viewer: '
                '<a href="https://github.com/niivue/niivue">NiiVue</a> (BSD-2-Clause).',
    },
}
qc_shared.write_page(OUT, faelle, methods, meta)
if OUT != HERE:
    shutil.copy(os.path.join(HERE, "niivue.umd.js"), os.path.join(OUT, "niivue.umd.js"))
print(f"done: {n} cases, F1 mean {methods[0]['f1']} pooled {methods[0]['f1_pooled']}, mean Dice {methods[0]['dice']}")
