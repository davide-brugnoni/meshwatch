"""Aggiornamento del firmware MeshCore: release, scelta della scheda, scrittura ESP32 e nRF52
(con strumenti simulati), ricollegamento, versioni dei ripetitori, pagina e bot.
Esecuzione:  python tests/test_firmware.py
"""
import asyncio
import hashlib
import os
import struct
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient  # noqa: E402

from meshwatch import firmware as fwmod  # noqa: E402
from meshwatch.auth import Auth  # noqa: E402
from meshwatch.core import Core  # noqa: E402
from meshwatch.db import DB  # noqa: E402
from meshwatch.firmware import (FirmwareManager, board_assets, board_kind, parse_partitions,  # noqa: E402
                                parse_version, release_boards, suggest_boards)
from meshwatch.web import create_app  # noqa: E402
from tests.test_chat import ChatMesh  # noqa: E402
from tests.test_features import Ctx, FakeChat, FakeUpdate  # noqa: E402


def check(cond, msg):
    print(("OK   " if cond else "FAIL ") + msg)
    if not cond:
        sys.exit(1)


# ---------- immagini ESP32 finte, con la stessa struttura di quelle vere (Heltec V3)
LAYOUT = [(1, 2, 0x9000, 0x5000, b"nvs"), (1, 0, 0xE000, 0x2000, b"otadata"), (0, 0x10, 0x10000, 0x330000, b"app0"),
          (0, 0x11, 0x340000, 0x330000, b"app1"), (1, 0x82, 0x670000, 0x180000, b"spiffs")]


def part_table(layout=LAYOUT):
    t = b"".join(b"\xaa\x50" + bytes([a, b]) + struct.pack("<II", off, size) + label.ljust(16, b"\0") + b"\0" * 4
                 for a, b, off, size, label in layout)
    return t + b"\xeb\xeb" + b"\xff" * 14 + hashlib.md5(t).digest()   # come la tabella vera: riga MD5 in fondo


def app_image(chip=9, size=4096):
    head = bytes([0xE9, 3, 2, 0x3F]) + b"\0" * 8 + struct.pack("<H", chip) + b"\0" * 10
    return head + bytes(i % 251 for i in range(size - len(head)))


def merged_image(app, layout=LAYOUT):
    m = bytearray(b"\xff" * (0x10000 + len(app)))
    t = part_table(layout)
    m[0x8000:0x8000 + len(t)] = t
    m[0x10000:0x10000 + len(app)] = app
    return bytes(m)


APP = app_image()
FILES = {
    "Heltec_v3_companion_radio_usb-v1.18.0-81dbae5.bin": APP,
    "Heltec_v3_companion_radio_usb-v1.18.0-81dbae5-merged.bin": merged_image(APP),
    "RAK_4631_companion_radio_usb-v1.18.0-81dbae5.zip": b"PK-dfu-package",
    "RAK_4631_companion_radio_usb-v1.18.0-81dbae5.uf2": b"uf2",
    "Xiao_rp2040_companion_radio_usb-v1.18.0-81dbae5.uf2": b"uf2",
    "Xiao_rp2040_companion_radio_usb-v1.18.0-81dbae5.bin": b"bin",
    "Heltec_v3_companion_radio_ble-v1.18.0-81dbae5.bin": b"ble",
}


def asset(name, data):
    return {"name": name, "size": len(data), "digest": "sha256:" + hashlib.sha256(data).hexdigest(),
            "browser_download_url": "https://example.invalid/" + name}


RELEASES = [
    {"tag_name": "companion-v1.19.0", "prerelease": True, "draft": False, "assets": []},
    {"tag_name": "repeater-v1.18.0", "prerelease": False, "draft": False, "assets": []},
    {"tag_name": "companion-v1.18.0", "prerelease": False, "draft": False, "html_url": "https://example.invalid/rel",
     "published_at": "2026-10-15T09:00:00Z", "assets": [asset(n, d) for n, d in FILES.items()]},
    {"tag_name": "companion-v1.17.1", "prerelease": False, "draft": False, "assets": []},
]


