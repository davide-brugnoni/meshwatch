"""Test di sensori, percorso dei pacchetti, copertura, aggiornamenti in tempo reale e bot,
con radio simulata.
Esecuzione:  python tests/test_features.py
"""
import asyncio
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient  # noqa: E402

from meshwatch import maintenance  # noqa: E402
from meshwatch.auth import Auth  # noqa: E402
from meshwatch.core import Core, fmt_sensor, sensor_info  # noqa: E402
from meshwatch.db import DB  # noqa: E402
from meshwatch.mesh import Mesh, decode_path, parse_lpp  # noqa: E402
from meshwatch.web import create_app  # noqa: E402
from tests.test_chat import ALICE, REP, REP2, ChatMesh, contact  # noqa: E402


class SensorMesh(ChatMesh):
    """Radio simulata che risponde alla telemetria con GPS e sensori."""

    def __init__(self):
        super().__init__()
        self.temp = 20.0
        self.fix = True
        self.answer = True
        self.route = (2, ["b2", "c3"])

    async def request_telemetry(self, name, attempts=2):
        self.asked = getattr(self, "asked", []) + [name]
        if not self.answer or name in getattr(self, "silent", ()):
            return None
        lpp = [{"channel": 1, "type": "voltage", "value": getattr(self, "volts", {}).get(name, 4.02)},
               {"channel": 2, "type": "temperature", "value": self.temp},
               {"channel": 2, "type": "humidity", "value": 55},
               {"channel": 3, "type": "accelerometer", "value": {"acc_x": 0.1, "acc_y": 0.0, "acc_z": 1.0}}]
        if self.fix:
            lpp.append({"channel": 1, "type": "gps", "value": {"latitude": 45.2, "longitude": 9.2, "altitude": 300}})
        res = parse_lpp(lpp)
        res["hops"], res["path"] = self.route
        return res


def check(cond, msg):
    print(("OK   " if cond else "FAIL ") + msg)
    if not cond:
        sys.exit(1)


def setup():
    db = DB(os.path.join(tempfile.mkdtemp(), "t.db"))
    mesh = SensorMesh()
    core = Core({}, db, mesh)
    c = TestClient(create_app(core, Auth(db), {"mode": "lan"}))
    c.post("/api/setup", json={"username": "admin1", "password": "segreto1"})
    c.post("/api/users", json={"username": "fam", "password": "123456", "role": "viewer"})
    v = TestClient(create_app(core, Auth(db), {"mode": "lan"}))
    v.post("/api/login", json={"username": "fam", "password": "123456"})
    anon = TestClient(create_app(core, Auth(db), {"mode": "lan"}))
    tid = db.add_tracker(contact="Alice", label="Escursionista")
    return db, mesh, core, c, v, anon, tid


