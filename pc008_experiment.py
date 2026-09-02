#!/usr/bin/env python3
"""Guarded per-CCD thermal experiment runner for PC 008."""

from __future__ import annotations

import csv
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from pc008_control import (
    CCD0_CPUS,
    CCD1_CPUS,
    BOTTOM_RADIATOR_CHANNEL,
    PUMP_CHANNEL,
    TOP_RADIATOR_CHANNEL,
    CoolerControlClient,
    capture_cooling_settings,
    restore_cooling_settings,
)


DATA_DIR = Path(
    os.environ.get(
        "MPS_DATA_DIR",
        str(Path.home() / ".local" / "share" / "mps-pressure-dashboard"),
    )
).expanduser()
RUNS_DIR = Path(
    os.environ.get("MPS_RUNS_DIR", str(DATA_DIR / "runs"))
).expanduser()
COMPARISONS_DIR = Path(
    os.environ.get("MPS_COMPARISONS_DIR", str(DATA_DIR / "comparisons"))
).expanduser()
PLOT_PYTHON = Path(
    os.environ.get("MPS_PLOT_PYTHON", sys.executable)
).expanduser()
WORKLOAD_PARENT_SERVICE = os.environ.get(
    "MPS_SERVICE_NAME", "mps-pressure-dashboard.service"
).strip() or "mps-pressure-dashboard.service"
if not WORKLOAD_PARENT_SERVICE.endswith(".service"):
    WORKLOAD_PARENT_SERVICE += ".service"
WORKLOAD_UNITS = {
    0: "pc008-ccd0-workload.service",
    1: "pc008-ccd1-workload.service",
}
WORKLOAD_QUOTA_PERIOD = "20ms"
CONTROL_INTERVAL_SEC = 5.0
CONTROL_SMOOTH_SAMPLES = 5
CONTROL_DEADBAND_W = 0.25
# PC008's package-power response has a tens-of-seconds boost/thermal lag,
# especially when only one CCD is active.  Faster gains chase normal telemetry
# and desktop-load excursions, producing a large limit cycle instead of a
# steady thermal condition.  Keep this loop deliberately slower than the
# thermal plant; reaching the setpoint takes longer but the measurement window
# is then made at a nearly fixed workload quota.
PI_KP = 0.02
PI_KI_PER_SEC = 0.008
PI_MAX_SLEW_LOAD = 1.0
PI_MIN_SLEW_LOAD = 0.05
SAMPLE_FIELDS = (
    "timestamp_iso", "elapsed_s", "stage_index", "package_power_target_w",
    "ccd0_workload_share_pct", "ccd1_workload_share_pct",
    "ccd0_applied_load_pct", "ccd1_applied_load_pct",
    "idle_package_power_w", "power_error_w",
    "cpu_package_power_w", "cpu_tctl_c",
    "ccd0_temp_c", "ccd1_temp_c", "water_outlet_temp_c",
    "mps_internal_temp_c", "pressure_drop_mbar", "cpu_load_pct",
    "ccd0_load_pct", "ccd1_load_pct", "ccd0_frequency_mhz",
    "ccd1_frequency_mhz", "pump_rpm", "pump_duty_pct", "top_radiator_rpm",
    "top_radiator_duty_pct", "bottom_radiator_rpm",
    "bottom_radiator_duty_pct", "state",
)
SUMMARY_FIELDS = (
    "stage_index", "result", "elapsed_s", "package_power_target_w",
    "ccd0_workload_share_pct", "ccd1_workload_share_pct",
    "ccd0_applied_load_mean_pct", "ccd1_applied_load_mean_pct",
    "idle_package_power_w", "power_error_mean_w", "sample_count",
    "cpu_package_power_mean_w",
    "cpu_package_power_min_w", "cpu_package_power_max_w", "cpu_tctl_mean_c",
    "cpu_tctl_min_c", "cpu_tctl_max_c", "cpu_tctl_peak_c",
    "ccd0_temp_mean_c", "ccd0_temp_min_c", "ccd0_temp_max_c",
    "ccd0_temp_peak_c", "ccd1_temp_mean_c", "ccd1_temp_min_c",
    "ccd1_temp_max_c", "ccd1_temp_peak_c",
    "ccd1_minus_ccd0_mean_c", "ccd_delta_abs_mean_c",
    "water_outlet_temp_mean_c",
    "water_outlet_temp_min_c", "water_outlet_temp_max_c",
    "pressure_drop_mean_mbar", "pump_rpm_mean", "pump_duty_mean_pct",
    "top_radiator_rpm_mean", "top_radiator_duty_mean_pct",
    "bottom_radiator_rpm_mean", "bottom_radiator_duty_mean_pct",
    "ccd0_apparent_rth_c_per_w",
    "ccd1_apparent_rth_c_per_w", "window_duration_s",
)


def _mean(rows: list[dict[str, Any]], key: str) -> float | None:
    values = [float(row[key]) for row in rows if isinstance(row.get(key), (int, float))]
    return sum(values) / len(values) if values else None


