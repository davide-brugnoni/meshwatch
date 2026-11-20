"""Test di chat, rubrica del gateway, mappa della rete e coda radio, con radio simulata.
Esecuzione:  python tests/test_chat.py
"""
import asyncio
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient  # noqa: E402

from meshwatch.auth import Auth  # noqa: E402
from meshwatch.core import Core  # noqa: E402
from meshwatch.db import DB  # noqa: E402
from meshwatch.mesh import Mesh, RadioLock, text_bytes  # noqa: E402
from meshwatch.web import create_app  # noqa: E402

ALICE = "a1" * 32
REP = "b2" * 32
REP2 = "c3" * 32
GW = "d4" * 32


def contact(key, name, typ, lat=None, lon=None, hops=-1, flags=0):
    return {"public_key": key, "adv_name": name, "type": typ, "flags": flags, "out_path_len": hops,
            "last_advert": int(time.time()) - 60, "adv_lat": lat or 0.0, "adv_lon": lon or 0.0}


class ChatMesh:
    """Radio simulata: usa i metodi veri di Mesh per rubrica e filtri, finge invii e ACK."""
    connected = True
    connecting = False
    contact_info = Mesh.contact_info
    TYPES = Mesh.TYPES

    def __init__(self):
        self.raw = {ALICE: contact(ALICE, "Alice", 1, 45.0, 9.0),
                    REP: contact(REP, "Rip-Monte", 2, 45.1, 9.1, hops=0),
                    REP2: contact(REP2, "Rip-Valle", 2, 45.2, 9.2, hops=1)}
        self.pending = {"e5" * 32: contact("e5" * 32, "Nuovo", 1)}
        self.channels = [{"idx": 0, "name": "Public"}, {"idx": 1, "name": "Famiglia"}]
        self.cli_pending = set()
        self.on_message = None
        self.sent = []
        self.ack = True
        self.removed = []
        self.manual = False

    def self_info(self):
        return {"name": "Casa", "adv_lat": 45.05, "adv_lon": 9.05, "public_key": GW, "manual_add_contacts": self.manual}

    def contacts(self):
        return sorted((Mesh.contact_info(c) for c in self.raw.values()), key=lambda c: c["name"].lower())

    def pending_contacts(self):
        return [Mesh.contact_info(c) for c in self.pending.values()]

    def contact_by_key(self, key):
        if not key or len(key) < 12:
            return None
        return next((c for k, c in self.raw.items() if k.startswith(key)), None)

    def channel_name(self, idx):
        return Mesh.channel_name(self, idx)

    async def send_text(self, key, text, on_status):
        await on_status("sending")
        await on_status("sent", attempt=1)
        self.sent.append(("dm", key, text))
        await on_status("delivered" if self.ack else "failed", **({"trip_ms": 900} if self.ack else {}))
        return self.ack

    async def send_channel(self, idx, text):
        self.sent.append(("chan", idx, text))
        return True

    async def request_status(self, name, pw):
        return {"bat": 4000}

    async def request_neighbours(self, name, pw):
        return [{"prefix": REP2[:8], "snr": 6.25, "secs_ago": 120}, {"prefix": "ffffffff", "snr": 1, "secs_ago": 5}]

    async def remove_contact(self, c):
        self.removed.append(c["public_key"])
        self.raw.pop(c["public_key"])

    async def reset_contact_path(self, c):
        c["out_path_len"] = -1

    async def set_favorite(self, c, on):
        c["flags"] = 1 if on else 0

    async def add_pending(self, key):
        self.raw[key] = self.pending.pop(key)

    async def set_manual_add(self, manual):
        self.manual = manual

    async def refresh_contacts(self):
        pass


def check(cond, msg):
    print(("OK   " if cond else "FAIL ") + msg)
    if not cond:
        sys.exit(1)


async def settle():
    for _ in range(5):
        await asyncio.sleep(0)