class FwMesh(ChatMesh):
    """Radio simulata: modello, versione, porta USB, sospensione e ripresa."""

    def __init__(self):
        super().__init__()
        self.model, self.version = "Heltec V3", "v1.17.0-abc1234"
        self.active_port, self.tcp = "/dev/ttyUSB0", None
        self.suspended = False
        self.events = []
        self.rep_ver = {"Rip-Monte": "v1.17.0-abc (Build: 14-Aug-2026)", "Rip-Valle": "v1.18.0-81dbae5 (Build: 15-Oct-2026)"}

    async def gateway_info(self):
        if not self.connected:
            return {}
        return {"model": self.model, "firmware": self.version, "recv": 1}

    async def suspend(self):
        self.events.append("suspend")
        self.suspended, self.connected = True, False

    async def resume(self):
        self.events.append("resume")
        self.suspended = False
        await asyncio.sleep(0.01)
        self.connected = True

    async def cli(self, name, pw, cmd, timeout=30):
        assert cmd == "ver"
        return self.rep_ver.get(name)


class FakeManager(FirmwareManager):
    """Rete e comandi simulati: scarica da FILES, esptool/nrfutil registrano i comandi."""

    def __init__(self, core):
        super().__init__(core, workdir=tempfile.mkdtemp())
        self.cmds, self.device_layout, self.flash_ok, self.new_version = [], LAYOUT, True, "v1.18.0-81dbae5"
        self.tools = {"esptool": True, "nordicsemi": True}
        self.corrupt = None

    def fetch_json(self, url):
        return RELEASES

    def download(self, url, dest):
        name = url.rsplit("/", 1)[1]
        data = FILES[name]
        if name == self.corrupt:
            data = data[:-1] + b"X"
        with open(dest, "wb") as f:
            f.write(data)

    def tool_available(self, module):
        return self.tools.get(module, False)

    async def run(self, cmd, timeout=600):
        cmd = [str(x) for x in cmd]
        self.cmds.append(cmd)
        if cmd[2] == "pip":
            self.tools[{"esptool>=5,<6": "esptool", "adafruit-nrfutil>=0.5.3": "nordicsemi"}[cmd[-1]]] = True
            return 0, "installato"
        if "read-flash" in cmd:
            with open(cmd[-1], "wb") as f:
                f.write(part_table(self.device_layout) + b"\xff" * 0x800)
            return 0, "ok"
        if "write-flash" in cmd or "dfu" in cmd:
            if not self.flash_ok:
                return 2, "A fatal error occurred: Failed to connect to ESP32-S3"
            self.core.mesh.version = self.new_version
            return 0, "Hash of data verified."
        return 1, "comando sconosciuto"


def setup():
    db = DB(os.path.join(tempfile.mkdtemp(), "t.db"))
    mesh = FwMesh()
    core = Core({}, db, mesh)
    core.firmware = FakeManager(core)
    notes = []

    async def collect(text, actions=None):
        notes.append(text)
    core.notifiers.append(collect)
    return db, mesh, core, notes


async def wait_job(fw, limit=5):
    for _ in range(int(limit / 0.02)):
        if not fw.busy:
            return
        await asyncio.sleep(0.02)


def test_pure():
    check(parse_version("v1.18.0-81dbae5") == (1, 18, 0) and parse_version("1.12") == (1, 12, 0)
          and parse_version("v1.9.1 (Build: 1-Jan-2025)") == (1, 9, 1) and parse_version("boh") is None,
          "versioni lette da tutti i formati")
    check(suggest_boards("Heltec V3")[0] == "Heltec_v3" and "Heltec_WSL3" in suggest_boards("Heltec V3"),
          "Heltec V3: proposta Heltec_v3, WSL3 come alternativa")
    check(suggest_boards("RAK 4631") == ["RAK_4631"] and suggest_boards("rak 4631") == ["RAK_4631"]
          and suggest_boards("Seeed Tracker T1000-E") == ["t1000e"] and suggest_boards("Scheda ignota") == [],
          "altre schede riconosciute dal modello dichiarato")
    rel = RELEASES[2]
    check(set(board_assets(rel, "Heltec_v3")) == {"app", "merged"} and board_kind(board_assets(rel, "Heltec_v3")) == "esp32",
          "Heltec V3: file di aggiornamento e completo, tipo ESP32 (il firmware BLE è escluso)")
    check(board_kind(board_assets(rel, "RAK_4631")) == "nrf52" and board_kind(board_assets(rel, "Xiao_rp2040")) is None,
          "RAK 4631 = nRF52 (DFU), RP2040 non aggiornabile da remoto")
    check(release_boards(rel) == ["Heltec_v3", "RAK_4631"], "elenco delle schede aggiornabili dalla release")
    t = parse_partitions(part_table())
    check(t["app"] == (0x10000, 0x330000) and t["otadata"] == (0xE000, 0x2000) and len(t["layout"]) == 5,
          "tabella delle partizioni: applicazione e otadata (la riga MD5 viene ignorata)")
    plan = FirmwareManager._esp32_plan({"app": _tmp(APP), "merged": _tmp(merged_image(APP))})
    check(plan["chip"] == "esp32s3", "chip letto dall'immagine (ESP32-S3)")
    try:
        other = app_image()[:-1] + b"X"   # stesso firmware tranne un byte: un altro build
        FirmwareManager._esp32_plan({"app": _tmp(app_image()), "merged": _tmp(merged_image(other))})
        check(False, "file non coerenti accettati")
    except fwmod.FirmwareError as e:
        check("non coincidono" in str(e), "file di aggiornamento e completo diversi: rifiutati")
    try:
        FirmwareManager._esp32_plan({"app": _tmp(b"\0" * 100), "merged": _tmp(merged_image(APP))})
        check(False, "immagine non valida accettata")
    except fwmod.FirmwareError as e:
        check("immagine ESP32" in str(e), "file che non è un firmware ESP32: rifiutato")


