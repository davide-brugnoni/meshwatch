"""Cache delle tessere delle mappe sul Raspberry.

Il browser chiede le tessere a /api/tiles/<mappa>/<z>/<x>/<y>: se il Raspberry le ha
già (e non sono scadute) risponde subito dalla SD, altrimenti le prende da Internet,
le conserva e le passa al browser. Le zone già viste si aprono quindi in un attimo
e restano visibili anche se manca Internet.

Rispetta le regole d'uso di OpenStreetMap (operations.osmfoundation.org/policies/tiles):
- salva solo le tessere che qualcuno guarda, mai scaricamenti in blocco;
- User-Agent che identifica il programma;
- rispetta la scadenza indicata dal server (Cache-Control / Expires) e, scaduta,
  chiede se è cambiata (If-None-Match / If-Modified-Since): se no, il server risponde
  304 senza rimandarla;
- poche richieste contemporanee e un tetto al minuto.
Le mappe satellitari Esri non passano dalla cache (le loro condizioni non lo prevedono).

Le tessere stanno in un database SQLite a parte (tiles.db, accanto a meshwatch.db):
niente migliaia di file piccoli sulla SD, dimensione facile da tenere sotto il limite.
Impostazioni (tabella settings del database principale):
    tile_cache      1 = attiva (default), 0 = il browser va direttamente su Internet
    tile_cache_mb   spazio massimo (default 500 MB): oltre, si cancellano le meno usate
"""
import asyncio
import email.utils
import logging
import os
import re
import shutil
import sqlite3
import threading
import time
from pathlib import Path

from . import __version__

log = logging.getLogger("meshwatch.tiles")

# mappe che passano dalla cache: indirizzo, sottodomini, zoom massimo
SOURCES = {
    "osm": ("https://tile.openstreetmap.org/{z}/{x}/{y}.png", "", 19),
    "cyclosm": ("https://{s}.tile-cyclosm.openstreetmap.fr/cyclosm/{z}/{x}/{y}.png", "abc", 20),
    "topo": ("https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png", "abc", 17),
}
USER_AGENT = f"MeshWatch/{__version__} (+https://github.com/davide-brugnoni/meshwatch; tile cache)"
DEFAULT_MB = 500
MIN_MB, MAX_MB = 50, 20000
DEFAULT_TTL = 7 * 86400        # se il server non dice nulla sulla scadenza
MIN_TTL = 3600
MIN_FREE_BYTES = 300 * 2**20   # sotto questo spazio libero sulla SD non salva più
PARALLEL = 4                   # richieste contemporanee verso i server delle mappe
PER_MINUTE = 600               # tetto di tessere scaricate al minuto
TOUCH_EVERY = 86400            # "ultimo uso" aggiornato al massimo una volta al giorno (meno scritture sulla SD)


class TileError(Exception):
    def __init__(self, status, msg):
        super().__init__(msg)
        self.status = status


def _ttl(headers):
    cc = headers.get("cache-control", "")
    m = re.search(r"max-age=(\d+)", cc)
    if m:
        return int(m.group(1))
    if headers.get("expires"):
        try:
            return int(email.utils.parsedate_to_datetime(headers["expires"]).timestamp() - time.time())
        except (TypeError, ValueError):
            pass
    return DEFAULT_TTL


