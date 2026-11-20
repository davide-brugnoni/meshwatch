import argparse
import asyncio
import getpass
import logging
import sys
from pathlib import Path

import uvicorn
import yaml

from .auth import Auth, hash_password
from .core import Core
from .db import DB
from .mesh import Mesh
from .web import create_app

log = logging.getLogger("meshwatch")


async def run(cfg):
    db = DB(cfg.get("database", "meshwatch.db"))
    mesh = Mesh(cfg.get("serial_port", "auto"), cfg.get("baudrate", 115200))
    core = Core(cfg, db, mesh)

    web_cfg = cfg.get("web", {})
    auth = Auth(db)
    if web_cfg.get("mode") == "public" and not auth.enabled:
        sys.exit("web.mode è 'public' ma non ci sono utenti: crea prima un utente admin dalla pagina "
                 "in modalità lan (Gestione → Utenti), poi passa a public.")

    tg = cfg.get("telegram") or {}
    if tg.get("token") and "BotFather" not in tg["token"]:
        from .bot import Bot
        await Bot(tg, core).run()
    else:
        log.info("Telegram non configurato: bot disattivato")

    server = uvicorn.Server(uvicorn.Config(
        create_app(core, auth, web_cfg),
        host=web_cfg.get("host", "0.0.0.0"), port=int(web_cfg.get("port", 8080)),
        log_level="warning", proxy_headers=web_cfg.get("mode") == "public"))

    async def radio():
        await mesh.connect()
        await asyncio.gather(core.tracker_loop(), core.repeater_loop())

    await asyncio.gather(server.serve(), radio())


def main():
    p = argparse.ArgumentParser(prog="meshwatch")
    p.add_argument("cmd", nargs="?", default="run", choices=["run", "hashpw"])
    p.add_argument("-c", "--config", default="config.yaml")
    a = p.parse_args()

    if a.cmd == "hashpw":
        pw = getpass.getpass("Nuova password: ")
        if pw != getpass.getpass("Ripeti: "):
            sys.exit("Le password non coincidono.")
        print(hash_password(pw))
        return

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    cfg = yaml.safe_load(Path(a.config).read_text(encoding="utf-8"))
    asyncio.run(run(cfg))


if __name__ == "__main__":
    main()
