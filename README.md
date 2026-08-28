# Clara Working Station

Was auf **Alita** gerechnet wird, und was daraus veröffentlicht ist.

**<https://mendeltem.github.io/clara_working_station/>**

*Work computed on Alita by the local agent. This is a workbench, not a
portfolio — only what was produced here is listed.*

---

## Ergebnisse / Results

| | |
|---|---|
| **[WMH — elf Verfahren im Vergleich](https://mendeltem.github.io/clara_working_station/qc-wmh-verfahren/)** | Je Patient eine Seite mit FLAIR, Referenzmaske und bis zu elf Segmentierungen übereinander, multiplanar und als 3D-Rendering. Dazu eine Seite je Verfahren und ein gestaffelter Vergleich mit gepaarten Tests. |
| **[WMH — U-Net, erste Fassung](https://mendeltem.github.io/clara_working_station/qc-unet-wmh/)** | Der erste QC: drei Schnitte je Fall, 170 Fälle in einer Datei. Bleibt als Vergleichspunkt stehen. |

---

## Das System dahinter

Clara ist ein lokales Modell auf einer einzelnen Workstation, ohne
Netzzugang. Werkzeuge, Messungen und Betriebsregeln stehen getrennt:

**[local_models](https://github.com/mendeltem/local_models)** — [Seite](https://mendeltem.github.io/local_models/) · [das modellunabhängige Gerüst](https://github.com/mendeltem/local_models/blob/main/docs/05-scaffolding.md)

---

## Was hier nicht liegt

Patientendaten und Ergebnisse auf eigenen Kohorten bleiben auf der
Maschine. Veröffentlicht wird nur, was auf öffentlichen Datensätzen
gerechnet wurde — der QC oben läuft auf der *WMH Segmentation
Challenge* (Utrecht, Singapore, Amsterdam).

*Patient data and results on in-house cohorts stay on the machine.*

Geprüft vor jedem Push mit `system/werkzeuge/pruefe-veroeffentlichung`:
jeder Datensatz braucht eine ausdrückliche Freigabe, und ein Pfad auf
nicht freigegebene Daten bricht ab.
