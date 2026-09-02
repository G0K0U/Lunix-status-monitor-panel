#!/usr/bin/env python3
"""PC 008 CPU/cooling telemetry and constrained CoolerControl access."""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


def _env(name: str, default: str) -> str:
    """Read a non-empty configuration value from the environment."""
    value = os.environ.get(name, "").strip()
    return value or default


def _cpu_list(name: str, default: tuple[int, ...]) -> tuple[int, ...]:
    """Parse a comma-separated CPU list, retaining safe built-in defaults."""
    value = os.environ.get(name, "").strip()
    if not value:
        return default
    try:
        cpus = tuple(sorted({int(item.strip()) for item in value.split(",") if item.strip()}))
    except ValueError as exc:
        raise ValueError(f"{name} must be a comma-separated CPU list") from exc
    if not cpus or any(cpu < 0 for cpu in cpus):
        raise ValueError(f"{name} must contain non-negative CPU numbers")
    return cpus


# Defaults match PC008; every hardware-specific value can be overridden in
# ~/.config/mps-pressure-dashboard.env before starting the user service.
COOLERCONTROL_URL = _env("COOLERCONTROL_URL", "http://localhost:11987")
NCT6687_UID = _env(
    "COOLERCONTROL_COOLING_UID",
    "7bfee7e15e0af819a1c74ac2f088d69197bdae5f361d2af4da2960c931be3cc7",
)
CPU_UID = _env(
    "COOLERCONTROL_CPU_UID",
    "c1c4f573af5adb37f4b2b21c38e7ab00131dec4e073a10af87799b5e930fee88",
)
PUMP_CHANNEL = _env("MPS_PUMP_CHANNEL", "fan2")
TOP_RADIATOR_CHANNEL = _env("MPS_TOP_RADIATOR_CHANNEL", "fan4")
BOTTOM_RADIATOR_CHANNEL = _env("MPS_BOTTOM_RADIATOR_CHANNEL", "fan5")
RADIATOR_CHANNELS = (TOP_RADIATOR_CHANNEL, BOTTOM_RADIATOR_CHANNEL)
CCD0_CPUS = _cpu_list("MPS_CCD0_CPUS", tuple(range(0, 8)) + tuple(range(16, 24)))
CCD1_CPUS = _cpu_list("MPS_CCD1_CPUS", tuple(range(8, 16)) + tuple(range(24, 32)))


class CoolerControlError(RuntimeError):
    pass


class CoolerControlClient:
    """Use a bearer token when configured, otherwise reuse the local UI session."""

    def __init__(self, base_url: str = COOLERCONTROL_URL):
        self.base_url = base_url.rstrip("/")

    @staticmethod
    def _headers() -> dict[str, str]:
        token = os.environ.get("COOLERCONTROL_TOKEN")
        if token:
            return {"Authorization": f"Bearer {token}"}
        config = Path.home() / ".config/org.coolercontrol.CoolerControl/CoolerControl.conf"
        try:
            text = config.read_text(encoding="utf-8")
        except OSError as exc:
            raise CoolerControlError(f"CoolerControl credentials unavailable: {exc}") from exc
        match = re.search(r"networkCookies=.*?cc=([^;\r\n]+)", text)
        if not match:
            raise CoolerControlError("CoolerControl session cookie not found")
        return {"Cookie": f"cc={match.group(1)}"}

    def request(self, path: str, method: str = "GET", payload: dict[str, Any] | None = None) -> Any:
        headers = self._headers()
        data = None
        if payload is not None:
            data = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"{self.base_url}{path}", data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=3.0) as response:
                body = response.read()
        except (urllib.error.URLError, TimeoutError) as exc:
            raise CoolerControlError(f"CoolerControl API request failed: {exc}") from exc
        if not body:
            return None
        return json.loads(body)

    def status(self) -> dict[str, Any]:
        return self.request("/status")

    def settings(self, device_uid: str = NCT6687_UID) -> list[dict[str, Any]]:
        result = self.request(f"/devices/{device_uid}/settings")
        return result.get("settings", [])

    def set_fixed_speed(self, channel: str, duty: int) -> None:
        limits = {
            PUMP_CHANNEL: (60, 100),
            TOP_RADIATOR_CHANNEL: (20, 100),
            BOTTOM_RADIATOR_CHANNEL: (20, 100),
        }
        if channel not in limits:
            raise ValueError("Only commissioned pump and radiator channels are allowed")
        low, high = limits[channel]
        if not isinstance(duty, int) or not low <= duty <= high:
            raise ValueError(f"{channel} duty must be from {low} through {high} percent")
        self.request(
            f"/devices/{NCT6687_UID}/settings/{channel}/manual",
            method="PUT",
            payload={"speed_fixed": duty},
        )

    def apply_setting(self, setting: dict[str, Any]) -> None:
        channel = setting["channel_name"]
        if setting.get("profile_uid"):
            self.request(
                f"/devices/{NCT6687_UID}/settings/{channel}/profile",
                method="PUT",
                payload={"profile_uid": setting["profile_uid"]},
            )
        elif setting.get("speed_fixed") is not None:
            self.set_fixed_speed(channel, int(setting["speed_fixed"]))
        else:
            self.request(
                f"/devices/{NCT6687_UID}/settings/{channel}/reset", method="PUT"
            )


def _latest_by_uid(status: dict[str, Any], uid: str) -> dict[str, Any]:
    for device in status.get("devices", []):
        if device.get("uid") == uid:
            history = device.get("status_history", [])
            return history[-1] if history else {}
    return {}


