"""Test del ripristino di un backup dalla pagina (Gestione → Sistema → Ripristina un backup).
Esecuzione:  python tests/test_restore.py
"""
import asyncio
import base64
import io
import os
import sqlite3
import sys
import tempfile
import time
import zipfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import yaml  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from meshwatch import restore, updater  # noqa: E402
from meshwatch.auth import Auth, hash_password  # noqa: E402
from meshwatch.core import Core  # noqa: E402
from meshwatch.db import DB  # noqa: E402
from meshwatch.web import create_app  # noqa: E402
from tests.test_chat import ChatMesh  # noqa: E402


def check(cond, msg):
    print(("OK   " if cond else "FAIL ") + msg)
    if not cond:
        sys.exit(1)


def install(tmp, token="TOKEN-ATTUALE", port=8080):
    """Un'installazione finta come quella di install.sh: database e config.yaml nella stessa cartella."""
    d = Path(tmp)
    cfg_path = d / "config.yaml"
    cfg_path.write_text(yaml.safe_dump({"telegram": {"token": token, "allowed_chat_ids": [1]},
                                        "web": {"mode": "lan", "port": port}, "serial_port": "auto",
                                        "database": str(d / "meshwatch.db")}))
    cfg = yaml.safe_load(cfg_path.read_text())
    cfg["_config_path"] = str(cfg_path)
    return cfg


def app_for(cfg):
    db = DB(cfg["database"])
    core = Core(cfg, db, ChatMesh())
    db.set_user("admin1", hash_password("segreto1"), "admin")
    db.set_user("fam", hash_password("123456"), "viewer")
    c = TestClient(create_app(core, Auth(db), {"mode": "lan"}))
    c.post("/api/login", json={"username": "admin1", "password": "segreto1"})
    v = TestClient(create_app(core, Auth(db), {"mode": "lan"}))
    v.post("/api/login", json={"username": "fam", "password": "123456"})
    return db, core, c, v


def upload(c, data, password=""):
    return c.post("/api/restore/check", content=data,
                  headers={"Content-Type": "application/zip",
                           "X-Backup-Password": base64.b64encode(password.encode()).decode()})


