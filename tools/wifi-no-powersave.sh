#!/bin/bash
# MeshWatch — disattiva il risparmio energetico del WiFi. Da eseguire come root.
# Sul Raspberry il chip WiFi, quando la rete è poco usata (di notte), "si addormenta" e smette di
# rispondere: la pagina non si apre più finché non si riavvia. Con il risparmio spento resta sveglio.
# Lo usano install.sh e tools/configura-servizio.sh; si può rilanciare quando si vuole.
# Tutti i file installati sono di root e non richiamano niente nella cartella del programma
# (che appartiene all'utente del servizio).
set -e
[ "$(id -u)" = 0 ] || { echo "Lancia con sudo"; exit 1; }

if ! command -v iw >/dev/null 2>&1; then
  apt-get install -y -qq iw >/dev/null 2>&1 || echo "ATTENZIONE: iw non installato (rete?), uso iwconfig se c'è"
fi

# Comando per tutte le interfacce WiFi presenti
cat > /usr/local/sbin/meshwatch-wifi-powersave-off <<'CMD'
#!/bin/sh
# Generato da MeshWatch (tools/wifi-no-powersave.sh): WiFi sempre sveglio.
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

# 1) all'avvio
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
systemctl daemon-reload
systemctl enable meshwatch-wifi >/dev/null 2>&1
systemctl restart meshwatch-wifi || true

# 2) ogni volta che il WiFi si ricollega (il driver rimette il risparmio): ifupdown (DietPi)
if [ -d /etc/network/if-up.d ]; then
  ln -sf /usr/local/sbin/meshwatch-wifi-powersave-off /etc/network/if-up.d/meshwatch-wifi
fi
# ... e NetworkManager (Raspberry Pi OS Bookworm e successivi)
if [ -d /etc/NetworkManager/conf.d ]; then
  printf '[connection]\n# MeshWatch: 2 = risparmio energetico del WiFi spento\nwifi.powersave = 2\n' \
    > /etc/NetworkManager/conf.d/meshwatch-wifi-powersave.conf
fi

echo "WiFi: risparmio energetico disattivato (anche dopo ogni riavvio e riconnessione)"
