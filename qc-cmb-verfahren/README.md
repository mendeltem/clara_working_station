# qc-cmb-verfahren — QC-Viewer fuer Mikroblutungen (Code)

Erzeugt statische HTML-Seiten zur Sichtpruefung der Mikroblutungs-Erkennung
(MB-Arena): je Fall **drei gekoppelte NiiVue-Fenster** uebereinander (oder
nebeneinander) —

1. SWI + **Ground truth** (an/aus),
2. SWI + **Verfahren A** (waehlbar),
3. SWI + **Verfahren B** (waehlbar).

Fadenkreuz und Schicht sind synchron; rechts eine Sprungliste jeder
Referenz-Mikroblutung (Treffer je Verfahren ✓/✗) und der Fehlalarme —
Klick oder `n`/`p` springt in allen drei Fenstern hin und zoomt. Ohne
Sprungliste findet man 2–10 mm grosse Herde beim Blaettern nicht.

Zaehlung wie `mb_metrik.py`: Komponenten bei 26er-Nachbarschaft, Treffer =
Voxelueberlappung. TP = getroffene Referenzkomponenten, FP = Vorhersage ohne
Referenz, FN = Referenz ohne Treffer.

## Nutzung

    python erzeuge_mb.py --phantom     # synthetischer Fall, OHNE Patientendaten
    python erzeuge_mb.py --alle        # ganze Kohorte (lokal)
    python erzeuge_mb.py --faelle <id> ...
    MB_ARENA=<ordner> QC_MB_ZIEL=<ziel> python erzeuge_mb.py --alle

`seite_pruefen.py` rendert eine Seite headless (Selenium + Firefox) und macht
Screenshots — nur fuer die Phantom-Seite gedacht.

## Warum liegen hier KEINE Seiten

Die echten Seiten betten SWI-Volumen einer **nicht freigegebenen eigenen
Kohorte** ein und bleiben deshalb auf der Maschine (`anzeigen.sh`,
`http://127.0.0.1:8811`) bzw. in einem **privaten** Repo. In dieses
oeffentliche Repo gehoert nur der Generator. Zum Vorfuehren ohne Daten:
`--phantom`.

## Abhaengigkeit

`erzeuge_mb.py` nutzt die gemeinsamen Bausteine aus `erzeuge.py` (dem
WMH-QC-Generator des lokalen Systems): `FARBEN`, `LADER_EXTERN`, `NIIVUE`,
`b64`, `maske_auf_gitter`, `nach_uint8`. Fuer einen Lauf muss `erzeuge.py`
im selben Verzeichnis (oder auf dem Importpfad) liegen.
