"""Stato del Raspberry e comandi di riavvio/spegnimento."""
import asyncio
import os
import shutil
import time

# Il servizio gira come utente senza privilegi: riavvio e spegnimento passano da sudo,
# abilitato solo per questi due comandi (vedi /etc/sudoers.d/meshwatch).
SUDO_CMDS = {
    "reboot": ["sudo", "-n", "/usr/bin/systemctl", "reboot"],
    "poweroff": ["sudo", "-n", "/usr/bin/systemctl", "poweroff"],
}

THROTTLE_PATHS = ("/sys/devices/platform/soc/soc:firmware/get_throttled",
                  "/sys/devices/platform/soc/soc:firmware/raspberrypi-hwmon/get_throttled")


def _read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def _throttled():
    """Bit del firmware Raspberry: 0 = tensione bassa ora, 16 = tensione bassa dall'avvio."""
    for p in THROTTLE_PATHS:
        v = _read(p)
        if v:
            try:
                n = int(v, 16)
                return {"undervoltage_now": bool(n & 0x1), "undervoltage_since_boot": bool(n & 0x10000),
                        "throttled_now": bool(n & 0x4), "throttled_since_boot": bool(n & 0x40000)}
            except ValueError:
                pass
    # alternativa: il driver hwmon segnala l'allarme sulla tensione
    for root, _, files in os.walk("/sys/class/hwmon"):
        if "in0_lcrit_alarm" in files:
            v = _read(os.path.join(root, "in0_lcrit_alarm"))
            if v is not None:
                return {"undervoltage_now": v == "1", "undervoltage_since_boot": None,
                        "throttled_now": None, "throttled_since_boot": None}
    return {}


def info():
    temp = _read("/sys/class/thermal/thermal_zone0/temp")
    up = _read("/proc/uptime")
    mem = {}
    for line in (_read("/proc/meminfo") or "").splitlines():
        k, _, v = line.partition(":")
        if k in ("MemTotal", "MemAvailable"):
            mem[k] = int(v.split()[0]) // 1024
    disk = shutil.disk_usage("/")
    load = os.getloadavg() if hasattr(os, "getloadavg") else (None,)
    return {
        "hostname": os.uname().nodename,
        "model": (_read("/proc/device-tree/model") or "").rstrip("\x00") or None,
        "temp_c": round(int(temp) / 1000, 1) if temp else None,
        "uptime": int(float(up.split()[0])) if up else None,
        "load": round(load[0], 2) if load[0] is not None else None,
        "mem_total_mb": mem.get("MemTotal"), "mem_free_mb": mem.get("MemAvailable"),
        "disk_total_gb": round(disk.total / 1e9, 1), "disk_free_gb": round(disk.free / 1e9, 1),
        **_throttled(),
        "time": int(time.time()),
    }


async def can_sudo(action):
    """Verifica senza eseguire che sudo sia permesso per il comando."""
    try:
        p = await asyncio.create_subprocess_exec("sudo", "-n", "-l", *SUDO_CMDS[action][2:],
                                                 stdout=asyncio.subprocess.DEVNULL,
                                                 stderr=asyncio.subprocess.DEVNULL)
        return await p.wait() == 0
    except FileNotFoundError:
        return False


async def run_later(action, delay=2):
    """Esegue riavvio/spegnimento dopo qualche secondo, così la risposta arriva al browser."""
    await asyncio.sleep(delay)
    await asyncio.create_subprocess_exec(*SUDO_CMDS[action])
