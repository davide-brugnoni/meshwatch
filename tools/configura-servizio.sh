#!/bin/bash
# MeshWatch (kit SD DietPi) — configura il servizio systemd. Da eseguire come root.
# Lo usa l'installazione al primo avvio; si può rilanciare quando si vuole (non tocca config e dati).
# - il programma in /opt/meshwatch appartiene all'utente del servizio, così può aggiornarsi
#   da solo dalla pagina web (Gestione → Sistema → Aggiornamento);
# - all'avvio copia il config dalla SD e, solo se AGGIORNA-SD.bat ha lasciato il segnale
#   meshwatch/AGGIORNA sulla SD, anche il programma;
# - tools/guard.sh ripristina la versione precedente se un aggiornamento non parte;
# - watchdog: se la pagina non risponde per 5 minuti systemd riavvia il programma;
# - WiFi senza risparmio energetico e spento quando c'è il cavo di rete (tools/configura-wifi.sh).
set -e
[ "$(id -u)" = 0 ] || { echo "Lancia con sudo"; exit 1; }
APP=/opt/meshwatch
SVC=meshwatch-svc
FAT="$1"
if [ -z "$FAT" ]; then
  for d in /boot/firmware /boot; do
    if [ -f "$d/meshwatch-config.yaml" ]; then FAT=$d; break; fi
  done
fi
[ -n "$FAT" ] && [ -f "$FAT/meshwatch-config.yaml" ] || { echo "ERRORE: meshwatch-config.yaml non trovato in /boot/firmware o /boot"; exit 1; }
[ -x "$APP/venv/bin/python" ] || { echo "ERRORE: $APP/venv non trovato: MeshWatch non è installato"; exit 1; }
id "$SVC" >/dev/null 2>&1 || useradd -r -m -d /var/lib/meshwatch -s /usr/sbin/nologin -G dialout "$SVC"

cat > /opt/meshwatch-prestart.sh <<PRE
#!/bin/sh
# Generato da tools/configura-servizio.sh
SD=$FAT/meshwatch
if [ -f \$SD/AGGIORNA ] && [ -f \$SD/meshwatch/__main__.py ]; then
  echo "MeshWatch: aggiorno il programma dalla SD"
  for i in meshwatch web tools requirements.txt config.example.yaml README.md LICENSE; do
    if [ -e "\$SD/\$i" ]; then rm -rf "$APP/\$i"; cp -r "\$SD/\$i" "$APP/"; fi
  done
  rm -rf $APP/.update $APP/VERSION.json
  chown -R $SVC:$SVC $APP
  runuser -u $SVC -- $APP/venv/bin/pip install --no-cache-dir -q -r $APP/requirements.txt || echo "pip non riuscito (rete?)"
  rm -f \$SD/AGGIORNA
fi
install -m 600 -o $SVC -g $SVC $FAT/meshwatch-config.yaml /var/lib/meshwatch/config.yaml
PRE
chmod 755 /opt/meshwatch-prestart.sh

cat > /etc/systemd/system/meshwatch.service <<UNIT
[Unit]
Description=MeshWatch - monitor nodi MeshCore
After=network-online.target
Wants=network-online.target

[Service]
User=$SVC
WorkingDirectory=/var/lib/meshwatch
Environment=PYTHONPATH=$APP
Environment=PYTHONUNBUFFERED=1
ExecStartPre=+/opt/meshwatch-prestart.sh
ExecStartPre=-/bin/sh $APP/tools/guard.sh
ExecStart=$APP/venv/bin/python -m meshwatch -c /var/lib/meshwatch/config.yaml
Restart=always
RestartSec=10
TimeoutStartSec=30min
# se la pagina non risponde per 5 minuti (programma bloccato) systemd lo riavvia: meshwatch/watchdog.py
WatchdogSec=300

[Install]
WantedBy=multi-user.target
UNIT

# WiFi sempre sveglio (di notte il risparmio energetico lo fa smettere di rispondere)
bash "$APP/tools/configura-wifi.sh" || echo "ATTENZIONE: configurazione del WiFi non riuscita"

# Riavvio e spegnimento dalla pagina (solo questi due comandi)
echo "$SVC ALL=(root) NOPASSWD: /usr/bin/systemctl reboot, /usr/bin/systemctl poweroff" > /etc/sudoers.d/meshwatch
chmod 440 /etc/sudoers.d/meshwatch
visudo -cf /etc/sudoers.d/meshwatch >/dev/null || rm -f /etc/sudoers.d/meshwatch

chown -R "$SVC:$SVC" "$APP"
systemctl daemon-reload
systemctl enable meshwatch >/dev/null 2>&1
echo "Servizio MeshWatch configurato (file sulla SD in $FAT). Aggiornamenti dalla pagina: abilitati."
