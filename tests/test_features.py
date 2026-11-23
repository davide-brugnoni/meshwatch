"""Test di sensori, percorso dei pacchetti, copertura, aggiornamenti in tempo reale e bot,
con radio simulata.
Esecuzione:  python tests/test_features.py
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
from meshwatch.core import Core, fmt_sensor, sensor_info  # noqa: E402
from meshwatch.db import DB  # noqa: E402
from meshcore import EventType  # noqa: E402
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

    async def request_telemetry(self, name, attempts=2, flood_first=False):
        self.flood_first = flood_first
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


def test_radio_ports():
    """Qualsiasi scheda: porte ordinate per chip USB, prova una porta dopo l'altra, WiFi."""
    from types import SimpleNamespace as P
    from unittest import mock
    from serial.tools import list_ports
    from meshwatch import mesh as m
    ports = [P(device="/dev/ttyAMA0", vid=None), P(device="/dev/ttyUSB0", vid=0x067B),
             P(device="/dev/ttyACM0", vid=0x239A), P(device="/dev/ttyACM1", vid=0x303A)]
    with mock.patch.object(list_ports, "comports", return_value=ports), \
            mock.patch("glob.glob", return_value=[]):
        check(m.serial_candidates() == ["/dev/ttyACM0", "/dev/ttyACM1", "/dev/ttyUSB0"],
              "porte: prima i chip delle radio note (RAK, ESP32-S3), poi le altre USB, mai la seriale interna")

        tried = []

        class FakeMC:
            contacts, is_connected = {}, True
            auto_update_contacts = False
            async def ensure_contacts(self, **k): pass
            def subscribe(self, *a): pass
            async def start_auto_message_fetching(self): pass

        async def create_serial(port, *a, **k):
            tried.append(port)
            return FakeMC() if port == "/dev/ttyACM1" else None

        radio = Mesh()
        radio.load_channels = lambda: asyncio.sleep(0)
        with mock.patch.object(m.MeshCore, "create_serial", create_serial):
            asyncio.run(radio.connect())
        check(tried == ["/dev/ttyACM0", "/dev/ttyACM1"] and radio.connected,
              "in automatico prova le porte una alla volta finché una risponde")

    w = Mesh.from_config({"connection": "wifi", "wifi_host": "192.168.1.50"})
    u = Mesh.from_config({"serial_port": "/dev/ttyUSB3"})
    check(w.tcp == ("192.168.1.50", 5000) and u.tcp is None and u.port == "/dev/ttyUSB3",
          "config: radio via WiFi o porta USB fissa")


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
        self.id, self.sent, self.locations, self.photos = cid, [], [], []

    async def send_photo(self, photo, caption="", **kw):
        self.photos.append((photo, caption))

    async def send_action(self, action):
        pass

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


class FakeTiles:
    """Cache delle mappe finta: una tessera grigia per ogni richiesta."""

    def __init__(self):
        import io
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (256, 256), "#dddddd").save(buf, "PNG")
        self.png, self.asked, self.fail = buf.getvalue(), [], False

    async def get(self, layer, z, x, y, force=False):
        from meshwatch.tiles import TileError
        self.asked.append((layer, z, x, y, force))
        if self.fail:
            raise TileError(504, "niente Internet")
        return self.png, "image/png", 3600

    def info(self):
        return {"enabled": False, "tiles": 12, "size": 3 * 2**20}


