"""Stato del Raspberry e comandi di riavvio/spegnimento."""
import asyncio
import os
import shutil
import subprocess
import time

# Il servizio gira come utente senza privilegi: riavvio e spegnimento passano da sudo,
# abilitato solo per questi due comandi (vedi /etc/sudoers.d/meshwatch).
SUDO_CMDS = {
    "reboot": ["sudo", "-n", "/usr/bin/systemctl", "reboot"],
    "poweroff": ["sudo", "-n", "/usr/bin/systemctl", "poweroff"],
}

HWMON_DIR = "/sys/class/hwmon"
THROTTLE_PATHS = ("/sys/devices/platform/soc/soc:firmware/get_throttled",
                  "/sys/devices/platform/soc/soc:firmware/raspberrypi-hwmon/get_throttled")


def _read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


_UV_SEEN = False  # tensione bassa vista almeno una volta da quando gira il servizio


def _from_bits(n):
    return {"undervoltage_now": bool(n & 0x1), "undervoltage_since_boot": bool(n & 0x10000),
            "throttled_now": bool(n & 0x4), "throttled_since_boot": bool(n & 0x40000)}


def _hwmon_alarm():
    """Driver rpi_volt: /sys/class/hwmon/hwmonN sono symlink, os.walk non li segue."""
    base = HWMON_DIR
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return None
    for d in names:
        path = os.path.join(base, d)
        if _read(os.path.join(path, "name")) not in (None, "rpi_volt"):
            continue
        v = _read(os.path.join(path, "in0_lcrit_alarm"))
        if v is not None:
            return v == "1"
    return None


def _vcgencmd():
    try:
        out = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True, text=True,
                             timeout=2).stdout
        return int(out.strip().split("=", 1)[1], 16)
    except (OSError, subprocess.SubprocessError, IndexError, ValueError):
        return None


def _throttled():
    """Bit del firmware Raspberry: 0 = tensione bassa ora, 16 = tensione bassa dall'avvio."""
    global _UV_SEEN
    for p in THROTTLE_PATHS:  # kernel vecchi
        v = _read(p)
        if v:
            try:
                return _from_bits(int(v, 16))
            except ValueError:
                pass
    # kernel recenti: get_throttled non c'è più in sysfs, il driver hwmon segnala l'allarme
    alarm = _hwmon_alarm()
    if alarm is not None:
        _UV_SEEN = _UV_SEEN or alarm
        return {"undervoltage_now": alarm, "undervoltage_since_boot": _UV_SEEN,
                "throttled_now": None, "throttled_since_boot": None}
    n = _vcgencmd()  # ultima risorsa (richiede il gruppo video)
    if n is not None:
        return _from_bits(n)
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
