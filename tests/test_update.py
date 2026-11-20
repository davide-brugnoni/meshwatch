"""Test dell'aggiornamento dalla pagina: estrazione sicura, controllo, sostituzione, ripristino.
Esecuzione:  python tests/test_update.py
"""
import asyncio
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from meshwatch import updater  # noqa: E402
from meshwatch.updater import UpdateError, Updater, safe_extract, validate  # noqa: E402


def check(cond, msg):
    print(("OK   " if cond else "FAIL ") + msg)
    if not cond:
        sys.exit(1)


def make_tar(files, top="meshwatch-abc123"):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(f"{top}/{name}" if not name.startswith(("/", "..")) else name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def fake_app(d, version="old"):
    d = Path(d)
    (d / "meshwatch").mkdir(parents=True)
    (d / "meshwatch" / "__main__.py").write_text(f"V = '{version}'\n")
    (d / "web").mkdir()
    (d / "web" / "index.html").write_text(version)
    (d / "requirements.txt").write_text("fastapi\n")
    shutil.copytree(ROOT / "tools", d / "tools")
    (d / "config.yaml").write_text("segreto: 1\n")
    (d / "meshwatch.db").write_text("dati")


def main():
    os.environ["INVOCATION_ID"] = "test"
    tmp = Path(tempfile.mkdtemp())

    # estrazione: niente percorsi fuori dalla cartella, solo i file del programma
    data = make_tar({"meshwatch/__main__.py": b"x=1\n", "../evil": b"!", "/etc/evil": b"!",
                     "sd-kit/PREPARA-SD.bat": b"", "config.yaml": b"no"})
    out = tmp / "x"
    out.mkdir()
    safe_extract(data, out)
    check((out / "meshwatch/__main__.py").exists() and not (tmp / "evil").exists()
          and not (out / "config.yaml").exists() and not (out / "sd-kit").exists(), "estrazione sicura e filtrata")

    # controllo: file mancanti ed errori di sintassi bloccano l'aggiornamento
    try:
        validate(out)
        check(False, "pacchetto incompleto rifiutato")
    except UpdateError:
        check(True, "pacchetto incompleto rifiutato")
    (out / "web").mkdir()
    (out / "web/index.html").write_text("")
    (out / "requirements.txt").write_text("")
    (out / "meshwatch/rotto.py").write_text("def x(:\n")
    try:
        validate(out)
        check(False, "errore di sintassi rifiutato")
    except UpdateError as e:
        check("rotto.py" in str(e), "errore di sintassi rifiutato")

    # aggiornamento completo in modalità archivio
    app = tmp / "app"
    fake_app(app)
    up = Updater({}, app_dir=app)
    check(up.status()["installed"]["source"] == "sd" and not up.status()["problems"], "stato iniziale (kit SD)")
    new_files = {"meshwatch/__main__.py": b"V = 'new'\n", "web/index.html": b"new", "requirements.txt": b"fastapi\n",
                 "tools/guard.sh": (ROOT / "tools/guard.sh").read_bytes()}
    updater._http = lambda url, timeout=30: make_tar(new_files)
    up.latest = {"sha": "abc123" * 6, "date": "2026-11-20T10:00:00Z", "message": "nuova versione"}
    check(up.status()["available"], "aggiornamento disponibile")

    async def run_job():
        await up.start()
        await asyncio.wait_for(updater.restart_event.wait(), 10)
    asyncio.run(run_job())
    check(up.job["state"] == "restarting", "aggiornamento eseguito, riavvio richiesto")
    check((app / "web/index.html").read_text() == "new" and (app / "meshwatch.db").read_text() == "dati"
          and (app / "config.yaml").exists(), "file sostituiti, config e dati intatti")
    check(json.loads((app / "VERSION.json").read_text())["sha"] == up.latest["sha"], "versione registrata")
    check(not up.status()["available"], "dopo l'aggiornamento non ce ne sono altri")
    check((app / ".update/pending").exists(), "aggiornamento in prova")

    # ripristino: tre avvii falliti → al quarto torna la versione precedente
    guard = ["sh", str(app / "tools/guard.sh")]
    for _ in range(3):
        subprocess.run(guard, check=True)
    check((app / "web/index.html").read_text() == "new", "prime 3 prove con la nuova versione")
    subprocess.run(guard, check=True, capture_output=True)
    check((app / "web/index.html").read_text() == "old" and not (app / "VERSION.json").exists()
          and (app / "tools/guard.sh").exists(), "versione precedente ripristinata")
    st = up.status()
    check(st["rolled_back"] and not st["pending"], "ripristino segnalato alla pagina")

    # conferma: se il programma resta su, l'aggiornamento non è più in prova
    (app / ".update/pending").write_text("{}")
    updater.CONFIRM_AFTER = 0
    asyncio.run(up.confirm_later())
    check(not (app / ".update/pending").exists(), "aggiornamento confermato dopo l'avvio")

    # senza systemd non si aggiorna (non ripartirebbe)
    del os.environ["INVOCATION_ID"]
    check(Updater({}, app_dir=app).problems(), "senza systemd l'aggiornamento è bloccato")

    # API
    from fastapi.testclient import TestClient
    from meshwatch.auth import Auth
    from meshwatch.core import Core
    from meshwatch.db import DB
    from meshwatch.web import create_app
    from tests.test_smoke import FakeMesh
    db = DB(str(tmp / "t.db"))
    c = TestClient(create_app(Core({}, db, FakeMesh()), Auth(db), {"mode": "lan"}, Updater({}, app_dir=app)))
    r = c.get("/api/update").json()
    check(r["version"] and r["problems"], "API stato aggiornamento")
    check(c.post("/api/update/apply").status_code == 400, "API rifiuta se non si può aggiornare")
    check(c.get("/icon.svg").status_code == 200 and c.get("/config.yaml").status_code == 404,
          "icona pubblica, nient'altro")
    print("Tutti i test superati.")


if __name__ == "__main__":
    main()
