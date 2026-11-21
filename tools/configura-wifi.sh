#!/bin/bash
# MeshWatch — WiFi del Raspberry. Da eseguire come root.
#  1) risparmio energetico spento: di notte, con la rete poco usata, il chip WiFi "si addormenta"
#     e smette di rispondere finché non si riavvia;
#  2) cavo di rete collegato → WiFi spento; cavo staccato → WiFi riacceso (controllo ogni 20 s).
#     Per tenere il WiFi sempre acceso anche col cavo:  sudo touch /etc/meshwatch-wifi-sempre-acceso
# Lo usano install.sh e tools/configura-servizio.sh; si può rilanciare quando si vuole.
# Tutti i file installati sono di root e non richiamano niente nella cartella del programma
# (che appartiene all'utente del servizio).
set -e
[ "$(id -u)" = 0 ] || { echo "Lancia con sudo"; exit 1; }

if ! command -v iw >/dev/null 2>&1; then
  apt-get install -y -qq iw >/dev/null 2>&1 || echo "ATTENZIONE: iw non installato (rete?), uso iwconfig se c'è"
fi

# ---------- 1) risparmio energetico spento, per tutte le interfacce WiFi
cat > /usr/local/sbin/meshwatch-wifi-powersave-off <<'CMD'
#!/bin/sh
# Generato da MeshWatch (tools/configura-wifi.sh): WiFi sempre sveglio.
for d in /sys/class/net/*/wireless; do
  [ -e "$d" ] || continue
  n=$(basename "$(dirname "$d")")
  /usr/sbin/iw dev "$n" set power_save off 2>/dev/null \
    || iw dev "$n" set power_save off 2>/dev/null \
    || iwconfig "$n" power off 2>/dev/null || true
done
exit 0
CMD
chmod 755 /usr/local/sbin/meshwatch-wifi-powersave-off

# ---------- 2) WiFi spento quando c'è il cavo
cat > /usr/local/sbin/meshwatch-wifi-auto <<'CMD'
#!/bin/sh
# Generato da MeshWatch (tools/configura-wifi.sh): cavo di rete collegato → WiFi spento,
# cavo staccato → WiFi riacceso. "Collegato" vuol dire che la scheda di rete via cavo ha il
# segnale E un indirizzo IP vero (non 169.254.x.x): un cavo senza rete non spegne il WiFi.
# Tocca il WiFi solo se l'ha spento lui (file di stato), mai uno spento a mano.
PATH=/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin
STATE=/var/lib/meshwatch-wifi/spento-dal-cavo   # su disco: lo stato del WiFi sopravvive al riavvio
mkdir -p "${STATE%/*}"

wired=no
for d in /sys/class/net/*; do
  n=${d##*/}
  [ -e "$d/wireless" ] && continue          # WiFi
  [ -e "$d/device" ] || continue            # solo schede vere (niente lo, docker, VPN)
  [ "$(cat "$d/carrier" 2>/dev/null)" = 1 ] || continue
  ip -4 -o addr show dev "$n" scope global 2>/dev/null | grep -q inet && wired=$n
done
[ -e /etc/meshwatch-wifi-sempre-acceso ] && keep=yes || keep=no

nm() { command -v nmcli >/dev/null 2>&1 && systemctl is-active -q NetworkManager 2>/dev/null; }

wifi_off() {
  if nm; then nmcli radio wifi off
  else
    for r in /sys/class/rfkill/*; do
      [ "$(cat "$r/type" 2>/dev/null)" = wlan ] && echo 1 > "$r/soft"
    done
  fi
}

wifi_on() {
  if nm; then nmcli radio wifi on
  else
    for r in /sys/class/rfkill/*; do
      [ "$(cat "$r/type" 2>/dev/null)" = wlan ] && echo 0 > "$r/soft"
    done
    # ifupdown (DietPi): di solito si ricollega da solo; se dopo 20 s non ha l'IP, rialza l'interfaccia
    sleep 20
    for d in /sys/class/net/*/wireless; do
      [ -e "$d" ] || continue
      n=$(basename "$(dirname "$d")")
      if ! ip -4 -o addr show dev "$n" scope global | grep -q inet && command -v ifup >/dev/null 2>&1; then
        ifdown --force "$n" >/dev/null 2>&1; ifup "$n" >/dev/null 2>&1 || true
      fi
    done
  fi
  /usr/local/sbin/meshwatch-wifi-powersave-off
}

if [ "$wired" != no ] && [ "$keep" = no ]; then
  if [ ! -e "$STATE" ]; then
    touch "$STATE"
    echo "Cavo di rete collegato ($wired): spengo il WiFi"
    wifi_off
  fi
elif [ -e "$STATE" ]; then
  rm -f "$STATE"
  echo "Cavo di rete staccato: riaccendo il WiFi"
  wifi_on
fi
exit 0
CMD
chmod 755 /usr/local/sbin/meshwatch-wifi-auto

# ---------- servizi di systemd
cat > /etc/systemd/system/meshwatch-wifi.service <<'UNIT'
[Unit]
Description=MeshWatch - WiFi senza risparmio energetico
After=network.target

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/meshwatch-wifi-powersave-off
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
UNIT

cat > /etc/systemd/system/meshwatch-wifi-auto.service <<'UNIT'
[Unit]
Description=MeshWatch - WiFi spento quando c'è il cavo di rete
After=network.target

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/meshwatch-wifi-auto
UNIT

cat > /etc/systemd/system/meshwatch-wifi-auto.timer <<'UNIT'
[Unit]
Description=MeshWatch - controlla ogni 20 secondi se c'è il cavo di rete

[Timer]
OnBootSec=30s
OnUnitActiveSec=20s
AccuracySec=5s

[Install]
WantedBy=timers.target
UNIT

systemctl daemon-reload
systemctl enable meshwatch-wifi meshwatch-wifi-auto.timer >/dev/null 2>&1
systemctl restart meshwatch-wifi || true
systemctl restart meshwatch-wifi-auto.timer || true

# risparmio spento anche a ogni riconnessione del WiFi (il driver lo rimette): ifupdown (DietPi)
if [ -d /etc/network/if-up.d ]; then
  ln -sf /usr/local/sbin/meshwatch-wifi-powersave-off /etc/network/if-up.d/meshwatch-wifi
fi
# ... e NetworkManager (Raspberry Pi OS Bookworm e successivi)
if [ -d /etc/NetworkManager/conf.d ]; then
  printf '[connection]\n# MeshWatch: 2 = risparmio energetico del WiFi spento\nwifi.powersave = 2\n' \
    > /etc/NetworkManager/conf.d/meshwatch-wifi-powersave.conf
fi

echo "WiFi: risparmio energetico disattivato; si spegne da solo quando è collegato il cavo di rete"
