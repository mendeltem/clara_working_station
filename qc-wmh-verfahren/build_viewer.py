# Build the method-comparison QC page (quality_control.html) for qc-wmh-verfahren.
#
#   python build_viewer.py            # run inside qc-wmh-verfahren/
#
# 1. Every qc_sub-XXX.html that still embeds its volumes (const D={...}) is
#    unpacked into daten/sub-XXX.js (FLAIR, reference, one mask per method);
#    the page then becomes a redirect to quality_control.html#sub-XXX&on=<method>.
# 2. From daten/*.js it counts lesions per case and method (26-connected,
#    TP/FP/FN, lesion F1) and the axial slices that hold reference or
#    prediction, and keeps Dice, precision, sensitivity and volume from the
#    original pages (cases.json).
# 3. quality_control.html is written from the shared template
#    (../qc_viewer_template.html via ../qc_shared.py); index.html forwards to it.
#
# Re-running is safe. When the generator on Alita writes new qc_sub pages, run again.

import base64
import glob
import gzip
import json
import os
import re
import sys

import nibabel as nib
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import qc_shared  # noqa: E402

DATEN = os.path.join(HERE, "daten")
CASES = os.path.join(HERE, "cases.json")
os.makedirs(DATEN, exist_ok=True)
cases = json.load(open(CASES)) if os.path.exists(CASES) else {}

def volume(b64):
    return nib.Nifti1Image.from_bytes(gzip.decompress(base64.b64decode(b64)))

# --- 1. unpack old per-patient pages -------------------------------------------
for page in sorted(glob.glob(os.path.join(HERE, "qc_sub-*.html"))):
    text = open(page, encoding="utf-8").read()
    m = re.search(r"const D=(\{.*?\});\s*\nconst msg", text, flags=re.S)
    if not m:
        continue
    D = json.loads(m.group(1))
    sub = D["sub"]
    ref_layer = next(e for e in D["ebenen"] if e["datei"] == "ref")
    methods = [e for e in D["ebenen"] if e["datei"] != "ref"]
    daten = {"flair": D["flair"], "ref": ref_layer["b64"], "masks": {e["name"]: e["b64"] for e in methods}}
    with open(os.path.join(DATEN, f"{sub}.js"), "w") as fh:
        fh.write(f'window.QC_DATEN=window.QC_DATEN||{{}};window.QC_DATEN["{sub}"]={json.dumps(daten)};')
    cases[sub] = {"sub": sub, "form": D.get("form", ""), "voxel": D.get("voxel", ""), "ref_ml": ref_layer.get("ml"),
                  "m": {e["name"]: {"dice": e["dice"], "prec": e["praez"], "sens": e["sens"], "ml": e["ml"]} for e in methods}}
    print(f"unpacked {sub}")

# --- 2. lesion counts and slice marks from the stored masks --------------------
for sub, c in cases.items():
    t = open(os.path.join(DATEN, f"{sub}.js")).read()
    d = json.loads(t[t.index("=", t.index("QC_DATEN[")) + 1:].rstrip().rstrip(";"))
    ref = np.asarray(volume(d["ref"]).dataobj) > 0
    c["ref_slices"] = qc_shared.axial_slices(ref)
    c["z0"] = int(np.argmax(ref.sum(axis=(0, 1)))) if ref.any() else ref.shape[2] // 2
    c["slices"] = {}
    for name, b64 in d["masks"].items():
        pred = np.asarray(volume(b64).dataobj) > 0
        counts = qc_shared.lesion_counts(ref, pred)
        c["ref_lesions"] = counts.pop("lesions")
        c["m"].setdefault(name, {}).update(counts)
        c["slices"][name] = qc_shared.axial_slices(pred)
json.dump(cases, open(CASES, "w"), indent=1)

# old links: the method pages call qc_sub-XXX.html#v=<method>
for sub in cases:
    with open(os.path.join(HERE, f"qc_{sub}.html"), "w", encoding="utf-8") as fh:
        fh.write(f'<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><title>QC {sub}</title>'
                 f'<script>const v=(location.hash.match(/v=([^&]*)/)||[])[1];'
                 f'location.replace("quality_control.html#{sub}"+(v?"&on="+v:""));</script>'
                 f'<meta http-equiv="refresh" content="0; url=quality_control.html#{sub}"></head>'
                 f'<body><a href="quality_control.html#{sub}">QC {sub}</a></body></html>')

