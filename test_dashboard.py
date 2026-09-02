import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from pc008_control import (
    BOTTOM_RADIATOR_CHANNEL,
    PUMP_CHANNEL,
    TOP_RADIATOR_CHANNEL,
    CoolerControlClient,
)
from pc008_experiment import ExperimentManager, validate_plan


MODULE_PATH = Path(__file__).with_name("mps_pressure_dashboard.py")
SPEC = importlib.util.spec_from_file_location("mps_pressure_dashboard", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class DecodeReportTest(unittest.TestCase):
    def test_known_highflow_capture(self):
        capture = (Path(__file__).with_name("tests") / "fixtures" / "highflow_sensors.bin").read_bytes()
        decoded = MODULE.decode_report(capture, timestamp=1.0)
        self.assertEqual(decoded["report_bytes"], 50)
        self.assertEqual(decoded["pressure_raw"], 638)
        self.assertEqual(decoded["pressure_offset_raw"], -240)
        self.assertEqual(decoded["pressure_normalized_raw"], 398)
        self.assertAlmostEqual(decoded["pressure_calibrated_mbar"], 48.828, places=3)
        self.assertEqual(decoded["field_0x23_raw"], 5803)
        self.assertEqual(decoded["temperature_external_c"], 26.07)
        self.assertEqual(decoded["temperature_internal_c"], 29.42)

    def test_short_report_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "short feature report"):
            MODULE.decode_report(b"\x02\x00")

    def test_calibration_curve_clamps_and_interpolates(self):
        self.assertEqual(MODULE.calibrated_pressure_mbar(0), 0.0)
        self.assertAlmostEqual(MODULE.calibrated_pressure_mbar(398), 48.828, places=3)
        self.assertEqual(MODULE.calibrated_pressure_mbar(900), 100.0)
        self.assertIsNone(MODULE.calibrated_pressure_mbar(None))

    def test_csv_schema_change_uses_pc008_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "readings.csv"
            day = "1970-01-01"
            old = path.with_name(f"readings-{day}.csv")
            old.write_text("timestamp,old_field\n", encoding="utf-8")
            MODULE.append_csv(path, {"timestamp": 1.0})
            upgraded = path.with_name(f"readings-{day}-pc008.csv")
            self.assertTrue(upgraded.exists())
            self.assertEqual(upgraded.read_text().splitlines()[0].split(","), list(MODULE.CSV_FIELDS))


