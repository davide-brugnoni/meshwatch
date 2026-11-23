"""Test di base con radio simulata: database, migrazione, API, ruoli, scheduler.
Esecuzione:  python tests/test_smoke.py   (serve httpx per il TestClient di FastAPI)
"""
import asyncio
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient  # noqa: E402

from meshwatch.auth import Auth  # noqa: E402
from meshwatch.core import Core, is_dangerous  # noqa: E402
from meshwatch.db import DB  # noqa: E402
from meshwatch.mesh import parse_lpp  # noqa: E402
from meshwatch.web import create_app  # noqa: E402


class FakeMesh:
    connected = True
    connecting = False
    n = 0
    reconnects = 0

    async def reconnect(self):
        self.reconnects += 1
        self.connected = True

    async def request_telemetry(self, name, attempts=3, flood_first=False):
        self.n += 1
        return {"lat": 45.0 + self.n * 0.001, "lon": 9.0, "alt": 500, "batt_v": 3.9}

    async def request_status(self, name, pw):
        return {"bat": 4012, "uptime": 9000, "noise_floor": -112, "last_rssi": -90, "last_snr": 7.5,
                "nb_recv": 1, "nb_sent": 1}

    def contacts(self):
        return [{"name": "Tracker-1", "type": "companion"}, {"name": "Ripetitore-1", "type": "ripetitore"}]

    async def send_advert(self):
        pass

    async def cli(self, n, p, c):
        return "v1.18.0"

    async def gateway_info(self):
        return {"name": "Gateway", "tx_power": 22, "max_tx_power": 22, "radio_freq": 869.618,
                "radio_bw": 62.5, "radio_sf": 8, "radio_cr": 8}

    async def gateway_set(self, **k):
        return []


def check(cond, msg):
    print(("OK   " if cond else "FAIL ") + msg)
    if not cond:
        sys.exit(1)


