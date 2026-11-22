"""Pagina web in un browser vero (Chromium headless), con radio simulata e dati di esempio.
Esecuzione:  pip install playwright && python -m playwright install chromium
             python tests/test_page_browser.py

Apre la pagina come ospite e come admin, su computer e telefono, passa per tutte le schede e
i livelli della mappa e fallisce se nella pagina c'è un errore JavaScript o se manca un pezzo.
Le tessere delle mappe (Internet) vengono bloccate: il test funziona anche offline.
"""
import asyncio
import math
import os
import socket
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import uvicorn  # noqa: E402

from meshwatch.auth import Auth, hash_password  # noqa: E402
from meshwatch.core import Core  # noqa: E402
from meshwatch.db import DB  # noqa: E402
from meshwatch.web import create_app  # noqa: E402
from tests.test_chat import ALICE, ChatMesh  # noqa: E402


def check(cond, msg):
    print(("OK   " if cond else "FAIL ") + msg)
    if not cond:
        sys.exit(1)


def sample_data():
    db = DB(os.path.join(tempfile.mkdtemp(), "t.db"))
    mesh = ChatMesh()
    core = Core({}, db, mesh)
    db.set_user("admin1", hash_password("segreto1"), "admin")
    tid = db.add_tracker(contact="Alice", label="Escursionista")
    now = int(time.time())
    for i in range(20):
        ts = now - (20 - i) * 1200
        db._exec("INSERT INTO positions (ts, lat, lon, alt, batt_v, tracker_id) VALUES (?,?,?,?,?,?)",
                 (ts, 45.25 + i * 0.002, 9.25, 400 + i, 4.0, tid))
        db._exec("INSERT INTO tracker_polls (ts, ok, tracker_id, hops, path, lat, lon, est) VALUES (?,?,?,?,?,?,?,?)",
                 (ts, 1 if i % 4 else 0, tid, 2, "b2,c3", 45.25 + i * 0.002, 9.25, 0))
        db.add_sensor_readings(tid, [{"channel": 2, "type": "temperature", "value": 15 + math.sin(i)},
                                     {"channel": 1, "type": "voltage", "value": 4.0}], ts)
    db.add_sensor_rule(tid, "temperature", 2, ">", 30)
    mesh.raw[ALICE].update(out_path_len=2, out_path="b2c3", out_path_hash_mode=0)
    db.set_repeater("Rip-Monte", "pw")
    for i in range(10):
        db._exec("INSERT INTO repeater_status VALUES (?,?,?,?,?,?,?,?,?,?)",
                 (now - i * 3600, "Rip-Monte", 1, 4000 - i, 9000, -110, -90, 6.5, 10, 10))
    asyncio.run(core.on_message({"kind": "dm", "peer": ALICE[:12], "peer_name": "Alice", "text": "Ciao casa",
                                 "snr": 7.5, "path_len": 2, "sender_ts": 1}))
    return core, db


def start_server(app):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning",
                                           timeout_graceful_shutdown=1))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        if server.started:
            return server, f"http://127.0.0.1:{port}"
        time.sleep(0.05)
    sys.exit("server non partito")


def launch(pw):
    try:
        return pw.chromium.launch()
    except Exception:
        # browser già installato in una versione diversa da quella del pacchetto: CHROMIUM=percorso
        exe = os.environ.get("CHROMIUM", "/opt/pw-browsers/chromium")
        if not os.path.exists(exe):
            raise
        return pw.chromium.launch(executable_path=exe)


