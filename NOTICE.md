# Notices

The `driver/` directory contains a modified snapshot of the GPL-2.0-or-later
`aquacomputer_d5next-hwmon` driver. Its upstream project and license are
retained in `driver/README.md` and `driver/LICENSE`. The local change adds an
`mps_pressure=1` module option for Aqua Computer MPS Pressure devices sharing
USB product ID `0c70:f003`.

The Python dashboard and installation scripts are provided for this private
testing project. Verify the pressure calibration against a known reference
before using it for safety-critical control.
