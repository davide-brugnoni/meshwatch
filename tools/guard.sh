#!/bin/sh
# MeshWatch — eseguito da systemd prima di ogni avvio (ExecStartPre, come utente del servizio).
# Se l'ultimo aggiornamento dalla pagina non si è confermato (il programma non è rimasto
# acceso 90 secondi) per 3 avvii di fila, rimette la versione precedente.
APP="$(cd "$(dirname "$0")/.." && pwd)"
U="$APP/.update"
[ -f "$U/pending" ] || exit 0
n=$(cat "$U/tries" 2>/dev/null || echo 0)
n=$((n + 1))
echo "$n" > "$U/tries"
[ "$n" -le 3 ] && exit 0

echo "MeshWatch: la nuova versione non parte, ripristino la precedente"
if [ -f "$U/prev_git" ]; then
  git -C "$APP" reset -q --hard "$(cat "$U/prev_git")"
elif [ -d "$U/backup" ]; then
  if [ -f "$U/backup/.added" ]; then
    while read -r name; do [ -n "$name" ] && rm -rf "${APP:?}/$name"; done < "$U/backup/.added"
  fi
  for i in "$U/backup"/*; do
    [ -e "$i" ] || continue
    name=$(basename "$i")
    rm -rf "${APP:?}/$name"
    mv "$i" "$APP/$name"
  done
fi
date '+%Y-%m-%d %H:%M' > "$U/rolled_back"
rm -f "$U/pending" "$U/tries" "$U/prev_git"
exit 0