def test_sensors():
    db, mesh, core, c, v, anon, tid = setup()
    notes = []

    async def collect(text):
        notes.append(text)
    core.notifiers.append(collect)

    lpp = parse_lpp([{"channel": 3, "type": "accelerometer", "value": {"acc_x": 0.5, "acc_y": 0, "acc_z": 1}},
                     {"channel": 4, "type": "presence", "value": True}, {"channel": 5, "type": "colour", "value": "x"}])
    check({s["type"] for s in lpp["sensors"]} == {"accelerometer.acc_x", "accelerometer.acc_y", "accelerometer.acc_z", "presence"},
          "LPP: valori composti divisi per campo, valori non numerici ignorati")
    check(sensor_info("voltage", 1)["label"] == "Batteria" and sensor_info("voltage", 2)["label"] == "Tensione"
          and sensor_info("accelerometer.acc_x", 3)["label"] == "Accelerometro X", "nomi dei sensori")
    check(fmt_sensor("temperature", 2, 21.04) == "21.0 °C" and sensor_info("sconosciuto", 0)["label"] == "Sconosciuto",
          "formato dei valori e sensori sconosciuti")

    asyncio.run(core.poll_tracker(tid))
    latest = {(x["type"], x["channel"]): x["value"] for x in db.sensor_latest(tid)}
    check(latest[("temperature", 2)] == 20.0 and latest[("voltage", 1)] == 4.02 and len(latest) == 6,
          "letture dei sensori salvate")
    mesh.fix = False
    mesh.temp = 22.5
    asyncio.run(core.poll_tracker(tid))
    check(len(db.positions_since(tid, 0)) == 1 and db.sensor_latest(tid)[0]["value"] in (4.02, 22.5),
          "senza fix GPS i sensori (e la batteria) vengono salvati lo stesso")
    # ultimi valori: uguali a un GROUP BY su tutto lo storico, anche con canali o tipi mancanti
    db.add_sensor_readings(tid, [{"channel": None, "type": "x", "value": 1}, {"channel": 7, "type": None, "value": 2},
                                 {"channel": 0, "type": "x", "value": 3}])
    db.add_sensor_readings(tid, [{"channel": None, "type": "x", "value": 4}])
    full = db._all("SELECT type, channel, value, ts FROM sensor_readings WHERE rowid IN (SELECT MAX(rowid) "
                   "FROM sensor_readings WHERE tracker_id=? GROUP BY type, channel) ORDER BY channel, type", (tid,))
    check(db.sensor_latest(tid) == full and len(full) == 9, "ultimi valori dei sensori dall'indice, come leggendo tutto")
    with db.lock:
        db.conn.execute("DELETE FROM sensor_readings WHERE type IS NULL OR type='x'")
        db.conn.commit()
    st = anon.get("/api/state").json()["trackers"][0]
    temp = next(x for x in st["sensors"] if x["key"] == "temperature:2")
    check(temp["value"] == 22.5 and temp["label"] == "Temperatura" and temp["unit"] == "°C"
          and len(st["sensor_history"]["temperature:2"]) == 2, "sensori e storico nello stato della pagina")

    # soglie
    check(v.get(f"/api/trackers/{tid}/rules").status_code == 403 and anon.get(f"/api/trackers/{tid}/rules").status_code == 401,
          "soglie solo per l'admin")
    r = c.get(f"/api/trackers/{tid}/rules").json()
    check(not r["rules"] and any(s["type"] == "temperature" for s in r["sensors"]), "sensori disponibili per le soglie")
    check(c.post(f"/api/trackers/{tid}/rules", json={"type": "temperature", "channel": 2, "op": "=", "value": 1}).status_code == 422,
          "operatore non valido rifiutato")
    rid = c.post(f"/api/trackers/{tid}/rules", json={"type": "temperature", "channel": 2, "op": ">", "value": 30}).json()["id"]
    c.post(f"/api/trackers/{tid}/rules", json={"type": "humidity", "channel": 2, "op": "<", "value": 20})
    mesh.temp = 35
    asyncio.run(core.poll_tracker(tid))
    asyncio.run(core.poll_tracker(tid))
    check(len(notes) == 1 and "temperatura 35.0 °C (sopra 30.0 °C)" in notes[0], "avviso soglia superata, una volta sola")
    mesh.answer = False
    asyncio.run(core.poll_tracker(tid))
    mesh.answer = True
    check(len(notes) == 1, "nessuna risposta: lo stato della soglia non cambia")
    mesh.temp = 25
    asyncio.run(core.poll_tracker(tid))
    check(len(notes) == 2 and "di nuovo nella norma" in notes[1], "avviso di rientro")
    check(c.delete(f"/api/trackers/{tid}/rules/999").status_code == 404, "soglia inesistente")
    check(c.delete(f"/api/trackers/{tid}/rules/{rid}").json()["ok"] and len(db.sensor_rules(tid)) == 1, "soglia rimossa")
    acts = [x["action"] for x in c.get("/api/audit").json()]
    check("soglia sensore aggiunta" in acts and "soglia sensore rimossa" in acts, "soglie nel registro attività")

    # pulizia e cancellazione
    db._exec("UPDATE sensor_readings SET ts=? WHERE rowid IN (SELECT rowid FROM sensor_readings LIMIT 6)",
             (int(time.time()) - 400 * 86400,))
    n_before = db._one("SELECT COUNT(*) AS n FROM sensor_readings")["n"]
    maintenance.purge(db, 180)
    check(db._one("SELECT COUNT(*) AS n FROM sensor_readings")["n"] == n_before - 6, "pulizia delle letture vecchie")
    db.delete_tracker(tid)
    check(not db.sensor_rules(tid) and not db.sensor_latest(tid), "eliminando il dispositivo spariscono letture e soglie")


