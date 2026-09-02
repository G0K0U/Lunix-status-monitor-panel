#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="${MPS_SERVICE_NAME:-mps-pressure-dashboard.service}"
URL="http://${MPS_LISTEN:-127.0.0.1}:${MPS_PORT:-18080}/"

CONFIG_FILE="${MPS_CONFIG_FILE:-${XDG_CONFIG_HOME:-$HOME/.config}/mps-pressure-dashboard.env}"
if [[ -f "$CONFIG_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  . "$CONFIG_FILE"
  set +a
  SERVICE_NAME="${MPS_SERVICE_NAME:-mps-pressure-dashboard.service}"
  URL="http://${MPS_LISTEN:-127.0.0.1}:${MPS_PORT:-18080}/"
fi

if command -v systemctl >/dev/null 2>&1 && systemctl --user daemon-reload >/dev/null 2>&1; then
  systemctl --user start "$SERVICE_NAME"
fi
if command -v xdg-open >/dev/null 2>&1; then
  xdg-open "$URL" >/dev/null 2>&1 &
else
  printf 'Dashboard URL: %s\n' "$URL"
fi
