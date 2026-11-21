import asyncio
import heapq
import itertools
import logging
import time

from meshcore import EventType, MeshCore

log = logging.getLogger("meshwatch.mesh")

# Lunghezza massima del testo di un messaggio MeshCore (byte UTF-8). Il firmware ne accetta
# circa 160; nei canali il nome del mittente ("Nome: ") viene aggiunto al testo e conta.
MAX_MSG_BYTES = 150

TXT_PLAIN, TXT_CLI_DATA, TXT_SIGNED = 0, 1, 2


class RadioLock:
    """Una richiesta radio alla volta, ma chi ha priorità (i messaggi della chat) passa davanti
    a quelli in coda (interrogazioni di posizione e ripetitori). Chi sta già usando la radio
    finisce comunque la sua richiesta."""

    def __init__(self):
        self._busy = False
        self._queue = []          # (priorità, ordine, future)
        self._order = itertools.count()

    def locked(self):
        return self._busy

    async def acquire(self, prio=1):
        if not self._busy and not self._queue:
            self._busy = True
            return
        fut = asyncio.get_running_loop().create_future()
        heapq.heappush(self._queue, (prio, next(self._order), fut))
        try:
            await fut
        except asyncio.CancelledError:
            if fut.done() and not fut.cancelled():
                self.release()        # il turno era già arrivato: lo passa al prossimo
            raise

    def release(self):
        while self._queue:
            _, _, fut = heapq.heappop(self._queue)
            if not fut.done():
                fut.set_result(True)   # _busy resta True: passa direttamente al prossimo
                return
        self._busy = False

    async def __aenter__(self):
        await self.acquire()

    async def __aexit__(self, *exc):
        self.release()

    def priority(self):
        lock = self

        class _Prio:
            async def __aenter__(self):
                await lock.acquire(0)

            async def __aexit__(self, *exc):
                lock.release()
        return _Prio()


def text_bytes(text):
    return len(text.encode("utf-8"))


def parse_lpp(lpp):
    """Estrae posizione, batteria e sensori dalla telemetria Cayenne LPP.
    sensors: [{channel, type, value}] con tutti i valori numerici tranne il GPS; i valori
    composti (es. accelerometro {acc_x, acc_y, acc_z}) diventano un sensore per campo."""
    out = {"lat": None, "lon": None, "alt": None, "batt_v": None, "sensors": []}
    for item in lpp or []:
        t = item.get("type")
        v = item.get("value")
        ch = item.get("channel", 0)
        if t == "gps":
            if isinstance(v, dict):
                out["lat"], out["lon"], out["alt"] = v.get("latitude"), v.get("longitude"), v.get("altitude")
            elif isinstance(v, (list, tuple)) and len(v) >= 2:
                out["lat"], out["lon"] = v[0], v[1]
                out["alt"] = v[2] if len(v) > 2 else None
            continue
        if t == "voltage" and out["batt_v"] is None and ch == 1:
            out["batt_v"] = v
        if isinstance(v, bool):
            v = int(v)
        if isinstance(v, (int, float)) and t:
            out["sensors"].append({"channel": ch, "type": t, "value": float(v)})
        elif isinstance(v, dict) and t:
            for k, x in v.items():
                if isinstance(x, (int, float)) and not isinstance(x, bool):
                    out["sensors"].append({"channel": ch, "type": f"{t}.{k}", "value": float(x)})
    if out["lat"] == 0 and out["lon"] == 0:   # GPS senza fix
        out["lat"] = out["lon"] = None
    return out


def decode_path(path_hex, hash_mode=0):
    """Percorso di un contatto ("out_path" in esadecimale) → elenco dei ripetitori come
    prefissi della chiave pubblica (1, 2 o 3 byte per salto secondo hash_mode)."""
    if not path_hex:
        return []
    size = 2 * ((hash_mode if hash_mode and hash_mode > 0 else 0) + 1)
    return [path_hex[i:i + size] for i in range(0, len(path_hex) - size + 1, size)]


