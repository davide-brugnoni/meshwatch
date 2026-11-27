"""Pubblicazione delle sole statistiche verso una dashboard esterna: niente dispositivi seguiti,
niente companion (di base), posizioni dei ripetitori arrotondate, token nell'intestazione.
Esecuzione:  python tests/test_publish.py
"""
import asyncio
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import httpx  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from meshwatch.auth import Auth  # noqa: E402
from meshwatch.core import Core  # noqa: E402
from meshwatch.web import create_app  # noqa: E402
from meshwatch.db import DB  # noqa: E402
from meshwatch.publish import REP_KEYS, Publisher, build, series, settings, snap  # noqa: E402
from tests.test_chat import ChatMesh  # noqa: E402

REP_KEY, COMP_KEY, TRK_KEY = "a1" * 32, "b2" * 32, "c3" * 32
CFG = {"publish": {"url": "https://esempio.test/api/meshwatch", "token": "segreto-lungo", "interval_min": 1}}


def check(cond, msg):
    print(("OK   " if cond else "FAIL ") + msg)
    if not cond:
        sys.exit(1)


def setup(cfg=CFG):
    db = DB(os.path.join(tempfile.mkdtemp(), "t.db"))
    core = Core(cfg, db, ChatMesh())
    ns = core.netstats
    ns.record_advert(REP_KEY, name="Rip-Monte", type="ripetitore", adv_ts=1, hops=0, snr=6, lat=43.35123, lon=12.57891)
    ns.record_advert(COMP_KEY, name="Telefono-Mario", type="companion", adv_ts=1, hops=0, snr=3, lat=43.3, lon=12.5)
    db.add_tracker(contact="Tracker-1", label="Bici di Anna", interval_min=30)
    ns.record_advert(TRK_KEY, name="Tracker-1", type="companion", adv_ts=1, hops=0, snr=2, lat=43.1, lon=12.1)
    return db, core


def test_settings():
    check(not settings({})["enabled"], "senza url e token la pubblicazione è spenta")
    s = settings(CFG)
    check(s["enabled"] and s["interval_min"] == 5, "intervallo minimo 5 minuti")
    check(settings({"publish": {**CFG["publish"], "enabled": False}})["enabled"] is False, "enabled: false la spegne")


def test_snap():
    p = snap(43.35123, 12.57891, 500)
    check(p is not None and p != [43.35123, 12.57891], "posizione spostata al centro della cella")
    check(abs(p[0] - 43.35123) * 111320 <= 250 + 1, "entro mezza cella in latitudine")
    check(snap(43.35124, 12.57892, 500) == p, "punti vicini finiscono nella stessa cella")
    check(snap(43.3, 12.5, 0) is None and snap(None, 1, 500) is None and snap(0, 0, 500) is None, "nessuna posizione")


def test_build():
    db, core = setup()
    d = build(core)
    text = json.dumps(d)
    names = [n["name"] for n in d["nodes"]]
    check("Rip-Monte" in names, "ripetitore elencato")
    check("Tracker-1" not in text and "Anna" not in text, "il dispositivo seguito non compare da nessuna parte")
    check("Telefono-Mario" not in names, "companion non elencati di base")
    check(d["types_24h"].get("companion") == 1, "ma contati")
    rep = next(n for n in d["nodes"] if n["name"] == "Rip-Monte")
    check(rep["pos"] and rep["pos"] != [43.35123, 12.57891], "posizione del ripetitore arrotondata")
    check(REP_KEY not in text and rep["id"] == REP_KEY[:8], "solo l'inizio della chiave pubblica")
    check(not {"alerts", "users", "positions", "sensors", "messages"} & set(d), "niente impostazioni né posizioni dei dispositivi")
    check("segreto-lungo" not in text, "il token non finisce nei dati")
    check(set(d["gateway"]) == {"name", "radio_freq", "radio_bw", "radio_sf", "radio_cr", "tx_power"}, "gateway senza posizione")
    check("45.05" not in text and "9.05" not in text, "la posizione del gateway non compare")
    check("hostname" not in d["system"], "il nome in rete del Raspberry non parte")
    check({"history", "firmware", "compare", "trend"} <= set(d), "storico, firmware, confronto e andamento")
    check(all(x["to"] not in ("Telefono-Mario", "Tracker-1") for x in d["links"]), "nessun collegamento verso companion o dispositivi")
    check(any(x["to"] == "Rip-Monte" for x in d["links"]), "collegamento gateway → ripetitore pubblicato")
    cfg = {"publish": {**CFG["publish"], "companions": True}}
    db2, core2 = setup(cfg)
    comp = next(n for n in build(core2)["nodes"] if n["name"] == "Telefono-Mario")
    check(comp["pos"] is None, "companion elencato su richiesta, ma senza posizione")


