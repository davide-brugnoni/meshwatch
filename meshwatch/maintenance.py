"""Manutenzione: pulizia dei dati vecchi e backup.

Tre tipi di backup (make_backup, kind=...):
    configurazione  config.yaml + impostazioni.json (dispositivi, ripetitori, utenti, regole dei
                    sensori e impostazioni della pagina): pochi kB, senza lo storico
    dati            meshwatch.db: il database completo (storico e impostazioni della pagina)
    completo        meshwatch.db + config.yaml

Impostazioni salvate nel database (tabella settings), modificabili dalla pagina:
    retention_days    giorni di storico da tenere (0 = per sempre)
    backup_telegram   1 = backup settimanale inviato su Telegram alle chat admin
    backup_password   se impostata, i file nello zip sono cifrati (AES-256)
    backup_kind       tipo di backup inviato su Telegram (completo, configurazione, dati)

La password del backup protegge le copie che escono dal Raspberry (scaricate o mandate su
Telegram). Il Raspberry la conserva nel suo database per i backup automatici: non protegge
chi ha in mano la SD.
"""
import importlib.util
import io
import json
import os
import tempfile
import time
import zipfile
from datetime import datetime
from pathlib import Path

DEFAULT_RETENTION_DAYS = 180
BACKUP_EVERY_DAYS = 7
MIN_BACKUP_PASSWORD = 8
KINDS = ("completo", "configurazione", "dati")
KIND_LABEL = {"completo": "completo (dati e configurazione)", "configurazione": "configurazione", "dati": "dati"}
SETTINGS_FORMAT = "meshwatch-impostazioni"
SETTINGS_VERSION = 1
# impostazioni che sono stato del programma, non scelte: non vanno nel backup della configurazione
STATE_KEYS = {"seeded", "session_secret", "backup_error_day", "tg_live", "tg_report_last", "tg_report_prev",
              "tg_keyboard_chats", "nodes_since", "update_notified", "update_rollback_notified",
              "fw_notified_gw", "gw_fw_model", "interval_min", "paused"}
STATE_PREFIXES = ("last_", "fw_checked:", "rep_fw:", "fw_notified")
TRACKER_COLS = ("id", "contact", "label", "interval_min", "active_hours", "offline_alert_min",
                "low_battery_v", "paused", "color", "moving_interval_min")


class BackupError(Exception):
    pass


def crypto_available():
    return importlib.util.find_spec("pyzipper") is not None


PROTECTED_TXT = """
PROTETTO DA PASSWORD: i file del backup sono cifrati (AES-256) con la
password scelta in Gestione -> Sistema -> Backup.
Per aprirli:
  Windows   7-Zip (gratuito, www.7-zip.org): tasto destro -> 7-Zip -> Estrai.
            Esplora risorse di Windows NON apre questo tipo di zip.
  Linux     7z x {name}   (pacchetto p7zip-full; il comando unzip non lo apre)
  macOS     Keka oppure 7z da Homebrew.
Se hai dimenticato la password il backup non si può aprire.
"""

RESTORE_TXT = """Backup di MeshWatch del {when} - {kind}

Contiene:
{files}
ATTENZIONE: contiene password{pos}. Conservalo in un posto sicuro.

Il modo più semplice: pagina di MeshWatch -> Gestione -> Sistema -> Ripristina un backup.
"""

FILES_TXT = {
    "meshwatch.db": "  meshwatch.db       dati: dispositivi, posizioni, sensori, ripetitori, statistiche,\n"
                    "                     messaggi, utenti, registro e impostazioni della pagina\n",
    "config.yaml": "  config.yaml        file di configurazione (token Telegram, radio, pagina web)\n",
    "impostazioni.json": "  impostazioni.json  impostazioni della pagina: dispositivi, ripetitori (con password),\n"
                         "                     utenti, regole dei sensori, avvisi, riepilogo, backup, firmware\n",
}

MANUAL_TXT = """

A mano, sul Raspberry (via SSH):
  sudo systemctl stop meshwatch

  SD preparata con il kit (DietPi):
    meshwatch.db -> /var/lib/meshwatch/meshwatch.db
    config.yaml  -> /boot/firmware/meshwatch-config.yaml
                    (a ogni avvio viene ricopiato da qui)
    poi: sudo chown meshwatch-svc: /var/lib/meshwatch/meshwatch.db

  Installazione con install.sh:
    meshwatch.db e config.yaml nella cartella del programma

  sudo systemctl start meshwatch
"""