def _tmp(data):
    fd, p = tempfile.mkstemp()
    os.write(fd, data)
    os.close(fd)
    from pathlib import Path
    return Path(p)


def test_status():
    db, mesh, core, notes = setup()
    fw = core.firmware
    run = asyncio.run
    st = run(fw.status())
    check(st["board"] == "Heltec_v3" and st["kind"] == "esp32" and st["available"] and st["latest"]["version"] == "v1.18.0"
          and not st["problems"], "Heltec V3 con v1.17.0: aggiornamento a v1.18.0 disponibile (pre-release ignorata)")
    check(st["latest_repeater"] == "v1.18.0", "ultima versione per i ripetitori")
    mesh.version = "v1.18.0-81dbae5"
    check(not run(fw.status())["available"], "già aggiornato: niente da fare")
    mesh.model = "Scheda ignota"
    st = run(fw.status())
    check(st["board"] is None and any("scheda non riconosciuta" in p for p in st["problems"]), "scheda sconosciuta: va scelta")
    fw.set_board("Xiao_rp2040")
    st = run(fw.status())
    check(st["board"] == "Xiao_rp2040" and any("BOOT" in p for p in st["problems"]), "RP2040: non si aggiorna da remoto")
    fw.set_board("RAK_4631")
    check(run(fw.status())["kind"] == "nrf52", "scheda scelta a mano: resta salvata")
    mesh.tcp = ("10.0.0.5", 5000)
    check(any("WiFi" in p for p in run(fw.status())["problems"]), "radio via WiFi: serve il cavo USB")
    mesh.tcp = None
    fw.tools["nordicsemi"] = False
    check(run(fw.status())["tool_missing"], "strumento mancante: verrà installato al primo uso")


def test_esp32_update():
    db, mesh, core, notes = setup()
    fw = core.firmware
    msgs = []

    async def say(t):
        msgs.append(t)

    async def go():
        await fw.start("admin1", notify=say)
        await wait_job(fw)
    asyncio.run(go())
    writes = [c for c in fw.cmds if "write-flash" in c]
    check(fw.job["state"] == "done" and "v1.18.0" in fw.job["message"], "ESP32: aggiornamento riuscito")
    w = writes[0]
    check("--chip" in w and w[w.index("--chip") + 1] == "esp32s3" and w[w.index("--port") + 1] == "/dev/ttyUSB0",
          "esptool con il chip dell'immagine e la porta della radio")
    i = w.index("write-flash")
    check(w[i + 1] == "0xe000" and w[i + 3] == "0x10000" and w[i + 4].endswith("-81dbae5.bin"),
          "scritti solo otadata (azzerato) e applicazione: bootloader e dati intatti")
    check(not any("merged" in x for x in w), "il file completo non viene scritto (cancellerebbe le impostazioni)")
    check(fw.cmds.index(next(c for c in fw.cmds if "read-flash" in c)) < fw.cmds.index(w),
          "prima si legge la tabella delle partizioni della radio")
    check(mesh.events == ["suspend", "resume"] and mesh.connected and not mesh.suspended, "radio staccata e ricollegata")
    check(any("scaricato" in m for m in msgs) and "✅ Gateway aggiornato" in msgs[-1], "tappe e esito mandati al bot")
    acts = [a["action"] for a in db.audit(20)]
    check("firmware gateway avviato" in acts and "firmware gateway aggiornato" in acts, "aggiornamento nel registro attività")
    check(not list(fw.workdir.glob("*.bin")), "file scaricati cancellati alla fine")


