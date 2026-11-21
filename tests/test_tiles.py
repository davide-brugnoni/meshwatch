"""Cache delle tessere delle mappe, con un server delle mappe finto (niente Internet).
Esecuzione:  python tests/test_tiles.py
"""
import asyncio
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from meshwatch import tiles as T  # noqa: E402
from meshwatch.auth import Auth  # noqa: E402
from meshwatch.core import Core  # noqa: E402
from meshwatch.db import DB  # noqa: E402
from meshwatch.web import create_app  # noqa: E402
from tests.test_chat import ChatMesh  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 2000


def check(cond, msg):
    print(("OK   " if cond else "FAIL ") + msg)
    if not cond:
        sys.exit(1)


class FakeServer:
    """Server delle mappe: conta le richieste, risponde 304 se l'ETag coincide."""

    def __init__(self):
        self.calls, self.online, self.max_age, self.status = [], True, 3600, 200

    def __call__(self, req):
        if not self.online:
            raise httpx.ConnectError("niente Internet")
        self.calls.append(req)
        if self.status != 200:
            return httpx.Response(self.status, text="no")
        if req.headers.get("if-none-match") == '"v1"':
            return httpx.Response(304, headers={"cache-control": f"max-age={self.max_age}"})
        return httpx.Response(200, content=PNG, headers={"content-type": "image/png", "etag": '"v1"',
                                                         "cache-control": f"max-age={self.max_age}"})


async def unit(tmp):
    db = DB(os.path.join(tmp, "u.db"))
    srv = FakeServer()
    tc = T.TileCache(db, transport=httpx.MockTransport(srv))
    check(tc.path == os.path.join(tmp, "tiles.db"), "tiles.db accanto al database")

    data, ctype, ttl = await tc.get("osm", 5, 10, 11)
    check(data == PNG and ctype == "image/png" and len(srv.calls) == 1, "prima volta: scaricata da Internet")
    req = srv.calls[0]
    check(str(req.url) == "https://tile.openstreetmap.org/5/10/11.png", "indirizzo OpenStreetMap giusto")
    check(req.headers["user-agent"].startswith("MeshWatch/"), "User-Agent che identifica il programma")
    await tc.get("osm", 5, 10, 11)
    check(len(srv.calls) == 1 and tc.hits == 1 and tc.count == 1, "seconda volta: dalla cache, nessun download")

    # più richieste della stessa tessera insieme: un solo download
    await asyncio.gather(*[tc.get("topo", 6, 1, 2) for _ in range(5)])
    check(len(srv.calls) == 2 and "tile.opentopomap.org/6/1/2.png" in str(srv.calls[1].url), "richieste uguali insieme: un download")

    # scaduta: chiede se è cambiata, il server risponde 304
    with tc.lock:
        tc.conn.execute("UPDATE tiles SET expires=0 WHERE layer='osm'")
        tc.conn.commit()
    n = len(srv.calls)
    data, _, ttl = await tc.get("osm", 5, 10, 11)
    check(data == PNG and ttl == T.STALE_TTL, "scaduta: subito la tessera vecchia, senza aspettare Internet")
    await asyncio.gather(*tc.inflight.values())   # aggiornamento in sottofondo
    check(len(srv.calls) == n + 1 and srv.calls[-1].headers.get("if-none-match") == '"v1"', "scaduta: chiede se è cambiata (304)")
    check(tc._get(("osm", 5, 10, 11))["expires"] > time.time(), "nuova scadenza salvata")
    data, _, ttl = await tc.get("osm", 5, 10, 11)
    check(data == PNG and ttl > 3000 and len(srv.calls) == n + 1, "dopo l'aggiornamento: di nuovo valida, nessun download")

    # senza Internet: la tessera vecchia è meglio di niente; una mai vista dà errore
    with tc.lock:
        tc.conn.execute("UPDATE tiles SET expires=0")
        tc.conn.commit()
    srv.online = False
    data, _, ttl = await tc.get("osm", 5, 10, 11)
    await asyncio.gather(*tc.inflight.values())
    check(data == PNG and ttl == T.STALE_TTL and tc.stale == 1, "senza Internet: mostra la tessera vecchia")
    try:
        await tc.get("osm", 5, 0, 0)
        check(False, "tessera mai vista senza Internet")
    except T.TileError as e:
        check(e.status == 504, "senza Internet e mai vista: errore 504")
    srv.online = True

    # il server risponde con un errore: non si salva niente
    srv.status = 429
    try:
        await tc.get("cyclosm", 3, 1, 1)
        check(False, "errore del server")
    except T.TileError as e:
        check(e.status == 502 and tc.count == 2, "errore del server: niente in cache")
    srv.status = 200

    # coordinate e mappe non valide
    for args in (("esri", 1, 0, 0), ("osm", 20, 0, 0), ("osm", 2, 4, 0), ("osm", 2, 0, -1)):
        try:
            await tc.get(*args)
            check(False, f"tessera non valida {args}")
        except T.TileError as e:
            check(e.status == 404, f"rifiutata {args}")

    # tetto al minuto
    tc.window = (tc.window[0], T.PER_MINUTE)
    try:
        await tc.get("osm", 4, 1, 1)
        check(False, "tetto al minuto")
    except T.TileError as e:
        check(e.status == 429, "oltre il tetto al minuto: 429")
    tc.window = (0, 0)

    # spazio massimo: restano le tessere usate più di recente
    tc.limit_bytes = lambda: 5 * len(PNG)
    for i in range(10):
        await tc.get("osm", 10, i, 0)
    real = tc.conn.execute("SELECT COUNT(*), SUM(LENGTH(data)) FROM tiles").fetchone()
    check(tc.size <= 5 * len(PNG) and (tc.count, tc.size) == real, f"oltre il limite: pulizia ({tc.count} tessere)")
    check(tc._get(("osm", 10, 9, 0)) is not None, "resta l'ultima vista")

    # disattivata
    db.set("tile_cache", "0")
    try:
        await tc.get("osm", 10, 9, 0)
        check(False, "cache disattivata")
    except T.TileError as e:
        check(e.status == 404, "disattivata: il browser va diretto su Internet")
    db.set("tile_cache", "1")

    n = tc.count
    check(tc.clear() == n and tc.count == 0 and tc.size == 0, "svuota cache")
    if tc.client:
        await tc.client.aclose()


