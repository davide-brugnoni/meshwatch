import asyncio
import logging
import math
import time
from datetime import datetime

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
        self.moving = {}             # tracker_id -> True se all'ultima posizione si stava spostando
        self.file_senders = []       # funzioni async(data, filename, caption) per i backup (Telegram)
        db.seed_from_config(cfg)

    # ---------- registro attività (pagina e Telegram)
    def audit(self, user, action, detail="", ip=""):
        try:
            self.db.add_audit(user, action, detail, ip)
        except Exception as e:
            log.error("Registro attività: %s", e)

    # ---------- avvisi
    async def notify(self, text):
        for n in self.notifiers:
            try:
                await n(text)
            except Exception as e:
                log.error("Notifica fallita: %s", e)

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
        """Chiede la posizione adesso. Ritorna il dict ricevuto o None."""
        t = self.db.tracker(tid)
        if not t or not self.mesh.connected:
            return None
        res = await self.mesh.request_telemetry(t["contact"])
        ok = res is not None
        self.db.add_tracker_poll(tid, ok)
        if ok:
            if res["lat"] is not None:
                prev = self.db.last_position(tid)
                self.db.add_position(tid, res["lat"], res["lon"], res["alt"], res["batt_v"])
                self._update_moving(t, prev, res)
            await self._tracker_ok(t, res)
        else:
            await self._tracker_fail(t)
        return res

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
            if res["batt_v"] < lim and "batt" not in f:
                f.add("batt")
                await self.notify(f"🔋 Batteria bassa su {t['label']}: {res['batt_v']:.2f} V")
            elif res["batt_v"] >= lim + 0.1:
                f.discard("batt")

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

    # ---------- ripetitori
    def repeater_password(self, name):
        r = self.db.repeater(name)
        if not r:
            raise ValueError(f"'{name}' non è tra i ripetitori configurati")
        return r["password"]

    async def poll_repeater(self, name):
        st = await self.mesh.request_status(name, self.repeater_password(name))
        self.db.add_repeater_status(name, st is not None, st)
        await self._repeater_alerts(name, st)
        return st

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
            if v < lim and "batt" not in f:
                f.add("batt")
                await self.notify(f"🔋 Batteria bassa sul ripetitore {name}: {v:.2f} V")
            elif v >= lim + 0.1:
                f.discard("batt")

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

    async def send_backup(self, user="automatico"):
        """Invia il backup con Telegram. Ritorna False se Telegram non è configurato."""
        if not self.file_senders:
            return False
        from . import maintenance
        data, name = await asyncio.to_thread(maintenance.make_backup, self.db, self.cfg.get("_config_path"))
        for send in self.file_senders:
            await send(data, name, "💾 Backup di MeshWatch (dati e config). Contiene password: conservalo con cura.")
        self.db.set("last_backup", int(time.time()))
        self.audit(user, "backup inviato su Telegram", name)
        return True

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
                    await self.send_backup()
            except Exception as e:
                log.exception("Errore manutenzione: %s", e)
            await asyncio.sleep(3600)

    # ---------- dati per interfacce
    def tracker_state(self, t, since):
        return {
            **t,
            "paused": bool(t["paused"]),
            "moving": self.moving.get(t["id"], False),
            "current_interval_min": self.interval_min(t),
            "last_ok": self.db.last_tracker_ok(t["id"]),
            "last_position": self.db.last_position(t["id"]),
            "track": self.db.positions_since(t["id"], since),
        }

    def state(self, hours=24):
        since = int(time.time()) - hours * 3600
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
            "trackers": [self.tracker_state(t, since) for t in self.db.trackers()],
            "repeaters": reps,
            "contacts": self.mesh.contacts(),
        }
