"""Aggiornamento del firmware MeshCore.

Gateway (la radio collegata via USB al Raspberry): si aggiorna da qui, dalla pagina o dal bot.
- Le versioni arrivano dalle release di MeshCore su GitHub (tag "companion-vX.Y.Z"): ogni scheda
  ha i suoi file, chiamati "<build>_companion_radio_usb-vX.Y.Z-<commit>.<estensione>".
- La build giusta si propone dal modello che la radio dichiara (firmware_boards.py) e si può
  cambiare a mano: resta salvata nel database.
- ESP32 (Heltec, LilyGo, Station G2, Xiao S3/C3, …): si scrive solo l'applicazione con esptool,
  all'indirizzo letto dalla tabella delle partizioni, e si azzera "otadata" così la radio avvia
  l'applicazione appena scritta. Bootloader, tabella delle partizioni e dati (nome, chiavi,
  rubrica, canali) non si toccano. Prima si legge la tabella dalla radio: se non coincide con
  quella del firmware nuovo ci vorrebbe un'installazione completa, e ci si ferma.
- nRF52 (RAK 4631, T-Echo, Xiao nRF52, T114, …): pacchetto DFU (.zip) con adafruit-nrfutil via
  seriale. Il bootloader conserva i dati salvati nella memoria interna.
- RP2040 e STM32 non si aggiornano da remoto: servono il tasto BOOT o un programmatore.

Ripetitori: il firmware non passa dalla rete LoRa (troppo lenta). MeshWatch legge la versione
con il comando "ver", avvisa quando è vecchia e da /terminale si può lanciare "start ota"
quando si è vicino al ripetitore (WiFi del ripetitore per ESP32, Bluetooth per nRF52).
"""
import asyncio
import hashlib
import json
import logging
import os
import re
import struct
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from .firmware_boards import BOARDS

log = logging.getLogger("meshwatch.firmware")

RELEASES_URL = "https://api.github.com/repos/meshcore-dev/MeshCore/releases?per_page=40"
UA = {"User-Agent": "meshwatch-firmware", "Accept": "application/vnd.github+json"}
SUFFIX = "_companion_radio_usb"
ASSET_RE = re.compile(r"^(?P<board>.+?)" + SUFFIX + r"-(?P<ver>v\d+\.\d+(?:\.\d+)?)(?:-[0-9a-f]{6,})?"
                      r"(?P<merged>-merged)?\.(?P<ext>bin|zip|uf2|hex)$")
VERSION_RE = re.compile(r"v?(\d+)\.(\d+)(?:\.(\d+))?")
CACHE_S = 3600
PART_TABLE = 0x8000           # tabella delle partizioni ESP32: sempre qui
PART_TABLE_LEN = 0xC00
ESP_CHIPS = {0: "esp32", 2: "esp32s2", 5: "esp32c3", 9: "esp32s3", 12: "esp32c2", 13: "esp32c6", 16: "esp32h2"}
REPEATER_VERSION_EVERY_S = 24 * 3600
TOOLS = {"esp32": ("esptool", "esptool>=5,<6"), "nrf52": ("nordicsemi", "adafruit-nrfutil>=0.5.3")}


class FirmwareError(Exception):
    pass


def parse_version(text):
    """(1, 18, 0) da "v1.18.0-81dbae5", "1.12" o "v1.18.0 (Build: 15-Oct-2025)"; None se non c'è."""
    m = VERSION_RE.search(text or "")
    return tuple(int(x or 0) for x in m.groups()) if m else None


def fmt_version(v):
    return "v" + ".".join(map(str, v)) if v else "?"


def _norm(s):
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def suggest_boards(model):
    """Build candidate per il modello dichiarato dalla radio, la più probabile per prima."""
    if not model:
        return []
    envs = BOARDS.get(model)
    if envs is None:   # il nome può differire per maiuscole/spazi
        envs = next((v for k, v in BOARDS.items() if _norm(k) == _norm(model)), [])
    return sorted(envs, key=lambda e: (_norm(e) != _norm(model), len(e)))


def board_assets(release, board):
    """File della release per una build: {"app": …, "merged": …, "zip": …, "uf2": …} (solo quelli presenti)."""
    out = {}
    for a in release.get("assets", []):
        m = ASSET_RE.match(a["name"])
        if not m or m.group("board") != board:
            continue
        ext = m.group("ext")
        key = "merged" if m.group("merged") else ("app" if ext == "bin" else ext)
        out[key] = a
    return out


