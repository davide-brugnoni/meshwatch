#!/bin/bash
# MeshWatch — installazione automatica al primo avvio di DietPi.
# Lo esegue DietPi da solo (AUTO_SETUP_CUSTOM_SCRIPT_EXEC=0).
# Su Raspberry Pi con DietPi Trixie la partizione FAT (quella che vedi da Windows) e' montata
# in /boot/firmware, mentre dietpi.txt e questo script vengono spostati in /boot: cerco quindi
# la cartella meshwatch in entrambi i posti. Log leggibile da Windows: meshwatch-install.log
set -e
FAT=/boot
for d in /boot/firmware /boot; do
  if [ -d "$d/meshwatch" ]; then FAT=$d; break; fi
done
exec > >(tee -a "$FAT/meshwatch-install.log") 2>&1
echo "=== MeshWatch: installazione $(date) — file in $FAT ==="
[ -d "$FAT/meshwatch" ] || { echo "ERRORE: cartella meshwatch non trovata"; exit 1; }

apt-get install -y python3 python3-venv python3-pip python3-yaml avahi-daemon sudo iw fonts-dejavu-core

# Utente del servizio (senza login)
id meshwatch-svc >/dev/null 2>&1 || useradd -r -m -d /var/lib/meshwatch -s /usr/sbin/nologin -G dialout meshwatch-svc

# Utente per l'accesso SSH: "meshwatch", stessa password scelta in PREPARA-SD (copiata dall'utente dietpi)
id meshwatch >/dev/null 2>&1 || useradd -m -s /bin/bash meshwatch
for g in sudo dialout systemd-journal; do usermod -aG "$g" meshwatch 2>/dev/null || true; done
PWHASH=$(getent shadow dietpi | cut -d: -f2)
case "$PWHASH" in
  ""|"*"|"!"*)
    echo "ATTENZIONE: password non copiata, l'utente meshwatch non puo' accedere; resta l'accesso come root" ;;
  *)
    usermod -p "$PWHASH" meshwatch
    # SSH solo per l'utente meshwatch (root e dietpi non possono piu' entrare via SSH)
    mkdir -p /etc/ssh/sshd_config.d
    printf 'AllowUsers meshwatch\nPermitRootLogin no\n' > /etc/ssh/sshd_config.d/10-meshwatch.conf
    if grep -qi '^Include /etc/ssh/sshd_config.d' /etc/ssh/sshd_config && /usr/sbin/sshd -t; then
      systemctl reload ssh 2>/dev/null || systemctl reload sshd 2>/dev/null || true
      echo "SSH: accesso consentito solo all'utente meshwatch"
    else
      rm -f /etc/ssh/sshd_config.d/10-meshwatch.conf
      echo "ATTENZIONE: configurazione SSH non applicata, resta l'accesso come root"
    fi ;;
esac

mkdir -p /opt/meshwatch
cp -r "$FAT/meshwatch/." /opt/meshwatch/
python3 -m venv --system-site-packages /opt/meshwatch/venv   # usa PyYAML di sistema (su ARMv7 non ci sono wheel pronte)
/opt/meshwatch/venv/bin/pip install --no-cache-dir -q -r /opt/meshwatch/requirements.txt

[ -f "$FAT/meshwatch-config.yaml" ] || cp /opt/meshwatch/config.example.yaml "$FAT/meshwatch-config.yaml"

# Servizio systemd, permessi di riavvio/spegnimento, aggiornamento dalla pagina web
rm -f /opt/meshwatch/AGGIORNA
bash /opt/meshwatch/tools/configura-servizio.sh "$FAT"
rm -f "$FAT/meshwatch/AGGIORNA"
systemctl restart meshwatch || true
echo "=== MeshWatch installato. Pagina: http://$(hostname).local:8080 ==="
