"""Ripristino di un backup dalla pagina (Gestione → Sistema → Ripristina un backup).

1. check: lo zip caricato viene controllato senza cambiare niente (password, database integro e
   apribile da questa versione, config valido) e la pagina mostra cosa contiene.
2. apply: si salva lo stato attuale (per poter annullare), si preparano i file e il servizio
   si riavvia (systemd lo fa ripartire, come dopo un aggiornamento).
3. all'avvio, prima di aprire il database, apply_pending() mette i file al loro posto.

Configurazione:
- installazione con install.sh: config.yaml nella cartella del programma, scritto direttamente;
- kit SD: a ogni avvio /opt/meshwatch-prestart.sh (root) ricopia il config dalla SD. Il config
  ripristinato va lasciato in .restore/config-sd.yaml: lo script lo copia sulla SD prima
  (tools/configura-servizio.sh; le SD preparate prima vanno riconfigurate una volta).

Un backup può contenere meshwatch.db (dati), config.yaml (file di configurazione) e
impostazioni.json (impostazioni della pagina, dal backup della configurazione): ognuno si sceglie a
parte. impostazioni.json all'avvio si applica al database senza toccare lo storico.

Tutto sta in .restore/ accanto al database:
    upload/<token>/   backup caricato e controllato, in attesa di conferma (1 ora)
    pending/          da applicare al prossimo avvio
    prev/             stato prima dell'ultimo ripristino (per "Annulla")
    config-sd.yaml    config per la SD del kit (lo copia lo script di avvio)
    status.json       esito dell'ultimo ripristino
"""
import json
import logging
import os
import re
import secrets
import shutil
import sqlite3
import tempfile
import time
import zipfile
import zlib
from datetime import datetime
from pathlib import Path

import yaml

log = logging.getLogger("meshwatch.restore")

MAX_UPLOAD = 512 * 2**20          # zip caricato
MAX_MEMBER = 2 * 2**30            # file dentro lo zip (database grande)
MIN_FREE = 300 * 2**20            # spazio da lasciare libero sulla SD
UPLOAD_TTL = 3600
SD_DATA = Path("/var/lib/meshwatch")
SD_PRESTART = Path("/opt/meshwatch-prestart.sh")
SD_MARK = "config-sd.yaml"        # lo script di avvio del kit sa copiare il config ripristinato
SD_RESTORE = SD_DATA / ".restore" / SD_MARK   # dove lo script di avvio lo cerca
NEED_TABLES = {"trackers", "users", "settings", "positions"}
MEMBERS = ("meshwatch.db", "config.yaml", "impostazioni.json")
MAX_SETTINGS = 20 * 2**20


class RestoreError(Exception):
    def __init__(self, msg, code="invalid"):
        super().__init__(msg)
        self.code = code   # "password": serve (o è sbagliata) la password del backup


def db_path(cfg):
    return Path(cfg.get("database", "meshwatch.db")).resolve()


def base_dir(cfg):
    return db_path(cfg).parent / ".restore"


def config_target(cfg):
    """("direct" | "sd" | None, motivo se None): come si può ripristinare il config."""
    p = cfg.get("_config_path")
    if not p:
        return None, "percorso del config sconosciuto"
    p = Path(p)
    if p == SD_DATA / "config.yaml" and SD_PRESTART.exists():
        try:
            ready = SD_MARK in SD_PRESTART.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            ready = False
        if ready:
            return "sd", None
        return None, ("sulla SD del kit il config si ripristina dopo un'abilitazione una tantum, via SSH: "
                      "sudo bash /opt/meshwatch/tools/configura-servizio.sh")
    if p.is_file() and os.access(p, os.W_OK) and os.access(p.parent, os.W_OK):
        return "direct", None
    return None, f"il programma non può scrivere {p}"


# ---------------------------------------------------------------- controllo dello zip
def _bad_zip_errors():
    """Errori di zip rotto: pyzipper ha i suoi, diversi da quelli di zipfile."""
    errs = [zipfile.BadZipFile, zlib.error, EOFError]
    try:
        import pyzipper
        errs.append(pyzipper.BadZipFile)
    except (ImportError, AttributeError):
        pass
    return tuple(errs)