def test_series():
    h = [{"ts": 0, "uptime": 100, "nb_recv": 0, "nb_sent": 0, "airtime": 0},
         {"ts": 3600, "uptime": 3700, "nb_recv": 120, "nb_sent": 60, "airtime": 36},
         {"ts": 7200, "uptime": 50, "nb_recv": 5, "nb_sent": 1, "airtime": 1}]
    p = series(h, REP_KEYS)
    check(p[0]["rx_h"] is None and p[1]["rx_h"] == 120 and p[1]["tx_h"] == 60, "pacchetti all'ora tra due letture")
    check(p[1]["air_tx"] == 1.0, "airtime in % del tempo")
    check(p[2]["rx_h"] is None, "dopo un riavvio il tratto si salta")


def test_sections():
    db, core = setup()
    from meshwatch.publish import save_settings
    save_settings(db, core.cfg, sections=["traffic"], grid_m=0, title="Prova")
    d = build(core)
    check(d["title"] == "Prova" and d["sections"] == ["traffic"], "titolo e sezioni dalla pagina")
    check("trend" in d and not {"history", "links", "compare", "system", "availability"} & set(d), "le sezioni spente non partono")
    check(d["nodes"] == [] and set(d["gateway"]) == {"name"}, "senza mappa né elenco: nessun nodo, del gateway solo il nome")
    save_settings(db, core.cfg, sections=["map", "nodes"], grid_m=500)
    check(any(n["pos"] for n in build(core)["nodes"]), "con la mappa le posizioni (approssimate) tornano")


def test_api():
    db, core = setup({})
    c = TestClient(create_app(core, Auth(db), {"mode": "lan"}))
    c.post("/api/setup", json={"username": "admin1", "password": "segreto1"})
    c.post("/api/users", json={"username": "fam", "password": "123456", "role": "viewer"})
    v = TestClient(create_app(core, Auth(db), {"mode": "lan"}))
    v.post("/api/login", json={"username": "fam", "password": "123456"})
    j = c.get("/api/publish").json()
    check(not j["enabled"] or not j["ready"], "senza configurazione non parte niente")
    check(v.get("/api/publish").status_code == 403 and v.get("/api/publish/preview").status_code == 403, "solo l'admin")
    r = c.put("/api/publish", json={"enabled": True, "url": "https://esempio.test/api/meshwatch", "token": "abc123",
                                    "interval_min": 10, "grid_m": 1000, "title": "Rete di casa", "sections": ["map", "boh"]})
    j = r.json()
    check(r.status_code == 200 and j["ready"] and j["token_set"] and "token" not in j, "salvato, il token non torna indietro")
    check(j["sections"] == ["map"] and j["grid_m"] == 1000 and j["interval_min"] == 10, "valori salvati, sezioni sconosciute scartate")
    check(c.put("/api/publish", json={"url": "http://insicuro.test"}).status_code == 422, "solo indirizzi https")
    check(c.put("/api/publish", json={"grid_m": 123}).status_code == 422, "precisione non prevista respinta")
    check(c.put("/api/publish", json={"interval_min": 1}).status_code == 422, "intervallo minimo 5 minuti")
    j = c.put("/api/publish", json={"title": "Altro"}).json()
    check(j["token_set"] and j["url"].startswith("https://esempio"), "un salvataggio parziale non cancella il token")
    check(core.publisher.settings()["token"] == "abc123", "token conservato")
    p = c.get("/api/publish/preview").json()
    check(p["title"] == "Altro" and "abc123" not in json.dumps(p), "anteprima senza il token")
    j = c.put("/api/publish", json={"clear_token": True}).json()
    check(not j["token_set"] and not j["ready"], "token tolto: non parte più")
    check(c.post("/api/publish/send").status_code == 502, "invio manuale senza token: errore chiaro")
    check(any(a["action"] == "dashboard pubblica" for a in db.audit()), "modifiche nel registro attività")


def test_send():
    db, core = setup()
    seen = {}

    def handler(req):
        seen["auth"] = req.headers.get("authorization")
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"ok": True})
    p = Publisher(core, transport=httpx.MockTransport(handler))
    asyncio.run(p.send_once())
    check(seen["auth"] == "Bearer segreto-lungo", "token nell'intestazione Authorization")
    check(seen["body"]["v"] == 1 and seen["body"]["generated"] <= time.time(), "riassunto inviato")
    p = Publisher(core, transport=httpx.MockTransport(lambda r: httpx.Response(401, text="no")))
    try:
        asyncio.run(p.send_once())
        check(False, "errore del server segnalato")
    except RuntimeError as e:
        check("401" in str(e), "errore del server segnalato")


if __name__ == "__main__":
    test_settings()
    test_snap()
    test_build()
    test_series()
    test_sections()
    test_api()
    test_send()
    print("Tutto ok")