def test_battery_alerts():
    db, mesh, core, c, v, anon, tid = setup()
    notes = []

    async def collect(text):
        notes.append(text)
    core.notifiers.append(collect)
    alert = lambda volt: asyncio.run(core._battery_alert(("t", tid), volt, 3.5, f"batt {volt}"))

    check(c.get("/api/alerts/battery").json() == {"batt_repeat_h": 0, "batt_quiet": ""},
          "avvisi batteria: valori iniziali")
    check(v.get("/api/alerts/battery").status_code == 403, "impostazioni avvisi batteria solo per l'admin")
    alert(3.3); alert(3.2)
    check(len(notes) == 1, "di default un avviso solo finché la batteria resta bassa")
    alert(3.55); alert(3.3)
    check(len(notes) == 1, "risalita sotto i 0,1 V di margine: nessun nuovo avviso")
    alert(3.7); alert(3.3)
    check(len(notes) == 2, "batteria risalita e di nuovo bassa: nuovo avviso")

    check(c.put("/api/alerts/battery", json={"batt_repeat_h": 3}).json()["ok"], "ripetizione ogni 3 ore salvata")
    alert(3.3)
    check(len(notes) == 2, "prima delle 3 ore non si ripete")
    core.batt_sent[("t", tid)] -= 3 * 3600 + 1
    alert(3.3)
    check(len(notes) == 3, "dopo 3 ore l'avviso si ripete")

    for bad in ("25:00-07:00", "22:00", "07:00-07:00"):
        check(c.put("/api/alerts/battery", json={"batt_quiet": bad}).status_code == 400, f"fascia non valida rifiutata ({bad})")
    a, b = time.strftime("%H:%M", time.localtime(time.time() - 3600)), time.strftime("%H:%M", time.localtime(time.time() + 3600))
    check(c.put("/api/alerts/battery", json={"batt_quiet": f"{a}-{b}"}).json()["ok"], "fascia di silenzio salvata")
    core.batt_sent.clear()
    alert(3.3)
    check(len(notes) == 3, "durante il silenzio nessun avviso")
    c.put("/api/alerts/battery", json={"batt_quiet": ""})
    alert(3.3)
    check(len(notes) == 4, "finito il silenzio l'avviso arriva alla lettura successiva")
    check("avvisi batteria" in [x["action"] for x in c.get("/api/audit").json()], "avvisi batteria nel registro attività")