def _open_zip(path, password):
    try:
        import pyzipper
        opener = pyzipper.AESZipFile
    except ImportError:
        pyzipper, opener = None, zipfile.ZipFile
    try:
        z = opener(path)
    except _bad_zip_errors() + (OSError, ValueError):
        raise RestoreError("il file non è uno zip valido") from None
    names = {i.filename: i for i in z.infolist()}
    if not any(n in names for n in MEMBERS):
        z.close()
        raise RestoreError("non è un backup di MeshWatch: dentro non ci sono meshwatch.db, config.yaml né impostazioni.json")
    encrypted = any(names[n].flag_bits & 1 for n in MEMBERS if n in names)
    if encrypted:
        if pyzipper is None:
            z.close()
            raise RestoreError("il backup è protetto da password ma manca la libreria pyzipper: aggiorna MeshWatch")
        if not password:
            z.close()
            raise RestoreError("il backup è protetto da password: scrivila nel campo Password", "password")
        z.setpassword(password.encode())
    return z, names, encrypted


def _extract(z, info, dest):
    if info.file_size > MAX_MEMBER:
        raise RestoreError(f"{info.filename} è troppo grande")
    if shutil.disk_usage(dest.parent).free < info.file_size + MIN_FREE:
        raise RestoreError("spazio insufficiente sulla SD per controllare il backup")
    n = 0
    try:
        with z.open(info) as src, open(dest, "wb") as out:
            while True:
                chunk = src.read(1 << 20)
                if not chunk:
                    break
                n += len(chunk)
                if n > MAX_MEMBER:
                    raise RestoreError(f"{info.filename} è troppo grande")
                out.write(chunk)
    except RuntimeError as e:   # password sbagliata (zipfile e pyzipper)
        if "password" in str(e).lower():
            raise RestoreError("password del backup sbagliata", "password") from None
        raise RestoreError(f"zip non leggibile: {e}") from None
    except _bad_zip_errors():
        raise RestoreError("lo zip è danneggiato") from None


def _when(readme):
    m = re.search(r"Backup di MeshWatch del (\d{2}/\d{2}/\d{4} \d{2}:\d{2})", readme or "")
    if not m:
        return None
    try:
        return int(datetime.strptime(m.group(1), "%d/%m/%Y %H:%M").timestamp())
    except ValueError:
        return None


def _inspect_db(path):
    try:
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            ok = c.execute("PRAGMA integrity_check").fetchone()[0]
            tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if ok != "ok":
                raise RestoreError("il database del backup è danneggiato")
            if not NEED_TABLES <= tables:
                raise RestoreError("meshwatch.db non è un database di MeshWatch")
            one = lambda sql: c.execute(sql).fetchone()   # noqa: E731
            info = {
                "trackers": [r[0] for r in c.execute("SELECT label FROM trackers ORDER BY id")],
                "repeaters": [r[0] for r in c.execute("SELECT name FROM repeaters ORDER BY name")] if "repeaters" in tables else [],
                "users": [{"username": r[0], "role": r[1]} for r in c.execute("SELECT username, role FROM users ORDER BY username")],
                "positions": one("SELECT COUNT(*) FROM positions")[0],
                "first_ts": one("SELECT MIN(ts) FROM positions")[0],
                "last_ts": one("SELECT MAX(ts) FROM positions")[0],
                "messages": one("SELECT COUNT(*) FROM messages")[0] if "messages" in tables else 0,
                "size": os.path.getsize(path),
            }
        finally:
            c.close()
    except sqlite3.DatabaseError:
        raise RestoreError("meshwatch.db non è un database valido") from None
    if not any(u["role"] == "admin" for u in info["users"]):
        raise RestoreError("nel backup non c'è nessun utente admin: dopo il ripristino non potresti più entrare in Gestione")
    # prova ad aprirlo con questa versione (aggiorna le tabelle dei backup vecchi) su una copia
    from .db import DB
    fd, tmp = tempfile.mkstemp(suffix=".db", dir=path.parent)
    os.close(fd)
    try:
        shutil.copyfile(path, tmp)
        d = DB(tmp)
        d.conn.close()
    except Exception as e:
        raise RestoreError(f"questa versione di MeshWatch non riesce ad aprire il database del backup: {e}") from None
    finally:
        for suf in ("", "-wal", "-shm", "-journal"):
            Path(tmp + suf).unlink(missing_ok=True)
    return info


