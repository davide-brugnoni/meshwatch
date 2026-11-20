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
    n = 0

    async def request_telemetry(self, name):
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
    check(lpp == {"lat": 45.0, "lon": 9.0, "alt": 520, "batt_v": 3.91}, "parse telemetria LPP")
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
    check(c.get("/api/me").json()["role"] == "admin", "senza utenti la pagina in lan è aperta")
    check(c.post("/api/trackers/1/poll").json()["ok"], "richiesta posizione")
    check(c.post("/api/trackers", json={"contact": "X", "active_hours": "25:00-1"}).status_code == 400,
          "validazione fascia oraria")
    check(c.post("/api/repeaters", json={"name": "Ripetitore-1", "password": "pw"}).json()["ok"], "aggiunta ripetitore")
    check(c.post("/api/cli", json={"node": "Ripetitore-1", "cmd": "reboot"}).json().get("needs_confirm"),
          "conferma per comandi pericolosi")
    check(c.post("/api/users", json={"username": "fam", "password": "123456", "role": "viewer"}).status_code == 400,
          "il primo utente deve essere admin")
    c.post("/api/users", json={"username": "admin1", "password": "segreto1", "role": "admin"})
    check(c.get("/api/state").status_code == 401, "dopo il primo utente serve il login")
    c.post("/api/login", json={"username": "admin1", "password": "segreto1"})
    check(c.get("/api/state").status_code == 200, "login")
    c.post("/api/users", json={"username": "fam", "password": "123456", "role": "viewer"})
    check(c.delete("/api/users/admin1").status_code == 400, "non si elimina l'ultimo admin")
    check(c.post("/api/gateway", json={"radio_freq": 869.618, "radio_bw": 62.5, "radio_sf": 9, "radio_cr": 8})
          .status_code == 400, "conferma per cambiare i parametri radio")

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