# --- 3. texts (English, they live here, next to the code that computes the numbers) ---
faelle = sorted(cases.values(), key=lambda c: c["sub"])
names = sorted({n for c in faelle for n in c["m"]})
colors = qc_shared.method_colors(names)
methods = qc_shared.method_table(faelle, colors)

def run_text(n):
    if n.startswith("bianca-n201"):
        rest = n[len("bianca-n201"):].lstrip("-")
        t = "FSL BIANCA, k-NN classifier trained on 201 manually labelled cases (model n201)"
        thr = re.search(r"t(\d\d)", rest)
        t += f"; lesion probability thresholded at 0.{thr.group(1)}" if thr else "; default threshold of the pipeline"
        if "c3" in rest:
            t += "; suffix c3 from the run name, its exact meaning (presumably a minimum cluster size of 3 voxels) is not recorded in the page data"
        return t + "."
    if n.startswith("monai-"):
        return f"MONAI {n[6:]} trained on FLAIR in the WMH arena on Alita; training settings are not recorded in the page data."
    if n == "unet-eigen":
        return "Own U-Net written in plain PyTorch (\"eigen\" = own); training settings are not recorded in the page data."
    if n == "unet-vorhersage":
        return "Predictions of an earlier U-Net run (\"Vorhersage\" = prediction); training settings are not recorded in the page data."
    return "No description recorded for this run."

n_cases = len(faelle)
meta = {
    "title": "WMH methods compared",
    "generator": "qc-wmh-verfahren/build_viewer.py",
    "wiki": "https://github.com/mendeltem/blackbeard_wikki/tree/main/knowledge/mri_qc_viewers",
    "methods_text": {n: run_text(n) for n in names},
    "hover": {
        "dice": "Dice = 2|A∩R| / (|A| + |R|) over voxels, R = reference label 1, computed on Alita by the QC generator under the challenge rule (predictions on label 2 deleted); mean over the cases of this page.",
        "f1_case": "Lesion F1 = 2TP / (2TP + FP + FN). Lesions are 26-connected components of the stored masks (scipy.ndimage.label). TP = reference lesions touched by the method, FN = reference lesions not touched, FP = method lesions touching no reference. Label 2 is not in the page data, so predictions on label 2 are not removed here.",
        "f1_head": f"Mean: average of the per-case lesion F1 over the {n_cases} cases. Pooled: 2·ΣTP / (2·ΣTP + ΣFP + ΣFN) over all lesions of all cases. The mean is lower because cases with few lesions drop to 0 with one miss.",
        "ref_ml": "Reference WMH volume in ml: voxels of label 1 times voxel volume, from the original challenge mask.",
        "click": "Lesion = 26-connected component in the stored mask; matched by touching (any shared voxel).",
    },
    "text": {
        "what": f"Two linked axial views of the same FLAIR, same slice: the challenge reference on the left (blue), the methods on the right as outlines, each in its own colour, at most three at once. {n_cases} public test cases of the WMH Segmentation Challenge 2017. The slider bar marks slices with reference (red) and with a shown method (green).",
        "numbers": [
            "Dice: " + "Dice = 2|A∩R| / (|A| + |R|) over voxels against reference label 1, from the original volumes under the challenge rule (predictions on label 2 deleted).",
            "Lesion F1: 2TP / (2TP + FP + FN) with 26-connected lesions counted on the masks stored in this page; label 2 is not stored, so it is not removed for this count.",
            "Mean versus pooled: the mean averages per-case F1; the pooled value counts all lesions of all cases together. Both are shown because cases with one to four reference lesions fall to 0 with a single miss.",
            f"These {n_cases} cases are a sample for looking, not a result on the full test set.",
        ],
        "data": 'Only the public <a href="https://wmh.isi.uu.nl/">WMH Segmentation Challenge</a> data: Kuijf, H. J. et al., IEEE TMI 38(11), 2019, '
                '<a href="https://doi.org/10.1109/TMI.2019.2905770">doi:10.1109/TMI.2019.2905770</a>; data <a href="https://doi.org/10.34894/AECRSD">doi:10.34894/AECRSD</a>, '
                'licence CC BY-NC 4.0. Images are modified (brain-extracted, cropped, 8 bit) for viewing, not for computing. Viewer: '
                '<a href="https://github.com/niivue/niivue">NiiVue</a> (BSD-2-Clause).',
    },
}
qc_shared.write_page(HERE, faelle, methods, meta)
print(f"quality_control.html: {n_cases} cases, {len(methods)} methods; best mean Dice {methods[0]['name']} {methods[0]['dice']}, "
      f"F1 mean {methods[0]['f1']} pooled {methods[0]['f1_pooled']}")
