"""Controlli sulla pagina web (senza browser): file inclusi, sintassi degli script, id usati.
Esecuzione:  python tests/check_page.py   (serve node per il controllo di sintassi; senza, lo salta)

- ogni <script src> e <link href> locale di index.html esiste;
- ogni file in web/assets/js è incluso nella pagina (un file dimenticato = funzioni mancanti);
- la sintassi di ogni script è valida, anche messi uno dopo l'altro come li carica il browser
  (due `const` con lo stesso nome in file diversi bloccano la pagina);
- ogni $('id') usato negli script corrisponde a un elemento della pagina.
"""
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"


def check(cond, msg):
    print(("OK   " if cond else "FAIL ") + msg)
    if not cond:
        sys.exit(1)


def main():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    check("<script>" not in html and "<style>" not in html, "niente script o stile in linea nella pagina")
    refs = re.findall(r'<script src="/([^"]+)"', html) + re.findall(r'<link rel="stylesheet" href="/([^"]+)"', html)
    missing = [r for r in refs if not (WEB / r).is_file()]
    check(not missing, "file inclusi esistenti" + (f" (mancano: {', '.join(missing)})" if missing else ""))
    scripts = [r for r in re.findall(r'<script src="/([^"]+)"', html) if r.startswith("assets/")]
    on_disk = sorted("assets/js/" + p.name for p in (WEB / "assets/js").glob("*.js"))
    forgotten = sorted(set(on_disk) - set(scripts))
    check(not forgotten, "tutti gli script sono inclusi" + (f" (dimenticati: {', '.join(forgotten)})" if forgotten else ""))
    check(scripts and scripts[-1] == "assets/js/main.js", "main.js è l'ultimo script (avvia la pagina)")

    sources = {s: (WEB / s).read_text(encoding="utf-8") for s in scripts}
    ids = set(re.findall(r'\bid="([^"]+)"', html))
    created = set()
    for src in sources.values():
        created |= set(re.findall(r"\bid: '([^']+)'", src))
    used = set()
    for src in sources.values():
        used |= set(re.findall(r"\$\('([^']+)'\)", src))
    unknown = sorted(used - ids - created)
    check(not unknown, "id usati negli script presenti nella pagina" + (f" (sconosciuti: {', '.join(unknown)})" if unknown else ""))

    node = shutil.which("node")
    if not node:
        print("SKIP sintassi JavaScript: node non installato")
        return
    with tempfile.TemporaryDirectory() as tmp:
        for name, src in sources.items():
            f = Path(tmp) / Path(name).name
            f.write_text(src, encoding="utf-8")
            r = subprocess.run([node, "--check", str(f)], capture_output=True, text=True)
            check(r.returncode == 0, f"sintassi {name}" + (f"\n{r.stderr}" if r.returncode else ""))
        # tutti insieme come uno script solo: trova i nomi globali dichiarati due volte
        allf = Path(tmp) / "tutti.js"
        allf.write_text("\n;\n".join(sources.values()), encoding="utf-8")
        r = subprocess.run([node, "--check", str(allf)], capture_output=True, text=True)
        check(r.returncode == 0, "script insieme senza nomi globali duplicati" + (f"\n{r.stderr}" if r.returncode else ""))
    print("Tutti i controlli superati.")


if __name__ == "__main__":
    main()
