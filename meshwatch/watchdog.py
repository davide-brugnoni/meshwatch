"""Watchdog di systemd: se la pagina smette di rispondere, systemd riavvia MeshWatch.

Il servizio ha WatchdogSec=: systemd si aspetta un segnale "WATCHDOG=1" almeno ogni tanti
secondi, altrimenti uccide il programma e lo fa ripartire (Restart=always). Il segnale parte
solo se la pagina risponde davvero: ogni PING_S secondi il programma chiede /api/health a se
stesso. Così si riprende da solo anche quando il processo è acceso ma bloccato, caso in cui
Restart=always da solo non basta.

Fuori da systemd (sviluppo, test) NOTIFY_SOCKET non c'è e non fa nulla.
"""
import asyncio
import logging
import os
import socket

log = logging.getLogger("meshwatch.watchdog")

PING_S = 20          # ogni quanto controlla la pagina
TIMEOUT_S = 10       # quanto aspetta la risposta
STARTUP_MAX_S = 600  # durante l'avvio (bot Telegram, server) tiene vivo il servizio al massimo così


def notify(msg):
    """Invia un messaggio a systemd (sd_notify). Ritorna False se non gira sotto systemd."""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return False
    if addr.startswith("@"):
        addr = "\0" + addr[1:]   # socket astratto
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(addr)
            s.sendall(msg.encode())
        return True
    except OSError as e:
        log.warning("Notifica a systemd non riuscita: %s", e)
        return False


async def check_page(host, port, timeout=TIMEOUT_S):
    """True se la pagina risponde 200 a GET /api/health."""
    if host in ("0.0.0.0", "", None):
        host = "127.0.0.1"
    elif host in ("::",):
        host = "::1"
    writer = None
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
        writer.write(f"GET /api/health HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode())
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout)
        return line.split(b" ")[1:2] == [b"200"]
    except (OSError, asyncio.TimeoutError, IndexError):
        return False
    finally:
        if writer:
            writer.close()


async def run(host, port, server_started, ping=PING_S, startup_max=STARTUP_MAX_S, check=check_page):
    """Ciclo del watchdog. server_started() dice se il server web è partito: prima di allora
    il segnale parte comunque (per al massimo startup_max secondi), dopo solo se la pagina risponde."""
    if not os.environ.get("NOTIFY_SOCKET"):
        return
    waited = 0
    failures = 0
    while True:
        if not server_started():
            if waited < startup_max:
                notify("WATCHDOG=1")
            waited += ping
        elif await check(host, port):
            if failures:
                log.info("La pagina risponde di nuovo")
            failures = 0
            notify("WATCHDOG=1")
        else:
            failures += 1
            log.warning("La pagina non risponde (%d volte di fila): se continua systemd riavvia MeshWatch",
                        failures)
        await asyncio.sleep(ping)