def board_kind(assets):
    """Come si aggiorna la build: esp32, nrf52 oppure None (non da remoto)."""
    if "app" in assets and "merged" in assets:
        return "esp32"
    if "zip" in assets:
        return "nrf52"
    return None


def release_boards(release):
    """Tutte le build companion USB presenti nella release (per la scelta manuale)."""
    found = {}
    for a in release.get("assets", []):
        m = ASSET_RE.match(a["name"])
        if m:
            found.setdefault(m.group("board"), set()).add("merged" if m.group("merged") else m.group("ext"))
    return sorted(b for b, kinds in found.items() if ("bin" in kinds and "merged" in kinds) or "zip" in kinds)


def parse_partitions(data):
    """Tabella delle partizioni ESP32 → {"otadata": (off, size), "app": (off, size)}.
    "app" è la prima applicazione (factory o ota_0): quella che si avvia con otadata vuoto."""
    parts = []
    for i in range(0, len(data) - 31, 32):
        e = data[i:i + 32]
        if e[:2] != b"\xaa\x50":
            break
        ptype, sub, off, size = e[2], e[3], *struct.unpack("<II", e[4:12])
        parts.append((ptype, sub, off, size, e[12:28].rstrip(b"\0").decode(errors="ignore")))
    apps = sorted((p for p in parts if p[0] == 0 and p[1] in (0x00, 0x10)), key=lambda p: p[2])
    ota = [p for p in parts if p[0] == 1 and p[1] == 0]
    if not apps:
        raise FirmwareError("tabella delle partizioni senza applicazione")
    return {"app": (apps[0][2], apps[0][3]), "otadata": (ota[0][2], ota[0][3]) if ota else None,
            "layout": [(p[0], p[1], p[2], p[3]) for p in parts]}


def esp_chip(image):
    """Chip a cui è destinata un'immagine ESP32 (dall'intestazione), None se non è un'immagine valida."""
    if len(image) < 24 or image[0] != 0xE9:
        return None
    return ESP_CHIPS.get(struct.unpack("<H", image[12:14])[0])


