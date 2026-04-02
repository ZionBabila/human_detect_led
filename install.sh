#!/bin/bash
# Human Detect LED — One-command installer for Raspberry Pi OS
set -e

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE=human_detect_led
PYTHON=python3

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Human Detect LED — Installer"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# ── Check we're on a Pi ───────────────────────────────────────────────────────
if ! grep -q "Raspberry Pi\|BCM\|raspberrypi" /proc/cpuinfo /etc/os-release 2>/dev/null; then
  echo "Warning: this does not appear to be a Raspberry Pi."
  read -rp "Continue anyway? [y/N] " ans
  [[ "$ans" =~ ^[Yy]$ ]] || exit 1
fi

# ── System packages ───────────────────────────────────────────────────────────
echo "[1/5] Installing system packages..."
sudo apt-get update -qq
sudo apt-get install -y -qq \
  python3-pip python3-venv \
  python3-opencv \
  libatlas-base-dev \
  build-essential \
  2>/dev/null

# ── Python dependencies ───────────────────────────────────────────────────────
echo "[2/5] Installing Python dependencies..."
$PYTHON -m pip install --quiet --break-system-packages -r "$REPO_DIR/requirements.txt"

echo "[3/5] Installing optional dependencies (LED hardware, YOLO)..."
$PYTHON -m pip install --quiet --break-system-packages \
  rpi_ws281x gpiozero 2>/dev/null || true
$PYTHON -m pip install --quiet --break-system-packages \
  ultralytics 2>/dev/null || echo "  YOLOv8 skipped (install manually if needed)"

# ── YOLOv8 model weights ──────────────────────────────────────────────────────
if $PYTHON -c "from ultralytics import YOLO" 2>/dev/null; then
  echo "[4/5] Pre-downloading YOLOv8n model weights..."
  $PYTHON -c "from ultralytics import YOLO; YOLO('yolov8n.pt')" 2>/dev/null || true
else
  echo "[4/5] Skipping YOLOv8 weights (ultralytics not installed)"
fi

# ── systemd service ───────────────────────────────────────────────────────────
echo "[5/5] Installing systemd service..."
sudo cp "$REPO_DIR/${SERVICE}.service" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE"

# ── Firewall (ufw if present) ─────────────────────────────────────────────────
if command -v ufw &>/dev/null && sudo ufw status | grep -q "Status: active"; then
  sudo ufw allow 5000/tcp comment "LED Controller web UI" 2>/dev/null || true
fi

# ── Done ─────────────────────────────────────────────────────────────────────
IP=$(hostname -I | awk '{print $1}')
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Installation complete!"
echo ""
echo "  Start the service:  sudo systemctl start $SERVICE"
echo "  Open setup wizard:  http://${IP}:5000/setup"
echo "  View logs:          sudo journalctl -u $SERVICE -f"
echo ""
echo "  To set a password:  edit /etc/systemd/system/${SERVICE}.service"
echo "  and uncomment the LED_PASSWORD line, then 'sudo systemctl restart $SERVICE'"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