def _inspect_settings(path):
    from . import maintenance
    if path.stat().st_size > MAX_SETTINGS:
        raise RestoreError("impostazioni.json è troppo grande")
    try:
        data = maintenance.parse_settings(path.read_bytes())
    except maintenance.BackupError as e:
        raise RestoreError(str(e)) from None
    return {"trackers": [t["label"] for t in data.get("trackers", [])],
            "repeaters": [r["name"] for r in data.get("repeaters", [])],
            "users": [{"username": u["username"], "role": u["role"]} for u in data.get("users", [])],
            "sensor_rules": len(data.get("sensor_rules", [])),
            "settings": len(data.get("settings", {}))}


def _inspect_cfg(path, cfg):
    try:
        new = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except Exception:
        raise RestoreError("config.yaml del backup non è valido") from None
    if not isinstance(new, dict):
        raise RestoreError("config.yaml del backup non è valido")
    tg_new, tg_old = new.get("telegram") or {}, cfg.get("telegram") or {}
    token = str(tg_new.get("token") or "")
    info = {"telegram": bool(token and "BotFather" not in token),
            "chats": len(tg_new.get("allowed_chat_ids") or []),
            "radio": str(new.get("wifi_host") if new.get("connection") == "wifi" else new.get("serial_port", "auto")),
            "web_mode": (new.get("web") or {}).get("mode", "lan")}
    warn = []
    if token != str(tg_old.get("token") or ""):
        warn.append("il bot Telegram del backup è diverso da quello attuale (token diverso)")
    w_new, w_old = new.get("web") or {}, cfg.get("web") or {}
    for key, default, what in (("mode", "lan", "modalità"), ("host", "0.0.0.0", "indirizzo"), ("port", 8080, "porta")):
        a, b = str(w_old.get(key, default)), str(w_new.get(key, default))
        if a != b:
            warn.append(f"pagina web: {what} {a} → {b}: dopo il riavvio la pagina potrebbe aprirsi a un altro indirizzo")
    if str(new.get("database", "meshwatch.db")) != str(cfg.get("database", "meshwatch.db")):
        warn.append(f"il config indica un altro database ({new.get('database')}): i dati ripristinati non verrebbero usati")
    old_radio = str(cfg.get("wifi_host") if cfg.get("connection") == "wifi" else cfg.get("serial_port", "auto"))
    if info["radio"] != old_radio:
        warn.append(f"radio: {old_radio} → {info['radio']}")
    return info, warn


def _cleanup_uploads(root):
    """Un caricamento alla volta: il nuovo sostituisce quello non confermato."""
    shutil.rmtree(root / "upload", ignore_errors=True)


def check(zip_path, password, db, cfg):
    """Controlla il backup caricato. Ritorna il riassunto per la pagina con un token per applicarlo."""
    root = base_dir(cfg)
    _cleanup_uploads(root)
    token = secrets.token_hex(12)
    dest = root / "upload" / token
    dest.mkdir(parents=True, exist_ok=True)
    try:
        z, names, encrypted = _open_zip(zip_path, password)
        with z:
            readme = ""
            if "LEGGIMI.txt" in names and names["LEGGIMI.txt"].file_size < 100_000 and not names["LEGGIMI.txt"].flag_bits & 1:
                readme = z.read("LEGGIMI.txt").decode("utf-8", "replace")
            for n in MEMBERS:
                if n in names:
                    _extract(z, names[n], dest / n)
        out = {"token": token, "encrypted": encrypted, "backup_ts": _when(readme),
               "has_data": (dest / "meshwatch.db").exists(), "has_config": (dest / "config.yaml").exists(),
               "has_settings": (dest / "impostazioni.json").exists(), "warnings": []}
        out["kind"] = ("configurazione" if out["has_settings"] and not out["has_data"]
                       else "completo" if out["has_data"] and out["has_config"]
                       else "dati" if out["has_data"] else "configurazione")
        if out["has_data"]:
            out["data"] = info = _inspect_db(dest / "meshwatch.db")
            cur = db._one("SELECT COUNT(*) AS n, MAX(ts) AS t FROM positions")
            later = db._one("SELECT COUNT(*) AS n FROM positions WHERE ts > ?", (info["last_ts"] or 0,))["n"]
            if later:
                out["warnings"].append(f"{later} posizioni registrate dopo il backup andranno perse "
                                       "(lo stato attuale resta salvato: puoi annullare il ripristino)")
            out["current"] = {"positions": cur["n"], "last_ts": cur["t"],
                              "trackers": [t["label"] for t in db.trackers()]}
        if out["has_settings"]:
            out["settings"] = st = _inspect_settings(dest / "impostazioni.json")
            now = [t["label"] for t in db.trackers()]
            gone = [t for t in now if t not in st["trackers"]]
            if gone:
                out["warnings"].append("dispositivi che non sono nel backup e spariranno dalla mappa (il loro storico "
                                       "resta nel database): " + ", ".join(gone))
            out.setdefault("current", {})["trackers"] = now
        if out["has_config"]:
            out["config"], w = _inspect_cfg(dest / "config.yaml", cfg)
            out["config_warnings"] = w
        mode, reason = config_target(cfg)
        out["config_mode"], out["config_reason"] = mode, reason
        (dest / "meta.json").write_text(json.dumps({"ts": int(time.time()), "backup_ts": out["backup_ts"]}))
        return out
    except Exception:
        shutil.rmtree(dest, ignore_errors=True)
        raise


