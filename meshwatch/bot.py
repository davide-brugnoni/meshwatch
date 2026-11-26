import asyncio
import json
import logging
import secrets
import time
from datetime import datetime, timedelta

import httpx
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TimedOut
from telegram.request import HTTPXRequest
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, Update
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler,
                          filters)

from . import __version__, charts, maintenance, system
from .core import is_dangerous, maps_link, parse_hhmm
from .firmware import parse_version

log = logging.getLogger("meshwatch.bot")

BTN_POS, BTN_LAST, BTN_TRACK, BTN_NODES = "📍 Posizione ora", "🕓 Ultima posizione", "🧭 Traccia oggi", "📡 Nodi"
BTN_MAP, BTN_FOLLOW, BTN_STATS = "🗺 Mappa", "👣 Segui 2 ore", "📊 Statistiche"
BTN_REPORT, BTN_SYSTEM, BTN_NET = "📝 Riepilogo", "🖥 Sistema", "📶 Rete"
# "Traccia oggi" non è più sulla tastiera (la mappa la mostra meglio) ma il pulsante funziona
# ancora per chi ha la tastiera vecchia; /traccia resta nel menu
KEYBOARD = ReplyKeyboardMarkup(
    [[KeyboardButton(BTN_POS), KeyboardButton(BTN_LAST)],
     [KeyboardButton(BTN_MAP), KeyboardButton(BTN_FOLLOW)],
     [KeyboardButton(BTN_STATS), KeyboardButton(BTN_REPORT)],
     [KeyboardButton(BTN_NODES), KeyboardButton(BTN_NET), KeyboardButton(BTN_SYSTEM)]],
    resize_keyboard=True)
KEYBOARD_VERSION = "3"
KEYBOARD_NEWS = ("🆕 Nuovo pulsante 📶 Rete (anche /rete): nodi attivi, nuovi e spariti, disponibilità e batteria "
                 "dei ripetitori, collegamenti peggiorati. Grafici completi nella pagina, scheda Statistiche.")
NO_PIL = ("Per la mappa serve la libreria Pillow: aggiorna MeshWatch dalla pagina "
          "(Gestione → Sistema → Aggiornamento programma) oppure installala con  venv/bin/pip install Pillow")


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
    ("rete", "statistiche della rete: nodi nuovi e spariti, ripetitori, collegamenti"),
    ("aggiorna", "interroga subito i ripetitori"),
    ("sistema", "stato del Raspberry, della radio e del database"),
    ("riepilogo", "riepilogo della giornata; /riepilogo 21:00 ogni sera, /riepilogo no"),
    ("intervallo", "ogni quanti minuti chiedere la posizione: /intervallo 30"),
    ("pausa", "sospende il tracciamento"),
    ("riprendi", "riattiva il tracciamento"),
    ("advert", "il gateway si annuncia sulla mesh"),
    ("aggiornamento", "nuova versione di MeshWatch: controlla e installa"),
    ("firmware", "firmware MeshCore: aggiorna il gateway, versioni dei ripetitori"),
    ("terminale", "comandi CLI ai ripetitori"),
    ("aiuto", "elenco dei comandi"),
]
FOLLOW_MAX_H = 8
# azioni che cambiano qualcosa: come sulla pagina, solo admin (le altre sono da viewer)
ADMIN_ACTIONS = {"pause", "resume", "interval"}
NOT_ADMIN = "Solo le chat admin possono farlo."
REPORT_WINDOW_MIN = 30   # riepilogo automatico: se a quell'ora il servizio era fermo, lo manda entro 30 min


def thin(seq, n):
    """Al massimo n elementi a intervalli regolari, sempre con il primo e l'ultimo."""
    if len(seq) <= n:
        return list(seq)
    step = (len(seq) - 1) / (n - 1)
    return [seq[round(i * step)] for i in range(n)]


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


