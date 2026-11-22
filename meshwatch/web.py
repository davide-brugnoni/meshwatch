import asyncio
import re
import time
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware
from pydantic import BaseModel, Field

from .auth import hash_password
from . import maintenance, system
from .tiles import TileCache, TileError, MIN_MB, MAX_MB
from .core import fmt_sensor, is_dangerous, parse_hhmm, sensor_info
from .updater import UpdateError

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
COOKIE = "meshwatch_session"
GUEST = {"username": "ospite", "role": "guest"}
ROLES = {"guest": 0, "viewer": 1, "admin": 2}
HOURS_RE = re.compile(r"^$|^([01]\d|2[0-3]):[0-5]\d-([01]\d|2[0-3]):[0-5]\d$")


class LoginIn(BaseModel):
    username: str
    password: str


class SetupIn(BaseModel):
    username: str = Field(..., pattern=r"^[A-Za-z0-9_.-]{2,32}$")
    password: str = Field(..., min_length=8, max_length=128)


class CliIn(BaseModel):
    node: str
    cmd: str
    confirm: bool = False


class TrackerIn(BaseModel):
    contact: str | None = Field(None, max_length=64)
    label: str | None = Field(None, max_length=40)
    interval_min: int | None = Field(None, ge=5, le=720)
    active_hours: str | None = None
    offline_alert_min: int | None = Field(None, ge=10, le=1440)
    low_battery_v: float | None = Field(None, ge=0, le=5)
    paused: bool | None = None
    moving_interval_min: int | None = Field(None, ge=0, le=720)


class SensorRuleIn(BaseModel):
    type: str = Field(..., min_length=1, max_length=40)
    channel: int = Field(..., ge=0, le=255)
    op: str = Field(..., pattern=r"^[<>]$")
    value: float


class RepeaterIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    password: str = Field("", max_length=64)


class GatewayIn(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=31)
    lat: float | None = Field(None, ge=-90, le=90)
    lon: float | None = Field(None, ge=-180, le=180)
    share_location: bool | None = None
    tx_power: int | None = Field(None, ge=1, le=30)
    radio_freq: float | None = Field(None, ge=137, le=1020)
    radio_bw: float | None = None
    radio_sf: int | None = Field(None, ge=5, le=12)
    radio_cr: int | None = Field(None, ge=5, le=8)
    confirm_radio: bool = False


class MaintenanceIn(BaseModel):
    retention_days: int | None = Field(None, ge=0, le=3650)
    backup_telegram: bool | None = None
    backup_password: str | None = Field(None, max_length=128)   # "" = togli la password


class ReportIn(BaseModel):
    enabled: bool
    time: str = Field("21:00", pattern=r"^\d{1,2}[:.]\d{2}$")


class BatteryAlertsIn(BaseModel):
    batt_repeat_h: int | None = Field(None, ge=0, le=168)
    batt_quiet: str | None = None


class TileCacheIn(BaseModel):
    enabled: bool | None = None
    max_mb: int | None = Field(None, ge=MIN_MB, le=MAX_MB)


class ChatIn(BaseModel):
    kind: str = Field(..., pattern=r"^(dm|chan)$")
    peer: str = Field(..., min_length=1, max_length=64)
    text: str = Field(..., min_length=1, max_length=400)


class ChatReadIn(BaseModel):
    kind: str = Field(..., pattern=r"^(dm|chan)$")
    peer: str = Field(..., min_length=1, max_length=64)


class FavoriteIn(BaseModel):
    on: bool


class ContactSettingsIn(BaseModel):
    manual_add: bool


class UserIn(BaseModel):
    username: str | None = Field(None, pattern=r"^[A-Za-z0-9_.-]{2,32}$")
    password: str | None = Field(None, min_length=6, max_length=128)
    role: str | None = Field(None, pattern=r"^(admin|viewer)$")


class Compress:
    """Comprime pagina, script, stile e risposte JSON (leaflet.js: 148 KB → 43 KB sul WiFi del Raspberry).
    Restano fuori il tempo reale (va spedito subito, non accumulato), le tessere (PNG/JPEG
    già compressi) e il backup."""
    SKIP = ("/api/events", "/api/tiles/", "/api/backup")

    def __init__(self, app):
        self.app = app
        self.gzip = GZipMiddleware(app, minimum_size=1000, compresslevel=5)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and not scope["path"].startswith(self.SKIP):
            return await self.gzip(scope, receive, send)
        return await self.app(scope, receive, send)


