#!/usr/bin/env python3
"""Rendert eine QC-Seite headless in Firefox (Selenium + snap-geckodriver) und macht Screenshots.

    python seite_pruefen.py <seite.html> <ziel.png> [js-Schritte ...]

Wartet, bis #msg verschwunden ist (NiiVue fertig), fuehrt danach je Schritt ein JS-Stueck
aus und speichert <ziel>_<n>.png. Nur fuer Phantom-Seiten gedacht -- die Screenshots
zeigen die eingebetteten Bilder.
"""
import sys, time
from selenium import webdriver
from selenium.webdriver.firefox.service import Service
from selenium.webdriver.firefox.options import Options

seite, ziel, schritte = sys.argv[1], sys.argv[2], sys.argv[3:]
o = Options(); o.add_argument("--headless"); o.add_argument("--new-instance")
o.set_preference("webgl.force-enabled", True); o.set_preference("webgl.disabled", False)
d = webdriver.Firefox(service=Service("/snap/bin/firefox.geckodriver"), options=o)
d.set_window_size(1400, 1000)
d.get("file://" + seite)
for _ in range(60):
    msg = d.execute_script("const m=document.getElementById('msg'); return m?(getComputedStyle(m).display+'|'+m.textContent):'none'")
    if msg.startswith("none"): break
    time.sleep(1)
print("msg:", msg)
for e in d.get_log("browser") if hasattr(d, "get_log") else []: print("log:", e)
time.sleep(1); d.save_screenshot(ziel)
print("gespeichert", ziel)
for i, js in enumerate(schritte, 1):
    print("schritt", i, js[:80])
    try:
        r = d.execute_script(js)
        if r is not None: print("  ->", str(r)[:400])
    except Exception as ex:                       # Seite weiter pruefen, Fehler nur melden
        print("  FEHLER", str(ex).splitlines()[0][:200])
    time.sleep(1.5)
    p = ziel.replace(".png", f"_{i}.png"); d.save_screenshot(p); print("gespeichert", p)
d.quit()
