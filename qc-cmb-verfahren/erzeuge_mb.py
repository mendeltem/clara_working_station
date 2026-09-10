#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""QC Mikroblutungen (MB-Arena): je Fall drei Fenster uebereinander.

    Zeile 1  SWI + Ground truth (an/aus)
    Zeile 2  SWI + Verfahren A (waehlbar)
    Zeile 3  SWI + Verfahren B (waehlbar)

Fadenkreuz und Schicht sind gekoppelt. Rechts die Komponentenliste: jede
Referenz-Mikroblutung mit Treffer je Verfahren, darunter die Fehlalarme der
zwei gewaehlten Verfahren -- ein Klick springt in allen drei Fenstern hin und
zoomt auf die Stelle. Mikroblutungen sind 2-10 mm gross: ohne Sprungliste
findet man sie beim Blaettern nicht.

    python erzeuge_mb.py --alle                     alle Faelle der Kohorte
    python erzeuge_mb.py --faelle <id> [<id> ...]    einzelne Faelle
    python erzeuge_mb.py --index                    nur die Uebersicht neu bauen
    python erzeuge_mb.py --phantom                  synthetischer Fall zum Pruefen (keine Patientendaten)
    QC_MB_ZIEL=<ordner>    Ziel (Standard ~/data/work/mb-arena/qc)
    MB_ARENA=<ordner>      Wurzel der MB-Arena (Standard ~/data/work/mb-arena)
    QC_MB_MOD=swi|t2star   Modalitaet (Standard swi)

