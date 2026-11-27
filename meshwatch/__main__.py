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
from . import restore
from .tiles import TileCache
from . import watchdog
from .updater import Updater, restart_event
from .web import create_app

log = logging.getLogger("meshwatch")


async def run(cfg):
    web_cfg = cfg.get("web", {})
    host, port = web_cfg.get("host", "0.0.0.0"), int(web_cfg.get("port", 8080))
    server = None
    # parte subito: durante l'avvio tiene vivo il servizio, poi controlla che la pagina risponda
    dog = asyncio.create_task(watchdog.run(host, port, lambda: bool(server and server.started)))

    db = DB(cfg.get("database", "meshwatch.db"))
    mesh = Mesh.from_config(cfg)
    core = Core(cfg, db, mesh)
    restored = restore.take_announcement(cfg)
    if restored:   # backup ripristinato dalla pagina al riavvio precedente
        what = ", ".join(x for x, on in (("dati", restored.get("data")), ("impostazioni della pagina", restored.get("settings")),
                                          ("file di configurazione", restored.get("config"))) if on)
        core.audit(restored.get("user") or "sistema", "ripristino applicato" if restored["ok"] else "ripristino non riuscito",
                   (what + (": " + "; ".join(restored["errors"]) if restored["errors"] else ""))[:300])

        async def announce():
            await asyncio.sleep(90)   # il bot nel frattempo si collega
            await core.notify(("♻️ Backup ripristinato dalla pagina (" + what + ")." if restored["ok"] else
                               "⚠️ Ripristino del backup non riuscito: " + "; ".join(restored["errors"])))
        task = asyncio.create_task(announce())
        core.tasks.add(task)   # riferimento: un task senza riferimenti può sparire
        task.add_done_callback(core.tasks.discard)

    auth = Auth(db)
    if web_cfg.get("mode") == "public" and not auth.enabled:
        sys.exit("web.mode è 'public' ma non ci sono utenti: crea prima un utente admin dalla pagina "
                 "in modalità lan (al primo accesso a Gestione chiede la password admin), poi passa a public.")

    updater = Updater(cfg.get("update") or {})
    tiles = TileCache(db)   # una sola cache delle mappe per la pagina e per le immagini di Telegram
    tg = cfg.get("telegram") or {}
    bot = None
    if tg.get("token") and "BotFather" not in tg["token"]:
        from .bot import Bot
        bot = Bot(tg, core, tiles, updater)   # si collega a Telegram in sottofondo (vedi start_forever)
    else:
        log.info("Telegram non configurato: bot disattivato")

    server = uvicorn.Server(uvicorn.Config(
        create_app(core, auth, web_cfg, updater, tiles),
        host=host, port=port,
        log_level="warning", proxy_headers=web_cfg.get("mode") == "public",
        timeout_graceful_shutdown=5))   # le pagine collegate in tempo reale non bloccano il riavvio

    async def radio():
        await mesh.connect()
        await asyncio.gather(core.tracker_loop(), core.repeater_loop())

    serve = asyncio.create_task(server.serve())
    others = [asyncio.create_task(radio()), asyncio.create_task(updater.announce(
                  core.notify, (lambda: db.get("update_rollback_notified"), lambda v: db.set("update_rollback_notified", v)))),
              asyncio.create_task(core.maintenance_loop()), asyncio.create_task(core.radio_watchdog()),
              asyncio.create_task(core.firmware_watch()), asyncio.create_task(core.netstats.loop()),
              asyncio.create_task(core.publisher.loop())]
    if bot:   # senza Internet all'avvio la pagina parte lo stesso; il bot riprova finché la rete torna
        others.append(asyncio.create_task(bot.start_forever()))
    if (cfg.get("update") or {}).get("notify", True):
        others.append(asyncio.create_task(core.update_watch(updater)))
    restart = asyncio.create_task(restart_event.wait())
    await asyncio.wait([serve, restart], return_when=asyncio.FIRST_COMPLETED)
    if restart.done():
        log.info("Riavvio richiesto (aggiornamento): chiudo il servizio, systemd lo fa ripartire")
        core.events.close()
        server.should_exit = True
        await serve
    for t in others + [restart, dog]:
        t.cancel()


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
    cfg["_config_path"] = str(Path(a.config).resolve())   # per il backup
    # backup ripristinato dalla pagina: i file vanno al loro posto prima di aprire il database
    done = restore.apply_pending(cfg)
    if done and done.get("config") and done.get("mode") != "sd":
        cfg = yaml.safe_load(Path(a.config).read_text(encoding="utf-8"))
        cfg["_config_path"] = str(Path(a.config).resolve())
    asyncio.run(run(cfg))


if __name__ == "__main__":
    main()