# ---------------------------------------------------------------- applicazione
def _write_meta(path, meta):
    path.write_text(json.dumps(meta, ensure_ascii=False))


def _read_json(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def apply(token, data, config, db, cfg, user="", settings=False):
    """Salva lo stato attuale in prev/ e prepara i file in pending/: si applicano al riavvio.
    data = meshwatch.db, config = config.yaml, settings = impostazioni.json."""
    root = base_dir(cfg)
    if not re.fullmatch(r"[0-9a-f]{24}", token or ""):
        raise RestoreError("backup da ripristinare non trovato: caricalo di nuovo")
    up = root / "upload" / token
    meta = _read_json(up / "meta.json")
    if not meta or time.time() - meta["ts"] > UPLOAD_TTL:
        shutil.rmtree(up, ignore_errors=True)
        raise RestoreError("backup scaduto o non trovato: caricalo di nuovo")
    if not (data or config or settings):
        raise RestoreError("scegli cosa ripristinare: dati, impostazioni della pagina o file di configurazione")
    if data and not (up / "meshwatch.db").exists():
        raise RestoreError("il backup non contiene i dati")
    if settings and not (up / "impostazioni.json").exists():
        raise RestoreError("il backup non contiene le impostazioni della pagina")
    if data and settings:   # il database del backup ha già le sue impostazioni
        settings = False
    mode = None
    if config:
        if not (up / "config.yaml").exists():
            raise RestoreError("il backup non contiene la configurazione")
        mode, reason = config_target(cfg)
        if not mode:
            raise RestoreError("la configurazione non si può ripristinare: " + reason)
    return _stage(root, up, data, config, mode, db, cfg,
                  {"user": user, "source": "backup", "backup_ts": meta.get("backup_ts")}, settings=settings)


def undo(db, cfg, user=""):
    """Rimette lo stato salvato prima dell'ultimo ripristino."""
    root = base_dir(cfg)
    prev = root / "prev"
    pm = _read_json(prev / "meta.json")
    if not pm:
        raise RestoreError("non c'è un ripristino da annullare")
    data, config = (prev / "meshwatch.db").exists(), (prev / "config.yaml").exists()
    settings = (prev / "impostazioni.json").exists()
    mode = None
    if config:
        mode, reason = config_target(cfg)
        if not mode:
            raise RestoreError("la configurazione non si può rimettere: " + reason)
    src = root / "undo-src"
    shutil.rmtree(src, ignore_errors=True)
    prev.rename(src)   # prev diventa la sorgente: dopo l'annullamento non c'è un altro "annulla"
    try:
        return _stage(root, src, data, config, mode, db, cfg, {"user": user, "source": "annullamento",
                                                               "backup_ts": pm.get("ts")},
                      save_prev=False, settings=settings)
    finally:
        shutil.rmtree(src, ignore_errors=True)


def _stage(root, src, data, config, mode, db, cfg, meta, save_prev=True, settings=False):
    need = (src / "meshwatch.db").stat().st_size if data else 0
    if shutil.disk_usage(root).free < 2 * need + MIN_FREE:
        raise RestoreError("spazio insufficiente sulla SD per il ripristino")
    pending = root / "pending"
    shutil.rmtree(pending, ignore_errors=True)
    if save_prev:
        prev = root / "prev"
        shutil.rmtree(prev, ignore_errors=True)
        prev.mkdir(parents=True)
        if data:
            db.backup_to(str(prev / "meshwatch.db"))   # copia coerente del database attuale
        if config:
            shutil.copyfile(cfg["_config_path"], prev / "config.yaml")
        if settings:   # le impostazioni attuali, per poterle rimettere
            from . import maintenance
            (prev / "impostazioni.json").write_text(json.dumps(maintenance.export_settings(db), ensure_ascii=False))
        _write_meta(prev / "meta.json", {"ts": int(time.time()), "data": data, "config": config, "settings": settings})
    pending.mkdir(parents=True)
    if data:
        shutil.move(str(src / "meshwatch.db"), pending / "meshwatch.db")
    if settings:
        shutil.copyfile(src / "impostazioni.json", pending / "impostazioni.json")
    if config:
        if mode == "sd":   # lo copia sulla SD lo script di avvio (root), prima di ricopiarlo qui
            SD_RESTORE.parent.mkdir(parents=True, exist_ok=True)
            tmp = SD_RESTORE.with_name(SD_MARK + ".tmp")
            shutil.copyfile(src / "config.yaml", tmp)
            os.chmod(tmp, 0o600)
            os.replace(tmp, SD_RESTORE)
        else:
            shutil.copyfile(src / "config.yaml", pending / "config.yaml")
    _write_meta(pending / "meta.json", {**meta, "ts": int(time.time()), "data": data, "config": config,
                                        "settings": settings, "mode": mode})
    shutil.rmtree(root / "upload", ignore_errors=True)
    out = {"ok": True, "data": data, "config": config}
    if settings:
        out["settings"] = True
    return out


def apply_pending(cfg):
    """All'avvio, prima di aprire il database: mette al loro posto i file preparati.
    Ritorna l'esito (dict) o None se non c'era niente da fare."""
    root = base_dir(cfg)
    pending = root / "pending"
    meta = _read_json(pending / "meta.json")
    if not meta:
        return None
    res = {**meta, "applied": int(time.time()), "ok": True, "errors": [], "announced": False}
    try:
        if meta.get("data") and (pending / "meshwatch.db").exists():
            dbp = db_path(cfg)
            mode = dbp.stat().st_mode & 0o777 if dbp.exists() else 0o600
            for suf in ("-wal", "-shm", "-journal"):
                Path(str(dbp) + suf).unlink(missing_ok=True)
            os.chmod(pending / "meshwatch.db", mode)
            os.replace(pending / "meshwatch.db", dbp)
        if meta.get("settings") and (pending / "impostazioni.json").exists():
            from . import maintenance
            from .db import DB
            d = DB(str(db_path(cfg)))
            try:
                maintenance.import_settings(d, maintenance.parse_settings((pending / "impostazioni.json").read_bytes()))
            finally:
                d.conn.close()
        if meta.get("config"):
            if meta.get("mode") == "sd":
                if SD_RESTORE.exists():   # lo script di avvio non l'ha copiato sulla SD
                    res["ok"] = False
                    res["errors"].append("configurazione non ripristinata: lo script di avvio della SD non è aggiornato "
                                         "(sudo bash /opt/meshwatch/tools/configura-servizio.sh)")
                    SD_RESTORE.unlink(missing_ok=True)
            elif (pending / "config.yaml").exists():
                cp = Path(cfg["_config_path"])
                mode = cp.stat().st_mode & 0o777 if cp.exists() else 0o600
                tmp = cp.with_name(cp.name + ".ripristino")
                shutil.copyfile(pending / "config.yaml", tmp)
                os.chmod(tmp, mode)
                os.replace(tmp, cp)
    except Exception as e:
        log.exception("Ripristino non riuscito: %s", e)
        res["ok"] = False
        res["errors"].append(str(e))
    shutil.rmtree(pending, ignore_errors=True)
    _write_meta(root / "status.json", res)
    log.info("Ripristino applicato: %s", res)
    return res


def status(cfg):
    root = base_dir(cfg)
    st = _read_json(root / "status.json")
    prev = _read_json(root / "prev" / "meta.json")
    mode, reason = config_target(cfg)
    return {"last": st, "prev": prev, "config_mode": mode, "config_reason": reason,
            "pending": (root / "pending" / "meta.json").exists()}


def take_announcement(cfg):
    """Esito dell'ultimo ripristino da segnalare (registro attività, Telegram) una volta sola."""
    root = base_dir(cfg)
    st = _read_json(root / "status.json")
    if not st or st.get("announced"):
        return None
    st["announced"] = True
    _write_meta(root / "status.json", st)
    return st
