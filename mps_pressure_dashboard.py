#!/usr/bin/env python3
"""Read Aqua Computer MPS feature reports and expose a local dashboard.

The MPS family uses USB product ID 0c70:f003 for several different products.
This reader preserves every byte and labels only fields confirmed by the
reverse-engineering notes in aquacomputer_d5next-hwmon. A copied Aqua Computer
RAW-to-mbar calibration curve is loaded at startup for the MPS Pressure device.
"""

from __future__ import annotations

import argparse
from bisect import bisect_right
import csv
import fcntl
import json
import math
import os
import struct
import threading
import time
import xml.etree.ElementTree as ET
from collections import deque
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from pc008_control import (
    BOTTOM_RADIATOR_CHANNEL,
    PUMP_CHANNEL,
    TOP_RADIATOR_CHANNEL,
    CoolerControlClient,
    SystemTelemetry,
    capture_cooling_settings,
)
from pc008_experiment import COMPARISONS_DIR, ExperimentManager

STATUS_REPORT_ID = 0x02
STATUS_REPORT_SIZE = 0x76
CONFIG_REPORT_ID = 0x03
CONFIG_REPORT_SIZE = 0xD6
DEFAULT_DEVICE = "auto"
LEGACY_DEVICE_LINK = Path("/dev/input/by-id/usb-Aqua_Computer_MPS-hidraw")
MPS_HID_ID = "0003:00000C70:0000F003"
CALIBRATION_PATH = Path(__file__).with_name("calibration_curve_raw_to_mbar.xml")
DEFAULT_CALIBRATION_SCALED = (
    0.0, 6.67, 13.33, 20.0, 26.67, 33.33, 40.0, 46.76,
    53.33, 60.0, 66.67, 73.33, 80.0, 86.67, 93.33, 100.0,
)
DEFAULT_CALIBRATION_RAW = (
    7.0, 60.0, 114.0, 167.0, 221.0, 274.0, 328.0, 381.0,
    435.0, 488.0, 542.0, 595.0, 649.0, 702.0, 756.0, 809.0,
)
CSV_FIELDS = (
    "timestamp", "pressure_raw", "pressure_offset_raw", "pressure_normalized_raw",
    "field_0x23_raw", "field_0x25_raw", "pressure_candidate_mbar",
    "pressure_calibration_input_raw", "pressure_calibrated_mbar",
    "temperature_external_c", "temperature_internal_c", "cpu_tctl_c",
    "ccd0_temp_c", "ccd1_temp_c", "x3d_ccd_temp_c", "cpu_package_power_w",
    "ccd0_attributed_power_w", "ccd1_attributed_power_w",
    "ccd0_estimated_power_w", "ccd1_estimated_power_w",
    "ccd_power_idle_baseline_w",
    "cpu_load_pct", "ccd0_load_pct", "ccd1_load_pct", "ccd0_frequency_mhz",
    "ccd1_frequency_mhz", "pump_rpm", "pump_duty_pct", "top_radiator_rpm",
    "top_radiator_duty_pct", "bottom_radiator_rpm",
    "bottom_radiator_duty_pct", "hex",
)


def load_calibration_curve(path: Path = CALIBRATION_PATH) -> tuple[tuple[float, ...], tuple[float, ...]]:
    """Load Aqua Computer's exported RAW-to-mbar piecewise-linear curve."""
    try:
        root = ET.parse(path).getroot()
        raw_values = tuple(float(node.text) for node in root.findall("./rawValues/double"))
        scaled_values = tuple(float(node.text) for node in root.findall("./scaledValues/double"))
        if len(raw_values) < 2 or len(raw_values) != len(scaled_values):
            raise ValueError("calibration curve must contain equal arrays with at least two points")
        if any(b <= a for a, b in zip(raw_values, raw_values[1:])):
            raise ValueError("calibration raw values must be strictly increasing")
        return raw_values, scaled_values
    except (OSError, ET.ParseError, TypeError, ValueError):
        # Keep the panel usable if the removable calibration media is absent or
        # the copied file is damaged; the bundled defaults are the supplied XML.
        return DEFAULT_CALIBRATION_RAW, DEFAULT_CALIBRATION_SCALED


CALIBRATION_RAW_VALUES, CALIBRATION_MBAR_VALUES = load_calibration_curve()


def calibrated_pressure_mbar(raw_value: Any) -> float | None:
    """Interpolate the supplied RAW-to-mbar curve, clamping outside its range."""
    if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
        return None
    raw = float(raw_value)
    if not math.isfinite(raw):
        return None
    if raw <= CALIBRATION_RAW_VALUES[0]:
        return CALIBRATION_MBAR_VALUES[0]
    if raw >= CALIBRATION_RAW_VALUES[-1]:
        return CALIBRATION_MBAR_VALUES[-1]
    index = bisect_right(CALIBRATION_RAW_VALUES, raw) - 1
    raw_low, raw_high = CALIBRATION_RAW_VALUES[index:index + 2]
    mbar_low, mbar_high = CALIBRATION_MBAR_VALUES[index:index + 2]
    fraction = (raw - raw_low) / (raw_high - raw_low)
    return round(mbar_low + fraction * (mbar_high - mbar_low), 3)


def discover_mps_device() -> str:
    """Find MPS hidraw node independent of USB port and hidraw number."""
    if LEGACY_DEVICE_LINK.exists():
        return str(LEGACY_DEVICE_LINK)
    for hidraw in sorted(Path("/sys/class/hidraw").glob("hidraw*")):
        try:
            uevent = (hidraw / "device/uevent").read_text()
        except OSError:
            continue
        if f"HID_ID={MPS_HID_ID}" in uevent:
            return f"/dev/{hidraw.name}"
    raise FileNotFoundError(
        "Aqua Computer MPS USB device 0c70:f003 not detected; "
        "check internal USB header connection and orientation"
    )


def _ioc(direction: int, kind: int, number: int, size: int) -> int:
    return (direction << 30) | (size << 16) | (kind << 8) | number


def hidiocgfeature(size: int) -> int:
    return _ioc(3, ord("H"), 0x07, size)


def le_u16(data: bytes, offset: int) -> int:
    return struct.unpack_from("<H", data, offset)[0]


def le_i16(data: bytes, offset: int) -> int:
    return struct.unpack_from("<h", data, offset)[0]