def test_esp32_errors():
    run = asyncio.run

    def attempt(prepare):
        db, mesh, core, notes = setup()
        fw = core.firmware
        prepare(fw, mesh)

        async def go():
            await fw.start("admin1")
            await wait_job(fw)
        run(go())
        return fw, mesh

    fw, mesh = attempt(lambda fw, mesh: setattr(fw, "device_layout", LAYOUT[:2] + [(0, 0, 0x10000, 0x1F0000, b"factory")]))
    check(fw.job["state"] == "error" and "partizioni diverse" in fw.job["message"]
          and not any("write-flash" in c for c in fw.cmds) and mesh.events == ["suspend", "resume"] and mesh.connected,
          "partizioni della radio diverse: non scrive nulla e ricollega la radio")
    fw, mesh = attempt(lambda fw, mesh: setattr(fw, "corrupt", "Heltec_v3_companion_radio_usb-v1.18.0-81dbae5.bin"))
    check(fw.job["state"] == "error" and "sha256" in fw.job["message"] and mesh.events == [],
          "download alterato: si ferma prima di staccare la radio")
    fw, mesh = attempt(lambda fw, mesh: setattr(fw, "flash_ok", False))
    check(fw.job["state"] == "error" and "Failed to connect" in fw.job["message"] and mesh.connected,
          "scrittura fallita: errore chiaro e radio ricollegata")
    fw, mesh = attempt(lambda fw, mesh: setattr(fw, "new_version", "v1.17.0-abc1234"))
    check(fw.job["state"] == "error" and "invece di v1.18.0" in fw.job["message"], "versione non cambiata: segnalato")
    fw, mesh = attempt(lambda fw, mesh: fw.tools.update(esptool=False))
    check(fw.job["state"] == "done" and fw.cmds[0][2:4] == ["pip", "install"] and fw.cmds[0][-1] == "esptool>=5,<6",
          "esptool mancante: installato al primo uso")
    db, mesh, core, notes = setup()
    mesh.version = "v1.18.0-81dbae5"

    async def twice():
        await core.firmware.start("admin1")
        try:
            await core.firmware.start("admin1")
            return False
        except fwmod.FirmwareError as e:
            return "già in corso" in str(e)
        finally:
            await wait_job(core.firmware)
    check(run(twice()), "due aggiornamenti insieme: il secondo rifiutato (reinstallazione permessa)")


def test_nrf52_update():
    db, mesh, core, notes = setup()
    mesh.model = "RAK 4631"
    fw = core.firmware

    async def go():
        await fw.start("admin1")
        await wait_job(fw)
    asyncio.run(go())
    dfu = next(c for c in fw.cmds if "dfu" in c)
    check(fw.job["state"] == "done" and dfu[2] == "nordicsemi" and dfu[dfu.index("--package") + 1].endswith(".zip")
          and "--touch" in dfu and dfu[dfu.index("-p") + 1] == "/dev/ttyUSB0",
          "nRF52 (RAK 4631): pacchetto DFU via seriale con adafruit-nrfutil")


