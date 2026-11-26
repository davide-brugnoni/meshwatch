"""Statistiche della rete MeshCore.

Raccoglie:
- ogni nodo sentito dal gateway (advert ricevuti: prima e ultima volta, salti, SNR, RSSI);
- i pacchetti sentiti dalla radio, contati per ora e per tipo (tenuti in memoria e scritti
  ogni pochi minuti, per non consumare la SD con una scrittura a ogni pacchetto);
- avvisi su Telegram per i nodi nuovi e per quelli che non si sentono più.

Calcola per la pagina Statistiche: andamento della rete, disponibilità dei ripetitori,
previsione della batteria, confronto tra ripetitori e qualità dei collegamenti nel tempo.
"""
import asyncio
import logging
import threading
import time
from datetime import date, datetime, timedelta

log = logging.getLogger("meshwatch.netstats")

# tipi di pacchetto MeshCore (header del pacchetto, bit 2-5)
PTYPES = {0: "Richieste", 1: "Risposte", 2: "Messaggi privati", 3: "Conferme (ACK)", 4: "Advert",
          5: "Messaggi nei canali", 6: "Dati nei canali", 7: "Login", 8: "Percorsi", 9: "Trace",
          10: "Multiparte", 11: "Controllo", 15: "Personalizzati"}
ADVERT = 4
GONE_MODES = ("off", "repeaters", "all")
DAY = 86400


def _day(ts):
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def _last_days(n):
    today = date.today()
    return [(today - timedelta(days=i)).isoformat() for i in range(n - 1, -1, -1)]


def _avg(v):
    v = [x for x in v if x is not None]
    return sum(v) / len(v) if v else None


def _thin(pts, n=240):
    """Al massimo n punti per i grafici (il Raspberry manda meno dati al telefono)."""
    if len(pts) <= n:
        return pts
    step = len(pts) / n
    return [pts[int(i * step)] for i in range(n - 1)] + [pts[-1]]


def counter_rates(history, get, uptime="uptime"):
    """Velocità media di un contatore che cresce di continuo: (somma degli incrementi, secondi).
    Dopo un riavvio del nodo il contatore riparte da zero: quel tratto si salta."""
    inc = secs = 0
    for a, b in zip(history, history[1:]):
        va, vb, dt = get(a), get(b), b["ts"] - a["ts"]
        if va is None or vb is None or dt <= 0 or vb < va:
            continue
        if a.get(uptime) is not None and b.get(uptime) is not None and b[uptime] < a[uptime]:
            continue
        inc += vb - va
        secs += dt
    return (inc, secs) if secs else (None, None)


def linear_slope(pts):
    """Pendenza (valore al giorno) della retta che passa meglio tra i punti [(ts, valore)]."""
    n = len(pts)
    if n < 2:
        return None
    xs = [p[0] / DAY for p in pts]
    ys = [p[1] for p in pts]
    mx, my = sum(xs) / n, sum(ys) / n
    den = sum((x - mx) ** 2 for x in xs)
    if not den:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den