def _minimum(rows: list[dict[str, Any]], key: str) -> float | None:
    values = [float(row[key]) for row in rows if isinstance(row.get(key), (int, float))]
    return min(values) if values else None


def _maximum(rows: list[dict[str, Any]], key: str) -> float | None:
    values = [float(row[key]) for row in rows if isinstance(row.get(key), (int, float))]
    return max(values) if values else None


def _round(value: float | None, digits: int = 3) -> float | None:
    return None if value is None or not math.isfinite(value) else round(value, digits)


class ThermalLimitError(RuntimeError):
    """Requested power cannot be reached within the configured temperature limit."""


def validate_plan(raw: dict[str, Any]) -> dict[str, Any]:
    label = str(raw.get("label", "bykski_stock_base")).strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,47}", label):
        raise ValueError("label must use 1-48 letters, numbers, dot, underscore, or dash")

    def bounded_int(name: str, default: int, low: int, high: int) -> int:
        value = raw.get(name, default)
        if not isinstance(value, int) or not low <= value <= high:
            raise ValueError(f"{name} must be an integer from {low} through {high}")
        return value

    power_min = bounded_int("package_power_min_w", 60, 35, 230)
    power_max = bounded_int("package_power_max_w", 150, 35, 230)
    power_step = bounded_int("package_power_step_w", 15, 1, 100)
    ccd0_share = bounded_int("ccd0_workload_share_pct", 50, 0, 100)
    if power_max < power_min:
        raise ValueError("package_power_max_w must be at least package_power_min_w")
    targets = list(range(power_min, power_max + 1, power_step))
    if targets[-1] != power_max:
        targets.append(power_max)
    if len(targets) > 16:
        raise ValueError("power sweep must contain no more than 16 stages")

    pump = bounded_int("pump_duty_pct", 100, 60, 100)
    legacy_radiator = raw.get("radiator_duty_pct", 100)
    top_radiator = bounded_int(
        "top_radiator_duty_pct", legacy_radiator, 20, 100
    )
    bottom_radiator = bounded_int(
        "bottom_radiator_duty_pct", legacy_radiator, 20, 100
    )
    minimum_stability = bounded_int("min_stability_sec", 180, 60, 900)
    window = bounded_int("window_sec", 120, 30, 600)
    stage_max = bounded_int("stage_max_sec", 900, 90, 1800)
    cutoff = bounded_int("cutoff_c", 95, 70, 95)
    power_tolerance = raw.get("power_tolerance_w", 3.0)
    if (
        not isinstance(power_tolerance, (int, float))
        or isinstance(power_tolerance, bool)
        or not 1.0 <= power_tolerance <= 10.0
    ):
        raise ValueError("power_tolerance_w must be from 1 through 10")
    ambient = raw.get("ambient_c", 23.0)
    if (
        not isinstance(ambient, (int, float))
        or isinstance(ambient, bool)
        or not 0.0 <= ambient <= 50.0
    ):
        raise ValueError("ambient_c must be from 0 through 50")
    if window > minimum_stability or stage_max <= minimum_stability:
        raise ValueError("window must not exceed stability minimum; stage maximum must exceed it")
    max_temp_range = raw.get("max_temp_range_c", 4.0)
    max_water_range = raw.get("max_water_range_c", 0.3)
    if not isinstance(max_temp_range, (int, float)) or isinstance(max_temp_range, bool) or not 0.1 <= max_temp_range <= 6.0:
        raise ValueError("max_temp_range_c must be from 0.1 through 6.0")
    if not isinstance(max_water_range, (int, float)) or isinstance(max_water_range, bool) or not 0.05 <= max_water_range <= 1.0:
        raise ValueError("max_water_range_c must be from 0.05 through 1.0")
    return {
        "label": label,
        "block_notes": str(raw.get("block_notes", "Bykski AMD water block; stock copper base"))[:500],
        "package_power_min_w": power_min,
        "package_power_max_w": power_max,
        "package_power_step_w": power_step,
        "package_power_targets_w": targets,
        "ccd0_workload_share_pct": ccd0_share,
        "ccd1_workload_share_pct": 100 - ccd0_share,
        "pump_duty_pct": pump,
        "top_radiator_duty_pct": top_radiator,
        "bottom_radiator_duty_pct": bottom_radiator,
        "min_stability_sec": minimum_stability,
        "window_sec": window,
        "stage_max_sec": stage_max,
        "cutoff_c": cutoff,
        "ambient_c": float(ambient),
        "power_tolerance_w": float(power_tolerance),
        "max_temp_range_c": float(max_temp_range),
        "max_water_range_c": float(max_water_range),
        "dry_run": bool(raw.get("dry_run", True)),
    }


