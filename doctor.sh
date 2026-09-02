#!/usr/bin/env bash
set -u

ok=0
warn=0
fail=0
check() {
  local label=$1; shift
  if "$@" >/dev/null 2>&1; then
    printf 'OK   %s\n' "$label"; ok=$((ok + 1))
  else
    printf 'WARN %s\n' "$label"; warn=$((warn + 1))
  fi
}

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
printf 'PC Monitor System Panel diagnostics\n'
printf 'Install root: %s\n\n' "$SCRIPT_DIR"

check "Python 3.10+" python3 -c 'import sys; raise SystemExit(sys.version_info < (3,10))'
check "dashboard modules import" python3 -c "import sys; sys.path.insert(0, '$SCRIPT_DIR'); import mps_pressure_dashboard, pc008_control, pc008_experiment"
check "systemd user session" systemctl --user is-system-running
check "CoolerControl endpoint reachable" curl --silent --show-error --max-time 3 http://127.0.0.1:11987/status
check "stress-ng available" command -v stress-ng
check "systemd-run available" command -v systemd-run
check "MPS HID sysfs device present" test -d /sys/class/hidraw

if compgen -G /sys/class/hidraw/hidraw*/device/uevent >/dev/null; then
  if grep -Rqs 'HID_ID=.*00000C70.*0000F003' /sys/class/hidraw/hidraw*/device/uevent; then
    printf 'OK   MPS USB/HID identity 0c70:f003 detected\n'; ok=$((ok + 1))
  else
    printf 'WARN MPS USB/HID identity 0c70:f003 not detected\n'; warn=$((warn + 1))
  fi
else
  printf 'WARN no hidraw sysfs nodes found\n'; warn=$((warn + 1))
fi

if [[ -f "$SCRIPT_DIR/config.env.example" ]]; then
  printf '\nEdit ~/.config/mps-pressure-dashboard.env for per-machine CoolerControl UIDs and CCD CPU lists.\n'
fi
printf 'Summary: %d OK, %d warnings, %d failures\n' "$ok" "$warn" "$fail"
exit 0