class FirmwareManager:
    """Controllo versioni e aggiornamento del gateway; versione dei ripetitori."""

    def __init__(self, core, workdir=None):
        self.core = core
        self.workdir = Path(workdir or os.path.join(tempfile.gettempdir(), "meshwatch-firmware"))
        self.job = {"state": "idle", "message": "", "ts": 0}
        self._releases = (0, None)
        self._task = None

    # ---------- rete e comandi (sostituibili nei test)
    def fetch_json(self, url):
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())

    def download(self, url, dest):
        req = urllib.request.Request(url, headers={"User-Agent": UA["User-Agent"]})
        with urllib.request.urlopen(req, timeout=120) as r, open(dest, "wb") as f:
            while True:
                chunk = r.read(65536)
                if not chunk:
                    break
                f.write(chunk)

    async def run(self, cmd, timeout=600):
        """Esegue un comando, ritorna (codice, output). L'output finisce anche nel log."""
        log.info("Eseguo: %s", " ".join(map(str, cmd)))
        p = await asyncio.create_subprocess_exec(*map(str, cmd), stdout=asyncio.subprocess.PIPE,
                                                 stderr=asyncio.subprocess.STDOUT)
        try:
            out, _ = await asyncio.wait_for(p.communicate(), timeout)
        except asyncio.TimeoutError:
            p.kill()
            raise FirmwareError(f"{cmd[2] if len(cmd) > 2 else cmd[0]}: tempo scaduto")
        text = out.decode(errors="replace")
        log.info("Uscita %s:\n%s", p.returncode, text[-3000:])
        return p.returncode, text

    @staticmethod
    def tool_available(module):
        import importlib.util
        return importlib.util.find_spec(module) is not None

    # ---------- release
    async def releases(self, force=False):
        ts, data = self._releases
        if data is None or force or time.time() - ts > CACHE_S:
            try:
                data = await asyncio.to_thread(self.fetch_json, RELEASES_URL)
            except Exception as e:
                if data is None:
                    raise FirmwareError(f"GitHub non raggiungibile: {e}")
                log.warning("Release MeshCore non aggiornate: %s", e)
            else:
                self._releases = (time.time(), data)
        return data

    async def latest(self, kind="companion", force=False):
        """Ultima release stabile per tipo (companion, repeater, room-server)."""
        best = None
        for r in await self.releases(force):
            tag = r.get("tag_name", "")
            if r.get("draft") or r.get("prerelease") or not tag.startswith(kind + "-v"):
                continue
            v = parse_version(tag[len(kind) + 1:])
            if v and (best is None or v > best[0]):
                best = (v, r)
        return best

    # ---------- scheda del gateway
    def saved_board(self):
        return self.core.db.get("gw_fw_board") or None

    def set_board(self, board):
        self.core.db.set("gw_fw_board", board or "")

    async def status(self, force=False):
        """Stato per la pagina e il bot."""
        mesh = self.core.mesh
        g = await mesh.gateway_info() if mesh.connected else {}
        model, installed = g.get("model"), g.get("firmware")
        if g:
            self.core.db.set("gw_fw_model", model or "")
        model = model or self.core.db.get("gw_fw_model") or None
        out = {"model": model, "installed": installed, "installed_version": fmt_version(parse_version(installed)) if installed else None,
               "job": self.job, "suggested": suggest_boards(model), "board": None, "boards": [],
               "latest": None, "available": False, "kind": None, "problems": [],
               "repeaters": self.repeater_versions()}
        try:
            lat = await self.latest("companion", force)
        except FirmwareError as e:
            out["problems"].append(str(e))
            lat = None
        board = self.saved_board() or (out["suggested"][0] if out["suggested"] else None)
        out["board"] = board
        if lat:
            v, rel = lat
            out["latest"] = {"version": fmt_version(v), "tag": rel["tag_name"], "url": rel.get("html_url"),
                             "date": rel.get("published_at")}
            out["boards"] = release_boards(rel)
            if board:
                assets = board_assets(rel, board)
                out["kind"] = board_kind(assets)
                if not assets:
                    out["problems"].append(f"nessun file per la scheda {board} nella {rel['tag_name']}")
                elif not out["kind"]:
                    out["problems"].append(f"{board}: questa scheda (RP2040/STM32) non si aggiorna da remoto, "
                                           "serve il tasto BOOT")
            cur = parse_version(installed)
            out["available"] = bool(cur and v > cur)
            rep = await self.latest("repeater")
            out["latest_repeater"] = fmt_version(rep[0]) if rep else None
        if not board:
            out["problems"].append("scheda non riconosciuta: sceglila dall'elenco")
        if getattr(mesh, "tcp", None):
            out["problems"].append("la radio è collegata via WiFi: l'aggiornamento richiede il cavo USB")
        elif mesh.connected and not getattr(mesh, "active_port", None):
            out["problems"].append("porta USB della radio sconosciuta")
        if not mesh.connected and self.job["state"] not in ("flash", "reconnect"):
            out["problems"].append("radio non collegata")
        tool = TOOLS.get(out["kind"])
        out["tool_missing"] = bool(tool and not self.tool_available(tool[0]))
        return out

    async def ensure_tool(self, kind):
        """Installa nel venv lo strumento per programmare la scheda, se manca (solo al primo uso:
        non sta in requirements.txt perché a MeshWatch non serve per funzionare)."""
        module, package = TOOLS[kind]
        if self.tool_available(module):
            return
        self._set("download", f"installo {package.split('>')[0]} (solo la prima volta)")
        code, out = await self.run([sys.executable, "-m", "pip", "install", "--no-cache-dir", "-q", package], timeout=1200)
        import importlib
        importlib.invalidate_caches()
        if code != 0 or not self.tool_available(module):
            raise FirmwareError(f"installazione di {package} non riuscita: " + _last_line(out))

    @property
    def busy(self):
        return self.job["state"] in ("check", "download", "flash", "reconnect")

    def _set(self, state, message):
        self.job = {"state": state, "message": message, "ts": int(time.time())}
        log.info("Firmware: %s – %s", state, message)
        self.core.events.publish("firmware")

    # ---------- aggiornamento del gateway
    async def start(self, user, board=None, notify=None):
        """Avvia l'aggiornamento in sottofondo. notify(testo) riceve le tappe (per il bot)."""
        if self.busy:
            raise FirmwareError("aggiornamento del firmware già in corso")
        if board:
            self.set_board(board)
        st = await self.status(force=True)
        if st["problems"]:
            raise FirmwareError("; ".join(st["problems"]))
        self._set("check", f"preparo {st['board']} {st['latest']['version']}")
        self.core.audit(user, "firmware gateway avviato", f"{st['board']} {st['installed']} → {st['latest']['version']}")
        self._task = asyncio.create_task(self._update(user, st, notify))
        self.core.tasks.add(self._task)
        self._task.add_done_callback(self.core.tasks.discard)
        return st

    async def _say(self, notify, text):
        if notify:
            try:
                await notify(text)
            except Exception as e:
                log.warning("Avviso firmware non inviato: %s", e)

    async def _update(self, user, st, notify):
        mesh = self.core.mesh
        _, rel = await self.latest("companion")
        assets = board_assets(rel, st["board"])
        target = parse_version(st["latest"]["version"])
        self.workdir.mkdir(parents=True, exist_ok=True)
        files = {}
        suspended = False
        try:
            await self.ensure_tool(st["kind"])
            self._set("download", "scarico il firmware da GitHub")
            for key in (("app", "merged") if st["kind"] == "esp32" else ("zip",)):
                a = assets[key]
                dest = self.workdir / a["name"]
                await asyncio.to_thread(self.download, a["browser_download_url"], dest)
                self._check_file(dest, a)
                files[key] = dest
            plan = self._esp32_plan(files) if st["kind"] == "esp32" else None
            await self._say(notify, f"📥 Firmware {st['latest']['version']} per {st['board']} scaricato e controllato. "
                                    "Scrivo nella radio: per qualche minuto niente radio, posizioni e chat.")
            self._set("flash", "scrivo il firmware nella radio (non scollegarla)")
            port = mesh.active_port
            await mesh.suspend()
            suspended = True
            await asyncio.sleep(2)   # la porta seriale si libera
            if st["kind"] == "esp32":
                await self._flash_esp32(port, plan, files)
            else:
                await self._flash_nrf52(port, files["zip"])
            self._set("reconnect", "firmware scritto: ricollego la radio")
            suspended = False
            ver = await self._resume(wait=120)
            if ver is None:
                raise FirmwareError("firmware scritto, ma la radio non risponde ancora: "
                                    "MeshWatch continua a cercarla da solo; se non torna scollega e ricollega il cavo USB")
            got = parse_version(ver)
            if got != target:
                raise FirmwareError(f"la radio risponde ma con la versione {ver} invece di {fmt_version(target)}")
            self._set("done", f"aggiornato a {ver}")
            self.core.audit(user, "firmware gateway aggiornato", ver)
            await self._say(notify, f"✅ Gateway aggiornato a {ver}. Radio di nuovo collegata.")
        except Exception as e:
            msg = str(e) if isinstance(e, FirmwareError) else f"errore imprevisto: {e}"
            log.exception("Aggiornamento firmware non riuscito") if not isinstance(e, FirmwareError) else None
            if suspended:
                await self._resume(wait=60)
            self._set("error", msg)
            self.core.audit(user, "firmware gateway non riuscito", msg[:300])
            await self._say(notify, f"❌ Aggiornamento del firmware non riuscito: {msg}")
        finally:
            for f in files.values():
                try:
                    f.unlink()
                except OSError:
                    pass

    @staticmethod
    def _check_file(path, asset):
        size = path.stat().st_size
        if asset.get("size") and size != asset["size"]:
            raise FirmwareError(f"{asset['name']}: scaricato incompleto ({size} di {asset['size']} byte)")
        digest = asset.get("digest") or ""
        if digest.startswith("sha256:"):
            h = hashlib.sha256(path.read_bytes()).hexdigest()
            if h != digest[7:]:
                raise FirmwareError(f"{asset['name']}: controllo sha256 non superato")

    @staticmethod
    def _esp32_plan(files):
        app = files["app"].read_bytes()
        merged = files["merged"].read_bytes()
        chip = esp_chip(app)
        if not chip:
            raise FirmwareError("il file del firmware non è un'immagine ESP32 valida")
        table = parse_partitions(merged[PART_TABLE:PART_TABLE + PART_TABLE_LEN])
        if len(app) > table["app"][1]:
            raise FirmwareError("il firmware non entra nella partizione dell'applicazione")
        if merged[table["app"][0]:table["app"][0] + len(app)] != app:
            raise FirmwareError("il file completo e quello di aggiornamento non coincidono")
        return {"chip": chip, "table": table}

    async def _flash_esp32(self, port, plan, files):
        esptool = [sys.executable, "-m", "esptool", "--chip", plan["chip"], "--port", port]
        current = self.workdir / "partizioni-radio.bin"
        code, out = await self.run(esptool + ["--baud", "115200", "read-flash", hex(PART_TABLE), hex(PART_TABLE_LEN), current],
                                   timeout=120)
        if code != 0:
            raise FirmwareError("non riesco a leggere la radio con esptool: " + _last_line(out))
        try:
            dev = parse_partitions(current.read_bytes())
        finally:
            current.unlink(missing_ok=True)
        if dev["layout"] != plan["table"]["layout"]:
            raise FirmwareError("la radio ha partizioni diverse dal firmware nuovo: serve un'installazione completa "
                                "dal PC (flasher web di MeshCore). Nella radio non è stato scritto nulla")
        cmd = esptool + ["--baud", "460800", "write-flash"]
        if plan["table"]["otadata"]:
            off, size = plan["table"]["otadata"]
            blank = self.workdir / "otadata-vuoto.bin"
            blank.write_bytes(b"\xff" * size)
            cmd += [hex(off), blank]
        cmd += [hex(plan["table"]["app"][0]), files["app"]]
        try:
            code, out = await self.run(cmd, timeout=600)
        finally:
            (self.workdir / "otadata-vuoto.bin").unlink(missing_ok=True)
        if code != 0:
            raise FirmwareError("scrittura non riuscita: " + _last_line(out) +
                                ". La radio potrebbe non avviarsi: riprova l'aggiornamento, il bootloader ESP32 non si cancella")

    async def _flash_nrf52(self, port, package):
        code, out = await self.run([sys.executable, "-m", "nordicsemi", "dfu", "serial", "--package", package,
                                    "-p", port, "-b", "115200", "--singlebank", "--touch", "1200"], timeout=600)
        if code != 0:
            raise FirmwareError("aggiornamento DFU non riuscito: " + _last_line(out))

    async def _resume(self, wait=120):
        """Ricollega la radio e ritorna la versione del firmware (None se non risponde in tempo)."""
        mesh = self.core.mesh
        task = asyncio.create_task(mesh.resume())
        self.core.tasks.add(task)
        task.add_done_callback(self.core.tasks.discard)
        end = time.time() + wait
        while time.time() < end:
            if mesh.connected:
                g = await mesh.gateway_info()
                if g.get("firmware"):
                    return g["firmware"]
            await asyncio.sleep(2)
        return None

    # ---------- ripetitori: versione letta con "ver"
    def repeater_versions(self):
        out = []
        for r in self.core.db.repeaters():
            raw = self.core.db.get(f"rep_fw:{r['name']}")
            v = json.loads(raw) if raw else None
            out.append({"name": r["name"], "version": v["text"] if v else None, "ts": v["ts"] if v else None})
        return out

    async def poll_repeater_version(self, name, force=False):
        raw = self.core.db.get(f"rep_fw:{name}")
        if raw and not force and time.time() - json.loads(raw)["ts"] < REPEATER_VERSION_EVERY_S:
            return None
        try:
            text = await self.core.mesh.cli(name, self.core.repeater_password(name), "ver")
        except Exception as e:
            log.info("Versione di %s non letta: %s", name, e)
            return None
        if not text or not parse_version(text):
            return None
        text = text.strip().splitlines()[0][:80]
        self.core.db.set(f"rep_fw:{name}", json.dumps({"text": text, "ts": int(time.time())}))
        return text

    async def outdated_repeaters(self):
        """Ripetitori con firmware più vecchio dell'ultima release: [(nome, versione, ultima)]."""
        try:
            lat = await self.latest("repeater")
        except FirmwareError:
            return []
        if not lat:
            return []
        out = []
        for r in self.repeater_versions():
            v = parse_version(r["version"])
            if v and v < lat[0]:
                out.append((r["name"], r["version"], fmt_version(lat[0])))
        return out


def _last_line(text):
    lines = [x.strip() for x in (text or "").splitlines() if x.strip()]
    return lines[-1][:200] if lines else "nessun dettaglio"
