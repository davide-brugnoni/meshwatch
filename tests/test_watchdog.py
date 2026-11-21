"""Watchdog di systemd: il segnale WATCHDOG=1 parte solo se la pagina risponde.
Esecuzione:  python tests/test_watchdog.py
Simula systemd con un socket di notifica e avvia un vero server web su una porta libera.
"""
import asyncio
import os
import socket
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import uvicorn  # noqa: E402

from meshwatch import watchdog  # noqa: E402
from meshwatch.auth import Auth  # noqa: E402
from meshwatch.core import Core  # noqa: E402
from meshwatch.db import DB  # noqa: E402
from tests.test_smoke import FakeMesh  # noqa: E402

FAIL = 0


def check(cond, msg):
    global FAIL
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        FAIL += 1


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def received(sock):
    out = []
    while True:
        try:
            out.append(sock.recv(100).decode())
        except BlockingIOError:
            return out


async def main():
    from meshwatch.web import create_app
    tmp = tempfile.mkdtemp()
    sock_path = os.path.join(tmp, "notify")
    sd = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    sd.bind(sock_path)
    sd.setblocking(False)

    # fuori da systemd non fa nulla
    os.environ.pop("NOTIFY_SOCKET", None)
    check(watchdog.notify("WATCHDOG=1") is False, "senza systemd notify non invia nulla")
    await asyncio.wait_for(watchdog.run("127.0.0.1", 1, lambda: True), 1)
    check(True, "senza systemd il ciclo termina subito")

    os.environ["NOTIFY_SOCKET"] = sock_path
    check(watchdog.notify("WATCHDOG=1") and received(sd) == ["WATCHDOG=1"], "notify arriva a systemd")

    db = DB(os.path.join(tmp, "t.db"))
    core = Core({}, db, FakeMesh())
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(create_app(core, Auth(db), {}), host="127.0.0.1", port=port,
                                           log_level="warning"))
    check(not await watchdog.check_page("127.0.0.1", port, timeout=1), "server spento: pagina non risponde")

    # durante l'avvio il segnale parte comunque, ma solo per startup_max secondi
    task = asyncio.create_task(watchdog.run("0.0.0.0", port, lambda: server.started, ping=0.05, startup_max=0.1))
    await asyncio.sleep(0.4)
    n = received(sd).count("WATCHDOG=1")
    check(1 <= n <= 3, f"avvio: segnale solo per il tempo massimo ({n} segnali)")
    check(received(sd) == [], "avvio troppo lungo: nessun segnale, systemd riavvia")

    serve = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.05)
    check(await watchdog.check_page("0.0.0.0", port), "/api/health risponde 200 (0.0.0.0 → 127.0.0.1)")
    await asyncio.sleep(0.3)
    check(received(sd).count("WATCHDOG=1") >= 2, "pagina viva: segnale regolare")

    # pagina bloccata: il controllo fallisce e il segnale si ferma
    alive = {"ok": True}

    async def fake_check(host, port):
        return alive["ok"]
    task.cancel()
    task = asyncio.create_task(watchdog.run("127.0.0.1", port, lambda: True, ping=0.05, check=fake_check))
    await asyncio.sleep(0.2)
    check(received(sd), "controllo riuscito: segnale")
    alive["ok"] = False
    await asyncio.sleep(0.1)
    received(sd)
    await asyncio.sleep(0.3)
    check(received(sd) == [], "pagina bloccata: nessun segnale, systemd riavvia")
    alive["ok"] = True
    await asyncio.sleep(0.2)
    check(received(sd), "pagina di nuovo viva: segnale riprende")
    task.cancel()

    server.should_exit = True
    await serve
    sd.close()


asyncio.run(main())
print("\nTUTTO OK" if not FAIL else f"\n{FAIL} TEST FALLITI")
sys.exit(1 if FAIL else 0)
