"""Invio delle sole statistiche della rete a una dashboard pubblica.

Ogni `interval_min` minuti prepara un riassunto delle statistiche (lo stesso della scheda
Statistiche, ridotto) e lo manda con un POST HTTPS all'indirizzo scelto, con un token.
È solo il Raspberry a parlare verso l'esterno: non si apre nessuna porta e la pagina resta in casa.

Parte: riassunto della rete, andamento, mappa di calore, tipi di pacchetto, disponibilità,
batteria, storico di 7 giorni di ripetitori e gateway (batteria, rumore, SNR, RSSI, traffico,
airtime), confronto, collegamenti, versioni del firmware, stato del Raspberry, nodi sentiti.

Cosa NON parte mai: posizioni e tracce dei dispositivi seguiti (che non compaiono nemmeno
nell'elenco dei nodi), sensori, chat, rubrica, utenti, password, registro attività, posizione
del gateway, nome in rete del Raspberry.
Le posizioni dei ripetitori e dei room server sono arrotondate a una griglia (di base 500 m);
i companion sono contati ma non elencati, a meno di `companions: true` (e comunque senza posizione).

Si configura dalla pagina (Gestione → Dashboard); config.yaml dà solo i valori di partenza:
    publish:
      url: https://tuosito.it/api/meshwatch
      token: "una-stringa-lunga-e-casuale"
      interval_min: 15        # minimo 5
      grid_m: 500             # 0 = nessuna posizione
      companions: false       # elencare anche i companion (nome e ultimo advert, mai la posizione)
      title: "Rete MeshCore"  # titolo mostrato sulla dashboard
"""
import asyncio
import json
import logging
import math
import time

from . import __version__

log = logging.getLogger("meshwatch.publish")

MIN_INTERVAL = 5
WITH_POSITION = ("ripetitore", "room server")
DAY = 86400


SECTIONS = {"map": "mappa dei ripetitori", "repeaters": "disponibilità e batteria dei ripetitori",
            "history": "storico di 7 giorni delle radio", "traffic": "andamento e traffico della rete",
            "links": "qualità dei collegamenti", "compare": "confronto tra le radio",
            "nodes": "elenco dei nodi sentiti", "system": "stato del Raspberry, gateway e firmware"}
GRIDS = (0, 250, 500, 1000, 2000, 5000)
DB_KEY = "publish"


def _int(v, default, lo, hi):
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return default


def settings(cfg, db=None):
    """Impostazioni della pubblicazione: quelle salvate dalla pagina (Gestione → Dashboard)
    valgono su config.yaml, che resta il punto di partenza."""
    p = dict(cfg.get("publish") or {})
    if db is not None:
        try:
            p.update(json.loads(db.get(DB_KEY) or "{}"))
        except (ValueError, TypeError):
            pass
    url, token = str(p.get("url") or "").strip(), str(p.get("token") or "").strip()
    grid = _int(p.get("grid_m", p.get("position_grid_m", 500)), 500, 0, 5000)
    sections = p.get("sections")
    sections = [x for x in sections if x in SECTIONS] if isinstance(sections, list) else list(SECTIONS)
    return {"enabled": bool(url and token) and p.get("enabled", True) is not False,
            "on": p.get("enabled", True) is not False,
            "url": url, "token": token, "interval_min": _int(p.get("interval_min"), 15, MIN_INTERVAL, 1440),
            "grid_m": grid, "companions": bool(p.get("companions", False)),
            "title": str(p.get("title") or "Rete MeshCore").strip()[:80] or "Rete MeshCore", "sections": sections}


def save_settings(db, cfg, **new):
    """Salva dalla pagina. Un token vuoto lascia quello di prima."""
    cur = settings(cfg, db)
    data = {k: cur[k] for k in ("url", "token", "interval_min", "grid_m", "companions", "title", "sections")}
    data["enabled"] = cur["on"]
    for k, v in new.items():
        if v is None or (k == "token" and not str(v).strip()):
            continue
        data[k] = v
    if new.get("clear_token"):
        data["token"] = ""
    data.pop("clear_token", None)
    db.set(DB_KEY, json.dumps(data, ensure_ascii=False))
    return settings(cfg, db)


def snap(lat, lon, grid_m):
    """Centro della cella della griglia che contiene il punto: la posizione vera resta ignota
    entro grid_m metri. None se la posizione manca o non si pubblica."""
    if lat is None or lon is None or not grid_m or (lat == 0 and lon == 0):
        return None
    dlat = grid_m / 111320
    clat = (math.floor(lat / dlat) + 0.5) * dlat
    dlon = grid_m / (111320 * max(0.01, math.cos(math.radians(clat))))
    clon = (math.floor(lon / dlon) + 0.5) * dlon
    return [round(clat, 5), round(clon, 5)]


