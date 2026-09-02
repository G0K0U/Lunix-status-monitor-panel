#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: ./install.sh [options]

Install the PC Monitor System Panel as a per-user Linux application.

Options:
  --install-dir DIR       Application directory (default: ~/.local/share/pc-monitor-system-panel)
  --python PATH           Python 3 executable to seed the virtual environment
  --with-driver           Also install the bundled optional DKMS MPS driver (asks for sudo)
  --skip-python-deps      Create the venv but do not download matplotlib/pandas
  -h, --help              Show this help
EOF
}

if [[ ${EUID} -eq 0 ]]; then
  echo "Run this installer as the desktop user, not with sudo." >&2
  echo "Use --with-driver if you want it to invoke sudo for the optional kernel driver." >&2
  exit 1
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="${MPS_INSTALL_DIR:-${HOME}/.local/share/pc-monitor-system-panel}"
PYTHON_BIN="${MPS_PYTHON:-python3}"
WITH_DRIVER=0
SKIP_PYTHON_DEPS=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --install-dir)
      [[ $# -ge 2 ]] || { echo "--install-dir needs a value" >&2; exit 2; }
      INSTALL_DIR=$2; shift 2 ;;
    --python)
      [[ $# -ge 2 ]] || { echo "--python needs a value" >&2; exit 2; }
      PYTHON_BIN=$2; shift 2 ;;
    --with-driver) WITH_DRIVER=1; shift ;;
    --skip-python-deps) SKIP_PYTHON_DEPS=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "Python executable not found: $PYTHON_BIN" >&2
  exit 1
fi
if ! "$PYTHON_BIN" - <<'PY'
import sys
if sys.version_info < (3, 10):
    raise SystemExit("Python 3.10 or newer is required")
PY
then
  exit 1
fi

INSTALL_DIR="$(realpath -m "$INSTALL_DIR")"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}"
CONFIG_FILE="${MPS_CONFIG_FILE:-$CONFIG_DIR/mps-pressure-dashboard.env}"
SERVICE_DIR="$CONFIG_DIR/systemd/user"
DATA_DIR="${MPS_DATA_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/mps-pressure-dashboard}"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/mps-pressure-dashboard"
CSV_PATH="$STATE_DIR/readings.csv"

mkdir -p "$INSTALL_DIR" "$SERVICE_DIR" "$CONFIG_DIR" "$DATA_DIR/runs" "$DATA_DIR/comparisons" "$STATE_DIR"

# Copy only project files; runtime data and the venv live outside the source tree.
for item in \
  "$SCRIPT_DIR"/*.py "$SCRIPT_DIR"/*.xml "$SCRIPT_DIR"/*.txt \
  "$SCRIPT_DIR"/*.conf "$SCRIPT_DIR"/*.rules "$SCRIPT_DIR"/*.svg \
  "$SCRIPT_DIR"/*.in "$SCRIPT_DIR"/*.md "$SCRIPT_DIR"/*.example \
  "$SCRIPT_DIR"/VERSION "$SCRIPT_DIR"/requirements.txt \
  "$SCRIPT_DIR"/install.sh "$SCRIPT_DIR"/install-access.sh \
  "$SCRIPT_DIR"/install-driver.sh "$SCRIPT_DIR"/open-panel.sh \
  "$SCRIPT_DIR"/doctor.sh "$SCRIPT_DIR"/Makefile "$SCRIPT_DIR"/driver; do
  [[ -e "$item" ]] || continue
  cp -a "$item" "$INSTALL_DIR/"
done
chmod 0755 "$INSTALL_DIR"/*.sh 2>/dev/null || true

VENV="$INSTALL_DIR/.venv"
if [[ ! -x "$VENV/bin/python" ]]; then
  "$PYTHON_BIN" -m venv "$VENV"
fi
if [[ "$SKIP_PYTHON_DEPS" -eq 0 ]]; then
  "$VENV/bin/python" -m pip install --upgrade pip
  "$VENV/bin/python" -m pip install -r "$INSTALL_DIR/requirements.txt"
fi

if [[ ! -e "$CONFIG_FILE" ]]; then
  install -m0600 "$INSTALL_DIR/config.env.example" "$CONFIG_FILE"
  cat >> "$CONFIG_FILE" <<EOF

# Installer-managed portable paths.
MPS_DATA_DIR="$DATA_DIR"
MPS_RUNS_DIR="$DATA_DIR/runs"
MPS_COMPARISONS_DIR="$DATA_DIR/comparisons"
MPS_CSV_PATH="$CSV_PATH"
MPS_SERVICE_NAME=mps-pressure-dashboard.service
EOF
else
  chmod 0600 "$CONFIG_FILE"
fi

escape_sed() {
  printf '%s' "$1" | sed 's/[\\&|]/\\&/g'
}
INSTALL_ESC="$(escape_sed "$INSTALL_DIR")"
CONFIG_ESC="$(escape_sed "$CONFIG_FILE")"
PYTHON_ESC="$(escape_sed "$VENV/bin/python")"

sed \
  -e "s|@INSTALL_DIR@|$INSTALL_ESC|g" \
  -e "s|@CONFIG_FILE@|$CONFIG_ESC|g" \
  -e "s|@PYTHON@|$PYTHON_ESC|g" \
  "$INSTALL_DIR/mps-pressure-dashboard.service.in" \
  > "$SERVICE_DIR/mps-pressure-dashboard.service"

APP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
ICON_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/scalable/apps"
mkdir -p "$APP_DIR" "$ICON_DIR"
sed -e "s|@INSTALL_DIR@|$INSTALL_ESC|g" \
  "$INSTALL_DIR/mps-pressure-dashboard.desktop.in" \
  > "$APP_DIR/pc-monitor-system-panel.desktop"
install -m0644 "$INSTALL_DIR/mps-pressure-dashboard.svg" \
  "$ICON_DIR/mps-pressure-dashboard.svg"
chmod 0755 "$INSTALL_DIR/open-panel.sh"

if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$APP_DIR" >/dev/null 2>&1 || true
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
  gtk-update-icon-cache -f -t "${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor" >/dev/null 2>&1 || true
fi

if command -v systemctl >/dev/null 2>&1; then
  if systemctl --user daemon-reload >/dev/null 2>&1; then
    systemctl --user enable mps-pressure-dashboard.service >/dev/null 2>&1 || true
    systemctl --user start mps-pressure-dashboard.service >/dev/null 2>&1 || \
      echo "Service installed but not started; check: systemctl --user status mps-pressure-dashboard" >&2
  else
    echo "User systemd is unavailable; start manually after enabling lingering/session systemd." >&2
  fi
fi

if [[ "$WITH_DRIVER" -eq 1 ]]; then
  if ! command -v sudo >/dev/null 2>&1; then
    echo "--with-driver requested but sudo is unavailable." >&2
    exit 1
  fi
  sudo "$INSTALL_DIR/install-driver.sh"
fi

echo "Installed PC Monitor System Panel to $INSTALL_DIR"
echo "Configuration: $CONFIG_FILE"
echo "Dashboard: http://127.0.0.1:18080/"
echo "Run '$INSTALL_DIR/doctor.sh' if telemetry is missing."
