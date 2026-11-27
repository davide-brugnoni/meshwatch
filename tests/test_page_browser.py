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
    for i in range(30):   # storico dei nodi per la scheda Statistiche
        core.netstats.record_advert("e%d" % (i % 3) * 32, name=f"Nodo-{i % 3}", type="companion", adv_ts=i + 1,
                                    hops=i % 3, snr=4.0 + i % 3, now=now - i * 7200)
    core.netstats.record_rx({"payload_type": 5, "path_len": 1})
    for i in range(10):
        k = 10 - i   # contatori che crescono: i grafici mostrano pacchetti all'ora e airtime
        db._exec("INSERT INTO repeater_status (ts, name, ok, bat_mv, uptime, noise, rssi, snr, nb_recv, nb_sent, "
                 "sent_flood, sent_direct, recv_flood, recv_direct, flood_dups, direct_dups, airtime, rx_airtime, "
                 "tx_queue, full_evts, recv_errors) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (now - i * 3600, "Rip-Monte", 1, 4000 - i, 9000 + k * 3600, -110, -90, 6.5, 300 * k + i * i, 120 * k,
                  100 * k, 20 * k, 260 * k, 40 * k, 80 * k + i, 2 * k, 36 * k, 300 * k, 0, 0, k))
    async def gw_info():
        return {"name": "Casa", "model": "Heltec V3", "battery_mv": 4200, "uptime": 50000, "noise_floor": -115,
                "recv": 5000, "sent": 900, "flood_tx": 800, "direct_tx": 100, "flood_rx": 4500, "direct_rx": 500,
                "tx_air_secs": 400, "rx_air_secs": 3000, "contacts": 3}
    mesh.gateway_info = gw_info
    for i in range(6):
        k = 6 - i
        db._exec("INSERT INTO gateway_stats (ts, bat_mv, uptime, noise, nb_recv, nb_sent, sent_flood, tx_air, rx_air) "
                 "VALUES (?,?,?,?,?,?,?,?,?)", (now - i * 3600, 4200, 50000 + k * 3600, -115, 400 * k, 90 * k, 80 * k, 30 * k, 250 * k))
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
        check("Escursionista" in g.inner_text("#trackers") and g.is_hidden("#tab-chat") and g.is_hidden("#tab-stats"),
              "ospite: mappa e dispositivi, chat e statistiche nascoste")
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
        reps = a.inner_text("#reps").lower()   # le etichette sono in maiuscolo da CSS
        check("tx flood/dir." in reps and "duplicati fl./dir." in reps and "errori rx" in reps,
              "ripetitore: traffico flood/diretto, duplicati ed errori")
        a.click("#reps .metric-tabs button:has-text('Ricevuti')")
        check("media 29" in a.inner_text("#reps .spark-range"), "ripetitore: grafico dei pacchetti ricevuti all'ora")
        a.click("#reps .metric-tabs button:has-text('Airtime TX')")
        check("media 1.00%" in a.inner_text("#reps .spark-range"), "ripetitore: grafico dell'airtime in %")
        a.click("#tab-chat")
        a.click(".conv")
        a.wait_for_selector(".msg.in")
        check("Ciao casa" in a.inner_text("#chat-msgs"), "chat: conversazione aperta")
        a.click("#tab-stats")
        a.wait_for_selector("#st-kpi b")
        a.wait_for_selector("#st-trend svg.st-svg")
        check("Nodo-1" in a.inner_text("#st-nodes") and "Rip-Monte" in a.inner_text("#st-avail")
              and a.locator("#st-compare tbody tr").count() >= 2, "statistiche: nodi, disponibilità e confronto (ripetitore e gateway)")
        a.hover("#st-trend svg.st-svg .hit >> nth=-1")
        check(a.is_visible("#st-tip"), "statistiche: tooltip sui grafici")
        a.click("#st-nfilter [data-f=new]")
        a.click("#st-compare th:has-text('Rumore')")
        check("rumore" in a.locator("#st-compare th.sorted").inner_text().lower() and a.locator("#st-nfilter .on").get_attribute("data-f") == "new",
              "statistiche: filtri e ordinamento")
        a.click("#tab-admin")
        check(a.is_visible("#adm-nav") and a.locator("#adm-nav button").count() == 6, "gestione: 6 sezioni")
        a.click("#adm-nav [data-sec=dispositivi]")
        a.wait_for_selector("#adm-trackers .item")
        check(a.is_hidden("#ctc-list") and a.is_hidden("#sys-info"), "gestione: si vede solo la sezione scelta")
        a.click("#adm-trackers button:has-text('Soglie')")
        a.wait_for_selector("#rules-list .item")
        check("Temperatura (canale 2) sopra 30.0 °C" in a.inner_text("#rules-list"), "gestione: soglie dei sensori")
        a.click("#adm-nav [data-sec=rete]")
        a.wait_for_selector("#ctc-list .item")
        check("via Rip-Monte → Rip-Valle" in a.inner_text("#ctc-list"), "gestione: rubrica con percorso")
        a.wait_for_selector("#gw-chart .metric-tabs")
        check("tx flood/dir." in a.inner_text("#gw-info").lower() and "Duplicati" not in a.inner_text("#gw-chart"),
              "gestione: statistiche complete del gateway")
        a.click("#gw-chart .metric-tabs button:has-text('Inviati flood')")
        check("media 80/h" in a.inner_text("#gw-chart .spark-range"), "gestione: grafico del gateway")
        a.click("#adm-nav [data-sec=sistema]")
        a.wait_for_selector("#tc-info :text('Tessere salvate')")
        check(a.is_checked("#form-tc [name=enabled]"), "gestione: cache delle mappe attiva")
        a.click("#adm-nav [data-sec=dashboard]")
        a.wait_for_selector("#pub-sections input")
        check(a.evaluate("document.querySelectorAll('#pub-sections input:checked').length") == 8, "dashboard: tutte le sezioni proposte")
        a.fill("#form-pub [name=url]", "https://esempio.test/api/meshwatch")
        a.click("#pub-gen")
        check(len(a.input_value("#form-pub [name=token]")) == 48, "dashboard: token generato nella pagina")
        a.check("#form-pub [name=enabled]")
        a.uncheck("#pub-sections input[value=nodes]")
        a.click("#form-pub .primary")
        a.wait_for_function("document.getElementById('pub-state').textContent.startsWith('Attiva')")
        st = core.publisher.settings()
        check(st["enabled"] and len(st["token"]) == 48 and "nodes" not in st["sections"], "dashboard: impostazioni salvate dalla pagina")
        check("impostato" in a.get_attribute("#form-pub [name=token]", "placeholder") and not a.input_value("#form-pub [name=token]"),
              "dashboard: il token salvato non si rivede")
        if os.environ.get("SHOT"):
            a.screenshot(path=os.environ["SHOT"], full_page=False)
        a.click("#adm-nav [data-sec=telegram]")
        a.wait_for_selector("#rep-off", state="visible")
        a.wait_for_function("document.querySelector('#form-nodes-alert [name=gone]').value === 'off'")
        check(a.is_visible("#form-nodes-alert"), "gestione: avvisi nodi nuovi e spariti")
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
        for tab in ("list", "chat", "stats", "admin", "map"):
            ph.click(f"#mnav [data-t={tab}]")
            ph.wait_for_timeout(300)
            if tab == "admin":
                for sec in ("dispositivi", "rete", "telegram", "dashboard", "sistema", "accessi"):
                    ph.click(f"#adm-nav [data-sec={sec}]")
                    ph.wait_for_timeout(150)
                over = ph.evaluate("document.getElementById('admin-scroll').scrollWidth > window.innerWidth + 1")
                check(not over, "telefono: la gestione non scorre di lato")
            if tab == "stats":
                ph.wait_for_selector("#st-kpi b")
                over = ph.evaluate("document.getElementById('stats-scroll').scrollWidth > window.innerWidth + 1")
                check(not over, "telefono: le statistiche non scorrono di lato")
        check(ph.is_visible("#map-summary") and not ph.errors, "telefono: schede senza errori" + "".join("\n     " + e for e in ph.errors))
        browser.close()
    core.events.close()
    server.should_exit = True
    print("Tutti i test superati.")


if __name__ == "__main__":
    main()
