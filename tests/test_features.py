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
from meshwatch.mesh import parse_lpp  # noqa: E402
from meshwatch.web import create_app  # noqa: E402
from tests.test_chat import ChatMesh  # noqa: E402


class SensorMesh(ChatMesh):
    """Radio simulata che risponde alla telemetria con GPS e sensori."""

    def __init__(self):
        super().__init__()
        self.temp = 20.0
        self.fix = True
        self.answer = True

    async def request_telemetry(self, name):
        if not self.answer:
            return None
        lpp = [{"channel": 1, "type": "voltage", "value": 4.02},
               {"channel": 2, "type": "temperature", "value": self.temp},
               {"channel": 2, "type": "humidity", "value": 55},
               {"channel": 3, "type": "accelerometer", "value": {"acc_x": 0.1, "acc_y": 0.0, "acc_z": 1.0}}]
        if self.fix:
            lpp.append({"channel": 1, "type": "gps", "value": {"latitude": 45.2, "longitude": 9.2, "altitude": 300}})
        return parse_lpp(lpp)


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


def main():
    test_sensors()
    print("Tutti i test superati.")


if __name__ == "__main__":
    main()
