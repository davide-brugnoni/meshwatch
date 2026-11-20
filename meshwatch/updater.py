"""Aggiornamento di MeshWatch dalla pagina web, senza togliere la SD.

Scarica l'ultima versione da GitHub, la controlla, aggiorna le dipendenze se servono,
sostituisce i file e riavvia il servizio (systemd lo fa ripartire da solo).

Due modalità, scelte in automatico:
- "archivio": installazione dal kit SD (/opt/meshwatch). Scarica il .tar.gz del commit,
  sposta la versione attuale in .update/backup e mette al suo posto quella nuova.
- "git": installazione con install.sh da un clone git. Fa fetch + merge --ff-only.

Protezione: dopo l'aggiornamento resta il file .update/pending. tools/guard.sh, eseguito da
systemd prima di ogni avvio, conta gli avvii: se la nuova versione non arriva a confermarsi
(CONFIRM_AFTER secondi senza crash) per 3 volte, ripristina da solo la versione precedente.
"""
import asyncio
import io
import json
import logging
import os
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.request
from pathlib import Path

from . import __version__

log = logging.getLogger("meshwatch.update")

APP_DIR = Path(__file__).resolve().parent.parent
# cosa viene sostituito: config, database e venv restano dove sono
ITEMS = ("meshwatch", "web", "tools", "requirements.txt", "config.example.yaml", "README.md", "LICENSE")
REQUIRED = ("meshwatch/__main__.py", "web/index.html", "requirements.txt")
CONFIRM_AFTER = 90
UA = {"User-Agent": "meshwatch-updater", "Accept": "application/vnd.github+json"}

restart_event = asyncio.Event()   # __main__ lo aspetta: quando scatta chiude tutto ed esce


class UpdateError(Exception):
    pass


def _http(url, timeout=30):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _run(cmd, timeout=1200, cwd=None):
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        lines = (p.stderr or p.stdout).strip().splitlines()
        raise UpdateError(lines[-1] if lines else f"{cmd[0]}: comando fallito")
    return p.stdout.strip()


def safe_extract(data, dest):
    """Estrae il tar.gz di GitHub (cartella iniziale "repo-sha/") in dest, solo i file in ITEMS.
    Rifiuta percorsi assoluti o con "..", ignora link e file speciali."""
    dest = Path(dest)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for m in tar.getmembers():
            parts = Path(m.name).parts
            if len(parts) < 2 or m.name.startswith("/") or ".." in parts:
                continue
            rel = Path(*parts[1:])
            if rel.parts[0] not in ITEMS or "__pycache__" in rel.parts:
                continue
            target = dest / rel
            if m.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif m.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                with tar.extractfile(m) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out)
                if m.mode & 0o100:
                    target.chmod(0o755)


def validate(new_dir):
    new_dir = Path(new_dir)
    missing = [f for f in REQUIRED if not (new_dir / f).is_file()]
    if missing:
        raise UpdateError("pacchetto incompleto: manca " + ", ".join(missing))
    for py in (new_dir / "meshwatch").rglob("*.py"):
        try:
            compile(py.read_bytes(), str(py), "exec")
        except (SyntaxError, ValueError) as e:
            raise UpdateError(f"errore di sintassi in {py.name}: {e}")


def swap_in(app_dir, new_dir, backup_dir):
    """Sposta i file attuali in backup_dir e quelli nuovi al loro posto. In caso di errore rimette tutto."""
    app_dir, new_dir, backup_dir = Path(app_dir), Path(new_dir), Path(backup_dir)
    shutil.rmtree(backup_dir, ignore_errors=True)
    backup_dir.mkdir(parents=True)
    moved, added = [], []
    try:
        for name in ITEMS + ("VERSION.json",):
            cur = app_dir / name
            if cur.exists():
                os.replace(cur, backup_dir / name)
                moved.append(name)
            elif (new_dir / name).exists():
                added.append(name)
            if (new_dir / name).exists():
                os.replace(new_dir / name, cur)
        # file che prima non c'erano: guard.sh li toglie se ripristina
        (backup_dir / ".added").write_text("".join(n + "\n" for n in added))
    except OSError:
        for name in ITEMS + ("VERSION.json",):
            if name in moved or name in added:
                if (app_dir / name).is_dir():
                    shutil.rmtree(app_dir / name)
                elif (app_dir / name).exists():
                    (app_dir / name).unlink()
                if name in moved:
                    os.replace(backup_dir / name, app_dir / name)
        raise