SETTINGS_TXT = """
impostazioni.json non si copia a mano: si ripristina dalla pagina, scegliendo
"Impostazioni della pagina". Lo storico (posizioni, statistiche, messaggi) resta
quello del Raspberry. Va bene anche su un Raspberry nuovo, dopo il primo avvio.
config.yaml, a mano: sulla SD del kit va in /boot/firmware/meshwatch-config.yaml
(con install.sh nella cartella del programma), poi si riavvia.
"""


def settings(db):
    kind = db.get("backup_kind", "completo")
    return {
        "retention_days": int(db.get("retention_days", DEFAULT_RETENTION_DAYS)),
        "backup_telegram": db.get("backup_telegram", "0") == "1",
        "backup_kind": kind if kind in KINDS else "completo",
        "last_purge": int(db.get("last_purge", 0)) or None,
        "last_purge_deleted": int(db.get("last_purge_deleted", 0)),
        "last_backup": int(db.get("last_backup", 0)) or None,
        "backup_protected": bool(db.get("backup_password")),
    }


def set_backup_password(db, password):
    """"" = backup senza password."""
    if password and len(password) < MIN_BACKUP_PASSWORD:
        raise BackupError(f"la password del backup deve avere almeno {MIN_BACKUP_PASSWORD} caratteri")
    if password and not crypto_available():
        raise BackupError("manca la libreria pyzipper: aggiorna MeshWatch dalla pagina")
    db.set("backup_password", password or "")


def purge(db, days):
    """Cancella i dati più vecchi di `days` giorni. Restano sempre l'ultima posizione di
    ogni dispositivo e l'ultimo stato di ogni ripetitore. Ritorna il numero di righe eliminate."""
    if not days or days <= 0:
        return 0
    cutoff = int(time.time()) - days * 86400
    n = 0
    n += db._exec(
        "DELETE FROM positions WHERE ts < ? AND rowid NOT IN (SELECT (SELECT p2.rowid FROM positions p2 "
        "WHERE p2.tracker_id=t.id ORDER BY p2.ts DESC, p2.rowid DESC LIMIT 1) AS r FROM trackers t "
        "WHERE r IS NOT NULL)",   # un NULL nella lista (dispositivo senza posizioni) bloccherebbe tutto
        (cutoff,)).rowcount
    n += db._exec("DELETE FROM tracker_polls WHERE ts < ?", (cutoff,)).rowcount
    n += db._exec("DELETE FROM sensor_readings WHERE ts < ?", (cutoff,)).rowcount
    n += db._exec(
        "DELETE FROM repeater_status WHERE ts < ? AND rowid NOT IN "
        "(SELECT MAX(rowid) FROM repeater_status GROUP BY name) AND rowid NOT IN "
        "(SELECT MAX(rowid) FROM repeater_status WHERE ok=1 GROUP BY name)", (cutoff,)).rowcount
    n += db._exec("DELETE FROM gateway_stats WHERE ts < ?", (cutoff,)).rowcount
    n += db._exec("DELETE FROM cli_log WHERE ts < ?", (cutoff,)).rowcount
    n += db._exec("DELETE FROM node_adverts WHERE ts < ?", (cutoff,)).rowcount
    n += db._exec("DELETE FROM rx_hourly WHERE hour < ?", (cutoff,)).rowcount
    n += db._exec("DELETE FROM link_history WHERE ts < ?", (cutoff,)).rowcount
    n += db._exec("DELETE FROM audit WHERE ts < ?", (cutoff,)).rowcount
    n += db._exec("DELETE FROM messages WHERE ts < ?", (cutoff,)).rowcount
    db.clear_login_failures()
    if n > 5000:
        with db.lock:
            db.conn.execute("VACUUM")   # restituisce lo spazio sulla SD
    return n


def _is_state(key):
    return key in STATE_KEYS or key.startswith(STATE_PREFIXES)