def _named_values(items: list[dict[str, Any]], value_names: tuple[str, ...]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for item in items:
        name = item.get("name")
        if not name:
            continue
        for value_name in value_names:
            if value_name in item:
                result[name] = item[value_name]
                break
    return result


class SystemTelemetry:
    def __init__(self, client: CoolerControlClient | None = None):
        self.client = client or CoolerControlClient()
        self._previous_cpu: dict[int, tuple[int, int]] = {}

    @staticmethod
    def _proc_cpu_ticks() -> dict[int, tuple[int, int]]:
        ticks: dict[int, tuple[int, int]] = {}
        with Path("/proc/stat").open(encoding="utf-8") as handle:
            for line in handle:
                match = re.match(r"cpu(\d+)\s+(.+)", line)
                if not match:
                    continue
                values = [int(value) for value in match.group(2).split()]
                total = sum(values)
                idle = values[3] + (values[4] if len(values) > 4 else 0)
                ticks[int(match.group(1))] = (total, idle)
        return ticks

    def _loads(self) -> tuple[float | None, float | None]:
        current = self._proc_cpu_ticks()

        def group_load(cpus: tuple[int, ...]) -> float | None:
            total_delta = idle_delta = 0
            for cpu in cpus:
                if cpu not in current or cpu not in self._previous_cpu:
                    continue
                total_delta += current[cpu][0] - self._previous_cpu[cpu][0]
                idle_delta += current[cpu][1] - self._previous_cpu[cpu][1]
            if total_delta <= 0:
                return None
            return round(100.0 * (total_delta - idle_delta) / total_delta, 2)

        ccd0 = group_load(CCD0_CPUS)
        ccd1 = group_load(CCD1_CPUS)
        self._previous_cpu = current
        return ccd0, ccd1

    @staticmethod
    def _average_frequency(cpus: tuple[int, ...]) -> float | None:
        values: list[int] = []
        for cpu in cpus:
            path = Path(f"/sys/devices/system/cpu/cpufreq/policy{cpu}/scaling_cur_freq")
            try:
                values.append(int(path.read_text()))
            except (OSError, ValueError):
                continue
        return round(sum(values) / len(values) / 1000.0, 1) if values else None

    def read(self) -> dict[str, Any]:
        status = self.client.status()
        cpu = _latest_by_uid(status, CPU_UID)
        cooling = _latest_by_uid(status, NCT6687_UID)
        cpu_temps = _named_values(cpu.get("temps", []), ("temp",))
        cpu_channels = _named_values(cpu.get("channels", []), ("watts", "duty", "freq", "rpm"))
        cooling_channels = {
            item.get("name"): item for item in cooling.get("channels", []) if item.get("name")
        }
        ccd0_load, ccd1_load = self._loads()
        pump = cooling_channels.get(PUMP_CHANNEL, {})
        top_radiator = cooling_channels.get(TOP_RADIATOR_CHANNEL, {})
        bottom_radiator = cooling_channels.get(BOTTOM_RADIATOR_CHANNEL, {})
        return {
            "cpu_tctl_c": cpu_temps.get("temp1"),
            "ccd0_temp_c": cpu_temps.get("temp3"),
            "ccd1_temp_c": cpu_temps.get("temp4"),
            # Hardware mapping inference: CCD0 has lower CPPC rankings on this X3D host.
            "x3d_ccd_temp_c": cpu_temps.get("temp3"),
            "x3d_mapping": "CCD0/Tccd1 (inferred from CPPC preferred-core rankings)",
            "cpu_package_power_w": cpu_channels.get("power0"),
            "cpu_load_pct": cpu_channels.get("CPU Load"),
            "ccd0_load_pct": ccd0_load,
            "ccd1_load_pct": ccd1_load,
            "ccd0_frequency_mhz": self._average_frequency(CCD0_CPUS),
            "ccd1_frequency_mhz": self._average_frequency(CCD1_CPUS),
            "pump_rpm": pump.get("rpm"),
            "pump_duty_pct": pump.get("duty"),
            "top_radiator_rpm": top_radiator.get("rpm"),
            "top_radiator_duty_pct": top_radiator.get("duty"),
            "bottom_radiator_rpm": bottom_radiator.get("rpm"),
            "bottom_radiator_duty_pct": bottom_radiator.get("duty"),
            "coolercontrol_timestamp": cpu.get("timestamp"),
        }


def capture_cooling_settings(client: CoolerControlClient) -> dict[str, dict[str, Any]]:
    channels = (PUMP_CHANNEL, *RADIATOR_CHANNELS)
    captured = {
        setting["channel_name"]: setting
        for setting in client.settings()
        if setting.get("channel_name") in channels
    }
    # An absent CoolerControl setting means firmware/default control. Preserve
    # that state explicitly so a temporary manual experiment can reset it.
    return {channel: captured.get(channel, {"channel_name": channel}) for channel in channels}


def restore_cooling_settings(
    client: CoolerControlClient, settings: dict[str, dict[str, Any]]
) -> list[str]:
    errors: list[str] = []
    # Restore pump first so cooling never waits for radiator restoration.
    for channel in (PUMP_CHANNEL, *RADIATOR_CHANNELS):
        setting = settings.get(channel)
        if not setting:
            continue
        try:
            client.apply_setting(setting)
        except Exception as exc:  # Best-effort restoration; caller surfaces every error.
            errors.append(f"{channel}: {exc}")
    return errors