def api(tmp):
    db = DB(os.path.join(tmp, "a.db"))
    core = Core({}, db, ChatMesh())
    srv = FakeServer()
    tc = T.TileCache(db, path=os.path.join(tmp, "a-tiles.db"), transport=httpx.MockTransport(srv))
    admin = TestClient(create_app(core, Auth(db), {"mode": "lan"}, tiles=tc))
    check(admin.get("/api/me").json()["tile_cache"] is True, "/api/me dice se la cache è attiva")
    r = admin.get("/api/tiles/osm/3/2/1")
    check(r.status_code == 200 and r.content == PNG and r.headers["content-type"] == "image/png", "tessera dall'API")
    check(r.headers["cache-control"].startswith("private, max-age="), "il browser la tiene (non no-store)")
    r = admin.get("/api/tiles/osm/3/2/1", headers={"Accept-Encoding": "gzip"})
    check("content-encoding" not in r.headers, "tessere non ricompresse (sono già PNG)")
    check(admin.get("/api/state").headers["cache-control"] == "no-store", "il resto dell'API resta no-store")
    check(admin.get("/api/tiles/esri/3/2/1").status_code == 404, "satellite non passa dalla cache")

    admin.post("/api/setup", json={"username": "admin1", "password": "segreto1"})
    i = admin.get("/api/tilecache").json()
    check(i["tiles"] == 1 and i["enabled"] and i["max_mb"] == T.DEFAULT_MB and i["disk_free"] > 0, "stato nella Gestione")
    check(admin.put("/api/tilecache", json={"max_mb": 10}).status_code == 422, "spazio minimo 50 MB")
    check(admin.put("/api/tilecache", json={"enabled": False, "max_mb": 200}).status_code == 200, "impostazioni salvate")
    i = admin.get("/api/tilecache").json()
    check(not i["enabled"] and i["max_mb"] == 200 and admin.get("/api/me").json()["tile_cache"] is False, "cache disattivata")
    check(admin.get("/api/tiles/osm/3/2/1").status_code == 404, "disattivata: niente tessere dal Raspberry")
    check(admin.post("/api/tilecache/clear").json()["deleted"] == 1, "svuotata dalla pagina")
    check(any("cache delle mappe" in a["action"] for a in db.audit(20)), "nel registro attività")

    pub = TestClient(create_app(core, Auth(db), {"mode": "public"}, tiles=tc))
    check(pub.get("/api/tiles/osm/3/2/1").status_code == 401, "da Internet senza login: niente tessere")


def main():
    tmp = tempfile.mkdtemp()
    asyncio.run(unit(tmp))
    api(tmp)
    print("Tutti i test superati.")


if __name__ == "__main__":
    main()