def test_bot_images():
    """Mappa, sistema e riepilogo giornaliero su Telegram."""
    from datetime import datetime
    from meshwatch import bot as botmod, charts
    from meshwatch.bot import Bot, thin
    db, mesh, core, c, v, anon, tid = setup()
    tid2 = db.add_tracker(contact="Bob", label="Ciclista")
    tiles = FakeTiles()
    bot = Bot({"token": "123456:TEST", "allowed_chat_ids": [1], "admin_chat_ids": [1]}, core, tiles)
    chat = FakeChat(1)
    run = asyncio.run
    now = int(time.time())
    for i in range(12):
        db._exec("INSERT INTO positions (ts, lat, lon, alt, batt_v, tracker_id) VALUES (?,?,?,?,?,?)",
                 (now - 3600 * 10 + i * 1800, 45.0 + i * 0.002, 9.0 + i * 0.001, 300 + i * 10, 4.1 - i * 0.01, tid))
        db.add_sensor_readings(tid, [{"channel": 2, "type": "temperature", "value": 18 + i * 0.5},
                                     {"channel": 3, "type": "accelerometer.acc_x", "value": 0.1}], ts=now - 3600 * 10 + i * 1800)
        db.add_tracker_poll(tid, i % 3 != 0, hops=1, path=["b2"], lat=45.0 + i * 0.002, lon=9.0, est=0)
    check(thin(list(range(100)), 10)[0] == 0 and thin(list(range(100)), 10)[-1] == 99
          and len(thin(list(range(100)), 10)) == 10 and thin([1, 2], 10) == [1, 2], "punti diradati con primo e ultimo")

    # /mappa con il nome: un'immagine JPEG con la traccia, tessere chieste alla cache anche se disattivata
    run(bot._with_days("map", 1)(FakeUpdate(chat), Ctx("escursionista")))
    img, cap = chat.photos[-1]
    check(img[:3] == b"\xff\xd8\xff" and "Escursionista – ultime 24 ore" in cap and "posizioni" in cap
          and "google.com/maps" in cap, "/mappa nome: immagine con traccia e didascalia")
    check(tiles.asked and all(a[0] == "osm" and a[4] for a in tiles.asked) and 2 <= tiles.asked[0][1] <= 16,
          "tessere dalla cache del Raspberry (OpenStreetMap, anche con la cache spenta per la pagina)")
    # tutti i dispositivi in una sola mappa; Ciclista non ha posizioni ma non blocca
    n = len(chat.photos)
    run(bot.do(chat, "map", "all", "7"))
    check(len(chat.photos) == n + 1 and "Tutti i dispositivi – ultimi 7 giorni" in chat.photos[-1][1], "/mappa Tutti: una mappa sola")
    run(bot.do(chat, "map", str(tid2), "1"))
    check("Nessuna posizione registrata per Ciclista" in chat.sent[-1], "/mappa senza posizioni")
    # senza Internet: la mappa arriva lo stesso (sfondo grigio) con la nota
    tiles.fail = True
    run(bot.do(chat, "map", str(tid), "1"))
    check(chat.photos[-1][0][:3] == b"\xff\xd8\xff", "mappa anche senza tessere")
    tiles.fail = False

    # tastiera: tutti i pulsanti hanno un comando; mandata una volta sola dopo l'aggiornamento
    from meshwatch.bot import KEYBOARD
    texts = [b.text for row in KEYBOARD.keyboard for b in row]
    handled = [t for h in bot.app.handlers[0] if getattr(h, "filters", None) is not None
               and hasattr(h.filters, "strings") for t in h.filters.strings]
    check(len(texts) == 8 and set(texts) <= set(handled) and botmod.BTN_TRACK in handled,
          "tastiera con 8 pulsanti, tutti gestiti (anche Traccia oggi della tastiera vecchia)")
    kb_sent = []

    class FakeTgBot:
        async def send_message(self, cid, text, reply_markup=None, **kw):
            kb_sent.append((cid, reply_markup))
    bot.app = type("A", (), {"bot": FakeTgBot()})()
    run(bot.send_new_keyboard())
    run(bot.send_new_keyboard())
    check(kb_sent == [(1, KEYBOARD)], "tastiera nuova mandata una volta sola")

    # tastiera: una chat che ha bloccato il bot non fa rimandare il messaggio alle altre
    from telegram.error import Forbidden, NetworkError
    db.set("tg_keyboard", "")
    bot.allowed = {1, 2, 3}
    calls = []

    class FlakyBot:
        def __init__(self, fail):
            self.fail = fail

        async def send_message(self, cid, text, reply_markup=None, **kw):
            calls.append(cid)
            if cid in self.fail:
                raise self.fail[cid]
    bot.app = type("A", (), {"bot": FlakyBot({2: Forbidden("bloccato"), 3: NetworkError("rete")})})()
    run(bot.send_new_keyboard())
    check(db.get("tg_keyboard") != botmod.KEYBOARD_VERSION, "tastiera: rete giù, si riprova al prossimo avvio")
    calls.clear()
    bot.app = type("A", (), {"bot": FlakyBot({2: Forbidden("bloccato")})})()
    run(bot.send_new_keyboard())
    check(calls == [3] and db.get("tg_keyboard") == botmod.KEYBOARD_VERSION,
          "tastiera: al nuovo giro solo la chat che non l'aveva ricevuta")
    bot.allowed = {1}

    # avvio senza Internet: il bot riprova finché Telegram risponde, senza fermare il programma
    tries = []

    async def flaky_run():
        tries.append(1)
        if len(tries) < 3:
            raise NetworkError("rete non pronta")
    orig_run, orig_sleep = bot.run, asyncio.sleep
    bot.run = flaky_run
    waits = []

    async def no_sleep(sec):
        waits.append(sec)
    asyncio.sleep = no_sleep
    try:
        run(bot.start_forever(first_wait=10, max_wait=300))
    finally:
        asyncio.sleep, bot.run = orig_sleep, orig_run
    check(len(tries) == 3 and waits == [10, 20], "avvio senza Internet: il bot riprova (10 s, 20 s, …) e poi si collega")

    # mappa: se il disegno fallisce arriva comunque la risposta in testo
    orig_map = charts.static_map

    async def broken(*a, **kw):
        raise RuntimeError("disegno rotto")
    charts.static_map = broken
    try:
        run(bot.do(chat, "map", str(tid), "1"))
    finally:
        charts.static_map = orig_map
    check("immagine della mappa non disponibile" in chat.sent[-1] and "google.com/maps" in chat.sent[-1],
          "mappa non disegnata: risposta in testo con il link")
    # testo nelle immagini senza DejaVu: niente quadratini al posto delle lettere accentate
    orig_paths = charts.FONT_PATHS
    charts.FONT_PATHS = ()
    try:
        check(charts.safe_text("Città – 5°") == "Citta - 5 gradi", "senza DejaVu: lettere accentate senza accento")
    finally:
        charts.FONT_PATHS = orig_paths
    check(charts.safe_text("Città") == "Città" or not charts.has_full_font(), "con DejaVu: testo invariato")

    # /sistema
    txt = bot.system_text()
    check("Disco libero" in txt and "Radio gateway Casa" in txt and "posizioni" in txt and "Cache mappe: 12 tessere" in txt,
          "/sistema")
    db.set_repeater("Rip-Monte", "x")
    db._exec("INSERT INTO repeater_status VALUES (?,?,?,?,?,?,?,?,?,?)",
             (now - 600, "Rip-Monte", 1, 4100, 1000, -110, -80, 7.5, 1, 1))
    other = FakeChat(2)
    bot.allowed.add(2)

    # /riepilogo: adesso, ogni giorno, disattivato; viewer non può cambiarlo
    n = len(chat.photos)
    run(bot.report_cmd(FakeUpdate(chat), Ctx()))
    check(any("Riepilogo MeshWatch" in x and "Escursionista" in x and "Rip-Monte" in x for x in chat.sent[-3:])
          and len(chat.photos) == n + 1 and "spento" in chat.sent[-1] and "Gestione → Telegram" in chat.sent[-1], "/riepilogo adesso, con la mappa")
    run(bot.report_cmd(FakeUpdate(other), Ctx("21:00")))
    check("Solo le chat admin" in other.sent[-1] and not bot.report_time(), "/riepilogo HH:MM solo admin")
    run(bot.report_cmd(FakeUpdate(chat), Ctx("21.30")))
    check(bot.report_time() == "21:30" and "21:30" in chat.sent[-1], "/riepilogo 21.30")
    slot = bot.report_slot
    check(slot("21:30", datetime(2026, 5, 4, 21, 40)) == "2026-05-04" and slot("21:30", datetime(2026, 5, 4, 21, 29)) is None
          and slot("21:30", datetime(2026, 5, 4, 22, 5)) is None
          and slot("23:50", datetime(2026, 5, 5, 0, 10)) == "2026-05-04", "orario del riepilogo (anche a cavallo della mezzanotte)")
    sent = []

    async def fake_report(ch):
        sent.append(ch.id)
    bot.send_report = fake_report
    run(bot.report_tick(datetime(2026, 5, 4, 21, 31)))
    run(bot.report_tick(datetime(2026, 5, 4, 21, 35)))
    check(sorted(sent) == [1, 2], "riepilogo automatico una volta al giorno a tutte le chat")
    run(bot.report_cmd(FakeUpdate(chat), Ctx("no")))
    check(bot.report_time() == "" and not run(bot.report_tick(datetime(2026, 5, 5, 21, 31))), "/riepilogo no")
    core.cfg["telegram"] = {"daily_report": "8:05"}
    db._exec("DELETE FROM settings WHERE key='tg_report_time'")
    check(bot.report_time() == "08:05", "orario del riepilogo dal config (telegram.daily_report)")

    # dalla pagina (Gestione → Telegram): leggi, accendi, spegni, invia ora
    del bot.send_report
    sent.clear()

    async def fake_send(ch):
        sent.append(ch.id)
    bot.send_report = fake_send
    bot.app = type("A", (), {"bot": None})()
    r = c.get("/api/telegram/report").json()
    check(r == {"telegram": True, "enabled": True, "time": "08:05"}, f"pagina: riepilogo dal config {r}")
    r = c.put("/api/telegram/report", json={"enabled": False, "time": "20:15"}).json()
    check(not r["enabled"] and r["time"] == "20:15" and bot.report_time() == "", "pagina: riepilogo spento, orario ricordato")
    r = c.put("/api/telegram/report", json={"enabled": True, "time": "7.30"}).json()
    check(r["enabled"] and bot.report_time() == "07:30", "pagina: riepilogo acceso alle 07:30")
    check(c.put("/api/telegram/report", json={"enabled": True, "time": "25:00"}).status_code == 400
          and c.put("/api/telegram/report", json={"enabled": True, "time": "x"}).status_code == 422, "pagina: orario non valido")
    check(v.get("/api/telegram/report").status_code == 403, "pagina: solo admin")
    check(c.post("/api/telegram/report/send").json()["ok"] and sorted(sent) == [1, 2], "pagina: invia il riepilogo ora")
    acts = [x["action"] for x in c.get("/api/audit").json()]
    check("riepilogo automatico" in acts and "riepilogo inviato su Telegram" in acts, "riepilogo nel registro attività")
    core.reporters.clear()
    check(c.put("/api/telegram/report", json={"enabled": True, "time": "21:00"}).status_code == 400
          and not c.get("/api/telegram/report").json()["telegram"], "pagina: senza Telegram non si accende")

    # senza Pillow: risposte in testo
    orig = charts.available
    charts.available = lambda: False
    try:
        n = len(chat.photos)
        run(bot.do(chat, "map", str(tid), "1"))
        check(len(chat.photos) == n and botmod.NO_PIL in chat.sent[-1] and any("google.com/maps" in x for x in chat.sent[-3:]),
              "senza Pillow: traccia in testo, con l'indicazione per installarla")
    finally:
        charts.available = orig


