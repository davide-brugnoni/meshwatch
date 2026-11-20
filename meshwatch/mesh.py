import asyncio
import logging

from meshcore import EventType, MeshCore

log = logging.getLogger("meshwatch.mesh")


def parse_lpp(lpp):
    """Estrae posizione e batteria dalla telemetria Cayenne LPP."""
    out = {"lat": None, "lon": None, "alt": None, "batt_v": None}
    for item in lpp or []:
        t = item.get("type")
        v = item.get("value")
        if t == "gps":
            if isinstance(v, dict):
                out["lat"], out["lon"], out["alt"] = v.get("latitude"), v.get("longitude"), v.get("altitude")
            elif isinstance(v, (list, tuple)) and len(v) >= 2:
                out["lat"], out["lon"] = v[0], v[1]
                out["alt"] = v[2] if len(v) > 2 else None
        elif t == "voltage" and out["batt_v"] is None and item.get("channel") == 1:
            out["batt_v"] = v
    if out["lat"] == 0 and out["lon"] == 0:   # GPS senza fix
        out["lat"] = out["lon"] = None
    return out


def find_serial_port():
    """Trova la porta della radio: prima /dev/serial/by-id, poi ttyUSB*/ttyACM*."""
    import glob
    for pattern in ("/dev/serial/by-id/*", "/dev/ttyUSB*", "/dev/ttyACM*"):
        found = sorted(glob.glob(pattern))
        if found:
            return found[0]
    return None


class Mesh:
    def __init__(self, port, baudrate=115200):
        self.port = port
        self.baudrate = baudrate
        self.mc = None
        self.lock = asyncio.Lock()   # una richiesta radio alla volta

    async def connect(self):
        while True:
            try:
                port = self.port
                if not port or port == "auto":
                    port = find_serial_port()
                    if not port:
                        log.warning("Nessuna radio USB trovata: collega la Heltec")
                        await asyncio.sleep(15)
                        continue
                log.info("Connessione alla radio su %s", port)
                self.mc = await MeshCore.create_serial(
                    port, self.baudrate, auto_reconnect=True, max_reconnect_attempts=1000)
                if self.mc:
                    self.mc.auto_update_contacts = True
                    await self.mc.ensure_contacts()
                    await self.mc.start_auto_message_fetching()   # serve per le risposte CLI
                    log.info("Radio connessa, %d contatti", len(self.mc.contacts))
                    return
            except Exception as e:
                log.error("Connessione fallita: %s", e)
            await asyncio.sleep(15)

    @property
    def connected(self):
        return bool(self.mc and self.mc.is_connected)

    async def contact(self, name):
        await self.mc.ensure_contacts(follow=True)
        return self.mc.get_contact_by_name(name)

    TYPES = {1: "companion", 2: "ripetitore", 3: "room server", 4: "sensore"}

    def contacts(self):
        """Contatti visti dal gateway: [{name, type, last_advert}]"""
        if not self.mc:
            return []
        out = [{"name": c.get("adv_name", "?"), "type": self.TYPES.get(c.get("type"), "altro"),
                "last_advert": c.get("last_advert")} for c in self.mc.contacts.values()]
        return sorted(out, key=lambda c: c["name"].lower())

    async def request_telemetry(self, name, attempts=2):
        """Chiede la telemetria al nodo. Ritorna dict (lat, lon, alt, batt_v) o None."""
        async with self.lock:
            c = await self.contact(name)
            if not c:
                log.warning("Contatto '%s' non trovato", name)
                return None
            for i in range(attempts):
                try:
                    if i > 0:
                        await self.mc.commands.reset_path(c)   # secondo tentativo in flood
                    lpp = await self.mc.commands.req_telemetry_sync(c, min_timeout=20)
                    if lpp is not None:
                        return parse_lpp(lpp)
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
            waiter = asyncio.create_task(self.mc.wait_for_event(
                EventType.CONTACT_MSG_RECV, attribute_filters={"pubkey_prefix": prefix}, timeout=timeout))
            await asyncio.sleep(0)   # il listener deve essere attivo prima dell'invio
            res = await self.mc.commands.send_cmd(c, cmd)
            if res is None or res.type == EventType.ERROR:
                waiter.cancel()
                raise RuntimeError("invio fallito")
            ev = await waiter
            return ev.payload.get("text") if ev else None

    # ---------- gateway (la Heltec collegata via USB)
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