class Updater:
    def __init__(self, cfg=None, app_dir=APP_DIR):
        cfg = cfg or {}
        self.repo = cfg.get("repo", "davide-brugnoni/meshwatch")
        self.branch = cfg.get("branch", "main")
        self.app = Path(app_dir)
        self.udir = self.app / ".update"
        self.git = (self.app / ".git").exists()
        self.latest = None
        self.checked_at = None
        self.job = {"state": "idle", "message": "", "ts": None}
        self._lock = asyncio.Lock()

    # ---------- stato
    def installed(self):
        if self.git:
            try:
                sha = _run(["git", "-C", str(self.app), "rev-parse", "HEAD"], timeout=10)
                date = _run(["git", "-C", str(self.app), "log", "-1", "--format=%cI"], timeout=10)
                return {"sha": sha, "date": date, "source": "git"}
            except (UpdateError, OSError, subprocess.SubprocessError):
                return {"sha": None, "source": "git"}
        try:
            return {**json.loads((self.app / "VERSION.json").read_text()), "source": "github"}
        except (OSError, ValueError):
            return {"sha": None, "source": "sd"}   # installata dal kit SD: commit sconosciuto

    def problems(self):
        """Motivi per cui l'aggiornamento dalla pagina non è possibile (lista vuota = si può)."""
        p = []
        if not os.environ.get("INVOCATION_ID"):
            p.append("MeshWatch non gira come servizio systemd: non potrebbe ripartire da solo")
        if not os.access(self.app, os.W_OK) or not os.access(self.app / "meshwatch", os.W_OK):
            p.append("la cartella del programma non è scrivibile dal servizio")
        elif not (self.app / "tools" / "guard.sh").exists():
            p.append("manca la protezione di ripristino (tools/guard.sh)")
        return p

    def status(self):
        inst = self.installed()
        rolled = self.udir / "rolled_back"
        out = {
            "version": __version__, "installed": inst, "mode": "git" if self.git else "archivio",
            "repo": self.repo, "branch": self.branch, "problems": self.problems(),
            "latest": self.latest, "checked_at": self.checked_at, "job": self.job,
            "pending": (self.udir / "pending").exists(),
            "rolled_back": rolled.read_text().strip() if rolled.exists() else None,
        }
        out["available"] = bool(self.latest and self.latest["sha"] != inst.get("sha"))
        return out

    # ---------- controllo
    async def check(self):
        def get():
            j = json.loads(_http(f"https://api.github.com/repos/{self.repo}/commits/{self.branch}"))
            c = j["commit"]
            return {"sha": j["sha"], "date": c["committer"]["date"],
                    "message": c["message"].splitlines()[0][:200]}
        try:
            self.latest = await asyncio.to_thread(get)
        except Exception as e:
            raise UpdateError(f"GitHub non raggiungibile: {e}")
        self.checked_at = int(time.time())
        return self.status()

    # ---------- installazione
    def _set(self, state, message=""):
        self.job = {"state": state, "message": message, "ts": int(time.time())}
        log.info("Aggiornamento: %s %s", state, message)

    async def start(self):
        if self._lock.locked():
            raise UpdateError("aggiornamento già in corso")
        probs = self.problems()
        if probs:
            raise UpdateError(probs[0])
        if not self.latest:
            await self.check()
        if not self.status()["available"]:
            raise UpdateError("hai già l'ultima versione")
        asyncio.create_task(self._job(dict(self.latest)))

    async def _job(self, target):
        async with self._lock:
            try:
                await asyncio.to_thread(self._apply_git if self.git else self._apply_archive, target)
            except Exception as e:
                self._set("error", str(e))
                return
            self._set("restarting", "riavvio del servizio")
            await asyncio.sleep(1.5)        # la pagina fa in tempo a leggere lo stato
            restart_event.set()

    def _pip(self, req_file):
        self._set("deps", "aggiornamento librerie (può richiedere alcuni minuti)")
        _run([sys.executable, "-m", "pip", "install", "--no-cache-dir", "-q", "-r", str(req_file)], timeout=1800)

    def _mark_pending(self, extra):
        self.udir.mkdir(exist_ok=True)
        (self.udir / "tries").unlink(missing_ok=True)
        (self.udir / "rolled_back").unlink(missing_ok=True)
        for k, v in extra.items():
            (self.udir / k).write_text(v)
        (self.udir / "pending").write_text(json.dumps({"ts": int(time.time())}))

    def _apply_archive(self, target):
        self._set("download", "download da GitHub")
        data = _http(f"https://codeload.github.com/{self.repo}/tar.gz/{target['sha']}", timeout=120)
        self.udir.mkdir(exist_ok=True)
        new = self.udir / "new"
        shutil.rmtree(new, ignore_errors=True)
        new.mkdir()
        self._set("check", "controllo dei file")
        safe_extract(data, new)
        validate(new)
        (new / "VERSION.json").write_text(json.dumps(
            {"sha": target["sha"], "date": target["date"], "message": target["message"], "installed": int(time.time())}))
        old_req = self.app / "requirements.txt"
        if not old_req.exists() or old_req.read_bytes() != (new / "requirements.txt").read_bytes():
            self._pip(new / "requirements.txt")
        self._set("install", "installazione")
        (self.udir / "prev_git").unlink(missing_ok=True)
        swap_in(self.app, new, self.udir / "backup")
        shutil.rmtree(new, ignore_errors=True)
        self._mark_pending({})

    def _apply_git(self, target):
        g = ["git", "-C", str(self.app)]
        self._set("download", "git fetch")
        prev = _run(g + ["rev-parse", "HEAD"], timeout=10)
        old_req = (self.app / "requirements.txt").read_bytes()
        _run(g + ["fetch", "-q", "origin", self.branch], timeout=300)
        self._set("install", "git merge")
        _run(g + ["merge", "--ff-only", "-q", "FETCH_HEAD"], timeout=60)
        try:
            validate(self.app)
            if (self.app / "requirements.txt").read_bytes() != old_req:
                self._pip(self.app / "requirements.txt")
        except Exception:
            _run(g + ["reset", "-q", "--hard", prev], timeout=60)
            raise
        self._mark_pending({"prev_git": prev})

    # ---------- dopo il riavvio
    async def confirm_later(self):
        """Se l'ultimo aggiornamento è in prova e il programma resta su, lo conferma."""
        if not (self.udir / "pending").exists():
            return
        await asyncio.sleep(CONFIRM_AFTER)
        for f in ("pending", "tries"):
            (self.udir / f).unlink(missing_ok=True)
        log.info("Aggiornamento confermato: la nuova versione funziona")
