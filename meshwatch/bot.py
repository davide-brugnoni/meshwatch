import asyncio
import logging
import secrets
import time
from datetime import datetime

import httpx
from telegram.error import NetworkError, TimedOut
from telegram.request import HTTPXRequest
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, Update
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler,
                          filters)

from .core import is_dangerous, maps_link

log = logging.getLogger("meshwatch.bot")

BTN_POS, BTN_LAST, BTN_TRACK, BTN_NODES = "📍 Posizione ora", "🕓 Ultima posizione", "🧭 Traccia oggi", "📡 Nodi"
KEYBOARD = ReplyKeyboardMarkup(
    [[KeyboardButton(BTN_POS), KeyboardButton(BTN_LAST)],
     [KeyboardButton(BTN_TRACK), KeyboardButton(BTN_NODES)]],
    resize_keyboard=True)


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
    def __init__(self, cfg, core):
        self.core = core
        self.allowed = set(int(x) for x in cfg.get("allowed_chat_ids") or [])
        # chi può usare il terminale; se vuoto, tutte le chat autorizzate
        self.admins = set(int(x) for x in cfg.get("admin_chat_ids") or []) or self.allowed
        self.sessions = {}   # chat_id -> (nome ripetitore, scadenza)
        self.pending = {}    # token -> (chat_id, nodo, comando)
        # concurrent_updates: un comando che aspetta la radio non blocca gli altri
        self.app = (Application.builder().token(cfg["token"]).concurrent_updates(True)
                    .request(self._request()).get_updates_request(self._request(read_timeout=30))
                    .build())
        a = self.app
        a.add_handler(CommandHandler("start", self.start))
        a.add_handler(CommandHandler(["aiuto", "help"], self.help))
        a.add_handler(CommandHandler("posizione", lambda u, c: self.pick(u, "pos")))
        a.add_handler(CommandHandler("ultima", lambda u, c: self.pick(u, "last")))
        a.add_handler(CommandHandler("traccia", lambda u, c: self.pick(u, "track")))
        a.add_handler(CommandHandler("pausa", lambda u, c: self.pick(u, "pause")))
        a.add_handler(CommandHandler("riprendi", lambda u, c: self.pick(u, "resume")))
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
        a.add_handler(CallbackQueryHandler(self.on_button))
        a.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.term_text))
        a.add_error_handler(self.on_error)
        core.notifiers.append(self.broadcast)

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
    async def pick(self, update: Update, action, arg=""):
        """Esegue l'azione sul dispositivo; se ce n'è più di uno chiede quale."""
        if not self.ok(update):
            return
        ts = self.core.trackers()
        if not ts:
            await update.message.reply_text("Nessun dispositivo da tracciare. Aggiungilo dalla pagina web → Gestione.")
            return
        if len(ts) == 1:
            await self.do(update.effective_chat, action, ts[0]["id"], arg)
            return
        kb = [[InlineKeyboardButton(t["label"], callback_data=f"{action}:{t['id']}:{arg}")] for t in ts]
        if action in ("pos", "last"):
            kb.append([InlineKeyboardButton("Tutti", callback_data=f"{action}:all:{arg}")])
        await update.message.reply_text("Quale dispositivo?", reply_markup=InlineKeyboardMarkup(kb))

    async def do(self, chat, action, tid, arg=""):
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
            "/posizione – chiede adesso la posizione\n"
            "/ultima – ultima posizione ricevuta\n"
            "/traccia – posizioni delle ultime 12 ore\n"
            "/nodi – stato di dispositivi e ripetitori\n"
            "/aggiorna – interroga subito i ripetitori\n"
            "/intervallo 30 – ogni quanti minuti chiedere la posizione\n"
            "/pausa · /riprendi – sospende o riattiva il tracciamento\n"
            "/advert – fa annunciare il gateway sulla mesh\n"
            "/terminale – comandi CLI ai ripetitori · /fine per uscire",
            reply_markup=KEYBOARD)

    async def _send_pos(self, chat, p, title):
        await chat.send_location(latitude=p["lat"], longitude=p["lon"])
        txt = f"{title}\n{fmt_ts(p['ts'])}\n{maps_link(p['lat'], p['lon'])}"
        if p.get("alt") is not None:
            txt += f"\nQuota {p['alt']:.0f} m"
        if p.get("batt_v") is not None:
            txt += f" · batteria {p['batt_v']:.2f} V"
        await chat.send_message(txt, disable_web_page_preview=True)

    async def position_now(self, chat, t):
        await chat.send_message(f"Chiedo la posizione a {t['label']}… (può volerci fino a un minuto)")
        res = await self.core.poll_tracker(t["id"])
        if res and res["lat"] is not None:
            res["ts"] = int(time.time())
            await self._send_pos(chat, res, f"📍 {t['label']} adesso")
            return
        if res:
            await chat.send_message(f"{t['label']} ha risposto ma il GPS non ha ancora il fix."
                                    + (f" Batteria {res['batt_v']:.2f} V." if res.get("batt_v") else ""))
        else:
            await chat.send_message(f"❌ {t['label']} non ha risposto.")
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
            await update.message.reply_text("Uso: /intervallo 30")
            return
        await self.pick(update, "interval", str(m))

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
        if action in ("pos", "last", "track", "pause", "resume", "interval"):
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