def test_repeaters():
    db, mesh, core, notes = setup()
    fw = core.firmware
    run = asyncio.run
    db.set_repeater("Rip-Monte", "pw")
    db.set_repeater("Rip-Valle", "pw")
    run(core.poll_repeater("Rip-Monte"))
    run(fw.poll_repeater_version("Rip-Valle"))
    vers = {r["name"]: r["version"] for r in fw.repeater_versions()}
    check(vers["Rip-Monte"].startswith("v1.17.0") and vers["Rip-Valle"].startswith("v1.18.0"),
          "versione dei ripetitori letta con 'ver' durante il giro")
    mesh.rep_ver["Rip-Monte"] = "v1.18.0"
    run(fw.poll_repeater_version("Rip-Monte"))
    check(fw.repeater_versions()[0]["version"].startswith("v1.17.0"), "versione riletta al massimo una volta al giorno")
    mesh.rep_ver["Rip-Monte"] = "v1.17.0-abc (Build: 14-Aug-2026)"
    check([x[0] for x in run(fw.outdated_repeaters())] == ["Rip-Monte"], "ripetitore con firmware vecchio individuato")
    run(core.firmware_check())
    check(len(notes) == 2 and "gateway" in notes[0] and "/firmware" in notes[0]
          and "Rip-Monte" in notes[1] and "start ota" in notes[1], "avvisi: gateway e ripetitore da aggiornare")
    run(core.firmware_check())
    check(len(notes) == 2, "lo stesso avviso non si ripete")
    # controllo automatico: di base domenica alle 9, giorno e ora scelti per gateway e ripetitori
    from datetime import datetime
    sch = core.firmware_schedule()
    check(sch["gateway"] == sch["repeaters"] == {"enabled": True, "day": 6, "time": "09:00"},
          "controllo automatico: di base domenica alle 9")
    slot = core.firmware_slot
    gw = sch["gateway"]
    check(slot(gw, datetime(2026, 11, 29, 8, 59)) is None and slot(gw, datetime(2026, 11, 28, 9, 30)) is None
          and slot(gw, datetime(2026, 11, 30, 9, 0)) is None, "niente controllo prima dell'ora o fuori dal giorno")
    check(slot(gw, datetime(2026, 11, 29, 9, 0)) == "2026-11-29" and slot(gw, datetime(2026, 11, 29, 23, 0)) == "2026-11-29",
          "controllo la domenica dalle 9 (anche in ritardo, entro la giornata)")
    check(core.firmware_next(gw, datetime(2026, 11, 26, 18, 0)) == datetime(2026, 11, 29, 9, 0)
          and core.firmware_next(gw, datetime(2026, 11, 29, 9, 0)) == datetime(2026, 12, 6, 9, 0), "prossimo controllo")
    calls = []
    orig = core.firmware_check

    async def spy(gateway=True, repeaters=True):
        calls.append((gateway, repeaters))
        return await orig(gateway=gateway, repeaters=repeaters)
    core.firmware_check = spy
    check(run(core.firmware_tick(datetime(2026, 11, 28, 9, 0))) == [] and not calls, "sabato: nessun controllo")
    check(run(core.firmware_tick(datetime(2026, 11, 29, 9, 1))) == ["gateway", "repeaters"]
          and calls == [(True, False), (False, True)], "domenica alle 9: gateway e ripetitori")
    check(run(core.firmware_tick(datetime(2026, 11, 29, 15, 0))) == [] and len(calls) == 2, "una volta sola per domenica")
    core.set_firmware_schedule("repeaters", True, 7, "18:30")
    core.set_firmware_schedule("gateway", False, 6, "09:00")
    check(run(core.firmware_tick(datetime(2026, 11, 30, 18, 0))) == [], "ripetitori ogni giorno alle 18:30: non prima")
    check(run(core.firmware_tick(datetime(2026, 11, 30, 18, 31))) == ["repeaters"], "ripetitori ogni giorno alle 18:30")
    check(run(core.firmware_tick(datetime(2026, 12, 6, 19, 0))) == ["repeaters"], "gateway spento: solo i ripetitori")
    core.db.set("fw_schedule", "{}")
    core.firmware_check = orig


