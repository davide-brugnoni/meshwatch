"""Statistiche della rete: storico dei nodi, pacchetti all'ora, avvisi nodi nuovi e spariti,
disponibilità, batteria, confronto e collegamenti dei ripetitori, API della scheda Statistiche.
Esecuzione:  python tests/test_netstats.py
"""
import asyncio
import os
import sys
import tempfile
import time
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient  # noqa: E402

from meshwatch import maintenance  # noqa: E402
from meshwatch.auth import Auth  # noqa: E402
from meshwatch.core import Core  # noqa: E402
from meshwatch.db import DB  # noqa: E402
from meshwatch.mesh import Mesh  # noqa: E402
from meshwatch.netstats import counter_rates, linear_slope  # noqa: E402
from meshwatch.web import create_app  # noqa: E402
from tests.test_chat import ALICE, GW, REP, REP2, ChatMesh  # noqa: E402

NEW = "e7" * 32


def check(cond, msg):
    print(("OK   " if cond else "FAIL ") + msg)
    if not cond:
        sys.exit(1)


def setup(cfg=None):
    db = DB(os.path.join(tempfile.mkdtemp(), "t.db"))
    mesh = ChatMesh()
    core = Core(cfg or {}, db, mesh)
    return db, mesh, core, core.netstats


def advert(key, name, ts, adv_ts, hops=1, snr=5.0, typ="companion"):
    return {"payload_type": 4, "path_len": hops, "snr": snr, "rssi": -100, "adv_key": key, "adv_name": name,
            "adv_type": typ, "adv_lat": 0.0, "adv_lon": 0.0, "adv_timestamp": adv_ts}


def test_collect():
    db, mesh, core, ns = setup()
    check(mesh.on_rx == ns.record_rx, "la radio passa i pacchetti ricevuti alle statistiche")
    now = int(time.time())
    # primo avvio: la rubrica entra nello storico senza contare come "nuovo"
    ns.sync_contacts(mesh.contacts(), now=now)
    rows = db._all("SELECT * FROM nodes ORDER BY name")
    check(len(rows) == 3 and all(r["seeded"] for r in rows), "contatti già in rubrica: nello storico, non nuovi")
    check(db.get("nodes_since") is not None, "data di inizio dello storico salvata")
    ns.set_alert_settings(new=True)
    ns.record_rx(advert(NEW, "Nuovo-Nodo", now, 1000, hops=3, snr=2.5), now=now)
    ns.record_rx(advert(NEW, "Nuovo-Nodo", now + 5, 1000, hops=1, snr=7.0), now=now + 5)   # stesso advert, altra strada
    n = db._one("SELECT * FROM nodes WHERE key=?", (NEW,))
    check(n and n["adverts"] == 1 and n["hops"] == 1 and not n["seeded"], "advert doppio contato una volta, percorso più corto")
    check(n["lat"] is None, "posizione 0,0 ignorata")
    check(db._one("SELECT COUNT(*) AS n FROM node_adverts WHERE key=?", (NEW,))["n"] == 1, "una sola riga nello storico degli advert")
    check(any("Nuovo nodo sentito: Nuovo-Nodo" in t and "3 salti" in t for t in ns.outbox), "avviso del nodo nuovo")
    ns.record_rx(advert(NEW, "Nuovo-Nodo", now + 3600, 1001, hops=2), now=now + 3600)
    check(db._one("SELECT adverts FROM nodes WHERE key=?", (NEW,))["adverts"] == 2, "advert successivo contato")
    # il nostro advert ripetuto dai ripetitori non è un nodo
    ns.record_rx(advert(GW, "Casa", now, 5), now=now)
    check(not db._one("SELECT 1 AS x FROM nodes WHERE key=?", (GW,)), "advert del gateway ignorato")
    # pacchetti contati per ora e tipo, scritti tutti insieme
    for t in (5, 5, 2):
        ns.record_rx({"payload_type": t, "path_len": 1, "snr": 3, "rssi": -90}, now=now)
    check(ns.summary(now + 10)["packets_24h"] == 7, "pacchetti contati anche prima della scrittura (4 advert + 3)")
    check(ns.flush() == 7 and ns.flush() == 0, "contatori scritti nel database e azzerati")
    h = now - now % 3600
    check(db._one("SELECT n FROM rx_hourly WHERE hour=? AND ptype=5", (h,))["n"] == 2, "pacchetti per ora e tipo")
    # advert visto solo dalla rubrica (firmware senza pacchetti grezzi)
    mesh.raw[ALICE]["last_advert"] = now + 100
    ns.sync_contacts(mesh.contacts(), now=now + 200)
    check(db._one("SELECT adverts FROM nodes WHERE key=?", (ALICE,))["adverts"] == 1, "advert ricavato dalla rubrica")
    ns.sync_contacts(mesh.contacts(), now=now + 300)
    check(db._one("SELECT adverts FROM nodes WHERE key=?", (ALICE,))["adverts"] == 1, "rubrica invariata: niente doppioni")


