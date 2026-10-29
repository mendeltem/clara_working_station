# qc-unet-wmh: scrollable QC for the U-Net

Builds the slice viewer for this page (`quality_control.html`, `index.html`
forwards to it): two linked axial NiiVue views of the same FLAIR, reference on
the left in blue, the prediction on the right as an outline. Mouse wheel or
↑/↓ move through the slices, ←/→ through the cases, N/P jump to the next slice
with a finding; clicking a lesion shows its size and whether it was matched.
Built after the wiki rules in
[mri_qc_viewers](https://github.com/mendeltem/blackbeard_wikki/tree/main/knowledge/mri_qc_viewers).

## Files

| File | Role |
|---|---|
| `build_qc.py` | reads FLAIR, reference and prediction volumes, writes `quality_control.html`, `index.html`, `daten/<sub>.js` |
| `../qc_viewer_template.html`, `../qc_shared.py` | the viewer and the shared counting, shared with `qc-wmh-verfahren` |
| `niivue.umd.js` | NiiVue, bundled so the page works offline |

## Run on Alita

Needs `nibabel`, `numpy` and `scipy` (the `dl` environment has them). Each
pattern contains `{sub}`; the three volumes of a case must share one voxel
grid. The prediction can be a binary mask or a probability map
(`--threshold`, default 0.5).

```bash
conda activate dl
cd clara_working_station/qc-unet-wmh
python build_qc.py \
  --flair "/path/to/unet-qc/flair/{sub}.nii.gz" \
  --ref   "/path/to/unet-qc/ref/{sub}.nii.gz" \
  --pred  "/path/to/unet-qc/oof/{sub}.nii.gz" \
  --meta  /path/to/unet-qc/cases.csv
```

The three paths above are examples: point them at the folders where the
preprocessed FLAIR, the challenge masks and the out-of-fold predictions of
the run actually lie. `cases.csv` is optional, with columns `sub,site,fold`.

## Counting

Same rules as the official challenge `evaluation.py`: reference = label 1,
predictions on label 2 are deleted before counting, lesions are fully
connected (26-neighbourhood, `SetFullyConnected(True)`). Dice of the stand-in
test matched the wmhseg page to three decimals (sub-130: 0.369, sub-162:
0.246). Lesion counts differ from wmhseg, which counts with the
6-neighbourhood (sub-130: 19 here, 21 there).

## Publishing

Only for released public data (`wmh-challenge`). The page embeds 8-bit
FLAIR volumes. Run the publication check before pushing.