class TileCache:
    def __init__(self, db, path=None, transport=None):
        self.db = db   # database principale: impostazioni
        if path is None:
            base = Path(db.path).resolve().parent if db.path and db.path != ":memory:" else Path(".")
            path = base / "tiles.db"
        self.path = str(path)
        self.transport = transport   # nei test: server finto
        self.lock = threading.Lock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        with self.lock:
            self.conn.execute("PRAGMA auto_vacuum=INCREMENTAL")   # vale solo per un file nuovo
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS tiles (
                    layer TEXT, z INTEGER, x INTEGER, y INTEGER,
                    data BLOB, ctype TEXT, etag TEXT, lastmod TEXT,
                    fetched INTEGER, expires INTEGER, used INTEGER,
                    PRIMARY KEY (layer, z, x, y)
                );
                CREATE INDEX IF NOT EXISTS tiles_used ON tiles(used);
                """)
            r = self.conn.execute("SELECT COUNT(*), COALESCE(SUM(LENGTH(data)), 0) FROM tiles").fetchone()
        self.count, self.size = r[0], r[1]
        self.hits = self.misses = self.stale = self.errors = 0
        self.inflight = {}
        self.sem = None
        self.client = None
        self.window = (0, 0)   # (minuto, tessere scaricate in quel minuto)

    # ---------- impostazioni
    def enabled(self):
        return self.db.get("tile_cache", "1") == "1"

    def limit_bytes(self):
        try:
            mb = int(self.db.get("tile_cache_mb", DEFAULT_MB))
        except ValueError:
            mb = DEFAULT_MB
        return max(MIN_MB, min(mb, MAX_MB)) * 2**20

    def info(self):
        return {"enabled": self.enabled(), "max_mb": self.limit_bytes() // 2**20,
                "tiles": self.count, "size": self.size,
                "hits": self.hits, "misses": self.misses, "stale": self.stale, "errors": self.errors,
                "layers": list(SOURCES)}

    # ---------- database
    def _get(self, key):
        with self.lock:
            r = self.conn.execute("SELECT data, ctype, etag, lastmod, expires, used FROM tiles "
                                  "WHERE layer=? AND z=? AND x=? AND y=?", key).fetchone()
        return dict(zip(("data", "ctype", "etag", "lastmod", "expires", "used"), r)) if r else None

    def _touch(self, key, expires=None):
        now = int(time.time())
        with self.lock:
            if expires is None:
                self.conn.execute("UPDATE tiles SET used=? WHERE layer=? AND z=? AND x=? AND y=?", (now, *key))
            else:
                self.conn.execute("UPDATE tiles SET used=?, fetched=?, expires=? WHERE layer=? AND z=? AND x=? AND y=?",
                                  (now, now, expires, *key))
            self.conn.commit()

    def _put(self, key, data, ctype, etag, lastmod, expires):
        if shutil.disk_usage(os.path.dirname(os.path.abspath(self.path))).free < MIN_FREE_BYTES:
            return   # SD quasi piena: la tessera va al browser ma non si salva
        now = int(time.time())
        with self.lock:
            old = self.conn.execute("SELECT LENGTH(data) FROM tiles WHERE layer=? AND z=? AND x=? AND y=?", key).fetchone()
            self.conn.execute("INSERT OR REPLACE INTO tiles VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                              (*key, data, ctype, etag, lastmod, now, expires, now))
            self.conn.commit()
            if old:
                self.size -= old[0] or 0
            else:
                self.count += 1
            self.size += len(data)
        if self.size > self.limit_bytes():
            self._prune(int(self.limit_bytes() * 0.9))

    def _prune(self, target):
        """Cancella le tessere usate meno di recente finché la cache scende sotto `target` byte."""
        with self.lock:
            while self.size > target:
                rows = self.conn.execute("SELECT rowid, LENGTH(data) FROM tiles ORDER BY used, rowid LIMIT 500").fetchall()
                if not rows:
                    break
                gone = []
                for rowid, n in rows:
                    if self.size <= target:
                        break
                    gone.append((rowid,))
                    self.size -= n or 0
                self.conn.executemany("DELETE FROM tiles WHERE rowid=?", gone)
                self.count -= len(gone)
            self.conn.commit()
            self.conn.execute("PRAGMA incremental_vacuum")
            self.size = max(self.size, 0)

    def clear(self):
        with self.lock:
            n = self.count
            self.conn.execute("DELETE FROM tiles")
            self.conn.commit()
            self.conn.execute("VACUUM")
            self.count = self.size = 0
        return n

    def set_limit(self, mb):
        self.db.set("tile_cache_mb", int(mb))
        if self.size > self.limit_bytes():
            self._prune(self.limit_bytes())

    # ---------- Internet
    def _allow_fetch(self):
        minute = int(time.time() // 60)
        m, n = self.window
        n = n + 1 if m == minute else 1
        self.window = (minute, n)
        return n <= PER_MINUTE

    async def _fetch(self, layer, z, x, y, old):
        import httpx
        if self.client is None:
            self.client = httpx.AsyncClient(timeout=httpx.Timeout(10, connect=5), transport=self.transport,
                                            headers={"User-Agent": USER_AGENT}, follow_redirects=True)
            self.sem = asyncio.Semaphore(PARALLEL)
        url, subs, _ = SOURCES[layer]
        url = url.format(s=subs[(x + y) % len(subs)] if subs else "", z=z, x=x, y=y)
        headers = {}
        if old and old["etag"]:
            headers["If-None-Match"] = old["etag"]
        if old and old["lastmod"]:
            headers["If-Modified-Since"] = old["lastmod"]
        async with self.sem:
            return await self.client.get(url, headers=headers)

    async def get(self, layer, z, x, y):
        """Ritorna (dati, content-type, secondi di validità per il browser)."""
        if layer not in SOURCES:
            raise TileError(404, "mappa sconosciuta")
        if not (0 <= z <= SOURCES[layer][2] and 0 <= x < 2**z and 0 <= y < 2**z):
            raise TileError(404, "tessera fuori dalla mappa")
        if not self.enabled():
            raise TileError(404, "cache delle mappe disattivata")
        key = (layer, z, x, y)
        now = time.time()
        old = await asyncio.to_thread(self._get, key)
        if old and old["expires"] > now:
            self.hits += 1
            if now - (old["used"] or 0) > TOUCH_EVERY:
                await asyncio.to_thread(self._touch, key)
            return old["data"], old["ctype"], int(old["expires"] - now)
        # più richieste della stessa tessera nello stesso momento: un solo download
        task = self.inflight.get(key)
        if task is None:
            task = asyncio.ensure_future(self._refresh(key, old))
            self.inflight[key] = task
            task.add_done_callback(lambda _t: self.inflight.pop(key, None))
        return await asyncio.shield(task)

    async def _refresh(self, key, old):
        layer, z, x, y = key
        if not self._allow_fetch():
            if old:
                self.stale += 1
                return old["data"], old["ctype"], 300
            raise TileError(429, "troppe tessere richieste: riprova tra un minuto")
        try:
            r = await self._fetch(layer, z, x, y, old)
        except Exception as e:   # niente Internet, server lento, …
            self.errors += 1
            if old:
                self.stale += 1   # meglio una tessera vecchia che niente
                return old["data"], old["ctype"], 300
            log.debug("tessera %s non scaricata: %s", key, e)
            raise TileError(504, "mappa non raggiungibile da Internet")
        ttl = max(_ttl(r.headers), MIN_TTL)
        expires = int(time.time()) + ttl
        if r.status_code == 304 and old:
            self.hits += 1
            await asyncio.to_thread(self._touch, key, expires)
            return old["data"], old["ctype"], ttl
        ctype = r.headers.get("content-type", "")
        if r.status_code != 200 or not ctype.startswith("image/"):
            self.errors += 1
            if old:
                self.stale += 1
                return old["data"], old["ctype"], 300
            raise TileError(502, f"il server della mappa ha risposto {r.status_code}")
        self.misses += 1
        await asyncio.to_thread(self._put, key, r.content, ctype, r.headers.get("etag"),
                                r.headers.get("last-modified"), expires)
        return r.content, ctype, ttl