class PC008ControlTest(unittest.TestCase):
    def test_attributed_power_is_only_exposed_during_active_stage(self):
        active = MODULE.attributed_power_fields({
            "state": "running",
            "latest": {"active_ccd": 1, "attributed_ccd_power_w": 72.5},
        })
        self.assertEqual(active["ccd0_attributed_power_w"], 0.0)
        self.assertEqual(active["ccd1_attributed_power_w"], 72.5)
        idle = MODULE.attributed_power_fields({"state": "completed", "latest": {}})
        self.assertIsNone(idle["ccd0_attributed_power_w"])
        self.assertIsNone(idle["ccd1_attributed_power_w"])

    def test_continuous_ccd_power_estimate_sums_to_dynamic_package_power(self):
        estimator = MODULE.CCDPowerEstimator()
        baseline = {
            "cpu_package_power_w": 35.0,
            "ccd0_load_pct": 2.0,
            "ccd1_load_pct": 2.0,
            "ccd0_frequency_mhz": 1000.0,
            "ccd1_frequency_mhz": 1000.0,
        }
        estimator.update(baseline)
        loaded = dict(baseline, cpu_package_power_w=95.0, ccd0_load_pct=75.0, ccd1_load_pct=25.0)
        result = estimator.update(loaded)
        self.assertAlmostEqual(result["ccd0_estimated_power_w"], 45.0)
        self.assertAlmostEqual(result["ccd1_estimated_power_w"], 15.0)

    def test_experiment_plan_generates_package_power_sweep(self):
        plan = validate_plan({
            "label": "bykski_stock",
            "package_power_min_w": 60,
            "package_power_max_w": 100,
            "package_power_step_w": 15,
            "ccd0_workload_share_pct": 70,
        })
        self.assertEqual(plan["package_power_targets_w"], [60, 75, 90, 100])
        self.assertEqual(plan["ccd0_workload_share_pct"], 70)
        self.assertEqual(plan["ccd1_workload_share_pct"], 30)
        self.assertEqual(plan["cutoff_c"], 95)
        self.assertEqual(plan["ambient_c"], 23.0)
        self.assertTrue(plan["dry_run"])

    def test_experiment_rejects_reversed_power_range(self):
        with self.assertRaisesRegex(ValueError, "package_power_max_w"):
            validate_plan({
                "package_power_min_w": 100,
                "package_power_max_w": 60,
            })

    def test_experiment_rejects_cutoff_above_95_c(self):
        with self.assertRaisesRegex(ValueError, "70 through 95"):
            validate_plan({"cutoff_c": 96})

    def test_workload_split_preserves_requested_ratio(self):
        self.assertEqual(ExperimentManager._split_loads(100, 70), (70, 30))
        self.assertEqual(ExperimentManager._split_loads(200, 70), (100, 42.86))
        self.assertEqual(ExperimentManager._split_loads(80, 0), (0, 80))

    def test_workload_controller_uses_fractional_quota_and_fixed_workers(self):
        self.assertEqual(ExperimentManager._split_loads(3, 50), (1.5, 1.5))
        self.assertEqual(ExperimentManager._quota_percent(1.5), "12.00%")
        self.assertEqual(ExperimentManager._worker_count(1.5), 8)
        self.assertEqual(ExperimentManager._worker_count(60), 8)
        self.assertEqual(ExperimentManager._worker_count(0), 0)

    def test_high_power_controller_uses_bounded_acquisition(self):
        # High-load feed-forward now uses the measured 0.63 W per aggregate
        # load point slope instead of the old optimistic 1.8 W/load.
        self.assertAlmostEqual(
            ExperimentManager._initial_total_load(200, 32), 224.0476, places=3
        )
        # PI gains are tuned for the 5 s control interval and deliberately
        # slew-limited so single-CCD boost lag cannot create a limit cycle.
        self.assertAlmostEqual(
            ExperimentManager._control_correction(120, 120, 5, 30), 1.0
        )
        self.assertAlmostEqual(
            ExperimentManager._control_correction(50, 120, 5, 70), 0.6
        )
        self.assertAlmostEqual(
            ExperimentManager._control_correction(-25, 50, 5, 90), -1.0
        )
        self.assertEqual(ExperimentManager._control_correction(8, 8, 5, 3), 0.3)

    def test_low_power_feed_forward_matches_boost_region(self):
        # Stage 1 of the final run: idle 35.734 W, target 60 W, so the
        # dynamic delta is 24.266 W and the old 6 W/load low-load rule
        # gives about 4.04 aggregate load points (2.02 per CCD).
        self.assertAlmostEqual(
            ExperimentManager._initial_total_load(60, 35.734), 4.0443, places=3
        )

    def test_max_total_load_respects_workload_split(self):
        self.assertEqual(ExperimentManager._max_total_load(50), 200.0)
        self.assertAlmostEqual(ExperimentManager._max_total_load(70), 142.8571, places=3)

    def test_thermal_headroom_limits_positive_ramp_only(self):
        guard = ExperimentManager._temperature_guarded_correction
        self.assertEqual(guard(12, 60, 85), 12)
        self.assertEqual(guard(12, 75, 85), 4)
        self.assertEqual(guard(12, 79, 85), 1)
        self.assertEqual(guard(12, 81, 85), 0)
        self.assertEqual(guard(-12, 84, 85), -12)

    def test_cooling_control_is_channel_and_range_limited(self):
        client = CoolerControlClient()
        client.request = Mock()
        client.set_fixed_speed(PUMP_CHANNEL, 60)
        client.set_fixed_speed(TOP_RADIATOR_CHANNEL, 20)
        client.set_fixed_speed(BOTTOM_RADIATOR_CHANNEL, 20)
        self.assertEqual(client.request.call_count, 3)
        with self.assertRaisesRegex(ValueError, "60 through 100"):
            client.set_fixed_speed(PUMP_CHANNEL, 59)
        with self.assertRaisesRegex(ValueError, "Only commissioned"):
            client.set_fixed_speed("fan1", 100)

    def test_experiment_plan_supports_independent_radiator_groups(self):
        plan = validate_plan({
            "top_radiator_duty_pct": 40,
            "bottom_radiator_duty_pct": 70,
        })
        self.assertEqual(plan["top_radiator_duty_pct"], 40)
        self.assertEqual(plan["bottom_radiator_duty_pct"], 70)

    def test_power_stage_summary_records_package_target_and_split(self):
        row = {
            "elapsed_s": 120,
            "package_power_target_w": 80,
            "ccd0_workload_share_pct": 70,
            "ccd1_workload_share_pct": 30,
            "ccd0_applied_load_pct": 42,
            "ccd1_applied_load_pct": 18,
            "idle_package_power_w": 30,
            "power_error_w": 1,
            "cpu_package_power_w": 79,
            "cpu_tctl_c": 70,
            "ccd0_temp_c": 65,
            "ccd1_temp_c": 45,
            "water_outlet_temp_c": 30,
            "pressure_drop_mbar": 86,
            "pump_rpm": 4800,
            "pump_duty_pct": 100,
            "top_radiator_rpm": 0,
            "top_radiator_duty_pct": 100,
            "bottom_radiator_rpm": 0,
            "bottom_radiator_duty_pct": 100,
        }
        summary = ExperimentManager._summary(1, "stable", [row], {"window_sec": 120})
        self.assertEqual(summary["package_power_target_w"], 80)
        self.assertEqual(summary["ccd0_workload_share_pct"], 70)
        self.assertEqual(summary["ccd1_applied_load_mean_pct"], 18)
        self.assertEqual(summary["ccd0_temp_min_c"], 65)
        self.assertEqual(summary["ccd0_temp_max_c"], 65)
        self.assertEqual(summary["ccd1_minus_ccd0_mean_c"], -20)
        self.assertEqual(summary["ccd_delta_abs_mean_c"], 20)


if __name__ == "__main__":
    unittest.main()
