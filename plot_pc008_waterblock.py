#!/usr/bin/env python3
"""Generate PC 008 water-block run and comparison plots."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd


RUN_COLORS = [
    "#e63946", "#3772ff", "#2a9d8f", "#8338ec", "#f4a261",
    "#6d597a", "#118ab2", "#d62828", "#3a86ff", "#4d908e",
]


def load_run(run_dir: Path) -> tuple[pd.DataFrame, dict]:
    summary = pd.read_csv(run_dir / "summary.csv")
    info = json.loads((run_dir / "run_info.json").read_text(encoding="utf-8"))
    summary = summary[summary["result"] == "stable"].copy()
    for column in summary.columns:
        if column not in {"result"}:
            summary[column] = pd.to_numeric(summary[column], errors="coerce")
    return summary, info["plan"]


def load_completed_runs(runs_dir: Path) -> list[tuple[pd.DataFrame, dict, Path]]:
    """Load every real run containing at least one stable stage.

    This mirrors Cubesat007's comparison workflow: scan run directories,
    ignore aliases/symlinks and incomplete runs, then overlay the remaining
    series in chronological order.
    """
    loaded: list[tuple[pd.DataFrame, dict, Path]] = []
    for run_dir in sorted(runs_dir.glob("waterblock_*")):
        if not run_dir.is_dir() or run_dir.is_symlink():
            continue
        try:
            summary, plan = load_run(run_dir)
        except (FileNotFoundError, KeyError, ValueError, json.JSONDecodeError):
            continue
        if not summary.empty:
            loaded.append((summary, plan, run_dir))
    return loaded


def comparison_label(plan: dict, run_dir: Path) -> str:
    """Use the plan label, adding the timestamp only for duplicate labels."""
    return str(plan.get("label") or run_dir.name)


def description(plan: dict) -> str:
    top = plan.get("top_radiator_duty_pct", plan.get("radiator_duty_pct", "?"))
    bottom = plan.get("bottom_radiator_duty_pct", plan.get("radiator_duty_pct", "?"))
    return (
        f"{plan['label']} | pump {plan['pump_duty_pct']}% | "
        f"top/bottom radiators {top}/{bottom}%"
    )


def stage_label(row: pd.Series) -> str:
    if "package_power_target_w" in row.index:
        return (
            f"{int(row.package_power_target_w)} W · "
            f"{int(row.ccd0_workload_share_pct)}/{int(row.ccd1_workload_share_pct)}%"
        )
    if "ccd0_target_power_w" in row.index:
        return f"{int(row.ccd0_target_power_w)}/{int(row.ccd1_target_power_w)} W"
    return f"{int(row.ccd0_target_load_pct)}/{int(row.ccd1_target_load_pct)}%"


def active_rows(summary: pd.DataFrame, ccd: int) -> pd.DataFrame:
    share_column = f"ccd{ccd}_workload_share_pct"
    power_column = f"ccd{ccd}_target_power_w"
    load_column = f"ccd{ccd}_target_load_pct"
    if share_column in summary:
        return summary[summary[share_column] > 0]
    if power_column in summary:
        return summary[summary[power_column] > 0]
    if load_column in summary:
        return summary[summary[load_column] > 0]
    return summary


def stable_band(axis, rows: pd.DataFrame, prefix: str, color: str) -> None:
    low, high = f"{prefix}_min_c", f"{prefix}_max_c"
    if low in rows and high in rows and rows[low].notna().any() and rows[high].notna().any():
        axis.fill_between(
            rows["cpu_package_power_mean_w"], rows[low], rows[high],
            color=color, alpha=0.12,
        )


def style() -> None:
    plt.rcParams.update(
        {
            "figure.dpi": 110,
            "savefig.dpi": 180,
            "axes.grid": True,
            "grid.alpha": 0.3,
            "axes.titlesize": 16,
            "axes.labelsize": 12,
            "xtick.labelsize": 10,
            "ytick.labelsize": 10,
            "legend.fontsize": 11,
        }
    )


def measured_power_ticks(axis, summary: pd.DataFrame) -> None:
    """Put every measured stable power directly on the x-axis."""
    values = summary["cpu_package_power_mean_w"].dropna().tolist()
    axis.set_xticks(values, [f"{value:.1f}" for value in values], rotation=35, ha="right")


def label_points(axis, x, y, *, unit: str, offset: tuple[int, int]) -> None:
    """Label every point with its exact x/y coordinate."""
    points = list(zip(x, y))
    for index, (x_value, y_value) in enumerate(points):
        point_offset = (-68, offset[1]) if index == len(points) - 1 else offset
        axis.annotate(
            f"{x_value:.1f} W\n{y_value:.2f}{unit}",
            (x_value, y_value),
            xytext=point_offset,
            textcoords="offset points",
            fontsize=9,
            fontweight="semibold",
            bbox={"boxstyle": "round,pad=0.22", "fc": "white", "ec": "none", "alpha": 0.82},
        )


def plot_temperature(summary: pd.DataFrame, plan: dict, output: Path) -> None:
    figure, axis = plt.subplots(figsize=(11, 7), constrained_layout=True)
    x = summary["cpu_package_power_mean_w"]
    colors = {"ccd0": "#219ebc", "ccd1": "#8338ec", "tctl": "#e63946", "water": "#2a9d8f"}
    for ccd, marker, label in (
        (0, "o", "CCD0 / Tccd1 (V-Cache inferred)"),
        (1, "s", "CCD1 / Tccd2"),
    ):
        rows = active_rows(summary, ccd)
        if not rows.empty:
            color = colors[f"ccd{ccd}"]
            axis.plot(rows["cpu_package_power_mean_w"], rows[f"ccd{ccd}_temp_mean_c"], marker=marker, color=color, label=label)
            stable_band(axis, rows, f"ccd{ccd}_temp", color)
    axis.plot(x, summary["cpu_tctl_mean_c"], marker="^", color=colors["tctl"], label="CPU Tctl")
    stable_band(axis, summary, "cpu_tctl", colors["tctl"])
    axis.plot(x, summary["water_outlet_temp_mean_c"], marker="D", color=colors["water"], label="Water after block")
    stable_band(axis, summary, "water_outlet_temp", colors["water"])
    for _, row in summary.iterrows():
        axis.annotate(
            stage_label(row),
            (row.cpu_package_power_mean_w, row.cpu_tctl_mean_c),
            xytext=(4, 5), textcoords="offset points", fontsize=8,
        )
    axis.set_title(f"PC 008 Water-Block Temperature vs Measured Package Power\n{description(plan)}")
    axis.set_xlabel("Measured CPU package power (W)")
    axis.set_ylabel("Steady-state temperature (°C)")
    axis.axhline(plan.get("cutoff_c", 95), color="#444444", linestyle="--", linewidth=1, label="Experiment cutoff")
    axis.legend()
    figure.savefig(output)
    plt.close(figure)


def plot_resistance(summary: pd.DataFrame, plan: dict, output: Path) -> None:
    figure, axis = plt.subplots(figsize=(11, 7), constrained_layout=True)
    x = summary["cpu_package_power_mean_w"]
    if summary["ccd0_apparent_rth_c_per_w"].notna().any():
        axis.plot(x, summary["ccd0_apparent_rth_c_per_w"], marker="o", label="CCD0 apparent Rθ")
    if summary["ccd1_apparent_rth_c_per_w"].notna().any():
        axis.plot(x, summary["ccd1_apparent_rth_c_per_w"], marker="s", label="CCD1 apparent Rθ")
    values = pd.concat(
        [summary["ccd0_apparent_rth_c_per_w"], summary["ccd1_apparent_rth_c_per_w"]]
    ).dropna()
    if not values.empty:
        axis.axhline(values.mean(), color="#555555", linestyle="--", linewidth=1, label=f"Mean {values.mean():.3f} °C/W")
    axis.set_title(f"PC 008 CPU-to-Water Apparent Thermal Resistance\n{description(plan)}")
    axis.set_xlabel("Measured CPU package power (W)")
    axis.set_ylabel("(CCD temperature − water-block outlet) / package power (°C/W)")
    axis.legend()
    figure.savefig(output)
    plt.close(figure)


def plot_comparisons(runs_dir: Path, comparison: Path) -> None:
    """Overlay all completed runs, following the Cubesat007 comparison model."""
    loaded = load_completed_runs(runs_dir)
    if not loaded:
        return
    comparison.mkdir(parents=True, exist_ok=True)

    label_counts: dict[str, int] = {}
    for _, plan, run_dir in loaded:
        label = comparison_label(plan, run_dir)
        label_counts[label] = label_counts.get(label, 0) + 1

    def run_label(plan: dict, run_dir: Path) -> str:
        label = comparison_label(plan, run_dir)
        if label_counts[label] > 1:
            stamp = run_dir.name.removeprefix("waterblock_").split("_", 1)[0]
            return f"{label} · {stamp}"
        return label

    figure, axis = plt.subplots(figsize=(15, 9), constrained_layout=True)
    for run_index, (summary, plan, run_dir) in enumerate(loaded):
        label = run_label(plan, run_dir)
        color = RUN_COLORS[run_index % len(RUN_COLORS)]
        axis.plot(
            summary["cpu_package_power_mean_w"], summary["cpu_tctl_mean_c"],
            marker="o", markersize=8, linewidth=2.2,
            color=color, label=label,
        )
        stable_band(axis, summary, "cpu_tctl", color)
        if len(loaded) == 1:
            label_points(
                axis, summary["cpu_package_power_mean_w"], summary["cpu_tctl_mean_c"],
                unit="°C", offset=(7, 8),
            )
    axis.margins(x=0.06, y=0.12)
    axis.set_title(f"PC 008 CPU Tctl vs Measured Package Power\n{len(loaded)} completed runs overlaid")
    axis.set_xlabel("Measured CPU package power (W)")
    axis.set_ylabel("Steady-state CPU Tctl (°C)")
    axis.legend(fontsize=8)
    figure.savefig(comparison / "comparison_temp_vs_power.png")
    plt.close(figure)

    figure, axis = plt.subplots(figsize=(15, 9), constrained_layout=True)
    for run_index, (summary, plan, run_dir) in enumerate(loaded):
        label = run_label(plan, run_dir)
        color = RUN_COLORS[run_index % len(RUN_COLORS)]
        ambient = float(plan.get("ambient_c", 23.0))
        x = summary["cpu_package_power_mean_w"]
        axis.plot(
            x, summary["cpu_tctl_mean_c"] - ambient, marker="^",
            markersize=8, linewidth=2.2, color=color, label=f"{label} · Tctl",
        )
        if len(loaded) == 1:
            label_points(axis, x, summary["cpu_tctl_mean_c"] - ambient, unit="°C", offset=(7, 8))
        for ccd, marker, linestyle in ((0, "o", "-"), (1, "s", "--")):
            rows = active_rows(summary, ccd)
            if not rows.empty:
                axis.plot(
                    rows["cpu_package_power_mean_w"],
                    rows[f"ccd{ccd}_temp_mean_c"] - ambient,
                    marker=marker, markersize=8, linewidth=2.2, linestyle=linestyle,
                    color=color, alpha=0.55, label=f"{label} · CCD{ccd}",
                )
                if len(loaded) == 1:
                    label_points(
                        axis, rows["cpu_package_power_mean_w"],
                        rows[f"ccd{ccd}_temp_mean_c"] - ambient,
                        unit="°C", offset=(7, -30 if ccd == 0 else -10),
                    )
    axis.margins(x=0.06, y=0.14)
    axis.set_title(f"PC 008 CPU Delta-T vs Package Power\n{len(loaded)} completed runs overlaid; each run uses its recorded ambient")
    axis.set_xlabel("Measured CPU package power (W)")
    axis.set_ylabel("CPU temperature − ambient (°C)")
    axis.legend(fontsize=8)
    figure.savefig(comparison / "comparison_delta_t_vs_power.png")
    plt.close(figure)

    figure, axis = plt.subplots(figsize=(15, 9), constrained_layout=True)
    for run_index, (summary, plan, run_dir) in enumerate(loaded):
        label = run_label(plan, run_dir)
        color = RUN_COLORS[run_index % len(RUN_COLORS)]
        x = summary["cpu_package_power_mean_w"]
        ambient = float(plan.get("ambient_c", 23.0))
        tctl_rth = (summary["cpu_tctl_mean_c"] - ambient) / x
        axis.plot(x, tctl_rth, marker="^", markersize=8, linewidth=2.2, color=color, label=f"{label} · Tctl")
        if len(loaded) == 1:
            label_points(axis, x, tctl_rth, unit="°C/W", offset=(7, 8))
        for ccd, marker, linestyle in ((0, "o", "-"), (1, "s", "--")):
            rows = active_rows(summary, ccd)
            if not rows.empty:
                rth = ((rows[f"ccd{ccd}_temp_mean_c"] - ambient)
                       / rows["cpu_package_power_mean_w"])
                axis.plot(
                    rows["cpu_package_power_mean_w"],
                    rth, marker=marker, markersize=8, linewidth=2.2,
                    linestyle=linestyle, color=color, alpha=0.55,
                    label=f"{label} · CCD{ccd}",
                )
                if len(loaded) == 1:
                    label_points(
                        axis, rows["cpu_package_power_mean_w"], rth,
                        unit="°C/W", offset=(7, -30 if ccd == 0 else -10),
                    )
    axis.margins(x=0.06, y=0.14)
    axis.set_title(f"PC 008 Ambient-Referenced Thermal Resistance\n{len(loaded)} completed runs overlaid")
    axis.set_xlabel("Measured CPU package power (W)")
    axis.set_ylabel("(CPU temperature − ambient) / package power (°C/W)")
    axis.legend(fontsize=8)
    figure.savefig(comparison / "comparison_thermal_resistance.png")
    plt.close(figure)

    metrics: list[dict[str, float | str]] = []
    for summary, plan, run_dir in loaded:
        nearest_90 = summary.iloc[(summary["cpu_package_power_mean_w"] - 90).abs().argsort()[:1]]
        metrics.append(
            {
                "label": run_label(plan, run_dir),
                "temp_at_90w_c": float(nearest_90["cpu_tctl_mean_c"].iloc[0]),
                "max_stable_power_w": float(summary["cpu_package_power_mean_w"].max()),
            }
        )
    metrics_frame = pd.DataFrame(metrics).sort_values("temp_at_90w_c", ascending=False)
    positions = range(len(metrics_frame))
    height = max(5.5, 0.55 * len(metrics_frame) + 2.2)
    figure, axes = plt.subplots(
        1, 2, figsize=(15, height), sharey=True, constrained_layout=True
    )
    axes[0].barh(positions, metrics_frame["temp_at_90w_c"], color="#e63946")
    axes[0].set_title("Tctl at nearest stable ~90 W")
    axes[0].set_xlabel("CPU Tctl (°C)")
    axes[0].set_yticks(list(positions), metrics_frame["label"], fontsize=8)
    axes[0].invert_yaxis()
    axes[1].barh(positions, metrics_frame["max_stable_power_w"], color="#3772ff")
    axes[1].set_title("Maximum stable measured package power")
    axes[1].set_xlabel("Package power (W)")
    for index, value in enumerate(metrics_frame["temp_at_90w_c"]):
        axes[0].text(value - 1, index, f"{value:.2f}°C", va="center", ha="right", color="white", fontweight="bold", fontsize=12)
    for index, value in enumerate(metrics_frame["max_stable_power_w"]):
        axes[1].text(value - 2, index, f"{value:.2f} W", va="center", ha="right", color="white", fontweight="bold", fontsize=12)
    figure.suptitle("PC 008 Water-Block Key Metrics")
    figure.savefig(comparison / "comparison_key_metrics.png")
    plt.close(figure)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--runs-dir", type=Path, required=True)
    parser.add_argument("--comparison-dir", type=Path)
    args = parser.parse_args()
    style()
    summary, plan = load_run(args.run_dir)
    if summary.empty:
        raise SystemExit("run has no stable stages")
    plots = args.run_dir / "plots"
    plots.mkdir(exist_ok=True)
    plot_temperature(summary, plan, plots / "temperature_vs_power.png")
    plot_resistance(summary, plan, plots / "thermal_resistance.png")
    plot_comparisons(args.runs_dir, args.comparison_dir or args.runs_dir / "comparisons")


if __name__ == "__main__":
    main()
