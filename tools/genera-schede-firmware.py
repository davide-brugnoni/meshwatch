"""Rigenera meshwatch/firmware_boards.py dal codice sorgente di MeshCore.

Per ogni cartella variants/<scheda> legge il nome che la scheda dichiara (getManufacturerName,
quello che MeshWatch vede come "Modello") e le build "companion_radio_usb" definite lì.
Serve a proporre in automatico il firmware giusto per la radio collegata al Raspberry.

Uso:  python tools/genera-schede-firmware.py /percorso/clone/MeshCore
"""
import re
import sys
from pathlib import Path

SUFFIX = "_companion_radio_usb"


def main(src):
    src = Path(src)
    boards = {}
    for d in sorted((src / "variants").iterdir()):
        if not d.is_dir():
            continue
        envs = sorted({m.group(1)[:-len(SUFFIX)] for ini in d.glob("*.ini")
                       for m in re.finditer(r"^\[env:([^\]]+)\]", ini.read_text(errors="ignore"), re.M)
                       if m.group(1).endswith(SUFFIX)})
        if not envs:
            continue
        names = set()
        for f in list(d.glob("*.h")) + list(d.glob("*.cpp")):
            for m in re.finditer(r"getManufacturerName\(\)[^{]*\{\s*return\s+\"([^\"]+)\"", f.read_text(errors="ignore")):
                names.add(m.group(1))
        for n in names or {d.name}:
            boards.setdefault(n, set()).update(envs)
    out = ['"""Generato da tools/genera-schede-firmware.py: non modificare a mano.',
           "",
           "Nome dichiarato dalla scheda (Modello) -> build companion USB di MeshCore che la usano.",
           '"""', "", "BOARDS = {"]
    for n in sorted(boards, key=str.lower):
        out.append(f"    {n!r}: {sorted(boards[n])!r},")
    out.append("}")
    Path(__file__).resolve().parent.parent.joinpath("meshwatch", "firmware_boards.py").write_text("\n".join(out) + "\n")
    print(f"{len(boards)} schede")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else ".")
