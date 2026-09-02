# PC Monitor System Panel

Local Linux dashboard for Aqua Computer MPS pressure/temperature telemetry,
CPU package and CCD temperatures/load/frequency, pump and radiator status, and
guarded package-power experiments. It serves a browser panel at
`http://127.0.0.1:18080/` and stores raw readings plus experiment results under
the user's XDG data/state directories.

The repository is self-contained for the Python application and includes the
optional DKMS source needed to distinguish an Aqua Computer MPS Pressure
device from the shared `0c70:f003` MPS Flow identity. The dashboard reads HID
feature reports directly, so the kernel module is optional for basic operation.

## Install on a new Linux PC

Requirements:

- Linux with Python 3.10 or newer and the `venv` module;
- a local [CoolerControl](https://coolercontrol.org/) service at port 11987
  for CPU/cooling telemetry and control;
- an Aqua Computer MPS USB/HID device for pressure and water temperature;
- `stress-ng` and a user systemd session only when running experiments;
- matching kernel headers, DKMS, and root access only when installing the
  optional kernel driver.

From a checkout, run as the desktop user:

```bash
./install.sh
```

The installer creates a private virtual environment, installs matplotlib and
pandas, installs a per-user systemd service and desktop launcher, and creates
`~/.config/mps-pressure-dashboard.env` without overwriting an existing config.
The service is enabled for future logins and the panel is opened with the
desktop launcher **PC Monitor System Panel**.

Check a new machine with:

```bash
./doctor.sh
```

If kernel support is needed, install your distribution's kernel headers and
DKMS package, then run:

```bash
./install.sh --with-driver
```

This invokes `sudo` only for the bundled driver, udev access rule, and module
option. If the current module is busy, reboot once after installation.

For an offline install, create the virtual environment and install the wheel
dependencies from a local package cache, then use:

```bash
./install.sh --skip-python-deps
```

## Configuration

Hardware-specific CoolerControl UIDs differ between machines. Edit
`~/.config/mps-pressure-dashboard.env` after installation. The template
`config.env.example` documents every supported override:

- `COOLERCONTROL_CPU_UID` and `COOLERCONTROL_COOLING_UID`;
- `MPS_PUMP_CHANNEL`, `MPS_TOP_RADIATOR_CHANNEL`, and
  `MPS_BOTTOM_RADIATOR_CHANNEL`;
- `MPS_CCD0_CPUS` and `MPS_CCD1_CPUS` as comma-separated logical CPU lists;
- `MPS_DEVICE`, `MPS_PORT`, `MPS_INTERVAL`, and XDG/data path overrides.

After editing the file:

```bash
systemctl --user restart mps-pressure-dashboard.service
```

The default storage locations are:

- live CSV: `~/.local/state/mps-pressure-dashboard/readings-YYYY-MM-DD.csv`;
- experiment runs: `~/.local/share/mps-pressure-dashboard/runs/`;
- combined plots: `~/.local/share/mps-pressure-dashboard/comparisons/`.

## Safety and control behavior

Manual cooling writes go through CoolerControl's authenticated REST API. Only
the configured pump and two radiator channels are allowlisted. Manual controls
lock during an automated experiment. Every experiment captures the original
CoolerControl settings and restores them on completion, abort, thermal cutoff,
or error.

Safe defaults are dry-run validation, pump/radiators at 100%, a 95°C cutoff,
180-second minimum stage time, a 120-second stable window, a 900-second hard
stage timeout, stale-telemetry/pump-RPM/workload-exit checks, and a bounded
velocity-PI load controller. The experiment records both CCD temperatures,
water-after-block temperature, calibrated pressure, package power, CCD load
and frequency, pump, and both radiator groups.

The panel exposes measured CPU **package** watts. Per-CCD watts are explicitly
estimates based on dynamic package power and `load × frequency`; the requested
CCD workload split is a scheduler ratio, not a guaranteed power ratio.

## Aqua calibration

`calibration_curve_raw_to_mbar.xml` is Aqua Computer's exported 16-point
piecewise-linear RAW→mbar curve. At startup the panel loads it beside the
Python module, clamps values outside the exported range to 0–100 mbar, and
retains the original report bytes and legacy result candidate for diagnostics.
The normalized raw field is used as the calibration input. Confirm the curve
against a known zero/reference pressure before treating it as a laboratory
measurement.

## HTTP endpoints

- `/` — interactive local dashboard;
- `/api/live` — latest decoded reading and experiment status;
- `/api/history` — chart history;
- `/api/calibration` — active Aqua curve;
- `/metrics` — Prometheus text metrics.

The server binds to loopback by default. Change `MPS_LISTEN` only when a
trusted network exposure is intended.

## Development and tests

```bash
make test
make compile
```

The tests use the bundled 50-byte HID fixture in `tests/fixtures`, so they do
not depend on the original development machine or a connected device.

## Repository layout

- `mps_pressure_dashboard.py` — HID reader, HTTP server, UI, and CSV writer;
- `pc008_control.py` — CoolerControl client and system telemetry;
- `pc008_experiment.py` — guarded CCD workload/power experiment manager;
- `plot_pc008_waterblock.py` — per-run and combined comparison plots;
- `driver/` — optional modified GPL driver source and reverse-engineering docs;
- `install.sh`, `doctor.sh` — portable user install and diagnostics;
- `config.env.example` — per-machine configuration template.

See `NOTICE.md` for the upstream driver license and provenance.