def build(core, now=None):
    """Il riassunto da pubblicare. Gira in un thread (legge il database).
    Le sezioni spente in Gestione → Dashboard non partono proprio."""
    st = settings(core.cfg, core.db)
    on = set(st["sections"])
    ns = core.netstats
    contacts = core.mesh.contacts() if core.mesh.connected else []
    ns._seed_if_empty(contacts)
    now = int(now or time.time())
    tracked = {(t.get(k) or "").strip().lower() for t in core.db.trackers() for k in ("contact", "label")} - {""}
    summary = ns.summary(now)
    trend = ns.trend(now, 30)

    nodes, types, hidden = [], {}, set(tracked)
    for n in ns.nodes(now):
        if (n.get("name") or "").strip().lower() in tracked:
            continue          # i dispositivi seguiti non compaiono mai
        if (n.get("type") or "") == "companion" and not st["companions"]:
            hidden |= {(n.get("name") or "").strip().lower(), n["key"][:8]}
        if n["last_seen"] < now - 30 * DAY:
            continue
        typ = n.get("type") or "sconosciuto"
        if n["last_seen"] >= now - DAY:
            types[typ] = types.get(typ, 0) + 1
        if typ == "companion" and not st["companions"]:
            continue
        nodes.append({"id": n["key"][:8], "name": n.get("name") or n["key"][:8], "type": typ,
                      "first_seen": n["first_seen"], "last_seen": n["last_seen"], "new": n["new"],
                      "adverts_7d": n["adverts_7d"], "hops": n["hops"], "snr": n["snr"],
                      "pos": snap(n.get("lat"), n.get("lon"), st["grid_m"]) if typ in WITH_POSITION and "map" in on else None})

    availability = [{k: a[k] for k in ("name", "pct_24h", "pct_7d", "pct_30d", "polls_30d", "online", "last_ok",
                                       "offline_since", "days", "hours")} for a in ns.availability(now)]
    battery = [{k: b[k] for k in ("name", "volts", "threshold_v", "trend", "slope_mv_day", "days_left", "solar",
                                  "swing_mv", "daily")} for b in ns.battery(now)]
    links = [{k: x[k] for k in ("kind", "from", "to", "last", "last_ts", "avg_7d", "avg_prev_7d", "delta", "min", "max",
                                "samples")} | {"points": _thin(x["points"], 60)}
             for x in ns.links(now, contacts) if x["last_ts"] >= now - 7 * DAY
             and not {str(x["from"]).strip().lower(), str(x["to"]).strip().lower()} & hidden]
    si = (core.mesh.self_info() or {}) if hasattr(core.mesh, "self_info") else {}
    # del gateway solo nome e parametri radio: la sua posizione (casa) non parte mai
    gateway = {k: si.get(k) for k in ("name", "radio_freq", "radio_bw", "radio_sf", "radio_cr", "tx_power")}
    history = [{"name": r["name"], "kind": "ripetitore", "points": series(core.db.repeater_history(r["name"], now - 7 * DAY), REP_KEYS)}
               for r in core.db.repeaters()]
    gw_hist = core.db.gateway_history(now - 7 * DAY)
    if gw_hist:
        history.append({"name": gateway["name"] or "Gateway", "kind": "gateway", "points": series(gw_hist, GW_KEYS)})
    if "nodes" not in on:     # senza elenco restano solo i nodi già sulla mappa
        nodes = [n for n in nodes if n["pos"]]
    if "system" not in on:
        gateway = {"name": gateway["name"]}
    out = {
        "v": 1, "generated": now, "app": f"MeshWatch {__version__}", "title": st["title"],
        "grid_m": st["grid_m"], "gateway": gateway, "sections": sorted(on),
        "summary": summary, "types_24h": types, "nodes": nodes[:1000],
    }
    if "traffic" in on:
        out["trend"] = trend
    if "repeaters" in on:
        out["availability"], out["battery"] = availability, battery
    if "compare" in on:
        out["compare"] = ns.compare(now)
    if "links" in on:
        out["links"] = links
    if "history" in on:
        out["history"] = history
    if "system" in on:
        out["firmware"] = [{"name": v["name"], "version": v["version"]} for v in core.firmware.repeater_versions()]
        out["system"] = system_info()
    return out


# contatori che crescono di continuo -> valore all'ora (o % del tempo per l'airtime)
REP_KEYS = {"rx_h": ("nb_recv",), "tx_h": ("nb_sent",), "flood_h": ("sent_flood",),
            "dups_h": ("flood_dups", "direct_dups"), "errors_h": ("recv_errors",),
            "air_tx": ("airtime",), "air_rx": ("rx_airtime",)}
GW_KEYS = {"rx_h": ("nb_recv",), "tx_h": ("nb_sent",), "flood_h": ("sent_flood",), "errors_h": ("recv_errors",),
           "air_tx": ("tx_air",), "air_rx": ("rx_air",)}


