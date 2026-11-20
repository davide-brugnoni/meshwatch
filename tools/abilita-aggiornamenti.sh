#!/bin/bash
# MeshWatch — una tantum, per le schede preparate prima della versione 1.1:
# installa l'ultima versione da GitHub e abilita l'aggiornamento dalla pagina web.
# Dal Raspberry (SSH, utente meshwatch):
#   curl -fsSL https://raw.githubusercontent.com/davide-brugnoni/meshwatch/main/tools/abilita-aggiornamenti.sh | sudo bash
# oppure da Windows: doppio clic su sd-kit\ABILITA-AGGIORNAMENTI.bat
set -e
[ "$(id -u)" = 0 ] || { echo "Lancia con sudo"; exit 1; }
REPO=${MESHWATCH_REPO:-davide-brugnoni/meshwatch}
BRANCH=${MESHWATCH_BRANCH:-main}
APP=/opt/meshwatch
[ -x "$APP/venv/bin/python" ] || { echo "ERRORE: MeshWatch non è installato in $APP"; exit 1; }

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
echo "→ Scarico MeshWatch ($REPO, $BRANCH)"
if command -v curl >/dev/null; then curl -fsSL "https://codeload.github.com/$REPO/tar.gz/refs/heads/$BRANCH" -o "$TMP/m.tgz"
else wget -qO "$TMP/m.tgz" "https://codeload.github.com/$REPO/tar.gz/refs/heads/$BRANCH"; fi
tar -xzf "$TMP/m.tgz" -C "$TMP"
SRC=$(find "$TMP" -mindepth 1 -maxdepth 1 -type d | head -n1)
[ -f "$SRC/meshwatch/__main__.py" ] && [ -f "$SRC/tools/configura-servizio.sh" ] || { echo "ERRORE: archivio non valido"; exit 1; }

echo "→ Installo in $APP"
systemctl stop meshwatch 2>/dev/null || true
for i in meshwatch web tools requirements.txt config.example.yaml README.md LICENSE; do
  if [ -e "$SRC/$i" ]; then rm -rf "${APP:?}/$i"; cp -r "$SRC/$i" "$APP/"; fi
done
rm -rf "$APP/.update" "$APP/VERSION.json"
"$APP/venv/bin/pip" install --no-cache-dir -q -r "$APP/requirements.txt"
bash "$APP/tools/configura-servizio.sh"
systemctl start meshwatch
echo "Fatto: d'ora in poi aggiorni dalla pagina, Gestione → Sistema → Aggiornamento."
