#!/usr/bin/env bash
# Installa MeshWatch come servizio sul Raspberry. Lancia dalla cartella meshwatch:  bash install.sh
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
USER_NAME="$(whoami)"
cd "$DIR"

echo "→ Pacchetti di sistema"
sudo apt-get update -qq
sudo apt-get install -y -qq python3-venv python3-pip

echo "→ Ambiente Python"
python3 -m venv venv
./venv/bin/pip install -q --upgrade pip
./venv/bin/pip install -q -r requirements.txt

echo "→ Permessi porta seriale"
sudo usermod -aG dialout "$USER_NAME"

if [ ! -f config.yaml ]; then
  cp config.example.yaml config.yaml
  echo "→ Creato config.yaml: modificalo prima di avviare (nano config.yaml)"
fi

echo "→ Permesso di riavvio/spegnimento dalla pagina web"
echo "$USER_NAME ALL=(root) NOPASSWD: /usr/bin/systemctl reboot, /usr/bin/systemctl poweroff" | sudo tee /etc/sudoers.d/meshwatch >/dev/null
sudo chmod 440 /etc/sudoers.d/meshwatch
sudo visudo -cf /etc/sudoers.d/meshwatch >/dev/null || sudo rm -f /etc/sudoers.d/meshwatch

echo "→ Servizio systemd"
sed -e "s|__DIR__|$DIR|g" -e "s|__USER__|$USER_NAME|g" meshwatch.service | sudo tee /etc/systemd/system/meshwatch.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable meshwatch

echo
echo "Fatto. Porte seriali trovate:"
ls /dev/serial/by-id/ 2>/dev/null || echo "  (nessuna: collega la Heltec)"
echo
echo "1) nano config.yaml        (porta seriale, nomi nodi, password, token Telegram)"
echo "2) sudo systemctl start meshwatch"
echo "3) apri http://$(hostname -I | awk '{print $1}'):8080"
echo "Log: journalctl -u meshwatch -f"