Die Seiten betten Bilder der EIGENEN Kohorte ein und bleiben deshalb auf der
Maschine: ansehen mit `qc/anzeigen.sh` (http://127.0.0.1:8811) oder direkt als
Datei im Browser. Ins oeffentliche Repo gehoert nur dieses Skript;
`pruefe-veroeffentlichung` bricht bei Seiten aus nicht freigegebenen
Datensaetzen ab. Zum Vorfuehren ohne Patientendaten: --phantom.

Zaehlung wie mb_metrik.py: Komponenten bei 26er-Nachbarschaft, Treffer =
Voxelueberlappung. TP = getroffene Referenzkomponenten, FP = Vorhersage-
komponenten ohne Referenz, FN = Referenzkomponenten ohne Vorhersage.

Bausteine (uint8-Bild, Maske aufs Gitter, NiiVue-Lader) kommen aus erzeuge.py.
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import nibabel as nib
import numpy as np
from scipy import ndimage

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from erzeuge import FARBEN, LADER_EXTERN, NIIVUE, b64, maske_auf_gitter, nach_uint8  # noqa: E402

ARENA = Path(os.environ.get("MB_ARENA", os.path.expanduser("~/data/work/mb-arena")))
MOD = os.environ.get("QC_MB_MOD", "swi")
ZIEL = Path(os.environ.get("QC_MB_ZIEL", str(ARENA / "qc")))
AUSSER = {"referenz-selbst", "smoke-train"}     # kein Verfahren: Referenz als Vorhersage, Probelauf
N26 = np.ones((3, 3, 3), dtype=bool)


def verfahren_liste() -> list[dict]:
    """Alle gemessenen Verfahren, bestes (F1 der Kohorte) zuerst."""
    out = []
    for d in sorted((ARENA / "ergebnisse" / MOD).glob("*/")):
        z = d / "zusammenfassung.json"
        if d.name in AUSSER or not z.exists():
            continue
        s = json.load(open(z))
        out.append({"name": d.name, "f1": s.get("f1"), "fp_je_fall": s.get("fp_je_fall")})
    out.sort(key=lambda v: -(v["f1"] if v["f1"] is not None else -1))
    return out


def komponenten(arr: np.ndarray, aff: np.ndarray, vox_mm3: float) -> tuple[np.ndarray, list[dict]]:
    """Komponenten (26er) mit Schwerpunkt in mm (Weltkoordinaten der kanonischen Affine)."""
    lab, n = ndimage.label(arr, structure=N26)
    if n == 0:
        return lab, []
    idx = np.arange(1, n + 1)
    cms = np.array(ndimage.center_of_mass(arr, lab, idx), dtype=float).reshape(-1, 3)
    groessen = ndimage.sum(arr, lab, idx)
    mm = nib.affines.apply_affine(aff, cms)
    return lab, [{"id": int(i), "mm": [round(float(x), 1) for x in m], "mm3": round(float(g) * vox_mm3, 1)}
                 for i, m, g in zip(idx, mm, groessen)]


def treffer(lr: np.ndarray, lp: np.ndarray) -> tuple[set, set]:
    """(getroffene Referenz-IDs, getroffene Vorhersage-IDs) -- Ueberlappung wie mb_metrik.treffer."""
    beide = (lr > 0) & (lp > 0)
    if not beide.any():
        return set(), set()
    return set(np.unique(lr[beide]).tolist()), set(np.unique(lp[beide]).tolist())


def baue(sub: str, bild: Path, maske: Path, verf: list[tuple[str, Path, dict]], meta: dict) -> dict | None:
    """Daten einer Seite: SWI (uint8, kanonisch), GT und Verfahren als Masken samt Komponenten.

    verf: (Name, Vorhersagepfad, Kohortenzahlen) je Verfahren.
    """
    if not bild.exists() or not maske.exists():
        return None
    ref_img = nib.as_closest_canonical(nib.load(str(bild)))
    vox = float(np.prod(ref_img.header.get_zooms()[:3]))
    with tempfile.TemporaryDirectory(prefix="qcmb_") as tmp:
        t = Path(tmp)
        fk = t / "swi_kanonisch.nii.gz"
        nib.save(ref_img, str(fk))
        sw = nach_uint8(fk, t / "swi.nii.gz")

        rp = maske_auf_gitter(maske, ref_img, t / "gt.nii.gz")
        rarr = np.asarray(nib.load(str(rp)).dataobj) > 0
        lr, gt_komp = komponenten(rarr, ref_img.affine, vox)

        ebenen = []
        for i, (name, pfad, kohorte) in enumerate(verf):
            q = maske_auf_gitter(pfad, ref_img, t / f"v{i}.nii.gz") if pfad else None
            if not q:
                continue
            parr = np.asarray(nib.load(str(q)).dataobj) > 0
            lp, komp = komponenten(parr, ref_img.affine, vox)
            ref_hit, pred_hit = treffer(lr, lp)
            for k in komp:
                k["tp"] = k["id"] in pred_hit
            tp, fp = len(ref_hit), sum(1 for k in komp if not k["tp"])
            fn = len(gt_komp) - tp
            ebenen.append({"name": name, "datei": f"v{i}", "farbe": FARBEN[1 + i % (len(FARBEN) - 1)][0],
                           "tp": tp, "fp": fp, "fn": fn, "n_pred": len(komp),
                           "f1_kohorte": kohorte.get("f1"), "getroffen": sorted(int(r) for r in ref_hit),
                           "komp": komp, "b64": b64(q)})
        return {"sub": sub, "meta": meta,
                "form": "×".join(map(str, ref_img.shape[:3])),
                "voxel": "×".join(f"{z:g}" for z in ref_img.header.get_zooms()[:3]),
                "swi": b64(sw),
                "gt": {"farbe": FARBEN[0][0], "komp": gt_komp, "b64": b64(rp)},
                "verfahren": ebenen}


VORLAGE = r"""<!DOCTYPE html>
<html lang="de"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>QC CMB __SUB__</title><style>
 :root{--bg:#080c0f;--panel:#0f1a20;--line:#22363f;--ink:#e8f1f3;--muted:#7d99a2;
       --accent:#f0a830;--accent2:#33c6b6;--ok:#00b050;--nein:#ff6b4a}
 *{box-sizing:border-box;margin:0;padding:0}html,body{height:100%}
 body{background:var(--bg);color:var(--ink);font-family:ui-monospace,monospace;
      height:100vh;display:flex;flex-direction:column;overflow:hidden}
 header{border-bottom:1px solid var(--line);padding:7px 12px;display:flex;
        flex-wrap:wrap;align-items:center;gap:6px 14px}
 h1{font-size:14px;font-weight:800}h1 .a{color:var(--accent)}
 .lbl{font-size:9px;letter-spacing:.12em;text-transform:uppercase;color:var(--muted)}
 .grp{display:flex;align-items:center;gap:5px;flex-wrap:wrap;font-size:11px}
 button{font:inherit;font-size:11px;background:var(--panel);color:var(--ink);
        border:1px solid var(--line);border-radius:7px;padding:3px 8px;cursor:pointer}
 button:hover{border-color:var(--accent2)}
 button.on{background:rgba(240,168,48,.18);border-color:var(--accent)}
 input[type=range]{accent-color:var(--accent2);cursor:pointer;width:90px}
 .m{color:var(--muted);font-size:11px}
 main{flex:1;display:grid;grid-template-columns:1fr 330px;min-height:0}
 #wrap{position:relative;min-height:0;min-width:0}
 #msg{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
      color:var(--muted);font-size:13px;text-align:center;padding:20px;pointer-events:none;z-index:2}
 #panes{position:absolute;inset:0;display:grid;grid-template-rows:repeat(3,1fr);gap:3px;background:var(--line)}
 #panes.spalten{grid-template-rows:none;grid-template-columns:repeat(3,1fr)}
 .feld{display:flex;flex-direction:column;min-width:0;min-height:0;background:#000}
 .feld .kopf{display:flex;align-items:center;gap:8px;padding:3px 8px;background:var(--panel);
      border-bottom:1px solid var(--line);font-size:11px;flex-wrap:wrap}
 .feld select{font:inherit;font-size:11px;background:var(--bg);color:var(--ink);border:1px solid var(--line);
      border-radius:6px;padding:2px 5px;max-width:100%}
 .feld .kz{color:var(--muted);white-space:nowrap}.feld .kz b{color:var(--accent2)}
 .feld .cv{flex:1;position:relative;min-height:0}canvas{width:100%;height:100%;display:block}
 .sw{width:10px;height:10px;border-radius:3px;display:inline-block;vertical-align:-1px;margin-right:4px}
 aside{border-left:1px solid var(--line);display:flex;flex-direction:column;min-height:0;font-size:11px}
 aside .kopf{padding:6px 10px;border-bottom:1px solid var(--line);color:var(--muted);font-size:10px;line-height:1.5}
 #liste{flex:1;overflow:auto}
 .abschnitt{padding:6px 10px 3px;font-size:9.5px;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);
      border-bottom:1px solid rgba(34,54,63,.6);position:sticky;top:0;background:var(--bg)}
 .eintrag{display:grid;grid-template-columns:52px 1fr 22px 22px;gap:6px;padding:4px 10px;cursor:pointer;
      border-bottom:1px solid rgba(34,54,63,.4);align-items:center}
 .eintrag:hover{background:rgba(51,198,182,.08)}
 .eintrag.on{background:rgba(240,168,48,.16);border-left:3px solid var(--accent);padding-left:7px}
 .eintrag .nr{font-weight:700}.eintrag .mm3{color:var(--muted);font-variant-numeric:tabular-nums}
 .ok{color:var(--ok);font-weight:700;text-align:center}.nein{color:var(--nein);font-weight:700;text-align:center}
 .leer{padding:6px 10px;color:var(--muted)}
</style></head><body>
<header>
 <h1>QC CMB <span class="a">__SUB__</span></h1>
 <span class="m">__FORM__ · __VOXEL__ mm · __META__</span>
 <div class="grp"><span class="lbl">View</span>
  <button data-v="ax" class="on">Ax</button><button data-v="cor">Cor</button><button data-v="sag">Sag</button></div>
 <div class="grp"><span class="lbl">Layout</span>
  <button data-l="zeilen" class="on">3 rows</button><button data-l="spalten">3 columns</button></div>
 <div class="grp"><span class="lbl">Zoom</span>
  <button data-z="1">1×</button><button data-z="2" class="on">2×</button><button data-z="4">4×</button><button data-z="8">8×</button></div>
 <div class="grp"><span class="lbl">Opacity</span>
  <input type="range" id="op" min="0" max="100" value="75"><span id="opv" class="m">75%</span></div>
 <div class="grp"><label><input type="checkbox" id="gtin"> ground truth also in A/B</label></div>
 <div class="grp m">n / p = next / previous CMB · click = crosshair · drag right/middle = pan · wheel = slice</div>
</header>
<main>
 <div id="wrap"><div id="msg">loading NiiVue …</div><div id="panes"></div></div>
 <aside>
  <div class="kopf">Components, 26-connectivity, hit = voxel overlap.<br>
   A / B = the two chosen methods. ✓ found, ✗ missed. FP = false alarms of that method.</div>
  <div id="liste"></div>
 </aside>
</main>
<script>
const D=__DATEN__;
const msg=document.getElementById('msg');
const solid=(r,g,b)=>({R:[0,r],G:[0,g],B:[0,b],A:[0,255],I:[0,255]});
const hex=h=>[parseInt(h.slice(1,3),16),parseInt(h.slice(3,5),16),parseInt(h.slice(5,7),16)];
const S={op:.75,gt:true,gtIn:false,zoom:2,wahl:[],nvs:[],aktiv:null,mm:null};
const V=k=>D.verfahren.find(v=>v.datei===k);
async function bild(b,name){return await niivue.NVImage.loadFromBase64({base64:b,name});}
async function ebene(n,e,cm){const v=await bild(e.b64,(e.datei||'gt')+'.nii.gz');
 v.colormap=cm; v.cal_min=.5; v.cal_max=1.0; v.opacity=S.op; n.addVolume(v); return v;}
function kennz(){S.wahl.forEach((k,i)=>{const v=V(k);
 document.getElementById('kz'+i).innerHTML=`TP <b>${v.tp}</b> · FP <b>${v.fp}</b> · FN <b>${v.fn}</b>`+
  (v.f1_kohorte!==null?` <span class="m">(cohort F1 ${v.f1_kohorte.toFixed(3)})</span>`:'');});}
// Deckkraft: GT im ersten Fenster nach Schalter, in A/B nur wenn gewuenscht; Verfahren immer
function sicht(){S.nvs.forEach((n,i)=>{
 if(n.volumes[1])n.volumes[1].opacity=(i===0?S.gt:S.gtIn)?S.op:0;
 if(n.volumes[2])n.volumes[2].opacity=S.op; n.updateGLVolume();});}
// NiiVue zeichnet eine Einzelschicht in einer Kachel mit dem Seitenverhaeltnis des Volumens und
// beschneidet den Zoom dort (bei drei Zeilen bleibt ein schmaler Streifen). draw2DMain mit leerer
// Kachel dehnt das Sichtfeld auf die ganze Leinwand (gleichmaessiger Massstab); fehlt die Methode
// (andere NiiVue-Fassung vom CDN), bleibt es bei der Kachel.
function fuelle(n){if(typeof n.draw2DMain!=='function')return; const alt=Object.getPrototypeOf(n).draw2D;
 n.draw2D=function(A,g,B=NaN,Q){if(A&&A[2]===0&&A[3]===0&&!this.opts.bounds)return this.draw2DMain([0,0,0,0],g,B);
  return alt.call(this,A,g,B,Q);};}
// Sprung: Fadenkreuz in allen drei Fenstern auf mm, dann Zoom um diese Stelle.
// Bildschirmlage = zoom*mm + pan; die Bildmitte (frac .5) liegt bei zoom 1 / pan 0 in der Fenstermitte.
function zoomAuf(n,mm){const z=S.zoom, c=n.frac2mm([.5,.5,.5]);
 n.setPan2Dxyzmm([c[0]-z*mm[0],c[1]-z*mm[1],c[2]-z*mm[2],z]);}
function springe(mm,key){S.aktiv=key; S.mm=mm;
 S.nvs.forEach(n=>{n.scene.crosshairPos=n.mm2frac(mm); zoomAuf(n,mm); n.drawScene();}); liste();
 const e=document.querySelector('.eintrag.on'); if(e)e.scrollIntoView({block:'nearest'});}
function eintraege(){const A=V(S.wahl[0]),B=V(S.wahl[1]);
 const l=D.gt.komp.map(k=>({key:'gt-'+k.id,mm:k.mm}));
 [A,B].forEach(v=>v.komp.filter(k=>!k.tp).forEach(k=>l.push({key:v.datei+'-'+k.id,mm:k.mm}))); return l;}
function weiter(d){const l=eintraege(); if(!l.length)return;
 let i=l.findIndex(e=>e.key===S.aktiv); i=(i+d+l.length)%l.length; springe(l[i].mm,l[i].key);}
function liste(){const A=V(S.wahl[0]),B=V(S.wahl[1]),z=document.getElementById('liste');
 const mark=(v,id)=>v.getroffen.includes(id)?`<span class="ok">✓</span>`:`<span class="nein">✗</span>`;
 const zeile=(key,mm,a,b,c,d)=>`<div class="eintrag${S.aktiv===key?' on':''}" data-mm="${mm.join(',')}" data-key="${key}">
  <span class="nr">${a}</span><span class="mm3">${b}</span>${c}${d}</div>`;
 let h=`<div class="abschnitt"><i class="sw" style="background:${D.gt.farbe}"></i>Ground truth · ${D.gt.komp.length} CMB · <span>A</span> <span>B</span></div>`;
 h+=D.gt.komp.length?D.gt.komp.map(k=>zeile('gt-'+k.id,k.mm,'#'+k.id,k.mm3+' mm³',mark(A,k.id),mark(B,k.id))).join(''):'<div class="leer">none</div>';
 [A,B].forEach((v,i)=>{const fp=v.komp.filter(k=>!k.tp);
  h+=`<div class="abschnitt"><i class="sw" style="background:${v.farbe}"></i>${i===0?'A':'B'} ${v.name} · ${fp.length} FP</div>`;
  h+=fp.length?fp.map(k=>zeile(v.datei+'-'+k.id,k.mm,'FP '+k.id,k.mm3+' mm³','','')).join(''):'<div class="leer">none</div>';});
 z.innerHTML=h;
 z.querySelectorAll('.eintrag').forEach(e=>e.onclick=()=>springe(e.dataset.mm.split(',').map(Number),e.dataset.key));}
async function starte(){
 S.wahl=D.verfahren.slice(0,2).map(v=>v.datei); if(S.wahl.length===1)S.wahl.push(S.wahl[0]);
 if(!S.wahl.length){msg.textContent='no predictions for this case';return;}
 const box=document.getElementById('panes');
 const kopf0=`<label><input type="checkbox" id="gtan" checked> <i class="sw" style="background:${D.gt.farbe}"></i>Ground truth</label>
   <span class="kz">${D.gt.komp.length} CMB</span>`;
 const kopfV=i=>`<b>${i===0?'A':'B'}</b> <select data-i="${i}">${D.verfahren.map(v=>`<option value="${v.datei}"${v.datei===S.wahl[i]?' selected':''}>${v.name}</option>`).join('')}</select>
   <span class="kz" id="kz${i}"></span>`;
 box.innerHTML=[kopf0,kopfV(0),kopfV(1)].map((k,i)=>`<div class="feld"><div class="kopf">${k}</div><div class="cv"><canvas id="gl${i}"></canvas></div></div>`).join('');
 msg.textContent='decoding volumes …';
 const DM=(window.niivue&&niivue.DRAG_MODE)?niivue.DRAG_MODE.pan:3;
 for(let i=0;i<3;i++){
  const n=new niivue.Niivue({backColor:[0,0,0,1],crosshairColor:[.94,.66,.19,1],dragMode:DM,isSliceMM:true});
  n.attachTo('gl'+i); fuelle(n);
  n.addColormap('gt',solid(...hex(D.gt.farbe)));
  D.verfahren.forEach((v,j)=>n.addColormap('v'+j,solid(...hex(v.farbe))));
  const sw=await bild(D.swi,'swi.nii.gz'); sw.colormap='gray'; n.addVolume(sw);
  await ebene(n,D.gt,'gt');
  if(i>0){const v=V(S.wahl[i-1]); await ebene(n,v,'v'+D.verfahren.indexOf(v));}
  n.setSliceType(n.sliceTypeAxial); S.nvs.push(n);}
 S.nvs.forEach((n,i)=>n.broadcastTo(S.nvs.filter((_,j)=>j!==i),{'2d':true,'3d':true}));
 document.getElementById('gtan').onchange=e=>{S.gt=e.target.checked; sicht();};
 document.getElementById('gtin').onchange=e=>{S.gtIn=e.target.checked; sicht();};
 box.querySelectorAll('select').forEach(sel=>sel.onchange=async()=>{
  const i=+sel.dataset.i, n=S.nvs[i+1], v=V(sel.value); S.wahl[i]=sel.value;
  if(n.volumes.length>2)n.removeVolume(n.volumes[2]);        // Kanal 2 = das Verfahren (0 SWI, 1 GT)
  await ebene(n,v,'v'+D.verfahren.indexOf(v)); kennz(); liste(); sicht();});
 kennz(); liste(); sicht(); msg.style.display='none';
 const l=eintraege(); const h=(location.hash.match(/k=([^&]+)/)||[])[1];
 const e0=l.find(e=>e.key===h)||l[0]; if(e0)springe(e0.mm,e0.key);}
document.getElementById('op').oninput=e=>{S.op=e.target.value/100;
 document.getElementById('opv').textContent=e.target.value+'%'; sicht();};
const SICHT={ax:n=>n.setSliceType(n.sliceTypeAxial),cor:n=>n.setSliceType(n.sliceTypeCoronal),sag:n=>n.setSliceType(n.sliceTypeSagittal)};
document.querySelectorAll('[data-v]').forEach(b=>b.onclick=()=>{
 document.querySelectorAll('[data-v]').forEach(x=>x.classList.remove('on')); b.classList.add('on');
 S.nvs.forEach(n=>{SICHT[b.dataset.v](n); if(S.mm)zoomAuf(n,S.mm); n.drawScene();});});
document.querySelectorAll('[data-l]').forEach(b=>b.onclick=()=>{
 document.querySelectorAll('[data-l]').forEach(x=>x.classList.remove('on')); b.classList.add('on');
 document.getElementById('panes').classList.toggle('spalten',b.dataset.l==='spalten');
 setTimeout(()=>S.nvs.forEach(n=>{n.resizeListener(); if(S.mm)zoomAuf(n,S.mm); n.drawScene();}),50);});
document.querySelectorAll('[data-z]').forEach(b=>b.onclick=()=>{
 document.querySelectorAll('[data-z]').forEach(x=>x.classList.remove('on')); b.classList.add('on');
 S.zoom=+b.dataset.z; const mm=S.mm||S.nvs[0].frac2mm(S.nvs[0].scene.crosshairPos);
 S.nvs.forEach(n=>{zoomAuf(n,mm); n.drawScene();});});
document.addEventListener('keydown',e=>{if(e.target.tagName==='SELECT'||e.target.tagName==='INPUT')return;
 if(e.key==='n')weiter(1); else if(e.key==='p')weiter(-1);});
__LADER__
</script></body></html>"""


def schreibe(d: dict, eigenstaendig: bool = False) -> Path:
    lader = LADER_EXTERN
    if eigenstaendig:
        lader = ("<\\/script>\n<script>" + NIIVUE.read_text(encoding="utf-8")
                 + "<\\/script>\n<script>starte();")
    meta = " · ".join(f"{k} {v}" for k, v in d["meta"].items())
    html = (VORLAGE.replace("__SUB__", d["sub"]).replace("__FORM__", d["form"])
            .replace("__VOXEL__", d["voxel"]).replace("__META__", meta)
            .replace("__DATEN__", json.dumps(d)).replace("__LADER__", lader))
    if eigenstaendig:
        html = html.replace("<\\/script>", "</script>")
    ZIEL.mkdir(parents=True, exist_ok=True)
    p = ZIEL / f"qc_{d['sub']}.html"
    p.write_text(html, encoding="utf-8")
    # Kennzahlen je Fall fuer die Uebersicht (bleiben lokal wie die Seiten)
    (ZIEL / "zahlen").mkdir(exist_ok=True)
    (ZIEL / "zahlen" / f"{d['sub']}.json").write_text(json.dumps({
        "sub": d["sub"], "datei": p.name, "meta": d["meta"], "n_gt": len(d["gt"]["komp"]),
        "mb": round(p.stat().st_size / 1e6, 1),
        "verfahren": {v["name"]: {"tp": v["tp"], "fp": v["fp"], "fn": v["fn"]} for v in d["verfahren"]}}))
    return p


INDEX = r"""<!DOCTYPE html>
<html lang="de"><head><meta charset="UTF-8"><title>__TITEL__</title><style>
 :root{--bg:#080c0f;--panel:#0f1a20;--line:#22363f;--ink:#e8f1f3;--muted:#7d99a2;
       --accent:#f0a830;--accent2:#33c6b6;--warn:#ff6b4a}
 *{box-sizing:border-box;margin:0;padding:0}html,body{height:100%}
 body{background:var(--bg);color:var(--ink);font-family:ui-monospace,monospace;
      height:100vh;display:flex;flex-direction:column;overflow:hidden;font-size:13px}
 header{border-bottom:1px solid var(--line);padding:10px 15px}
 h1{font-size:18px;font-weight:800}h1 .a{color:var(--accent)}
 .sub{font-size:9.5px;color:var(--muted);letter-spacing:.13em;text-transform:uppercase;margin-top:4px}
 main{flex:1;display:grid;grid-template-columns:430px 1fr;min-height:0}
 aside{border-right:1px solid var(--line);display:flex;flex-direction:column;min-height:0}
 .list{flex:1;overflow:auto}
 iframe{width:100%;height:100%;border:0;display:block;background:#000}
 .row{display:grid;grid-template-columns:1fr auto;gap:2px 10px;padding:7px 12px;
      border-bottom:1px solid rgba(34,54,63,.5);cursor:pointer}
 .row:hover{background:rgba(51,198,182,.08)}
 .row.on{background:rgba(240,168,48,.14);border-left:3px solid var(--accent);padding-left:9px}
 .sid{font-weight:700}.dc{text-align:right;font-weight:700;font-variant-numeric:tabular-nums;white-space:nowrap}
 .meta{font-size:10px;color:var(--muted);grid-column:1/-1}
 .schlecht{color:var(--warn)}
 button,select{font:inherit;font-size:9.5px;background:var(--panel);color:var(--ink);
        border:1px solid var(--line);border-radius:6px;padding:3px 7px;cursor:pointer}
 button.on{background:rgba(240,168,48,.18);border-color:var(--accent)}
 input[type=search]{width:100%;font:inherit;font-size:12px;background:var(--panel);
      color:var(--ink);border:1px solid var(--line);border-radius:7px;padding:5px 8px}
 .kopf{padding:8px 12px;border-bottom:1px solid var(--line);display:flex;gap:5px;align-items:center;flex-wrap:wrap}
 .lbl{font-size:9px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted)}
</style></head><body>
<header><h1>__TITEL__</h1><div class="sub">__KOPF__</div></header>
<main>
 <aside>
  <div class="kopf"><input id="q" type="search" placeholder="filter cases …"></div>
  <div class="kopf"><span class="lbl">method</span><select id="verf">__VERF__</select></div>
  <div class="kopf"><span class="lbl">sort by</span>
   <button data-k="fn" class="on">missed (FN)</button><button data-k="fp">false alarms</button>
   <button data-k="n_gt">CMB</button><button data-k="sub">name</button></div>
  <div class="list" id="list"></div>
 </aside>
 <section><iframe id="v" title="Viewer"></iframe></section>
</main>
<script>
const DATA=__DATEN__;
let key='fn',richtung=-1,aktiv=null;
const list=document.getElementById('list'),v=document.getElementById('v'),q=document.getElementById('q'),
      verf=document.getElementById('verf');
const z=d=>d.verfahren[verf.value]||{tp:null,fp:null,fn:null};
function zeichne(){
 const t=q.value.trim().toLowerCase();
 let l=DATA.filter(d=>!t||d.sub.toLowerCase().includes(t)||JSON.stringify(d.meta).toLowerCase().includes(t));
 l.sort((a,b)=>{ if(key==='sub')return richtung*a.sub.localeCompare(b.sub);
  const x=key==='n_gt'?a.n_gt:(z(a)[key]??-1), y=key==='n_gt'?b.n_gt:(z(b)[key]??-1); return richtung*(x-y)||a.sub.localeCompare(b.sub);});
 list.innerHTML=l.map(d=>{const k=z(d);
  return `<div class="row${d.datei===aktiv?' on':''}" data-f="${d.datei}">
  <div class="sid">${d.sub}</div>
  <div class="dc">${d.n_gt} CMB · <span class="${k.fn>0?'schlecht':''}">FN ${k.fn??'—'}</span> · FP ${k.fp??'—'}</div>
  <div class="meta">${Object.entries(d.meta).map(([a,b])=>a+' '+b).join(' · ')} · ${Object.keys(d.verfahren).length} methods · ${d.mb} MB</div></div>`;}).join('');
 list.querySelectorAll('.row').forEach(r=>r.onclick=()=>{
  aktiv=r.dataset.f; v.src=aktiv; document.querySelectorAll('.row').forEach(x=>x.classList.remove('on'));
  r.classList.add('on');});}
q.oninput=zeichne; verf.onchange=zeichne;
document.querySelectorAll('[data-k]').forEach(b=>b.onclick=()=>{
 if(key===b.dataset.k)richtung*=-1; else {key=b.dataset.k;richtung=key==='sub'?1:-1;}
 document.querySelectorAll('[data-k]').forEach(x=>x.classList.remove('on'));
 b.classList.add('on'); zeichne();});
zeichne();
if(DATA.length){aktiv=DATA[0].datei;v.src=aktiv;zeichne();}
</script></body></html>"""


def baue_index(titel: str = "QC CMB — MB-Arena") -> Path:
    daten = [json.load(open(p)) for p in sorted((ZIEL / "zahlen").glob("*.json"))]
    daten = [d for d in daten if (ZIEL / d["datei"]).exists()]
    verf = verfahren_liste()
    namen = [v["name"] for v in verf] or sorted({n for d in daten for n in d["verfahren"]})
    n_gt = sum(d["n_gt"] for d in daten)
    kopf = (f"{len(daten)} cases · {n_gt} reference CMB · {MOD.upper()} · "
            + " · ".join(f"{v['name']} F1 {v['f1']:.3f}" for v in verf if v["f1"] is not None))
    html = (INDEX.replace("__TITEL__", titel).replace("__KOPF__", kopf)
            .replace("__VERF__", "".join(f'<option value="{n}">{n}</option>' for n in namen))
            .replace("__DATEN__", json.dumps(daten)))
    p = ZIEL / "index.html"
    p.write_text(html, encoding="utf-8")
    return p


def niivue_daneben() -> None:
    ZIEL.mkdir(parents=True, exist_ok=True)
    z = ZIEL / "niivue.umd.js"
    if not z.exists() and NIIVUE.exists():
        shutil.copy(NIIVUE, z)
    a = ZIEL / "anzeigen.sh"
    if not a.exists():
        a.write_text("#!/bin/bash\n# QC-Seiten nur auf Loopback ausliefern -- Patientendaten bleiben auf der Maschine.\n"
                     f"cd \"$(dirname \"$0\")\" && exec python3 -m http.server 8811 --bind 127.0.0.1\n")
        a.chmod(0o755)


def faelle_der_kohorte() -> list[dict]:
    return json.load(open(ARENA / "faelle.json"))[MOD]["faelle"]


def vorhersage(netz: str, fall: dict) -> Path | None:
    """Vorhersage des Falls in seiner Testfalte (wie mb_metrik: sonst die erste gefundene)."""
    p = ARENA / "ergebnisse" / MOD / netz / f"fold{fall['fold']}" / f"{fall['id']}_pred.nii.gz"
    if p.exists():
        return p
    g = sorted(glob.glob(str(ARENA / "ergebnisse" / MOD / netz / "fold*" / f"{fall['id']}_pred.nii.gz")))
    return Path(g[0]) if g else None


def phantom() -> Path:
    """Synthetischer Fall: Rauschbild mit drei dunklen Kugeln (GT) und zwei erfundenen Verfahren.

    A trifft zwei, uebersieht eine, ein Fehlalarm; B trifft eine, zwei Fehlalarme.
    Damit laesst sich die Seite ohne Patientendaten pruefen (Sprung, Zoom, Liste).
    """
    rng = np.random.default_rng(7)
    form = (96, 112, 40)
    aff = np.diag([0.6, 0.6, 2.0, 1.0]); aff[:3, 3] = [-28, -34, -40]
    z, y, x = np.ogrid[:form[0], :form[1], :form[2]]

    def kugel(c, r):
        return ((x - c[2]) ** 2 * 4 + (y - c[1]) ** 2 + (z - c[0]) ** 2) <= r * r  # z-Achse 2 mm grob

    kopf = ((z - 48) ** 2 / 44 ** 2 + (y - 56) ** 2 / 52 ** 2 + (x - 20) ** 2 / 18 ** 2) <= 1
    bild = (kopf * 600 + rng.normal(0, 40, form)).astype(np.float32)
    gt = np.zeros(form, np.uint8)
    for c, r in (((30, 40, 12), 3), ((60, 70, 25), 2), ((70, 30, 30), 3)):
        gt[kugel(c, r)] = 1
    bild[gt > 0] = 80
    a = np.zeros(form, np.uint8); b = np.zeros(form, np.uint8)
    a[kugel((30, 41, 12), 3)] = 1; a[kugel((60, 70, 25), 2)] = 1; a[kugel((20, 90, 8), 2)] = 1
    b[kugel((70, 30, 30), 2)] = 1; b[kugel((45, 55, 20), 2)] = 1; b[kugel((80, 80, 15), 2)] = 1
    with tempfile.TemporaryDirectory(prefix="qcmb_phantom_") as tmp:
        t = Path(tmp)
        pf = {}
        for k, arr in (("bild", bild), ("gt", gt), ("a", a), ("b", b)):
            pf[k] = t / f"{k}.nii.gz"
            nib.save(nib.Nifti1Image(arr, aff), str(pf[k]))
        d = baue("phantom", pf["bild"], pf["gt"],
                 [("phantom-a", pf["a"], {"f1": 0.667}), ("phantom-b", pf["b"], {"f1": 0.333})],
                 {"class": "synthetic"})
    niivue_daneben()
    return schreibe(d)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--faelle", nargs="*")
    ap.add_argument("--alle", action="store_true")
    ap.add_argument("--index", action="store_true")
    ap.add_argument("--phantom", action="store_true")
    ap.add_argument("--eigenstaendig", action="store_true")
    a = ap.parse_args()
    if a.phantom:
        print(phantom())
        return 0
    if a.index:
        print(baue_index())
        return 0
    verf = verfahren_liste()
    if not verf:
        print("keine gemessenen Verfahren unter", ARENA / "ergebnisse" / MOD, file=sys.stderr)
        return 2
    faelle = faelle_der_kohorte()
    if a.faelle:
        faelle = [f for f in faelle if f["id"] in set(a.faelle)]
    elif not a.alle:
        ap.error("--faelle, --alle, --index oder --phantom")
    niivue_daneben()
    n = 0
    for f in faelle:
        d = baue(f["id"], Path(f["bild"]), Path(f["maske"]),
                 [(v["name"], vorhersage(v["name"], f), v) for v in verf],
                 {"class": f.get("klasse", "?"), "fold": f.get("fold", "?")})
        if d is None:
            print(f"{f['id']}: Bild oder Maske fehlt", file=sys.stderr)
            continue
        p = schreibe(d, a.eigenstaendig)
        n += 1
        print(f"{p.name}  {p.stat().st_size / 1e6:.1f} MB  GT {len(d['gt']['komp'])}  "
              + "  ".join(f"{v['name']} {v['tp']}/{v['fp']}/{v['fn']}" for v in d["verfahren"]))
    print(baue_index())
    print(f"{n} Seiten unter {ZIEL}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