def zip_of(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


def main():
    # ---------- il backup: un'altra installazione con dati diversi
    src_dir = tempfile.mkdtemp()
    src_cfg = install(src_dir, token="TOKEN-DEL-BACKUP", port=9090)
    sdb, score, sc, _ = app_for(src_cfg)
    t_old = sdb.add_tracker(contact="Bob", label="Dal backup")
    now = int(time.time())
    # orari fissi: il backup è di un'ora fa, le posizioni attuali sono dopo (su una macchina veloce
    # tutto succederebbe nello stesso secondo e "dopo il backup" non si potrebbe distinguere)
    sdb._exec("INSERT INTO positions (ts, lat, lon, alt, batt_v, tracker_id) VALUES (?,?,?,?,?,?)",
              (now - 3600, 44.0, 8.0, 100, 4.0, t_old))
    sdb.set_repeater("Rip-Backup", "pw")
    sdb.set("tg_report_time", "07:15")
    plain = sc.get("/api/backup").content
    sc.put("/api/maintenance", json={"backup_password": "segreta-123"})
    locked = sc.get("/api/backup").content

    # ---------- l'installazione da ripristinare
    tmp = tempfile.mkdtemp()
    cfg = install(tmp)
    db, core, c, v = app_for(cfg)
    t_now = db.add_tracker(contact="Alice", label="Attuale")
    for i in range(3):
        db._exec("INSERT INTO positions (ts, lat, lon, alt, batt_v, tracker_id) VALUES (?,?,?,?,?,?)",
                 (now - 600 + i * 60, 45.0 + i / 100, 9.0, 300, 4.1, t_now))

    # controllo: niente cambia, riassunto corretto
    r = upload(c, plain)
    j = r.json()
    check(r.status_code == 200 and j["has_data"] and j["has_config"] and not j["encrypted"] and j["backup_ts"],
          "controllo del backup: dati e config trovati, data del backup letta")
    check(j["data"]["trackers"] == ["Dal backup"] and j["data"]["repeaters"] == ["Rip-Backup"]
          and {"username": "admin1", "role": "admin"} in j["data"]["users"] and j["data"]["positions"] == 1,
          "riassunto: dispositivi, ripetitori, utenti, posizioni")
    check(any("posizioni registrate dopo il backup" in w for w in j["warnings"]), "avviso: posizioni recenti che si perderebbero")
    w = " ".join(j["config_warnings"])
    check("token diverso" in w and "porta 8080 → 9090" in w and "TOKEN-DEL-BACKUP" not in r.text,
          "avvisi sul config (bot e porta diversi) senza mostrare il token")
    check(j["config_mode"] == "direct", "config scrivibile: si può ripristinare (installazione con install.sh)")
    check([t["label"] for t in db.trackers()] == ["Attuale"], "il controllo non cambia niente")

    # password
    r = upload(c, locked)
    check(r.status_code == 422 and r.json()["detail"]["code"] == "password", "backup protetto senza password: la chiede")
    r = upload(c, locked, "sbagliata")
    check(r.status_code == 422 and "sbagliata" in r.json()["detail"]["message"], "password sbagliata")
    r = upload(c, locked, "segreta-123")
    check(r.status_code == 200 and r.json()["encrypted"] and r.json()["data"]["trackers"] == ["Dal backup"],
          "backup protetto con la password giusta")
    check(upload(c, "è".encode() * 10, "x").status_code == 400, "password con lettere accentate e file non zip")

    # file non validi
    check("non è uno zip" in upload(c, b"ciao").json()["detail"], "file che non è uno zip")
    check("non è un backup di MeshWatch" in upload(c, zip_of({"foto.jpg": b"x"})).json()["detail"], "zip che non è un backup")
    check(upload(c, b"").status_code == 400, "nessun file")
    check("non è un database valido" in upload(c, zip_of({"meshwatch.db": b"x" * 5000})).json()["detail"], "database finto")
    bad = Path(tmp) / "noadmin.db"
    nd = DB(str(bad))
    nd.add_tracker(contact="X", label="X")
    nd.conn.close()
    check("nessun utente admin" in upload(c, zip_of({"meshwatch.db": bad.read_bytes()})).json()["detail"],
          "backup senza utente admin rifiutato (non si potrebbe più entrare)")
    other = Path(tmp) / "altro.db"
    oc = sqlite3.connect(other)
    oc.execute("CREATE TABLE foto (x)")
    oc.commit()
    oc.close()
    check("non è un database di MeshWatch" in upload(c, zip_of({"meshwatch.db": other.read_bytes()})).json()["detail"],
          "database di un altro programma")
    check("non è valido" in upload(c, zip_of({"config.yaml": b"[1, 2"})).json()["detail"], "config.yaml rotto")

    # permessi
    check(v.post("/api/restore/check", content=plain).status_code == 403
          and v.post("/api/restore/apply", json={"token": "x"}).status_code == 403
          and v.post("/api/restore/undo").status_code == 403 and v.get("/api/restore").status_code == 403,
          "ripristino solo per gli admin")
    check(c.post("/api/restore/apply", json={"token": "0" * 24, "data": True}).status_code == 400, "token sconosciuto")

    # ---------- ripristino di dati e config, poi "riavvio"
    j = upload(c, locked, "segreta-123").json()
    check(c.post("/api/restore/apply", json={"token": j["token"]}).status_code == 400, "niente scelto: rifiutato")
    updater.restart_event.clear()
    r = c.post("/api/restore/apply", json={"token": j["token"], "data": True, "config": True})
    check(r.status_code == 200 and r.json() == {"ok": True, "data": True, "config": True}, "ripristino avviato")
    asyncio.run(asyncio.sleep(0))
    root = restore.base_dir(cfg)
    check((root / "pending" / "meshwatch.db").exists() and (root / "prev" / "meshwatch.db").exists()
          and (root / "prev" / "config.yaml").exists() and not (root / "upload").exists(),
          "file pronti per il riavvio e stato attuale salvato")
    check(c.post("/api/restore/apply", json={"token": j["token"], "data": True}).status_code == 400,
          "lo stesso caricamento non si applica due volte")
    acts = [x["action"] for x in c.get("/api/audit").json()]
    check("ripristino del backup avviato" in acts and "backup caricato per il ripristino" in acts, "nel registro attività")
    st = c.get("/api/restore").json()
    check(st["pending"] and st["prev"]["data"], "stato: ripristino in attesa del riavvio")

    # il riavvio: il programma si chiude e riparte
    db.conn.close()
    done = restore.apply_pending(cfg)
    check(done["ok"] and done["data"] and done["config"] and not (root / "pending").exists(), "all'avvio i file vanno al loro posto")
    new_cfg = yaml.safe_load(Path(cfg["_config_path"]).read_text())
    check(new_cfg["telegram"]["token"] == "TOKEN-DEL-BACKUP", "config ripristinato")
    new_cfg["_config_path"] = cfg["_config_path"]
    new_cfg["database"] = cfg["database"]
    db2 = DB(cfg["database"])
    check([t["label"] for t in db2.trackers()] == ["Dal backup"] and db2.get("tg_report_time") == "07:15"
          and db2.repeater("Rip-Backup"), "dati del backup al loro posto (dispositivi, ripetitori, impostazioni)")
    ann = restore.take_announcement(cfg)
    check(ann and ann["ok"] and ann["user"] == "admin1" and restore.take_announcement(cfg) is None,
          "esito segnalato una volta sola (registro e Telegram)")
    check(restore.apply_pending(cfg) is None, "al riavvio successivo niente da fare")

    # ---------- annulla: torna lo stato di prima
    core2 = Core(cfg, db2, ChatMesh())
    c2 = TestClient(create_app(core2, Auth(db2), {"mode": "lan"}))
    c2.post("/api/login", json={"username": "admin1", "password": "segreto1"})
    st = c2.get("/api/restore").json()
    check(st["prev"] and st["last"]["ok"], "dopo il ripristino si può annullare")
    check(c2.post("/api/restore/undo").json()["ok"], "annulla avviato")
    db2.conn.close()
    done = restore.apply_pending(cfg)
    db3 = DB(cfg["database"])
    check(done["ok"] and done["source"] == "annullamento" and [t["label"] for t in db3.trackers()] == ["Attuale"]
          and db3._one("SELECT COUNT(*) AS n FROM positions")["n"] == 3, "annullato: dati di prima, con tutte le posizioni")
    check(yaml.safe_load(Path(cfg["_config_path"]).read_text())["telegram"]["token"] == "TOKEN-ATTUALE", "annullato: config di prima")
    check(restore.status(cfg)["prev"] is None, "dopo l'annullamento non c'è un altro annulla")

    # ---------- solo i dati, config non scrivibile
    core3 = Core(cfg, db3, ChatMesh())
    c3 = TestClient(create_app(core3, Auth(db3), {"mode": "lan"}))
    c3.post("/api/login", json={"username": "admin1", "password": "segreto1"})
    check(c3.post("/api/restore/undo").status_code == 400, "annulla senza niente da annullare")
    os.chmod(cfg["_config_path"], 0o400)
    try:
        if os.access(cfg["_config_path"], os.W_OK):   # da root i permessi non contano: simula
            orig = restore.config_target
            restore.config_target = lambda cfg: (None, "il programma non può scrivere il config")
        else:
            orig = None
        j = upload(c3, plain).json()
        check(j["config_mode"] is None and "non può scrivere" in j["config_reason"], "config non scrivibile: lo dice")
        check(c3.post("/api/restore/apply", json={"token": j["token"], "config": True}).status_code == 400,
              "config non scrivibile: ripristino del config rifiutato")
        check(c3.post("/api/restore/apply", json={"token": upload(c3, plain).json()["token"], "data": True}).json()["data"],
              "solo dati: si può")
    finally:
        os.chmod(cfg["_config_path"], 0o600)
        if orig:
            restore.config_target = orig
    db3.conn.close()
    check(restore.apply_pending(cfg)["ok"] and [t["label"] for t in DB(cfg["database"]).trackers()] == ["Dal backup"],
          "solo dati ripristinati")

    # ---------- kit SD: config tramite lo script di avvio
    sd = Path(tempfile.mkdtemp())
    restore.SD_DATA, restore.SD_PRESTART = sd, sd / "prestart.sh"
    restore.SD_RESTORE = sd / ".restore" / restore.SD_MARK
    (sd / "config.yaml").write_text(Path(cfg["_config_path"]).read_text())
    sd_cfg = {**cfg, "_config_path": str(sd / "config.yaml"), "database": str(sd / "meshwatch.db")}
    restore.SD_PRESTART.write_text("#!/bin/sh\ninstall config\n")
    mode, why = restore.config_target(sd_cfg)
    check(mode is None and "configura-servizio.sh" in why, "SD preparata prima: config da abilitare, con il comando")
    restore.SD_PRESTART.write_text("#!/bin/sh\nR=/var/lib/meshwatch/.restore/config-sd.yaml\n")
    check(restore.config_target(sd_cfg)[0] == "sd", "SD con lo script aggiornato: config ripristinabile")
    sdb2 = DB(sd_cfg["database"])
    sdb2.set_user("admin1", hash_password("segreto1"), "admin")
    up = restore.check(_tmpzip(plain), "", sdb2, sd_cfg)
    restore.apply(up["token"], True, True, sdb2, sd_cfg, "admin1")
    check(restore.SD_RESTORE.exists() and "TOKEN-DEL-BACKUP" in restore.SD_RESTORE.read_text()
          and oct(restore.SD_RESTORE.stat().st_mode & 0o777) == "0o600", "SD: config lasciato per lo script di avvio (permessi 600)")
    sdb2.conn.close()
    done = restore.apply_pending(sd_cfg)   # lo script di avvio non l'ha preso (simula SD non aggiornata)
    check(not done["ok"] and "script di avvio" in done["errors"][0] and not restore.SD_RESTORE.exists(),
          "SD: se lo script non ha copiato il config lo dice (i dati sono comunque ripristinati)")
    check([t["label"] for t in DB(sd_cfg["database"]).trackers()] == ["Dal backup"], "SD: dati ripristinati")

    # pulizia dei vecchi caricamenti e file scaduti
    d4 = DB(str(Path(tempfile.mkdtemp()) / "m.db"))
    d4.set_user("a", hash_password("segreto1"), "admin")
    c4 = {**cfg, "database": d4.path}
    a = restore.check(_tmpzip(plain), "", d4, c4)
    b = restore.check(_tmpzip(plain), "", d4, c4)
    check(not (restore.base_dir(c4) / "upload" / a["token"]).exists(), "un caricamento alla volta: il vecchio si cancella")
    meta = restore.base_dir(c4) / "upload" / b["token"] / "meta.json"
    meta.write_text('{"ts": %d}' % (time.time() - 7200))
    try:
        restore.apply(b["token"], True, False, d4, c4)
        expired = False
    except restore.RestoreError as e:
        expired = "scaduto" in str(e)
    check(expired, "caricamento non confermato entro un'ora: scaduto")
    print("Tutti i test superati.")


def _tmpzip(data):
    fd, p = tempfile.mkstemp(suffix=".zip")
    os.write(fd, data)
    os.close(fd)
    return p


if __name__ == "__main__":
    main()
