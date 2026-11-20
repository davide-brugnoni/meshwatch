import re
import time
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .auth import hash_password
from . import system
from .core import is_dangerous

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
COOKIE = "meshwatch_session"
HOURS_RE = re.compile(r"^$|^([01]\d|2[0-3]):[0-5]\d-([01]\d|2[0-3]):[0-5]\d$")


class LoginIn(BaseModel):
    username: str
    password: str


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


class UserIn(BaseModel):
    username: str | None = Field(None, pattern=r"^[A-Za-z0-9_.-]{2,32}$")
    password: str | None = Field(None, min_length=6, max_length=128)
    role: str | None = Field(None, pattern=r"^(admin|viewer)$")


def create_app(core, auth, web_cfg):
    public = web_cfg.get("mode", "lan") == "public"
    db = core.db
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    def client_ip(req: Request):
        if public:   # dietro Cloudflare Tunnel / reverse proxy
            ip = req.headers.get("cf-connecting-ip") or req.headers.get("x-forwarded-for", "")
            if ip:
                return ip.split(",")[0].strip()
        return req.client.host if req.client else "?"

    def current_user(req: Request):
        u = auth.user_from_token(req.cookies.get(COOKIE))
        if not u:
            raise HTTPException(401, "login richiesto")
        return u

    def admin(u=Depends(current_user)):
        if u["role"] != "admin":
            raise HTTPException(403, "solo amministratore")
        return u

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
        if req.url.path.startswith("/api/"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.get("/")
    async def index():
        return FileResponse(WEB_DIR / "index.html")

    # ---------- sessione
    @app.post("/api/login")
    async def login(body: LoginIn, req: Request, resp: Response):
        token = auth.login(body.username, body.password, client_ip(req))
        if not token:
            raise HTTPException(401, "credenziali errate")
        resp.set_cookie(COOKIE, token, httponly=True, samesite="strict",
                        secure=public, max_age=30 * 86400)
        return {"ok": True}

    @app.post("/api/logout")
    async def logout(resp: Response):
        resp.delete_cookie(COOKIE)
        return {"ok": True}

    @app.get("/api/me")
    async def me(u=Depends(current_user)):
        return {**u, "auth": auth.enabled}

    # ---------- stato
    @app.get("/api/state")
    async def state(hours: int = 24, u=Depends(current_user)):
        return core.state(max(1, min(hours, 24 * 30)))

    # ---------- tracker
    @app.post("/api/trackers/{tid}/poll")
    async def poll_tracker(tid: int, u=Depends(current_user)):
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
        if "paused" in f and old["paused"] and not f["paused"]:
            core.schedule_now(tid)                 # ripreso: chiede subito
        elif "interval_min" in f:
            core.next_due[tid] = time.time() + f["interval_min"] * 60
        return {"ok": True}

    @app.delete("/api/trackers/{tid}")
    async def delete_tracker(tid: int, u=Depends(admin)):
        get_tracker(tid)
        db.delete_tracker(tid)
        core.next_due.pop(tid, None)
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
        return {"ok": True}

    @app.delete("/api/repeaters/{name}")
    async def delete_repeater(name: str, u=Depends(admin)):
        db.delete_repeater(name)
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
        return {"ok": True}

    @app.delete("/api/users/{username}")
    async def delete_user(username: str, u=Depends(admin)):
        target = db.user(username)
        if not target:
            raise HTTPException(404, "utente non trovato")
        admins = [x for x in db.users() if x["role"] == "admin"]
        if target["role"] == "admin" and len(admins) <= 1 and len(db.users()) > 1:
            raise HTTPException(400, "deve restare almeno un admin")
        if public and len(db.users()) <= 1:
            raise HTTPException(400, "in modalità public deve restare almeno un utente")
        db.delete_user(username)
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
        if errors:
            raise HTTPException(500, "non applicato: " + ", ".join(errors))
        return {"ok": True, "radio_changed": radio is not None}

    @app.post("/api/gateway/reboot")
    async def gateway_reboot(u=Depends(admin)):
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
        import asyncio
        asyncio.create_task(system.run_later(action))
        return {"ok": True}

    # ---------- varie
    @app.post("/api/advert")
    async def advert(u=Depends(admin)):
        await core.mesh.send_advert()
        return {"ok": True}

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

    app.mount("/vendor", StaticFiles(directory=WEB_DIR / "vendor"), name="vendor")
    return app
