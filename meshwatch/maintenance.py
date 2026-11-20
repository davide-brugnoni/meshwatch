"""Manutenzione: pulizia dei dati vecchi e backup (database + config).

Impostazioni salvate nel database (tabella settings), modificabili dalla pagina:
    retention_days    giorni di storico da tenere (0 = per sempre)
    backup_telegram   1 = backup settimanale inviato su Telegram alle chat admin
"""
import io
import os
import tempfile
import time
import zipfile
from datetime import datetime
from pathlib import Path

DEFAULT_RETENTION_DAYS = 180
BACKUP_EVERY_DAYS = 7

RESTORE_TXT = """Backup di MeshWatch del {when}

Contiene:
  meshwatch.db   dati (dispositivi, posizioni, ripetitori, messaggi, utenti, registro)
  config.yaml    configurazione (token Telegram, password dei ripetitori)

ATTENZIONE: contiene password e posizioni. Conservalo in un posto sicuro.

Per ripristinarlo sul Raspberry (via SSH):
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
    }


def purge(db, days):
    """Cancella i dati più vecchi di `days` giorni. Restano sempre l'ultima posizione di
    ogni dispositivo e l'ultimo stato di ogni ripetitore. Ritorna il numero di righe eliminate."""
    if not days or days <= 0:
        return 0
    cutoff = int(time.time()) - days * 86400
    n = 0
    n += db._exec(
        "DELETE FROM positions WHERE ts < ? AND rowid NOT IN (SELECT (SELECT p2.rowid FROM positions p2 "
        "WHERE p2.tracker_id=t.id ORDER BY p2.ts DESC, p2.rowid DESC LIMIT 1) FROM trackers t)",
        (cutoff,)).rowcount
    n += db._exec("DELETE FROM tracker_polls WHERE ts < ?", (cutoff,)).rowcount
    n += db._exec(
        "DELETE FROM repeater_status WHERE ts < ? AND rowid NOT IN "
        "(SELECT MAX(rowid) FROM repeater_status GROUP BY name) AND rowid NOT IN "
        "(SELECT MAX(rowid) FROM repeater_status WHERE ok=1 GROUP BY name)", (cutoff,)).rowcount
    n += db._exec("DELETE FROM cli_log WHERE ts < ?", (cutoff,)).rowcount
    n += db._exec("DELETE FROM audit WHERE ts < ?", (cutoff,)).rowcount
    n += db._exec("DELETE FROM messages WHERE ts < ?", (cutoff,)).rowcount
    db.clear_login_failures()
    if n > 5000:
        with db.lock:
            db.conn.execute("VACUUM")   # restituisce lo spazio sulla SD
    return n


def make_backup(db, config_path=None):
    """Ritorna (contenuto zip, nome file)."""
    when = datetime.now()
    buf = io.BytesIO()
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        db.backup_to(tmp)   # copia coerente anche mentre il programma scrive
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(tmp, "meshwatch.db")
            if config_path and Path(config_path).is_file():
                z.write(config_path, "config.yaml")
            z.writestr("LEGGIMI.txt", RESTORE_TXT.format(when=when.strftime("%d/%m/%Y %H:%M")))
    finally:
        os.unlink(tmp)
    return buf.getvalue(), f"meshwatch-backup-{when.strftime('%Y%m%d-%H%M')}.zip"


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