def export_settings(db):
    """Impostazioni della pagina (senza lo storico) come dizionario da salvare in impostazioni.json."""
    q = lambda sql: db._all(sql)   # noqa: E731
    return {
        "format": SETTINGS_FORMAT, "version": SETTINGS_VERSION, "created": int(time.time()),
        "trackers": [{k: t.get(k) for k in TRACKER_COLS} for t in db.trackers()],
        "sensor_rules": q("SELECT tracker_id, type, channel, op, value FROM sensor_rules ORDER BY id"),
        "repeaters": q("SELECT name, password FROM repeaters ORDER BY name"),
        "users": q("SELECT username, password_hash, role FROM users ORDER BY username"),
        "settings": {r["key"]: r["val"] for r in q("SELECT key, val FROM settings ORDER BY key")
                     if not _is_state(r["key"])},
    }


def parse_settings(raw):
    """Controlla impostazioni.json. Ritorna il dizionario o solleva BackupError."""
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise BackupError("impostazioni.json non è valido") from None
    if not isinstance(data, dict) or data.get("format") != SETTINGS_FORMAT:
        raise BackupError("impostazioni.json non è un file di MeshWatch")
    if int(data.get("version") or 0) > SETTINGS_VERSION:
        raise BackupError("impostazioni.json viene da una versione più recente di MeshWatch: aggiorna prima il programma")
    try:
        for key in ("trackers", "sensor_rules", "repeaters", "users"):
            if not isinstance(data.get(key, []), list) or not all(isinstance(x, dict) for x in data.get(key, [])):
                raise ValueError(key)
        if not isinstance(data.get("settings", {}), dict):
            raise ValueError("settings")
        for t in data.get("trackers", []):
            if not t.get("contact") or not t.get("label"):
                raise ValueError("trackers")
        for u in data.get("users", []):
            if not u.get("username") or not u.get("password_hash") or u.get("role") not in ("admin", "viewer"):
                raise ValueError("users")
        for r in data.get("repeaters", []):
            if not r.get("name"):
                raise ValueError("repeaters")
    except ValueError as e:
        raise BackupError(f"impostazioni.json non è valido ({e})") from None
    if not any(u["role"] == "admin" for u in data.get("users", [])):
        raise BackupError("in impostazioni.json non c'è nessun utente admin: dopo il ripristino non potresti più entrare in Gestione")
    return data