def test_route():
    db, mesh, core, c, v, anon, tid = setup()
    check(decode_path("b2c3", 0) == ["b2", "c3"] and decode_path("b2b2c3c3", 1) == ["b2b2", "c3c3"]
          and decode_path("", 0) == [] and decode_path("b2c", 0) == ["b2"], "decodifica del percorso (1 o 2 byte per salto)")
    info = Mesh.contact_info({**contact(ALICE, "Alice", 1), "out_path_len": 2, "out_path": "b2c3", "out_path_hash_mode": 0})
    check(info["path"] == ["b2", "c3"] and Mesh.contact_info(contact(ALICE, "Alice", 1))["path"] == [],
          "percorso nei contatti della rubrica")

    asyncio.run(core.poll_tracker(tid))
    r = db.last_route(tid)
    check(r["hops"] == 2 and r["path"] == ["b2", "c3"], "percorso della risposta salvato")
    st = v.get("/api/state").json()["trackers"][0]["route"]
    check([n["name"] for n in st["nodes"]] == ["Rip-Monte", "Rip-Valle"] and st["nodes"][0]["lat"] == 45.1
          and st["gateway"]["name"] == "Casa" and st["gateway"]["lat"] == 45.05, "ripetitori del percorso con posizione")
    check(anon.get("/api/state").json()["trackers"][0]["route"] is None, "l'ospite non vede i ripetitori attraversati")

    # prefisso da un byte condiviso da due ripetitori: il più recente, segnato come incerto
    other = "b2" + "77" * 31
    mesh.raw[other] = {**contact(other, "Rip-Omonimo", 2, 44.0, 8.0), "last_advert": int(time.time()) - 5}
    n = core.resolve_path(["b2", "ff"])
    check(n[0]["name"] == "Rip-Omonimo" and n[0]["ambiguous"] and n[1]["name"] is None, "prefisso ambiguo o sconosciuto")
    del mesh.raw[other]

    mesh.route = (-1, [])
    asyncio.run(core.poll_tracker(tid))
    check(v.get("/api/state").json()["trackers"][0]["route"]["hops"] == -1, "risposta arrivata in flood")
    mesh.answer = False
    asyncio.run(core.poll_tracker(tid))
    check(db.last_route(tid)["hops"] == -1, "una richiesta senza risposta non cambia il percorso")

    mesh.raw[ALICE].update(out_path_len=1, out_path="c3", out_path_hash_mode=0)
    alice = next(x for x in c.get("/api/contacts").json()["contacts"] if x["name"] == "Alice")
    check(alice["route"] == ["Rip-Valle"], "percorso dei contatti nella rubrica")
    check(REP.startswith("b2") and REP2.startswith("c3"), "dati di prova coerenti")


