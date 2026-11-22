import asyncio
import logging
import secrets
import time
from datetime import datetime, timedelta

import httpx
from telegram.error import NetworkError, TimedOut
from telegram.request import HTTPXRequest
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, Update
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler,
                          filters)

from . import __version__, charts, maintenance, system
from .core import is_dangerous, maps_link

log = logging.getLogger("meshwatch.bot")

BTN_POS, BTN_LAST, BTN_TRACK, BTN_NODES = "📍 Posizione ora", "🕓 Ultima posizione", "🧭 Traccia oggi", "📡 Nodi"
BTN_MAP = "🗺 Mappa"
KEYBOARD = ReplyKeyboardMarkup(
    [[KeyboardButton(BTN_POS), KeyboardButton(BTN_LAST)],
     [KeyboardButton(BTN_MAP), KeyboardButton(BTN_TRACK)],
     [KeyboardButton(BTN_NODES)]],
    resize_keyboard=True)
NO_PIL = ("Per la mappa serve la libreria Pillow: aggiorna MeshWatch dalla pagina "
          "(Gestione → Aggiornamento programma) oppure installala con  venv/bin/pip install Pillow")


COMMANDS = [
    ("posizione", "chiede adesso la posizione (anche: /posizione nome)"),
    ("dove", "ultima posizione ricevuta di un dispositivo"),
    ("traccia", "posizioni delle ultime 12 ore"),
    ("segui", "posizione in diretta per qualche ora: /segui 2"),
    ("basta", "smette di mandare la posizione in diretta"),
    ("mappa", "mappa con la traccia: /mappa 7 per una settimana"),
    ("stats", "km, movimento e risposte: /stats 7 per una settimana"),
    ("sensori", "ultimi valori dei sensori"),
    ("percorso", "ripetitori attraversati dall'ultima risposta"),
    ("nodi", "stato di dispositivi e ripetitori"),
    ("aggiorna", "interroga subito i ripetitori"),
    ("sistema", "stato del Raspberry, della radio e del database"),
    ("riepilogo", "riepilogo della giornata; /riepilogo 21:00 ogni sera, /riepilogo no"),
    ("intervallo", "ogni quanti minuti chiedere la posizione: /intervallo 30"),
    ("pausa", "sospende il tracciamento"),
    ("riprendi", "riattiva il tracciamento"),
    ("advert", "il gateway si annuncia sulla mesh"),
    ("terminale", "comandi CLI ai ripetitori"),
    ("aiuto", "elenco dei comandi"),
]
FOLLOW_MAX_H = 8
REPORT_WINDOW_MIN = 30   # riepilogo automatico: se a quell'ora il servizio era fermo, lo manda entro 30 min


def thin(seq, n):
    """Al massimo n elementi a intervalli regolari, sempre con il primo e l'ultimo."""
    if len(seq) <= n:
        return list(seq)
    step = (len(seq) - 1) / (n - 1)
    return [seq[round(i * step)] for i in range(n)]


def parse_hhmm(text):
    try:
        h, m = text.strip().replace(".", ":").split(":")
        h, m = int(h), int(m)
    except ValueError:
        return None
    return f"{h:02d}:{m:02d}" if 0 <= h < 24 and 0 <= m < 60 else None


def period_text(days):
    return "ultime 24 ore" if days == 1 else f"ultimi {days} giorni"


class _Chat:
    """Chat raggiunta solo con l'ID (riepilogo automatico): stessi metodi della chat di un messaggio."""

    def __init__(self, bot, cid):
        self.bot, self.id = bot, cid

    async def send_message(self, text, **kw):
        return await self.bot.send_message(self.id, text, **kw)

    async def send_photo(self, photo, **kw):
        return await self.bot.send_photo(self.id, photo, **kw)

    async def send_action(self, action):
        return await self.bot.send_chat_action(self.id, action)


def fmt_ts(ts):
    if not ts:
        return "mai"
    d = datetime.fromtimestamp(ts)
    ago = int((time.time() - ts) / 60)
    ago_s = f"{ago} min fa" if ago < 120 else f"{ago // 60} ore fa"
    return f"{d.strftime('%d/%m %H:%M')} ({ago_s})"


def fmt_uptime(s):
    if not s:
        return "-"
    d, r = divmod(int(s), 86400)
    return f"{d}g {r // 3600}h"


