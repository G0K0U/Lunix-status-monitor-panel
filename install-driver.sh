#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "Run as root: sudo $0" >&2
  exit 1
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
DRIVER_DIR="$SCRIPT_DIR/driver"
DRIVER_NAME=aquacomputer_d5next
DRIVER_VERSION="${MPS_DRIVER_VERSION:-0.1.0}"
KERNEL="$(uname -r)"

if [[ ! -f "$DRIVER_DIR/aquacomputer_d5next.c" || ! -f "$DRIVER_DIR/dkms.conf" ]]; then
  echo "Bundled driver source is incomplete: $DRIVER_DIR" >&2
  exit 1
fi
if ! command -v dkms >/dev/null 2>&1; then
  echo "DKMS is required. Install your distribution's dkms package, then rerun." >&2
  exit 1
fi
if [[ ! -e "/lib/modules/$KERNEL/build" && ! -e "/usr/src/linux-headers-$KERNEL" && ! -e "/usr/src/kernels/$KERNEL" ]]; then
  echo "Kernel headers/build tree not found for $KERNEL." >&2
  echo "Install the matching kernel headers/devel package, then rerun." >&2
  exit 1
fi

DKMS_SOURCE="/usr/src/${DRIVER_NAME}-${DRIVER_VERSION}"
if dkms status -m "$DRIVER_NAME" -v "$DRIVER_VERSION" 2>/dev/null | grep -q .; then
  dkms remove -m "$DRIVER_NAME" -v "$DRIVER_VERSION" --all || true
fi
rm -rf "$DKMS_SOURCE"
install -d -m0755 "$DKMS_SOURCE"
cp -a "$DRIVER_DIR/." "$DKMS_SOURCE/"
sed -i "s/#MODULE_VERSION#/$DRIVER_VERSION/g" "$DKMS_SOURCE/dkms.conf"
printf '%s\n' "$DRIVER_VERSION" > "$DKMS_SOURCE/VERSION"

dkms add -m "$DRIVER_NAME" -v "$DRIVER_VERSION"
dkms build -m "$DRIVER_NAME" -v "$DRIVER_VERSION" -k "$KERNEL"
dkms install --force -m "$DRIVER_NAME" -v "$DRIVER_VERSION" -k "$KERNEL"

"$SCRIPT_DIR/install-access.sh"
depmod -a "$KERNEL"

# The stock module may already be loaded for the shared 0c70:f003 product ID.
# Rebinding is best-effort; a reboot activates the persistent option if busy.
modprobe -r "$DRIVER_NAME" 2>/dev/null || true
if modprobe "$DRIVER_NAME" mps_pressure=1; then
  echo "MPS Pressure driver active for kernel $KERNEL."
  cat "/sys/module/$DRIVER_NAME/parameters/mps_pressure" 2>/dev/null || true
else
  echo "Driver installed but could not be rebound now; reboot to activate it." >&2
fi
