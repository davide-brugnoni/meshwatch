import asyncio
import json
import logging
import math
import time
from datetime import datetime, timedelta

from .events import Events

log = logging.getLogger("meshwatch.core")

# Comandi che cambiano la configurazione o riavviano: chiedono conferma.
DANGER_PREFIXES = ("reboot", "set ", "password", "erase", "start ota", "clear", "setperm", "gps sync",
                   "time ", "clock sync", "region ")


def is_dangerous(cmd):
    c = cmd.strip().lower()
    return any(c == p.strip() or c.startswith(p) for p in DANGER_PREFIXES)


def in_active_hours(spec):
    if not spec:
        return True
    try:
        a, b = spec.split("-")
        now = datetime.now().strftime("%H:%M")
        return a <= now <= b if a <= b else (now >= a or now <= b)
    except ValueError:
        return True


# Intervallo adattivo: se tra due posizioni il dispositivo si è spostato più di
# MOVE_M metri è "in movimento" e viene interrogato più spesso.
MOVE_M = 150


def distance_m(a, b):
    """Distanza in metri tra due punti {lat, lon} (formula dell'emisenoverso)."""
    r = 6371000
    p1, p2 = math.radians(a["lat"]), math.radians(b["lat"])
    dp, dl = p2 - p1, math.radians(b["lon"] - a["lon"])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


# Sensori Cayenne LPP: nome e unità per pagina, avvisi e Telegram
SENSORS = {
    "temperature": ("Temperatura", "°C", 1), "humidity": ("Umidità", "%", 0), "barometer": ("Pressione", "hPa", 0),
    "voltage": ("Tensione", "V", 2), "current": ("Corrente", "A", 3), "illuminance": ("Luce", "lux", 0),
    "percentage": ("Percentuale", "%", 0), "altitude": ("Quota", "m", 0), "power": ("Potenza", "W", 1),
    "energy": ("Energia", "kWh", 2), "distance": ("Distanza", "m", 2), "direction": ("Direzione", "°", 0),
    "concentration": ("Concentrazione", "ppm", 0), "load": ("Carico", "kg", 2), "frequency": ("Frequenza", "Hz", 0),
    "presence": ("Presenza", "", 0), "digital input": ("Ingresso digitale", "", 0),
    "digital output": ("Uscita digitale", "", 0), "analog input": ("Ingresso analogico", "", 2),
    "analog output": ("Uscita analogica", "", 2), "generic sensor": ("Sensore", "", 2), "switch": ("Interruttore", "", 0),
    "time": ("Ora", "s", 0), "accelerometer": ("Accelerometro", "g", 2), "gyrometer": ("Giroscopio", "°/s", 1),
}


def sensor_info(type, channel):
    """{label, unit, dec} di un sensore. La tensione sul canale 1 è la batteria del dispositivo."""
    base, _, field = type.partition(".")
    label, unit, dec = SENSORS.get(base, (base.capitalize(), "", 2))
    if base == "voltage" and channel == 1:
        label = "Batteria"
    if field:
        label += " " + field.replace("acc_", "").upper()
    return {"label": label, "unit": unit, "dec": dec}


def fmt_sensor(type, channel, value):
    i = sensor_info(type, channel)
    return f"{value:.{i['dec']}f}" + (f" {i['unit']}" if i["unit"] else "")


def parse_hhmm(text):
    """"21:00", "8.05" → "08:05"; None se non è un orario."""
    try:
        h, m = str(text).strip().replace(".", ":").split(":")
        h, m = int(h), int(m)
    except ValueError:
        return None
    return f"{h:02d}:{m:02d}" if 0 <= h < 24 and 0 <= m < 60 else None


def maps_link(lat, lon):
    return f"https://www.google.com/maps?q={lat:.6f},{lon:.6f}"