def main():
    # ---------- coda radio: la chat passa davanti, chi usa la radio finisce
    async def lock_order():
        lock, order = RadioLock(), []

        async def job(name, prio, hold=0.02):
            if prio:
                async with lock:
                    order.append(name)
                    await asyncio.sleep(hold)
            else:
                async with lock.priority():
                    order.append(name)
                    await asyncio.sleep(hold)
        first = asyncio.create_task(job("poll-in-corso", 1, 0.05))
        await asyncio.sleep(0.01)
        rest = [asyncio.create_task(job("poll-2", 1)), asyncio.create_task(job("poll-3", 1))]
        await asyncio.sleep(0)
        rest.append(asyncio.create_task(job("chat", 0)))
        await asyncio.gather(first, *rest)
        # una attesa cancellata non deve bloccare la coda
        async with lock:
            t = asyncio.create_task(lock.acquire())
            await asyncio.sleep(0)
            t.cancel()
        async with lock:
            order.append("dopo-cancellazione")
        return order, lock.locked()
    order, busy = asyncio.run(lock_order())
    check(order == ["poll-in-corso", "chat", "poll-2", "poll-3", "dopo-cancellazione"] and not busy,
          "coda radio: il messaggio passa davanti ai poll in attesa")

    # ---------- filtro: le risposte CLI non finiscono in chat
    m = Mesh("auto")
    m.mc = type("MC", (), {"get_contact_by_key_prefix": lambda self, p: {"type": 2} if p.startswith("b2") else {"type": 1}})()
    check(m.is_chat_message({"txt_type": 0, "pubkey_prefix": ALICE[:12]}), "messaggio normale = chat")
    check(not m.is_chat_message({"txt_type": 1, "pubkey_prefix": REP[:12]}), "testo CLI escluso dalla chat")
    m.cli_pending.add(REP[:12])
    check(not m.is_chat_message({"txt_type": 0, "pubkey_prefix": REP[:12]}),
          "risposta di un ripetitore con comando in corso esclusa")
    check(text_bytes("città") == 6, "lunghezza in byte UTF-8")
    check(Mesh.contact_info(contact(ALICE, "A", 1))["lat"] is None, "posizione 0,0 = senza posizione")

    # ---------- invio vero (Mesh.send_text) con una radio finta che risponde con l'ACK
    from meshcore import EventType

    class Ev:
        def __init__(self, type, payload=None, attributes=None):
            self.type, self.payload, self.attributes = type, payload or {}, attributes or {}

    class FakeMC:
        def __init__(self, acks_on_attempt, early=False):
            self.subs, self.calls, self.resets = [], [], 0
            self.acks_on_attempt, self.early = acks_on_attempt, early
            self.commands = self
            self.contacts = {ALICE: contact(ALICE, "Alice", 1)}

        def get_contact_by_key_prefix(self, p):
            return next((c for k, c in self.contacts.items() if k.startswith(p)), None)

        def subscribe(self, typ, cb):
            self.subs.append(cb)
            mc = self

            class Sub:
                def unsubscribe(self):
                    mc.subs.remove(cb)
            return Sub()

        async def reset_path(self, c):
            self.resets += 1

        async def send_msg(self, c, text, ts, attempt=0):
            code = bytes([attempt + 1] * 4)
            self.calls.append((text, ts, attempt))
            if attempt + 1 == self.acks_on_attempt:
                ack = Ev(EventType.ACK, {"code": code.hex(), "trip_time": 1234}, {"code": code.hex()})
                if self.early:   # l'ACK arriva prima che send_msg ritorni
                    for cb in list(self.subs):
                        cb(ack)
                else:
                    asyncio.get_running_loop().call_later(0.01, lambda: [cb(ack) for cb in list(self.subs)])
            return Ev(EventType.MSG_SENT, {"expected_ack": code, "suggested_timeout": 1})

    async def real_send(acks_on, early=False):
        mesh_ = Mesh("auto")
        mesh_.mc = FakeMC(acks_on, early)
        states = []

        async def st(s, **k):
            states.append((s, k))
        mesh_.ACK_MIN_WAIT = 0.05
        ok = await asyncio.wait_for(mesh_.send_text(ALICE[:12], "ciao", st), 60)
        return ok, states, mesh_.mc
    ok, states, mc = asyncio.run(real_send(1))
    check(ok and states[-1] == ("delivered", {"trip_ms": 1234}) and len(mc.calls) == 1 and not mc.subs,
          "invio con ACK al primo tentativo")
    ok, states, mc = asyncio.run(real_send(3))
    check(ok and len(mc.calls) == 3 and mc.resets == 1 and len({c[1] for c in mc.calls}) == 1
          and [c[2] for c in mc.calls] == [0, 1, 2], "nuovi tentativi, ultimo in flood, stesso timestamp")
    ok, states, mc = asyncio.run(real_send(0))
    check(not ok and states[-1][0] == "failed" and not mc.subs, "nessun ACK dopo 3 tentativi")
    ok, states, mc = asyncio.run(real_send(1, early=True))
    check(ok and len(mc.calls) == 1, "ACK arrivato prima della risposta all'invio")

    # ---------- canali: "Mittente: testo"
    got = []

    async def chan_parse():
        mm_ = Mesh("auto")
        mm_.channels = [{"idx": 1, "name": "Famiglia"}]

        async def on(msg):
            got.append(msg)
        mm_.on_message = on
        await mm_._on_channel_msg(Ev(EventType.CHANNEL_MSG_RECV, {"txt_type": 0, "channel_idx": 1,
                                     "text": "Bob: ci vediamo: alle 8", "sender_timestamp": 5}))
    asyncio.run(chan_parse())
    check(got and got[0]["sender"] == "Bob" and got[0]["text"] == "ci vediamo: alle 8" and got[0]["peer_name"] == "Famiglia",
          "messaggio di canale: mittente e testo separati")

    # ---------- chat via API
    tmp = tempfile.mkdtemp()
    db = DB(os.path.join(tmp, "t.db"))
    mesh = ChatMesh()
    core = Core({}, db, mesh)
    check(mesh.on_message == core.on_message, "il core riceve i messaggi della radio")
    c = TestClient(create_app(core, Auth(db), {"mode": "lan"}))
    anon = TestClient(create_app(core, Auth(db), {"mode": "lan"}))
    check(anon.get("/api/chat").status_code == 401, "la chat chiede il login")
    c.post("/api/setup", json={"username": "admin1", "password": "segreto1"})
    c.post("/api/users", json={"username": "fam", "password": "123456", "role": "viewer"})
    v = TestClient(create_app(core, Auth(db), {"mode": "lan"}))
    v.post("/api/login", json={"username": "fam", "password": "123456"})

    lst = v.get("/api/chat").json()
    names = [t["name"] for t in lst["targets"]]
    check(names == ["Public", "Famiglia", "Alice"], "destinatari: canali e companion, non i ripetitori")
    check(lst["limits"]["dm"] == 150 and lst["limits"]["chan"] == 150 - len("Casa: "),
          "limite di lunghezza (nei canali conta anche il nome del gateway)")

    async def incoming():
        await core.on_message({"kind": "dm", "peer": ALICE[:12], "peer_name": "Alice", "text": "Ciao casa",
                               "snr": 7.5, "path_len": 2, "sender_ts": 1000})
        await core.on_message({"kind": "dm", "peer": ALICE[:12], "peer_name": "Alice", "text": "Ciao casa",
                               "snr": 3, "path_len": 3, "sender_ts": 1000})   # stesso messaggio da un altro ripetitore
        await core.on_message({"kind": "chan", "peer": "0", "peer_name": "Public", "sender": "Bob",
                               "text": "qualcuno in ascolto?", "sender_ts": 1001})
    asyncio.run(incoming())
    check(db.unread_count() == 2, "messaggi ricevuti salvati, doppioni scartati")
    check(v.get("/api/state").json()["unread"] == 2, "non letti nello stato della pagina")
    check("unread" not in anon.get("/api/state").json(), "l'ospite non vede i non letti")
    conv = v.get("/api/chat").json()["conversations"]
    check(len(conv) == 2 and {x["unread"] for x in conv} == {1}, "elenco conversazioni con non letti")
    msgs = v.get("/api/chat/messages", params={"kind": "dm", "peer": ALICE[:12]}).json()
    check(len(msgs) == 1 and msgs[0]["text"] == "Ciao casa" and msgs[0]["snr"] == 7.5, "messaggi di una conversazione")
    v.post("/api/chat/read", json={"kind": "dm", "peer": ALICE[:12]})
    check(db.unread_count() == 1, "segna come letto")

    # invio: in background, lo stato si aggiorna
    async def send_and_wait(client, body):
        r = client.post("/api/chat", json=body)
        await settle()
        return r
    r = asyncio.run(send_and_wait(v, {"kind": "dm", "peer": ALICE[:12], "text": "Arrivo alle 8"}))
    j = r.json()
    check(r.status_code == 200 and j["message"]["status"] in ("queued", "delivered"), "invio messaggio privato")

    # il TestClient usa un suo loop: il task di invio gira lì; diamo tempo
    time.sleep(0.2)
    m1 = db.message(j["message"]["id"])
    check(m1["status"] == "delivered" and m1["trip_ms"] == 900 and m1["user"] == "fam", "conferma di consegna (ACK)")
    check(("dm", ALICE[:12], "Arrivo alle 8") in mesh.sent, "il testo parte verso il contatto giusto")
    mesh.ack = False
    r = v.post("/api/chat", json={"kind": "dm", "peer": ALICE[:12], "text": "Mi senti?"})
    time.sleep(0.2)
    check(db.message(r.json()["message"]["id"])["status"] == "failed", "nessuna conferma = non consegnato")
    r = v.post("/api/chat", json={"kind": "chan", "peer": "1", "text": "Ciao famiglia"})
    time.sleep(0.2)
    check(db.message(r.json()["message"]["id"])["status"] == "sent" and ("chan", 1, "Ciao famiglia") in mesh.sent,
          "messaggio sul canale")
    upd = v.get("/api/chat/updates", params={"after": 0}).json()
    check(len(upd["messages"]) == 5 and any(x["status"] == "failed" for x in upd["status"]), "aggiornamenti per la pagina")

    check(v.post("/api/chat", json={"kind": "dm", "peer": ALICE[:12], "text": "è" * 76}).status_code == 400,
          "messaggio troppo lungo rifiutato (byte, non caratteri)")
    check(v.post("/api/chat", json={"kind": "chan", "peer": "5", "text": "x"}).status_code == 400, "canale inesistente")
    check(v.post("/api/chat", json={"kind": "dm", "peer": "000000000000", "text": "x"}).status_code == 400,
          "contatto inesistente")
    check(v.post("/api/chat", json={"kind": "dm", "peer": REP[:12], "text": "   "}).status_code in (400, 422), "messaggio vuoto")
    mesh.connected = False
    check(v.post("/api/chat", json={"kind": "dm", "peer": ALICE[:12], "text": "x"}).status_code == 503, "radio scollegata")
    mesh.connected = True
    for i in range(10):
        r = v.post("/api/chat", json={"kind": "chan", "peer": "0", "text": f"n{i}"})
    check(r.status_code == 429, "limite di messaggi al minuto (duty cycle)")
    time.sleep(0.2)
    acts = [x["action"] for x in c.get("/api/audit").json()]
    check("messaggio inviato" in acts, "invii nel registro attività")
    check(not any("Arrivo" in (x["detail"] or "") for x in c.get("/api/audit").json()), "il testo non va nel registro")
    check(v.delete("/api/chat", params={"kind": "dm", "peer": ALICE[:12]}).status_code == 403
          and c.delete("/api/chat", params={"kind": "dm", "peer": ALICE[:12]}).json()["deleted"] == 3,
          "solo l'admin cancella una conversazione")

    db._exec("UPDATE messages SET status='sending' WHERE id=(SELECT MAX(id) FROM messages)")
    Core({}, db, ChatMesh())
    check(not db._all("SELECT id FROM messages WHERE status IN ('queued','sending','sent') AND kind='dm'"),
          "invii interrotti da un riavvio segnati come non riusciti")

    # ---------- rubrica del gateway
    check(v.get("/api/contacts").status_code == 403, "rubrica solo admin")
    db.set_repeater("Rip-Monte", "pw")
    ct = c.get("/api/contacts").json()
    rm = next(x for x in ct["contacts"] if x["name"] == "Rip-Monte")
    check(len(ct["contacts"]) == 3 and rm["used_by"] == ["ripetitore Rip-Monte"] and rm["hops"] == 0
          and len(ct["pending"]) == 1, "rubrica con uso, salti e nodi in attesa")
    check(c.post(f"/api/contacts/{ALICE}/favorite", json={"on": True}).json()["ok"]
          and mesh.raw[ALICE]["flags"] == 1, "preferito")
    check(c.post(f"/api/contacts/{REP}/reset_path").json()["ok"] and mesh.raw[REP]["out_path_len"] == -1, "azzera percorso")
    mesh.raw[REP]["out_path_len"] = 0
    check(c.delete(f"/api/contacts/{REP}").status_code == 409, "rimozione di un contatto in uso chiede conferma")
    check(c.post(f"/api/contacts/pending/{'e5' * 32}").json()["ok"] and "e5" * 32 in mesh.raw, "aggiunge un nodo in attesa")
    check(c.delete(f"/api/contacts/{'e5' * 32}").json()["ok"] and mesh.removed == ["e5" * 32], "rimuove un contatto")
    check(c.put("/api/contacts/settings", json={"manual_add": True}).json()["ok"]
          and c.get("/api/contacts").json()["manual_add"], "aggiunta manuale dei contatti")
    check(c.delete("/api/contacts/000000000000").status_code == 404, "contatto inesistente")

    # ---------- mappa della rete
    async def poll():
        await core.poll_repeater("Rip-Monte")
        return await core.poll_neighbours("Rip-Monte")   # entro 6 ore non richiede di nuovo
    again = asyncio.run(poll())
    check(len(db.neighbours()) == 2 and again is None, "vicini salvati, richiesti al massimo ogni 6 ore")
    net = v.get("/api/network").json()
    kinds = sorted(link["kind"] for link in net["links"])
    nb = next(link for link in net["links"] if link["kind"] == "neighbour")
    check(net["gateway"]["name"] == "Casa" and len(net["nodes"]) == 3, "nodi con posizione e gateway")
    check(kinds == ["direct", "neighbour"] and {nb["a"], nb["b"]} == {"Rip-Monte", "Rip-Valle"} and nb["snr"] == 6.25,
          "collegamenti: vicini con SNR e nodi sentiti dal gateway (vicino sconosciuto ignorato)")
    check(anon.get("/api/network").status_code == 401, "mappa della rete con login")
    check(c.post("/api/network/neighbours").json()["result"] == {"Rip-Monte": True}, "aggiornamento vicini dalla pagina")
    db.delete_repeater("Rip-Monte")
    check(not db.neighbours(), "rimuovendo il ripetitore si cancellano i suoi vicini")

    # ---------- pulizia dei dati vecchi
    from meshwatch import maintenance
    db._exec("UPDATE messages SET ts=? WHERE id=(SELECT MIN(id) FROM messages)", (int(time.time()) - 400 * 86400,))
    n_before = db._one("SELECT COUNT(*) AS n FROM messages")["n"]
    maintenance.purge(db, 180)
    check(db._one("SELECT COUNT(*) AS n FROM messages")["n"] == n_before - 1, "pulizia dei messaggi vecchi")
    print("Tutti i test superati.")


if __name__ == "__main__":
    main()