class Bot:
    def __init__(self, cfg, core, tiles=None):
        self.core = core
        self.tiles = tiles   # cache delle mappe condivisa con la pagina (vedi __main__)
        self.report_default = parse_hhmm(str(cfg.get("daily_report") or "")) or ""
        self.report_task = None
        self.allowed = set(int(x) for x in cfg.get("allowed_chat_ids") or [])
        # chi può usare il terminale; se vuoto, tutte le chat autorizzate
        self.admins = set(int(x) for x in cfg.get("admin_chat_ids") or []) or self.allowed
        self.sessions = {}   # chat_id -> (nome ripetitore, scadenza)
        self.pending = {}    # token -> (chat_id, nodo, comando)
        self.live = {}       # (chat_id, tracker_id) -> (messaggio con la posizione in diretta, scadenza)
        # concurrent_updates: un comando che aspetta la radio non blocca gli altri
        self.app = (Application.builder().token(cfg["token"]).concurrent_updates(True)
                    .request(self._request()).get_updates_request(self._request(read_timeout=30))
                    .build())
        a = self.app
        a.add_handler(CommandHandler("start", self.start))
        a.add_handler(CommandHandler(["aiuto", "help"], self.help))
        # i comandi sui dispositivi accettano il nome: /posizione escursionista
        for cmd, action in (("posizione", "pos"), ("ultima", "last"), ("dove", "last"), ("traccia", "track"),
                            ("pausa", "pause"), ("riprendi", "resume"), ("sensori", "sens"), ("percorso", "route")):
            a.add_handler(CommandHandler(cmd, self._by_name(action)))
        a.add_handler(CommandHandler("stats", self.stats))
        a.add_handler(CommandHandler("mappa", self._with_days("map", 1)))
        a.add_handler(CommandHandler("sistema", self.system_cmd))
        a.add_handler(CommandHandler("riepilogo", self.report_cmd))

        a.add_handler(CommandHandler("segui", self.follow))
        a.add_handler(CommandHandler("basta", self.unfollow))
        a.add_handler(CommandHandler("intervallo", self.interval))
        a.add_handler(CommandHandler("nodi", self.nodes))
        a.add_handler(CommandHandler("aggiorna", self.refresh_nodes))
        a.add_handler(CommandHandler("advert", self.advert))
        a.add_handler(CommandHandler("terminale", self.term_start))
        a.add_handler(CommandHandler("fine", self.term_end))
        a.add_handler(MessageHandler(filters.Text([BTN_POS]), lambda u, c: self.pick(u, "pos")))
        a.add_handler(MessageHandler(filters.Text([BTN_LAST]), lambda u, c: self.pick(u, "last")))
        a.add_handler(MessageHandler(filters.Text([BTN_TRACK]), lambda u, c: self.pick(u, "track")))
        a.add_handler(MessageHandler(filters.Text([BTN_NODES]), self.nodes))
        a.add_handler(MessageHandler(filters.Text([BTN_MAP]), lambda u, c: self.pick(u, "map", "1")))
        a.add_handler(CallbackQueryHandler(self.on_button))
        a.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.term_text))
        a.add_error_handler(self.on_error)
        core.notifiers.append(self.broadcast)
        core.file_senders.append(self.send_file)
        core.position_listeners.append(self.on_position)

    @staticmethod
    def _request(read_timeout=10):
        """Connessione a Telegram solo via IPv4 (sul Raspberry IPv6 non funziona e faceva
        fallire alcune richieste) e con qualche nuovo tentativo automatico."""
        return HTTPXRequest(
            connect_timeout=10, read_timeout=read_timeout, write_timeout=10, pool_timeout=5,
            httpx_kwargs={"transport": httpx.AsyncHTTPTransport(local_address="0.0.0.0", retries=3)})

    async def on_error(self, update, ctx):
        e = ctx.error
        if isinstance(e, (NetworkError, TimedOut)):
            log.warning("Telegram non raggiungibile: %s", e)   # una riga, niente traceback
        else:
            log.error("Errore nel bot: %r", e, exc_info=e)

    async def run(self):
        await self.app.initialize()
        await self.app.start()
        # i comandi arrivati mentre il servizio era fermo vengono eseguiti,
        # tranne quelli più vecchi di 3 minuti (vedi self.ok)
        await self.app.updater.start_polling(drop_pending_updates=False)
        log.info("Bot Telegram attivo")
        self.report_task = asyncio.create_task(self.report_loop())
        try:   # menu dei comandi che Telegram mostra scrivendo "/"
            from telegram import BotCommand
            await self.app.bot.set_my_commands([BotCommand(c, d) for c, d in COMMANDS])
        except Exception as e:
            log.info("Menu dei comandi non impostato: %s", e)

    async def broadcast(self, text):
        """Avvisi: se la rete cade, riprova per qualche minuto invece di perderli."""
        for chat in self.allowed:
            for attempt in range(6):
                try:
                    await self.app.bot.send_message(chat, text, disable_web_page_preview=True)
                    break
                except (NetworkError, TimedOut) as e:
                    log.warning("Avviso non inviato (tentativo %d): %s", attempt + 1, e)
                    await asyncio.sleep(min(60, 10 * (attempt + 1)))

    async def send_file(self, data, filename, caption=""):
        """Backup: solo alle chat admin (contiene password)."""
        from telegram import InputFile
        for chat in self.admins:
            for attempt in range(4):
                try:
                    await self.app.bot.send_document(chat, InputFile(data, filename=filename), caption=caption,
                                                     read_timeout=60, write_timeout=60)
                    break
                except (NetworkError, TimedOut) as e:
                    log.warning("Backup non inviato (tentativo %d): %s", attempt + 1, e)
                    await asyncio.sleep(30 * (attempt + 1))

    def ok(self, update: Update):
        if not (update.effective_chat and update.effective_chat.id in self.allowed):
            return False
        msg = update.effective_message
        if msg and msg.date and time.time() - msg.date.timestamp() > 180:
            return False   # comando vecchio, arrivato mentre il servizio era fermo
        return True

    def is_admin(self, update: Update):
        return update.effective_chat and update.effective_chat.id in self.admins

    # ---------- scelta del dispositivo
    def _by_name(self, action):
        async def handler(update, ctx):
            await self.pick(update, action, name=" ".join(ctx.args or []))
        return handler

    async def pick(self, update: Update, action, arg="", name=""):
        """Esegue l'azione sul dispositivo indicato per nome; se non c'è il nome e i dispositivi
        sono più di uno, chiede quale con dei pulsanti."""
        if not self.ok(update):
            return
        ts = self.core.find_trackers(name)
        if not self.core.trackers():
            await update.message.reply_text("Nessun dispositivo da tracciare. Aggiungilo dalla pagina web → Gestione.")
            return
        if not ts:
            names = ", ".join(t["label"] for t in self.core.trackers())
            await update.message.reply_text(f"Nessun dispositivo si chiama \"{name}\". Dispositivi: {names}.")
            return
        if len(ts) == 1:
            await self.do(update.effective_chat, action, ts[0]["id"], arg)
            return
        kb = [[InlineKeyboardButton(t["label"], callback_data=f"{action}:{t['id']}:{arg}")] for t in ts]
        if action in ("pos", "last", "stats", "sens", "map") and not name:
            kb.append([InlineKeyboardButton("Tutti", callback_data=f"{action}:all:{arg}")])
        await update.message.reply_text("Quale dispositivo?", reply_markup=InlineKeyboardMarkup(kb))

    async def do(self, chat, action, tid, arg=""):
        if tid == "all" and action == "map":   # una sola mappa con tutti
            await self.send_map(chat, None, int(arg or 1))
            return
        if tid == "all":
            for t in self.core.trackers():
                await self.do(chat, action, t["id"], arg)
            return
        t = self.core.db.tracker(int(tid))
        if not t:
            await chat.send_message("Dispositivo non più presente.")
            return
        if action == "pos":
            await self.position_now(chat, t)
        elif action == "last":
            await self.last_position(chat, t)
        elif action == "track":
            await self.track(chat, t)
        elif action == "stats":
            await chat.send_message(self.stats_text(t, int(arg or 1)))
        elif action == "sens":
            await chat.send_message(self.sensors_text(t))
        elif action == "route":
            await chat.send_message(self.route_text(t))
        elif action == "map":
            await self.send_map(chat, t, int(arg or 1))
        elif action == "follow":
            await self.follow_start(chat, t, int(arg or 2))
        elif action == "pause":
            self.core.db.update_tracker(t["id"], paused=1)
            self.core.audit(f"telegram:{chat.id}", "pausa tracciamento", t["label"])
            await chat.send_message(f"⏸ Tracciamento di {t['label']} sospeso.")
        elif action == "resume":
            self.core.db.update_tracker(t["id"], paused=0)
            self.core.schedule_now(t["id"])
            self.core.audit(f"telegram:{chat.id}", "ripresa tracciamento", t["label"])
            await chat.send_message(f"▶️ Tracciamento di {t['label']} riattivato.")
        elif action == "interval":
            m = max(5, min(int(arg), 720))
            self.core.db.update_tracker(t["id"], interval_min=m)
            self.core.audit(f"telegram:{chat.id}", "intervallo", f"{t['label']}: {m} min")
            self.core.next_due[t["id"]] = time.time() + m * 60
            await chat.send_message(f"{t['label']}: posizione ogni {m} minuti.")

    # ---------- statistiche, sensori, percorso
    def stats_text(self, t, days=1):
        days = max(1, min(days, 365))
        st = self.core.tracker_stats(t["id"], int(time.time()) - days * 86400)
        period = "ultime 24 ore" if days == 1 else f"ultimi {days} giorni"
        lines = [f"📊 {t['label']} – {period}"]
        if st["points"]:
            lines.append(f"Percorsi {st['km']:.1f} km · in movimento circa {st['moving_h']:.1f} h · {st['points']} posizioni")
        else:
            lines.append("Nessuna posizione nel periodo.")
        if st["polls"]:
            lines.append(f"Risposte {st['ok']} su {st['polls']} richieste ({st['ok_pct']}%)")
        if st["alt_max"] is not None:
            lines.append(f"Quota da {st['alt_min']:.0f} a {st['alt_max']:.0f} m")
        if st["batt_last"] is not None:
            lines.append(f"Batteria {st['batt_first']:.2f} → {st['batt_last']:.2f} V")
        return "\n".join(lines)

    def sensors_text(self, t):
        from .core import fmt_sensor
        sensors, _ = self.core.sensors_state(t["id"], int(time.time()))
        if not sensors:
            return f"{t['label']} non ha ancora mandato valori dei sensori."
        lines = [f"🌡️ Sensori di {t['label']}"]
        for x in sensors:
            lines.append(f"{x['label']}: {fmt_sensor(x['type'], x['channel'], x['value'])} ({fmt_ts(x['ts'])})")
        return "\n".join(lines)

    def route_desc(self, t, hops, path):
        """Percorso in parole: diretto, in flood o gateway → ripetitori → nodo."""
        if hops is None or hops < 0:
            return "percorso sconosciuto: la richiesta è stata diffusa in flood"
        nodes = self.core.resolve_path(path)
        if hops == 0 or not nodes:
            return "diretto: il gateway lo sente senza ripetitori"
        names = [(n["name"] or n["hash"]) + ("?" if n["ambiguous"] else "") for n in nodes]
        gw = self.core.gateway_node()["name"]
        return f"{hops} salt{'o' if hops == 1 else 'i'}: {gw} → " + " → ".join(names) + f" → {t['label']}"

    def route_text(self, t):
        r = self.core.route(t["id"])
        if not r:
            return f"Nessun percorso registrato per {t['label']}."
        return f"🛰 Percorso di {t['label']} ({fmt_ts(r['ts'])})\n{self.route_desc(t, r['hops'], [n['hash'] for n in r['nodes']])}"

    @staticmethod
    def _days_args(ctx, default):
        """/comando [giorni] [nome] → (giorni, nome)."""
        args = list(ctx.args or [])
        days = int(args.pop(0)) if args and args[0].isdigit() else default
        return max(1, min(days, 365)), " ".join(args)

    def _with_days(self, action, default):
        async def handler(update, ctx):
            days, name = self._days_args(ctx, default)
            await self.pick(update, action, str(days), name)
        return handler

    async def stats(self, update: Update, ctx):
        days, name = self._days_args(ctx, 1)
        await self.pick(update, "stats", str(days), name)

    # ---------- posizione in diretta (live location di Telegram)
    async def follow(self, update: Update, ctx):
        args = list(ctx.args or [])
        hours = int(args.pop(0)) if args and args[0].isdigit() else 2
        await self.pick(update, "follow", str(max(1, min(hours, FOLLOW_MAX_H))), " ".join(args))

    async def follow_start(self, chat, t, hours):
        p = self.core.db.last_position(t["id"])
        if not p:
            await chat.send_message(f"Nessuna posizione di {t['label']} da condividere.")
            return
        old = self.live.pop((chat.id, t["id"]), None)
        if old:
            await self._stop_live(old[0])
        msg = await chat.send_location(latitude=p["lat"], longitude=p["lon"], live_period=hours * 3600)
        self.live[(chat.id, t["id"])] = (msg, time.time() + hours * 3600)
        self.core.audit(f"telegram:{chat.id}", "posizione in diretta", f"{t['label']}: {hours} h")
        await chat.send_message(
            f"📡 Posizione in diretta di {t['label']} per {hours} or{'a' if hours == 1 else 'e'}: si sposta a ogni nuova "
            f"posizione (ogni {self.core.interval_min(t)} min). /basta per smettere.")

    async def on_position(self, t, p):
        for (chat_id, tid), (msg, until) in list(self.live.items()):
            if tid != t["id"]:
                continue
            if time.time() > until:
                self.live.pop((chat_id, tid), None)
                continue
            try:
                await msg.edit_live_location(latitude=p["lat"], longitude=p["lon"])
            except Exception as e:
                if "not modified" not in str(e).lower():
                    log.info("Posizione in diretta terminata (%s): %s", t["label"], e)
                    self.live.pop((chat_id, tid), None)

    @staticmethod
    async def _stop_live(msg):
        try:
            await msg.stop_live_location()
        except Exception:
            pass   # già scaduta o fermata da Telegram

    async def unfollow(self, update: Update, ctx):
        if not self.ok(update):
            return
        chat = update.effective_chat.id
        mine = [k for k in self.live if k[0] == chat]
        for k in mine:
            await self._stop_live(self.live.pop(k)[0])
        await update.message.reply_text("Posizione in diretta fermata." if mine else "Nessuna posizione in diretta attiva.")

    # ---------- mappa (immagine)
    async def _photo(self, chat, data, caption):
        await chat.send_photo(photo=data, caption=caption[:1024], read_timeout=60, write_timeout=60)

    async def _busy(self, chat):
        try:   # "sta inviando una foto…": disegnare sul Raspberry richiede qualche secondo
            await chat.send_action("upload_photo")
        except Exception:
            pass

    async def _get_tile(self, z, x, y):
        if self.tiles is None:
            from .tiles import TileCache
            self.tiles = TileCache(self.core.db)
        data, _, _ = await self.tiles.get("osm", z, x, y, force=True)
        return data

    def _color(self, t):
        ids = [x["id"] for x in self.core.trackers()]
        i = ids.index(t["id"]) if t["id"] in ids else 0
        return t.get("color") or charts.PALETTE[i % len(charts.PALETTE)]

    def _net_markers(self, fit=False):
        """Gateway e ripetitori con posizione. Non allargano la mappa (fit=False): compaiono
        solo se stanno nella zona della traccia."""
        out = []
        try:
            gw = self.core.gateway_node()
            if gw.get("lat") is not None:
                out.append({"lat": gw["lat"], "lon": gw["lon"], "color": "#3ccf8e", "shape": "square",
                            "size": 7, "label": gw["name"], "fit": fit})
            for c in self.core.mesh.contacts():
                if c.get("type") == "ripetitore" and (c.get("lat") or c.get("lon")):
                    out.append({"lat": c["lat"], "lon": c["lon"], "color": "#f2c94c", "shape": "diamond",
                                "size": 7, "label": c["name"], "fit": fit})
        except Exception as e:
            log.info("Nodi della rete non disponibili per la mappa: %s", e)
        return out

    async def send_map(self, chat, t, days=1):
        """Mappa con la traccia del periodo e l'ultima posizione; t=None: tutti i dispositivi."""
        ts = [t] if t else self.core.trackers()
        if not charts.available():
            for x in ts:
                await self.track(chat, x)
            await chat.send_message(NO_PIL)
            return
        await self._busy(chat)
        since = int(time.time()) - days * 86400
        tracks, markers, lines = [], [], []
        for x in ts:
            pts = self.core.db.positions_since(x["id"], since)
            last = self.core.db.last_position(x["id"])
            color = self._color(x)
            if pts:
                tracks.append({"color": color, "points": thin([(p["lat"], p["lon"]) for p in pts], 1500)})
            if last:
                markers.append({"lat": last["lat"], "lon": last["lon"], "color": color, "size": 9, "bold": True,
                                "label": f"{x['label']} {datetime.fromtimestamp(last['ts']).strftime('%H:%M')}"})
                st = self.core.tracker_stats(x["id"], since)
                lines.append(f"{x['label']}: " + (f"{st['km']:.1f} km, {st['points']} posizioni" if pts
                                                  else "nessuna posizione nel periodo")
                             + f"\n  ultima {fmt_ts(last['ts'])} {maps_link(last['lat'], last['lon'])}")
        if not markers:
            await chat.send_message("Nessuna posizione registrata" + (f" per {t['label']}." if t else "."))
            return
        img = await charts.static_map(self._get_tile, tracks, markers + self._net_markers())
        title = f"🗺 {t['label'] if t else 'Tutti i dispositivi'} – {period_text(days)}"
        await self._photo(chat, img, title + "\n" + "\n".join(lines))

    # ---------- sistema
    def system_text(self):
        i = system.info()
        lines = ["🖥 Sistema"]
        lines.append(" · ".join(x for x in (i.get("model"), i.get("hostname")) if x))
        row = []
        if i.get("temp_c") is not None:
            row.append(f"temperatura {i['temp_c']:.0f} °C" + (" 🔥" if i["temp_c"] >= 75 else ""))
        if i.get("load") is not None:
            row.append(f"carico {i['load']}")
        if i.get("uptime"):
            row.append(f"acceso da {fmt_uptime(i['uptime'])}")
        if row:
            lines.append(", ".join(row).capitalize())
        if i.get("mem_total_mb"):
            lines.append(f"Memoria libera {i['mem_free_mb']} su {i['mem_total_mb']} MB")
        lines.append(f"Disco libero {i['disk_free_gb']} su {i['disk_total_gb']} GB"
                     + (" ⚠️" if i["disk_free_gb"] < 1 else ""))
        if i.get("undervoltage_now"):
            lines.append("⚠️ Tensione bassa ADESSO: alimentatore o cavo insufficienti")
        elif i.get("undervoltage_since_boot"):
            lines.append("⚠️ Tensione bassa dall'ultimo avvio: controlla alimentatore e cavo")
        gw = self.core.gateway_node()
        lines.append(f"\n📻 Radio gateway {gw['name']}: {'collegata' if self.core.mesh.connected else 'NON collegata'}")
        try:
            d = maintenance.db_info(self.core.db)
            size = f"{d['db_size'] / 2**20:.1f} MB · " if d["db_size"] else ""
            since = f" dal {datetime.fromtimestamp(d['oldest']).strftime('%d/%m/%Y')}" if d["oldest"] else ""
            lines.append(f"💾 Database {size}{d['positions']} posizioni{since}")
            st = maintenance.settings(self.core.db)
            lines.append(f"Ultimo backup: {fmt_ts(st['last_backup'])}")
        except Exception as e:
            log.info("Informazioni sul database non disponibili: %s", e)
        if self.tiles is not None:
            ti = self.tiles.info()
            lines.append(f"🗺 Cache mappe: {ti['tiles']} tessere, {ti['size'] / 2**20:.0f} MB"
                         + ("" if ti["enabled"] else " (disattivata per la pagina)"))
        lines.append(f"MeshWatch {__version__}")
        return "\n".join(lines)

    async def system_cmd(self, update: Update, ctx):
        if self.ok(update):
            await update.message.reply_text(self.system_text())

    # ---------- riepilogo giornaliero
    def report_time(self):
        """Ora del riepilogo automatico ("" = disattivato). Impostata da Telegram, altrimenti dal config."""
        v = self.core.db.get("tg_report_time")
        return self.report_default if v is None else v

    def report_text(self):
        now = int(time.time())
        lines = [f"☀️ Riepilogo MeshWatch – {datetime.now().strftime('%d/%m %H:%M')}"]
        if not self.core.mesh.connected:
            lines.append("⚠️ Radio gateway NON collegata")
        for t in self.core.trackers():
            st = self.core.tracker_stats(t["id"], now - 86400)
            last = self.core.db.last_position(t["id"])
            parts = [f"{st['km']:.1f} km" if st["points"] else "nessuna posizione"]
            if st["polls"]:
                parts.append(f"risposte {st['ok']}/{st['polls']} ({st['ok_pct']}%)")
            if st["batt_last"] is not None:
                parts.append(f"batteria {st['batt_last']:.2f} V")
            lines.append(f"\n📍 {t['label']}{' (in pausa)' if t['paused'] else ''}: " + ", ".join(parts))
            if last:
                lines.append(f"  ultima {fmt_ts(last['ts'])} {maps_link(last['lat'], last['lon'])}")
        reps = self.core.db.repeaters()
        if reps:
            lines.append("")
        for r in reps:
            last, good = self.core.db.repeater_latest(r["name"])
            if last and last["ok"] and good:
                lines.append(f"🟢 {r['name']}: {(good['bat_mv'] or 0) / 1000:.2f} V")
            elif last:
                lines.append(f"🔴 {r['name']}: non risponde, ultima risposta {fmt_ts(good['ts'] if good else None)}")
            else:
                lines.append(f"⚪ {r['name']}: nessun dato")
        i = system.info()
        sysl = []
        if i.get("temp_c") is not None:
            sysl.append(f"{i['temp_c']:.0f} °C")
        sysl.append(f"disco libero {i['disk_free_gb']} GB")
        if i.get("undervoltage_now") or i.get("undervoltage_since_boot"):
            sysl.append("⚠️ tensione bassa")
        lines.append("\n🖥 Raspberry: " + ", ".join(sysl))
        return "\n".join(lines)

    async def send_report(self, chat):
        await chat.send_message(self.report_text(), disable_web_page_preview=True)
        since = int(time.time()) - 86400
        if charts.available() and any(self.core.db.positions_since(t["id"], since) for t in self.core.trackers()):
            try:
                await self.send_map(chat, None, 1)
            except Exception as e:
                log.warning("Mappa del riepilogo non inviata: %s", e)

    async def report_cmd(self, update: Update, ctx):
        if not self.ok(update):
            return
        arg = " ".join(ctx.args or []).strip().lower()
        if not arg:
            await self.send_report(update.effective_chat)
            when = self.report_time()
            await update.message.reply_text(
                f"Riepilogo automatico: ogni giorno alle {when}." if when else
                "Riepilogo automatico disattivato. Per riceverlo ogni sera: /riepilogo 21:00")
            return
        if not self.is_admin(update):
            await update.message.reply_text("Solo le chat admin possono cambiare il riepilogo automatico.")
            return
        if arg in ("no", "off", "basta", "stop"):
            self.core.db.set("tg_report_time", "")
            self.core.audit(f"telegram:{update.effective_chat.id}", "riepilogo automatico", "disattivato")
            await update.message.reply_text("Riepilogo automatico disattivato.")
            return
        when = parse_hhmm(arg)
        if not when:
            await update.message.reply_text("Uso: /riepilogo (adesso) · /riepilogo 21:00 (ogni giorno) · /riepilogo no")
            return
        self.core.db.set("tg_report_time", when)
        self.core.audit(f"telegram:{update.effective_chat.id}", "riepilogo automatico", when)
        await update.message.reply_text(f"Riepilogo automatico ogni giorno alle {when}, a tutte le chat autorizzate.")

    @staticmethod
    def report_slot(when, now=None):
        """Giorno del riepilogo da mandare adesso ("2026-11-22"), o None fuori dalla mezz'ora
        che segue l'orario. Se il servizio era fermo proprio a quell'ora, arriva appena riparte."""
        now = now or datetime.now()
        h, m = map(int, when.split(":"))
        sched = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if sched > now:
            sched -= timedelta(days=1)   # orario poco prima della mezzanotte, adesso è già il giorno dopo
        return sched.strftime("%Y-%m-%d") if now - sched < timedelta(minutes=REPORT_WINDOW_MIN) else None

    async def report_tick(self, now=None):
        when = self.report_time()
        day = self.report_slot(when, now) if when else None
        if not day or self.core.db.get("tg_report_last") == day:
            return False
        self.core.db.set("tg_report_last", day)
        for cid in self.allowed:
            try:
                await self.send_report(_Chat(self.app.bot, cid))
            except Exception as e:
                log.warning("Riepilogo non inviato a %s: %s", cid, e)
        return True

    async def report_loop(self):
        while True:
            await asyncio.sleep(30)
            try:
                await self.report_tick()
            except Exception as e:
                log.exception("Errore nel riepilogo giornaliero: %s", e)

    # ---------- comandi
    async def start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        cid = update.effective_chat.id
        if not self.ok(update):
            await update.message.reply_text(
                f"Non sei autorizzato.\nIl tuo chat ID è {cid}: aggiungilo in "
                f"telegram.allowed_chat_ids nel config e riavvia MeshWatch.")
            return
        await update.message.reply_text("MeshWatch attivo.", reply_markup=KEYBOARD)
        await self.help(update, ctx)

    async def help(self, update: Update, ctx):
        if not self.ok(update):
            return
        await update.message.reply_text(
            "\n".join(f"/{c} – {d}" for c, d in COMMANDS)
            + "\n\nCon più dispositivi si può scrivere il nome dopo il comando (/dove escursionista) "
              "oppure scegliere con i pulsanti.",
            reply_markup=KEYBOARD)

    async def _send_pos(self, chat, p, title, route=None):
        await chat.send_location(latitude=p["lat"], longitude=p["lon"])
        txt = f"{title}\n{fmt_ts(p['ts'])}\n{maps_link(p['lat'], p['lon'])}"
        if p.get("alt") is not None:
            txt += f"\nQuota {p['alt']:.0f} m"
        if p.get("batt_v") is not None:
            txt += f" · batteria {p['batt_v']:.2f} V"
        if route:
            txt += f"\n🛰 {route}"
        await chat.send_message(txt, disable_web_page_preview=True)

    async def position_now(self, chat, t):
        await chat.send_message(f"Chiedo la posizione a {t['label']}… (può volerci un paio di minuti)")
        start = int(time.time())
        res = await self.core.poll_tracker(t["id"])
        if res and res["lat"] is not None:
            res["ts"] = int(time.time())
            await self._send_pos(chat, res, f"📍 {t['label']} adesso", route=self.route_desc(t, res.get("hops"), res.get("path")))
            return
        if res:
            await chat.send_message(f"{t['label']} ha risposto ma il GPS non ha ancora il fix."
                                    + (f" Batteria {res['batt_v']:.2f} V." if res.get("batt_v") else "")
                                    + f"\n🛰 {self.route_desc(t, res.get('hops'), res.get('path'))}")
        else:
            txt = f"❌ {t['label']} non ha risposto."
            p = self.core.db.last_poll(t["id"])
            if p and not p["ok"] and p["ts"] >= start:
                first = ("in flood" if p["hops"] is None or p["hops"] < 0
                         else "sul percorso salvato (" + self.route_desc(t, p["hops"], p["path"]) + ")")
                txt += f"\nTentativi: prima {first}, poi due volte in flood."
            await chat.send_message(txt)
        await self.last_position(chat, t)

    async def last_position(self, chat, t):
        p = self.core.db.last_position(t["id"])
        if not p:
            await chat.send_message(f"Nessuna posizione registrata per {t['label']}.")
            return
        await self._send_pos(chat, p, f"🕓 Ultima posizione di {t['label']}")

    async def track(self, chat, t):
        pts = self.core.db.positions_since(t["id"], int(time.time()) - 12 * 3600)
        if not pts:
            await chat.send_message(f"Nessuna posizione di {t['label']} nelle ultime 12 ore.")
            return
        lines = [f"🧭 {t['label']} – ultime 12 ore ({len(pts)} punti)"]
        for p in pts[-15:]:
            lines.append(f"{datetime.fromtimestamp(p['ts']).strftime('%H:%M')}  {maps_link(p['lat'], p['lon'])}")
        await chat.send_message("\n".join(lines), disable_web_page_preview=True)

    async def interval(self, update: Update, ctx):
        if not self.ok(update):
            return
        try:
            m = int(ctx.args[0])
        except (IndexError, ValueError):
            await update.message.reply_text("Uso: /intervallo 30 (oppure /intervallo 30 nome)")
            return
        await self.pick(update, "interval", str(m), " ".join(ctx.args[1:]))

    async def nodes(self, update: Update, ctx):
        if not self.ok(update):
            return
        st = self.core.state()
        lines = ["📡 Stato", f"Radio gateway: {'collegata' if st['radio_connected'] else 'NON collegata'}"]
        for t in st["trackers"]:
            lines.append(f"\n{t['label']}: ultima risposta {fmt_ts(t['last_ok'])}"
                         f"{' · IN PAUSA' if t['paused'] else ''} · ogni {t['interval_min']} min")
        if st["repeaters"]:
            lines.append("")
        for r in st["repeaters"]:
            s = r["status"]
            head = "🟢" if r["last_ok"] else ("🔴" if r["last_poll"] else "⚪")
            if s:
                lines.append(
                    f"{head} {r['name']}: {(s['bat_mv'] or 0) / 1000:.2f} V · up {fmt_uptime(s['uptime'])}"
                    f" · rumore {s['noise']} dBm · SNR {s['snr']} · {fmt_ts(s['ts'])}")
            else:
                lines.append(f"{head} {r['name']}: nessun dato")
        await update.message.reply_text("\n".join(lines))

    async def refresh_nodes(self, update: Update, ctx):
        if not self.ok(update):
            return
        await update.message.reply_text("Interrogo i ripetitori…")
        await self.core.poll_repeaters()
        await self.nodes(update, ctx)

    async def advert(self, update: Update, ctx):
        if self.ok(update):
            await self.core.mesh.send_advert()
            self.core.audit(f"telegram:{update.effective_chat.id}", "advert gateway")
            await update.message.reply_text("Advert inviato.")

    # ---------- pulsanti
    async def on_button(self, update: Update, ctx):
        q = update.callback_query
        await q.answer()
        chat = q.message.chat
        if chat.id not in self.allowed:
            return
        action, _, rest = q.data.partition(":")
        if action in ("pos", "last", "track", "pause", "resume", "interval", "stats", "sens", "route", "follow",
                      "map"):
            tid, _, arg = rest.partition(":")
            await q.edit_message_reply_markup(None)
            await self.do(chat, action, tid, arg)
            return
        if chat.id not in self.admins:
            return
        if action == "node":
            self.sessions[chat.id] = (rest, time.time() + 600)
            await q.edit_message_text(
                f"Terminale su {rest}. Scrivi i comandi (es. ver, clock, get radio, neighbors).\n"
                f"/fine per uscire · si chiude da solo dopo 10 minuti senza comandi.")
        elif action in ("ok", "no"):
            item = self.pending.pop(rest, None)
            if not item or item[0] != chat.id:
                await q.edit_message_text("Richiesta scaduta.")
                return
            _, node, cmd = item
            if action == "no":
                await q.edit_message_text(f"Annullato: {cmd}")
                return
            await q.edit_message_text(f"> {cmd} @ {node}\n… in attesa della risposta via radio")
            await self._exec(chat, node, cmd)

    # ---------- terminale
    async def term_start(self, update: Update, ctx):
        if not self.is_admin(update):
            return
        reps = [r["name"] for r in self.core.db.repeaters()]
        if not reps:
            await update.message.reply_text("Nessun ripetitore configurato.")
            return
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(n, callback_data="node:" + n[:50])] for n in reps])
        await update.message.reply_text("Terminale: scegli il ripetitore.", reply_markup=kb)

    async def term_end(self, update: Update, ctx):
        if self.sessions.pop(update.effective_chat.id, None):
            await update.message.reply_text("Terminale chiuso.", reply_markup=KEYBOARD)

    async def term_text(self, update: Update, ctx):
        chat = update.effective_chat.id
        sess = self.sessions.get(chat)
        if not self.is_admin(update) or not sess:
            return
        node, exp = sess
        if time.time() > exp:
            self.sessions.pop(chat, None)
            await update.message.reply_text("Sessione scaduta. /terminale per riaprirla.")
            return
        self.sessions[chat] = (node, time.time() + 600)
        cmd = update.message.text.strip()
        if is_dangerous(cmd):
            tok = secrets.token_hex(4)
            self.pending[tok] = (chat, node, cmd)
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Conferma", callback_data="ok:" + tok),
                                        InlineKeyboardButton("✖ Annulla", callback_data="no:" + tok)]])
            await update.message.reply_text(f"⚠️ \"{cmd}\" modifica o riavvia {node}. Confermi?", reply_markup=kb)
            return
        await update.message.reply_text(f"> {cmd} @ {node}\n… in attesa della risposta via radio")
        await self._exec(update.effective_chat, node, cmd)

    async def _exec(self, chat_obj, node, cmd):
        try:
            reply = await self.core.run_cli(node, cmd, f"telegram:{chat_obj.id}")
            text = reply if reply is not None else "(nessuna risposta: ripetitore non raggiungibile o password errata)"
        except Exception as e:
            text = f"Errore: {e}"
        await chat_obj.send_message(f"{node}:\n{text}")