def test_rx_event():
    seen = []
    m = Mesh()
    m.on_rx = seen.append
    ev = SimpleNamespace(payload={"payload_type": 4, "path_len": 2, "snr": 4.25, "rssi": -97, "adv_key": NEW,
                                  "adv_name": "X", "adv_type": 2, "adv_timestamp": 7, "raw_hex": "00"})
    asyncio.run(m._on_rx_log(ev))
    check(seen and seen[0]["adv_type"] == "ripetitore" and seen[0]["path_len"] == 2 and "raw_hex" not in seen[0],
          "pacchetto della radio convertito per le statistiche")


def test_gone():
    db, mesh, core, ns = setup()
    now = int(time.time())
    db.set("nodes_since", now - 30 * 86400)
    ns.record_advert(REP, name="Rip-Monte", type="ripetitore", adv_ts=1, now=now - 10 * 3600)
    ns.record_advert(ALICE, name="Alice", type="companion", adv_ts=1, now=now - 10 * 3600)
    ns.record_advert(REP2, name="Rip-Valle", type="ripetitore", adv_ts=1, now=now - 10 * 86400)   # già sparito
    ns.set_alert_settings(gone="repeaters", gone_h=72)
    check(db._one("SELECT gone_notified FROM nodes WHERE key=?", (REP2,))["gone_notified"] == 1,
          "accendendo gli avvisi i nodi già spariti non mandano raffiche")
    check(not ns.check_gone(now), "nessun ripetitore zitto da più di 72 ore")
    ns.set_alert_settings(gone_h=6)
    db._exec("UPDATE nodes SET gone_notified=0")
    db._exec("UPDATE nodes SET gone_notified=1 WHERE key=?", (REP2,))
    gone = ns.check_gone(now)
    check([r["name"] for r in gone] == ["Rip-Monte"], "solo i ripetitori: Alice non conta")
    check(any("Rip-Monte" in t and "non si sente" in t for t in ns.outbox), "avviso del ripetitore sparito")
    check(not ns.check_gone(now), "un avviso solo")
    ns.record_advert(REP, name="Rip-Monte", type="ripetitore", adv_ts=2, now=now)
    check(any("Rip-Monte di nuovo sentito" in t for t in ns.outbox), "avviso quando torna")
    check(ns.alert_settings() == {"new": False, "gone": "repeaters", "gone_h": 6}, "impostazioni salvate")