def test_web_and_bot():
    from meshwatch.bot import Bot
    db, mesh, core, notes = setup()
    db.set_repeater("Rip-Monte", "pw")
    run = asyncio.run
    # "with": il ciclo di eventi resta acceso tra una richiesta e l'altra, così l'aggiornamento
    # avviato dalla pagina continua in sottofondo come sul Raspberry
    with TestClient(create_app(core, Auth(db), {"mode": "lan"})) as c:
        c.post("/api/setup", json={"username": "admin1", "password": "segreto1"})
        st = c.get("/api/firmware").json()
        check(st["board"] == "Heltec_v3" and st["available"] and st["repeaters"][0]["name"] == "Rip-Monte", "/api/firmware")
        check(c.put("/api/firmware/board", json={"board": "../x"}).status_code == 422, "nome della scheda non valido rifiutato")
        r = c.get("/api/firmware/schedule").json()
        check(r["gateway"]["text"] == "ogni domenica alle 09:00" and r["days"][7] == "ogni giorno" and r["repeaters"]["next"],
              "/api/firmware/schedule")
        r = c.put("/api/firmware/schedule", json={"kind": "repeaters", "enabled": True, "day": 0, "time": "7.15"}).json()
        check(r["repeaters"]["text"] == "ogni lunedì alle 07:15" and r["gateway"]["day"] == 6, "orario dei ripetitori dalla pagina")
        check(c.put("/api/firmware/schedule", json={"kind": "x", "enabled": True}).status_code == 422
              and c.put("/api/firmware/schedule", json={"kind": "gateway", "enabled": True, "day": 9}).status_code == 422
              and c.put("/api/firmware/schedule", json={"kind": "gateway", "enabled": True, "time": "25:00"}).status_code == 400,
              "impostazioni non valide rifiutate")
        r = c.put("/api/firmware/schedule", json={"kind": "gateway", "enabled": False, "day": 6, "time": "09:00"}).json()
        check(not r["gateway"]["enabled"] and r["gateway"]["next"] is None and r["gateway"]["text"] == "spento",
              "controllo del gateway spento dalla pagina")
        check(c.put("/api/firmware/board", json={"board": "Heltec_WSL3"}).json()["board"] == "Heltec_WSL3", "scheda scelta dalla pagina")
        check(c.post("/api/firmware/update", json={"board": "Heltec_v3"}).status_code == 400, "senza conferma non parte")
        r = c.post("/api/firmware/update", json={"board": "Heltec_v3", "confirm": True}).json()
        check(r["ok"] and r["version"] == "v1.18.0", "aggiornamento avviato dalla pagina")
        check(c.post("/api/firmware/repeaters").status_code in (200, 409), "durante l'aggiornamento: niente richieste alla radio")
        import time
        for _ in range(250):   # l'aggiornamento continua in sottofondo (sul CI può essere lento)
            if not core.firmware.busy:
                break
            time.sleep(0.02)
        check(core.firmware.job["state"] == "done", "aggiornamento dalla pagina concluso")
        check(c.post("/api/firmware/repeaters").json()["repeaters"][0]["version"].startswith("v1.17.0"),
              "versioni dei ripetitori rilette dalla pagina")

    db2, mesh2, core2, _ = setup()
    db2.set_repeater("Rip-Monte", "pw")
    run(core2.firmware.poll_repeater_version("Rip-Monte"))
    bot = Bot({"token": "123456:TEST", "allowed_chat_ids": [1, 2], "admin_chat_ids": [1]}, core2)
    chat, other = FakeChat(1), FakeChat(2)
    run(bot.firmware_cmd(FakeUpdate(other), Ctx()))
    check("Solo le chat admin" in other.sent[-1], "/firmware solo admin")

    class Q:
        def __init__(self):
            self.text = None

        async def edit_message_text(self, text, **kw):
            self.text = text

        async def edit_message_reply_markup(self, markup):
            pass
    sent_kb = []
    orig = chat.send_message

    async def send(text, **kw):
        sent_kb.append(kw.get("reply_markup"))
        await orig(text, **kw)
    chat.send_message = send
    run(bot.firmware_cmd(FakeUpdate(chat), Ctx()))
    txt = chat.sent[-1]
    check("Heltec V3" in txt and "v1.17.0" in txt and "v1.18.0" in txt and "🟠 Rip-Monte" in txt and "start ota" in txt,
          "/firmware: gateway, ultima versione e ripetitore da aggiornare")
    datas = [b.callback_data for row in sent_kb[-1].inline_keyboard for b in row]
    check("fw:go" in datas and "fw:b:Heltec_WSL3" in datas, "pulsanti: aggiorna e scheda alternativa")
    q = Q()
    run(bot.firmware_button(chat, q, "go"))
    check("Non scollegare" in chat.sent[-1], "prima chiede conferma")
    run(bot.firmware_button(chat, q, "no"))
    check("annullato" in q.text and core2.firmware.job["state"] == "idle", "Annulla: niente aggiornamento")

    async def yes():
        await bot.firmware_button(chat, q, "yes")
        await wait_job(core2.firmware)
    run(yes())
    check("avviato" in q.text and "✅ Gateway aggiornato" in chat.sent[-1], "conferma: aggiornato, esito in chat")
    run(bot.firmware_button(chat, Q(), "b:Heltec_WSL3"))
    check(core2.firmware.saved_board() == "Heltec_WSL3", "scheda cambiata dal bot")


def main():
    test_pure()
    test_status()
    test_esp32_update()
    test_esp32_errors()
    test_nrf52_update()
    test_repeaters()
    test_web_and_bot()
    print("Tutti i test superati.")


if __name__ == "__main__":
    main()