def decode_report(data: bytes, timestamp: float | None = None) -> dict[str, Any]:
    if len(data) < 0x2F:
        raise ValueError(f"short feature report: {len(data)} bytes")
    external_raw = le_u16(data, 0x2B)
    internal_raw = le_u16(data, 0x2D)
    normalized_raw = le_u16(data, 0x1B)
    field_0x25 = le_u16(data, 0x25)
    words = {
        f"word_0x{offset:02x}": le_u16(data, offset)
        for offset in range(1, len(data) - 1, 2)
    }
    return {
        "timestamp": timestamp if timestamp is not None else time.time(),
        "report_bytes": len(data),
        "report_id": data[0],
        "firmware_raw": le_u16(data, 0x03),
        "serial_raw": le_u16(data, 0x09),
        "pressure_raw": le_u16(data, 0x11),
        "pressure_offset_raw": le_i16(data, 0x19),
        "pressure_normalized_raw": normalized_raw,
        "field_0x23_raw": le_u16(data, 0x23),
        "field_0x25_raw": field_0x25,
        # Preserve the legacy result candidate while applying the exported
        # Aqua Computer RAW-to-mbar curve to the normalized raw field.
        "pressure_candidate_mbar": field_0x25 / 10.0,
        "pressure_calibration_input_raw": normalized_raw,
        "pressure_calibrated_mbar": calibrated_pressure_mbar(normalized_raw),
        "temperature_external_c": None if external_raw == 0x7FFF else external_raw / 100.0,
        "temperature_internal_c": None if internal_raw == 0x7FFF else internal_raw / 100.0,
        "hex": data.hex(" "),
        "words_le_u16": words,
    }


class MPSDevice:
    def __init__(self, path: str):
        self.requested_path = path
        self.path: str | None = None
        self.fd: int | None = None

    def open(self) -> None:
        if self.fd is None:
            requested = Path(self.requested_path)
            if self.requested_path == "auto" or not requested.exists():
                self.path = discover_mps_device()
            else:
                self.path = self.requested_path
            self.fd = os.open(self.path, os.O_RDWR | os.O_CLOEXEC)

    def close(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def read_feature(self, report_id: int, report_size: int) -> bytes:
        self.open()
        report = bytearray(report_size)
        report[0] = report_id
        try:
            result = fcntl.ioctl(self.fd, hidiocgfeature(len(report)), report, True)
        except OSError:
            self.close()
            raise
        length = result if isinstance(result, int) and result > 0 else len(report)
        return bytes(report[:length])

    def read_status(self) -> bytes:
        return self.read_feature(STATUS_REPORT_ID, STATUS_REPORT_SIZE)

    def read_config(self) -> bytes:
        return self.read_feature(CONFIG_REPORT_ID, CONFIG_REPORT_SIZE)


@dataclass
class SharedState:
    latest: dict[str, Any] = field(default_factory=dict)
    config: dict[str, Any] = field(default_factory=dict)
    history: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=3600))
    error: str | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)