def test_calcs():
    check(round(linear_slope([(0, 4000), (86400, 3990), (2 * 86400, 3980)]), 3) == -10, "pendenza al giorno")
    h = [{"ts": 0, "c": 100, "uptime": 10}, {"ts": 3600, "c": 160, "uptime": 3610},
         {"ts": 7200, "c": 5, "uptime": 20}, {"ts": 10800, "c": 65, "uptime": 3620}]
    check(counter_rates(h, lambda x: x["c"]) == (120, 7200), "contatori: riavvio saltato")

    db, mesh, core, ns = setup({"repeater_low_battery_v": 3.5})
    now = int(time.time())
    db.set_repeater("Rip-Monte", "pw")
    for i in range(48):   # due giorni, una lettura all'ora: scende di 20 mV al giorno, una risposta persa su 4
        ts = now - (47 - i) * 3600
        ok = i % 4 != 0
        db._exec("INSERT INTO repeater_status (ts, name, ok, bat_mv, uptime, noise, snr, rssi, nb_recv, nb_sent, flood_dups, direct_dups, "
                 "airtime, rx_airtime, tx_queue, recv_errors) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (ts, "Rip-Monte", 1 if ok else 0, int(3700 - 20 * (i / 24)) if ok else None, i * 3600, -110, 5.0, -95,
                  i * 100, i * 40, i * 10, 0, i * 36, i * 72, 1, i))
    av = ns.availability(now)[0]
    check(av["pct_7d"] == 75.0 and av["pct_30d"] == 75.0 and 70 < av["pct_24h"] < 80, "disponibilità in percentuale")
    check(len(av["days"]) == 30 and len(av["hours"]) == 48 and sum(d["total"] for d in av["days"]) == 48, "quadretti dei giorni e delle ore")
    b = ns.battery(now)[0]
    check(b["trend"] == "giù" and -22 < b["slope_mv_day"] < -18, f"batteria in calo di ~20 mV al giorno ({b['slope_mv_day']})")
    check(b["days_left"] and 7 < b["days_left"] < 9, f"previsione: da 3,66 V alla soglia di 3,5 V in ~8 giorni ({b['days_left']})")
    c = ns.compare(now)[0]
    check(c["rx_h"] == 100 and c["tx_h"] == 40 and c["dups_pct"] == 10, "confronto: pacchetti all'ora e duplicati in %")
    check(c["air_tx_pct"] == 1.0 and 70 < c["online_pct"] < 80 and c["errors_h"] == 1, "confronto: airtime e disponibilità")
    # collegamenti: settimana scorsa SNR 8, questa 5
    for d in range(14):
        ts = now - d * 86400 - 60
        db._exec("INSERT INTO link_history VALUES (?,?,?,?)", (ts, "Rip-Monte", REP2[:8], 5.0 if d < 7 else 8.0))
    db.set_neighbours("Rip-Monte", [{"prefix": REP2[:8], "snr": 5.0, "secs_ago": 10}])
    links = ns.links(now, mesh.contacts())
    check(links and links[0]["to"] == "Rip-Valle" and links[0]["delta"] == -3.0, "collegamento peggiorato di 3 dB, nome risolto")
    check(db._one("SELECT COUNT(*) AS n FROM link_history")["n"] == 15, "i vicini letti finiscono anche nello storico")
    ns.record_advert(ALICE, name="Alice", adv_ts=9, hops=0, snr=6.5, now=now)
    check(any(link["kind"] == "gateway" and link["to"] == "Alice" for link in ns.links(now)), "nodo sentito direttamente dal gateway")
    t = ns.trend(now, 30)
    check(len(t["days"]) == 30 and len(t["hours"]) == 48 and len(t["heat"]) == 7, "andamento: giorni, ore, mappa di calore")
    # pulizia dei dati vecchi
    db._exec("INSERT INTO node_adverts VALUES (?,?,?,?,?)", (now - 400 * 86400, ALICE, 1, 1, 1))
    db._exec("INSERT INTO rx_hourly VALUES (?,?,?)", (now - 400 * 86400, 4, 3))
    db._exec("INSERT INTO link_history VALUES (?,?,?,?)", (now - 400 * 86400, "x", "y", 1))
    maintenance.purge(db, 180)
    check(not db._one("SELECT 1 AS x FROM node_adverts WHERE ts < ?", (now - 300 * 86400,))
          and not db._one("SELECT 1 AS x FROM rx_hourly WHERE hour < ?", (now - 300 * 86400,))
          and not db._one("SELECT 1 AS x FROM link_history WHERE ts < ?", (now - 300 * 86400,)), "pulizia dei dati vecchi")


