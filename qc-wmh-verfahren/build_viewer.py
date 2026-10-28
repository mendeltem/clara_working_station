# Turn the per-patient QC pages into one axial slice viewer (wmhseg-style).
#
#   python build_viewer.py            # run inside qc-wmh-verfahren/
#
# 1. Every qc_sub-XXX.html that still embeds its volumes (const D={...}) is
#    unpacked into daten/sub-XXX.js (FLAIR, reference, one mask per method)
#    and its numbers go into cases.json.
# 2. index.html is rebuilt from viewer_template.html and cases.json.
# 3. The unpacked qc_sub-XXX.html becomes a small redirect to
#    index.html#sub-XXX&a=<method>, so links from the method pages still work.
#
# Re-running is safe: pages that are already redirects are skipped, and
# cases.json keeps what was unpacked before. When the generator on Alita writes
# new qc_sub pages, run this again.

import base64
import glob
import gzip
import json
import os
import re

import nibabel as nib
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
DATEN = os.path.join(HERE, "daten")
CASES = os.path.join(HERE, "cases.json")
os.makedirs(DATEN, exist_ok=True)

cases = json.load(open(CASES)) if os.path.exists(CASES) else {}

def volume(b64):
    return nib.Nifti1Image.from_bytes(gzip.decompress(base64.b64decode(b64)))

# --- 1. unpack ---------------------------------------------------------------
for page in sorted(glob.glob(os.path.join(HERE, "qc_sub-*.html"))):
    text = open(page, encoding="utf-8").read()
    m = re.search(r"const D=(\{.*?\});\s*\nconst msg", text, flags=re.S)
    if not m:
        continue                                            # already a redirect
    D = json.loads(m.group(1))
    sub = D["sub"]
    layers = D["ebenen"]
    ref_layer = next(e for e in layers if e["datei"] == "ref")
    methods = [e for e in layers if e["datei"] != "ref"]

    # start position: the axial slice with the most reference voxels, at their centre
    ref_img = volume(ref_layer["b64"])
    ref = np.asarray(ref_img.dataobj) > 0
    codes = nib.aff2axcodes(ref_img.affine)
    s_axis = next(i for i, c in enumerate(codes) if c in ("S", "I"))
    if ref.any():
        counts = ref.sum(axis=tuple(i for i in range(3) if i != s_axis))
        k = int(np.argmax(counts))
        idx = [slice(None)] * 3
        idx[s_axis] = k
        pts = np.argwhere(ref[tuple(idx)]).mean(0)
        vox = list(pts)
        vox.insert(s_axis, k)
    else:
        vox = list(np.array(ref.shape) / 2)
    mm0 = [round(float(x), 2) for x in (ref_img.affine @ np.array([*vox, 1.0]))[:3]]

    daten = {"flair": D["flair"], "ref": ref_layer["b64"], "masks": {e["name"]: e["b64"] for e in methods}}
    with open(os.path.join(DATEN, f"{sub}.js"), "w") as fh:
        fh.write(f'window.QC_DATEN=window.QC_DATEN||{{}};window.QC_DATEN["{sub}"]={json.dumps(daten)};')

    cases[sub] = {
        "sub": sub, "form": D.get("form", ""), "voxel": D.get("voxel", ""),
        "ref_ml": ref_layer.get("ml"), "mm0": mm0,
        "m": {e["name"]: {"dice": e["dice"], "prec": e["praez"], "sens": e["sens"], "ml": e["ml"]} for e in methods},
    }

    redirect = (f'<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><title>QC {sub}</title>'
                f'<script>const v=(location.hash.match(/v=([^&]*)/)||[])[1];'
                f'location.replace("index.html#{sub}"+(v?"&a="+v:""));</script>'
                f'<meta http-equiv="refresh" content="0; url=index.html#{sub}"></head>'
                f'<body><a href="index.html#{sub}">QC {sub}</a></body></html>')
    with open(page, "w", encoding="utf-8") as fh:
        fh.write(redirect)
    print(f"unpacked {sub}: {len(methods)} methods")

json.dump(cases, open(CASES, "w"), indent=1)

# --- 2. page -------------------------------------------------------------------
faelle = sorted(cases.values(), key=lambda c: c["sub"])
names = sorted({n for c in faelle for n in c["m"]})
methods = []
for n in names:
    ds = [c["m"][n]["dice"] for c in faelle if n in c["m"] and c["m"][n]["dice"] is not None]
    methods.append({"name": n, "dice": round(float(np.mean(ds)), 4) if ds else None, "n": len(ds)})
methods.sort(key=lambda x: -(x["dice"] if x["dice"] is not None else -1))

html = open(os.path.join(HERE, "viewer_template.html"), encoding="utf-8").read()
html = html.replace("/*FAELLE*/[]", json.dumps(faelle)).replace("/*METHODS*/[]", json.dumps(methods))
with open(os.path.join(HERE, "index.html"), "w", encoding="utf-8") as fh:
    fh.write(html)
print(f"index.html: {len(faelle)} cases, {len(methods)} methods, best mean Dice {methods[0]['name']} {methods[0]['dice']}")