def main():
    lpp = parse_lpp([{"channel": 1, "type": "voltage", "value": 3.91},
                     {"channel": 1, "type": "gps", "value": {"latitude": 45.0, "longitude": 9.0, "altitude": 520}}])
    check(lpp == {"lat": 45.0, "lon": 9.0, "alt": 520, "batt_v": 3.91,
                  "sensors": [{"channel": 1, "type": "voltage", "value": 3.91}]}, "parse telemetria LPP")
    check(is_dangerous("reboot") and is_dangerous("set tx 20") and not is_dangerous("ver"), "comandi pericolosi")

    tmp = tempfile.mkdtemp()
    cfg = {"tracker": {"name": "Tracker-1", "interval_min": 30}, "repeaters": [{"name": "Ripetitore-1", "password": "x"}]}
    db = DB(os.path.join(tmp, "t.db"))
    core = Core(cfg, db, FakeMesh())
    check(len(db.trackers()) == 1 and len(db.repeaters()) == 1, "import dal config al primo avvio")
    db.delete_repeater("Ripetitore-1")
    Core(cfg, db, FakeMesh())
    check(not db.repeaters(), "import non ripetuto ai riavvii")

    c = TestClient(create_app(core, Auth(db), {"mode": "lan"}))
    me = c.get("/api/me").json()
    check(me["role"] == "guest" and me["setup"], "senza utenti: mappa libera, gestione da impostare")
    check(c.get("/api/state").status_code == 200, "mappa libera in lan")
    js = c.get("/vendor/leaflet/leaflet.js", headers={"Accept-Encoding": "gzip"})
    check(js.headers.get("content-encoding") == "gzip" and int(js.headers.get("content-length", 0)) < 60000,
          "script compressi (leaflet.js sotto i 60 KB)")
    check(c.get("/", headers={"Accept-Encoding": "gzip"}).headers.get("content-encoding") == "gzip", "pagina compressa")
    check(c.get("/api/admin").status_code == 401 and c.post("/api/update/apply").status_code == 401
          and c.post("/api/system/reboot").status_code == 401, "gestione chiusa anche senza utenti")
    check(c.post("/api/trackers/1/poll").status_code == 401, "l'ospite non chiede la posizione")
    check(c.post("/api/users", json={"username": "x1", "password": "segreto12", "role": "admin"}).status_code == 401,
          "senza login non si creano utenti")
    check(c.post("/api/setup", json={"username": "admin1", "password": "corta"}).status_code == 422,
          "password admin di almeno 8 caratteri")
    check(c.post("/api/setup", json={"username": "admin1", "password": "segreto1"}).json()["ok"], "primo avvio: crea admin")
    check(TestClient(create_app(core, Auth(db), {"mode": "lan"})).post(
        "/api/setup", json={"username": "evil", "password": "segreto99"}).status_code == 403, "setup una volta sola")
    check(c.get("/api/me").json()["role"] == "admin", "dopo il setup si è già dentro come admin")
    check(c.post("/api/trackers/1/poll").json()["ok"], "richiesta posizione")
    check(c.post("/api/trackers", json={"contact": "X", "active_hours": "25:00-1"}).status_code == 400,
          "validazione fascia oraria")
    check(c.post("/api/repeaters", json={"name": "Ripetitore-1", "password": "pw"}).json()["ok"], "aggiunta ripetitore")
    check(c.post("/api/cli", json={"node": "Ripetitore-1", "cmd": "reboot"}).json().get("needs_confirm"),
          "conferma per comandi pericolosi")
    anon = TestClient(create_app(core, Auth(db), {"mode": "lan"}))
    st = anon.get("/api/state")
    check(st.status_code == 200 and st.json()["contacts"] == [], "mappa libera senza login, senza rubrica")
    check(anon.get("/api/admin").status_code == 401, "gestione con login")
    closed = TestClient(create_app(core, Auth(db), {"mode": "lan", "public_map": False}))
    check(closed.get("/api/state").status_code == 401 and closed.get("/api/me").json()["role"] is None,
          "public_map: false chiude la mappa")
    pub = TestClient(create_app(core, Auth(db), {"mode": "public"}))
    check(pub.get("/api/state").status_code == 401, "in modalità public la mappa chiede il login")
    check(pub.post("/api/setup", json={"username": "x2", "password": "segreto12"}).status_code == 403,
          "niente setup in modalità public")
    c.post("/api/logout")
    c.post("/api/login", json={"username": "admin1", "password": "segreto1"})
    check(c.get("/api/state").status_code == 200, "login")
    c.post("/api/users", json={"username": "fam", "password": "123456", "role": "viewer"})
    check(c.delete("/api/users/admin1").status_code == 400, "non si elimina l'ultimo admin")
    check(c.post("/api/gateway", json={"radio_freq": 869.618, "radio_bw": 62.5, "radio_sf": 9, "radio_cr": 8})
          .status_code == 400, "conferma per cambiare i parametri radio")

    for _ in range(3):
        c.post("/api/trackers/1/poll")
    tid2 = db.add_tracker(contact="T2", label="T2", paused=0)
    db.add_position(tid2, 44.0, 8.0, None, None)
    db.add_position(tid2, 44.1, 8.0, None, None)
    tid3 = db.add_tracker(contact="T3", label="T3 senza posizioni", paused=0)
    n_before = len(db.positions_since(1, 0))
    last = db.last_position(1)
    r = c.delete("/api/trackers/1/track?keep_last=true").json()
    check(r["deleted"] == n_before - 1 and db.positions_since(1, 0) == [last]
          and len(db.positions_since(tid2, 0)) == 2,
          "cancella traccia tenendo l'ultima posizione (anche con un dispositivo senza posizioni)")
    db._exec("INSERT INTO tracker_polls (ts, ok, tracker_id, lat, lon) VALUES (?,?,?,?,?)", (int(time.time()), 1, 1, 44.0, 8.0))
    r = c.delete("/api/trackers/1/track").json()
    check(r["deleted"] == 1 and db.last_position(1) is None and len(db.positions_since(tid2, 0)) == 2
          and not db.coverage_polls(0, 1), "cancella traccia: sparisce tutto, anche l'ultima posizione e la copertura")
    check(c.delete("/api/track").json()["deleted"] == 2 and db.last_position(tid2) is None
          and db.last_position(tid3) is None, "cancella tutte le tracce")
    v = TestClient(create_app(core, Auth(db), {"mode": "lan"}))
    v.post("/api/login", json={"username": "fam", "password": "123456"})
    check(v.post("/api/trackers/1/poll").json()["ok"] and v.get("/api/admin").status_code == 403,
          "il viewer chiede la posizione ma non gestisce")
    check(v.delete("/api/track").status_code == 403, "un viewer non cancella le tracce")

    # sicurezza: blocco dei tentativi persistente e registro attività
    for _ in range(8):
        anon.post("/api/login", json={"username": "admin1", "password": "sbagliata"})
    check(anon.post("/api/login", json={"username": "admin1", "password": "segreto1"}).status_code == 429,
          "dopo 8 errori l'IP è bloccato")
    restarted = TestClient(create_app(core, Auth(db), {"mode": "lan"}))
    check(restarted.post("/api/login", json={"username": "admin1", "password": "segreto1"}).status_code == 429,
          "il blocco resta dopo un riavvio")
    db.clear_login_failures("testclient")
    log_rows = c.get("/api/audit").json()
    acts = [r["action"] for r in log_rows]
    check("IP bloccato per 15 minuti" in acts and "login fallito" in acts and "login" in acts
          and "password admin impostata (primo accesso)" in acts and "tracce cancellate" in acts,
          "registro attività")
    check(anon.get("/api/audit").status_code == 401 and v.get("/api/audit").status_code == 403,
          "registro solo per admin")

    # manutenzione e backup
    import io, zipfile
    old = int(time.time()) - 400 * 86400
    db._exec("INSERT INTO positions (ts, lat, lon, alt, batt_v, tracker_id) VALUES (?,?,?,?,?,?)", (old, 1, 1, 0, 0, tid2))
    db.add_position(tid2, 44.2, 8.0, None, None)   # dopo "cancella tutte le tracce" non ne aveva più
    db._exec("INSERT INTO repeater_status (ts, name, ok) VALUES (?,?,?)", (old, "Ripetitore-1", 1))
    db.add_repeater_status("Ripetitore-1", True, {"bat": 4000})
    db._exec("INSERT INTO audit VALUES (?,?,?,?,?)", (old, "x", "", "vecchio", ""))
    last2 = db.last_position(tid2)
    check(c.get("/api/maintenance").json()["retention_days"] == 180, "conservazione di default 180 giorni")
    check(c.put("/api/maintenance", json={"retention_days": 3}).status_code == 400, "minimo 7 giorni")
    check(c.put("/api/maintenance", json={"backup_telegram": True}).status_code == 400, "backup Telegram senza bot")
    n = c.post("/api/maintenance/purge").json()["deleted"]
    check(n == 3 and db.last_position(tid2) == last2 and db.repeater_latest("Ripetitore-1")[0],
          "pulizia dei dati vecchi, restano ultima posizione e ultimo stato")
    tid3 = db.add_tracker(contact="T3", label="T3", paused=0)
    db._exec("INSERT INTO positions (ts, lat, lon, alt, batt_v, tracker_id) VALUES (?,?,?,?,?,?)", (old, 3, 3, 0, 0, tid3))
    c.post("/api/maintenance/purge")
    check(db.last_position(tid3) is not None, "resta l'ultima posizione anche se vecchia")
    c.put("/api/maintenance", json={"retention_days": 0})
    check(c.post("/api/maintenance/purge").status_code == 400, "0 = per sempre, niente pulizia")
    r = c.get("/api/backup")
    z = zipfile.ZipFile(io.BytesIO(r.content))
    check(r.status_code == 200 and "meshwatch.db" in z.namelist() and "LEGGIMI.txt" in z.namelist(), "backup scaricabile")
    check(anon.get("/api/backup").status_code == 401 and v.get("/api/backup").status_code == 403, "backup solo admin")
    sent = []
    async def fake_send(data, name, caption):
        sent.append(name)
    core.file_senders.append(fake_send)
    check(c.post("/api/backup/telegram").json()["ok"] and sent and db.get("last_backup"), "backup su Telegram")
    # backup protetto da password (AES-256): database e config cifrati, LEGGIMI in chiaro
    import pyzipper
    check(c.get("/api/maintenance").json()["backup_protected"] is False, "backup senza password di default")
    check(c.put("/api/maintenance", json={"backup_password": "corta"}).status_code == 400, "password del backup: almeno 8 caratteri")
    check(v.put("/api/maintenance", json={"backup_password": "segreta-123"}).status_code == 403, "password del backup solo admin")
    check(c.put("/api/maintenance", json={"backup_password": "segreta-123"}).json()["ok"]
          and c.get("/api/maintenance").json()["backup_protected"], "password del backup impostata")
    check("segreta-123" not in c.get("/api/maintenance").text and "segreta-123" not in c.get("/api/audit").text,
          "la password del backup non torna alla pagina né nel registro")
    r = c.get("/api/backup")
    z = zipfile.ZipFile(io.BytesIO(r.content))
    enc = {i.filename: bool(i.flag_bits & 1) for i in z.infolist()}
    check(enc == {"LEGGIMI.txt": False, "meshwatch.db": True} and "7-Zip" in z.read("LEGGIMI.txt").decode(),
          "backup protetto: database cifrato, LEGGIMI in chiaro con le istruzioni")
    try:
        z.read("meshwatch.db")
        opened = True
    except RuntimeError:
        opened = False
    az = pyzipper.AESZipFile(io.BytesIO(r.content))
    az.setpassword(b"sbagliata")
    try:
        az.read("meshwatch.db")
        wrong = True
    except RuntimeError:
        wrong = False
    az.setpassword(b"segreta-123")
    check(not opened and not wrong and az.read("meshwatch.db")[:15] == b"SQLite format 3",
          "backup protetto: senza password o con quella sbagliata non si apre, con quella giusta sì")
    captions = []

    async def cap_send(data, name, caption):
        captions.append(caption)
    core.file_senders[:] = [cap_send]
    c.post("/api/backup/telegram")
    check("protetto da password" in captions[-1], "backup su Telegram protetto, scritto nella didascalia")
    # password impostata ma libreria mancante: mai un backup in chiaro
    import builtins
    real_import = builtins.__import__

    def no_pyzipper(name, *a, **kw):
        if name == "pyzipper":
            raise ImportError(name)
        return real_import(name, *a, **kw)
    builtins.__import__ = no_pyzipper
    try:
        r = c.get("/api/backup")
        rt = c.post("/api/backup/telegram")
    finally:
        builtins.__import__ = real_import
    check(r.status_code == 503 and "pyzipper" in r.json()["detail"] and rt.status_code == 503,
          "password senza libreria: backup rifiutato, non in chiaro")
    check(c.put("/api/maintenance", json={"backup_password": ""}).json()["ok"]
          and not c.get("/api/maintenance").json()["backup_protected"], "password del backup tolta")
    z = zipfile.ZipFile(io.BytesIO(c.get("/api/backup").content))
    check(z.read("meshwatch.db")[:15] == b"SQLite format 3", "senza password: zip normale")
    acts = [x["action"] for x in c.get("/api/audit").json()]
    check("password del backup impostata" in acts and "password del backup rimossa" in acts, "password del backup nel registro")
    core.file_senders.clear()
    db.delete_tracker(tid3)

    # intervallo adattivo
    from meshwatch.core import distance_m
    check(abs(distance_m({"lat": 45.0, "lon": 9.0}, {"lat": 45.001, "lon": 9.0}) - 111) < 2, "distanza in metri")
    db.update_tracker(1, interval_min=60, moving_interval_min=10)
    t1 = db.tracker(1)
    check(core.interval_min(t1) == 60, "fermo: intervallo normale")
    core._update_moving(t1, {"lat": 45.0, "lon": 9.0}, {"lat": 45.01, "lon": 9.0})
    check(core.interval_min(t1) == 10 and core.next_due[1] <= time.time() + 600, "in movimento: intervallo veloce")
    core._update_moving(t1, {"lat": 45.01, "lon": 9.0}, {"lat": 45.0101, "lon": 9.0})
    check(core.interval_min(t1) == 60, "di nuovo fermo")
    db.update_tracker(1, moving_interval_min=0)
    core._update_moving(t1, {"lat": 45.0, "lon": 9.0}, {"lat": 45.01, "lon": 9.0})
    check(core.interval_min(db.tracker(1)) == 60, "intervallo adattivo disattivato")
    core.moving.clear()

    # radio: avviso, riconnessione, ritorno
    notes = []
    async def collect(text):
        notes.append(text)
    async def watchdog():
        core.notifiers.append(collect)
        core.RADIO_ALERT_S, core.RADIO_RECONNECT_S = 0.05, 0.1
        core.mesh.connected = False
        task = asyncio.create_task(core.radio_watchdog(tick=0.02))
        await asyncio.sleep(0.4)
        task.cancel()
        core.notifiers.remove(collect)
    asyncio.run(watchdog())
    check(core.mesh.reconnects == 1 and core.mesh.connected, "radio scollegata: riconnessione completa")
    check(len(notes) == 2 and "scollegata" in notes[0] and "di nuovo collegata" in notes[1], "avvisi radio")

    # avviso di nuova versione: una volta sola per versione
    class FakeUpdater:
        sha = "a1"
        async def check(self):
            return {"available": True, "problems": [], "latest": {"sha": self.sha, "message": "Novità"}}
    upd_notes = []
    async def collect_upd(text):
        upd_notes.append(text)
    async def upd_watch():
        core.notifiers.append(collect_upd)
        fu = FakeUpdater()
        task = asyncio.create_task(core.update_watch(fu, every=0.02, first_delay=0))
        await asyncio.sleep(0.1)
        fu.sha = "b2"
        await asyncio.sleep(0.1)
        task.cancel()
        core.notifiers.remove(collect_upd)
    asyncio.run(upd_watch())
    check(len(upd_notes) == 2 and "Novità" in upd_notes[0], "avviso nuova versione, una volta per versione")

    async def sched():
        core.next_due.clear()
        task = asyncio.create_task(core.tracker_loop())
        await asyncio.sleep(0.3)
        task.cancel()
    asyncio.run(sched())
    check(core.next_due.get(1, 0) > time.time(), "scheduler dei tracker")
    print("Tutti i test superati.")


if __name__ == "__main__":
    main()
