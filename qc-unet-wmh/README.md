# qc-unet-wmh: scrollable QC for the U-Net

Builds the slice viewer for this page: three linked NiiVue views of the same
FLAIR (reference, U-Net, both overlaid). The mouse wheel moves through the
slices, the arrow keys through the cases. Layout after
[wmhseg QC](https://mendeltem.github.io/wmhseg/).

## Files

| File | Role |
|---|---|
| `build_qc.py` | reads FLAIR, reference and prediction volumes, writes `index.html`, `daten/<sub>.js`, `uebersicht.json` |
| `template.html` | the viewer; `build_qc.py` fills in the case table and the summary |
| `niivue.umd.js` | NiiVue, bundled so the page works offline |

## Run on Alita

Needs `nibabel`, `numpy` and `scipy` (the `dl` environment has them). Each
pattern contains `{sub}`; the three volumes of a case must share one voxel
grid. The prediction can be a binary mask or a probability map
(`--threshold`, default 0.5).

```bash
conda activate dl
cd /home/uchralt/clara_working_station/qc-unet-wmh
python build_qc.py \
  --flair "/home/uchralt/data/work/unet-qc/flair/{sub}.nii.gz" \
  --ref   "/home/uchralt/data/work/unet-qc/ref/{sub}.nii.gz" \
  --pred  "/home/uchralt/data/work/unet-qc/oof/{sub}.nii.gz" \
  --meta  /home/uchralt/data/work/unet-qc/cases.csv
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
