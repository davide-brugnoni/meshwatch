"""Permessi di TUTTI gli endpoint dell'API, per ogni ruolo.
Esecuzione:  python tests/test_permissions.py

POLICY dichiara per ogni endpoint il ruolo minimo. Il test:
- fallisce se l'app ha un endpoint non dichiarato qui (va deciso chi può usarlo);
- per ogni endpoint e ogni ruolo controlla che chi non ha i permessi venga respinto
  (401 login richiesto, 403 solo amministratore) e che chi li ha passi il controllo
  (qualunque altra risposta: 200, 400, 404, 422, … dipendono dai dati, non dai permessi).
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient  # noqa: E402

from meshwatch.auth import Auth  # noqa: E402
from meshwatch.core import Core  # noqa: E402
from meshwatch.db import DB  # noqa: E402
from meshwatch.web import create_app  # noqa: E402
from tests.test_chat import ChatMesh  # noqa: E402

# ruolo minimo: public (nessun controllo), guest (mappa libera o login), viewer, admin
POLICY = {
    ("POST", "/api/login"): "public",
    ("POST", "/api/logout"): "public",
    ("GET", "/api/me"): "public",
    ("POST", "/api/setup"): "public",
    ("GET", "/api/state"): "guest",
    ("GET", "/api/events"): "guest",
    ("GET", "/api/chat"): "viewer",
    ("GET", "/api/chat/messages"): "viewer",
    ("GET", "/api/chat/updates"): "viewer",
    ("POST", "/api/chat"): "viewer",
    ("POST", "/api/chat/read"): "viewer",
    ("DELETE", "/api/chat"): "admin",
    ("GET", "/api/network"): "viewer",
    ("GET", "/api/coverage"): "viewer",
    ("POST", "/api/network/neighbours"): "admin",
    ("POST", "/api/trackers/{tid}/poll"): "viewer",
    ("POST", "/api/trackers"): "admin",
    ("PUT", "/api/trackers/{tid}"): "admin",
    ("DELETE", "/api/trackers/{tid}"): "admin",
    ("DELETE", "/api/trackers/{tid}/track"): "admin",
    ("DELETE", "/api/track"): "admin",
    ("GET", "/api/trackers/{tid}/rules"): "admin",
    ("POST", "/api/trackers/{tid}/rules"): "admin",
    ("DELETE", "/api/trackers/{tid}/rules/{rid}"): "admin",
    ("POST", "/api/repeaters/poll"): "admin",
    ("POST", "/api/repeaters"): "admin",
    ("DELETE", "/api/repeaters/{name}"): "admin",
    ("GET", "/api/admin"): "admin",
    ("POST", "/api/users"): "admin",
    ("PUT", "/api/users/{username}"): "admin",
    ("DELETE", "/api/users/{username}"): "admin",
    ("GET", "/api/gateway"): "admin",
    ("POST", "/api/gateway"): "admin",
    ("GET", "/api/contacts"): "admin",
    ("POST", "/api/contacts/refresh"): "admin",
    ("POST", "/api/contacts/{key}/reset_path"): "admin",
    ("POST", "/api/contacts/{key}/favorite"): "admin",
    ("DELETE", "/api/contacts/{key}"): "admin",
    ("POST", "/api/contacts/pending/{key}"): "admin",
    ("PUT", "/api/contacts/settings"): "admin",
    ("POST", "/api/radio/reconnect"): "admin",
    ("POST", "/api/gateway/reboot"): "admin",
    ("GET", "/api/system"): "admin",
    ("POST", "/api/system/{action}"): "admin",
    ("GET", "/api/update"): "admin",
    ("POST", "/api/update/check"): "admin",
    ("POST", "/api/update/apply"): "admin",
    ("GET", "/api/maintenance"): "admin",
    ("PUT", "/api/maintenance"): "admin",
    ("POST", "/api/maintenance/purge"): "admin",
    ("GET", "/api/backup"): "admin",
    ("POST", "/api/backup/telegram"): "admin",
    ("GET", "/api/tiles/{layer}/{z}/{x}/{y}"): "guest",
    ("GET", "/api/alerts/battery"): "admin",
    ("PUT", "/api/alerts/battery"): "admin",
    ("GET", "/api/tilecache"): "admin",
    ("PUT", "/api/tilecache"): "admin",
    ("POST", "/api/tilecache/clear"): "admin",
    ("POST", "/api/advert"): "admin",
    ("GET", "/api/audit"): "admin",
    ("GET", "/api/cli/{node}"): "admin",
    ("POST", "/api/cli"): "admin",
}
LEVEL = {"public": 0, "guest": 1, "viewer": 2, "admin": 3}
# valori innocui per i parametri: nessuna azione distruttiva va a buon fine
PARAMS = {"{tid}": "999", "{rid}": "999", "{name}": "nessuno", "{username}": "nessuno", "{key}": "000000000000",
          "{action}": "nessuna", "{node}": "nessuno",
          "{layer}": "nessuna", "{z}": "0", "{x}": "0", "{y}": "0"}   # mappa inesistente: nessun download
# chi lo può usare riceve un flusso infinito: si controlla solo che gli altri vengano respinti
STREAMS = {("GET", "/api/events")}


def check(cond, msg):
    print(("OK   " if cond else "FAIL ") + msg)
    if not cond:
        sys.exit(1)


def denied(r):
    if r.status_code == 401:
        return True
    return r.status_code == 403 and (r.json() or {}).get("detail") == "solo amministratore"


def main():
    db = DB(os.path.join(tempfile.mkdtemp(), "t.db"))
    core = Core({}, db, ChatMesh())

    def client(**web):
        return TestClient(create_app(core, Auth(db), {"mode": "lan", **web}), raise_server_exceptions=False)

    admin = client()
    admin.post("/api/setup", json={"username": "admin1", "password": "segreto1"})
    admin.post("/api/users", json={"username": "fam", "password": "123456", "role": "viewer"})
    viewer = client()
    viewer.post("/api/login", json={"username": "fam", "password": "123456"})
    clients = {"nessuno": (client(public_map=False), 0), "ospite": (client(), 1), "viewer": (viewer, 2), "admin": (admin, 3)}

    routes = {(m, r.path) for r in admin.app.routes if getattr(r, "path", "").startswith("/api") for m in r.methods}
    check(not routes - set(POLICY), "ogni endpoint ha un ruolo dichiarato"
          + (f" (mancano: {sorted(routes - set(POLICY))})" if routes - set(POLICY) else ""))
    check(not set(POLICY) - routes, "nessun endpoint dichiarato che non esiste più"
          + (f" ({sorted(set(POLICY) - routes)})" if set(POLICY) - routes else ""))

    errors = []
    for who, (c, level) in clients.items():   # l'admin per ultimo: le sue chiamate cambiano i dati
        for (method, path), need in sorted(POLICY.items()):
            if need == "public" or ((method, path) in STREAMS and level >= LEVEL[need]):
                continue
            url = path
            for k, v in PARAMS.items():
                url = url.replace(k, v)
            r = c.request(method, url, json={} if method in ("POST", "PUT") else None)
            if denied(r) != (level < LEVEL[need]):
                errors.append(f"{who} {method} {path}: {r.status_code} {r.text[:80]}")
    check(not errors, f"permessi rispettati su {len(POLICY)} endpoint per 4 ruoli" + "".join("\n     " + e for e in errors))

    # stessa cosa su Internet: senza login non passa niente (tranne login e me)
    pub = client(mode="public")
    closed = [p for (m, p), need in POLICY.items() if need != "public" and m == "GET" and (m, p) not in STREAMS
              and pub.get(p.replace("{tid}", "1").replace("{node}", "x")).status_code != 401]
    check(not closed, "modalità public: tutto chiede il login" + (f" ({closed})" if closed else ""))
    print("Tutti i test superati.")


if __name__ == "__main__":
    main()