def import_settings(db, data):
    """Rimette le impostazioni della pagina salvate con export_settings, senza toccare lo storico.
    I dispositivi si riconoscono dal nome del contatto: chi c'è già tiene le sue posizioni; chi non è
    nel backup sparisce dalla mappa ma il suo storico resta nel database (con lo stesso id torna con
    "Annulla l'ultimo ripristino")."""
    with db.lock:
        c = db.conn
        try:
            c.execute("BEGIN")
            have = {r["contact"]: r["id"] for r in c.execute("SELECT id, contact FROM trackers")}
            used = set(have.values())
            idmap, keep = {}, set()
            for t in data.get("trackers", []):
                f = {k: t.get(k) for k in TRACKER_COLS if k != "id" and t.get(k) is not None}
                tid = have.get(t["contact"])
                if tid is None:
                    old = t.get("id")
                    if isinstance(old, int) and old not in used:
                        f["id"] = old
                    cols = list(f)
                    tid = c.execute(f"INSERT INTO trackers ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                                    [f[k] for k in cols]).lastrowid
                    used.add(tid)
                else:
                    cols = [k for k in f if k != "contact"]
                    if cols:
                        c.execute(f"UPDATE trackers SET {','.join(k + '=?' for k in cols)} WHERE id=?",
                                  [f[k] for k in cols] + [tid])
                if t.get("id") is not None:
                    idmap[t["id"]] = tid
                keep.add(tid)
            for tid in set(have.values()) - keep:
                c.execute("DELETE FROM trackers WHERE id=?", (tid,))
            c.execute("DELETE FROM sensor_rules")
            for r in data.get("sensor_rules", []):
                if r.get("tracker_id") in idmap:
                    c.execute("INSERT INTO sensor_rules (tracker_id, type, channel, op, value) VALUES (?,?,?,?,?)",
                              (idmap[r["tracker_id"]], r.get("type"), r.get("channel"), r.get("op"), r.get("value")))
            c.execute("DELETE FROM repeaters")
            for r in data.get("repeaters", []):
                c.execute("INSERT OR REPLACE INTO repeaters (name, password) VALUES (?,?)", (r["name"], r.get("password") or ""))
            c.execute("DELETE FROM users")
            for u in data.get("users", []):
                c.execute("INSERT OR REPLACE INTO users (username, password_hash, role) VALUES (?,?,?)",
                          (u["username"], u["password_hash"], u["role"]))
            new = {str(k): str(v) for k, v in (data.get("settings") or {}).items() if not _is_state(str(k))}
            for (key,) in c.execute("SELECT key FROM settings").fetchall():
                if not _is_state(key) and key not in new:
                    c.execute("DELETE FROM settings WHERE key=?", (key,))
            c.executemany("INSERT OR REPLACE INTO settings VALUES (?,?)", list(new.items()))
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise
    return {"trackers": len(keep), "repeaters": len(data.get("repeaters", [])), "users": len(data.get("users", []))}


def make_backup(db, config_path=None, kind="completo"):
    """Ritorna (contenuto zip, nome file). kind: completo, configurazione o dati (vedi in alto).
    Con la password del backup impostata i file sono cifrati AES-256; LEGGIMI.txt resta leggibile
    e spiega come aprirli. Se la password c'è ma manca la libreria solleva BackupError: mai un
    backup in chiaro quando ne è stato chiesto uno protetto."""
    if kind not in KINDS:
        raise BackupError(f"tipo di backup sconosciuto: {kind}")
    when = datetime.now()
    prefix = {"completo": "meshwatch-backup", "configurazione": "meshwatch-configurazione", "dati": "meshwatch-dati"}[kind]
    name = f"{prefix}-{when.strftime('%Y%m%d-%H%M')}.zip"
    password = db.get("backup_password") or ""
    has_cfg = kind != "dati" and config_path and Path(config_path).is_file()
    files = {"completo": ["meshwatch.db", "config.yaml"], "configurazione": ["config.yaml", "impostazioni.json"],
             "dati": ["meshwatch.db"]}[kind]
    files = [f for f in files if f != "config.yaml" or has_cfg]
    readme = RESTORE_TXT.format(when=when.strftime("%d/%m/%Y %H:%M"), kind=KIND_LABEL[kind],
                                files="".join(FILES_TXT[f] for f in files),
                                pos="" if kind == "configurazione" else " e posizioni")
    readme += SETTINGS_TXT if kind == "configurazione" else MANUAL_TXT
    if password:
        try:
            import pyzipper
        except ImportError:
            raise BackupError("backup protetto da password ma manca la libreria pyzipper: "
                              "aggiorna MeshWatch dalla pagina") from None
        readme += PROTECTED_TXT.format(name=name)
    buf = io.BytesIO()
    tmp = None
    try:
        if "meshwatch.db" in files:
            fd, tmp = tempfile.mkstemp(suffix=".db")
            os.close(fd)
            db.backup_to(tmp)   # copia coerente anche mentre il programma scrive
        if password:
            z = pyzipper.AESZipFile(buf, "w", compression=pyzipper.ZIP_DEFLATED)
        else:
            z = zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED)
        with z:
            z.writestr("LEGGIMI.txt", readme)   # prima della password: resta in chiaro
            if password:
                z.setpassword(password.encode())
                z.setencryption(pyzipper.WZ_AES, nbits=256)
            if tmp:
                z.write(tmp, "meshwatch.db")
            if has_cfg:
                z.write(config_path, "config.yaml")
            if "impostazioni.json" in files:
                z.writestr("impostazioni.json", json.dumps(export_settings(db), ensure_ascii=False, indent=1))
    finally:
        if tmp:
            os.unlink(tmp)
    return buf.getvalue(), name


def db_info(db):
    path = db.path
    size = os.path.getsize(path) if path and os.path.exists(path) else None
    first = db._one("SELECT MIN(ts) AS ts FROM positions")
    return {
        "db_size": size,
        "positions": db._one("SELECT COUNT(*) AS n FROM positions")["n"],
        "repeater_rows": db._one("SELECT COUNT(*) AS n FROM repeater_status")["n"],
        "oldest": first["ts"] if first else None,
    }