# Chip USB delle radio MeshCore più diffuse (VID USB → descrizione). Le porte con questi chip
# vengono provate per prime; le altre porte USB dopo, nel caso di schede meno comuni.
KNOWN_USB = {
    0x10C4: "CP210x (Heltec V3, T-Beam, Station G2…)",
    0x1A86: "CH340/CH9102 (LilyGO, T-Beam…)",
    0x303A: "ESP32-S3/C3 USB nativa (Heltec V4, T-Deck, XIAO ESP32…)",
    0x239A: "nRF52 (RAK4631, WisMesh, T-Echo, XIAO nRF52…)",
    0x2886: "Seeed (SenseCAP T1000-E, Wio Tracker…)",
    0x1915: "Nordic nRF52",
    0x2E8A: "RP2040 (RAK11310, Pico…)",
    0x0403: "FTDI",
}


def serial_candidates():
    """Porte seriali dove può esserci la radio, le più probabili prima.
    Usa il percorso stabile /dev/serial/by-id quando c'è (non cambia se la porta si sposta)."""
    import glob
    import os
    by_id = {}
    for link in glob.glob("/dev/serial/by-id/*"):
        by_id[os.path.realpath(link)] = link
    ports = []
    try:
        from serial.tools import list_ports
        for p in list_ports.comports():
            if p.vid is None:          # porte interne del Raspberry (ttyAMA, ttyS): mai una radio
                continue
            rank = 0 if p.vid in KNOWN_USB else 1
            ports.append((rank, by_id.get(os.path.realpath(p.device), p.device)))
    except Exception as e:
        log.debug("Elenco porte seriali: %s", e)
    if not ports:                      # senza pyserial o senza informazioni USB
        ports = [(0, x) for x in sorted(by_id.values())]
        ports += [(1, x) for pat in ("/dev/ttyUSB*", "/dev/ttyACM*") for x in sorted(glob.glob(pat))
                  if x not in by_id]
    seen, out = set(), []
    for _, dev in sorted(ports):
        if dev not in seen:
            seen.add(dev)
            out.append(dev)
    return out


def find_serial_port():
    """La porta più probabile della radio (None se non c'è nessuna porta USB)."""
    found = serial_candidates()
    return found[0] if found else None