def traffic_text(history):
    """Traffico del ripetitore tra le ultime due letture: pacchetti all'ora e airtime.
    I contatori ripartono da zero al riavvio: in quel caso non si calcola nulla."""
    s = history[-1] if history else None
    if not s:
        return ""
    def tot(*keys):
        v = [s.get(k) for k in keys]
        return None if any(x is None for x in v) else sum(v)
    parts = []
    if s.get("nb_recv") is not None:
        parts.append(f"Rx {s['nb_recv']} · Tx {s['nb_sent']}")
    if s.get("sent_flood") is not None:
        parts.append(f"flood/diretti Tx {s['sent_flood']}/{s['sent_direct']}")
    dups = tot("flood_dups", "direct_dups")
    if dups is not None:
        parts.append(f"duplicati {dups}")
    if s.get("recv_errors"):
        parts.append(f"errori {s['recv_errors']}")
    if s.get("full_evts"):
        parts.append(f"coda piena {s['full_evts']}")
    if len(history) >= 2:
        a, b = history[-2], history[-1]
        dt = b["ts"] - a["ts"]
        rebooted = (a.get("uptime") or 0) > (b.get("uptime") or 0)
        def rate(key, scale):
            if dt <= 0 or rebooted or a.get(key) is None or b.get(key) is None or b[key] < a[key]:
                return None
            return (b[key] - a[key]) / dt * scale
        rx, tx, air = rate("nb_recv", 3600), rate("nb_sent", 3600), rate("airtime", 100)
        r = []
        if rx is not None and tx is not None:
            r.append(f"{rx:.0f} Rx/h, {tx:.0f} Tx/h")
        if air is not None:
            r.append(f"airtime TX {air:.1f}%")
        if r:
            parts.append("ultimo periodo: " + ", ".join(r))
    return " · ".join(parts)