class NetStats:
    FLUSH_S = 120          # ogni quanto si scrivono i contatori dei pacchetti e si legge la rubrica
    DUP_S = 600            # lo stesso advert ripetuto da più ripetitori arriva più volte

    def __init__(self, core):
        self.core = core
        self.db = core.db
        self.lock = threading.Lock()     # i contatori si leggono anche dal thread delle statistiche
        self.counts = {}                 # (ora, tipo) -> pacchetti non ancora scritti
        self.outbox = []                 # avvisi senza un ciclo asyncio (solo nei test)

    # ---------- impostazioni degli avvisi
    def alert_settings(self):
        mode = self.db.get("node_alert_gone", "off")
        try:
            hours = max(1, int(self.db.get("node_gone_h", 72) or 72))
        except ValueError:
            hours = 72
        return {"new": self.db.get("node_alert_new", "0") == "1",
                "gone": mode if mode in GONE_MODES else "off", "gone_h": hours}

    def set_alert_settings(self, new=None, gone=None, gone_h=None):
        if new is not None:
            self.db.set("node_alert_new", "1" if new else "0")
        if gone_h is not None:
            self.db.set("node_gone_h", int(gone_h))
        if gone is not None:
            self.db.set("node_alert_gone", gone)
        if gone is not None or gone_h is not None:
            # i nodi già spariti adesso non mandano una raffica di avvisi: vale da qui in poi
            st = self.alert_settings()
            self.db._exec("UPDATE nodes SET gone_notified=1 WHERE last_seen < ?",
                          (int(time.time()) - st["gone_h"] * 3600,))
        return self.alert_settings()

    def _notify(self, text):
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self.outbox.append(text)
            return
        task = loop.create_task(self.core.notify(text))
        self.core.tasks.add(task)
        task.add_done_callback(self.core.tasks.discard)

    def _gateway_key(self):
        si = self.core.mesh.self_info() if hasattr(self.core.mesh, "self_info") else {}
        return (si.get("public_key") or "").lower()

    # ---------- raccolta
    def record_rx(self, d, now=None):
        """Un pacchetto sentito dalla radio (vedi Mesh._on_rx_log)."""
        ptype = d.get("payload_type")
        if ptype is None or ptype < 0:
            return
        now = int(now or time.time())
        hour = now - now % 3600
        with self.lock:
            self.counts[(hour, ptype)] = self.counts.get((hour, ptype), 0) + 1
        if ptype == ADVERT and d.get("adv_key"):
            self.record_advert(d["adv_key"], name=d.get("adv_name"), type=d.get("adv_type"),
                               lat=d.get("adv_lat"), lon=d.get("adv_lon"), adv_ts=d.get("adv_timestamp"),
                               hops=d.get("path_len"), snr=d.get("snr"), rssi=d.get("rssi"), now=now)

    def record_advert(self, key, name=None, type=None, lat=None, lon=None, adv_ts=None,
                      hops=None, snr=None, rssi=None, now=None, seeded=False):
        key = (key or "").lower()
        if len(key) < 12 or key == self._gateway_key():   # il nostro advert ripetuto dai ripetitori
            return
        now = int(now or time.time())
        if lat is not None and lon is not None and lat == 0 and lon == 0:
            lat = lon = None
        if hops is not None and hops < 0:
            hops = None
        db = self.db
        row = db._one("SELECT * FROM nodes WHERE key=?", (key,))
        if row and adv_ts and row["last_adv_ts"] == adv_ts:
            # stesso advert arrivato per un'altra strada: tieni il percorso più corto e il segnale
            if now - (row["last_seen"] or 0) <= self.DUP_S:
                upd = {}
                if hops is not None and (row["hops"] is None or hops < row["hops"]):
                    upd["hops"] = hops
                if snr is not None and row["snr"] is None:
                    upd["snr"], upd["rssi"] = snr, rssi
                if upd:
                    db._exec(f"UPDATE nodes SET {','.join(k + '=?' for k in upd)} WHERE key=?", (*upd.values(), key))
                    db._exec(f"UPDATE node_adverts SET {','.join(k + '=?' for k in upd)} WHERE key=? AND ts=?",
                             (*upd.values(), key, row["last_seen"]))
            return
        first_run = db.get("nodes_since") is None
        if first_run:
            db.set("nodes_since", now)
        if not row:
            seeded = seeded or first_run
            db._exec("INSERT INTO nodes (key, name, type, first_seen, last_seen, adverts, last_adv_ts, lat, lon, "
                     "hops, snr, rssi, seeded) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (key, name, type, now, now, 1, adv_ts, lat, lon, hops, snr, rssi, 1 if seeded else 0))
            db._exec("INSERT INTO node_adverts VALUES (?,?,?,?,?)", (now, key, hops, snr, rssi))
            if not seeded and self.alert_settings()["new"]:
                self._notify(self._new_text(name or key[:8], type, hops, snr))
            return
        db._exec("UPDATE nodes SET name=COALESCE(?, name), type=COALESCE(?, type), last_seen=?, adverts=adverts+1, "
                 "last_adv_ts=?, lat=COALESCE(?, lat), lon=COALESCE(?, lon), hops=?, snr=?, rssi=?, gone_notified=0 "
                 "WHERE key=?", (name or None, type, now, adv_ts, lat, lon, hops, snr, rssi, key))
        db._exec("INSERT INTO node_adverts VALUES (?,?,?,?,?)", (now, key, hops, snr, rssi))
        if row["gone_notified"] and self._gone_applies(type or row["type"]):
            away = (now - (row["last_seen"] or now)) / 3600
            self._notify(f"📶 {name or row['name'] or key[:8]} di nuovo sentito dopo "
                         + (f"{away / 24:.0f} giorni." if away >= 48 else f"{away:.0f} ore."))

    @staticmethod
    def _new_text(name, type, hops, snr):
        bits = [type] if type else []
        if hops is not None:
            bits.append("diretto" if hops == 0 else f"{hops} salti")
        if snr is not None:
            bits.append(f"SNR {snr:.1f} dB")
        return f"🆕 Nuovo nodo sentito: {name}" + (f" ({', '.join(bits)})" if bits else "")

    def _gone_applies(self, type):
        mode = self.alert_settings()["gone"]
        return mode == "all" or (mode == "repeaters" and type == "ripetitore")

    def sync_contacts(self, contacts, now=None):
        """Rubrica della radio: un advert ricevuto quando il firmware non manda i pacchetti
        grezzi (o mentre il programma era fermo) cambia l'orario dell'ultimo advert del contatto.
        Al primo avvio i contatti già in rubrica entrano nello storico senza contare come nuovi."""
        now = int(now or time.time())
        first_run = self.db.get("nodes_since") is None
        known = {r["key"]: r for r in self.db._all("SELECT key, last_adv_ts FROM nodes")}
        for c in contacts:
            key = (c.get("public_key") or "").lower()
            adv = c.get("last_advert")
            if not key or not adv:
                continue
            r = known.get(key)
            if r and r["last_adv_ts"] == adv:
                continue
            if first_run or not r and adv < now - 7 * DAY:
                # già in rubrica (o sentito tanto tempo fa): entra nello storico senza contare un advert
                seen = adv if now - 365 * DAY < adv <= now else now
                self.db._exec("INSERT OR IGNORE INTO nodes (key, name, type, first_seen, last_seen, adverts, "
                              "last_adv_ts, lat, lon, hops, seeded) VALUES (?,?,?,?,?,0,?,?,?,?,1)",
                              (key, c.get("name"), c.get("type"), now, seen, adv, c.get("lat"), c.get("lon"),
                               c["hops"] if c.get("hops") is not None and c["hops"] >= 0 else None))
                continue
            self.record_advert(key, name=c.get("name"), type=c.get("type"), lat=c.get("lat"), lon=c.get("lon"),
                               adv_ts=adv, hops=c.get("hops"), now=now)
        if first_run:
            self.db.set("nodes_since", now)

    def check_gone(self, now=None):
        st = self.alert_settings()
        if st["gone"] == "off":
            return []
        now = int(now or time.time())
        sql = "SELECT * FROM nodes WHERE gone_notified=0 AND last_seen < ? AND last_seen >= ?"
        if st["gone"] == "repeaters":
            sql += " AND type='ripetitore'"
        rows = self.db._all(sql, (now - st["gone_h"] * 3600, now - 30 * DAY))
        for r in rows:
            self.db._exec("UPDATE nodes SET gone_notified=1 WHERE key=?", (r["key"],))
            self._notify(f"🔕 {r['name'] or r['key'][:8]}"
                         + (f" ({r['type']})" if r["type"] else "")
                         + f" non si sente da più di {st['gone_h']} ore.")
        return rows

    def flush(self):
        with self.lock:
            items, self.counts = self.counts, {}
        if not items:
            return 0
        with self.db.lock:
            self.db.conn.executemany(
                "INSERT INTO rx_hourly VALUES (?,?,?) ON CONFLICT(hour, ptype) DO UPDATE SET n=n+excluded.n",
                [(h, t, n) for (h, t), n in items.items()])
            self.db.conn.commit()
        return sum(items.values())

    async def loop(self):
        await asyncio.sleep(20)   # la radio nel frattempo si collega
        while True:
            try:
                if self.core.mesh.connected:
                    self.sync_contacts(self.core.mesh.contacts())
                self.check_gone()
                await asyncio.to_thread(self.flush)
            except Exception as e:
                log.exception("Statistiche della rete: %s", e)
            await asyncio.sleep(self.FLUSH_S)

    # ---------- calcoli per la pagina
    def _rx_rows(self, since):
        rows = self.db._all("SELECT hour, ptype, n FROM rx_hourly WHERE hour >= ?", (since - since % 3600,))
        with self.lock:
            pending = dict(self.counts)
        merged = {}
        for r in rows:
            merged[(r["hour"], r["ptype"])] = r["n"]
        for k, n in pending.items():
            if k[0] >= since - since % 3600:
                merged[k] = merged.get(k, 0) + n
        return merged

    def summary(self, now):
        st = self.alert_settings()
        one = lambda sql, *a: (self.db._one(sql, a) or {}).get("n") or 0   # noqa: E731
        rx = self._rx_rows(now - DAY)
        any_rx = bool(rx) or bool(self.db._one("SELECT 1 AS n FROM rx_hourly LIMIT 1"))
        return {
            "nodes": one("SELECT COUNT(*) AS n FROM nodes"),
            "active_24h": one("SELECT COUNT(*) AS n FROM nodes WHERE last_seen >= ?", now - DAY),
            "active_7d": one("SELECT COUNT(*) AS n FROM nodes WHERE last_seen >= ?", now - 7 * DAY),
            "new_7d": one("SELECT COUNT(*) AS n FROM nodes WHERE first_seen >= ? AND seeded=0", now - 7 * DAY),
            "gone": one("SELECT COUNT(*) AS n FROM nodes WHERE last_seen < ? AND last_seen >= ?",
                        now - st["gone_h"] * 3600, now - 30 * DAY),
            "adverts_24h": one("SELECT COUNT(*) AS n FROM node_adverts WHERE ts >= ?", now - DAY),
            "packets_24h": sum(rx.values()) if any_rx else None,
            "since": int(self.db.get("nodes_since") or 0) or None,
            "gone_h": st["gone_h"],
        }

    def nodes(self, now):
        rows = self.db._all("SELECT key, name, type, first_seen, last_seen, adverts, lat, lon, hops, snr, rssi, seeded "
                            "FROM nodes ORDER BY last_seen DESC LIMIT 1000")
        week = {r["key"]: r for r in self.db._all(
            "SELECT key, COUNT(*) AS n, AVG(snr) AS snr, MIN(hops) AS hops FROM node_adverts WHERE ts >= ? GROUP BY key",
            (now - 7 * DAY,))}
        for r in rows:
            w = week.get(r["key"]) or {}
            r["adverts_7d"] = w.get("n", 0)
            r["snr_7d"] = round(w["snr"], 1) if w.get("snr") is not None else None
            r["hops_min_7d"] = w.get("hops")
            r["new"] = not r.pop("seeded") and r["first_seen"] >= now - 7 * DAY
        return rows

    def trend(self, now, days):
        since = now - days * DAY
        per_day = {d: {"day": d, "nodes": 0, "adverts": 0, "new": 0} for d in _last_days(days)}
        for r in self.db._all("SELECT date(ts, 'unixepoch', 'localtime') AS d, COUNT(DISTINCT key) AS nodes, "
                              "COUNT(*) AS adverts FROM node_adverts WHERE ts >= ? GROUP BY d", (since,)):
            if r["d"] in per_day:
                per_day[r["d"]].update(nodes=r["nodes"], adverts=r["adverts"])
        for r in self.db._all("SELECT date(first_seen, 'unixepoch', 'localtime') AS d, COUNT(*) AS n FROM nodes "
                              "WHERE first_seen >= ? AND seeded=0 GROUP BY d", (since,)):
            if r["d"] in per_day:
                per_day[r["d"]]["new"] = r["n"]
        # ultime 48 ore, ora per ora: pacchetti sentiti e advert
        start = now - now % 3600 - 47 * 3600
        hours = {start + i * 3600: {"hour": start + i * 3600, "packets": 0, "adverts": 0} for i in range(48)}
        rx = self._rx_rows(start)
        for (h, _t), n in rx.items():
            if h in hours:
                hours[h]["packets"] += n
        for r in self.db._all("SELECT ts - ts % 3600 AS h, COUNT(*) AS n FROM node_adverts WHERE ts >= ? GROUP BY h", (start,)):
            if r["h"] in hours:
                hours[r["h"]]["adverts"] = r["n"]
        # mappa di calore giorno della settimana × ora: pacchetti medi all'ora
        heat_rx = self._rx_rows(since)
        source = "packets"
        if heat_rx:
            per_hour = {}
            for (h, _t), n in heat_rx.items():
                per_hour[h] = per_hour.get(h, 0) + n
        else:
            source = "adverts"
            per_hour = {r["h"]: r["n"] for r in self.db._all(
                "SELECT ts - ts % 3600 AS h, COUNT(*) AS n FROM node_adverts WHERE ts >= ? GROUP BY h", (since,))}
        tot = [[0] * 24 for _ in range(7)]
        cnt = [[0] * 24 for _ in range(7)]
        for h, n in per_hour.items():
            lt = time.localtime(h)
            tot[lt.tm_wday][lt.tm_hour] += n
            cnt[lt.tm_wday][lt.tm_hour] += 1
        heat = [[round(tot[d][h] / cnt[d][h], 1) if cnt[d][h] else None for h in range(24)] for d in range(7)]
        types = {}
        for (h, t), n in self._rx_rows(now - DAY).items():
            types[t] = types.get(t, 0) + n
        return {"days": list(per_day.values()), "hours": list(hours.values()),
                "heat": heat, "heat_source": source,
                "types": sorted(({"type": t, "label": PTYPES.get(t, f"Tipo {t}"), "n": n} for t, n in types.items()),
                                key=lambda x: -x["n"])}

    def availability(self, now):
        out = []
        today = _last_days(30)
        start48 = now - now % 3600 - 47 * 3600
        for rep in self.db.repeaters():
            rows = self.db._all("SELECT ts, ok FROM repeater_status WHERE name=? AND ts >= ? ORDER BY ts",
                                (rep["name"], now - 30 * DAY))

            def pct(since):
                sel = [r["ok"] for r in rows if r["ts"] >= since]
                return round(sum(sel) / len(sel) * 100, 1) if sel else None
            days = {d: [0, 0] for d in today}
            hours = {start48 + i * 3600: [0, 0] for i in range(48)}
            for r in rows:
                d = _day(r["ts"])
                if d in days:
                    days[d][0] += r["ok"]
                    days[d][1] += 1
                h = r["ts"] - r["ts"] % 3600
                if h in hours:
                    hours[h][0] += r["ok"]
                    hours[h][1] += 1
            last_ok = next((r["ts"] for r in reversed(rows) if r["ok"]), None)
            offline_since = None
            if rows and not rows[-1]["ok"]:
                fails = [r["ts"] for r in rows if not r["ok"] and (last_ok is None or r["ts"] > last_ok)]
                offline_since = fails[0] if fails else None
            out.append({"name": rep["name"], "pct_24h": pct(now - DAY), "pct_7d": pct(now - 7 * DAY),
                        "pct_30d": pct(now - 30 * DAY), "polls_30d": len(rows), "last_ok": last_ok,
                        "online": bool(rows and rows[-1]["ok"]), "offline_since": offline_since,
                        "days": [{"day": d, "ok": v[0], "total": v[1]} for d, v in days.items()],
                        "hours": [{"hour": h, "ok": v[0], "total": v[1]} for h, v in hours.items()]})
        return out

    def battery(self, now):
        thr = self.core.cfg.get("repeater_low_battery_v") or 3.4
        out = []
        for rep in self.db.repeaters():
            rows = self.db._all("SELECT ts, bat_mv FROM repeater_status WHERE name=? AND ok=1 AND bat_mv > 0 "
                                "AND ts >= ? ORDER BY ts", (rep["name"], now - 7 * DAY))
            pts = [(r["ts"], r["bat_mv"]) for r in rows]
            item = {"name": rep["name"], "threshold_v": thr, "points": _thin(pts),
                    "volts": pts[-1][1] / 1000 if pts else None, "slope_mv_day": None, "days_left": None,
                    "trend": None, "swing_mv": None, "solar": False, "daily": []}
            daily = {}
            for ts, mv in pts:
                d = daily.setdefault(_day(ts), [mv, mv])
                d[0], d[1] = min(d[0], mv), max(d[1], mv)
            item["daily"] = [{"day": d, "min": v[0], "max": v[1]} for d, v in daily.items()]
            span = pts[-1][0] - pts[0][0] if pts else 0
            if len(pts) >= 4 and span >= 12 * 3600:
                slope = linear_slope(pts)
                item["slope_mv_day"] = round(slope, 1) if slope is not None else None
                full = [v[1] - v[0] for d, v in daily.items() if d not in (_day(pts[0][0]), _day(now))]
                item["swing_mv"] = round(_avg(full)) if full else None
                item["solar"] = bool(item["swing_mv"] and item["swing_mv"] >= 100)
                if slope is not None:
                    item["trend"] = "giù" if slope < -3 else "su" if slope > 3 else "stabile"
                    cur = pts[-1][1]
                    if cur <= thr * 1000:
                        item["days_left"] = 0
                    elif slope < -3:
                        item["days_left"] = round((cur - thr * 1000) / -slope, 1)
            out.append(item)
        return out

    def compare(self, now):
        since = now - DAY
        rows = []

        def rate(hist, get, pct=False, uptime="uptime"):
            inc, secs = counter_rates(hist, get, uptime)
            if inc is None:
                return None
            return round(inc / secs * (100 if pct else 3600), 2 if pct else 1)

        def add(name, kind, hist, polls, ok, g):
            dups = rate(hist, lambda h: None if h.get("flood_dups") is None else
                        (h.get("flood_dups") or 0) + (h.get("direct_dups") or 0))
            rx = rate(hist, lambda h: h.get("nb_recv"))
            last = hist[-1] if hist else {}
            rows.append({
                "name": name, "kind": kind,
                "online_pct": round(ok / polls * 100, 1) if polls else None,
                "volts": last["bat_mv"] / 1000 if last.get("bat_mv") else None,
                "noise": _avg([h.get("noise") for h in hist]),
                "snr": _avg([h.get("snr") for h in hist]),
                "rssi": _avg([h.get("rssi") for h in hist]),
                "rx_h": rx, "tx_h": rate(hist, lambda h: h.get("nb_sent")),
                "flood_h": rate(hist, lambda h: h.get("sent_flood")),
                "dups_pct": round(dups / rx * 100, 1) if dups is not None and rx else None,
                "errors_h": rate(hist, lambda h: h.get("recv_errors")),
                "air_tx_pct": rate(hist, g["air_tx"], pct=True), "air_rx_pct": rate(hist, g["air_rx"], pct=True),
                "queue_max": max((h.get(g["queue"]) or 0 for h in hist), default=None) if hist else None,
                "uptime": last.get("uptime"), "samples": len(hist)})

        for rep in self.db.repeaters():
            hist = self.db.repeater_history(rep["name"], since)
            st = self.db._one("SELECT COUNT(*) AS n, SUM(ok) AS ok FROM repeater_status WHERE name=? AND ts >= ?",
                              (rep["name"], since)) or {}
            add(rep["name"], "ripetitore", hist, st.get("n") or 0, st.get("ok") or 0,
                {"air_tx": lambda h: h.get("airtime"), "air_rx": lambda h: h.get("rx_airtime"), "queue": "tx_queue"})
        gw = self.db.gateway_history(since)
        if gw:
            name = (self.core.mesh.self_info() or {}).get("name") if hasattr(self.core.mesh, "self_info") else None
            add(name or "Gateway", "gateway", gw, len(gw), len(gw),
                {"air_tx": lambda h: h.get("tx_air"), "air_rx": lambda h: h.get("rx_air"), "queue": "queue_len"})
        for r in rows:
            for k in ("noise", "snr", "rssi"):
                if r[k] is not None:
                    r[k] = round(r[k], 1)
        return rows

    def links(self, now, contacts=()):
        names = {r["key"]: r["name"] for r in self.db._all("SELECT key, name FROM nodes WHERE name IS NOT NULL")}
        for c in contacts:
            names.setdefault((c.get("public_key") or "").lower(), c.get("name"))

        def resolve(prefix):
            p = (prefix or "").lower()
            if not p:
                return None
            return next((n for k, n in names.items() if k.startswith(p)), None)

        groups = {}
        for r in self.db._all("SELECT ts, repeater, prefix, snr FROM link_history WHERE ts >= ? ORDER BY ts",
                              (now - 30 * DAY,)):
            g = groups.setdefault(("nb", r["repeater"], r["prefix"]), [])
            g.append((r["ts"], r["snr"]))
        gw_name = ((self.core.mesh.self_info() or {}).get("name") if hasattr(self.core.mesh, "self_info") else None) or "Gateway"
        for r in self.db._all("SELECT ts, key, snr FROM node_adverts WHERE hops=0 AND snr IS NOT NULL AND ts >= ? "
                              "ORDER BY ts", (now - 30 * DAY,)):
            groups.setdefault(("gw", gw_name, r["key"]), []).append((r["ts"], r["snr"]))
        out = []
        for (kind, a, b), pts in groups.items():
            recent = [v for t, v in pts if t >= now - 7 * DAY]
            prev = [v for t, v in pts if now - 14 * DAY <= t < now - 7 * DAY]
            avg7, avgp = _avg(recent), _avg(prev)
            out.append({"kind": "neighbour" if kind == "nb" else "gateway", "from": a,
                        "to": resolve(b) or b[:8], "prefix": b[:8], "last": pts[-1][1], "last_ts": pts[-1][0],
                        "avg_7d": round(avg7, 1) if avg7 is not None else None,
                        "avg_prev_7d": round(avgp, 1) if avgp is not None else None,
                        "delta": round(avg7 - avgp, 1) if avg7 is not None and avgp is not None else None,
                        "min": min(v for _, v in pts), "max": max(v for _, v in pts),
                        "samples": len(pts), "points": _thin(pts, 120)})
        out.sort(key=lambda x: (x["delta"] is None, x["delta"] if x["delta"] is not None else 0, -x["last_ts"]))
        return out

    def _seed_if_empty(self, contacts):
        """Storico vuoto (appena installato o subito dopo un riavvio, prima che il ciclo legga la
        rubrica): i contatti già in mano entrano subito, così /rete e la pagina non dicono «nessun nodo»."""
        if contacts and not self.db._one("SELECT 1 AS n FROM nodes LIMIT 1"):
            self.sync_contacts(contacts)

    # ---------- testo per Telegram (/rete e pulsante 📶 Rete)
    def telegram_text(self, contacts=(), now=None):
        self._seed_if_empty(contacts)
        now = int(now or time.time())
        s = self.summary(now)
        nodes = self.nodes(now)
        lines = ["📶 Rete – ultime 24 ore"]
        if not s["nodes"]:
            lines.append("Nessun nodo ancora nello storico (rubrica del gateway vuota): si riempie man mano "
                         "che il gateway sente gli advert. I ripetitori qui sotto si interrogano a parte.")
        else:
            lines.append(f"Nodi attivi {s['active_24h']} ({s['active_7d']} in 7 giorni) · conosciuti {s['nodes']}")
            lines.append(f"Advert {s['adverts_24h']}" + (f" · pacchetti {s['packets_24h']}" if s["packets_24h"] is not None else ""))
            label = lambda n: n["name"] or n["key"][:8]   # noqa: E731

            def names(items, fmt=label, most=8):
                out = ", ".join(fmt(n) for n in items[:most])
                return out + (f" (+{len(items) - most})" if len(items) > most else "")
            new = sorted((n for n in nodes if n["new"]), key=lambda n: -n["first_seen"])
            if new:
                lines.append(f"\n🆕 Nuovi (7 giorni): {names(new)}")
            gone = sorted((n for n in nodes if now - 30 * DAY <= n["last_seen"] < now - s["gone_h"] * 3600),
                          key=lambda n: -n["last_seen"])
            if gone:
                def away(n):
                    h = (now - n["last_seen"]) / 3600
                    return f"{label(n)} ({h / 24:.0f} giorni)" if h >= 48 else f"{label(n)} ({h:.0f} ore)"
                lines.append(f"🔕 Spariti (zitti da più di {s['gone_h']} ore): {names(gone, away)}")
            active = sorted((n for n in nodes if n["adverts_7d"]), key=lambda n: -n["adverts_7d"])
            if active:
                lines.append("🏆 Più attivi (7 giorni): " + ", ".join(f"{label(n)} {n['adverts_7d']}" for n in active[:3]))
        avail = {a["name"]: a for a in self.availability(now)}
        batt = {b["name"]: b for b in self.battery(now)}
        if avail:
            lines.append("\n📡 Ripetitori (disponibilità 7 giorni)")
        for name, a in avail.items():
            b = batt.get(name) or {}
            head = "🟢" if a["online"] else "🔴" if a["polls_30d"] else "⚪"
            parts = [f"{a['pct_7d']:.1f}%" if a["pct_7d"] is not None else "nessun dato"]
            if b.get("volts"):
                v = f"{b['volts']:.2f} V"
                if b.get("trend") == "giù":
                    v += f", scende {-b['slope_mv_day']:.0f} mV/giorno"
                    if b.get("days_left") == 0:
                        v += " → sotto la soglia"
                    elif b.get("days_left") is not None:
                        v += (" → soglia tra oltre un anno" if b["days_left"] > 365
                              else f" → soglia tra ~{b['days_left']:.0f} giorni" if b["days_left"] >= 2
                              else f" → soglia tra ~{b['days_left'] * 24:.0f} ore")
                elif b.get("trend") == "su":
                    v += ", in carica"
                elif b.get("trend") == "stabile":
                    v += " stabile"
                if b.get("solar"):
                    v += " ☀️"
                parts.append(v)
            if not a["online"] and a["offline_since"]:
                h = (now - a["offline_since"]) / 3600
                parts.append(f"non risponde da {h / 24:.0f} giorni" if h >= 48 else f"non risponde da {max(1, round(h))} ore")
            lines.append(f"{head} {name} · " + " · ".join(parts))
        worse = [x for x in self.links(now, contacts) if x["delta"] is not None and x["delta"] <= -1][:5]
        if worse:
            lines.append("\n⚠️ Collegamenti peggiorati (rispetto alla settimana prima)")
            lines += [f"{x['from']} → {x['to']} {x['delta']:+.1f} dB (ora {x['last']:.1f} dB)" for x in worse]
        lines.append("\nGrafici e dettagli: pagina → Statistiche")
        return "\n".join(lines)

    def all(self, days=30, contacts=()):
        """Tutto per la pagina. Gira in un thread: la rubrica (che vive nella radio) va letta prima."""
        self._seed_if_empty(contacts)
        now = int(time.time())
        days = max(7, min(int(days), 90))
        return {"generated": now, "days": days, "summary": self.summary(now), "nodes": self.nodes(now),
                "trend": self.trend(now, days), "availability": self.availability(now),
                "battery": self.battery(now), "compare": self.compare(now), "links": self.links(now, contacts),
                "alerts": self.alert_settings()}