class Mesh:
    def __init__(self, port="auto", baudrate=115200, tcp=None):
        self.port = port
        self.baudrate = baudrate
        self.tcp = tcp               # (host, porta) per le radio col firmware "Companion WiFi"
        self.mc = None
        self.lock = RadioLock()      # una richiesta radio alla volta (la chat ha la precedenza)
        self.tried = {}              # nome → (salti, percorso) provato nell'ultima richiesta di telemetria
        self.connecting = False
        self.on_message = None       # funzione async(dict) chiamata per ogni messaggio ricevuto
        self.channels = []           # [{idx, name}] canali configurati nella radio
        self.cli_pending = set()     # prefissi dei ripetitori con un comando CLI in corso

    async def connect(self):
        """Si collega alla radio; riprova finché non ci riesce."""
        self.connecting = True
        try:
            await self._connect()
        finally:
            self.connecting = False

    async def reconnect(self):
        """Chiude la connessione e la rifà da capo, cercando di nuovo la porta USB
        (scollegando e ricollegando la radio la porta può cambiare, es. ttyUSB0 → ttyACM0)."""
        if self.connecting:
            return
        self.connecting = True
        try:
            async with self.lock:
                old, self.mc = self.mc, None
            if old:
                try:
                    await asyncio.wait_for(old.disconnect(), 10)
                except Exception as e:
                    log.warning("Chiusura della vecchia connessione: %s", e)
            await self._connect()
        finally:
            self.connecting = False

    @classmethod
    def from_config(cls, cfg):
        """connection: usb (default, qualsiasi scheda) oppure wifi (firmware Companion WiFi)."""
        if str(cfg.get("connection", "usb")).lower() in ("wifi", "tcp"):
            host = cfg.get("wifi_host")
            if not host:
                raise SystemExit("connection è 'wifi' ma manca wifi_host nel config")
            return cls(tcp=(str(host), int(cfg.get("wifi_port", 5000))))
        return cls(cfg.get("serial_port", "auto"), cfg.get("baudrate", 115200))

    async def _open(self, port):
        if self.tcp:
            log.info("Connessione alla radio via WiFi su %s:%d", *self.tcp)
            return await MeshCore.create_tcp(*self.tcp, auto_reconnect=True, max_reconnect_attempts=1000)
        log.info("Connessione alla radio su %s", port)
        return await MeshCore.create_serial(port, self.baudrate, auto_reconnect=True, max_reconnect_attempts=1000)

    async def _connect(self):
        while True:
            if self.tcp:
                targets = [None]
            elif self.port and self.port != "auto":
                targets = [self.port]
            else:
                targets = serial_candidates()
                if not targets:
                    log.warning("Nessuna radio USB trovata: collega la radio MeshCore (firmware Companion USB)")
            for port in targets:      # in automatico prova le porte una per una finché una risponde
                try:
                    self.mc = await self._open(port)
                    if self.mc:
                        await self._ready()
                        return
                    log.warning("Nessuna risposta MeshCore su %s", port or "%s:%d" % self.tcp)
                except Exception as e:
                    log.error("Connessione fallita (%s): %s", port or "WiFi", e)
                    self.mc = None
            await asyncio.sleep(15)

    async def _ready(self):
        self.mc.auto_update_contacts = True
        await self.mc.ensure_contacts()
        # i listener vanno registrati prima di leggere i messaggi in coda nella radio
        self.mc.subscribe(EventType.CONTACT_MSG_RECV, self._on_contact_msg)
        self.mc.subscribe(EventType.CHANNEL_MSG_RECV, self._on_channel_msg)
        await self.load_channels()
        await self.mc.start_auto_message_fetching()   # chat e risposte CLI
        log.info("Radio connessa, %d contatti, %d canali", len(self.mc.contacts), len(self.channels))

    @property
    def connected(self):
        return bool(self.mc and self.mc.is_connected)

    async def contact(self, name):
        await self.mc.ensure_contacts(follow=True)
        return self.mc.get_contact_by_name(name)

    TYPES = {1: "companion", 2: "ripetitore", 3: "room server", 4: "sensore"}

    @classmethod
    def contact_info(cls, c):
        lat, lon = c.get("adv_lat"), c.get("adv_lon")
        has_pos = lat is not None and lon is not None and not (lat == 0 and lon == 0)
        return {"name": c.get("adv_name", "?"), "type": cls.TYPES.get(c.get("type"), "altro"),
                "public_key": c.get("public_key", ""),
                "last_advert": c.get("last_advert"),
                "lat": lat if has_pos else None, "lon": lon if has_pos else None,
                "hops": c.get("out_path_len", -1),          # -1 = percorso sconosciuto (flood)
                "path": decode_path(c.get("out_path"), c.get("out_path_hash_mode")) if (c.get("out_path_len") or 0) > 0 else [],
                "favorite": bool((c.get("flags") or 0) & 1)}

    def contacts(self):
        """Contatti visti dal gateway: [{name, type, public_key, last_advert, lat, lon, hops, favorite}]"""
        if not self.mc:
            return []
        out = [self.contact_info(c) for c in self.mc.contacts.values()]
        return sorted(out, key=lambda c: c["name"].lower())

    def pending_contacts(self):
        """Nodi sentiti ma non ancora in rubrica (solo con l'aggiunta manuale attiva)."""
        if not self.mc:
            return []
        return sorted((self.contact_info(c) for c in self.mc.pending_contacts.values()),
                      key=lambda c: c["name"].lower())

    def self_info(self):
        return dict(self.mc.self_info or {}) if self.mc else {}

    def contact_by_key(self, key):
        """Contatto per chiave pubblica (o suo prefisso, almeno 12 caratteri)."""
        if not self.mc or not key or len(key) < 12:
            return None
        return self.mc.get_contact_by_key_prefix(key)

    RETRY_DELAY_S = 5   # pausa prima dell'ultimo tentativo: in movimento i buchi di copertura durano pochi secondi

    async def request_telemetry(self, name, attempts=3):
        """Chiede la telemetria al nodo. Ritorna dict (lat, lon, alt, batt_v, sensors, hops, path) o None.
        Primo tentativo sul percorso salvato, poi in flood; l'ultimo dopo una breve pausa.
        Il percorso provato per primo resta in self.tried[name] (hops, path), anche se non risponde."""
        async with self.lock:
            c = await self.contact(name)
            if not c:
                log.warning("Contatto '%s' non trovato", name)
                return None
            info = self.contact_info(c)
            self.tried[name] = (info["hops"], info["path"])
            for i in range(attempts):
                try:
                    if i > 0:
                        if i == attempts - 1:
                            await asyncio.sleep(self.RETRY_DELAY_S)
                        await self.mc.commands.reset_path(c)   # tentativi successivi in flood
                    lpp = await self.mc.commands.req_telemetry_sync(c, min_timeout=20)
                    if lpp is not None:
                        res = parse_lpp(lpp)
                        # percorso che il gateway usa ora per raggiungere il nodo
                        info = self.contact_info(self.mc.get_contact_by_name(name) or c)
                        res["hops"], res["path"] = info["hops"], info["path"]
                        return res
                    log.info("Telemetria %s: nessuna risposta al tentativo %d/%d (%s)", name, i + 1, attempts,
                             "flood" if i > 0 or info["hops"] < 0 else f"{info['hops']} salti via {','.join(info['path']) or 'diretto'}")
                except Exception as e:
                    log.error("Telemetria %s: %s", name, e)
            return None

    async def request_status(self, name, password, attempts=2):
        """Login sul ripetitore e richiesta statistiche. Ritorna dict o None."""
        async with self.lock:
            c = await self.contact(name)
            if not c:
                log.warning("Ripetitore '%s' non trovato tra i contatti", name)
                return None
            for i in range(attempts):
                try:
                    if i > 0:
                        await self.mc.commands.reset_path(c)
                    if password:
                        await self.mc.commands.send_login_sync(c, password, min_timeout=15)
                    st = await self.mc.commands.req_status_sync(c, min_timeout=20)
                    if st:
                        return st
                except Exception as e:
                    log.error("Status %s: %s", name, e)
            return None

    async def cli(self, name, password, cmd, timeout=30):
        """Invia un comando CLI al ripetitore (login prima) e ritorna il testo della risposta.
        Ritorna None se il ripetitore non risponde in tempo."""
        async with self.lock:
            c = await self.contact(name)
            if not c:
                raise ValueError(f"'{name}' non è tra i contatti del gateway")
            if password:
                await self.mc.commands.send_login_sync(c, password, min_timeout=15)
            prefix = c["public_key"][:12]
            self.cli_pending.add(prefix)   # la risposta non deve finire nella chat
            try:
                waiter = asyncio.create_task(self.mc.wait_for_event(
                    EventType.CONTACT_MSG_RECV, attribute_filters={"pubkey_prefix": prefix}, timeout=timeout))
                await asyncio.sleep(0)   # il listener deve essere attivo prima dell'invio
                res = await self.mc.commands.send_cmd(c, cmd)
                if res is None or res.type == EventType.ERROR:
                    waiter.cancel()
                    raise RuntimeError("invio fallito")
                ev = await waiter
                return ev.payload.get("text") if ev else None
            finally:
                asyncio.get_running_loop().call_later(5, self.cli_pending.discard, prefix)

    # ---------- chat
    def is_chat_message(self, p):
        """Le risposte CLI dei ripetitori arrivano con lo stesso evento dei messaggi:
        hanno il tipo di testo "CLI" oppure arrivano da un ripetitore a cui abbiamo appena
        mandato un comando. Quelle non sono chat."""
        if p.get("txt_type") not in (TXT_PLAIN, TXT_SIGNED):
            return False
        prefix = p.get("pubkey_prefix", "")
        if prefix in self.cli_pending:
            c = self.contact_by_key(prefix)
            if c is None or c.get("type") == 2:
                return False
        return True

    async def _on_contact_msg(self, ev):
        p = ev.payload or {}
        if not self.is_chat_message(p) or not self.on_message:
            return
        c = self.contact_by_key(p.get("pubkey_prefix", ""))
        await self._deliver({
            "kind": "dm", "peer": (c or {}).get("public_key", p.get("pubkey_prefix", ""))[:12],
            "peer_name": (c or {}).get("adv_name") or p.get("pubkey_prefix", "?"),
            "text": p.get("text", ""), "snr": p.get("SNR"), "path_len": p.get("path_len"),
            "sender_ts": p.get("sender_timestamp")})

    async def _on_channel_msg(self, ev):
        p = ev.payload or {}
        if p.get("txt_type") != TXT_PLAIN or not self.on_message:
            return
        text = p.get("text", "")
        sender, sep, body = text.partition(": ")   # nei canali il testo è "Mittente: messaggio"
        if not sep or len(sender) > 32:
            sender, body = "", text
        idx = p.get("channel_idx", 0)
        await self._deliver({
            "kind": "chan", "peer": str(idx), "peer_name": self.channel_name(idx),
            "sender": sender, "text": body, "snr": p.get("SNR"), "path_len": p.get("path_len"),
            "sender_ts": p.get("sender_timestamp")})

    async def _deliver(self, msg):
        try:
            await self.on_message(msg)
        except Exception as e:
            log.exception("Messaggio ricevuto non salvato: %s", e)

    def channel_name(self, idx):
        for ch in self.channels:
            if ch["idx"] == idx:
                return ch["name"]
        return "Public" if idx == 0 else f"Canale {idx}"

    async def load_channels(self, max_channels=8):
        """Legge i canali configurati nella radio (Public e quelli privati)."""
        out = []
        for idx in range(max_channels):
            try:
                ev = await self.mc.commands.get_channel(idx)
            except Exception as e:
                log.debug("canale %d: %s", idx, e)
                break
            if ev is None or ev.type == EventType.ERROR:
                break
            name = (ev.payload or {}).get("channel_name", "")
            secret = (ev.payload or {}).get("channel_secret") or b""
            if name or any(secret):
                out.append({"idx": idx, "name": name or ("Public" if idx == 0 else f"Canale {idx}")})
        self.channels = out
        return out

    ACK_MIN_WAIT = 8.0   # secondi di attesa minima della conferma per ogni tentativo

    async def send_text(self, key, text, on_status=None, attempts=3):
        """Messaggio privato con conferma di consegna (ACK). Dal terzo tentativo azzera il
        percorso e passa in flood. Ritorna True se consegnato, False se nessuna conferma."""
        async def status(s, **extra):
            if on_status:
                await on_status(s, **extra)

        async with self.lock.priority():
            c = self.contact_by_key(key)
            if not c:
                raise ValueError("contatto non trovato nella rubrica del gateway")
            await status("sending")
            loop = asyncio.get_running_loop()
            codes, acked = set(), loop.create_future()

            def on_ack(ev):
                code = (ev.attributes or {}).get("code") or (ev.payload or {}).get("code")
                if code in codes and not acked.done():
                    acked.set_result((ev.payload or {}).get("trip_time"))
                elif code and not acked.done():
                    early.add(code)
            early = set()
            sub = self.mc.subscribe(EventType.ACK, on_ack)   # prima dell'invio: l'ACK può arrivare subito
            ts = int(time.time())
            try:
                for i in range(attempts):
                    if i == attempts - 1 and i > 0:
                        await self.mc.commands.reset_path(c)   # ultimo tentativo in flood
                    res = await self.mc.commands.send_msg(c, text, ts, attempt=i)
                    if res is None or res.type == EventType.ERROR:
                        continue
                    code = res.payload["expected_ack"].hex()
                    codes.add(code)
                    if code in early and not acked.done():
                        acked.set_result(None)
                    await status("sent", attempt=i + 1)
                    wait = max(self.ACK_MIN_WAIT, res.payload.get("suggested_timeout", 8000) / 1000 * 1.2)
                    await asyncio.wait([acked], timeout=wait)
                    if acked.done():
                        await status("delivered", trip_ms=acked.result())
                        return True
            finally:
                sub.unsubscribe()
            await status("failed")
            return False

    async def send_channel(self, idx, text):
        """Messaggio su un canale: nessuna conferma di consegna (è una trasmissione a tutti)."""
        async with self.lock.priority():
            ev = await self.mc.commands.send_chan_msg(idx, text)
            return ev is not None and ev.type != EventType.ERROR

    # ---------- rubrica del gateway
    async def _contact_cmd(self, coro):
        async with self.lock.priority():
            ev = await coro
            if ev is None or ev.type == EventType.ERROR:
                raise RuntimeError("la radio ha rifiutato il comando")
            return ev

    async def refresh_contacts(self):
        async with self.lock:
            await self.mc.commands.get_contacts()

    async def remove_contact(self, c):
        await self._contact_cmd(self.mc.commands.remove_contact(c))
        self.mc.contacts.pop(c["public_key"], None)

    async def reset_contact_path(self, c):
        await self._contact_cmd(self.mc.commands.reset_path(c))

    async def set_favorite(self, c, on):
        flags = (c.get("flags") or 0)
        flags = flags | 1 if on else flags & ~1
        await self._contact_cmd(self.mc.commands.change_contact_flags(c, flags))

    async def add_pending(self, key):
        c = self.mc.pending_contacts.get(key)
        if not c:
            raise ValueError("nodo non più in attesa")
        await self._contact_cmd(self.mc.commands.add_contact(c))
        self.mc.pop_pending_contact(key)
        self.mc.contacts[key] = c

    async def set_manual_add(self, manual):
        await self._contact_cmd(self.mc.commands.set_manual_add_contacts(manual))
        async with self.lock:
            await self.mc.commands.send_appstart()

    # ---------- mappa della rete: vicini dei ripetitori
    async def request_neighbours(self, name, password=None):
        """Vicini sentiti dal ripetitore: [{prefix, snr, secs_ago}] o None."""
        async with self.lock:
            c = await self.contact(name)
            if not c:
                return None
            try:
                if password:
                    await self.mc.commands.send_login_sync(c, password, min_timeout=15)
                fetch = getattr(self.mc.commands, "fetch_all_neighbours", None)
                if fetch is None:
                    return None
                res = await fetch(c, min_timeout=20)
            except Exception as e:
                log.info("Vicini di %s: %s", name, e)
                return None
            if not res:
                return None
            return [{"prefix": n.get("pubkey", ""), "snr": n.get("snr"), "secs_ago": n.get("secs_ago")}
                    for n in res.get("neighbours", [])]

    # ---------- gateway (la radio collegata al Raspberry)
    async def _safe(self, coro):
        try:
            ev = await coro
            if ev is None or ev.type == EventType.ERROR:
                return {}
            return ev.payload or {}
        except Exception as e:
            log.debug("richiesta gateway fallita: %s", e)
            return {}

    async def gateway_info(self):
        """Stato e configurazione del gateway. Ritorna dict (vuoto se la radio non è collegata)."""
        if not self.connected:
            return {}
        async with self.lock:
            cmd = self.mc.commands
            await self._safe(cmd.send_appstart())          # aggiorna self_info
            dev = await self._safe(cmd.send_device_query())
            core = await self._safe(cmd.get_stats_core())
            radio = await self._safe(cmd.get_stats_radio())
            pk = await self._safe(cmd.get_stats_packets())
            bat = await self._safe(cmd.get_bat())
        si = dict(self.mc.self_info or {})
        return {
            "name": si.get("name"),
            "public_key": si.get("public_key"),
            "lat": si.get("adv_lat"), "lon": si.get("adv_lon"),
            "share_location": bool(si.get("adv_loc_policy")),
            "tx_power": si.get("tx_power"), "max_tx_power": si.get("max_tx_power"),
            "radio_freq": si.get("radio_freq"), "radio_bw": si.get("radio_bw"),
            "radio_sf": si.get("radio_sf"), "radio_cr": si.get("radio_cr"),
            "model": dev.get("model"), "firmware": dev.get("ver") or dev.get("fw ver"),
            "fw_build": dev.get("fw_build"),
            "battery_mv": core.get("battery_mv") or bat.get("level"),
            "uptime": core.get("uptime_secs"), "queue_len": core.get("queue_len"),
            "noise_floor": radio.get("noise_floor"), "last_rssi": radio.get("last_rssi"),
            "last_snr": radio.get("last_snr"),
            "tx_air_secs": radio.get("tx_air_secs"), "rx_air_secs": radio.get("rx_air_secs"),
            "recv": pk.get("recv"), "sent": pk.get("sent"),
            "contacts": len(self.mc.contacts or {}),
        }

    async def gateway_set(self, name=None, lat=None, lon=None, share_location=None,
                          tx_power=None, radio=None):
        """Applica le modifiche richieste. radio = (freq MHz, bw kHz, sf, cr)."""
        async with self.lock:
            cmd = self.mc.commands
            errors = []

            async def run(label, coro):
                ev = await coro
                if ev is None or ev.type == EventType.ERROR:
                    errors.append(label)

            if name is not None:
                await run("nome", cmd.set_name(name))
            if lat is not None and lon is not None:
                await run("posizione", cmd.set_coords(lat, lon))
            if share_location is not None:
                await run("condivisione posizione", cmd.set_advert_loc_policy(1 if share_location else 0))
            if tx_power is not None:
                await run("potenza", cmd.set_tx_power(tx_power))
            if radio is not None:
                await run("parametri radio", cmd.set_radio(*radio))
            await self._safe(cmd.send_appstart())
            return errors

    async def gateway_reboot(self):
        async with self.lock:
            await self.mc.commands.reboot()

    async def send_advert(self):
        async with self.lock:
            await self.mc.commands.send_advert(flood=True)