class ExperimentManager:
    def __init__(
        self,
        latest_reading: Callable[[], dict[str, Any]],
        client: CoolerControlClient | None = None,
    ):
        self.latest_reading = latest_reading
        self.client = client or CoolerControlClient()
        self.lock = threading.Lock()
        self.abort_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.workload_lock = threading.Lock()
        self.workload_loads: dict[int, float] = {}
        self.workload_workers: dict[int, int] = {}
        self.info: dict[str, Any] = {
            "state": "idle", "message": "No experiment running", "run_dir": None
        }

    def status(self) -> dict[str, Any]:
        with self.lock:
            return dict(self.info)

    def _update(self, **values: Any) -> None:
        with self.lock:
            self.info.update(values)

    def start(self, raw_plan: dict[str, Any]) -> dict[str, Any]:
        plan = validate_plan(raw_plan)
        with self.lock:
            if self.thread is not None and self.thread.is_alive():
                raise RuntimeError("an experiment is already running")
            if plan["dry_run"]:
                self.info = {
                    "state": "dry-run validated",
                    "message": "Plan validated; no controls or workload changed",
                    "plan": plan,
                    "run_dir": None,
                }
                return dict(self.info)
            self.abort_event.clear()
            self.info = {"state": "starting", "message": "Capturing cooling settings", "plan": plan}
            self.thread = threading.Thread(target=self._run, args=(plan,), daemon=True)
            self.thread.start()
            return dict(self.info)

    def abort(self) -> dict[str, Any]:
        with self.lock:
            if self.thread is None or not self.thread.is_alive():
                return dict(self.info)
        self.abort_event.set()
        self._stop_workloads()
        self._update(state="aborting", message="Abort requested; stopping workloads")
        return self.status()

    @staticmethod
    def _cpu_list(cpus: tuple[int, ...]) -> str:
        return ",".join(str(cpu) for cpu in cpus)

    @staticmethod
    def _run_systemctl(
        *arguments: str, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["systemctl", "--user", *arguments], capture_output=True,
            text=True, check=check, timeout=10,
        )

    @staticmethod
    def _quota_percent(load: float) -> str:
        """Convert per-CCD load percent to quota across eight workers."""
        return f"{max(5.0, load * 8):.2f}%"

    @staticmethod
    def _worker_count(load: float) -> int:
        """Keep the spatial workload fixed; vary only the cgroup CPU quota.

        Changing the worker count at 12.5% load boundaries restarts the
        transient service, momentarily removes the load and makes the PI
        controller wind up.  Eight pinned workers keep the exercised cores
        and workload topology constant across every stage and adjustment.
        """
        return 8 if load > 0 else 0

    def _start_workload(self, ccd: int, cpus: tuple[int, ...], load: float) -> None:
        if load <= 0:
            return
        unit = WORKLOAD_UNITS[ccd]
        workers = self._worker_count(load)
        result = subprocess.run(
            [
                "systemd-run", "--user", f"--unit={unit}", "--collect",
                f"--property=CPUQuota={self._quota_percent(load)}",
                f"--property=CPUQuotaPeriodSec={WORKLOAD_QUOTA_PERIOD}",
                f"--property=CPUAffinity={self._cpu_list(cpus)}",
                f"--property=PartOf={WORKLOAD_PARENT_SERVICE}",
                "--property=KillMode=control-group",
                "stress-ng", "--cpu", str(workers), "--cpu-load", "100",
                "--cpu-method", "matrixprod", "--quiet",
            ],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode:
            raise RuntimeError(
                f"could not start CCD{ccd} workload: {result.stderr.strip()}"
            )
        time.sleep(0.2)
        if self._run_systemctl(
            "is-active", "--quiet", unit, check=False
        ).returncode:
            raise RuntimeError(f"CCD{ccd} workload service exited during startup")
        self.workload_loads[ccd] = load
        self.workload_workers[ccd] = workers

    @staticmethod
    def _split_loads(total_load: float, ccd0_share: int) -> tuple[float, float]:
        """Convert total load points into per-CCD duty while preserving the split."""
        share0 = ccd0_share / 100
        share1 = 1 - share0
        maximum = min(100 / share for share in (share0, share1) if share > 0)
        total_load = max(0.0, min(maximum, total_load))
        load0 = min(100.0, round(total_load * share0, 2))
        load1 = min(100.0, round(total_load * share1, 2))
        # Linux CFS enforces a 1 ms minimum quota; at 20 ms this is 5% of one
        # CPU, equivalent to 0.625% across the eight-core CCD workload.
        if load0 > 0:
            load0 = max(0.625, load0)
        if load1 > 0:
            load1 = max(0.625, load1)
        return load0, load1

    @staticmethod
    def _max_total_load(ccd0_share: int) -> float:
        """Return the aggregate CCD load at which both CCDs saturate."""
        share0 = ccd0_share / 100
        share1 = 1 - share0
        maxima = [100 / share for share in (share0, share1) if share > 0]
        return min(maxima) if maxima else 200.0

    @staticmethod
    def _initial_total_load(target: float, idle_power: float) -> float:
        """Estimate aggregate CCD load from the measured idle-to-target delta.

        The first ~30 W of dynamic package power arrives steeply as cores
        leave idle and enter boost, so it is modelled with the original
        6 W/load rule.  Above that, measured PC008 package power scales at
        about 0.63 W per aggregate load point, not the old optimistic
        1.8 W/load.  This is still only a feed-forward estimate; the PI
        loop owns the final approach to the target.
        """
        dynamic_target = max(0.0, target - idle_power)
        if dynamic_target <= 30:
            return max(0.625, dynamic_target / 6.0)
        return 5.0 + (dynamic_target - 30.0) / 0.63

    @staticmethod
    def _control_correction(
        error: float, previous_error: float, interval_s: float, total_load: float
    ) -> float:
        """Velocity PI correction tuned for the configured control interval.

        ``error`` is positive when package power is below target and more load
        is needed.  The proportional term reacts to the change in the measured
        error; the integral term accumulates through the load itself and
        removes steady-state bias.  Slew limits keep large errors from
        overshooting while allowing small deliberate moves near the setpoint.
        """
        correction = PI_KP * (error - previous_error) + PI_KI_PER_SEC * error * interval_s
        magnitude = abs(error)
        # Per-adjustment slew limit.  At 5 s intervals this ramps hard for
        # large errors but takes small, deliberate steps near the target.
        limit = min(PI_MAX_SLEW_LOAD, max(PI_MIN_SLEW_LOAD, magnitude * 0.10))
        # Low-power Zen boost has a steep transition around one percent quota.
        if total_load < 5:
            limit = min(limit, 0.3)
        return max(-limit, min(limit, correction))

    @staticmethod
    def _temperature_guarded_correction(
        correction: float, hottest_c: float, cutoff_c: float
    ) -> float:
        """Slow positive acquisition as thermal headroom disappears."""
        if correction <= 0:
            return correction
        headroom = cutoff_c - hottest_c
        if headroom <= 5:
            return 0.0
        if headroom <= 8:
            return min(correction, 1.0)
        if headroom <= 12:
            return min(correction, 4.0)
        return correction

    def _set_workloads(self, load0: float, load1: float) -> None:
        with self.workload_lock:
            if self.abort_event.is_set():
                raise InterruptedError("abort requested")
            for ccd, cpus, load in ((0, CCD0_CPUS, load0), (1, CCD1_CPUS, load1)):
                if self.abort_event.is_set():
                    raise InterruptedError("abort requested")
                unit = WORKLOAD_UNITS[ccd]
                current = self.workload_loads.get(ccd)
                if load <= 0:
                    if current is not None:
                        self._run_systemctl("stop", unit, check=False)
                        self.workload_loads.pop(ccd, None)
                        self.workload_workers.pop(ccd, None)
                    continue
                if current is None:
                    self._start_workload(ccd, cpus, load)
                elif self.workload_workers.get(ccd) != self._worker_count(load):
                    self._run_systemctl("stop", unit, check=False)
                    self.workload_loads.pop(ccd, None)
                    self.workload_workers.pop(ccd, None)
                    self._start_workload(ccd, cpus, load)
                elif abs(current - load) >= 0.01:
                    result = self._run_systemctl(
                        "set-property", "--runtime", unit,
                        f"CPUQuota={self._quota_percent(load)}", check=False,
                    )
                    if result.returncode:
                        raise RuntimeError(
                            f"could not update CCD{ccd} workload quota: "
                            f"{result.stderr.strip()}"
                        )
                    self.workload_loads[ccd] = load

    def _workloads_alive(self) -> bool:
        return all(
            self._run_systemctl(
                "is-active", "--quiet", WORKLOAD_UNITS[ccd], check=False
            ).returncode == 0
            for ccd in self.workload_loads
        )

    def _stop_workloads(self) -> None:
        with self.workload_lock:
            for unit in WORKLOAD_UNITS.values():
                self._run_systemctl("stop", unit, check=False)
                self._run_systemctl("reset-failed", unit, check=False)
            self.workload_loads.clear()
            self.workload_workers.clear()

    def _idle_baseline(self, plan: dict[str, Any]) -> float:
        """Measure package idle power for initial controller load estimation."""
        values: list[float] = []
        self._stop_workloads()
        self._update(message="Measuring idle package-power baseline")
        for _ in range(6):
            if self.abort_event.wait(1.0):
                raise InterruptedError("abort requested")
            reading = self.latest_reading()
            if time.time() - float(reading.get("timestamp", 0)) > 4:
                raise RuntimeError("telemetry is stale during idle baseline")
            power = reading.get("cpu_package_power_w")
            temperatures = [reading.get(key) for key in ("cpu_tctl_c", "ccd0_temp_c", "ccd1_temp_c")]
            if not isinstance(power, (int, float)):
                raise RuntimeError("CPU package-power telemetry unavailable")
            if any(not isinstance(value, (int, float)) for value in temperatures):
                raise RuntimeError("CPU temperature telemetry unavailable")
            if max(temperatures) >= plan["cutoff_c"]:
                raise RuntimeError(f"temperature cutoff reached: {max(temperatures):.2f} C")
            if not isinstance(reading.get("pump_rpm"), (int, float)) or reading["pump_rpm"] < 1000:
                raise RuntimeError("pump RPM safety check failed")
            values.append(float(power))
        return sum(values) / len(values)

    @staticmethod
    def _sample(
        reading: dict[str, Any], start: float, stage: int, target: int,
        share0: int, load0: float, load1: float, idle_power: float,
    ) -> dict[str, Any]:
        package_power = reading.get("cpu_package_power_w")
        return {
            "timestamp_iso": datetime.now(timezone.utc).isoformat(),
            "elapsed_s": round(time.monotonic() - start, 2),
            "stage_index": stage,
            "package_power_target_w": target,
            "ccd0_workload_share_pct": share0,
            "ccd1_workload_share_pct": 100 - share0,
            "ccd0_applied_load_pct": load0,
            "ccd1_applied_load_pct": load1,
            "idle_package_power_w": round(idle_power, 3),
            "power_error_w": _round(target - float(package_power))
            if isinstance(package_power, (int, float)) else None,
            "cpu_package_power_w": package_power,
            "cpu_tctl_c": reading.get("cpu_tctl_c"),
            "ccd0_temp_c": reading.get("ccd0_temp_c"),
            "ccd1_temp_c": reading.get("ccd1_temp_c"),
            "water_outlet_temp_c": reading.get("temperature_external_c"),
            "mps_internal_temp_c": reading.get("temperature_internal_c"),
            "pressure_drop_mbar": reading.get("pressure_calibrated_mbar"),
            "cpu_load_pct": reading.get("cpu_load_pct"),
            "ccd0_load_pct": reading.get("ccd0_load_pct"),
            "ccd1_load_pct": reading.get("ccd1_load_pct"),
            "ccd0_frequency_mhz": reading.get("ccd0_frequency_mhz"),
            "ccd1_frequency_mhz": reading.get("ccd1_frequency_mhz"),
            "pump_rpm": reading.get("pump_rpm"),
            "pump_duty_pct": reading.get("pump_duty_pct"),
            "top_radiator_rpm": reading.get("top_radiator_rpm"),
            "top_radiator_duty_pct": reading.get("top_radiator_duty_pct"),
            "bottom_radiator_rpm": reading.get("bottom_radiator_rpm"),
            "bottom_radiator_duty_pct": reading.get("bottom_radiator_duty_pct"),
            "state": "load",
        }

    @staticmethod
    def _stable(rows: deque[dict[str, Any]], plan: dict[str, Any]) -> bool:
        if not rows or rows[-1]["elapsed_s"] < plan["min_stability_sec"]:
            return False
        cutoff = rows[-1]["elapsed_s"] - plan["window_sec"]
        window = [row for row in rows if row["elapsed_s"] >= cutoff]
        if len(window) < max(10, plan["window_sec"] // 2):
            return False
        for key in ("cpu_tctl_c", "ccd0_temp_c", "ccd1_temp_c"):
            values = [row[key] for row in window if isinstance(row.get(key), (int, float))]
            buckets = [
                sum(values[index:index + 10]) / 10
                for index in range(0, len(values), 10)
                if len(values[index:index + 10]) == 10
            ]
            if not buckets:
                return False
            bucket_mean = sum(buckets) / len(buckets)
            bucket_std = math.sqrt(
                sum((value - bucket_mean) ** 2 for value in buckets) / len(buckets)
            )
            if bucket_std > plan["max_temp_range_c"] / 2:
                return False
        water = [row["water_outlet_temp_c"] for row in window if isinstance(row.get("water_outlet_temp_c"), (int, float))]
        errors = [row["power_error_w"] for row in window if isinstance(row.get("power_error_w"), (int, float))]
        powers = [row["cpu_package_power_w"] for row in window if isinstance(row.get("cpu_package_power_w"), (int, float))]
        power_buckets = [
            sum(powers[index:index + 10]) / len(powers[index:index + 10])
            for index in range(0, len(powers), 10)
            if len(powers[index:index + 10]) == 10
        ]
        bucket_mean = sum(power_buckets) / len(power_buckets) if power_buckets else 0.0
        bucket_std = (
            math.sqrt(
                sum((value - bucket_mean) ** 2 for value in power_buckets)
                / len(power_buckets)
            )
            if power_buckets else math.inf
        )
        mean_error_limit = min(plan["power_tolerance_w"], 1.0)
        power_std_limit = max(3.0, plan["power_tolerance_w"] * 0.6)
        return (
            bool(water)
            and max(water) - min(water) <= plan["max_water_range_c"]
            and bool(errors)
            and abs(sum(errors) / len(errors)) <= mean_error_limit
            and bucket_std <= power_std_limit
        )

    @staticmethod
    def _summary(stage: int, result: str, rows: list[dict[str, Any]], plan: dict[str, Any]) -> dict[str, Any]:
        end = rows[-1]["elapsed_s"] if rows else 0
        window_rows = [row for row in rows if row["elapsed_s"] >= end - plan["window_sec"]]
        power = _mean(window_rows, "cpu_package_power_w")
        water = _mean(window_rows, "water_outlet_temp_c")
        ccd0 = _mean(window_rows, "ccd0_temp_c")
        ccd1 = _mean(window_rows, "ccd1_temp_c")
        ccd_deltas = [
            float(row["ccd1_temp_c"]) - float(row["ccd0_temp_c"])
            for row in window_rows
            if isinstance(row.get("ccd0_temp_c"), (int, float))
            and isinstance(row.get("ccd1_temp_c"), (int, float))
        ]
        share0 = rows[-1]["ccd0_workload_share_pct"] if rows else 0
        share1 = rows[-1]["ccd1_workload_share_pct"] if rows else 0
        rth0 = (ccd0 - water) / power if share0 > 0 and None not in (ccd0, water, power) and power > 0 else None
        rth1 = (ccd1 - water) / power if share1 > 0 and None not in (ccd1, water, power) and power > 0 else None
        return {
            "stage_index": stage, "result": result, "elapsed_s": end,
            "package_power_target_w": rows[-1]["package_power_target_w"] if rows else None,
            "ccd0_workload_share_pct": share0 if rows else None,
            "ccd1_workload_share_pct": share1 if rows else None,
            "ccd0_applied_load_mean_pct": _round(_mean(window_rows, "ccd0_applied_load_pct")),
            "ccd1_applied_load_mean_pct": _round(_mean(window_rows, "ccd1_applied_load_pct")),
            "idle_package_power_w": _round(_mean(window_rows, "idle_package_power_w")),
            "power_error_mean_w": _round(_mean(window_rows, "power_error_w")),
            "sample_count": len(rows),
            "cpu_package_power_mean_w": _round(power),
            "cpu_package_power_min_w": _round(_minimum(window_rows, "cpu_package_power_w")),
            "cpu_package_power_max_w": _round(_maximum(window_rows, "cpu_package_power_w")),
            "cpu_tctl_mean_c": _round(_mean(window_rows, "cpu_tctl_c")),
            "cpu_tctl_min_c": _round(_minimum(window_rows, "cpu_tctl_c")),
            "cpu_tctl_max_c": _round(_maximum(window_rows, "cpu_tctl_c")),
            "cpu_tctl_peak_c": _round(_maximum(rows, "cpu_tctl_c")),
            "ccd0_temp_mean_c": _round(ccd0),
            "ccd0_temp_min_c": _round(_minimum(window_rows, "ccd0_temp_c")),
            "ccd0_temp_max_c": _round(_maximum(window_rows, "ccd0_temp_c")),
            "ccd0_temp_peak_c": _round(_maximum(rows, "ccd0_temp_c")),
            "ccd1_temp_mean_c": _round(ccd1),
            "ccd1_temp_min_c": _round(_minimum(window_rows, "ccd1_temp_c")),
            "ccd1_temp_max_c": _round(_maximum(window_rows, "ccd1_temp_c")),
            "ccd1_temp_peak_c": _round(_maximum(rows, "ccd1_temp_c")),
            "ccd1_minus_ccd0_mean_c": _round(
                sum(ccd_deltas) / len(ccd_deltas) if ccd_deltas else None
            ),
            "ccd_delta_abs_mean_c": _round(
                sum(abs(value) for value in ccd_deltas) / len(ccd_deltas)
                if ccd_deltas else None
            ),
            "water_outlet_temp_mean_c": _round(water),
            "water_outlet_temp_min_c": _round(_minimum(window_rows, "water_outlet_temp_c")),
            "water_outlet_temp_max_c": _round(_maximum(window_rows, "water_outlet_temp_c")),
            "pressure_drop_mean_mbar": _round(_mean(window_rows, "pressure_drop_mbar")),
            "pump_rpm_mean": _round(_mean(window_rows, "pump_rpm")),
            "pump_duty_mean_pct": _round(_mean(window_rows, "pump_duty_pct")),
            "top_radiator_rpm_mean": _round(_mean(window_rows, "top_radiator_rpm")),
            "top_radiator_duty_mean_pct": _round(_mean(window_rows, "top_radiator_duty_pct")),
            "bottom_radiator_rpm_mean": _round(_mean(window_rows, "bottom_radiator_rpm")),
            "bottom_radiator_duty_mean_pct": _round(_mean(window_rows, "bottom_radiator_duty_pct")),
            "ccd0_apparent_rth_c_per_w": _round(rth0, 5),
            "ccd1_apparent_rth_c_per_w": _round(rth1, 5),
            "window_duration_s": plan["window_sec"],
        }

    def _run(self, plan: dict[str, Any]) -> None:
        saved: dict[str, dict[str, Any]] = {}
        run_dir: Path | None = None
        result_state = "completed"
        result_message = "All stages completed"
        try:
            saved = capture_cooling_settings(self.client)
            self.client.set_fixed_speed(PUMP_CHANNEL, plan["pump_duty_pct"])
            self.client.set_fixed_speed(
                TOP_RADIATOR_CHANNEL, plan["top_radiator_duty_pct"]
            )
            self.client.set_fixed_speed(
                BOTTOM_RADIATOR_CHANNEL, plan["bottom_radiator_duty_pct"]
            )
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            run_dir = RUNS_DIR / f"waterblock_{stamp}_{plan['label']}"
            run_dir.mkdir(parents=True, exist_ok=False)
            (run_dir / "run_info.json").write_text(
                json.dumps({"plan": plan, "saved_cooling_settings": saved}, indent=2),
                encoding="utf-8",
            )
            sample_path = run_dir / "samples.csv"
            summary_path = run_dir / "summary.csv"
            with sample_path.open("w", newline="", encoding="utf-8") as sample_handle, summary_path.open("w", newline="", encoding="utf-8") as summary_handle:
                sample_writer = csv.DictWriter(sample_handle, fieldnames=SAMPLE_FIELDS)
                summary_writer = csv.DictWriter(summary_handle, fieldnames=SUMMARY_FIELDS)
                sample_writer.writeheader(); summary_writer.writeheader()
                idle_power = self._idle_baseline(plan)
                if plan["package_power_min_w"] <= idle_power + 3:
                    raise RuntimeError(
                        f"minimum package-power target must exceed measured idle "
                        f"{idle_power:.1f} W by more than 3 W"
                    )
                share0 = plan["ccd0_workload_share_pct"]
                maximum_total_load = self._max_total_load(share0)
                previous_stage_load: float | None = None
                for stage, target in enumerate(plan["package_power_targets_w"], 1):
                    if self.abort_event.is_set():
                        raise InterruptedError("abort requested")
                    if previous_stage_load is None:
                        total_load = self._initial_total_load(target, idle_power)
                    else:
                        # Carry the previous stage's final load forward.  The
                        # old code restarted every stage from the feed-forward
                        # estimate, which dropped the load at each transition
                        # and forced the slow integrator to re-climb for most
                        # of the stage.
                        total_load = previous_stage_load
                    load0, load1 = self._split_loads(total_load, share0)
                    total_load = load0 + load1
                    self._update(
                        state="running",
                        message=(
                            f"Stage {stage}: package target {target} W, workload "
                            f"split {share0}/{100 - share0}%"
                        ),
                        stage=stage,
                        run_dir=str(run_dir),
                    )
                    self._set_workloads(load0, load1)
                    stage_start = time.monotonic()
                    last_adjust = stage_start
                    previous_error: float | None = None
                    thermal_guard_hits = 0
                    saturation_hits = 0
                    thermal_message = ""
                    rows: deque[dict[str, Any]] = deque(maxlen=plan["stage_max_sec"] + 30)
                    stage_result = "timeout"
                    while time.monotonic() - stage_start < plan["stage_max_sec"]:
                        if self.abort_event.wait(1.0):
                            raise InterruptedError("abort requested")
                        if self.workload_loads and not self._workloads_alive():
                            raise RuntimeError("stress-ng exited before stage completion")
                        reading = self.latest_reading()
                        if time.time() - float(reading.get("timestamp", 0)) > 4:
                            raise RuntimeError("telemetry is stale")
                        row = self._sample(
                            reading, stage_start, stage, target, share0,
                            load0, load1, idle_power,
                        )
                        package_power = row.get("cpu_package_power_w")
                        if (
                            not isinstance(package_power, (int, float))
                            or not math.isfinite(float(package_power))
                        ):
                            raise RuntimeError("CPU package-power telemetry unavailable")
                        temperatures = [row.get(key) for key in ("cpu_tctl_c", "ccd0_temp_c", "ccd1_temp_c")]
                        if any(not isinstance(value, (int, float)) for value in temperatures):
                            raise RuntimeError("CPU temperature telemetry unavailable")
                        rows.append(row); sample_writer.writerow(row); sample_handle.flush()
                        self._update(elapsed_s=row["elapsed_s"], latest=row)
                        hottest = max(temperatures)
                        if hottest >= plan["cutoff_c"]:
                            stage_result = "thermal-limit"
                            thermal_message = (
                                f"target {target} W thermally limited: hottest CPU "
                                f"sensor reached {hottest:.2f} C at "
                                f"{package_power:.1f} W; configured "
                                f"cutoff is {plan['cutoff_c']} C"
                            )
                            break
                        if not isinstance(row.get("pump_rpm"), (int, float)) or row["pump_rpm"] < 1000:
                            raise RuntimeError("pump RPM safety check failed")
                        if self._stable(rows, plan):
                            stage_result = "stable"; break
                        now = time.monotonic()
                        if (
                            now - last_adjust >= CONTROL_INTERVAL_SEC
                            and len(rows) >= CONTROL_SMOOTH_SAMPLES
                        ):
                            recent_rows = list(rows)
                            measured = _mean(
                                recent_rows[-CONTROL_SMOOTH_SAMPLES:],
                                "cpu_package_power_w",
                            )
                            if measured is not None:
                                error = target - measured
                                if previous_error is None:
                                    previous_error = error
                                # Correct continuously; the deadband is only
                                # 0.25 W so steady-state bias stays far below
                                # the old power_tolerance_w deadband.
                                if abs(error) > CONTROL_DEADBAND_W:
                                    correction = self._control_correction(
                                        error, previous_error,
                                        CONTROL_INTERVAL_SEC, total_load,
                                    )
                                    hot_rows = recent_rows[-10:]
                                    hot_values = [
                                        max(
                                            float(item["cpu_tctl_c"]),
                                            float(item["ccd0_temp_c"]),
                                            float(item["ccd1_temp_c"]),
                                        )
                                        for item in hot_rows
                                    ]
                                    rise_rate = (
                                        max(0.0, hot_values[-1] - hot_values[0])
                                        / max(
                                            1.0,
                                            float(hot_rows[-1]["elapsed_s"])
                                            - float(hot_rows[0]["elapsed_s"]),
                                        )
                                        if len(hot_rows) >= 2 else 0.0
                                    )
                                    predicted_hot = hottest + rise_rate * 10.0
                                    guarded = self._temperature_guarded_correction(
                                        correction,
                                        max(hottest, max(hot_values), predicted_hot),
                                        plan["cutoff_c"],
                                    )
                                    if correction > 0 and guarded <= 0:
                                        thermal_guard_hits += 1
                                        self._update(
                                            message=(
                                                f"Stage {stage}: thermal guard holding "
                                                f"load; {CONTROL_INTERVAL_SEC:.0f} s mean "
                                                f"{measured:.1f} W, hottest {hottest:.1f} C"
                                            )
                                        )
                                    else:
                                        thermal_guard_hits = 0
                                    correction = guarded
                                    if thermal_guard_hits >= 3:
                                        stage_result = "thermal-limit"
                                        thermal_message = (
                                            f"target {target} W thermally limited near "
                                            f"{measured:.1f} W: less than 5 C headroom "
                                            f"remained below the {plan['cutoff_c']} C cutoff"
                                        )
                                        break
                                    next_total = total_load + correction
                                    next0, next1 = self._split_loads(next_total, share0)
                                    if (next0, next1) != (load0, load1):
                                        saturation_hits = 0
                                        load0, load1 = next0, next1
                                        total_load = load0 + load1
                                        self._set_workloads(load0, load1)
                                        self._update(
                                            message=(
                                                f"Stage {stage}: package target {target} W, "
                                                f"{CONTROL_INTERVAL_SEC:.0f} s mean "
                                                f"{measured:.1f} W, feedback loads "
                                                f"CCD0/CCD1 {load0:.2f}/{load1:.2f}%"
                                            )
                                        )
                                    elif (
                                        correction > 0
                                        and total_load >= maximum_total_load - 0.01
                                    ):
                                        saturation_hits += 1
                                        if saturation_hits >= 3:
                                            raise RuntimeError(
                                                f"target {target} W unreachable: "
                                                "workload quotas saturated below target"
                                            )
                                previous_error = error
                                self._update(controlled_mean_power_w=round(measured, 3))
                            last_adjust = now
                    previous_stage_load = total_load
                    stage_rows = list(rows)
                    summary_writer.writerow(self._summary(stage, stage_result, stage_rows, plan)); summary_handle.flush()
                    if stage_result == "stable":
                        self._generate_plots(run_dir)
                    if stage_result == "thermal-limit":
                        raise ThermalLimitError(thermal_message)
                    if stage_result != "stable":
                        raise RuntimeError(f"stage {stage} did not reach steady state")
        except InterruptedError as exc:
            result_state, result_message = "aborted", str(exc)
        except ThermalLimitError as exc:
            result_state, result_message = "thermal-limit", str(exc)
        except Exception as exc:
            result_state, result_message = "error", f"{type(exc).__name__}: {exc}"
        finally:
            self._stop_workloads()
            restore_errors = restore_cooling_settings(self.client, saved) if saved else []
            if restore_errors:
                result_state = "error"
                result_message += "; cooling restoration failed: " + "; ".join(restore_errors)
            self._update(state=result_state, message=result_message, run_dir=str(run_dir) if run_dir else None)

    @staticmethod
    def _generate_plots(run_dir: Path) -> None:
        script = Path(__file__).with_name("plot_pc008_waterblock.py")
        result = subprocess.run(
            [
                str(PLOT_PYTHON), str(script), "--run-dir", str(run_dir),
                "--runs-dir", str(RUNS_DIR), "--comparison-dir",
                str(COMPARISONS_DIR),
            ],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode:
            raise RuntimeError(f"plot generation failed: {result.stderr.strip()}")