def create_app(core, auth, web_cfg, updater=None, tiles=None):
    public = web_cfg.get("mode", "lan") == "public"
    # mappa visibile senza login (sola lettura): di default sì in casa, no su Internet
    public_map = bool(web_cfg.get("public_map", not public))
    db = core.db
    tiles = tiles or TileCache(db)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(Compress)

    def client_ip(req: Request):
        if public:   # dietro Cloudflare Tunnel / reverse proxy
            ip = req.headers.get("cf-connecting-ip") or req.headers.get("x-forwarded-for", "")
            if ip:
                return ip.split(",")[0].strip()
        return req.client.host if req.client else "?"

    def resolve_user(req: Request):
        """Utente collegato, ospite (se la mappa è libera) o None."""
        return auth.user_from_token(req.cookies.get(COOKIE)) or (GUEST if public_map else None)

    def current_user(req: Request):
        u = resolve_user(req)
        if not u:
            raise HTTPException(401, "login richiesto")
        return {**u, "ip": client_ip(req)}

    def audit(u, action, detail=""):
        core.audit(u["username"], action, detail, u.get("ip", ""))

    def needs(role):
        def dep(u=Depends(current_user)):
            if ROLES.get(u["role"], -1) < ROLES[role]:
                # l'ospite viene mandato al login, un viewer ha già fatto il login
                if u["role"] == "guest":
                    raise HTTPException(401, "login richiesto")
                raise HTTPException(403, "solo amministratore")
            return u
        return dep

    viewer = needs("viewer")
    admin = needs("admin")

    def get_tracker(tid):
        t = db.tracker(tid)
        if not t:
            raise HTTPException(404, "dispositivo non trovato")
        return t

    @app.middleware("http")
    async def headers(req, call_next):
        resp = await call_next(req)
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        if req.url.path.startswith("/api/") and "cache-control" not in resp.headers:   # le tessere hanno la loro
            resp.headers["Cache-Control"] = "no-store"
        elif req.url.path == "/" or req.url.path.startswith("/assets/"):
            # dopo un aggiornamento il browser deve prendere subito pagina, stile e script nuovi
            resp.headers["Cache-Control"] = "no-cache"
        return resp

    @app.get("/")
    async def index():
        return FileResponse(WEB_DIR / "index.html")

    # Per il watchdog di systemd (meshwatch/watchdog.py): risponde se il server è vivo.
    # Non tocca database né radio, così una pulizia lunga del database non fa scattare il riavvio.
    @app.get("/api/health")
    async def health():
        return {"ok": True}

    # ---------- sessione
    @app.post("/api/login")
    async def login(body: LoginIn, req: Request, resp: Response):
        ip = client_ip(req)
        if auth.blocked(ip):
            raise HTTPException(429, "troppi tentativi errati: riprova tra 15 minuti")
        token = auth.login(body.username, body.password, ip)
        if not token:
            core.audit(body.username[:32], "login fallito", "", ip)
            if auth.blocked(ip):
                core.audit(body.username[:32], "IP bloccato per 15 minuti", "troppi tentativi errati", ip)
            raise HTTPException(401, "credenziali errate")
        core.audit(body.username, "login", "", ip)
        resp.set_cookie(COOKIE, token, httponly=True, samesite="strict",
                        secure=public, max_age=30 * 86400)
        return {"ok": True}

    @app.post("/api/logout")
    async def logout(resp: Response):
        resp.delete_cookie(COOKIE)
        return {"ok": True}

    @app.get("/api/me")
    async def me(req: Request):
        u = resolve_user(req) or {"username": None, "role": None}
        return {**u, "auth": auth.enabled, "setup": not auth.enabled and not public,
                "public_map": public_map, "tile_cache": tiles.enabled()}

    # Primo avvio: crea l'utente admin. Possibile solo finché non esistono utenti
    # e mai in modalità public (lì il programma non parte senza utenti).
    @app.post("/api/setup")
    async def setup(body: SetupIn, req: Request, resp: Response):
        if public or auth.enabled:
            raise HTTPException(403, "password admin già impostata")
        db.set_user(body.username, hash_password(body.password), "admin")
        core.audit(body.username, "password admin impostata (primo accesso)", "", client_ip(req))
        resp.set_cookie(COOKIE, auth.login(body.username, body.password, client_ip(req)),
                        httponly=True, samesite="strict", secure=public, max_age=30 * 86400)
        return {"ok": True}

    # ---------- stato
    @app.get("/api/state")
    async def state(hours: int = 24, u=Depends(current_user)):
        # le query girano in un thread: intanto il server continua a servire tessere e script
        s = await asyncio.to_thread(core.state, max(1, min(hours, 24 * 30)),
                                    core.mesh.contacts(), core.gateway_node())
        if u["role"] == "guest":
            s["contacts"] = []   # l'ospite vede la mappa, non la rubrica della radio
            for t in s["trackers"]:
                t["route"] = None   # né i ripetitori attraversati
        else:
            s["unread"] = await asyncio.to_thread(db.unread_count)
        return s

    # ---------- aggiornamenti in tempo reale (Server-Sent Events)
    @app.get("/api/events")
    async def events(req: Request, u=Depends(current_user)):
        try:
            sub = core.events.subscribe()
        except (OverflowError, RuntimeError) as e:
            raise HTTPException(503, str(e))
        allowed = {"state"} if u["role"] == "guest" else {"state", "chat"}
        return StreamingResponse(core.events.stream(sub, allowed, req.is_disconnected), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    # ---------- chat (solo utenti con login: admin e viewer)
    @app.get("/api/chat")
    async def chat_list(u=Depends(viewer)):
        return {"conversations": db.conversations(), "targets": core.chat_targets(),
                "unread": db.unread_count(), "radio_connected": core.mesh.connected,
                "limits": {"dm": core.max_text_bytes("dm"), "chan": core.max_text_bytes("chan")},
                "last_id": (db._one("SELECT MAX(id) AS n FROM messages") or {}).get("n") or 0}

    @app.get("/api/chat/messages")
    async def chat_messages(kind: str, peer: str, before: int | None = None, u=Depends(viewer)):
        return db.messages(kind, peer, before)

    @app.get("/api/chat/updates")
    async def chat_updates(after: int = 0, u=Depends(viewer)):
        """Messaggi nuovi e cambi di stato degli invii recenti."""
        new = db.messages_after(after)
        recent = db._all("SELECT id, status, attempts, trip_ms FROM messages WHERE dir='out' AND ts>? ",
                         (int(time.time()) - 600,))
        return {"messages": new, "status": recent, "unread": db.unread_count()}

    @app.post("/api/chat")
    async def chat_send(body: ChatIn, u=Depends(viewer)):
        try:
            mid = core.send_chat(body.kind, body.peer, body.text, u["username"])
        except OverflowError as e:
            raise HTTPException(429, str(e))
        except RuntimeError as e:
            raise HTTPException(503, str(e))
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"ok": True, "message": db.message(mid)}

    @app.post("/api/chat/read")
    async def chat_read(body: ChatReadIn, u=Depends(viewer)):
        db.mark_read(body.kind, body.peer)
        return {"ok": True}

    @app.delete("/api/chat")
    async def chat_delete(kind: str, peer: str, u=Depends(admin)):
        n = db.delete_conversation(kind, peer)
        audit(u, "conversazione cancellata", f"{kind} {peer}: {n} messaggi")
        return {"ok": True, "deleted": n}

    # ---------- mappa della rete
    @app.get("/api/network")
    async def network(u=Depends(viewer)):
        return core.network()

    @app.get("/api/coverage")
    async def coverage(days: int = 30, tracker: int | None = None, cell: int = 250, u=Depends(viewer)):
        if tracker is not None:
            get_tracker(tracker)
        return await asyncio.to_thread(core.coverage, max(1, min(days, 365)), tracker, max(50, min(cell, 5000)))

    @app.post("/api/network/neighbours")
    async def network_refresh(u=Depends(admin)):
        if not core.mesh.connected:
            raise HTTPException(503, "radio gateway non collegata")
        res = {}
        for r in db.repeaters():
            res[r["name"]] = await core.poll_neighbours(r["name"], force=True) is not None
        audit(u, "vicini dei ripetitori aggiornati")
        return {"ok": True, "result": res}

    # ---------- tracker
    @app.post("/api/trackers/{tid}/poll")
    async def poll_tracker(tid: int, u=Depends(viewer)):
        get_tracker(tid)
        res = await core.poll_tracker(tid)
        return {"ok": res is not None, "result": res}

    @app.post("/api/trackers")
    async def add_tracker(body: TrackerIn, u=Depends(admin)):
        if not body.contact:
            raise HTTPException(400, "scegli il contatto MeshCore")
        if body.active_hours and not HOURS_RE.match(body.active_hours):
            raise HTTPException(400, "fascia oraria nel formato 06:00-20:00")
        f = {k: v for k, v in body.model_dump().items() if v is not None}
        f["label"] = f.get("label") or body.contact
        f["paused"] = 1 if f.get("paused") else 0
        tid = db.add_tracker(**f)
        core.schedule_now(tid)
        audit(u, "dispositivo aggiunto", f["label"])
        core.events.publish("state")
        return {"ok": True, "id": tid}

    @app.put("/api/trackers/{tid}")
    async def edit_tracker(tid: int, body: TrackerIn, u=Depends(admin)):
        old = get_tracker(tid)
        if body.active_hours and not HOURS_RE.match(body.active_hours):
            raise HTTPException(400, "fascia oraria nel formato 06:00-20:00")
        f = {k: v for k, v in body.model_dump(exclude_unset=True).items() if v is not None or k == "active_hours"}
        if "active_hours" in f and f["active_hours"] is None:
            f["active_hours"] = ""
        if "paused" in f:
            f["paused"] = 1 if f["paused"] else 0
        db.update_tracker(tid, **f)
        audit(u, "dispositivo modificato", old["label"] + ": " + ", ".join(f"{k}={v}" for k, v in f.items()))
        if "paused" in f and old["paused"] and not f["paused"]:
            core.schedule_now(tid)                 # ripreso: chiede subito
        elif "interval_min" in f or "moving_interval_min" in f:
            core.next_due[tid] = time.time() + core.interval_min(db.tracker(tid)) * 60
        core.events.publish("state")
        return {"ok": True}

    @app.delete("/api/trackers/{tid}")
    async def delete_tracker(tid: int, u=Depends(admin)):
        audit(u, "dispositivo eliminato", get_tracker(tid)["label"])
        db.delete_tracker(tid)
        core.next_due.pop(tid, None)
        core.events.publish("state")
        return {"ok": True}

    @app.delete("/api/trackers/{tid}/track")
    async def clear_tracker_track(tid: int, keep_last: bool = False, u=Depends(admin)):
        t = get_tracker(tid)
        n = db.clear_track(tid, keep_last)
        audit(u, "traccia cancellata", f"{t['label']}: {n} punti")
        core.events.publish("state")
        return {"ok": True, "deleted": n}

    @app.delete("/api/track")
    async def clear_all_tracks(keep_last: bool = False, u=Depends(admin)):
        n = db.clear_track(None, keep_last)
        audit(u, "tracce cancellate", f"tutti i dispositivi: {n} punti")
        core.events.publish("state")
        return {"ok": True, "deleted": n}

    # ---------- soglie dei sensori
    @app.get("/api/trackers/{tid}/rules")
    async def sensor_rules(tid: int, u=Depends(admin)):
        get_tracker(tid)
        rules = [{**r, **sensor_info(r["type"], r["channel"])} for r in db.sensor_rules(tid)]
        sensors = [{"type": x["type"], "channel": x["channel"], **sensor_info(x["type"], x["channel"])}
                   for x in db.sensor_latest(tid)]
        return {"rules": rules, "sensors": sensors}

    @app.post("/api/trackers/{tid}/rules")
    async def add_sensor_rule(tid: int, body: SensorRuleIn, u=Depends(admin)):
        t = get_tracker(tid)
        if len(db.sensor_rules(tid)) >= 20:
            raise HTTPException(400, "massimo 20 soglie per dispositivo")
        rid = db.add_sensor_rule(tid, body.type, body.channel, body.op, body.value)
        audit(u, "soglia sensore aggiunta", f"{t['label']}: {sensor_info(body.type, body.channel)['label']} "
              f"{body.op} {fmt_sensor(body.type, body.channel, body.value)}")
        return {"ok": True, "id": rid}

    @app.delete("/api/trackers/{tid}/rules/{rid}")
    async def delete_sensor_rule(tid: int, rid: int, u=Depends(admin)):
        t = get_tracker(tid)
        if not any(r["id"] == rid for r in db.sensor_rules(tid)):
            raise HTTPException(404, "soglia non trovata")
        db.delete_sensor_rule(rid)
        core.alerts.get(("t", tid), set()).discard(f"rule{rid}")
        audit(u, "soglia sensore rimossa", t["label"])
        return {"ok": True}

    # ---------- ripetitori
    @app.post("/api/repeaters/poll")
    async def poll_repeaters(u=Depends(admin)):
        res = await core.poll_repeaters()
        return {"ok": True, "result": {k: v is not None for k, v in res.items()}}

    @app.post("/api/repeaters")
    async def set_repeater(body: RepeaterIn, u=Depends(admin)):
        old = db.repeater(body.name)
        # password vuota in modifica = lascia quella che c'era
        pw = body.password if (body.password or not old) else old["password"]
        db.set_repeater(body.name, pw)
        audit(u, "ripetitore modificato" if old else "ripetitore aggiunto", body.name)
        return {"ok": True}

    @app.delete("/api/repeaters/{name}")
    async def delete_repeater(name: str, u=Depends(admin)):
        db.delete_repeater(name)
        audit(u, "ripetitore rimosso", name)
        return {"ok": True}

    # ---------- amministrazione
    @app.get("/api/admin")
    async def admin_data(u=Depends(admin)):
        return {
            "trackers": db.trackers(),
            "repeaters": [{"name": r["name"], "has_password": bool(r["password"])} for r in db.repeaters()],
            "users": db.users(),
            "contacts": core.mesh.contacts(),
            "me": u["username"],
        }

    @app.post("/api/users")
    async def add_user(body: UserIn, u=Depends(admin)):
        if not body.username or not body.password:
            raise HTTPException(400, "utente e password (almeno 6 caratteri) obbligatori")
        if db.user(body.username):
            raise HTTPException(400, "utente già esistente")
        role = body.role or "viewer"
        if not db.users() and role != "admin":
            raise HTTPException(400, "il primo utente deve essere admin")
        db.set_user(body.username, hash_password(body.password), role)
        audit(u, "utente creato", f"{body.username} ({role})")
        return {"ok": True, "first": len(db.users()) == 1}

    @app.put("/api/users/{username}")
    async def edit_user(username: str, body: UserIn, u=Depends(admin)):
        target = db.user(username)
        if not target:
            raise HTTPException(404, "utente non trovato")
        admins = [x for x in db.users() if x["role"] == "admin"]
        if body.role == "viewer" and target["role"] == "admin" and len(admins) <= 1:
            raise HTTPException(400, "deve restare almeno un admin")
        db.set_user(username, hash_password(body.password) if body.password else None, body.role)
        audit(u, "utente modificato", username + (" (nuova password)" if body.password else "")
              + (f" (ruolo {body.role})" if body.role else ""))
        return {"ok": True}

    @app.delete("/api/users/{username}")
    async def delete_user(username: str, u=Depends(admin)):
        target = db.user(username)
        if not target:
            raise HTTPException(404, "utente non trovato")
        admins = [x for x in db.users() if x["role"] == "admin"]
        if target["role"] == "admin" and len(admins) <= 1:
            raise HTTPException(400, "deve restare almeno un admin")
        db.delete_user(username)
        audit(u, "utente eliminato", username)
        return {"ok": True}

    # ---------- gateway
    @app.get("/api/gateway")
    async def gateway(u=Depends(admin)):
        if not core.mesh.connected:
            raise HTTPException(503, "radio gateway non collegata")
        return await core.mesh.gateway_info()

    @app.post("/api/gateway")
    async def gateway_set(body: GatewayIn, u=Depends(admin)):
        if not core.mesh.connected:
            raise HTTPException(503, "radio gateway non collegata")
        cur = await core.mesh.gateway_info()
        if body.tx_power is not None and cur.get("max_tx_power") and body.tx_power > cur["max_tx_power"]:
            raise HTTPException(400, f"potenza massima {cur['max_tx_power']} dBm")
        radio = None
        r = (body.radio_freq, body.radio_bw, body.radio_sf, body.radio_cr)
        if any(v is not None for v in r):
            if any(v is None for v in r):
                raise HTTPException(400, "parametri radio incompleti")
            if body.radio_bw not in (7.8, 10.4, 15.6, 20.8, 31.25, 41.7, 62.5, 125, 250, 500):
                raise HTTPException(400, "bandwidth non valida")
            changed = (r != (cur.get("radio_freq"), cur.get("radio_bw"), cur.get("radio_sf"), cur.get("radio_cr")))
            if changed and not body.confirm_radio:
                raise HTTPException(400, "conferma richiesta per cambiare i parametri radio")
            radio = r if changed else None
        if (body.lat is None) != (body.lon is None):
            raise HTTPException(400, "servono sia latitudine sia longitudine")
        errors = await core.mesh.gateway_set(
            name=body.name if body.name and body.name != cur.get("name") else None,
            lat=body.lat, lon=body.lon, share_location=body.share_location,
            tx_power=body.tx_power if body.tx_power != cur.get("tx_power") else None,
            radio=radio)
        audit(u, "gateway configurato", ", ".join(f"{k}={v}" for k, v in body.model_dump(exclude_none=True).items()
                                                   if k != "confirm_radio")
              + (" — errori: " + ", ".join(errors) if errors else ""))
        if errors:
            raise HTTPException(500, "non applicato: " + ", ".join(errors))
        return {"ok": True, "radio_changed": radio is not None}

    # ---------- rubrica del gateway
    def need_radio():
        if not core.mesh.connected:
            raise HTTPException(503, "radio gateway non collegata")

    def get_contact(key):
        need_radio()
        c = core.mesh.contact_by_key(key)
        if not c:
            raise HTTPException(404, "contatto non trovato")
        return c

    def contact_uses(name):
        uses = [f"dispositivo {t['label']}" for t in db.trackers() if t["contact"].lower() == name.lower()]
        uses += [f"ripetitore {r['name']}" for r in db.repeaters() if r["name"].lower() == name.lower()]
        return uses

    @app.get("/api/contacts")
    async def contacts_list(u=Depends(admin)):
        need_radio()
        out = []
        contacts = core.mesh.contacts()
        for c in contacts:
            out.append({**c, "used_by": contact_uses(c["name"]),
                        "route": [n["name"] or n["hash"] for n in core.resolve_path(c.get("path"), contacts)]})
        si = core.mesh.self_info()
        return {"contacts": out, "pending": core.mesh.pending_contacts(),
                "manual_add": bool(si.get("manual_add_contacts"))}

    @app.post("/api/contacts/refresh")
    async def contacts_refresh(u=Depends(admin)):
        need_radio()
        await core.mesh.refresh_contacts()
        return {"ok": True}

    async def contact_action(u, label, c, coro):
        try:
            await coro
        except (RuntimeError, ValueError) as e:
            raise HTTPException(400, str(e))
        audit(u, label, c.get("adv_name") or c.get("name", ""))
        return {"ok": True}

    @app.post("/api/contacts/{key}/reset_path")
    async def contact_reset_path(key: str, u=Depends(admin)):
        c = get_contact(key)
        return await contact_action(u, "percorso contatto azzerato", c, core.mesh.reset_contact_path(c))

    @app.post("/api/contacts/{key}/favorite")
    async def contact_favorite(key: str, body: FavoriteIn, u=Depends(admin)):
        c = get_contact(key)
        return await contact_action(u, "contatto preferito" if body.on else "contatto non più preferito",
                                    c, core.mesh.set_favorite(c, body.on))

    @app.delete("/api/contacts/{key}")
    async def contact_remove(key: str, force: bool = False, u=Depends(admin)):
        c = get_contact(key)
        uses = contact_uses(c.get("adv_name", ""))
        if uses and not force:
            raise HTTPException(409, "usato da: " + ", ".join(uses))
        return await contact_action(u, "contatto rimosso dal gateway", c, core.mesh.remove_contact(c))

    @app.post("/api/contacts/pending/{key}")
    async def contact_add_pending(key: str, u=Depends(admin)):
        need_radio()
        c = next((x for x in core.mesh.pending_contacts() if x["public_key"] == key), None)
        if not c:
            raise HTTPException(404, "nodo non più in attesa")
        return await contact_action(u, "contatto aggiunto al gateway", c, core.mesh.add_pending(key))

    @app.put("/api/contacts/settings")
    async def contact_settings(body: ContactSettingsIn, u=Depends(admin)):
        need_radio()
        try:
            await core.mesh.set_manual_add(body.manual_add)
        except RuntimeError as e:
            raise HTTPException(400, str(e))
        audit(u, "aggiunta contatti", "manuale" if body.manual_add else "automatica")
        return {"ok": True}

    @app.post("/api/radio/reconnect")
    async def radio_reconnect(u=Depends(admin)):
        if core.mesh.connecting:
            raise HTTPException(409, "riconnessione già in corso")
        audit(u, "riconnessione radio")
        asyncio.create_task(core.mesh.reconnect())
        return {"ok": True}

    @app.post("/api/gateway/reboot")
    async def gateway_reboot(u=Depends(admin)):
        audit(u, "riavvio gateway")
        await core.mesh.gateway_reboot()
        return {"ok": True}

    # ---------- sistema (Raspberry)
    @app.get("/api/system")
    async def system_info(u=Depends(admin)):
        return {**system.info(),
                "can_reboot": await system.can_sudo("reboot"),
                "can_poweroff": await system.can_sudo("poweroff")}

    @app.post("/api/system/{action}")
    async def system_action(action: str, u=Depends(admin)):
        if action not in ("reboot", "poweroff"):
            raise HTTPException(404, "azione sconosciuta")
        if not await system.can_sudo(action):
            raise HTTPException(403, "permesso mancante: esegui una volta il comando indicato nel README")
        audit(u, "riavvio Raspberry" if action == "reboot" else "spegnimento Raspberry")
        asyncio.create_task(system.run_later(action))
        return {"ok": True}

    # ---------- aggiornamento del programma
    def get_updater():
        if updater is None:
            raise HTTPException(503, "aggiornamento non disponibile")
        return updater

    @app.get("/api/update")
    async def update_status(u=Depends(admin)):
        return get_updater().status()

    @app.post("/api/update/check")
    async def update_check(u=Depends(admin)):
        try:
            return await get_updater().check()
        except UpdateError as e:
            raise HTTPException(502, str(e))

    @app.post("/api/update/apply")
    async def update_apply(u=Depends(admin)):
        try:
            await get_updater().start()
        except UpdateError as e:
            raise HTTPException(400, str(e))
        audit(u, "aggiornamento avviato")
        return {"ok": True}

    # ---------- manutenzione e backup
    @app.get("/api/maintenance")
    async def maintenance_info(u=Depends(admin)):
        return {**maintenance.settings(db), **maintenance.db_info(db),
                "telegram": bool(core.file_senders), "backup_crypto": maintenance.crypto_available()}

    @app.put("/api/maintenance")
    async def maintenance_set(body: MaintenanceIn, u=Depends(admin)):
        if body.retention_days is not None:
            if 0 < body.retention_days < 7:
                raise HTTPException(400, "tieni almeno 7 giorni di dati (0 = per sempre)")
            db.set("retention_days", body.retention_days)
        if body.backup_telegram is not None:
            if body.backup_telegram and not core.file_senders:
                raise HTTPException(400, "Telegram non è configurato")
            db.set("backup_telegram", "1" if body.backup_telegram else "0")
        if body.backup_password is not None:
            try:
                maintenance.set_backup_password(db, body.backup_password)
            except maintenance.BackupError as e:
                raise HTTPException(400, str(e))
            audit(u, "password del backup " + ("impostata" if body.backup_password else "rimossa"))
            if body.retention_days is None and body.backup_telegram is None:
                return {"ok": True}
        st = maintenance.settings(db)
        audit(u, "impostazioni manutenzione", f"conserva {st['retention_days'] or 'per sempre'} giorni, "
              f"backup Telegram {'sì' if st['backup_telegram'] else 'no'}")
        return {"ok": True}

    # ---------- riepilogo giornaliero su Telegram
    @app.get("/api/telegram/report")
    async def report_get(u=Depends(admin)):
        return core.report_settings()

    @app.put("/api/telegram/report")
    async def report_set(body: ReportIn, u=Depends(admin)):
        when = parse_hhmm(body.time)
        if not when:
            raise HTTPException(400, "orario non valido")
        if body.enabled and not core.reporters:
            raise HTTPException(400, "Telegram non è configurato")
        if body.enabled:
            core.set_report_time(when)
        else:
            core.db.set("tg_report_prev", when)
            core.set_report_time("")
        audit(u, "riepilogo automatico", f"ogni giorno alle {when}" if body.enabled else "disattivato")
        return core.report_settings()

    @app.post("/api/telegram/report/send")
    async def report_send(u=Depends(admin)):
        if not core.reporters:
            raise HTTPException(400, "Telegram non è configurato")
        for send in core.reporters:
            await send()
        audit(u, "riepilogo inviato su Telegram")
        return {"ok": True}

    @app.post("/api/maintenance/purge")
    async def maintenance_purge(u=Depends(admin)):
        if not maintenance.settings(db)["retention_days"]:
            raise HTTPException(400, "conservazione impostata a \"per sempre\": niente da pulire")
        return {"ok": True, "deleted": await core.run_purge(u["username"])}

    @app.get("/api/backup")
    async def backup_download(u=Depends(admin)):
        try:
            data, name = await asyncio.to_thread(maintenance.make_backup, db, core.cfg.get("_config_path"))
        except maintenance.BackupError as e:
            raise HTTPException(503, str(e))
        audit(u, "backup scaricato", name + (" (protetto da password)" if maintenance.settings(db)["backup_protected"] else ""))
        return Response(data, media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.post("/api/backup/telegram")
    async def backup_telegram(u=Depends(admin)):
        try:
            if not await core.send_backup(u["username"]):
                raise HTTPException(400, "Telegram non è configurato")
        except maintenance.BackupError as e:
            raise HTTPException(503, str(e))
        return {"ok": True}

    # ---------- avvisi batteria bassa
    @app.get("/api/alerts/battery")
    async def battery_alerts_info(u=Depends(admin)):
        return core.battery_alert_settings()

    @app.put("/api/alerts/battery")
    async def battery_alerts_set(body: BatteryAlertsIn, u=Depends(admin)):
        if body.batt_quiet is not None:
            q = body.batt_quiet.strip()
            if not HOURS_RE.match(q) or (q and q[:5] == q[6:]):
                raise HTTPException(400, "fascia di silenzio non valida (es. 22:00-07:00)")
            db.set("batt_quiet", q)
        if body.batt_repeat_h is not None:
            db.set("batt_repeat_h", body.batt_repeat_h)
        st = core.battery_alert_settings()
        audit(u, "avvisi batteria", (f"ripeti ogni {st['batt_repeat_h']} ore" if st["batt_repeat_h"] else "una volta sola")
              + (f", silenzio {st['batt_quiet']}" if st["batt_quiet"] else ""))
        return {"ok": True}

    # ---------- cache delle mappe
    # chi vede la mappa può chiedere le tessere: il Raspberry le scarica solo per chi ha accesso
    @app.get("/api/tiles/{layer}/{z}/{x}/{y}")
    async def tile(layer: str, z: int, x: int, y: int, u=Depends(current_user)):
        try:
            data, ctype, ttl = await tiles.get(layer, z, x, y)
        except TileError as e:
            raise HTTPException(e.status, str(e))
        return Response(data, media_type=ctype,
                        headers={"Cache-Control": f"private, max-age={max(60, min(ttl, 7 * 86400))}"})

    @app.get("/api/tilecache")
    async def tilecache_info(u=Depends(admin)):
        import shutil
        info = tiles.info()
        info["disk_free"] = shutil.disk_usage(Path(tiles.path).resolve().parent).free
        return info

    @app.put("/api/tilecache")
    async def tilecache_set(body: TileCacheIn, u=Depends(admin)):
        if body.enabled is not None:
            db.set("tile_cache", "1" if body.enabled else "0")
        if body.max_mb is not None:
            await asyncio.to_thread(tiles.set_limit, body.max_mb)
        i = tiles.info()
        audit(u, "cache delle mappe", f"{'attiva' if i['enabled'] else 'disattivata'}, massimo {i['max_mb']} MB")
        return {"ok": True}

    @app.post("/api/tilecache/clear")
    async def tilecache_clear(u=Depends(admin)):
        n = await asyncio.to_thread(tiles.clear)
        audit(u, "cache delle mappe svuotata", f"{n} tessere")
        return {"ok": True, "deleted": n}

    # ---------- varie
    @app.post("/api/advert")
    async def advert(u=Depends(admin)):
        await core.mesh.send_advert()
        audit(u, "advert gateway")
        return {"ok": True}

    @app.get("/api/audit")
    async def audit_log(u=Depends(admin)):
        return db.audit(300)

    @app.get("/api/cli/{node}")
    async def cli_history(node: str, u=Depends(admin)):
        return db.cli_history(node)

    @app.post("/api/cli")
    async def cli(body: CliIn, u=Depends(admin)):
        if is_dangerous(body.cmd) and not body.confirm:
            return {"ok": False, "needs_confirm": True}
        try:
            reply = await core.run_cli(body.node, body.cmd, u["username"])
        except (ValueError, RuntimeError) as e:
            raise HTTPException(400, str(e))
        return {"ok": reply is not None, "reply": reply}

    app.mount("/assets", StaticFiles(directory=WEB_DIR / "assets"), name="assets")
    app.mount("/vendor", StaticFiles(directory=WEB_DIR / "vendor"), name="vendor")
    return app