def main():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("SKIP playwright non installato (pip install playwright && python -m playwright install chromium)")
        return
    core, db = sample_data()
    server, url = start_server(create_app(core, Auth(db), {"mode": "lan"}))
    with sync_playwright() as pw:
        browser = launch(pw)

        def page(viewport, login=False, storage=None):
            ctx = browser.new_context(viewport=viewport)
            ctx.route("**/*", lambda r: r.continue_() if r.request.url.startswith(url) and "/api/tiles/" not in r.request.url
                      else r.abort())   # anche le tessere dalla cache: il Raspberry finto non va su Internet
            p = ctx.new_page()
            p.errors = []
            p.on("pageerror", lambda e: p.errors.append(str(e)))
            p.on("console", lambda m: m.type == "error" and "Failed to load resource" not in m.text and p.errors.append(m.text))
            p.goto(url)
            if storage:
                p.evaluate("s => Object.entries(s).forEach(([k, v]) => localStorage.setItem(k, v))", storage)
            if login:
                p.evaluate("""() => fetch('/api/login', {method: 'POST', headers: {'Content-Type': 'application/json'},
                              body: JSON.stringify({username: 'admin1', password: 'segreto1'})})""")
            p.reload()
            try:
                p.wait_for_selector("#trackers .trk", state="attached", timeout=10000)
            except Exception:
                check(False, "la pagina non mostra i dispositivi" + "".join("\n     " + e for e in p.errors))
            return p

        # ospite: mappa e dispositivi, niente chat, niente percorsi
        g = page({"width": 1280, "height": 800})
        check("Escursionista" in g.inner_text("#trackers") and g.is_hidden("#tab-chat"), "ospite: mappa e dispositivi, chat nascosta")
        check("via " not in g.inner_text("#trackers") and "Temperatura" in g.inner_text("#trackers"),
              "ospite: sensori sì, percorso no")
        check(not g.errors, "ospite: nessun errore JavaScript" + "".join("\n     " + e for e in g.errors))

        # admin su computer: tutti i livelli della mappa
        a = page({"width": 1280, "height": 800}, login=True, storage={"netLayer": "1", "covLayer": "1", "routeLayer": "1"})
        a.wait_for_selector("#radio.live", timeout=5000)
        check(True, "tempo reale collegato (· in diretta)")
        txt = a.inner_text("#trackers")
        check("via Rip-Monte → Rip-Valle" in txt and "Temperatura" in txt, "scheda del dispositivo: percorso e sensori")
        a.wait_for_function("document.querySelectorAll('.leaflet-overlay-pane path').length > 3")
        check(a.locator(".cov-legend").count() == 1 and a.locator(".node-ic").count() >= 3, "livelli copertura, rete e percorsi")
        a.click("#trackers .sensors .metric-tabs button")
        a.click("#tab-chat")
        a.click(".conv")
        a.wait_for_selector(".msg.in")
        check("Ciao casa" in a.inner_text("#chat-msgs"), "chat: conversazione aperta")
        a.click("#tab-admin")
        check(a.is_visible("#adm-nav") and a.locator("#adm-nav button").count() == 5, "gestione: 5 sezioni")
        a.click("#adm-nav [data-sec=dispositivi]")
        a.wait_for_selector("#adm-trackers .item")
        check(a.is_hidden("#ctc-list") and a.is_hidden("#sys-info"), "gestione: si vede solo la sezione scelta")
        a.click("#adm-trackers button:has-text('Soglie')")
        a.wait_for_selector("#rules-list .item")
        check("Temperatura (canale 2) sopra 30.0 °C" in a.inner_text("#rules-list"), "gestione: soglie dei sensori")
        a.click("#adm-nav [data-sec=rete]")
        a.wait_for_selector("#ctc-list .item")
        check("via Rip-Monte → Rip-Valle" in a.inner_text("#ctc-list"), "gestione: rubrica con percorso")
        a.click("#adm-nav [data-sec=sistema]")
        a.wait_for_selector("#tc-info :text('Tessere salvate')")
        check(a.is_checked("#form-tc [name=enabled]"), "gestione: cache delle mappe attiva")
        a.click("#adm-nav [data-sec=telegram]")
        a.wait_for_selector("#rep-off", state="visible")
        check(a.is_disabled("#form-report [name=enabled]") and a.is_disabled("#rep-send"),
              "gestione: senza Telegram riepilogo e backup non si attivano")
        a.reload()
        a.wait_for_selector("#trackers .trk", state="attached")
        a.click("#tab-admin")
        check(a.is_visible("#form-report"), "gestione: la sezione scelta resta dopo il ricaricamento")
        a.click("#adm-nav [data-sec=dispositivi]")
        check(a.evaluate("[...document.querySelectorAll('.leaflet-tile')].some(t => t.src.includes('/api/tiles/osm/'))"),
              "mappa: le tessere passano dalla cache del Raspberry")
        a.click("#tab-map")
        # una modifica da un'altra pagina arriva da sola
        other = page({"width": 800, "height": 600}, login=True)
        other.evaluate("() => fetch('/api/trackers/1', {method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({label: 'Rinominato'})})")
        a.wait_for_selector("#trackers :text('Rinominato')", timeout=5000)
        check(True, "aggiornamento in tempo reale da un'altra pagina")
        check(not a.errors and not other.errors, "admin: nessun errore JavaScript" + "".join("\n     " + e for e in a.errors + other.errors))

        # telefono: barra in basso e schede
        ph = page({"width": 390, "height": 844}, login=True)
        for tab in ("list", "chat", "admin", "map"):
            ph.click(f"#mnav [data-t={tab}]")
            ph.wait_for_timeout(300)
            if tab == "admin":
                for sec in ("dispositivi", "rete", "telegram", "sistema", "accessi"):
                    ph.click(f"#adm-nav [data-sec={sec}]")
                    ph.wait_for_timeout(150)
                over = ph.evaluate("document.getElementById('admin-scroll').scrollWidth > window.innerWidth + 1")
                check(not over, "telefono: la gestione non scorre di lato")
        check(ph.is_visible("#map-summary") and not ph.errors, "telefono: schede senza errori" + "".join("\n     " + e for e in ph.errors))
        browser.close()
    core.events.close()
    server.should_exit = True
    print("Tutti i test superati.")


if __name__ == "__main__":
    main()
