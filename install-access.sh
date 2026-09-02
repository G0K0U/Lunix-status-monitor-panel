#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "Run as root: sudo $0" >&2
  exit 1
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
RULE_DEST=/etc/udev/rules.d/69-aquacomputer-mps-pressure.rules
MODPROBE_DEST=/etc/modprobe.d/aquacomputer_d5next.conf

install -Dm0644 "$SCRIPT_DIR/99-aquacomputer-mps-pressure.rules" "$RULE_DEST"
install -Dm0644 "$SCRIPT_DIR/aquacomputer_d5next.conf" "$MODPROBE_DEST"
rm -f /etc/udev/rules.d/99-aquacomputer-mps-pressure.rules

udevadm control --reload-rules 2>/dev/null || true
udevadm trigger --action=change --subsystem-match=hidraw 2>/dev/null || true
udevadm settle 2>/dev/null || true

echo "Installed persistent MPS HID access rule: $RULE_DEST"
echo "Installed driver module option: $MODPROBE_DEST"