class Core:
    """Logica centrale: interrogazioni periodiche, storico, avvisi."""

    def __init__(self, cfg, db, mesh):
        self.cfg = cfg
        self.db = db
        self.mesh = mesh
        self.notifiers = []          # funzioni async(text) per gli avvisi (Telegram, ...)
        self.wakeup = asyncio.Event()
        self.next_due = {}           # tracker_id -> timestamp prossima richiesta
        self.alerts = {}             # chiave -> set di avvisi attivi
        self.batt_sent = {}          # chiave -> ultimo avviso di batteria bassa inviato
        self.moving = {}             # tracker_id -> True se all'ultima posizione si stava spostando
        self.file_senders = []       # funzioni async(data, filename, caption) per i backup (Telegram)
        self.tasks = set()           # invii della chat in corso
        self.events = Events()       # notifiche in tempo reale per le pagine aperte
        self.polls_inflight = {}     # tracker_id -> richiesta di posizione in corso (vedi poll_tracker)
        self.position_listeners = [] # funzioni async(tracker, posizione) per ogni nuova posizione (Telegram /segui)
        self.reporters = []          # funzioni async() che mandano il riepilogo a tutte le chat (Telegram)
        from .firmware import FirmwareManager
        self.firmware = FirmwareManager(self)   # aggiornamento del firmware del gateway, versioni dei ripetitori
        db.seed_from_config(cfg)
        mesh.on_message = self.on_message
        from .netstats import NetStats
        self.netstats = NetStats(self)          # storico dei nodi e statistiche della rete
        mesh.on_rx = self.netstats.record_rx
        # invii rimasti a metà quando il programma si è fermato
        db._exec("UPDATE messages SET status='failed' WHERE dir='out' AND status IN ('queued','sending','sent')")

    # ---------- registro attività (pagina e Telegram)
    def audit(self, user, action, detail="", ip=""):
        try:
            self.db.add_audit(user, action, detail, ip)
        except Exception as e:
            log.error("Registro attività: %s", e)

    # ---------- riepilogo giornaliero su Telegram
    def report_time(self):
        """Ora del riepilogo automatico ("" = spento). Impostata dalla pagina o dal bot,
        altrimenti telegram.daily_report del config."""
        v = self.db.get("tg_report_time")
        if v is None:
            v = parse_hhmm((self.cfg.get("telegram") or {}).get("daily_report") or "") or ""
        return v

    def set_report_time(self, when):
        """when = "HH:MM" per accenderlo, "" per spegnerlo (l'ultimo orario resta ricordato)."""
        if when:
            self.db.set("tg_report_prev", when)
        self.db.set("tg_report_time", when)

    def report_settings(self):
        t = self.report_time()
        return {"telegram": bool(self.reporters), "enabled": bool(t),
                "time": t or self.db.get("tg_report_prev") or "21:00"}

    # ---------- avvisi
    async def notify(self, text):
        for n in self.notifiers:
            try:
                await n(text)
            except Exception as e:
                log.error("Notifica fallita: %s", e)

    def battery_alert_settings(self):
        """Avvisi di batteria bassa: ogni quante ore ripeterli (0 = una volta sola, finché la
        batteria non risale) e fascia oraria in cui stare zitti ("22:00-07:00", vuoto = mai)."""
        try:
            repeat = max(0, int(self.db.get("batt_repeat_h", 0) or 0))
        except ValueError:
            repeat = 0
        return {"batt_repeat_h": repeat, "batt_quiet": self.db.get("batt_quiet", "") or ""}

    async def _battery_alert(self, key, v, lim, text):
        """Batteria sotto la soglia: avvisa subito, poi di nuovo ogni batt_repeat_h ore. Durante
        la fascia di silenzio non manda nulla: se la batteria è ancora bassa l'avviso arriva
        alla prima lettura dopo la fine della fascia. Risale di 0,1 V sopra la soglia: si riparte."""
        if v >= lim + 0.1:
            self.batt_sent.pop(key, None)
            return
        if v >= lim:
            return
        st = self.battery_alert_settings()
        if st["batt_quiet"] and in_active_hours(st["batt_quiet"]):
            return
        last = self.batt_sent.get(key)
        if last is not None and (not st["batt_repeat_h"] or time.time() - last < st["batt_repeat_h"] * 3600):
            return
        self.batt_sent[key] = time.time()
        await self.notify(text)

    def _flags(self, key):
        return self.alerts.setdefault(key, set())

    # ---------- tracker
    def trackers(self):
        return self.db.trackers()

    def schedule_now(self, tid=None):
        if tid is None:
            self.next_due.clear()
        else:
            self.next_due.pop(tid, None)
        self.wakeup.set()

    async def poll_tracker(self, tid):
        """Chiede la posizione adesso. Ritorna il dict ricevuto o None. Se una richiesta allo
        stesso dispositivo è già in corso (dalla pagina, da Telegram o dal ciclo) si aspetta il suo
        esito invece di mandarne un'altra via radio: niente doppioni nel duty cycle."""
        inflight = self.polls_inflight
        task = inflight.get(tid)
        if task is None:
            task = asyncio.ensure_future(self._poll_tracker(tid))
            inflight[tid] = task
            task.add_done_callback(lambda _t, tid=tid: inflight.pop(tid, None))
        res = await asyncio.shield(task)
        return dict(res) if res is not None else None

    async def _poll_tracker(self, tid):
        t = self.db.tracker(tid)
        if not t or not self.mesh.connected:
            return None
        last = self.db.last_position(tid)
        # in movimento o dopo una richiesta andata a vuoto il percorso salvato è probabilmente
        # quello del ripetitore di prima: si parte subito in flood invece di perdere un tentativo
        prev_poll = self.db.last_poll(tid)
        flood_first = self.moving.get(tid, False) or bool(prev_poll and not prev_poll["ok"])
        res = await self.mesh.request_telemetry(t["contact"], flood_first=flood_first)
        ok = res is not None
        # senza risposta si salva il percorso provato per primo, per capire dove si interrompe
        tried = getattr(self.mesh, "tried", {}).get(t["contact"], (None, None))
        self.db.add_tracker_poll(tid, ok, *((res.get("hops"), res.get("path")) if ok else tried),
                                 *self._poll_place(t, res, last))
        if ok:
            if res["lat"] is not None:
                prev = self.db.last_position(tid)
                self.db.add_position(tid, res["lat"], res["lon"], res["alt"], res["batt_v"])
                self._update_moving(t, prev, res)
                for listener in self.position_listeners:
                    try:
                        await listener(t, self.db.last_position(tid))
                    except Exception as e:
                        log.error("Posizione non inoltrata: %s", e)
            if res.get("sensors"):
                self.db.add_sensor_readings(tid, res["sensors"])
            self.events.publish("state")
            await self._tracker_ok(t, res)
            await self._sensor_alerts(t, res.get("sensors") or [])
        else:
            self.events.publish("state")
            await self._tracker_fail(t)
        return res

    COVERAGE_MAX_AGE_S = 3600   # oltre, l'ultima posizione nota non dice dove fosse il dispositivo

    def _poll_place(self, t, res, last):
        """Dove si trovava il dispositivo durante la richiesta, per la mappa della copertura:
        (lat, lon, stimata). Senza risposta o senza fix si usa l'ultima posizione nota, se recente."""
        if res and res.get("lat") is not None:
            return res["lat"], res["lon"], 0
        max_age = max(self.COVERAGE_MAX_AGE_S, 2 * self.interval_min(t) * 60)
        if last and time.time() - last["ts"] <= max_age:
            return last["lat"], last["lon"], 1
        return None, None, None

    def _update_moving(self, t, prev, res):
        was = self.moving.get(t["id"], False)
        now = bool(prev) and distance_m(prev, res) > MOVE_M
        self.moving[t["id"]] = now
        if now and not was and t.get("moving_interval_min"):
            # appena partito: la prossima richiesta arriva prima
            due = time.time() + t["moving_interval_min"] * 60
            if self.next_due.get(t["id"], due + 1) > due:
                self.next_due[t["id"]] = due

    def interval_min(self, t):
        """Intervallo in uso: quello "in movimento" se attivo e il dispositivo si sposta."""
        fast = t.get("moving_interval_min") or 0
        if fast and self.moving.get(t["id"]):
            return max(5, min(fast, t["interval_min"]))
        return max(5, t["interval_min"])

    async def _tracker_ok(self, t, res):
        f = self._flags(("t", t["id"]))
        if "offline" in f:
            f.discard("offline")
            msg = f"✅ {t['label']} di nuovo raggiungibile."
            if res["lat"] is not None:
                msg += f"\n{maps_link(res['lat'], res['lon'])}"
            await self.notify(msg)
        lim = t["low_battery_v"]
        if lim and res["batt_v"] is not None:
            await self._battery_alert(("t", t["id"]), res["batt_v"], lim,
                                      f"🔋 Batteria bassa su {t['label']}: {res['batt_v']:.2f} V")

    async def _sensor_alerts(self, t, sensors):
        """Soglie impostate dalla pagina: un avviso quando il valore supera la soglia, uno
        quando rientra. Senza lettura del sensore lo stato resta com'era."""
        f = self._flags(("t", t["id"]))
        values = {(x["type"], x["channel"]): x["value"] for x in sensors}
        for r in self.db.sensor_rules(t["id"]):
            v = values.get((r["type"], r["channel"]))
            if v is None:
                continue
            key = f"rule{r['id']}"
            out = v < r["value"] if r["op"] == "<" else v > r["value"]
            what = sensor_info(r["type"], r["channel"])["label"].lower()
            limit = f"{'sotto' if r['op'] == '<' else 'sopra'} {fmt_sensor(r['type'], r['channel'], r['value'])}"
            if out and key not in f:
                f.add(key)
                await self.notify(f"🌡️ {t['label']}: {what} {fmt_sensor(r['type'], r['channel'], v)} ({limit})")
            elif not out and key in f:
                f.discard(key)
                await self.notify(f"✅ {t['label']}: {what} di nuovo nella norma ({fmt_sensor(r['type'], r['channel'], v)})")

    def sensors_state(self, tid, since):
        """Ultimi valori dei sensori e storico per i grafici."""
        latest = [{**x, **sensor_info(x["type"], x["channel"]), "key": f"{x['type']}:{x['channel']}"}
                  for x in self.db.sensor_latest(tid)]
        history = {}
        for h in self.db.sensor_history(tid, since):
            history.setdefault(f"{h['type']}:{h['channel']}", []).append([h["ts"], h["value"]])
        return latest, history

    async def _tracker_fail(self, t):
        f = self._flags(("t", t["id"]))
        lim = t["offline_alert_min"] or 90
        last_ok = self.db.last_tracker_ok(t["id"])
        if "offline" in f or not last_ok:
            return
        if time.time() - last_ok > lim * 60:
            f.add("offline")
            msg = f"⚠️ {t['label']} non risponde da più di {lim} minuti."
            p = self.db.last_position(t["id"])
            if p:
                when = datetime.fromtimestamp(p["ts"]).strftime("%d/%m %H:%M")
                msg += f"\nUltima posizione ({when}):\n{maps_link(p['lat'], p['lon'])}"
            await self.notify(msg)

    def find_trackers(self, text):
        """Dispositivi che corrispondono a un nome scritto (es. su Telegram): prima uguale al nome
        o al contatto, poi che inizia così, poi che lo contiene. Maiuscole indifferenti."""
        q = (text or "").strip().lower()
        ts = self.db.trackers()
        if not q:
            return ts
        for test in (lambda n: n == q, lambda n: n.startswith(q), lambda n: q in n):
            found = [t for t in ts if test(t["label"].lower()) or test(t["contact"].lower())]
            if found:
                return found
        return []

    def tracker_stats(self, tid, since):
        """Statistiche del periodo: distanza, movimento, risposte, quota, batteria."""
        pts = self.db.positions_since(tid, since)
        polls = self.db._all("SELECT ok FROM tracker_polls WHERE tracker_id=? AND ts>=?", (tid, since))
        dist = moving_s = 0.0
        for a, b in zip(pts, pts[1:]):
            d = distance_m(a, b)
            if d > MOVE_M / 3:          # sotto ~50 m è l'errore del GPS, non uno spostamento
                dist += d
                if b["ts"] - a["ts"] <= 3 * 3600:
                    moving_s += b["ts"] - a["ts"]
        alts = [p["alt"] for p in pts if p.get("alt") is not None]
        batt = [p["batt_v"] for p in pts if p.get("batt_v") is not None]
        ok = sum(1 for p in polls if p["ok"])
        return {"points": len(pts), "km": round(dist / 1000, 2), "moving_h": round(moving_s / 3600, 1),
                "polls": len(polls), "ok": ok, "ok_pct": round(100 * ok / len(polls)) if polls else None,
                "alt_min": min(alts) if alts else None, "alt_max": max(alts) if alts else None,
                "batt_first": batt[0] if batt else None, "batt_last": batt[-1] if batt else None}

    # ---------- ripetitori
    def repeater_password(self, name):
        r = self.db.repeater(name)
        if not r:
            raise ValueError(f"'{name}' non è tra i ripetitori configurati")
        return r["password"]

    NEIGHBOURS_EVERY_S = 6 * 3600   # i vicini cambiano poco: non serve occupare la radio ogni ora

    async def poll_repeater(self, name):
        st = await self.mesh.request_status(name, self.repeater_password(name))
        self.db.add_repeater_status(name, st is not None, st)
        self.events.publish("state")
        await self._repeater_alerts(name, st)
        if st is not None:
            await self.poll_neighbours(name)
            if getattr(self.mesh, "cli", None):
                await self.firmware.poll_repeater_version(name)   # al massimo una volta al giorno
        return st

    async def poll_neighbours(self, name, force=False):
        """Vicini del ripetitore per la mappa della rete (al massimo ogni 6 ore)."""
        last = self.db.neighbours_ts(name)
        if not force and last and time.time() - last < self.NEIGHBOURS_EVERY_S:
            return None
        req = getattr(self.mesh, "request_neighbours", None)
        if not req:
            return None
        try:
            items = await req(name, self.repeater_password(name))
        except Exception as e:
            log.info("Vicini di %s non letti: %s", name, e)
            return None
        if items is not None:
            self.db.set_neighbours(name, items)
        return items

    async def record_gateway(self):
        """Statistiche del gateway nello storico, per i grafici in Gestione."""
        info = getattr(self.mesh, "gateway_info", None)
        if not info or not self.mesh.connected:
            return None
        try:
            g = await info()
        except Exception as e:
            log.info("Statistiche del gateway non lette: %s", e)
            return None
        if g and any(g.get(k) is not None for k in ("recv", "uptime", "noise_floor")):
            self.db.add_gateway_stats(g)
        return g

    async def poll_repeaters(self):
        results = {}
        for r in self.db.repeaters():
            if not self.mesh.connected:
                break
            results[r["name"]] = await self.poll_repeater(r["name"])
        return results

    async def _repeater_alerts(self, name, st):
        f = self._flags(("r", name))
        if st is None:
            last, good = self.db.repeater_latest(name)
            if good and time.time() - good["ts"] > 3 * 3600 and "offline" not in f:
                f.add("offline")
                await self.notify(f"📡 Il ripetitore {name} non risponde da più di 3 ore.")
            return
        if "offline" in f:
            f.discard("offline")
            await self.notify(f"📡 Ripetitore {name} di nuovo raggiungibile.")
        lim = self.cfg.get("repeater_low_battery_v")
        v = (st.get("bat") or 0) / 1000
        if lim and v:
            await self._battery_alert(("r", name), v, lim, f"🔋 Batteria bassa sul ripetitore {name}: {v:.2f} V")

    # ---------- terminale
    async def run_cli(self, name, cmd, user):
        cmd = cmd.strip()
        if not cmd or len(cmd) > 160:
            raise ValueError("comando vuoto o troppo lungo")
        if not self.mesh.connected:
            raise RuntimeError("radio gateway non collegata")
        reply = await self.mesh.cli(name, self.repeater_password(name), cmd)
        self.db.add_cli(user, name, cmd, reply)
        self.audit(user, "terminale", f"{name}: {cmd}")
        log.info("CLI %s @%s: %s -> %s", user, name, cmd, reply)
        return reply

    # ---------- chat
    MSG_RATE_PER_MIN = 10   # rispetto del duty cycle: niente raffiche di messaggi

    async def on_message(self, m):
        """Messaggio ricevuto dalla mesh: va nel database (anche a pagina chiusa)."""
        if self.db.is_duplicate_message(m["kind"], m["peer"], m["text"], m.get("sender_ts")):
            return
        self.db.add_message(dir="in", kind=m["kind"], peer=m["peer"], peer_name=m.get("peer_name"),
                            sender=m.get("sender"), text=m["text"], snr=m.get("snr"),
                            path_len=m.get("path_len"), sender_ts=m.get("sender_ts"), status="received")
        self.events.publish("chat")
        log.info("Messaggio ricevuto (%s %s)", m["kind"], m.get("peer_name"))

    def max_text_bytes(self, kind):
        from .mesh import MAX_MSG_BYTES
        if kind == "chan":
            name = (self.mesh.self_info() if hasattr(self.mesh, "self_info") else {}).get("name") or ""
            return MAX_MSG_BYTES - len(name.encode("utf-8")) - 2   # "Nome: " fa parte del testo
        return MAX_MSG_BYTES

    def send_chat(self, kind, peer, text, user):
        """Mette il messaggio in coda e ritorna subito l'id: l'invio (e l'attesa della
        conferma) avviene in background, la pagina segue lo stato."""
        from .mesh import text_bytes
        text = (text or "").strip()
        if not text:
            raise ValueError("messaggio vuoto")
        if "\n" in text or "\r" in text:
            text = " ".join(text.split())
        limit = self.max_text_bytes(kind)
        if text_bytes(text) > limit:
            raise ValueError(f"messaggio troppo lungo: massimo {limit} byte")
        if not self.mesh.connected:
            raise RuntimeError("radio gateway non collegata")
        if self.db.sent_since(int(time.time()) - 60) >= self.MSG_RATE_PER_MIN:
            raise OverflowError(f"massimo {self.MSG_RATE_PER_MIN} messaggi al minuto: aspetta un momento")
        if kind == "dm":
            c = self.mesh.contact_by_key(peer)
            if not c:
                raise ValueError("contatto non trovato nella rubrica del gateway")
            peer, name = c["public_key"][:12], c.get("adv_name", peer)
        elif kind == "chan":
            if not str(peer).isdigit():
                raise ValueError("canale non valido")
            idx = int(peer)
            if idx not in [ch["idx"] for ch in self.mesh.channels]:
                raise ValueError("canale non configurato nella radio")
            peer, name = str(idx), self.mesh.channel_name(idx)
        else:
            raise ValueError("tipo di conversazione non valido")
        mid = self.db.add_message(dir="out", kind=kind, peer=peer, peer_name=name, text=text,
                                  status="queued", user=user, read=1)
        self.audit(user, "messaggio inviato", f"{'canale ' if kind == 'chan' else 'a '}{name} ({text_bytes(text)} byte)")
        task = asyncio.create_task(self._send_chat(mid, kind, peer, text))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return mid

    async def _send_chat(self, mid, kind, peer, text):
        async def on_status(st, attempt=None, trip_ms=None):
            f = {"status": st}
            if attempt:
                f["attempts"] = attempt
            if trip_ms is not None:
                f["trip_ms"] = trip_ms
            self.db.update_message(mid, **f)
            self.events.publish("chat")
        try:
            if kind == "dm":
                await self.mesh.send_text(peer, text, on_status)
            else:
                await on_status("sending")
                ok = await self.mesh.send_channel(int(peer), text)
                await on_status("sent" if ok else "failed")
        except Exception as e:
            log.error("Invio messaggio %s: %s", mid, e)
            await on_status("failed")

    def chat_targets(self):
        """Con chi si può scrivere: canali della radio e contatti companion/room server."""
        chans = [{"kind": "chan", "peer": str(c["idx"]), "name": c["name"]} for c in self.mesh.channels]
        people = [{"kind": "dm", "peer": c["public_key"][:12], "name": c["name"], "type": c["type"]}
                  for c in self.mesh.contacts() if c["type"] in ("companion", "room server") and c.get("public_key")]
        return chans + people

    # ---------- percorso dei pacchetti
    def gateway_node(self):
        si = self.mesh.self_info() if hasattr(self.mesh, "self_info") else {}
        has_pos = si.get("adv_lat") or si.get("adv_lon")
        return {"name": si.get("name") or "Gateway", "public_key": si.get("public_key", ""),
                "lat": si.get("adv_lat") if has_pos else None, "lon": si.get("adv_lon") if has_pos else None}

    def resolve_path(self, path, contacts=None):
        """Prefissi dei ripetitori → [{hash, name, lat, lon, ambiguous}]. Con prefissi da un byte
        più ripetitori possono coincidere: si sceglie quello sentito più di recente."""
        contacts = self.mesh.contacts() if contacts is None else contacts
        out = []
        for h in path or []:
            cands = [c for c in contacts if c.get("public_key", "").lower().startswith(h.lower())]
            reps = [c for c in cands if c["type"] in ("ripetitore", "room server")] or cands
            best = max(reps, key=lambda c: c.get("last_advert") or 0) if reps else None
            out.append({"hash": h, "name": best["name"] if best else None,
                        "lat": best["lat"] if best else None, "lon": best["lon"] if best else None,
                        "ambiguous": len(reps) > 1})
        return out

    def route(self, tid, contacts=None, gateway=None):
        r = self.db.last_route(tid)
        if not r:
            return None
        return {"ts": r["ts"], "hops": r["hops"], "nodes": self.resolve_path(r["path"], contacts),
                "gateway": gateway or self.gateway_node()}

    # ---------- mappa della copertura
    def coverage(self, days=30, tid=None, cell_m=250):
        """Richieste raggruppate in celle di circa cell_m metri: risposte, richieste senza
        risposta e salti medi. Le richieste senza risposta stanno nell'ultima posizione nota."""
        polls = self.db.coverage_polls(int(time.time()) - days * 86400, tid)
        if not polls:
            return {"cells": [], "days": days, "cell_m": cell_m, "polls": 0}
        mid = sum(p["lat"] for p in polls) / len(polls)
        dlat = cell_m / 111320
        dlon = cell_m / (111320 * max(0.1, math.cos(math.radians(mid))))
        cells = {}
        for p in polls:
            k = (math.floor(p["lat"] / dlat), math.floor(p["lon"] / dlon))
            c = cells.setdefault(k, {"ok": 0, "fail": 0, "hops": [], "last": 0})
            c["ok" if p["ok"] else "fail"] += 1
            if p["ok"] and p["hops"] is not None and p["hops"] >= 0:
                c["hops"].append(p["hops"])
            c["last"] = max(c["last"], p["ts"])
        out = []
        for (i, j), c in cells.items():
            out.append({"bounds": [[i * dlat, j * dlon], [(i + 1) * dlat, (j + 1) * dlon]],
                        "ok": c["ok"], "fail": c["fail"], "last": c["last"],
                        "hops": round(sum(c["hops"]) / len(c["hops"]), 1) if c["hops"] else None})
        return {"cells": out, "days": days, "cell_m": cell_m, "polls": len(polls)}

    # ---------- mappa della rete
    def network(self):
        """Nodi con posizione, gateway e collegamenti (vicini dei ripetitori e nodi sentiti
        direttamente dal gateway)."""
        contacts = self.mesh.contacts()
        si = self.mesh.self_info() if hasattr(self.mesh, "self_info") else {}
        gw = None
        if si.get("adv_lat") or si.get("adv_lon"):
            gw = {"name": si.get("name"), "lat": si.get("adv_lat"), "lon": si.get("adv_lon"),
                  "public_key": si.get("public_key", "")}

        def find(prefix):
            prefix = (prefix or "").lower()
            if not prefix:
                return None
            if gw and gw["public_key"].lower().startswith(prefix):
                return gw
            return next((c for c in contacts if c["public_key"].lower().startswith(prefix)), None)

        links, seen = [], set()
        for n in self.db.neighbours():
            a = next((c for c in contacts if c["name"] == n["repeater"]), None)
            b = find(n["prefix"])
            if not a or not b or a["lat"] is None or b.get("lat") is None:
                continue
            key = tuple(sorted((a["public_key"], b["public_key"])))
            if key in seen:
                continue
            seen.add(key)
            links.append({"a": a["name"], "b": b["name"], "from": [a["lat"], a["lon"]], "to": [b["lat"], b["lon"]],
                          "snr": n["snr"], "secs_ago": n["secs_ago"], "ts": n["ts"], "kind": "neighbour"})
        if gw:
            for c in contacts:
                if c["hops"] == 0 and c["lat"] is not None and c["type"] == "ripetitore":
                    links.append({"a": gw["name"], "b": c["name"], "from": [gw["lat"], gw["lon"]],
                                  "to": [c["lat"], c["lon"]], "kind": "direct"})
        return {"gateway": gw, "nodes": [c for c in contacts if c["lat"] is not None], "links": links}

    # ---------- cicli periodici
    async def tracker_loop(self):
        """Un unico ciclo per tutti i tracker: interroga quelli scaduti, uno alla volta."""
        while True:
            now = time.time()
            for t in self.db.trackers():
                tid = t["id"]
                due = self.next_due.get(tid, 0)
                if now < due:
                    continue
                self.next_due[tid] = now + self.interval_min(t) * 60
                if t["paused"] or not in_active_hours(t["active_hours"]):
                    continue
                try:
                    await self.poll_tracker(tid)
                except Exception as e:
                    log.exception("Errore poll %s: %s", t["label"], e)
                # la richiesta può aver cambiato lo stato "in movimento"
                self.next_due[tid] = now + self.interval_min(t) * 60
            self.wakeup.clear()
            try:
                await asyncio.wait_for(self.wakeup.wait(), timeout=30)
            except asyncio.TimeoutError:
                pass

    async def repeater_loop(self):
        await asyncio.sleep(60)
        while True:
            try:
                await self.poll_repeaters()
                await self.record_gateway()
            except Exception as e:
                log.exception("Errore poll ripetitori: %s", e)
            await asyncio.sleep(int(self.cfg.get("repeater_interval_min", 60)) * 60)

    async def run_purge(self, user="automatico"):
        from . import maintenance
        days = maintenance.settings(self.db)["retention_days"]
        n = await asyncio.to_thread(maintenance.purge, self.db, days)
        self.db.set("last_purge", int(time.time()))
        self.db.set("last_purge_deleted", n)
        if n:
            self.audit(user, "pulizia dati", f"{n} righe più vecchie di {days} giorni")
        return n

    async def send_backup(self, user="automatico", kind=None):
        """Invia il backup con Telegram (kind: completo, configurazione, dati; di base quello scelto
        in Gestione). Ritorna False se Telegram non è configurato."""
        if not self.file_senders:
            return False
        from . import maintenance
        st = maintenance.settings(self.db)
        kind = kind or st["backup_kind"]
        data, name = await asyncio.to_thread(maintenance.make_backup, self.db, self.cfg.get("_config_path"), kind)
        what = maintenance.KIND_LABEL[kind]
        caption = (f"💾 Backup di MeshWatch: {what} 🔒 protetto da password: si apre con 7-Zip."
                   if st["backup_protected"]
                   else f"💾 Backup di MeshWatch: {what}. Contiene password: conservalo con cura.")
        for send in self.file_senders:
            await send(data, name, caption)
        self.db.set("last_backup", int(time.time()))
        self.audit(user, "backup inviato su Telegram", name)
        return True

    # ---------- radio: avviso e riconnessione se si scollega
    RADIO_ALERT_S = 120       # avviso dopo 2 minuti senza radio
    RADIO_RECONNECT_S = 180   # poi riconnessione completa (ritrova la porta USB)

    async def radio_watchdog(self, tick=20):
        down_since = alerted = None
        last_try = 0
        was = None
        while True:
            await asyncio.sleep(tick)
            now = time.time()
            if getattr(self.mesh, "suspended", False):   # aggiornamento del firmware: radio staccata apposta
                down_since = alerted = None
                continue
            if self.mesh.connected != was:
                was = self.mesh.connected
                self.events.publish("state")   # la pagina mostra subito radio collegata/scollegata
            if self.mesh.connected:
                if down_since:
                    mins = max(1, round((now - down_since) / 60))
                    log.info("Radio di nuovo collegata dopo %d min", mins)
                    if alerted:
                        self.audit("sistema", "radio ricollegata", f"dopo {mins} min")
                        await self.notify(f"📻 Radio gateway di nuovo collegata (era scollegata da {mins} min).")
                down_since = alerted = None
                continue
            down_since = down_since or now
            if not alerted and now - down_since >= self.RADIO_ALERT_S:
                alerted = True
                self.audit("sistema", "radio scollegata")
                await self.notify("📻 Radio gateway scollegata: niente posizioni né ripetitori finché non torna. "
                                  "Provo a ricollegarla da solo; se non torna controlla "
                                  + ("il WiFi della radio e l'alimentazione." if getattr(self.mesh, "tcp", None)
                                     else "il cavo USB e l'alimentazione."))
            if (now - down_since >= self.RADIO_RECONNECT_S and not self.mesh.connecting
                    and now - last_try >= self.RADIO_RECONNECT_S):
                last_try = now
                log.warning("Radio scollegata da %d s: riconnessione completa", now - down_since)
                asyncio.create_task(self.mesh.reconnect())

    # ---------- firmware MeshCore: avviso una volta per ogni versione nuova
    # controllo automatico delle release: gateway e ripetitori, ognuno col suo giorno e la sua ora
    FW_KINDS = ("gateway", "repeaters")
    FW_DAYS = ("lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica", "ogni giorno")
    FW_EVERY_DAY = 7
    FW_DEFAULT = {"enabled": True, "day": 6, "time": "09:00"}   # domenica alle 9

    def firmware_schedule(self):
        """{"gateway": {...}, "repeaters": {...}}, ognuno con enabled, day (0 = lunedì … 6 = domenica,
        7 = ogni giorno) e time "HH:MM". Impostato da Gestione → Rete, altrimenti domenica alle 9."""
        try:
            saved = json.loads(self.db.get("fw_schedule") or "{}")
        except ValueError:
            saved = {}
        out = {}
        for k in self.FW_KINDS:
            v = dict(self.FW_DEFAULT, **(saved.get(k) or {}))
            if not isinstance(v["day"], int) or not 0 <= v["day"] <= self.FW_EVERY_DAY:
                v["day"] = self.FW_DEFAULT["day"]
            v["time"] = parse_hhmm(str(v["time"])) or self.FW_DEFAULT["time"]
            v["enabled"] = bool(v["enabled"])
            out[k] = v
        return out

    def set_firmware_schedule(self, kind, enabled, day, when):
        sch = self.firmware_schedule()
        sch[kind] = {"enabled": bool(enabled), "day": int(day), "time": when}
        self.db.set("fw_schedule", json.dumps(sch))
        return sch

    def firmware_schedule_text(self, s):
        if not s["enabled"]:
            return "spento"
        day = "ogni giorno" if s["day"] == self.FW_EVERY_DAY else "ogni " + self.FW_DAYS[s["day"]]
        return f"{day} alle {s['time']}"

    @classmethod
    def firmware_slot(cls, s, now=None):
        """Giorno del controllo da fare adesso ("2026-11-29"), o None. Dall'ora scelta a mezzanotte:
        se a quell'ora la radio era scollegata o il servizio fermo, si fa appena torna."""
        if not s["enabled"]:
            return None
        now = now or datetime.now()
        h, m = map(int, s["time"].split(":"))
        sched = now.replace(hour=h, minute=m, second=0, microsecond=0)
        ok_day = s["day"] == cls.FW_EVERY_DAY or now.weekday() == s["day"]
        return sched.strftime("%Y-%m-%d") if ok_day and now >= sched else None

    @classmethod
    def firmware_next(cls, s, now=None):
        """Prossimo controllo (datetime), o None se spento."""
        if not s["enabled"]:
            return None
        now = now or datetime.now()
        h, m = map(int, s["time"].split(":"))
        for d in range(8):
            t = (now + timedelta(days=d)).replace(hour=h, minute=m, second=0, microsecond=0)
            if t > now and (s["day"] == cls.FW_EVERY_DAY or t.weekday() == s["day"]):
                return t
        return None

    def firmware_schedule_settings(self, now=None):
        sch = self.firmware_schedule()
        for k, s in sch.items():
            nxt = self.firmware_next(s, now)
            s["next"] = nxt.strftime("%Y-%m-%d %H:%M") if nxt else None
            s["text"] = self.firmware_schedule_text(s)
            s["last"] = self.db.get(f"fw_checked:{k}")
        return {"days": list(self.FW_DAYS), **sch}

    async def firmware_check(self, gateway=True, repeaters=True):
        fw = self.firmware
        notes = []
        st = await fw.status(force=True) if gateway else None
        if st and st["available"] and st["latest"]:
            key = f"gw:{st['latest']['version']}"
            if self.db.get("fw_notified_gw") != key:
                self.db.set("fw_notified_gw", key)
                how = ("Aggiornalo da Telegram con /firmware o da Gestione → Rete." if not st["problems"]
                       else "Da qui non si aggiorna: " + "; ".join(st["problems"]) + ".")
                notes.append(f"📦 Firmware MeshCore {st['latest']['version']} disponibile per il gateway "
                             f"({st['installed_version']} installato). {how}")
        for name, cur, new in (await fw.outdated_repeaters() if repeaters else []):
            key = f"rep_fw_notified:{name}"
            if self.db.get(key) != new:
                self.db.set(key, new)
                notes.append(f"📦 Ripetitore {name}: firmware {cur}, disponibile {new}. Si aggiorna sul posto: "
                             "/terminale → start ota (ESP32: WiFi del ripetitore; nRF52: app via Bluetooth).")
        for n in notes:
            await self.notify(n)
        return notes

    async def firmware_tick(self, now=None):
        """Controlli automatici del firmware all'orario scelto, una volta per giorno previsto.
        Restituisce i tipi controllati ("gateway", "repeaters")."""
        if not self.mesh.connected:
            return []
        done = []
        for kind, s in self.firmware_schedule().items():
            day = self.firmware_slot(s, now)
            if not day or self.db.get(f"fw_checked:{kind}") == day:
                continue
            await self.firmware_check(gateway=kind == "gateway", repeaters=kind == "repeaters")
            self.db.set(f"fw_checked:{kind}", day)   # solo se riuscito: altrimenti si riprova al giro dopo
            done.append(kind)
        return done

    async def firmware_watch(self, first_delay=900, tick=60):
        """Gateway e ripetitori all'orario scelto in Gestione → Rete (di base domenica alle 9)."""
        await asyncio.sleep(first_delay)
        while True:
            try:
                await self.firmware_tick()
            except Exception as e:
                log.info("Controllo del firmware non riuscito: %s", e)
            await asyncio.sleep(tick)

    # ---------- nuove versioni del programma
    async def update_watch(self, updater, every=12 * 3600, first_delay=600):
        """Controlla GitHub ogni 12 ore e avvisa una volta per ogni nuova versione."""
        await asyncio.sleep(first_delay)
        while True:
            try:
                st = await updater.check()
                latest = st.get("latest") or {}
                if st.get("available") and latest.get("sha") and latest["sha"] != self.db.get("update_notified"):
                    self.db.set("update_notified", latest["sha"])
                    how = ("Per installarla: /aggiornamento da una chat admin, oppure pagina → Gestione → "
                           "Aggiornamento programma → Aggiorna ora."
                           if not st.get("problems") else "Dalla pagina non si può aggiornare: vedi Gestione → Aggiornamento programma.")
                    await self.notify(f"🆕 Nuova versione di MeshWatch disponibile:\n{latest.get('message', '')}\n{how}")
            except Exception as e:
                log.info("Controllo aggiornamenti non riuscito: %s", e)
            await asyncio.sleep(every)

    async def maintenance_loop(self):
        """Una volta al giorno pulisce i dati vecchi; ogni settimana invia il backup (se attivo)."""
        from . import maintenance
        await asyncio.sleep(300)
        while True:
            try:
                st = maintenance.settings(self.db)
                now = time.time()
                if now - (st["last_purge"] or 0) > 86400:
                    await self.run_purge()
                if st["backup_telegram"] and now - (st["last_backup"] or 0) > maintenance.BACKUP_EVERY_DAYS * 86400:
                    try:
                        await self.send_backup()
                    except maintenance.BackupError as e:
                        if self.db.get("backup_error_day") != datetime.now().strftime("%Y-%m-%d"):   # un avviso al giorno
                            self.db.set("backup_error_day", datetime.now().strftime("%Y-%m-%d"))
                            await self.notify(f"💾 Backup settimanale non inviato: {e}")
            except Exception as e:
                log.exception("Errore manutenzione: %s", e)
            await asyncio.sleep(3600)

    # ---------- dati per interfacce
    def tracker_state(self, t, since, contacts=None, gateway=None):
        sensors, history = self.sensors_state(t["id"], since)
        return {
            "route": self.route(t["id"], contacts, gateway),
            "sensors": sensors,
            "sensor_history": history,
            **t,
            "paused": bool(t["paused"]),
            "moving": self.moving.get(t["id"], False),
            "current_interval_min": self.interval_min(t),
            "last_ok": self.db.last_tracker_ok(t["id"]),
            "last_position": self.db.last_position(t["id"]),
            "track": self.db.positions_since(t["id"], since),
        }

    def state(self, hours=24, contacts=None, gateway=None):
        """Tutto quello che serve alla pagina. Dalla pagina gira in un thread (solo database):
        contatti e gateway, che vivono nella radio, vanno letti prima e passati qui."""
        since = int(time.time()) - hours * 3600
        contacts = self.mesh.contacts() if contacts is None else contacts
        gateway = gateway or self.gateway_node()
        reps = []
        for r in self.db.repeaters():
            last, good = self.db.repeater_latest(r["name"])
            reps.append({
                "name": r["name"],
                "last_poll": last["ts"] if last else None,
                "last_ok": last["ok"] if last else None,
                "status": good,
                "history": self.db.repeater_history(r["name"], since),
            })
        return {
            "radio_connected": self.mesh.connected,
            "trackers": [self.tracker_state(t, since, contacts, gateway) for t in self.db.trackers()],
            "repeaters": reps,
            "contacts": contacts,
        }