def test_coverage():
    import sqlite3
    # migrazione: richieste riuscite delle versioni precedenti prendono la posizione salvata con loro
    path = os.path.join(tempfile.mkdtemp(), "vecchio.db")
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE positions (ts INTEGER, lat REAL, lon REAL, alt REAL, batt_v REAL, tracker_id INTEGER);
        CREATE TABLE tracker_polls (ts INTEGER, ok INTEGER, tracker_id INTEGER);
        INSERT INTO positions VALUES (1000, 45.5, 9.5, 0, 4, 1);
        INSERT INTO tracker_polls VALUES (999, 1, 1);
        INSERT INTO tracker_polls VALUES (2000, 0, 1);""")
    old.commit()
    old.close()
    rows = DB(path)._all("SELECT ts, lat, lon, est FROM tracker_polls ORDER BY ts")
    check(rows[0]["lat"] == 45.5 and rows[0]["est"] == 0 and rows[1]["lat"] is None,
          "migrazione: copertura ricostruita dalle posizioni già salvate")

    db, mesh, core, c, v, anon, tid = setup()
    asyncio.run(core.poll_tracker(tid))                     # risponde con fix a 45.2, 9.2
    mesh.answer = False
    asyncio.run(core.poll_tracker(tid))                     # non risponde: ultima posizione nota
    mesh.answer, mesh.fix = True, False
    asyncio.run(core.poll_tracker(tid))                     # risponde senza fix
    rows = db._all("SELECT ok, lat, est FROM tracker_polls ORDER BY rowid")
    check([(r["ok"], r["lat"], r["est"]) for r in rows] == [(1, 45.2, 0), (0, 45.2, 1), (1, 45.2, 1)],
          "posizione di ogni richiesta (stimata se senza risposta o senza fix)")
    db._exec("UPDATE positions SET ts=ts-7200")
    mesh.answer = False
    asyncio.run(core.poll_tracker(tid))
    check(db._one("SELECT lat FROM tracker_polls ORDER BY rowid DESC LIMIT 1")["lat"] is None,
          "posizione troppo vecchia: richiesta esclusa dalla copertura")

    db._exec("INSERT INTO tracker_polls (ts, ok, tracker_id, lat, lon, est) VALUES (?,?,?,?,?,?)",
             (int(time.time()), 0, tid, 46.0, 10.0, 1))
    cv = v.get("/api/coverage").json()
    cells = sorted(cv["cells"], key=lambda x: x["bounds"][0][0])
    check(cv["polls"] == 4 and len(cells) == 2, "richieste raggruppate in celle")
    check(cells[0]["ok"] == 2 and cells[0]["fail"] == 1 and cells[0]["hops"] == 2.0
          and cells[1]["ok"] == 0 and cells[1]["hops"] is None, "risposte, mancate risposte e salti medi per cella")
    b = cells[0]["bounds"]
    check(b[0][0] <= 45.2 < b[1][0] and b[0][1] <= 9.2 < b[1][1] and abs((b[1][0] - b[0][0]) * 111320 - 250) < 1,
          "cella di 250 m che contiene il punto")
    check(len(v.get("/api/coverage", params={"cell": 500000}).json()["cells"]) == 2, "dimensione della cella limitata")
    check(v.get("/api/coverage", params={"days": 0}).json()["days"] == 1 and v.get("/api/coverage", params={"tracker": 99}).status_code == 404,
          "parametri controllati")
    check(anon.get("/api/coverage").status_code == 401, "copertura solo con login")
    other = db.add_tracker(contact="Bob", label="Bob")
    check(v.get("/api/coverage", params={"tracker": other}).json()["cells"] == [], "copertura di un solo dispositivo")


def test_events():
    import threading
    from meshwatch import events as ev_mod
    from meshwatch.events import Events

    async def stream_checks():
        ev = Events()
        sub = ev.subscribe()
        st = ev.stream(sub, {"state", "chat"}, ping=0.05)
        first = await st.__anext__()
        assert "event: hello" in first
        ev.publish("state")
        ev.publish("state")
        ev.publish("chat")
        got = [await st.__anext__(), await st.__anext__()]
        ping = await st.__anext__()
        # pubblicato da un altro thread (es. una funzione eseguita con asyncio.to_thread)
        threading.Thread(target=ev.publish, args=("chat",)).start()
        from_thread = await asyncio.wait_for(st.__anext__(), 2)
        ev.close()
        ended = False
        try:
            await st.__anext__()
        except StopAsyncIteration:
            ended = True
        return got, ping, from_thread, ended, len(ev.subs)
    got, ping, from_thread, ended, left = asyncio.run(stream_checks())
    check(got == ["event: state\ndata: {}\n\n", "event: chat\ndata: {}\n\n"], "notifiche ravvicinate raggruppate per argomento")
    check(ping == ": ping\n\n", "ping periodico per tenere aperta la connessione")
    check(from_thread.startswith("event: chat"), "notifica pubblicata da un altro thread")
    check(ended and left == 0, "riavvio: il flusso si chiude e la pagina viene tolta")

    async def guest_filter():
        ev = Events()
        st = ev.stream(ev.subscribe(), {"state"}, ping=5)
        await st.__anext__()
        ev.publish("chat")
        ev.publish("state")
        out = await asyncio.wait_for(st.__anext__(), 1)
        await st.aclose()
        return out, len(ev.subs)
    out, left = asyncio.run(guest_filter())
    check(out.startswith("event: state") and left == 0, "l'ospite non riceve le notifiche della chat")

    async def disconnected():
        ev = Events()

        async def gone():
            return True
        st = ev.stream(ev.subscribe(), {"state"}, gone, ping=0.01)
        await st.__anext__()
        try:
            await st.__anext__()
            return False
        except StopAsyncIteration:
            return not ev.subs
    check(asyncio.run(disconnected()), "pagina chiusa: il flusso finisce")

    async def too_many():
        ev = Events()
        for _ in range(ev_mod.MAX_CLIENTS):
            ev.subscribe()
        try:
            ev.subscribe()
            return False
        except OverflowError:
            return True
    check(asyncio.run(too_many()), "limite di pagine collegate")

    # il core pubblica quando cambia qualcosa
    db, mesh, core, c, v, anon, tid = setup()
    seen = []
    core.events.publish = seen.append
    asyncio.run(core.poll_tracker(tid))
    asyncio.run(core.on_message({"kind": "dm", "peer": ALICE[:12], "peer_name": "Alice", "text": "ciao", "sender_ts": 7}))
    c.put(f"/api/trackers/{tid}", json={"label": "Nuovo nome"})
    check(seen == ["state", "chat", "state"], "notifiche per posizione, messaggio e dispositivo modificato")
    closed = TestClient(create_app(core, Auth(db), {"mode": "lan", "public_map": False}))
    check(closed.get("/api/events").status_code == 401, "tempo reale con login se la mappa non è libera")


class FakeMsg:
    def __init__(self, chat, lat=None, lon=None):
        self.chat, self.lat, self.lon, self.stopped = chat, lat, lon, False

    async def edit_live_location(self, latitude, longitude):
        if self.stopped:
            raise RuntimeError("Message can't be edited")
        self.lat, self.lon = latitude, longitude

    async def stop_live_location(self):
        self.stopped = True

    async def reply_text(self, text, **kw):
        self.chat.sent.append(text)


class FakeChat:
    def __init__(self, cid=1):
        self.id, self.sent, self.locations = cid, [], []

    async def send_message(self, text, **kw):
        self.sent.append(text)

    async def send_location(self, latitude, longitude, live_period=None, **kw):
        m = FakeMsg(self, latitude, longitude)
        m.live_period = live_period
        self.locations.append(m)
        return m


class FakeUpdate:
    def __init__(self, chat):
        from datetime import datetime, timezone
        self.effective_chat = chat
        self.message = self.effective_message = FakeMsg(chat)
        self.message.date = datetime.now(timezone.utc)


class Ctx:
    def __init__(self, *args):
        self.args = list(args)


def test_bot():
    from meshwatch.bot import COMMANDS, Bot
    db, mesh, core, c, v, anon, tid = setup()
    tid2 = db.add_tracker(contact="Bob", label="Ciclista")
    bot = Bot({"token": "123456:TEST", "allowed_chat_ids": [1]}, core)
    chat = FakeChat(1)
    run = asyncio.run

    check([t["label"] for t in core.find_trackers("escurs")] == ["Escursionista"]
          and [t["label"] for t in core.find_trackers("CICLISTA")] == ["Ciclista"]
          and len(core.find_trackers("")) == 2 and core.find_trackers("nessuno") == [], "dispositivo scelto per nome")

    # /dove con il nome: niente pulsanti
    run(core.poll_tracker(tid))
    run(bot._by_name("last")(FakeUpdate(chat), Ctx("escursionista")))
    check(chat.locations and "Ultima posizione di Escursionista" in chat.sent[-1], "/dove nome: ultima posizione senza pulsanti")
    run(bot._by_name("last")(FakeUpdate(chat), Ctx("zzz")))
    check("Nessun dispositivo si chiama" in chat.sent[-1] and "Ciclista" in chat.sent[-1], "nome sconosciuto: elenco dei dispositivi")
    stranger = FakeChat(99)
    run(bot._by_name("last")(FakeUpdate(stranger), Ctx("escursionista")))
    check(not stranger.sent and not stranger.locations, "chat non autorizzata ignorata")

    # /stats
    now = int(time.time())
    db._exec("DELETE FROM positions")
    for i, (la, alt, b) in enumerate([(45.0, 300, 4.1), (45.01, 350, 4.05), (45.02, 420, 4.0), (45.0201, 420, 3.98)]):
        db._exec("INSERT INTO positions (ts, lat, lon, alt, batt_v, tracker_id) VALUES (?,?,?,?,?,?)",
                 (now - 3600 + i * 600, la, 9.0, alt, b, tid))
    st = core.tracker_stats(tid, now - 86400)
    check(abs(st["km"] - 2.22) < 0.02 and st["moving_h"] == round(1200 / 3600, 1) and st["points"] == 4
          and st["alt_max"] == 420 and st["batt_last"] == 3.98, "statistiche: km, movimento (senza il rumore del GPS), quota, batteria")
    check(st["polls"] == 1 and st["ok_pct"] == 100, "statistiche: risposte")
    run(bot.stats(FakeUpdate(chat), Ctx("7", "escursionista")))
    check("ultimi 7 giorni" in chat.sent[-1] and "2.2 km" in chat.sent[-1] and "Quota da 300 a 420 m" in chat.sent[-1],
          "/stats 7 nome")
    run(bot.stats(FakeUpdate(chat), Ctx("ciclista")))
    check("ultime 24 ore" in chat.sent[-1] and "Nessuna posizione" in chat.sent[-1], "/stats senza dati")

    # /sensori e /percorso
    run(bot._by_name("sens")(FakeUpdate(chat), Ctx("escursionista")))
    check("Temperatura: 20.0 °C" in chat.sent[-1] and "Batteria: 4.02 V" in chat.sent[-1], "/sensori")
    run(bot._by_name("route")(FakeUpdate(chat), Ctx("escursionista")))
    check("2 salti: Casa → Rip-Monte → Rip-Valle → Escursionista" in chat.sent[-1], "/percorso")
    run(bot._by_name("route")(FakeUpdate(chat), Ctx("ciclista")))
    check("Nessun percorso" in chat.sent[-1], "/percorso senza dati")

    # /segui: posizione in diretta aggiornata a ogni nuova posizione
    run(bot.follow(FakeUpdate(chat), Ctx("3", "escursionista")))
    live = chat.locations[-1]
    check(live.live_period == 3 * 3600 and (1, tid) in bot.live and "per 3 ore" in chat.sent[-1], "/segui 3 nome")
    run(bot.follow(FakeUpdate(chat), Ctx("99", "escursionista")))
    check(chat.locations[-1].live_period == 8 * 3600 and live.stopped, "/segui: al massimo 8 ore, la precedente si ferma")
    live = chat.locations[-1]
    mesh.fix = True
    run(core.poll_tracker(tid))
    check((live.lat, live.lon) == (45.2, 9.2), "nuova posizione: la posizione in diretta si sposta")
    bot.live[(1, tid)] = (live, time.time() - 1)
    run(core.poll_tracker(tid))
    check((1, tid) not in bot.live, "posizione in diretta scaduta: non viene più aggiornata")
    run(bot.follow(FakeUpdate(chat), Ctx("escursionista")))
    live = chat.locations[-1]
    run(bot.unfollow(FakeUpdate(chat), Ctx()))
    check(live.stopped and not bot.live and "fermata" in chat.sent[-1], "/basta")
    run(bot.follow(FakeUpdate(chat), Ctx("ciclista")))
    check("Nessuna posizione di Ciclista" in chat.sent[-1], "/segui senza posizione")

    # /intervallo con il nome
    run(bot.interval(FakeUpdate(chat), Ctx("15", "ciclista")))
    check(db.tracker(tid2)["interval_min"] == 15 and db.tracker(tid)["interval_min"] == 30, "/intervallo 15 nome")
    # senza nome e con più dispositivi: pulsanti
    upd = FakeUpdate(chat)
    asked = []

    async def reply(text, reply_markup=None, **kw):
        asked.append(reply_markup)
    upd.message.reply_text = reply
    run(bot.pick(upd, "stats", "1"))
    rows = asked[0].inline_keyboard
    check(len(rows) == 3 and rows[-1][0].callback_data == "stats:all:1", "senza nome: pulsanti con \"Tutti\"")
    names = [c for c, _ in COMMANDS]
    check(len(names) == len(set(names)) and all(n.islower() and n.isalpha() for n in names)
          and {"segui", "basta", "stats", "sensori", "percorso", "dove"} <= set(names), "menu dei comandi valido")
    acts = [x["action"] for x in c.get("/api/audit").json()]
    check("posizione in diretta" in acts and "intervallo" in acts, "comandi del bot nel registro attività")


def main():
    test_sensors()
    test_battery_alerts()
    test_route()
    test_coverage()
    test_events()
    test_bot()
    print("Tutti i test superati.")


if __name__ == "__main__":
    main()