class Bot:
    def __init__(self, cfg, core, tiles=None, updater=None):
        self.core = core
        self.updater = updater   # aggiornamento del programma (lo stesso della pagina)
        self.tiles = tiles   # cache delle mappe condivisa con la pagina (vedi __main__)
        self.report_task = self.keyboard_task = None
        self.allowed = set(int(x) for x in cfg.get("allowed_chat_ids") or [])
        # chi può gestire (terminale, pausa, intervallo, ...); se vuoto, tutte le chat autorizzate.
        # Una chat admin è anche autorizzata, anche se manca in allowed_chat_ids.
        self.admins = set(int(x) for x in cfg.get("admin_chat_ids") or []) or set(self.allowed)
        self.allowed |= self.admins
        # nei gruppi la chat è di tutti i membri: per le azioni da admin conta chi scrive,
        # che deve stare qui oppure avere la sua chat privata tra le chat admin
        self.admin_users = set(int(x) for x in cfg.get("admin_user_ids") or [])
        self.sessions = {}   # chat_id -> (nome ripetitore, scadenza)
        self.pending = {}    # token -> (chat_id, nodo, comando)
        # (chat_id, tracker_id) -> (id del messaggio con la posizione in diretta, scadenza);
        # salvate nel database: dopo un riavvio il pin continua a spostarsi
        self.live = self._load_live()
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
        a.add_handler(CommandHandler("rete", self.network_cmd))
        a.add_handler(CommandHandler("aggiorna", self.refresh_nodes))
        a.add_handler(CommandHandler("advert", self.advert))
        a.add_handler(CommandHandler(["aggiornamento", "versione"], self.update_cmd))
        a.add_handler(CommandHandler("firmware", self.firmware_cmd))
        a.add_handler(CommandHandler("terminale", self.term_start))
        a.add_handler(CommandHandler("fine", self.term_end))
        a.add_handler(MessageHandler(filters.Text([BTN_POS]), lambda u, c: self.pick(u, "pos")))
        a.add_handler(MessageHandler(filters.Text([BTN_LAST]), lambda u, c: self.pick(u, "last")))
        a.add_handler(MessageHandler(filters.Text([BTN_TRACK]), lambda u, c: self.pick(u, "track")))
        a.add_handler(MessageHandler(filters.Text([BTN_NODES]), self.nodes))
        a.add_handler(MessageHandler(filters.Text([BTN_NET]), self.network_cmd))
        a.add_handler(MessageHandler(filters.Text([BTN_MAP]), lambda u, c: self.pick(u, "map", "1")))
        a.add_handler(MessageHandler(filters.Text([BTN_FOLLOW]), lambda u, c: self.pick(u, "follow", "2")))
        a.add_handler(MessageHandler(filters.Text([BTN_STATS]), lambda u, c: self.pick(u, "stats", "1")))
        a.add_handler(MessageHandler(filters.Text([BTN_REPORT]), self.report_cmd))
        a.add_handler(MessageHandler(filters.Text([BTN_SYSTEM]), self.system_cmd))
        a.add_handler(CallbackQueryHandler(self.on_button))
        a.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.term_text))
        a.add_error_handler(self.on_error)
        core.notifiers.append(self.broadcast)
        core.file_senders.append(self.send_file)
        core.position_listeners.append(self.on_position)
        core.reporters.append(self.report_all)

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

    async def start_forever(self, first_wait=10, max_wait=300):
        """Collegamento a Telegram con nuovi tentativi: senza Internet all'avvio (rete o DNS
        non ancora pronti) il programma non deve fermarsi, né la pagina restare spenta."""
        wait = first_wait
        while True:
            try:
                await self.run()
                return
            except (NetworkError, TimedOut) as e:
                log.warning("Telegram non raggiungibile all'avvio (%s): riprovo tra %d s", e, wait)
            except Exception as e:   # es. token sbagliato: il bot resta spento, il resto funziona
                log.error("Bot Telegram non avviato: %r", e)
                if "Unauthorized" in type(e).__name__ or "InvalidToken" in type(e).__name__:
                    return
            await asyncio.sleep(wait)
            wait = min(max_wait, wait * 2)

    async def run(self):
        await self.app.initialize()
        await self.app.start()
        # i comandi arrivati mentre il servizio era fermo vengono eseguiti,
        # tranne quelli più vecchi di 3 minuti (vedi self.ok)
        await self.app.updater.start_polling(drop_pending_updates=False)
        log.info("Bot Telegram attivo")
        self.report_task = asyncio.create_task(self.report_loop())
        # in sottofondo: senza Internet non deve ritardare l'avvio della pagina
        self.keyboard_task = asyncio.create_task(self.send_new_keyboard())
        try:   # menu dei comandi che Telegram mostra scrivendo "/"
            from telegram import BotCommand
            await self.app.bot.set_my_commands([BotCommand(c, d) for c, d in COMMANDS])
        except Exception as e:
            log.info("Menu dei comandi non impostato: %s", e)

    async def send_new_keyboard(self):
        """Telegram tiene la tastiera finché il bot non ne manda un'altra: dopo un aggiornamento
        che la cambia la rimanda una volta a ogni chat, con i pulsanti nuovi."""
        if self.core.db.get("tg_keyboard") == KEYBOARD_VERSION:
            return
        done = set((self.core.db.get("tg_keyboard_chats") or "").split(","))   # già mandata (giro precedente)
        for chat in self.allowed:
            if str(chat) in done:
                continue
            try:
                await self.app.bot.send_message(chat, KEYBOARD_NEWS, reply_markup=KEYBOARD)
            except (NetworkError, TimedOut) as e:
                log.info("Tastiera non inviata (Telegram non raggiungibile): %s", e)
                return   # riprova al prossimo avvio, solo alle chat che non l'hanno ricevuta
            except Exception as e:
                log.info("Tastiera non inviata a %s: %s", chat, e)   # chat che ha bloccato il bot, ID sbagliato…
            done.add(str(chat))
            self.core.db.set("tg_keyboard_chats", ",".join(sorted(x for x in done if x)))
        self.core.db.set("tg_keyboard", KEYBOARD_VERSION)
        self.core.db.set("tg_keyboard_chats", "")

    @staticmethod
    async def _deliver(chat, what, send, attempts, wait):
        """Una consegna a una chat: se la rete cade riprova per qualche minuto, se Telegram chiede
        di rallentare aspetta quanto dice; una chat che ha bloccato il bot (o un ID sbagliato)
        si salta. Mai un'eccezione: le altre chat ricevono comunque."""
        for attempt in range(attempts):
            try:
                await send()
                return True
            except RetryAfter as e:
                ra = e.retry_after
                delay = ra.total_seconds() if hasattr(ra, "total_seconds") else float(ra)
                log.warning("%s per %s: Telegram chiede di aspettare %.0f s", what, chat, delay)
                await asyncio.sleep(delay + 1)
            except (NetworkError, TimedOut) as e:
                if isinstance(e, BadRequest):   # BadRequest deriva da NetworkError ma non si risolve riprovando
                    log.warning("%s non inviato a %s: %s", what, chat, e)
                    return False
                log.warning("%s non inviato a %s (tentativo %d): %s", what, chat, attempt + 1, e)
                await asyncio.sleep(wait(attempt))
            except Forbidden as e:
                log.warning("%s non inviato a %s: la chat ha bloccato il bot (%s)", what, chat, e)
                return False
            except Exception as e:
                log.warning("%s non inviato a %s: %r", what, chat, e)
                return False
        return False

    async def broadcast(self, text):
        """Avvisi a tutte le chat autorizzate, in parallelo: una chat lenta o bloccata non
        ritarda né fa perdere l'avviso alle altre."""
        await asyncio.gather(*(
            self._deliver(chat, "Avviso", lambda chat=chat: self.app.bot.send_message(
                chat, text, disable_web_page_preview=True), 6, lambda a: min(60, 10 * (a + 1)))
            for chat in self.allowed))

    async def send_file(self, data, filename, caption=""):
        """Backup: solo alle chat admin (contiene password)."""
        from telegram import InputFile
        await asyncio.gather(*(
            self._deliver(chat, "Backup", lambda chat=chat: self.app.bot.send_document(
                chat, InputFile(data, filename=filename), caption=caption, read_timeout=60, write_timeout=60),
                4, lambda a: 30 * (a + 1))
            for chat in self.admins))

    def ok(self, update: Update):
        if not (update.effective_chat and update.effective_chat.id in self.allowed):
            return False
        msg = update.effective_message
        if msg and msg.date and time.time() - msg.date.timestamp() > 180:
            return False   # comando vecchio, arrivato mentre il servizio era fermo
        return True

    def admin_ok(self, chat, user):
        """Chat admin e, nei gruppi, anche chi scrive o preme il pulsante è un admin."""
        if not chat or chat.id not in self.admins:
            return False
        if getattr(chat, "type", "private") == "private":
            return True
        uid = getattr(user, "id", None)
        return uid is not None and (uid in self.admin_users or uid in self.admins)

    def is_admin(self, update: Update):
        return self.ok(update) and self.admin_ok(update.effective_chat, update.effective_user)

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
        msg = update.effective_message
        if action in ADMIN_ACTIONS and not self.is_admin(update):
            await msg.reply_text(NOT_ADMIN)
            return
        ts = self.core.find_trackers(name)
        if not self.core.trackers():
            await msg.reply_text("Nessun dispositivo da tracciare. Aggiungilo dalla pagina web → Gestione.")
            return
        if not ts:
            names = ", ".join(t["label"] for t in self.core.trackers())
            await msg.reply_text(f"Nessun dispositivo si chiama \"{name}\". Dispositivi: {names}.")
            return
        if len(ts) == 1:
            await self.do(update.effective_chat, action, ts[0]["id"], arg)
            return
        kb = [[InlineKeyboardButton(t["label"], callback_data=f"{action}:{t['id']}:{arg}")] for t in ts]
        if action in ("pos", "last", "stats", "sens", "map") and not name:
            kb.append([InlineKeyboardButton("Tutti", callback_data=f"{action}:all:{arg}")])
        await msg.reply_text("Quale dispositivo?", reply_markup=InlineKeyboardMarkup(kb))

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

    def _load_live(self):
        try:
            items = json.loads(self.core.db.get("tg_live") or "[]")
            now = time.time()
            return {(int(c), int(t)): (int(m), float(u)) for c, t, m, u in items if float(u) > now}
        except (ValueError, TypeError) as e:
            log.info("Posizioni in diretta salvate non leggibili: %s", e)
            return {}

    def _save_live(self):
        self.core.db.set("tg_live", json.dumps([[c, t, m, u] for (c, t), (m, u) in self.live.items()]))

    async def follow_start(self, chat, t, hours):
        p = self.core.db.last_position(t["id"])
        if not p:
            await chat.send_message(f"Nessuna posizione di {t['label']} da condividere.")
            return
        old = self.live.pop((chat.id, t["id"]), None)
        if old:
            await self._stop_live(chat.id, old[0])
        msg = await chat.send_location(latitude=p["lat"], longitude=p["lon"], live_period=hours * 3600)
        self.live[(chat.id, t["id"])] = (msg.message_id, time.time() + hours * 3600)
        self._save_live()
        self.core.audit(f"telegram:{chat.id}", "posizione in diretta", f"{t['label']}: {hours} h")
        await chat.send_message(
            f"📡 Posizione in diretta di {t['label']} per {hours} or{'a' if hours == 1 else 'e'}: si sposta a ogni nuova "
            f"posizione (ogni {self.core.interval_min(t)} min). /basta per smettere.")

    async def on_position(self, t, p):
        changed = False
        for (chat_id, tid), (msg_id, until) in list(self.live.items()):
            if tid != t["id"]:
                continue
            if time.time() > until:
                self.live.pop((chat_id, tid), None)
                changed = True
                continue
            try:
                await self.app.bot.edit_message_live_location(
                    chat_id=chat_id, message_id=msg_id, latitude=p["lat"], longitude=p["lon"])
            except (NetworkError, TimedOut) as e:
                if isinstance(e, BadRequest) and "not modified" not in str(e).lower():
                    log.info("Posizione in diretta terminata (%s): %s", t["label"], e)
                    self.live.pop((chat_id, tid), None)
                    changed = True
                elif not isinstance(e, BadRequest):   # rete giù: si riprova alla prossima posizione
                    log.info("Posizione in diretta non aggiornata (%s): %s", t["label"], e)
            except Exception as e:   # fermata dall'utente, chat che ha bloccato il bot…
                log.info("Posizione in diretta terminata (%s): %s", t["label"], e)
                self.live.pop((chat_id, tid), None)
                changed = True
        if changed:
            self._save_live()

    async def _stop_live(self, chat_id, msg_id):
        try:
            await self.app.bot.stop_message_live_location(chat_id=chat_id, message_id=msg_id)
        except Exception:
            pass   # già scaduta o fermata da Telegram

    async def unfollow(self, update: Update, ctx):
        if not self.ok(update):
            return
        chat = update.effective_chat.id
        mine = [k for k in self.live if k[0] == chat]
        for k in mine:
            await self._stop_live(chat, self.live.pop(k)[0])
        if mine:
            self._save_live()
        await update.effective_message.reply_text("Posizione in diretta fermata." if mine else "Nessuna posizione in diretta attiva.")

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
                when = datetime.fromtimestamp(last["ts"])
                fmt = "%H:%M" if when.date() == datetime.now().date() else "%d/%m %H:%M"
                markers.append({"lat": last["lat"], "lon": last["lon"], "color": color, "size": 9, "bold": True,
                                "label": f"{x['label']} {when.strftime(fmt)}"})
                st = self.core.tracker_stats(x["id"], since)
                lines.append(f"{x['label']}: " + (f"{st['km']:.1f} km, {st['points']} posizioni" if pts
                                                  else "nessuna posizione nel periodo")
                             + f"\n  ultima {fmt_ts(last['ts'])} {maps_link(last['lat'], last['lon'])}")
        if not markers:
            await chat.send_message("Nessuna posizione registrata" + (f" per {t['label']}." if t else "."))
            return
        title = f"🗺 {t['label'] if t else 'Tutti i dispositivi'} – {period_text(days)}"
        try:
            img = await charts.static_map(self._get_tile, tracks, markers + self._net_markers())
        except Exception as e:   # un errore nel disegno non deve lasciare la chat senza risposta
            log.exception("Mappa non disegnata: %s", e)
            img = None
        if not img:
            await chat.send_message(title + "\n" + "\n".join(lines) + "\n(immagine della mappa non disponibile)",
                                    disable_web_page_preview=True)
            return
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
            await update.effective_message.reply_text(self.system_text())

    # ---------- riepilogo giornaliero
    def report_time(self):
        return self.core.report_time()

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
        try:
            net = self.core.netstats.summary(now)
            if net["nodes"]:
                bits = [f"{net['active_24h']} nodi attivi", f"{net['adverts_24h']} advert"]
                if net["packets_24h"]:
                    bits.append(f"{net['packets_24h']} pacchetti")
                if net["new_7d"]:
                    bits.append(f"{net['new_7d']} nuovi in 7 giorni")
                if net["gone"]:
                    bits.append(f"{net['gone']} spariti")
                lines.append("\n📶 Rete (24 ore): " + ", ".join(bits))
        except Exception as e:
            log.warning("Statistiche della rete nel riepilogo: %s", e)
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
            await update.effective_message.reply_text(
                f"Riepilogo automatico: ogni giorno alle {when}." if when else
                "Riepilogo automatico spento. Si accende dalla pagina (Gestione → Telegram) "
                "oppure, da una chat admin, con /riepilogo 21:00")
            return
        if not self.is_admin(update):
            await update.effective_message.reply_text("Solo le chat admin possono cambiare il riepilogo automatico.")
            return
        if arg in ("no", "off", "basta", "stop"):
            self.core.set_report_time("")
            self.core.audit(f"telegram:{update.effective_chat.id}", "riepilogo automatico", "disattivato")
            await update.effective_message.reply_text("Riepilogo automatico disattivato.")
            return
        when = parse_hhmm(arg)
        if not when:
            await update.effective_message.reply_text("Uso: /riepilogo (adesso) · /riepilogo 21:00 (ogni giorno) · /riepilogo no")
            return
        self.core.set_report_time(when)
        self.core.audit(f"telegram:{update.effective_chat.id}", "riepilogo automatico", when)
        await update.effective_message.reply_text(f"Riepilogo automatico ogni giorno alle {when}, a tutte le chat autorizzate.")

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
        await self.report_all()
        return True

    async def report_all(self):
        """Riepilogo a tutte le chat autorizzate (ogni giorno, o dalla pagina con "Invia ora")."""
        for cid in self.allowed:
            try:
                await self.send_report(_Chat(self.app.bot, cid))
            except Exception as e:
                log.warning("Riepilogo non inviato a %s: %s", cid, e)

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
            await update.effective_message.reply_text(
                f"Non sei autorizzato.\nIl tuo chat ID è {cid}: aggiungilo in "
                f"telegram.allowed_chat_ids nel config e riavvia MeshWatch.")
            return
        await update.effective_message.reply_text("MeshWatch attivo.", reply_markup=KEYBOARD)
        await self.help(update, ctx)

    async def help(self, update: Update, ctx):
        if not self.ok(update):
            return
        await update.effective_message.reply_text(
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
                if p["hops"] is None or p["hops"] < 0:
                    txt += "\nTentativi: tre volte in flood."
                else:
                    txt += ("\nTentativi: prima sul percorso salvato ("
                            + self.route_desc(t, p["hops"], p["path"]) + "), poi due volte in flood.")
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
            await update.effective_message.reply_text("Uso: /intervallo 30 (oppure /intervallo 30 nome)")
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
                traffic = traffic_text(r["history"])
                if traffic:
                    lines.append(f"   📶 {traffic}")
            else:
                lines.append(f"{head} {r['name']}: nessun dato")
        await update.effective_message.reply_text("\n".join(lines))

    async def network_cmd(self, update: Update, ctx):
        if not self.ok(update):
            return
        # la rubrica vive nella radio: si legge qui, i calcoli (solo database) in un thread
        contacts = self.core.mesh.contacts() if self.core.mesh.connected else []
        text = await asyncio.to_thread(self.core.netstats.telegram_text, contacts)
        await update.effective_message.reply_text(text, reply_markup=KEYBOARD)

    async def refresh_nodes(self, update: Update, ctx):
        if not self.ok(update):
            return
        if not self.is_admin(update):
            await update.effective_message.reply_text(NOT_ADMIN)
            return
        await update.effective_message.reply_text("Interrogo i ripetitori…")
        await self.core.poll_repeaters()
        await self.nodes(update, ctx)

    async def advert(self, update: Update, ctx):
        if not self.ok(update):
            return
        if not self.is_admin(update):
            await update.effective_message.reply_text(NOT_ADMIN)
            return
        await self.core.mesh.send_advert()
        self.core.audit(f"telegram:{update.effective_chat.id}", "advert gateway")
        await update.effective_message.reply_text("Advert inviato.")

    # ---------- aggiornamento del programma
    @staticmethod
    def _version_line(v):
        if not v or not v.get("sha"):
            return "versione installata dal kit SD (commit sconosciuto)"
        when = ""
        try:
            when = datetime.fromisoformat(v["date"].replace("Z", "+00:00")).astimezone().strftime("%d/%m/%Y %H:%M")
        except (KeyError, TypeError, ValueError, AttributeError):
            pass
        return f"{v['sha'][:7]}" + (f" del {when}" if when else "") + (f" – {v['message']}" if v.get("message") else "")

    async def update_cmd(self, update: Update, ctx):
        """/aggiornamento: controlla GitHub e, se c'è una versione nuova, chiede conferma per installarla."""
        if not self.ok(update):
            return
        if not self.is_admin(update):
            await update.effective_message.reply_text("Solo le chat admin possono aggiornare MeshWatch.")
            return
        if self.updater is None:
            await update.effective_message.reply_text("Aggiornamento non disponibile.")
            return
        from .updater import UpdateError
        await update.effective_message.reply_text("Controllo se c'è una nuova versione…")
        try:
            st = await self.updater.check()
        except UpdateError as e:
            await update.effective_message.reply_text(f"❌ {e}")
            return
        lines = [f"⬆️ MeshWatch {__version__}", "Installata: " + self._version_line(st["installed"])]
        if st.get("rolled_back"):
            lines.append(f"⚠️ L'ultimo aggiornamento non partiva: rimessa la versione precedente ({st['rolled_back']}).")
        if st["job"]["state"] in ("download", "check", "deps", "install", "restarting"):
            lines.append(f"\nAggiornamento già in corso: {st['job']['message']}")
            await update.effective_message.reply_text("\n".join(lines))
            return
        if not st["available"]:
            lines.append("\n✅ Hai già l'ultima versione.")
            await update.effective_message.reply_text("\n".join(lines))
            return
        lines.append("Nuova: " + self._version_line(st["latest"]))
        if st["problems"]:
            lines.append("\nDa qui non si può aggiornare: " + "; ".join(st["problems"])
                         + ". Vedi il README, sezione Aggiornamenti.")
            await update.effective_message.reply_text("\n".join(lines))
            return
        lines.append("\nDurante l'installazione (circa un minuto, di più se cambiano le librerie) pagina, radio "
                     "e bot si fermano. Se la nuova versione non parte torna da sola alla precedente.")
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬆️ Aggiorna ora", callback_data="upd:go"),
                                    InlineKeyboardButton("✖ Annulla", callback_data="upd:no")]])
        await update.effective_message.reply_text("\n".join(lines), reply_markup=kb)

    async def update_button(self, chat, q, what):
        if what != "go":
            await q.edit_message_text("Aggiornamento annullato.")
            return
        if self.updater is None:
            return
        from .updater import UpdateError
        before = self.updater.job
        try:
            await self.updater.start()
        except UpdateError as e:
            await q.edit_message_text(f"❌ Aggiornamento non avviato: {e}")
            return
        self.core.audit(f"telegram:{chat.id}", "aggiornamento avviato")
        await q.edit_message_text("⬆️ Aggiornamento avviato: scarico e controllo la nuova versione, poi MeshWatch "
                                  "si riavvia. Circa 2 minuti dopo il riavvio arriva qui la conferma.")
        task = asyncio.create_task(self.update_progress(chat, before))
        self.core.tasks.add(task)
        task.add_done_callback(self.core.tasks.discard)

    async def update_progress(self, chat, before, every=2, limit=3600):
        """Segue l'aggiornamento avviato da Telegram: avvisa se aggiorna le librerie (lento) o se
        non riesce. Il riavvio non si annuncia qui (il bot si chiude subito): la conferma arriva
        dopo, da Updater.announce."""
        told_deps = False
        for _ in range(int(limit / every)):
            await asyncio.sleep(every)
            job = self.updater.job
            if job is before:
                continue
            if job["state"] == "deps" and not told_deps:
                told_deps = True
                await chat.send_message("📦 Aggiorno le librerie: può richiedere alcuni minuti.")
            elif job["state"] == "error":
                await chat.send_message(f"❌ Aggiornamento non riuscito: {job['message']}\nLa versione attuale resta com'era.")
                return
            elif job["state"] == "restarting":
                return

    # ---------- firmware MeshCore
    def firmware_text(self, st):
        lines = ["📦 Firmware MeshCore", f"Gateway: {st['model'] or 'modello sconosciuto'}"
                 f" · installato {st['installed'] or '?'}"]
        if st["board"]:
            lines.append(f"Scheda: {st['board']}" + ("" if self.core.firmware.saved_board() else " (riconosciuta in automatico)"))
        if st["latest"]:
            lines.append(f"Ultimo rilascio: {st['latest']['version']}"
                         + (" ✅ già installato" if not st["available"] and st["installed"] else ""))
        job = st["job"]
        if self.core.firmware.busy:
            lines.append(f"\n⏳ Aggiornamento in corso: {job['message']}")
        elif job["state"] in ("done", "error") and time.time() - job["ts"] < 3600:
            lines.append(("\n✅ " if job["state"] == "done" else "\n❌ ") + job["message"])
        if st["problems"]:
            lines.append("\nDa qui non si aggiorna: " + "; ".join(st["problems"]))
        elif st["tool_missing"]:
            lines.append("\nAl primo aggiornamento installo lo strumento per programmare la scheda (serve Internet).")
        if st["repeaters"]:
            lines.append("\nRipetitori" + (f" (ultimo {st['latest_repeater']})" if st.get("latest_repeater") else "") + ":")
            new = parse_version(st.get("latest_repeater"))
            for r in st["repeaters"]:
                v = parse_version(r["version"])
                mark = "⚪" if not v else ("🟢" if not new or v >= new else "🟠")
                lines.append(f"{mark} {r['name']}: {r['version'] or 'versione non ancora letta'}")
            if any(parse_version(r["version"]) and new and parse_version(r["version"]) < new for r in st["repeaters"]):
                lines.append("I ripetitori si aggiornano sul posto: /terminale → start ota, poi il WiFi del "
                             "ripetitore (ESP32) o l'app via Bluetooth (nRF52).")
        return "\n".join(lines)

    async def firmware_cmd(self, update: Update, ctx):
        if not self.ok(update):
            return
        if not self.is_admin(update):
            await update.effective_message.reply_text(NOT_ADMIN)
            return
        await update.effective_message.reply_text("Controllo le versioni del firmware…")
        await self.firmware_reply(update.effective_chat)

    async def firmware_reply(self, chat, force=True):
        from .firmware import FirmwareError
        try:
            st = await self.core.firmware.status(force=force)
        except FirmwareError as e:
            await chat.send_message(f"❌ {e}")
            return
        kb = []
        if not st["problems"] and not self.core.firmware.busy:
            label = (f"⬆️ Aggiorna a {st['latest']['version']}" if st["available"]
                     else f"🔁 Reinstalla {st['latest']['version']}")
            kb.append([InlineKeyboardButton(label, callback_data="fw:go")])
        if not self.core.firmware.busy:
            choices = [b for b in st["suggested"] if b != st["board"]]
            kb += [[InlineKeyboardButton(f"Scheda: {b}", callback_data="fw:b:" + b[:56])] for b in choices[:4]]
        await chat.send_message(self.firmware_text(st), reply_markup=InlineKeyboardMarkup(kb) if kb else None)

    async def firmware_button(self, chat, q, what):
        from .firmware import FirmwareError
        if what.startswith("b:"):
            self.core.firmware.set_board(what[2:])
            self.core.audit(f"telegram:{chat.id}", "scheda del gateway", what[2:])
            await q.edit_message_reply_markup(None)
            await self.firmware_reply(chat, force=False)
            return
        if what == "go":
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Sì, aggiorna", callback_data="fw:yes"),
                                        InlineKeyboardButton("✖ Annulla", callback_data="fw:no")]])
            await q.edit_message_reply_markup(None)
            await chat.send_message("Aggiorno il firmware del gateway? Per qualche minuto niente radio: posizioni, "
                                    "ripetitori e chat si fermano. Non scollegare la radio durante la scrittura.",
                                    reply_markup=kb)
            return
        if what == "no":
            await q.edit_message_text("Aggiornamento del firmware annullato.")
            return
        if what == "yes":
            try:
                st = await self.core.firmware.start(f"telegram:{chat.id}", notify=chat.send_message)
            except FirmwareError as e:
                await q.edit_message_text(f"❌ Aggiornamento non avviato: {e}")
                return
            await q.edit_message_text(f"⬆️ Aggiornamento del gateway a {st['latest']['version']} ({st['board']}) "
                                      "avviato. Ti scrivo qui a ogni passaggio.")

    # ---------- pulsanti
    async def on_button(self, update: Update, ctx):
        q = update.callback_query
        await q.answer()
        chat = q.message.chat
        if chat.id not in self.allowed:
            return
        action, _, rest = q.data.partition(":")
        admin = self.admin_ok(chat, q.from_user)
        if action in ("pos", "last", "track", "pause", "resume", "interval", "stats", "sens", "route", "follow",
                      "map"):
            if action in ADMIN_ACTIONS and not admin:
                await chat.send_message(NOT_ADMIN)
                return
            tid, _, arg = rest.partition(":")
            await q.edit_message_reply_markup(None)
            await self.do(chat, action, tid, arg)
            return
        if not admin:
            if action in ("upd", "node", "ok", "no", "fw"):
                await chat.send_message(NOT_ADMIN)
            return
        if action == "fw":
            await self.firmware_button(chat, q, rest)
            return
        if action == "upd":
            await self.update_button(chat, q, rest)
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
        if not self.ok(update):
            return
        if not self.is_admin(update):
            await update.effective_message.reply_text(NOT_ADMIN)
            return
        reps = [r["name"] for r in self.core.db.repeaters()]
        if not reps:
            await update.effective_message.reply_text("Nessun ripetitore configurato.")
            return
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(n, callback_data="node:" + n[:50])] for n in reps])
        await update.effective_message.reply_text("Terminale: scegli il ripetitore.", reply_markup=kb)

    async def term_end(self, update: Update, ctx):
        if self.is_admin(update) and self.sessions.pop(update.effective_chat.id, None):
            await update.effective_message.reply_text("Terminale chiuso.", reply_markup=KEYBOARD)

    async def term_text(self, update: Update, ctx):
        chat = update.effective_chat.id
        sess = self.sessions.get(chat)
        if not self.is_admin(update) or not sess:
            return
        node, exp = sess
        if time.time() > exp:
            self.sessions.pop(chat, None)
            await update.effective_message.reply_text("Sessione scaduta. /terminale per riaprirla.")
            return
        self.sessions[chat] = (node, time.time() + 600)
        cmd = (update.effective_message.text or "").strip()
        if is_dangerous(cmd):
            tok = secrets.token_hex(4)
            self.pending[tok] = (chat, node, cmd)
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Conferma", callback_data="ok:" + tok),
                                        InlineKeyboardButton("✖ Annulla", callback_data="no:" + tok)]])
            await update.effective_message.reply_text(f"⚠️ \"{cmd}\" modifica o riavvia {node}. Confermi?", reply_markup=kb)
            return
        await update.effective_message.reply_text(f"> {cmd} @ {node}\n… in attesa della risposta via radio")
        await self._exec(update.effective_chat, node, cmd)

    async def _exec(self, chat_obj, node, cmd):
        try:
            reply = await self.core.run_cli(node, cmd, f"telegram:{chat_obj.id}")
            text = reply if reply is not None else "(nessuna risposta: ripetitore non raggiungibile o password errata)"
        except Exception as e:
            text = f"Errore: {e}"
        await chat_obj.send_message(f"{node}:\n{text}")
