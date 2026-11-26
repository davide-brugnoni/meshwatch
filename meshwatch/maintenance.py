"""Manutenzione: pulizia dei dati vecchi e backup (database + config).

Impostazioni salvate nel database (tabella settings), modificabili dalla pagina:
    retention_days    giorni di storico da tenere (0 = per sempre)
    backup_telegram   1 = backup settimanale inviato su Telegram alle chat admin
    backup_password   se impostata, database e config nello zip sono cifrati (AES-256)

La password del backup protegge le copie che escono dal Raspberry (scaricate o mandate su
Telegram). Il Raspberry la conserva nel suo database per i backup automatici: non protegge
chi ha in mano la SD.
"""
import importlib.util
import io
import os
import tempfile
import time
import zipfile
from datetime import datetime
from pathlib import Path

DEFAULT_RETENTION_DAYS = 180
BACKUP_EVERY_DAYS = 7
MIN_BACKUP_PASSWORD = 8


class BackupError(Exception):
    pass


def crypto_available():
    return importlib.util.find_spec("pyzipper") is not None


PROTECTED_TXT = """
PROTETTO DA PASSWORD: meshwatch.db e config.yaml sono cifrati (AES-256) con la
password scelta in Gestione -> Sistema -> Manutenzione e backup.
Per aprirli:
  Windows   7-Zip (gratuito, www.7-zip.org): tasto destro -> 7-Zip -> Estrai.
            Esplora risorse di Windows NON apre questo tipo di zip.
  Linux     7z x {name}   (pacchetto p7zip-full; il comando unzip non lo apre)
  macOS     Keka oppure 7z da Homebrew.
Se hai dimenticato la password il backup non si può aprire.
"""

RESTORE_TXT = """Backup di MeshWatch del {when}

Contiene:
  meshwatch.db   dati (dispositivi, posizioni, sensori, ripetitori, messaggi, utenti, registro)
  config.yaml    configurazione (token Telegram, password dei ripetitori)

ATTENZIONE: contiene password e posizioni. Conservalo in un posto sicuro.

Il modo più semplice: pagina di MeshWatch -> Gestione -> Sistema -> Ripristina un backup.

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


def settings(db):
    return {
        "retention_days": int(db.get("retention_days", DEFAULT_RETENTION_DAYS)),
        "backup_telegram": db.get("backup_telegram", "0") == "1",
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


def make_backup(db, config_path=None):
    """Ritorna (contenuto zip, nome file). Con la password del backup impostata, database e
    config sono cifrati AES-256; LEGGIMI.txt resta leggibile e spiega come aprirli.
    Se la password c'è ma manca la libreria solleva BackupError: mai un backup in chiaro
    quando ne è stato chiesto uno protetto."""
    when = datetime.now()
    name = f"meshwatch-backup-{when.strftime('%Y%m%d-%H%M')}.zip"
    password = db.get("backup_password") or ""
    readme = RESTORE_TXT.format(when=when.strftime("%d/%m/%Y %H:%M"))
    if password:
        try:
            import pyzipper
        except ImportError:
            raise BackupError("backup protetto da password ma manca la libreria pyzipper: "
                              "aggiorna MeshWatch dalla pagina") from None
        readme += PROTECTED_TXT.format(name=name)
    buf = io.BytesIO()
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
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
            z.write(tmp, "meshwatch.db")
            if config_path and Path(config_path).is_file():
                z.write(config_path, "config.yaml")
    finally:
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
