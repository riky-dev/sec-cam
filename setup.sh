#!/usr/bin/env bash
# Setup script for Sec-Cam on Termux
# Installs packages, creates virtualenv, and prepares the repo for use on an Android device running Termux.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "Sec-Cam setup starting in ${ROOT_DIR}"

echo "1) Updating packages"
pkg update -y || true
pkg upgrade -y || true

echo "2) Installing required packages: python, ffmpeg, git, termux-api"
pkg install -y python ffmpeg git termux-api || true

echo "3) Creating Python venv"
python -m venv venv
.
venv/bin/python -m pip install --upgrade pip
venv/bin/pip install -r requirements.txt

echo "4) Prepare .env (copy example)"
if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env from .env.example. Please edit .env and set BOT_TOKEN and CHAT_ID before running the daemon."
else
  echo ".env already exists - leaving it in place"
fi

echo "5) Optional: create Termux:Boot starter script (~/.termux/boot/start_sec_cam.sh)"
if command -v termux-wake-lock >/dev/null 2>&1; then
  BOOT_DIR="$HOME/.termux/boot"
  mkdir -p "$BOOT_DIR"
  cat > "$BOOT_DIR/start_sec_cam.sh" <<'EOF'
#!/usr/bin/env sh
# Starter for Sec-Cam daemon for Termux:Boot
cd "$HOME/"sec-cam || exit 1
source venv/bin/activate
termux-wake-lock
nohup python camera_daemon.py >/dev/null 2>&1 &
EOF
  chmod +x "$BOOT_DIR/start_sec_cam.sh" || true
  echo "Created Termux:Boot starter script at $BOOT_DIR/start_sec_cam.sh"
else
  echo "termux-wake-lock not found; skipping Termux:Boot creation. You can still manually start the daemon." 
fi

echo "Setup finished. Edit .env and run: . venv/bin/activate && python camera_daemon.py"