def test_telemetry_retries():
    """Tre tentativi (percorso salvato, flood, flood dopo una pausa), percorso provato salvato
    anche senza risposta e mostrato dal bot."""
    from meshwatch.bot import Bot

    GPS = [{"channel": 1, "type": "gps", "value": {"latitude": 43.35, "longitude": 12.57, "altitude": 500}}]

    class Cmds:
        """answer_at: tentativo a cui il nodo risponde; late: la risposta arriva dopo la scadenza
        di quel tentativo (durante il successivo)."""
        def __init__(self, mc, answer_at, late=False):
            self.mc, self.answer_at, self.late, self.calls, self.resets = mc, answer_at, late, 0, 0

        async def reset_path(self, c):
            self.resets += 1
            c.update(out_path_len=-1, out_path="")

        async def send_binary_req(self, c, req, timeout=None, min_timeout=0):
            self.calls += 1
            tag = f"{self.calls:08x}"
            if self.calls == self.answer_at:
                delay = 0.045 if self.late else 0.005
                asyncio.get_running_loop().call_later(delay, self.mc.emit, {"tag": tag, "lpp": GPS})
            return SimpleNamespace(type=EventType.MSG_SENT, payload={"expected_ack": bytes.fromhex(tag), "suggested_timeout": 0})

    class MC:
        def __init__(self, raw, answer_at, late=False):
            self.raw, self.subs = raw, []
            self.commands = Cmds(self, answer_at, late)

        def subscribe(self, ev, cb):
            self.subs.append(cb)
            return SimpleNamespace(unsubscribe=lambda: self.subs.remove(cb))

        def emit(self, payload):
            for cb in list(self.subs):
                cb(SimpleNamespace(payload=payload))

        async def ensure_contacts(self, follow=False):
            pass

        def get_contact_by_name(self, name):
            return next((c for c in self.raw.values() if c["adv_name"] == name), None)

    def radio(answer_at, late=False):
        m = Mesh()
        m.RETRY_DELAY_S, m.DIRECT_WAIT_S, m.FLOOD_WAIT_S = 0, 0.03, 0.03
        raw = {ALICE: {**contact(ALICE, "Alice", 1), "out_path_len": 1, "out_path": "b2", "out_path_hash_mode": 0}}
        m.mc = MC(raw, answer_at, late)
        return m

    m = radio(answer_at=3)
    res = asyncio.run(m.request_telemetry("Alice"))
    check(res and res["lat"] == 43.35 and m.mc.commands.calls == 3 and m.mc.commands.resets == 2,
          "risposta al terzo tentativo, dopo due tentativi in flood")
    check(m.tried["Alice"] == (1, ["b2"]), "percorso provato per primo ricordato")
    check(m.direct["Alice"] == (["b2"], False), "tentativo diretto senza risposta registrato")
    m = radio(answer_at=0)
    check(asyncio.run(m.request_telemetry("Alice")) is None and m.mc.commands.calls == 3, "nessuna risposta dopo tre tentativi")
    m = radio(answer_at=1, late=True)
    res = asyncio.run(m.request_telemetry("Alice"))
    check(res and res["lat"] == 43.35 and m.mc.commands.calls == 2,
          "risposta in ritardo al primo tentativo accettata durante il secondo")
    check(not m.mc.subs, "nessun ascolto rimasto attivo")
    check(m.direct["Alice"] == (["b2"], True), "risposta in ritardo al tentativo diretto: il percorso funziona")
    m = radio(answer_at=1)
    res = asyncio.run(m.request_telemetry("Alice", flood_first=True))
    check(res and m.mc.commands.resets == 1 and m.tried["Alice"] == (-1, []),
          "in movimento: primo tentativo già in flood")
    check(m.direct["Alice"] is None, "partendo in flood il percorso salvato non viene giudicato")

    # advert: rubrica riletta e on_advert chiamato solo per i contatti che interessano
    m = radio(answer_at=1)
    got, reads = [], []

    async def ensure(follow=False):
        reads.append(1)
    m.mc.ensure_contacts = ensure
    m.mc.get_contact_by_key_prefix = lambda k: next((c for c in m.mc.raw.values() if c["public_key"].startswith(k)), None)

    async def on_adv(info):
        got.append(info["name"])
    m.on_advert, m.wants_advert = on_adv, lambda n: n == "Alice"
    asyncio.run(m._on_advert(SimpleNamespace(payload={"public_key": ALICE})))
    check(got == ["Alice"] and reads, "advert di un tracker passato al ciclo dei dispositivi")
    m.wants_advert = lambda n: False
    got.clear(), reads.clear()
    asyncio.run(m._on_advert(SimpleNamespace(payload={"public_key": ALICE})))
    check(not got and not reads, "advert di altri nodi: nessuna lettura in più della rubrica")

    db, mesh, core, c, v, anon, tid = setup()
    bot = Bot({"token": "123456:TEST", "allowed_chat_ids": [1]}, core)
    chat = FakeChat(1)
    t = db.tracker(tid)
    asyncio.run(bot.position_now(chat, t))
    check("Escursionista adesso" in chat.sent[-1] and "2 salti: Casa → Rip-Monte → Rip-Valle → Escursionista" in chat.sent[-1],
          "posizione adesso con il percorso della risposta")
    mesh.answer = False
    mesh.tried = {"Alice": (1, ["c3"])}
    asyncio.run(bot.position_now(chat, t))
    fail = next(x for x in chat.sent if "non ha risposto" in x)
    check("sul percorso salvato (1 salto: Casa → Rip-Valle → Escursionista)" in fail and "due volte in flood" in fail,
          "senza risposta: il bot dice quale percorso è stato provato")
    p = db.last_poll(tid)
    check(not p["ok"] and p["hops"] == 1 and p["path"] == ["c3"] and db.last_route(tid)["hops"] == 2,
          "percorso provato salvato senza cambiare quello dell'ultima risposta")
    check("tre volte in flood" not in fail, "testo dei tentativi col percorso salvato")
    mesh.answer = True
    asyncio.run(core.poll_tracker(tid))
    check(mesh.flood_first, "dopo una richiesta senza risposta si parte subito in flood")
    asyncio.run(core.poll_tracker(tid))
    check(not mesh.flood_first, "dopo una risposta, da fermo, si riprova prima il percorso salvato")


