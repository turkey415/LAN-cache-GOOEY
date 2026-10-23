#!/bin/bash
# One-time setup for LAN Cache GOOEY. Run with: sudo ./webgui/install.sh
#  - installs Docker + the compose plugin (Ubuntu packages) if missing
#  - lets the GUI's user run Docker (docker group)
#  - makes the cache log folder readable by the GUI
#  - installs and starts the lancache-gui systemd service (https://<this machine>:8443)
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then echo "Run with sudo: sudo $0"; exit 1; fi
GUI_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LANCACHE_DIR="$(dirname "$GUI_DIR")"
RUN_USER="${SUDO_USER:-$(stat -c %U "$GUI_DIR")}"
PORT="${LCGUI_PORT:-8443}"

if [ ! -f "$LANCACHE_DIR/docker-compose.yml" ] || [ ! -f "$LANCACHE_DIR/.env" ]; then
  echo "This folder should sit inside a lancache docker-compose folder, like this:"
  echo "  git clone https://github.com/lancachenet/docker-compose lancache"
  echo "  cd lancache && git clone https://github.com/turkey415/LAN-cache-GOOEY webgui"
  echo "  sudo ./webgui/install.sh"
  exit 1
fi

echo "== LAN Cache GOOEY setup"
echo "   lancache folder: $LANCACHE_DIR"
echo "   GUI runs as:     $RUN_USER"

if ! command -v docker >/dev/null 2>&1; then
  echo "== Installing Docker (docker.io + docker-compose-v2)"
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y docker.io docker-compose-v2
fi
if ! docker compose version >/dev/null 2>&1; then
  echo "== Installing the docker compose plugin"
  DEBIAN_FRONTEND=noninteractive apt-get install -y docker-compose-v2
fi
systemctl enable --now docker

echo "== Giving $RUN_USER access to Docker"
usermod -aG docker "$RUN_USER"

# The cache container writes logs as its own user; make sure the GUI can read them.
CACHE_ROOT=$(grep -E '^CACHE_ROOT=' "$LANCACHE_DIR/.env" | tail -1 | cut -d= -f2- || true)
CACHE_ROOT=${CACHE_ROOT:-./lancache}
case "$CACHE_ROOT" in /*) ;; *) CACHE_ROOT="$LANCACHE_DIR/${CACHE_ROOT#./}";; esac
mkdir -p "$CACHE_ROOT/logs"
chmod o+rx "$CACHE_ROOT" "$CACHE_ROOT/logs"
chmod -R o+r "$CACHE_ROOT/logs" 2>/dev/null || true

echo "== Installing the lancache-gui service"
cat > /etc/systemd/system/lancache-gui.service <<EOF
[Unit]
Description=LAN Cache GOOEY web GUI (https port $PORT)
After=network-online.target docker.service
Wants=network-online.target

[Service]
User=$RUN_USER
Group=docker
SupplementaryGroups=adm
WorkingDirectory=$GUI_DIR
Environment=PYTHONUNBUFFERED=1
Environment=LCGUI_PORT=$PORT
ExecStart=/usr/bin/python3 $GUI_DIR/server.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
# Stop a copy that was started by hand, so the service can use the port.
pkill -f "python3 $GUI_DIR/server.py" 2>/dev/null || true
pkill -f "^python3 server.py" -u "$RUN_USER" 2>/dev/null || true
systemctl enable lancache-gui
systemctl restart lancache-gui
sleep 2

if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  echo "== Opening firewall ports (GUI $PORT, DNS 53, HTTP 80, HTTPS 443)"
  ufw allow "$PORT"/tcp; ufw allow 53; ufw allow 80/tcp; ufw allow 443/tcp
fi

IP=$(ip -4 route get 1.1.1.1 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p')
echo
echo "Done. Open https://${IP:-<this machine>}:$PORT/"
if [ -f "$GUI_DIR/data/INITIAL_ADMIN_PASSWORD.txt" ]; then
  echo "First login:"; sed -n '1,2p' "$GUI_DIR/data/INITIAL_ADMIN_PASSWORD.txt"
fi
echo "(Your browser will warn about the self-signed certificate once; that's expected.)"