def _thin(pts, n):
    if len(pts) <= n:
        return [list(p) for p in pts]
    step = len(pts) / n
    return [list(pts[int(i * step)]) for i in range(n)] + [list(pts[-1])]


def series(hist, keys, most=168):
    """Storico di una radio per i grafici: batteria, rumore, SNR, RSSI e i contatori trasformati
    in velocità tra due letture (dopo un riavvio il contatore riparte da zero: quel tratto si salta)."""
    out = []
    for a, b in zip([None] + hist, hist):
        p = {"ts": b["ts"], "bat": b.get("bat_mv"), "noise": b.get("noise"), "snr": b.get("snr"), "rssi": b.get("rssi")}
        dt = b["ts"] - a["ts"] if a else 0
        reboot = a and a.get("uptime") is not None and b.get("uptime") is not None and b["uptime"] < a["uptime"]
        for k, cols in keys.items():
            va = sum(a.get(c) or 0 for c in cols) if a and all(a.get(c) is not None for c in cols[:1]) else None
            vb = sum(b.get(c) or 0 for c in cols) if all(b.get(c) is not None for c in cols[:1]) else None
            v = None
            if a and dt > 0 and not reboot and va is not None and vb is not None and vb >= va:
                v = round((vb - va) / dt * 100, 2) if k.startswith("air") else round((vb - va) / dt * 3600, 1)
            p[k] = v
        out.append(p)
    if len(out) > most:
        step = len(out) / most
        out = [out[int(i * step)] for i in range(most)] + [out[-1]]
    return out


def system_info():
    """Stato del Raspberry, senza il nome in rete."""
    try:
        from . import system
        i = system.info()
    except Exception:
        return {}
    return {k: i.get(k) for k in ("model", "temp_c", "uptime", "load", "mem_total_mb", "mem_free_mb",
                                  "disk_total_gb", "disk_free_gb", "undervoltage_now", "undervoltage_since_boot")}


class Publisher:
    def __init__(self, core, transport=None):
        self.core = core
        self.transport = transport       # per i test
        self.last = {"ok": None, "ts": None, "error": None, "bytes": None}
        self.wake = asyncio.Event()

    def settings(self):
        return settings(self.core.cfg, self.core.db)

    async def data(self):
        data = await asyncio.to_thread(build, self.core)
        if "system" in data["sections"]:
            try:
                if self.core.mesh.connected:
                    g = await asyncio.wait_for(self.core.mesh.gateway_info(), 15)
                    data["gateway"].update(model=g.get("model"), firmware=g.get("firmware"))
            except Exception:
                pass
        return data

    async def send_once(self):
        import httpx
        st = self.settings()
        if not st["url"] or not st["token"]:
            raise RuntimeError("mancano l'indirizzo o il token")
        data = await self.data()
        body = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(20, connect=10), transport=self.transport,
                                         headers={"User-Agent": f"MeshWatch/{__version__}"}) as client:
                r = await client.post(st["url"], content=body, headers={"Authorization": f"Bearer {st['token']}",
                                                                        "Content-Type": "application/json"})
            if r.status_code >= 300:
                raise RuntimeError({401: "token non accettato dal sito (401)", 404: "indirizzo non trovato (404)",
                                    413: "dati troppo grandi (413)"}.get(r.status_code, f"risposta {r.status_code}: {r.text[:120]}"))
        except httpx.HTTPError as e:
            raise RuntimeError(f"sito non raggiungibile: {e.__class__.__name__}") from e
        self.last = {"ok": True, "ts": int(time.time()), "error": None, "bytes": len(body)}
        return data

    async def loop(self):
        try:
            await asyncio.wait_for(self.wake.wait(), 90)   # la radio si collega e le statistiche leggono la rubrica
        except asyncio.TimeoutError:
            pass
        fails, announced = 0, None
        while True:
            self.wake.clear()
            st = self.settings()
            wait = 60
            if st["enabled"]:
                if announced != st["url"]:
                    log.info("Pubblicazione delle statistiche attiva: %s", st["url"])
                    announced = st["url"]
                try:
                    await self.send_once()
                    if fails:
                        log.info("Pubblicazione delle statistiche di nuovo riuscita")
                    fails = 0
                    wait = st["interval_min"] * 60
                except Exception as e:
                    fails += 1
                    self.last = {"ok": False, "ts": int(time.time()), "error": str(e)[:200], "bytes": None}
                    (log.warning if fails <= 3 else log.debug)("Pubblicazione delle statistiche non riuscita: %s", e)
                    wait = min(st["interval_min"] * 60, 60 * 2 ** min(fails, 4))   # 2, 4, 8, 16 minuti
            else:
                announced = None
            try:   # le impostazioni salvate dalla pagina svegliano il ciclo subito
                await asyncio.wait_for(self.wake.wait(), wait)
            except asyncio.TimeoutError:
                pass