def test_api():
    db, mesh, core, ns = setup()
    c = TestClient(create_app(core, Auth(db), {"mode": "lan"}))
    c.post("/api/setup", json={"username": "admin1", "password": "segreto1"})
    c.post("/api/users", json={"username": "fam", "password": "123456", "role": "viewer"})
    v = TestClient(create_app(core, Auth(db), {"mode": "lan"}))
    v.post("/api/login", json={"username": "fam", "password": "123456"})
    anon = TestClient(create_app(core, Auth(db), {"mode": "lan"}))
    ns.record_advert(NEW, name="Nodo", type="companion", adv_ts=1, hops=2, snr=4)
    r = v.get("/api/stats?days=7")
    check(r.status_code == 200, "statistiche visibili al viewer")
    j = r.json()
    check({"summary", "nodes", "trend", "availability", "battery", "compare", "links", "alerts"} <= set(j), "tutte le sezioni")
    check(j["summary"]["nodes"] == 1 and j["nodes"][0]["name"] == "Nodo", "nodi nella risposta")
    check(anon.get("/api/stats").status_code == 401, "l'ospite non vede le statistiche")
    check(v.put("/api/alerts/nodes", json={"new": True}).status_code == 403, "solo l'admin cambia gli avvisi")
    r = c.put("/api/alerts/nodes", json={"new": True, "gone": "all", "gone_h": 48})
    check(r.status_code == 200 and r.json() == {"new": True, "gone": "all", "gone_h": 48}, "avvisi nodi salvati")
    check(c.put("/api/alerts/nodes", json={"gone": "boh"}).status_code == 422, "valore non valido respinto")
    check(any(a["action"] == "avvisi nodi" for a in db.audit()), "modifica nel registro attività")


def test_telegram():
    from meshwatch.bot import BTN_NET, COMMANDS, KEYBOARD, Bot
    from tests.test_features import Ctx, FakeChat, FakeLiveBot, FakeUpdate
    db, mesh, core, ns = setup({"repeater_low_battery_v": 3.5})
    check("Ancora nessun nodo" in ns.telegram_text(), "/rete senza dati: spiega che lo storico si riempie")
    now = int(time.time())
    db.set("nodes_since", now - 30 * 86400)
    ns.record_advert(ALICE, name="Alice", type="companion", adv_ts=1, now=now - 5 * 86400, seeded=True)
    ns.record_advert(NEW, name="Nuovo-Nodo", type="companion", adv_ts=1, hops=2, now=now - 3600)
    ns.record_advert(NEW, name="Nuovo-Nodo", type="companion", adv_ts=2, hops=2, now=now - 60)
    db.set_repeater("Rip-Monte", "pw")
    for i in range(48):
        db._exec("INSERT INTO repeater_status (ts, name, ok, bat_mv) VALUES (?,?,?,?)",
                 (now - (47 - i) * 3600, "Rip-Monte", 1, int(3700 - 20 * (i / 24))))
    for d in range(14):
        db._exec("INSERT INTO link_history VALUES (?,?,?,?)", (now - d * 86400 - 60, "Rip-Monte", REP2[:8], 5.0 if d < 7 else 8.0))
    t = ns.telegram_text(mesh.contacts(), now=now)
    check("Nodi attivi 1 (2 in 7 giorni) · conosciuti 2" in t, "/rete: nodi attivi e conosciuti")
    check("🆕 Nuovi (7 giorni): Nuovo-Nodo" in t and "🔕 Spariti (zitti da più di 72 ore): Alice (5 giorni)" in t,
          "/rete: nuovi e spariti")
    check("🏆 Più attivi (7 giorni): Nuovo-Nodo 2, Alice 1" in t, "/rete: più attivi")
    check("🟢 Rip-Monte · 100.0% · 3.66 V, scende 20 mV/giorno → soglia tra ~8 giorni" in t, "/rete: ripetitore con previsione della batteria")
    check("Rip-Monte → Rip-Valle -3.0 dB (ora 5.0 dB)" in t, "/rete: collegamento peggiorato")
    check(("rete" in dict(COMMANDS)) and any(BTN_NET in [b.text for b in row] for row in KEYBOARD.keyboard), "/rete nel menu e pulsante 📶 Rete")
    bot = Bot({"token": "123456:TEST", "allowed_chat_ids": [1]}, core)
    bot.app = SimpleNamespace(bot=FakeLiveBot())
    chat, stranger = FakeChat(1), FakeChat(99)
    asyncio.run(bot.network_cmd(FakeUpdate(chat), Ctx()))
    check(chat.sent and chat.sent[-1].startswith("📶 Rete") and "Nuovo-Nodo" in chat.sent[-1], "bot: /rete risponde")
    asyncio.run(bot.network_cmd(FakeUpdate(stranger), Ctx()))
    check(not any("📶 Rete" in m for m in stranger.sent), "bot: chat non autorizzata respinta")


if __name__ == "__main__":
    test_collect()
    test_rx_event()
    test_gone()
    test_calcs()
    test_api()
    test_telegram()
    print("Tutti i test superati.")
