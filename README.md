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

## Was die WMH-Linie gelernt hat / Lessons from the WMH line

Gemessen auf den 110 öffentlichen Testfällen der WMH Segmentation Challenge
2017 (Kuijf et al. 2019, [doi:10.1109/TMI.2019.2905770](https://doi.org/10.1109/TMI.2019.2905770)),
offizielle Auswertung, gepaart über Fälle mit Bootstrap-Intervall. Das sind
Eigenmessungen, keine Platzierungen. Jede Zeile verweist auf ihren Eintrag im
Agenten-Wiki [blackbeard_wikki](https://github.com/mendeltem/blackbeard_wikki).

*Measured on the 110 public test cases, official evaluation, paired with bootstrap intervals.*

| Befund | Zahl |
|---|---|
| [Die Hirnmaske ist Teil des Modells.](https://github.com/mendeltem/blackbeard_wikki/blob/main/knowledge/wmh_segmentation/findings/brain-mask-is-part-of-the-model.md) Dasselbe Modell mit zwei HD-BET-Läufen: die z-Normierung innerhalb der Maske verschiebt das ganze Eingangsbild, nicht nur den Rand. | Dice 0,688 gegen 0,719 |
| [Eine zweite Bias-Korrektur hilft](https://github.com/mendeltem/blackbeard_wikki/blob/main/knowledge/wmh_segmentation/findings/second-bias-correction-helps-on-mixed-scanners.md), obwohl die Daten schon korrigiert sind. `fast -B` wirkt hier als Angleichung der Intensität über Scanner und senkt die Streuung über Startwerte auf rund ein Drittel. | ohne sie Dice −0,011 [−0,016; −0,006], Läsions-Recall −0,037 |
| [Die offizielle Auswertung löscht Vorhersagen auf Label 2](https://github.com/mendeltem/blackbeard_wikki/blob/main/knowledge/wmh_segmentation/findings/official-label-2-rule-deletes-the-prediction.md), statt sie als Fehlalarm zu zählen (`evaluation.py`, Zeilen 47–50). | strengere Zählung liegt 0,005–0,007 Dice zu tief |
| [Ungesehene Scanner sind nicht die Schwachstelle.](https://github.com/mendeltem/blackbeard_wikki/blob/main/knowledge/wmh_segmentation/findings/unseen-scanners-are-not-the-weak-spot.md) Der Testsatz hat fünf Geräte, nicht drei Standorte; alle Fälle unter Dice 0,5 liegen auf bekannten Geräten. | 20 Fälle auf ungesehenen Geräten, 0 Ausfälle |
| [Ein als Paket geändertes Netz brachte nichts.](https://github.com/mendeltem/blackbeard_wikki/blob/main/knowledge/wmh_segmentation/findings/architecture-package-brought-no-gain.md) Der scheinbare Gewinn von +0,032 Dice war das Maskenartefakt aus der ersten Zeile. | Dice −0,001, Läsions-Recall −0,038 |

Daraus folgt für jeden Modellvergleich: Maskenherkunft und Auswertungsfunktion
mitprotokollieren, Bausteine einzeln ändern.

---

## Das System dahinter

Clara ist ein lokales Modell auf einer einzelnen Workstation, ohne
Netzzugang. Werkzeuge, Messungen und Betriebsregeln stehen getrennt:

**[local_models](https://github.com/mendeltem/local_models)** — [Seite](https://mendeltem.github.io/local_models/) · [das modellunabhängige Gerüst](https://github.com/mendeltem/local_models/blob/main/docs/05-scaffolding.md)

Was die Agenten unterwegs gelernt haben, sammelt das Wiki
**[blackbeard_wikki](https://github.com/mendeltem/blackbeard_wikki)**, Bereich
[local_llm_operation](https://github.com/mendeltem/blackbeard_wikki/blob/main/knowledge/local_llm_operation/SUMMARY.md).

### Die Maschine, Stand Oktober 2026 / The machine, October 2026

- RTX A5000 mit 24 GB (Ampere, sm_86), i9-12900K, 62 GiB RAM, Ubuntu 24.04,
  NVIDIA-Treiber 550 (CUDA 12.4).
- Claras Modell ist Qwen3.8-27B in Q4 über llama.cpp. Am 23.10. auf derselben
  Karte gegen Nemotron 3.5 Bolt 74B-A9B gemessen: auf einem 13-Aufgaben-Test
  100 % gegen 88,5 % (bei 4096 Token 97,4 % gegen 96,2 %). Bolt erzeugt
  66–69 tok/s gegen rund 30, denkt aber länger; der Zeitgewinn schrumpft auf
  etwa 20 %. [Eintrag](https://github.com/mendeltem/blackbeard_wikki/blob/main/knowledge/local_llm_operation/findings/nemotron-bolt-74b-on-ampere-24gb.md)
- Der Modellserver entlädt sich im Leerlauf: 21 770 MiB VRAM im Betrieb,
  1 618 MiB danach, Aufwachen in 11,4 s. Die Karte bleibt so für Training frei.
- Bei Secure Boot kann ein Kernel-Update den NVIDIA-Treiber stilllegen, ohne dass
  ein Log ihn nennt. Vor dem Neustart `dkms status` und `mokutil --list-enrolled`.
  [Eintrag](https://github.com/mendeltem/blackbeard_wikki/blob/main/knowledge/local_llm_operation/risks/secure-boot-kernel-update-unloads-nvidia-dkms.md)

---

## Was hier nicht liegt

Patientendaten und Ergebnisse auf eigenen Kohorten bleiben auf der
Maschine. Veröffentlicht wird nur, was auf öffentlichen Datensätzen
gerechnet wurde — der QC oben läuft auf der *WMH Segmentation
Challenge* (Utrecht, Singapore, Amsterdam).

*Patient data and results on in-house cohorts stay on the machine.*

Das gilt auch für alles, was daraus abgeleitet ist: eine Maske, ein Embedding,
eine Logzeile, eine QC-Seite oder ein darauf trainiertes Modell trägt die Klasse
seiner Quelle ([Regel](https://github.com/mendeltem/blackbeard_wikki/blob/main/knowledge/agent_coordination/rules/three-data-classes-decide-what-may-leave-the-institution.md)).
Deshalb fehlen hier die Vergleiche auf dem gemischten Pool aus Challenge und
eigenen Kohorten, obwohl sie im Wiki stehen.

*Anything derived from in-house data inherits its class, so results on the mixed pool stay off this page.*

Geprüft vor jedem Push mit `system/werkzeuge/pruefe-veroeffentlichung`:
jeder Datensatz braucht eine ausdrückliche Freigabe, und ein Pfad auf
nicht freigegebene Daten bricht ab.