def test_bot_update():
    """/aggiornamento da Telegram: solo admin, conferma, errori e avviso dell'esito dopo il riavvio."""
    from meshwatch.bot import Bot
    from meshwatch.updater import UpdateError
    db, mesh, core, c, v, anon, tid = setup()

    class FakeUpdater:
        def __init__(self):
            self.job = {"state": "idle", "message": "", "ts": None}
            self.started = 0
            self.avail, self.probs, self.fail = True, [], None

        async def check(self):
            return {"installed": {"sha": "a" * 40, "date": "2026-11-01T10:00:00Z", "message": "vecchia"},
                    "latest": {"sha": "b" * 40, "date": "2026-11-20T10:00:00Z", "message": "Nuova funzione"},
                    "available": self.avail, "problems": self.probs, "job": self.job, "rolled_back": None}

        async def start(self):
            if self.fail:
                raise UpdateError(self.fail)
            self.started += 1

    up = FakeUpdater()
    bot = Bot({"token": "123456:TEST", "allowed_chat_ids": [1, 2], "admin_chat_ids": [1]}, core, None, up)
    chat, viewer = FakeChat(1), FakeChat(2)
    run = asyncio.run
    kbs = []
    upd = FakeUpdate(chat)

    async def reply(text, reply_markup=None, **kw):
        chat.sent.append(text)
        kbs.append(reply_markup)
    upd.message.reply_text = reply

    run(bot.update_cmd(FakeUpdate(viewer), Ctx()))
    check("Solo le chat admin" in viewer.sent[-1], "/aggiornamento: solo chat admin")
    run(bot.update_cmd(upd, Ctx()))
    check("Nuova: bbbbbbb" in chat.sent[-1] and "Nuova funzione" in chat.sent[-1]
          and [b.callback_data for b in kbs[-1].inline_keyboard[0]] == ["upd:go", "upd:no"],
          "/aggiornamento: nuova versione con i pulsanti di conferma")

    class Q:
        def __init__(self):
            self.text = None

        async def edit_message_text(self, text, **kw):
            self.text = text
    q = Q()
    run(bot.update_button(chat, q, "no"))
    check(up.started == 0 and "annullato" in q.text, "Annulla: niente aggiornamento")

    async def go():
        await bot.update_button(chat, q, "go")
        up.job = {"state": "error", "message": "pacchetto incompleto", "ts": 1}
        await asyncio.sleep(0.05)
        await bot.update_progress(chat, None, every=0.01, limit=0.2)
    run(go())
    check(up.started == 1 and "avviato" in q.text and "pacchetto incompleto" in chat.sent[-1],
          "Aggiorna ora: avvio ed errore riferito in chat")
    check("aggiornamento avviato" in [x["action"] for x in c.get("/api/audit").json()], "aggiornamento da Telegram nel registro")
    up.fail = "aggiornamento già in corso"
    run(bot.update_button(chat, q, "go"))
    check("già in corso" in q.text, "doppio clic: secondo aggiornamento rifiutato")

    up.avail = False
    run(bot.update_cmd(upd, Ctx()))
    check("già l'ultima versione" in chat.sent[-1] and kbs[-1] is None, "/aggiornamento: già aggiornato")
    up.avail, up.probs = True, ["la cartella del programma non è scrivibile dal servizio"]
    run(bot.update_cmd(upd, Ctx()))
    check("non si può aggiornare" in chat.sent[-1] and kbs[-1] is None, "/aggiornamento: motivo se non si può")