def append_csv(path: Path, reading: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    day = time.strftime("%Y-%m-%d", time.localtime(reading["timestamp"]))
    suffixes = ("", "-pc008", "-pc008-radiators", "-pc008-power")
    dated_path: Path | None = None
    for suffix in suffixes:
        candidate = path.with_name(f"{path.stem}-{day}{suffix}{path.suffix}")
        if not candidate.exists():
            dated_path = candidate
            break
        try:
            with candidate.open(newline="", encoding="utf-8") as existing:
                if next(csv.reader(existing), []) == list(CSV_FIELDS):
                    dated_path = candidate
                    break
        except OSError:
            continue
    if dated_path is None:
        dated_path = path.with_name(f"{path.stem}-{day}-schema-{len(CSV_FIELDS)}{path.suffix}")
    new_file = not dated_path.exists()
    if new_file:
        cutoff = time.time() - 7 * 24 * 60 * 60
        for old_path in path.parent.glob(f"{path.stem}-????-??-??{path.suffix}"):
            if old_path.stat().st_mtime < cutoff:
                old_path.unlink()
    with dated_path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        if new_file:
            writer.writerow(CSV_FIELDS)
        writer.writerow([reading.get(key) for key in CSV_FIELDS])


def read_hwmon_fallback() -> dict[str, Any]:
    """Return fields already exported by the kernel while HID access is pending."""
    for hwmon in Path("/sys/class/hwmon").glob("hwmon*"):
        try:
            if (hwmon / "name").read_text().strip() != "highflow":
                continue
            external = int((hwmon / "temp1_input").read_text()) / 1000.0
            internal = int((hwmon / "temp2_input").read_text()) / 1000.0
            field_0x23 = int((hwmon / "fan1_input").read_text())
            return {
                "timestamp": time.time(),
                "source": "hwmon-fallback",
                "field_0x23_raw": field_0x23,
                "temperature_external_c": external,
                "temperature_internal_c": internal,
            }
        except (OSError, ValueError):
            continue
    raise FileNotFoundError("highflow hwmon device not found")


def attributed_power_fields(experiment: dict[str, Any]) -> dict[str, float | None]:
    result: dict[str, float | None] = {
        "ccd0_attributed_power_w": None,
        "ccd1_attributed_power_w": None,
    }
    latest = experiment.get("latest", {})
    if experiment.get("state") != "running" or not isinstance(latest, dict):
        return result
    active_ccd = latest.get("active_ccd")
    attributed = latest.get("attributed_ccd_power_w")
    if active_ccd in {0, 1} and isinstance(attributed, (int, float)):
        result["ccd0_attributed_power_w"] = attributed if active_ccd == 0 else 0.0
        result["ccd1_attributed_power_w"] = attributed if active_ccd == 1 else 0.0
    return result


class CCDPowerEstimator:
    """Estimate continuous per-CCD dynamic watts from package power and activity."""

    def __init__(self) -> None:
        self.idle_package_w: float | None = None

    def update(self, reading: dict[str, Any]) -> dict[str, float | None]:
        power = reading.get("cpu_package_power_w")
        loads = (reading.get("ccd0_load_pct"), reading.get("ccd1_load_pct"))
        frequencies = (
            reading.get("ccd0_frequency_mhz"), reading.get("ccd1_frequency_mhz")
        )
        empty = {
            "ccd0_estimated_power_w": None,
            "ccd1_estimated_power_w": None,
            "ccd_power_idle_baseline_w": self.idle_package_w,
        }
        if not isinstance(power, (int, float)) or any(
            not isinstance(value, (int, float)) for value in (*loads, *frequencies)
        ):
            return empty
        total_load = float(loads[0]) + float(loads[1])
        # Learn idle only at genuinely light load; allow slow upward drift and
        # immediate downward correction as background services change.
        if total_load <= 8:
            if self.idle_package_w is None or power < self.idle_package_w:
                self.idle_package_w = float(power)
            else:
                self.idle_package_w = 0.995 * self.idle_package_w + 0.005 * float(power)
        if self.idle_package_w is None:
            self.idle_package_w = float(power)
        dynamic = max(0.0, float(power) - self.idle_package_w)
        weights = [
            max(0.0, float(loads[index])) * max(0.0, float(frequencies[index]))
            for index in (0, 1)
        ]
        total_weight = sum(weights)
        if total_weight <= 0:
            estimates = (0.0, 0.0)
        else:
            estimates = tuple(dynamic * weight / total_weight for weight in weights)
        return {
            "ccd0_estimated_power_w": round(estimates[0], 3),
            "ccd1_estimated_power_w": round(estimates[1], 3),
            "ccd_power_idle_baseline_w": round(self.idle_package_w, 3),
        }


def sample_loop(
    device: MPSDevice,
    system_telemetry: SystemTelemetry,
    state: SharedState,
    interval: float,
    csv_path: Path,
    experiment_status: Callable[[], dict[str, Any]],
) -> None:
    next_config_read = 0.0
    power_estimator = CCDPowerEstimator()
    while True:
        started = time.monotonic()
        errors: list[str] = []
        reading: dict[str, Any] = {"timestamp": time.time()}
        config = None
        try:
            reading.update(decode_report(device.read_status(), reading["timestamp"]))
            now = time.monotonic()
            if now >= next_config_read:
                raw_config = device.read_config()
                config = {
                    "timestamp": time.time(),
                    "report_bytes": len(raw_config),
                    "report_id": raw_config[0],
                    "bytes": list(raw_config),
                    "hex": raw_config.hex(" "),
                }
                next_config_read = now + 300.0
        except PermissionError as exc:
            try:
                reading.update(read_hwmon_fallback())
                errors.append(f"Raw pressure unavailable: {exc}")
            except Exception as fallback_exc:
                errors.append(f"MPS: {type(fallback_exc).__name__}: {fallback_exc}")
        except Exception as exc:
            errors.append(f"MPS: {type(exc).__name__}: {exc}")
        try:
            reading.update(system_telemetry.read())
        except Exception as exc:
            errors.append(f"System telemetry: {type(exc).__name__}: {exc}")
        reading.update(attributed_power_fields(experiment_status()))
        reading.update(power_estimator.update(reading))
        try:
            append_csv(csv_path, reading)
        except Exception as exc:
            errors.append(f"CSV: {type(exc).__name__}: {exc}")
        with state.lock:
            state.latest = reading
            state.history.append(reading)
            if config is not None:
                state.config = config
            state.error = "; ".join(errors) if errors else None
        elapsed = time.monotonic() - started
        time.sleep(max(0.1, interval - elapsed))


HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>PC 008 Water-Cooling Lab</title>
<style>
:root{color-scheme:dark;background:#101419;color:#e8edf2;font:15px system-ui,sans-serif}*{box-sizing:border-box}body{max-width:1280px;margin:auto;padding:24px}
h1{font-size:24px;margin:0 0 4px}h2{font-size:18px}.muted{color:#9da9b5}.hint{margin:6px 0 0;font-size:13px}.data-groups{display:grid;gap:14px;margin:20px 0}.data-group{padding:14px;border:1px solid var(--group);border-left:5px solid var(--group);border-radius:10px;background:#151b21}.data-group h2{margin:0 0 10px;color:var(--group)}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px;margin:0}.cpu-group{--group:#4fc3f7}.cooling-group{--group:#ab87ff}.loop-group{--group:#66d18f}
.card,.chart-panel,.control-panel{background:#192129;border:1px solid #2b3945;border-radius:10px}.card{padding:14px}.label{color:#9da9b5;font-size:12px;text-transform:uppercase}.value{font:26px ui-monospace,monospace;margin-top:5px}
.chart-panel{margin:16px 0;padding:14px}.chart-head{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:8px}.chart-title{font-weight:650;font-size:17px}.controls{display:flex;gap:6px;flex-wrap:wrap}button{color:#dce8f2;background:#263542;border:1px solid #3b5061;border-radius:6px;padding:5px 9px;cursor:pointer}button:hover{background:#314657}button.active{background:#17678d;border-color:#3baada}.chart-stats{display:grid;grid-template-columns:minmax(140px,1.5fr) repeat(3,minmax(90px,1fr)) minmax(70px,.7fr);gap:5px 12px;align-items:center;margin:8px 0 10px;padding:9px 12px;background:#151b21;border-radius:7px;font:13px ui-monospace,monospace}.stats-head{color:#7f8d99;font:11px system-ui,sans-serif;text-transform:uppercase}.stats-series{font-weight:650}.stats-number{color:#e8edf2}.stats-empty{grid-column:1/-1;color:#7f8d99}
.chart-wrap{position:relative;width:100%;height:350px;touch-action:none}.chart-wrap canvas{display:block;width:100%;height:100%;background:#151b21;border-radius:7px}.tooltip{position:absolute;display:none;pointer-events:none;z-index:2;min-width:145px;padding:8px 10px;background:#0c1116ee;border:1px solid #566775;border-radius:6px;font:12px ui-monospace,monospace;white-space:nowrap;box-shadow:0 3px 12px #0008}.result-plots{display:grid;grid-template-columns:repeat(auto-fit,minmax(420px,1fr));gap:14px}.result-plots img{display:block;width:100%;min-height:220px;object-fit:contain;background:#fff;border-radius:7px}
pre{overflow:auto;max-height:420px;background:#151b21;padding:14px;border-radius:10px}#error{color:#ff8b8b;margin-top:8px}.control-panel{padding:16px;margin:18px 0;border-left:5px solid #ffb74d}.control-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px}.field label{display:block;color:#aeb9c3;font-size:12px;margin-bottom:5px}.field input,.field textarea{width:100%;background:#111820;color:#e8edf2;border:1px solid #3b5061;border-radius:6px;padding:8px}.field textarea{min-height:68px;resize:vertical}.actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}.danger{background:#7a2930;border-color:#b64b55}.warning{color:#ffd58a}.control-result{min-height:20px;margin-top:9px;font:13px ui-monospace,monospace}.section-note{font-size:13px;color:#9da9b5;margin:5px 0 12px}
@media(max-width:650px){body{padding:12px}.chart-wrap{height:300px}.chart-head{align-items:flex-start;flex-direction:column}}
</style></head><body>
<h1>PC 008 Water-Cooling Lab</h1><div class="muted">Ryzen 9 9950X3D CCD telemetry, cooling controls, and MPS pressure Δ100. The supplied Aqua Computer 16-point RAW→mbar curve is applied to the normalized pressure field; raw values and the device result remain available for comparison.</div><div id="error"></div>
<div class="data-groups"><section class="data-group cpu-group"><h2>Processor</h2><div class="cards">
<div class="card"><div class="label">CPU package power</div><div class="value" id="cpu-power">—</div></div>
<div class="card"><div class="label">CCD0 estimated dynamic power Δ</div><div class="value" id="ccd0-power">—</div></div>
<div class="card"><div class="label">CCD1 estimated dynamic power Δ</div><div class="value" id="ccd1-power">—</div></div>
<div class="card"><div class="label">CPU Tctl</div><div class="value" id="cpu-tctl">—</div></div>
<div class="card"><div class="label">CCD0 / Tccd1 (X3D inferred)</div><div class="value" id="ccd0-temp">—</div></div>
<div class="card"><div class="label">CCD1 / Tccd2</div><div class="value" id="ccd1-temp">—</div></div>
<div class="card"><div class="label">CCD0 load</div><div class="value" id="ccd0-load">—</div></div>
<div class="card"><div class="label">CCD1 load</div><div class="value" id="ccd1-load">—</div></div>
</div></section><section class="data-group cooling-group"><h2>Cooling</h2><div class="cards"><div class="card"><div class="label">Pump</div><div class="value" id="pump-live">—</div></div>
<div class="card"><div class="label">Top radiator · SYS_FAN2</div><div class="value" id="top-radiator-live">—</div></div>
<div class="card"><div class="label">Bottom radiator · SYS_FAN3</div><div class="value" id="bottom-radiator-live">—</div></div>
</div></section><section class="data-group loop-group"><h2>Water loop</h2><div class="cards"><div class="card"><div class="label">Pressure drop (Aqua curve)</div><div class="value" id="pc">—</div></div><div class="card"><div class="label">Device result field (comparison)</div><div class="value" id="pc-device">—</div></div>
<div class="card"><div class="label">Water after block</div><div class="value" id="te">—</div></div>
<div class="card"><div class="label">MPS electronics temperature</div><div class="value" id="ti">—</div></div>
</div></section></div>
<div class="control-panel"><h2>Manual cooling control</h2><div class="section-note">Pump is constrained to 60–100%. Top and bottom radiator groups are independently constrained to 20–100%. Current mapping is top=SYS_FAN2 and bottom=SYS_FAN3; both tach inputs presently report 0 RPM, so verify the physical wiring before a real run. Restore returns each header to the firmware/profile setting captured at dashboard startup.</div><div class="control-grid"><div class="field"><label for="pump-duty">Pump duty (%)</label><input id="pump-duty" type="number" min="60" max="100" value="100"></div><div class="field"><label for="top-radiator-duty">Top radiator duty (%)</label><input id="top-radiator-duty" type="number" min="20" max="100" value="100"></div><div class="field"><label for="bottom-radiator-duty">Bottom radiator duty (%)</label><input id="bottom-radiator-duty" type="number" min="20" max="100" value="100"></div></div><div class="actions"><button onclick="setCooling('pump')">Apply pump</button><button onclick="restoreCooling('pump')">Restore pump</button><button onclick="setCooling('top_radiator')">Apply top radiator</button><button onclick="restoreCooling('top_radiator')">Restore top radiator</button><button onclick="setCooling('bottom_radiator')">Apply bottom radiator</button><button onclick="restoreCooling('bottom_radiator')">Restore bottom radiator</button></div><div id="control-result" class="control-result"></div></div>
<div class="control-panel"><h2>Automated steady-state power experiment</h2><div class="section-note">The sweep targets measured CPU package power. Min, max, and step generate the stages. CCD split controls the pinned workload ratio; it is not a guaranteed per-CCD watt ratio. Feedback adjusts both CCD loads while preserving the requested split.</div><div class="control-grid"><div class="field"><label for="run-label">Block/base label</label><input id="run-label" value="bykski_stock_base"></div><div class="field"><label for="block-notes">Block and mount notes</label><textarea id="block-notes">Bykski AMD water block; stock copper base</textarea></div><div class="field"><label for="package-power-min">Package power minimum (W)</label><input id="package-power-min" type="number" min="35" max="230" step="1" value="60"></div><div class="field"><label for="package-power-max">Package power maximum (W)</label><input id="package-power-max" type="number" min="35" max="230" step="1" value="150"></div><div class="field"><label for="package-power-step">Package power step (W)</label><input id="package-power-step" type="number" min="1" max="100" step="1" value="15"></div><div class="field"><label for="ccd0-workload-share">CCD0 workload share (%)</label><input id="ccd0-workload-share" type="number" min="0" max="100" step="1" value="50"></div><div class="field"><label>CCD1 workload share (%)</label><input id="ccd1-workload-share" type="number" value="50" readonly></div><div class="field"><label for="power-tolerance">Power tolerance (W)</label><input id="power-tolerance" type="number" min="1" max="10" step="0.5" value="3"></div><div class="field"><label for="run-pump">Fixed pump duty (%)</label><input id="run-pump" type="number" min="60" max="100" value="100"></div><div class="field"><label for="run-top-radiator">Fixed top radiator duty (%)</label><input id="run-top-radiator" type="number" min="20" max="100" value="100"></div><div class="field"><label for="run-bottom-radiator">Fixed bottom radiator duty (%)</label><input id="run-bottom-radiator" type="number" min="20" max="100" value="100"></div><div class="field"><label for="stability-sec">Minimum stage time (s)</label><input id="stability-sec" type="number" min="60" max="900" value="180"></div><div class="field"><label for="window-sec">Steady-state window (s)</label><input id="window-sec" type="number" min="30" max="600" value="120"></div><div class="field"><label for="stage-max-sec">Stage timeout (s)</label><input id="stage-max-sec" type="number" min="90" max="1800" value="900"></div><div class="field"><label for="cutoff-c">Emergency cutoff (°C)</label><input id="cutoff-c" type="number" min="70" max="95" value="95"></div></div><label class="warning"><input id="run-confirm" type="checkbox"> I confirm pump operation, top/bottom header mapping, and authorize CPU stress plus fixed cooling settings.</label><div class="actions"><button onclick="startExperiment(true)">Validate dry run</button><button onclick="startExperiment(false)">Start real run</button><button class="danger" onclick="abortExperiment()">Abort immediately</button></div><pre id="experiment-status">No experiment running.</pre></div>
<div class="chart-panel"><div class="chart-title">PC008 experiment comparisons</div><div class="section-note">Automatically refreshed after every stable stage. Files are stored in the configured comparisons directory.</div><div class="result-plots"><img id="comparison-temp" alt="Temperature versus power comparison"><img id="comparison-delta" alt="Delta-T versus power comparison"><img id="comparison-rth" alt="Thermal resistance comparison"><img id="comparison-metrics" alt="Key metrics comparison"></div></div>
<div class="muted hint">Mouse wheel: zoom · drag: pan · hover: exact time and values · double-click: reset</div>
<div class="chart-panel"><div class="chart-head"><div class="chart-title">CPU, CCD, and water-block outlet temperatures</div><div class="controls" data-chart="cpuTemperature"><button data-range="60">1 min</button><button data-range="300">5 min</button><button data-range="900">15 min</button><button data-range="all">All</button><button data-reset>Reset zoom</button></div></div><div class="chart-wrap"><canvas id="cpu-temperature-chart"></canvas><div class="tooltip"></div></div></div>
<div class="chart-panel"><div class="chart-head"><div class="chart-title">CPU package and estimated per-CCD dynamic power</div><div class="controls" data-chart="power"><button data-range="60">1 min</button><button data-range="300">5 min</button><button data-range="900">15 min</button><button data-range="all">All</button><button data-reset>Reset zoom</button></div></div><div class="section-note">Package power is measured. Continuous CCD curves estimate package power above a learned idle baseline, apportioned by CCD load × frequency. They are not hardware per-CCD power sensors.</div><div class="chart-wrap"><canvas id="power-chart"></canvas><div class="tooltip"></div></div></div>
<div class="chart-panel"><div class="chart-head"><div class="chart-title">Per-CCD CPU load</div><div class="controls" data-chart="loads"><button data-range="60">1 min</button><button data-range="300">5 min</button><button data-range="900">15 min</button><button data-range="all">All</button><button data-reset>Reset zoom</button></div></div><div class="chart-wrap"><canvas id="loads-chart"></canvas><div class="tooltip"></div></div></div>
<div class="chart-panel"><div class="chart-head"><div class="chart-title">Pump and radiator-group tachometers</div><div class="controls" data-chart="cooling"><button data-range="60">1 min</button><button data-range="300">5 min</button><button data-range="900">15 min</button><button data-range="all">All</button><button data-reset>Reset zoom</button></div></div><div class="chart-wrap"><canvas id="cooling-chart"></canvas><div class="tooltip"></div></div></div>
<div class="chart-panel"><div class="chart-head"><div class="chart-title">Pressure drop (Aqua calibration)</div><div class="controls" data-chart="pressure"><button data-range="60">1 min</button><button data-range="300">5 min</button><button data-range="900">15 min</button><button data-range="all">All</button><button data-reset>Reset zoom</button></div></div><div class="section-note">The 16-point RAW→mbar curve from Aqua Computer is interpolated against the normalized pressure field and clamped to 0–100 mbar outside the exported range. The device result field is retained above for comparison.</div><div class="chart-wrap"><canvas id="pressure-chart"></canvas><div class="tooltip"></div></div></div>
<div class="chart-panel"><div class="chart-head"><div class="chart-title">Water outlet and MPS electronics temperatures</div><div class="controls" data-chart="temperature"><button data-range="60">1 min</button><button data-range="300">5 min</button><button data-range="900">15 min</button><button data-range="all">All</button><button data-reset>Reset zoom</button></div></div><div class="chart-wrap"><canvas id="temperature-chart"></canvas><div class="tooltip"></div></div></div>
<script>
const $=id=>document.getElementById(id), history=[];
const timeLabel=t=>new Date(t*1000).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit',second:'2-digit'});
const finite=v=>Number.isFinite(v);

class InteractiveChart {
  constructor(id,yTitle,series){this.canvas=$(id);this.ctx=this.canvas.getContext('2d');this.wrap=this.canvas.parentElement;this.tip=this.wrap.querySelector('.tooltip');this.yTitle=yTitle;this.series=series;this.range=300;this.view=null;this.mouse=null;this.drag=null;this.stats=document.createElement('div');this.stats.className='chart-stats';this.wrap.before(this.stats);
    new ResizeObserver(()=>this.draw()).observe(this.wrap);
    this.canvas.addEventListener('wheel',e=>this.zoom(e),{passive:false});this.canvas.addEventListener('pointerdown',e=>{this.canvas.setPointerCapture(e.pointerId);this.drag={x:e.offsetX,view:this.bounds()};});
    this.canvas.addEventListener('pointermove',e=>this.move(e));this.canvas.addEventListener('pointerup',()=>this.drag=null);this.canvas.addEventListener('pointercancel',()=>this.drag=null);this.canvas.addEventListener('pointerleave',()=>{if(!this.drag){this.mouse=null;this.tip.style.display='none';this.draw();}});this.canvas.addEventListener('dblclick',()=>this.reset());
  }
  setRange(seconds){this.range=seconds;this.view=null;this.draw();}
  reset(){this.view=null;this.draw();}
  bounds(){const now=history.length?history[history.length-1].timestamp:Date.now()/1000,first=history.length?history[0].timestamp:now-60;if(this.view)return this.view;return this.range==='all'?[first,Math.max(now,first+10)]:[Math.max(first,now-this.range),Math.max(now,first+10)];}
  margins(){return {l:72,r:18,t:30,b:48};}
  zoom(e){e.preventDefault();if(history.length<2)return;const m=this.margins(),width=this.canvas.clientWidth-m.l-m.r;if(width<=0)return;const [a,b]=this.bounds(),focus=a+Math.max(0,Math.min(1,(e.offsetX-m.l)/width))*(b-a),factor=e.deltaY>0?1.25:.8,total=Math.max(10,history[history.length-1].timestamp-history[0].timestamp),span=Math.max(10,Math.min(total,(b-a)*factor)),ratio=(focus-a)/(b-a);let start=focus-span*ratio,end=start+span;const low=history[0].timestamp,high=history[history.length-1].timestamp;if(start<low){end+=low-start;start=low}if(end>high){start-=end-high;end=high}this.view=[Math.max(low,start),Math.min(high,end)];this.draw();}
  move(e){if(this.drag){const [a,b]=this.drag.view,m=this.margins(),dx=e.offsetX-this.drag.x,seconds=dx*(b-a)/Math.max(1,this.canvas.clientWidth-m.l-m.r);let start=a-seconds,end=b-seconds,low=history[0].timestamp,high=history[history.length-1].timestamp;if(start<low){end+=low-start;start=low}if(end>high){start-=end-high;end=high}this.view=[Math.max(low,start),Math.min(high,end)];}this.mouse={x:e.offsetX,y:e.offsetY};this.draw();}
  visible(){const [a,b]=this.bounds();return history.filter(p=>p.timestamp>=a&&p.timestamp<=b);}
  updateStats(points){let html='<span class="stats-head">Curve</span><span class="stats-head">Min</span><span class="stats-head">Mean</span><span class="stats-head">Max</span><span class="stats-head">Samples</span>',has=false;for(const s of this.series){const values=points.map(p=>p[s.key]).filter(finite);if(!values.length)continue;has=true;const mean=values.reduce((a,v)=>a+v,0)/values.length,d=s.decimals??2,u=s.unit?` ${s.unit}`:'';html+=`<span class="stats-series" style="color:${s.color}">${s.name}</span><span class="stats-number">${Math.min(...values).toFixed(d)}${u}</span><span class="stats-number">${mean.toFixed(d)}${u}</span><span class="stats-number">${Math.max(...values).toFixed(d)}${u}</span><span class="stats-number">${values.length}</span>`}this.stats.innerHTML=has?html:'<span class="stats-empty">No samples in visible range</span>'}
  draw(){const rect=this.canvas.getBoundingClientRect(),dpr=devicePixelRatio||1,w=Math.max(1,rect.width),h=Math.max(1,rect.height);if(this.canvas.width!==Math.round(w*dpr)||this.canvas.height!==Math.round(h*dpr)){this.canvas.width=Math.round(w*dpr);this.canvas.height=Math.round(h*dpr)}const c=this.ctx;c.setTransform(dpr,0,0,dpr,0,0);c.clearRect(0,0,w,h);const m=this.margins(),pw=w-m.l-m.r,ph=h-m.t-m.b,pts=this.visible(),[ta,tb]=this.bounds();
    this.updateStats(pts);const values=[];for(const p of pts)for(const s of this.series)if(finite(p[s.key]))values.push(p[s.key]);let ymin=values.length?Math.min(...values):0,ymax=values.length?Math.max(...values):1;let pad=Math.max((ymax-ymin)*.12,Math.abs(ymax)*.01,this.yTitle.includes('°C')?.1:1);ymin-=pad;ymax+=pad;if(ymin===ymax)ymax=ymin+1;
    const xp=t=>m.l+(t-ta)/(tb-ta)*pw,yp=v=>m.t+(ymax-v)/(ymax-ymin)*ph;c.font='12px system-ui';c.lineWidth=1;c.textBaseline='middle';for(let i=0;i<=5;i++){const y=m.t+i*ph/5,val=ymax-i*(ymax-ymin)/5;c.strokeStyle='#2b3945';c.beginPath();c.moveTo(m.l,y);c.lineTo(w-m.r,y);c.stroke();c.fillStyle='#9da9b5';c.textAlign='right';c.fillText(val.toFixed(Math.abs(ymax-ymin)<10?2:0),m.l-9,y)}for(let i=0;i<=5;i++){const x=m.l+i*pw/5,t=ta+i*(tb-ta)/5;c.strokeStyle='#26333d';c.beginPath();c.moveTo(x,m.t);c.lineTo(x,h-m.b);c.stroke();c.fillStyle='#9da9b5';c.textAlign=i===0?'left':i===5?'right':'center';c.textBaseline='top';c.fillText(timeLabel(t),x,h-m.b+9)}
    c.save();c.translate(17,m.t+ph/2);c.rotate(-Math.PI/2);c.fillStyle='#c1ccd5';c.textAlign='center';c.textBaseline='middle';c.fillText(this.yTitle,0,0);c.restore();c.fillStyle='#c1ccd5';c.textAlign='center';c.textBaseline='bottom';c.fillText('Time',m.l+pw/2,h-2);
    let lx=m.l;for(const s of this.series){c.fillStyle=s.color;c.fillRect(lx,m.t-20,14,3);c.fillStyle='#cbd5dd';c.textAlign='left';c.textBaseline='middle';c.fillText(s.name,lx+20,m.t-18);lx+=c.measureText(s.name).width+48;c.strokeStyle=s.color;c.lineWidth=2;c.beginPath();let started=false;for(const p of pts){const v=p[s.key];if(!finite(v)){started=false;continue}const x=xp(p.timestamp),y=yp(v);started?c.lineTo(x,y):c.moveTo(x,y);started=true}c.stroke()}
    if(this.mouse&&pts.length){const target=ta+Math.max(0,Math.min(1,(this.mouse.x-m.l)/pw))*(tb-ta);let nearest=pts.reduce((a,p)=>Math.abs(p.timestamp-target)<Math.abs(a.timestamp-target)?p:a);const x=xp(nearest.timestamp);c.strokeStyle='#d8e1e888';c.lineWidth=1;c.beginPath();c.moveTo(x,m.t);c.lineTo(x,h-m.b);c.stroke();let lines=[timeLabel(nearest.timestamp)];for(const s of this.series)if(finite(nearest[s.key])){const y=yp(nearest[s.key]);c.fillStyle=s.color;c.beginPath();c.arc(x,y,4,0,Math.PI*2);c.fill();lines.push(`${s.name}: ${nearest[s.key].toFixed(s.decimals??2)} ${s.unit||''}`)}this.tip.innerHTML=lines.join('<br>');this.tip.style.display='block';const tw=this.tip.offsetWidth,th=this.tip.offsetHeight;this.tip.style.left=Math.max(4,Math.min(w-tw-4,x+12))+'px';this.tip.style.top=Math.max(4,Math.min(h-th-4,this.mouse.y-th/2))+'px';}
  }
}
const charts={
 cpuTemperature:new InteractiveChart('cpu-temperature-chart','Temperature (°C)',[{key:'cpu_tctl_c',name:'CPU Tctl',color:'#ff6b6b',unit:'°C',decimals:2},{key:'ccd0_temp_c',name:'CCD0 / Tccd1',color:'#4fc3f7',unit:'°C',decimals:2},{key:'ccd1_temp_c',name:'CCD1 / Tccd2',color:'#ab87ff',unit:'°C',decimals:2},{key:'temperature_external_c',name:'Water after block',color:'#66d18f',unit:'°C',decimals:2}]),
 power:new InteractiveChart('power-chart','Power (W)',[{key:'cpu_package_power_w',name:'CPU package measured',color:'#ffb74d',unit:'W',decimals:2},{key:'ccd0_estimated_power_w',name:'CCD0 estimated dynamic Δ',color:'#4fc3f7',unit:'W',decimals:2},{key:'ccd1_estimated_power_w',name:'CCD1 estimated dynamic Δ',color:'#ab87ff',unit:'W',decimals:2}]),
 loads:new InteractiveChart('loads-chart','CPU load (%)',[{key:'ccd0_load_pct',name:'CCD0',color:'#4fc3f7',unit:'%',decimals:1},{key:'ccd1_load_pct',name:'CCD1',color:'#ab87ff',unit:'%',decimals:1}]),
 cooling:new InteractiveChart('cooling-chart','Tachometer speed (RPM)',[{key:'pump_rpm',name:'Pump',color:'#ffb74d',unit:'RPM',decimals:0},{key:'top_radiator_rpm',name:'Top radiator',color:'#4fc3f7',unit:'RPM',decimals:0},{key:'bottom_radiator_rpm',name:'Bottom radiator',color:'#66d18f',unit:'RPM',decimals:0}]),
 pressure:new InteractiveChart('pressure-chart','Pressure drop (mbar)',[{key:'pressure_calibrated_mbar',name:'Pressure drop (Aqua curve)',color:'#4fc3f7',unit:'mbar',decimals:1}]),
 temperature:new InteractiveChart('temperature-chart','Temperature (°C)',[{key:'temperature_external_c',name:'Water after block',color:'#ffb74d',unit:'°C',decimals:2},{key:'temperature_internal_c',name:'MPS electronics',color:'#ab87ff',unit:'°C',decimals:2}])};
document.querySelectorAll('.controls').forEach(group=>{const chart=charts[group.dataset.chart];group.querySelectorAll('[data-range]').forEach(btn=>btn.onclick=()=>{group.querySelectorAll('[data-range]').forEach(b=>b.classList.remove('active'));btn.classList.add('active');chart.setRange(btn.dataset.range==='all'?'all':Number(btn.dataset.range));});group.querySelector('[data-reset]').onclick=()=>chart.reset();group.querySelector('[data-range="300"]').classList.add('active');});
function addReading(v){if(!v||!finite(v.timestamp)||history.some(p=>p.timestamp===v.timestamp))return;history.push(v);history.sort((a,b)=>a.timestamp-b.timestamp);if(history.length>3600)history.splice(0,history.length-3600);}
function drawAll(){Object.values(charts).forEach(c=>c.draw())}
async function loadHistory(){try{const r=await fetch('/api/history');if(!r.ok)throw Error(`History: HTTP ${r.status}`);for(const v of await r.json())addReading(v);drawAll()}catch(e){$('error').textContent=e}}
async function postJSON(path,payload){const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}),d=await r.json();if(!r.ok)throw Error(d.error||`HTTP ${r.status}`);return d}
const coolingInput={pump:'pump-duty',top_radiator:'top-radiator-duty',bottom_radiator:'bottom-radiator-duty'};
async function setCooling(channel){try{const duty=Number($(coolingInput[channel]).value);const d=await postJSON('/api/control',{channel,action:'set',duty});$('control-result').textContent=`${d.channel} setting applied`;}catch(e){$('control-result').textContent=e}}
async function restoreCooling(channel){try{const d=await postJSON('/api/control',{channel,action:'restore'});$('control-result').textContent=`${d.channel} startup setting restored`;}catch(e){$('control-result').textContent=e}}
function updateWorkloadSplit(){const share=Math.max(0,Math.min(100,Number($('ccd0-workload-share').value)));$('ccd1-workload-share').value=100-share}
$('ccd0-workload-share').addEventListener('input',updateWorkloadSplit);updateWorkloadSplit();
function experimentPlan(dryRun){return {label:$('run-label').value,block_notes:$('block-notes').value,package_power_min_w:Number($('package-power-min').value),package_power_max_w:Number($('package-power-max').value),package_power_step_w:Number($('package-power-step').value),ccd0_workload_share_pct:Number($('ccd0-workload-share').value),power_tolerance_w:Number($('power-tolerance').value),pump_duty_pct:Number($('run-pump').value),top_radiator_duty_pct:Number($('run-top-radiator').value),bottom_radiator_duty_pct:Number($('run-bottom-radiator').value),min_stability_sec:Number($('stability-sec').value),window_sec:Number($('window-sec').value),stage_max_sec:Number($('stage-max-sec').value),cutoff_c:Number($('cutoff-c').value),dry_run:dryRun}}
async function startExperiment(dryRun){try{if(!dryRun&&!$('run-confirm').checked)throw Error('Confirm pump operation and test authorization first.');const d=await postJSON('/api/experiment/start',experimentPlan(dryRun));$('experiment-status').textContent=JSON.stringify(d,null,2)}catch(e){$('experiment-status').textContent=String(e)}}
async function abortExperiment(){try{$('experiment-status').textContent=JSON.stringify(await postJSON('/api/experiment/abort',{}),null,2)}catch(e){$('experiment-status').textContent=String(e)}}
async function updateExperiment(){try{const r=await fetch('/api/experiment'),d=await r.json();$('experiment-status').textContent=JSON.stringify(d,null,2)}catch(e){}}
function refreshResultPlots(){const stamp=Date.now();for(const [id,name] of [['comparison-temp','comparison_temp_vs_power.png'],['comparison-delta','comparison_delta_t_vs_power.png'],['comparison-rth','comparison_thermal_resistance.png'],['comparison-metrics','comparison_key_metrics.png']]){const img=$(id);img.style.display='block';img.onload=()=>img.style.display='block';img.onerror=()=>img.style.display='none';img.src=`/plots/${name}?t=${stamp}`}}
async function tick(){try{const r=await fetch('/api/live'),d=await r.json();$('error').textContent=d.error||'';if(!d.latest||!d.latest.timestamp)return;const v=d.latest,fmt=(x,n,u)=>x==null?'N/A':Number(x).toFixed(n)+' '+u,fan=(rpm,duty)=>`${rpm==null?'N/A':Number(rpm).toFixed(0)+' RPM'} / ${duty==null?'N/A':Number(duty).toFixed(0)+'%'}`;$('pc').textContent=v.pressure_calibrated_mbar==null?'N/A':v.pressure_calibrated_mbar.toFixed(1)+' mbar';$('pc-device').textContent=v.pressure_candidate_mbar==null?'N/A':v.pressure_candidate_mbar.toFixed(1)+' mbar?';$('te').textContent=fmt(v.temperature_external_c,2,'°C');$('ti').textContent=fmt(v.temperature_internal_c,2,'°C');$('cpu-power').textContent=fmt(v.cpu_package_power_w,1,'W');$('ccd0-power').textContent=fmt(v.ccd0_estimated_power_w,1,'W');$('ccd1-power').textContent=fmt(v.ccd1_estimated_power_w,1,'W');$('cpu-tctl').textContent=fmt(v.cpu_tctl_c,2,'°C');$('ccd0-temp').textContent=fmt(v.ccd0_temp_c,2,'°C');$('ccd1-temp').textContent=fmt(v.ccd1_temp_c,2,'°C');$('ccd0-load').textContent=fmt(v.ccd0_load_pct,1,'%');$('ccd1-load').textContent=fmt(v.ccd1_load_pct,1,'%');$('pump-live').textContent=fan(v.pump_rpm,v.pump_duty_pct);$('top-radiator-live').textContent=fan(v.top_radiator_rpm,v.top_radiator_duty_pct);$('bottom-radiator-live').textContent=fan(v.bottom_radiator_rpm,v.bottom_radiator_duty_pct);addReading(v);drawAll();updateExperiment()}catch(e){$('error').textContent=e}}loadHistory().then(tick);refreshResultPlots();setInterval(tick,1000);setInterval(refreshResultPlots,15000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    state: SharedState
    coolercontrol: CoolerControlClient
    cooling_defaults: dict[str, dict[str, Any]]
    experiment: ExperimentManager

    def send_body(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, status: int, value: Any) -> None:
        self.send_body(status, "application/json", json.dumps(value).encode())

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if not 0 < length <= 65536:
            raise ValueError("request body must be from 1 byte through 64 KiB")
        value = json.loads(self.rfile.read(length))
        if not isinstance(value, dict):
            raise ValueError("JSON body must be an object")
        return value

    def valid_control_origin(self) -> bool:
        origin = self.headers.get("Origin")
        if origin is None:
            return True
        port = self.server.server_address[1]
        return origin in {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/":
            self.send_body(200, "text/html; charset=utf-8", HTML.encode())
            return
        plot_name = self.path.split("?", 1)[0].removeprefix("/plots/")
        if self.path.startswith("/plots/") and plot_name in {
            "comparison_temp_vs_power.png",
            "comparison_thermal_resistance.png",
        }:
            try:
                self.send_body(200, "image/png", (COMPARISONS_DIR / plot_name).read_bytes())
            except FileNotFoundError:
                self.send_body(404, "text/plain", b"plot available after first stable stage\n")
            return
        with self.state.lock:
            latest = dict(self.state.latest)
            config = dict(self.state.config)
            error = self.state.error
        if self.path == "/api/calibration":
            self.send_json(200, {
                "source": CALIBRATION_PATH.name,
                "input_field": "pressure_normalized_raw",
                "raw_values": CALIBRATION_RAW_VALUES,
                "mbar_values": CALIBRATION_MBAR_VALUES,
                "out_of_range": "clamp to endpoint values",
            })
            return
        if self.path == "/api/live":
            self.send_json(200, {"latest": latest, "config": config, "error": error})
            return
        if self.path == "/api/experiment":
            self.send_json(200, self.experiment.status())
            return
        if self.path == "/api/history":
            with self.state.lock:
                history = list(self.state.history)
            fields = (
                "timestamp", "pressure_calibrated_mbar", "pressure_candidate_mbar",
                "pressure_calibration_input_raw", "pressure_normalized_raw",
                "pressure_raw", "pressure_offset_raw", "temperature_external_c",
                "temperature_internal_c", "cpu_tctl_c", "ccd0_temp_c",
                "ccd1_temp_c", "x3d_ccd_temp_c", "cpu_package_power_w",
                "ccd0_attributed_power_w", "ccd1_attributed_power_w",
                "ccd0_estimated_power_w", "ccd1_estimated_power_w",
                "ccd_power_idle_baseline_w",
                "cpu_load_pct", "ccd0_load_pct", "ccd1_load_pct",
                "ccd0_frequency_mhz", "ccd1_frequency_mhz", "pump_rpm",
                "pump_duty_pct", "top_radiator_rpm", "top_radiator_duty_pct",
                "bottom_radiator_rpm", "bottom_radiator_duty_pct",
            )
            compact = [{key: reading.get(key) for key in fields} for reading in history]
            self.send_json(200, compact)
            return
        if self.path == "/metrics":
            names = (
                "pressure_raw", "pressure_offset_raw", "pressure_normalized_raw",
                "field_0x23_raw", "field_0x25_raw", "pressure_candidate_mbar",
                "pressure_calibration_input_raw", "pressure_calibrated_mbar",
                "temperature_external_c", "temperature_internal_c", "cpu_tctl_c",
                "ccd0_temp_c", "ccd1_temp_c", "x3d_ccd_temp_c",
                "cpu_package_power_w", "cpu_load_pct", "ccd0_load_pct",
                "ccd0_attributed_power_w", "ccd1_attributed_power_w",
                "ccd0_estimated_power_w", "ccd1_estimated_power_w",
                "ccd_power_idle_baseline_w",
                "ccd1_load_pct", "ccd0_frequency_mhz", "ccd1_frequency_mhz",
                "pump_rpm", "pump_duty_pct", "top_radiator_rpm",
                "top_radiator_duty_pct", "bottom_radiator_rpm",
                "bottom_radiator_duty_pct",
            )
            lines = [f"mps_{name} {latest[name]}" for name in names if latest.get(name) is not None]
            lines.extend(
                f'mps_report_word{{offset="{name.removeprefix("word_")}"}} {value}'
                for name, value in latest.get("words_le_u16", {}).items()
            )
            self.send_body(200, "text/plain; version=0.0.4", ("\n".join(lines) + "\n").encode())
            return
        self.send_body(404, "text/plain", b"not found\n")

    def do_POST(self) -> None:  # noqa: N802
        if not self.valid_control_origin():
            self.send_json(403, {"error": "control request origin rejected"})
            return
        try:
            payload = self.read_json()
            if self.path == "/api/control":
                if self.experiment.status().get("state") in {"starting", "running", "aborting"}:
                    raise RuntimeError("manual control is locked while an experiment is active")
                channel_name = payload.get("channel")
                channel = {
                    "pump": PUMP_CHANNEL,
                    "top_radiator": TOP_RADIATOR_CHANNEL,
                    "bottom_radiator": BOTTOM_RADIATOR_CHANNEL,
                }.get(channel_name)
                if channel is None:
                    raise ValueError(
                        "channel must be pump, top_radiator, or bottom_radiator"
                    )
                action = payload.get("action", "set")
                if action == "set":
                    duty = payload.get("duty")
                    if isinstance(duty, bool) or not isinstance(duty, int):
                        raise ValueError("duty must be an integer percentage")
                    self.coolercontrol.set_fixed_speed(channel, duty)
                elif action == "restore":
                    setting = self.cooling_defaults.get(channel)
                    if setting is None:
                        raise RuntimeError(f"no captured default for {channel_name}")
                    self.coolercontrol.apply_setting(setting)
                else:
                    raise ValueError("action must be set or restore")
                self.send_json(200, {"ok": True, "channel": channel_name, "action": action})
                return
            if self.path == "/api/experiment/start":
                self.send_json(202, self.experiment.start(payload))
                return
            if self.path == "/api/experiment/abort":
                self.send_json(202, self.experiment.abort())
                return
            self.send_json(404, {"error": "not found"})
        except (ValueError, json.JSONDecodeError) as exc:
            self.send_json(400, {"error": str(exc)})
        except RuntimeError as exc:
            self.send_json(409, {"error": str(exc)})
        except Exception as exc:
            self.send_json(500, {"error": f"{type(exc).__name__}: {exc}"})

    def log_message(self, format: str, *args: Any) -> None:
        return


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", default=os.environ.get("MPS_DEVICE", DEFAULT_DEVICE))
    parser.add_argument("--listen", default=os.environ.get("MPS_LISTEN", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("MPS_PORT", "18080")))
    parser.add_argument("--interval", type=float, default=float(os.environ.get("MPS_INTERVAL", "1.0")))
    parser.add_argument(
        "--csv",
        type=Path,
        default=Path(
            os.environ.get(
                "MPS_CSV_PATH",
                str(Path.home() / ".local" / "state" / "mps-pressure-dashboard" / "readings.csv"),
            )
        ).expanduser(),
    )
    parser.add_argument("--decode-file", type=Path, help="decode a captured binary report and exit")
    args = parser.parse_args()
    if args.decode_file:
        print(json.dumps(decode_report(args.decode_file.read_bytes()), indent=2))
        return

    state = SharedState()
    device = MPSDevice(args.device)
    coolercontrol = CoolerControlClient()
    system_telemetry = SystemTelemetry(coolercontrol)
    Handler.coolercontrol = coolercontrol
    try:
        Handler.cooling_defaults = capture_cooling_settings(coolercontrol)
    except Exception:
        Handler.cooling_defaults = {}
    def latest_reading() -> dict[str, Any]:
        with state.lock:
            return dict(state.latest)
    Handler.experiment = ExperimentManager(latest_reading, coolercontrol)
    Handler.state = state
    threading.Thread(
        target=sample_loop,
        args=(
            device, system_telemetry, state, args.interval, args.csv,
            Handler.experiment.status,
        ),
        daemon=True,
    ).start()
    server = ThreadingHTTPServer((args.listen, args.port), Handler)
    print(f"MPS dashboard: http://{args.listen}:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