def test_reliability():
    """Recupero rapido dopo un fallimento, posizioni dagli advert, percorso inaffidabile,
    attese adattive."""
    import random as _random
    db, mesh, core, c, v, anon, tid = setup()
    t = db.tracker(tid)
    db.update_tracker(tid, interval_min=30)
    t = db.tracker(tid)
    _random.seed(1)
    d = core.next_delay_s(t)
    check(27 * 60 <= d <= 33 * 60, "intervallo normale con variazione casuale del 10%")
    mesh.answer = False
    asyncio.run(core.poll_tracker(tid, attempts=2))
    check(core.fail_streak[tid] == 1 and core.next_delay_s(t) <= 2 * 60 * 1.1, "dopo un fallimento si riprova dopo 2 minuti")
    asyncio.run(core.poll_tracker(tid))
    check(core.next_delay_s(t) <= 5 * 60 * 1.1, "poi dopo 5 minuti")
    asyncio.run(core.poll_tracker(tid))
    check(9 * 60 <= core.next_delay_s(t) <= 11 * 60, "poi dopo 10 minuti")
    asyncio.run(core.poll_tracker(tid))
    check(18 * 60 <= core.next_delay_s(t) <= 22 * 60, "poi il doppio: 20 minuti")
    for _ in range(20):
        asyncio.run(core.poll_tracker(tid))
    check(27 * 60 <= core.next_delay_s(t) <= 33 * 60, "spento a lungo: si torna all'intervallo normale")
    check(Core({}, db, mesh).fail_streak.get(tid) == core.fail_streak[tid], "fallimenti di fila ricordati dopo un riavvio")
    core.fail_streak[tid] = 1
    db.update_tracker(tid, interval_min=5)
    check(core.next_delay_s(db.tracker(tid)) <= 5 * 60 * 1.1, "mai oltre l'intervallo normale")
    db.update_tracker(tid, interval_min=30)

    # advert di un tracker che non rispondeva: richiesta anticipata
    core.next_due[tid] = time.time() + 600
    now = int(time.time())
    info = {"name": "Alice", "last_advert": now - 5, "lat": 43.1, "lon": 12.1}
    asyncio.run(core.on_advert(info))
    check(core.next_due[tid] <= time.time() + core.ADVERT_POLL_GAP_S and core.soon.get(tid),
          "advert dopo richieste senza risposta: si chiede subito la posizione (ma non a meno di un minuto dall'ultima)")
    core.next_due[tid] = time.time() + 600
    asyncio.run(core.on_advert({**info, "last_advert": now - 4}))
    check(core.next_due[tid] > time.time() + 500, "un solo anticipo per serie di fallimenti (collegamento a senso unico)")
    core.boosted[tid] -= core.ADVERT_REBOOST_S
    asyncio.run(core.on_advert({**info, "last_advert": now - 3}))
    check(core.next_due[tid] < time.time() + 500, "mezz'ora dopo un altro advert anticipa di nuovo")
    n = len(db.positions_since(tid, 0))
    check(n == 0, "primo advert: posizione solo ricordata (potrebbe essere impostata a mano)")
    asyncio.run(core.on_advert(info))
    check(len(db.positions_since(tid, 0)) == 0, "advert ripetuto ignorato")
    asyncio.run(core.on_advert({**info, "last_advert": now - 2}))
    check(len(db.positions_since(tid, 0)) == 0, "stessa posizione dell'advert precedente: non è GPS, ignorata")
    asyncio.run(core.on_advert({**info, "last_advert": now - 1, "lat": 43.2}))
    pos = db.positions_since(tid, 0)
    check(len(pos) == 1 and pos[0]["lat"] == 43.2 and pos[0]["src"] == "advert", "posizione nuova dall'advert salvata")
    asyncio.run(core.on_advert({**info, "last_advert": now, "lat": 43.3}))
    asyncio.run(core.on_advert({**info, "last_advert": now - 30, "lat": 43.4}))
    check(len(db.positions_since(tid, 0)) == 2, "advert più vecchio dell'ultimo ricevuto ignorato")
    db.set(f"advert_{tid}", f"{now + 10 ** 6},43.3,12.1")
    asyncio.run(core.on_advert({**info, "last_advert": now + 1, "lat": 43.5}))
    check(len(db.positions_since(tid, 0)) == 3, "dopo un advert con l'ora nel futuro i successivi valgono ancora")
    asyncio.run(core.on_advert({**info, "last_advert": now + 3 * 3600, "lat": 43.6}))
    check(len(db.positions_since(tid, 0)) == 3, "advert con l'ora sbagliata di ore: posizione ignorata")
    check(mesh.wants_advert("Alice") and not mesh.wants_advert("Bob"), "advert letti solo per i tracker")
    asyncio.run(core.on_advert({"name": "Altro", "last_advert": now, "lat": 1.0, "lon": 1.0}))
    check(len(db.positions_since(tid, 0)) == 3, "advert di un contatto che non è un tracker ignorato")

    # risposta: tutto torna normale
    mesh.answer = True
    asyncio.run(core.poll_tracker(tid))
    check(core.fail_streak[tid] == 0 and core.next_delay_s(t) >= 27 * 60, "dopo una risposta si torna all'intervallo normale")

    # percorso che fallisce spesso: si parte in flood anche da fermi
    core.moving[tid] = False
    check(not core.path_unreliable(tid), "percorso affidabile")
    route = db.last_route(tid)["path"]
    for _ in range(5):
        db.add_tracker_poll(tid, True, len(route), route)   # risposte in flood: non contano per il percorso
    db.add_tracker_poll(tid, True, len(route), route, direct=(route, True))
    db.add_tracker_poll(tid, False, len(route), route, direct=(route, False))
    check(not core.path_unreliable(tid), "un solo fallimento del tentativo diretto: ancora affidabile")
    db.add_tracker_poll(tid, True, len(route), route, direct=(route, False))   # diretto a vuoto, poi risposta in flood
    check(core.path_unreliable(tid), "percorso che ha fallito più volte di quante ha risposto: inaffidabile")
    asyncio.run(core.poll_tracker(tid))
    check(mesh.flood_first, "con il percorso inaffidabile il primo tentativo è in flood")
    db._exec("UPDATE tracker_polls SET ts=ts-4*3600 WHERE direct_path IS NOT NULL")
    check(not core.path_unreliable(tid), "percorso inaffidabile non provato da ore: si riprova in diretto")

    # un giro del ciclo vero: richiesta, ripianificazione, tracker eliminato tolto dalle scadenze
    async def loop_run():
        core.POLL_GAP_S = 0
        core.next_due.clear()
        mesh.answer = False
        core.next_due[999] = time.time() - 5     # tracker eliminato
        task = asyncio.create_task(core.tracker_loop())
        await asyncio.sleep(0.3)
        task.cancel()
    asyncio.run(loop_run())
    check(999 not in core.next_due and time.time() + 60 < core.next_due[tid] <= time.time() + 2 * 60 * 1.1,
          "ciclo: dopo una richiesta a vuoto la prossima tra 2 minuti; scadenze dei tracker eliminati tolte")
    mesh.answer = True

    # attese adattive
    m = Mesh()
    check(m.wait_for(False) == m.DIRECT_WAIT_S and m.wait_for(True) == m.FLOOD_WAIT_S, "attese minime senza misure")
    m._record_latency(False, 40, "Alice")
    check(m.wait_for(False, name="Alice") == m.DIRECT_WAIT_S, "una sola risposta lenta non cambia l'attesa")
    for x in (10, 20, 40, 40, 40, 40, 40, 40, 150):
        m._record_latency(True, x, "Alice")
    check(m.wait_for(True, name="Alice") == 52 and m.wait_for(False, name="Alice") == m.DIRECT_WAIT_S,
          "flood lento: si aspetta di più prima di ritrasmettere (il valore anomalo non conta)")
    check(m.wait_for(True, name="Bob") == m.FLOOD_WAIT_S, "tempi misurati per ogni nodo")
    for _ in range(20):
        m._record_latency(True, 200, "Alice")
    check(m.wait_for(True, name="Alice") == m.MAX_WAIT_S[True], "attesa mai oltre il massimo")
    check(m.wait_for(False, 25) == 25, "attesa suggerita dalla radio rispettata")


def main():
    test_sensors()
    test_battery_alerts()
    test_route()
    test_radio_ports()
    test_coverage()
    test_events()
    test_bot()
    test_bot_images()
    test_bot_update()
    test_telemetry_retries()
    test_reliability()
    print("Tutti i test superati.")


if __name__ == "__main__":
    main()
