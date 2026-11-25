"""Generate calibrated no-fault vs faulty sample datasets and 3 comparison plots.

Grounds fault parameters in empirical observations from:
- data/nofaults.csv (healthy baseline)
- source/three_faults_faulty_only.csv (heater_power_loss ~50%, therm_voltage_bias ~-4.9C)
- source/three_faults_contiguous.csv (ph_offset_bias ~-4.06 pH + post-clearance recovery)
- source/three_faults_and_variable_setpoints_faulty_only.csv

Outputs in visualisation_trial/:
- sample_nofaults.csv
- sample_heater_power_loss.csv
- sample_therm_voltage_bias.csv
- sample_ph_offset_bias.csv
- compare_heater_power_loss.png
- compare_therm_voltage_bias.png
- compare_ph_offset_bias.png
"""
from pathlib import Path
import sys
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "source"))

from phase1_detector import StreamingFaultDetector, load_telemetry_csv  # noqa: E402

OUT_DIR = ROOT / "visualisation_trial"


def make_heater_power_loss_sample(df_ok: pd.DataFrame) -> tuple[pd.DataFrame, np.ndarray]:
    """~50% heater power loss over windows 35..80 (matching recorded eff ~ 0.00180)."""
    d = df_ok.copy().reset_index(drop=True)
    y = np.zeros(len(d), dtype=bool)
    idx = np.arange(35, 81)
    y[idx] = True
    loss = 0.50  # observed median eff 0.00180 vs healthy 0.00360
    rng = np.random.default_rng(101)
    loss_vec = loss + rng.normal(0.0, 0.01, size=len(idx))
    u_new = d.loc[idx, "actuators_avg.heater_pwm"].to_numpy(float) / (1.0 - loss_vec)
    sat = u_new > 1.0
    d.loc[idx, "actuators_avg.heater_pwm"] = np.minimum(u_new, 1.0)
    # If unsaturated, PID delivers the same Wh; if saturated, energy drops proportionally
    e_scale = np.where(sat, (1.0 - loss_vec) * 1.0 / np.maximum(u_new, 1e-6), 1.0)
    d.loc[idx, "heater_energy_Wh"] *= e_scale
    d["faults.last_active"] = [
        "[{'name': 'heater_power_loss', 'id': None}]" if flag else "[]" for flag in y
    ]
    return d, y


def make_therm_voltage_bias_sample(
    df_ok: pd.DataFrame, t_amb: float = 22.0, c_th: float = 0.022
) -> tuple[pd.DataFrame, np.ndarray, np.ndarray]:
    """-4.5 C thermistor bias over windows 35..75 + post-clearance cooling (76..97).

    Uses the true-liquid thermal balance q = k_loss*(T_true - T_amb) + c_th*dT_true so that
    both the fault plateau and post-clearance healthy cooling match recorded simulator physics.
    """
    d = df_ok.copy().reset_index(drop=True)
    n = len(d)
    y = np.zeros(n, dtype=bool)
    rec_mask = np.zeros(n, dtype=bool)

    q_base = (d["heater_energy_Wh"] / d["window.samples"]).to_numpy(float)
    eff_base = q_base / d["actuators_avg.heater_pwm"].to_numpy(float)
    k_loss = float(np.median(q_base) / (np.median(d["temperature_C.mean"]) - t_amb))

    idx = np.arange(35, 76)
    y[idx] = True
    bias = -4.5  # negative bias matching source/three_faults_faulty_only.csv (-4.06 to -5.24 C)

    # True liquid warms smoothly from 30 C toward 34.5 C while T_meas = T_true + bias
    t_true = d["temperature_C.mean"].to_numpy(float).copy()
    dt_fault = 1.0 - np.exp(-np.arange(len(idx)) / 14.0)
    t_true[idx] = d.loc[idx, "temperature_C.mean"].to_numpy(float) - bias * dt_fault

    rec = np.arange(76, min(n, 98))
    rec_mask[rec] = True
    overshoot = t_true[idx[-1]] - float(d.loc[idx[-1], "setpoints.temperature_C"])
    t_true[rec] = d.loc[rec, "setpoints.temperature_C"].to_numpy(float) + overshoot * np.exp(-np.arange(len(rec)) / 10.0)

    # Measured temperature equals T_true + bias during fault, and T_true after clearance
    t_meas = t_true.copy()
    t_meas[idx] += bias
    shift = t_meas - d["temperature_C.mean"].to_numpy(float)
    for c in ("temperature_C.mean", "temperature_C.min", "temperature_C.max"):
        d[c] = d[c].to_numpy(float) + shift

    # Compute physically consistent energy q and heater_pwm (preserving eff)
    dt_true = np.diff(t_true, prepend=t_true[0])
    q_phys = np.clip(k_loss * (t_true - t_amb) + c_th * dt_true, 0.05 * np.median(q_base), 1.9 * np.median(q_base))
    active = np.zeros(n, dtype=bool)
    active[idx] = True
    active[rec] = True
    # Keep tiny baseline noise outside active/recovery; use physical ODE inside active+recovery
    q_new = np.where(active, q_phys + (q_base - np.median(q_base)), q_base)
    pwm_new = np.clip(q_new / eff_base, 0.0, 1.0)
    d["heater_energy_Wh"] = q_new * d["window.samples"].to_numpy(float)
    d["actuators_avg.heater_pwm"] = pwm_new

    d["faults.last_active"] = [
        "[{'name': 'therm_voltage_bias', 'id': None}]" if flag else "[]" for flag in y
    ]
    return d, y, rec_mask


def make_ph_offset_bias_sample(df_ok: pd.DataFrame) -> tuple[pd.DataFrame, np.ndarray, np.ndarray]:
    """-4.0 pH offset over windows 40..54 + post-clearance recovery (55..62), matching rows 181..196 of three_faults_contiguous.csv."""
    d = df_ok.copy().reset_index(drop=True)
    n = len(d)
    y = np.zeros(n, dtype=bool)
    rec_mask = np.zeros(n, dtype=bool)

    idx = np.arange(40, 55)
    y[idx] = True
    bias = -4.0
    # Matches recorded shape: sharp drop (5.0 -> 1.0), fast base titration (windows 40..45), then compensated plateau (46..54)
    k = np.arange(len(idx))
    ph_shift = bias * np.exp(-k / 2.2)
    for c in ("pH.mean", "pH.min", "pH.max"):
        d.loc[idx, c] += ph_shift
    # Base pump compensates strongly during pull-back, then subsides on plateau
    base_boost = 0.075 * np.exp(-k / 3.0)
    base_boost[k >= 6] = 0.0
    d.loc[idx, "actuators_avg.base_pwm"] += base_boost
    d.loc[idx, "actuators_avg.acid_pwm"] = np.where(k < 6, 0.0, d.loc[idx, "actuators_avg.acid_pwm"])

    # Clearance recovery: true over-alkalized pH (~8.3) appears and acid pump titrates back to 5.0
    rec = np.arange(55, min(n, 63))
    rec_mask[rec] = True
    kr = np.arange(len(rec))
    rec_shift = 3.3 * np.exp(-kr / 1.4)
    for c in ("pH.mean", "pH.min", "pH.max"):
        d.loc[rec, c] += rec_shift
    d.loc[rec, "actuators_avg.acid_pwm"] += 0.078 * np.exp(-kr / 1.8)
    d.loc[rec, "actuators_avg.base_pwm"] = 0.0

    d["faults.last_active"] = [
        "[{'name': 'ph_offset_bias', 'id': None}]" if flag else "[]" for flag in y
    ]
    return d, y, rec_mask


def compute_detector_traces(det: StreamingFaultDetector, df: pd.DataFrame) -> pd.DataFrame:
    """Run causal detector row-by-row to extract physical quantities and scores for plotting."""
    det.reset_state()
    rows = []
    for r in df.to_dict("records"):
        out = det.step(r)
        n = max(float(r["window.samples"]), 1.0)
        q = float(r["heater_energy_Wh"]) / n
        pwm = float(r["actuators_avg.heater_pwm"])
        eff = q / pwm if pwm >= det.u_min else np.nan
        rows.append(
            {
                "q": q,
                "eff": eff,
                "S_H1": out["scores"]["H1"],
                "S_Tbal": out["scores"]["T_bal"],
                "S_Tstep": out["scores"]["T_step"],
                "S_P1": out["scores"]["P1"],
                "S_P2": out["scores"]["P2"],
            }
        )
    return pd.DataFrame(rows)


def _shade_spans(ax, mask: np.ndarray, color: str, alpha: float, label: str | None = None):
    in_span = False
    start = 0
    labeled = False
    for i, val in enumerate(mask):
        if val and not in_span:
            in_span = True
            start = i
        elif not val and in_span:
            ax.axvspan(start - 0.5, i - 0.5, color=color, alpha=alpha, label=label if not labeled else None)
            labeled = True
            in_span = False
    if in_span:
        ax.axvspan(start - 0.5, len(mask) - 0.5, color=color, alpha=alpha, label=label if not labeled else None)


def plot_heater_comparison(df_ok: pd.DataFrame, df_f: pd.DataFrame, y: np.ndarray, tr_ok: pd.DataFrame, tr_f: pd.DataFrame) -> None:
    t = np.arange(len(df_ok))
    fig, axes = plt.subplots(3, 1, figsize=(11, 8.5), sharex=True, constrained_layout=True)
    fig.suptitle("Heater Power Loss (~50% Loss): No-Fault vs. Faulty Sample", fontsize=13, fontweight="bold")

    for ax in axes:
        _shade_spans(ax, y, "#ef4444", 0.14, "Fault active (heater_power_loss)" if ax is axes[0] else None)
        ax.grid(True, linestyle="--", alpha=0.35)

    axes[0].plot(t, df_ok["actuators_avg.heater_pwm"], color="#2563eb", lw=1.6, label="No-Fault heater_pwm")
    axes[0].plot(t, df_f["actuators_avg.heater_pwm"], color="#dc2626", lw=1.6, label="Faulty heater_pwm (PID compensates)")
    axes[0].set_ylabel("Heater Duty\n(0–1)")
    axes[0].legend(loc="upper right", fontsize=8.5)

    axes[1].plot(t, tr_ok["q"] * 1e3, color="#2563eb", lw=1.5, label="No-Fault energy/sample (mWh)")
    axes[1].plot(t, tr_f["q"] * 1e3, color="#dc2626", lw=1.4, ls="--", label="Faulty energy/sample (mWh)")
    ax1_r = axes[1].twinx()
    ax1_r.plot(t, df_f["temperature_C.mean"], color="#059669", lw=1.2, alpha=0.85, label="Faulty Temp (°C)")
    ax1_r.set_ylabel("Temp (°C)", color="#059669")
    ax1_r.set_ylim(28.5, 31.5)
    axes[1].set_ylabel("Energy / Sample\n(mWh)")
    lines1, labels1 = axes[1].get_legend_handles_labels()
    lines2, labels2 = ax1_r.get_legend_handles_labels()
    axes[1].legend(lines1 + lines2, labels1 + labels2, loc="upper right", fontsize=8.5)

    axes[2].plot(t, tr_ok["eff"] * 1e3, color="#2563eb", lw=1.6, label="No-Fault efficiency η (mWh/sample/duty)")
    axes[2].plot(t, tr_f["eff"] * 1e3, color="#dc2626", lw=1.7, label="Faulty efficiency η (drops ~50% to ~1.80)")
    axes[2].set_ylabel("Efficiency η\n(mWh / duty)")
    axes[2].set_xlabel("Window Index (1 Hz samples)")
    axes[2].legend(loc="lower right", fontsize=8.5)

    fig.savefig(OUT_DIR / "compare_heater_power_loss.png", dpi=160)
    plt.close(fig)


def plot_therm_comparison(
    df_ok: pd.DataFrame, df_f: pd.DataFrame, y: np.ndarray, rec: np.ndarray, tr_ok: pd.DataFrame, tr_f: pd.DataFrame
) -> None:
    t = np.arange(len(df_ok))
    fig, axes = plt.subplots(3, 1, figsize=(11, 8.5), sharex=True, constrained_layout=True)
    fig.suptitle("Thermistor Voltage Bias (-4.5 °C + Recovery): No-Fault vs. Faulty Sample", fontsize=13, fontweight="bold")

    for ax in axes:
        _shade_spans(ax, y, "#ef4444", 0.14, "Fault active (therm_voltage_bias)" if ax is axes[0] else None)
        _shade_spans(ax, rec, "#f59e0b", 0.14, "Post-clearance recovery (healthy)" if ax is axes[0] else None)
        ax.grid(True, linestyle="--", alpha=0.35)

    axes[0].plot(t, df_ok["temperature_C.mean"], color="#2563eb", lw=1.6, label="No-Fault Temp (°C)")
    axes[0].plot(t, df_f["temperature_C.mean"], color="#dc2626", lw=1.6, label="Faulty Temp (°C)")
    axes[0].plot(t, df_ok["setpoints.temperature_C"], color="#6b7280", lw=1.0, ls=":", label="Temp Setpoint (30 °C)")
    axes[0].set_ylabel("Temperature\n(°C)")
    axes[0].legend(loc="lower right", fontsize=8.5)

    axes[1].plot(t, tr_ok["q"] * 1e3, color="#2563eb", lw=1.5, label="No-Fault energy/sample (mWh)")
    axes[1].plot(t, tr_f["q"] * 1e3, color="#dc2626", lw=1.6, label="Faulty energy/sample (mWh)")
    axes[1].plot(t, df_f["actuators_avg.heater_pwm"] * 3.5, color="#9333ea", lw=1.1, ls="--", alpha=0.8, label="Faulty heater_pwm (×3.5)")
    axes[1].set_ylabel("Energy / Sample\n(mWh)")
    axes[1].legend(loc="upper right", fontsize=8.5)

    axes[2].plot(t, tr_ok["S_Tbal"], color="#2563eb", lw=1.5, label="No-Fault T_bal score (thermodynamic residual)")
    axes[2].plot(t, tr_f["S_Tbal"], color="#dc2626", lw=1.6, label="Faulty T_bal score")
    axes[2].plot(t, tr_f["S_H1"], color="#059669", lw=1.3, ls="--", label="Faulty H1 score (stays ~0: decoupled)")
    axes[2].axhline(5.0, color="#111827", lw=1.0, ls=":", label="T_bal threshold (k=5.0)")
    axes[2].set_ylabel("Anomaly Score\n(robust z)")
    axes[2].set_xlabel("Window Index (1 Hz samples)")
    axes[2].legend(loc="upper right", fontsize=8.5)

    fig.savefig(OUT_DIR / "compare_therm_voltage_bias.png", dpi=160)
    plt.close(fig)


def plot_ph_comparison(
    df_ok: pd.DataFrame, df_f: pd.DataFrame, y: np.ndarray, rec: np.ndarray, tr_ok: pd.DataFrame, tr_f: pd.DataFrame
) -> None:
    t = np.arange(len(df_ok))
    fig, axes = plt.subplots(3, 1, figsize=(11, 8.5), sharex=True, constrained_layout=True)
    fig.suptitle("pH Offset Bias (-4.0 pH + Recovery): No-Fault vs. Faulty Sample", fontsize=13, fontweight="bold")

    for ax in axes:
        _shade_spans(ax, y, "#ef4444", 0.14, "Fault active (ph_offset_bias)" if ax is axes[0] else None)
        _shade_spans(ax, rec, "#f59e0b", 0.14, "Post-clearance recovery (healthy)" if ax is axes[0] else None)
        ax.grid(True, linestyle="--", alpha=0.35)

    axes[0].plot(t, df_ok["pH.mean"], color="#2563eb", lw=1.5, label="No-Fault pH (sawtooth)")
    axes[0].plot(t, df_f["pH.mean"], color="#dc2626", lw=1.6, label="Faulty pH (step, plateau, overshoot)")
    axes[0].plot(t, df_ok["setpoints.pH"], color="#6b7280", lw=1.0, ls=":", label="pH Setpoint (5.0)")
    axes[0].set_ylabel("pH")
    axes[0].legend(loc="lower right", fontsize=8.5)

    axes[1].plot(t, df_ok["actuators_avg.base_pwm"] + df_ok["actuators_avg.acid_pwm"], color="#2563eb", lw=1.4, label="No-Fault total pump PWM")
    axes[1].plot(t, df_f["actuators_avg.base_pwm"], color="#dc2626", lw=1.5, label="Faulty base_pwm (compensates -4 pH)")
    axes[1].plot(t, df_f["actuators_avg.acid_pwm"], color="#d97706", lw=1.5, ls="--", label="Faulty acid_pwm (post-clearance recovery)")
    axes[1].set_ylabel("Pump Duty\n(PWM)")
    axes[1].legend(loc="upper right", fontsize=8.5)

    axes[2].plot(t, tr_ok["S_P2"], color="#2563eb", lw=1.4, label="No-Fault P2 rolling pump score")
    axes[2].plot(t, tr_f["S_P1"], color="#dc2626", lw=1.4, label="Faulty P1 step residual score")
    axes[2].plot(t, tr_f["S_P2"], color="#7c3aed", lw=1.5, ls="--", label="Faulty P2 rolling pump score")
    axes[2].axhline(5.5, color="#111827", lw=1.0, ls=":", label="P2 threshold (k=5.5)")
    axes[2].set_ylabel("Anomaly Score\n(robust z)")
    axes[2].set_xlabel("Window Index (1 Hz samples)")
    axes[2].legend(loc="upper right", fontsize=8.5)

    fig.savefig(OUT_DIR / "compare_ph_offset_bias.png", dpi=160)
    plt.close(fig)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    df_ok = load_telemetry_csv(str(ROOT / "data" / "nofaults.csv"))

    det = StreamingFaultDetector(adapt=False).fit_seed(df_ok)
    det.calibrate_thresholds(df_ok)

    df_h, y_h = make_heater_power_loss_sample(df_ok)
    df_t, y_t, rec_t = make_therm_voltage_bias_sample(df_ok)
    df_p, y_p, rec_p = make_ph_offset_bias_sample(df_ok)

    # Save generated sample datasets (< 50 KB each) in visualisation_trial/
    drop_cols = ["ts"]
    df_ok.drop(columns=drop_cols).to_csv(OUT_DIR / "sample_nofaults.csv", index=False)
    df_h.drop(columns=drop_cols).to_csv(OUT_DIR / "sample_heater_power_loss.csv", index=False)
    df_t.drop(columns=drop_cols).to_csv(OUT_DIR / "sample_therm_voltage_bias.csv", index=False)
    df_p.drop(columns=drop_cols).to_csv(OUT_DIR / "sample_ph_offset_bias.csv", index=False)

    tr_ok = compute_detector_traces(det, df_ok)
    tr_h = compute_detector_traces(det, df_h)
    tr_t = compute_detector_traces(det, df_t)
    tr_p = compute_detector_traces(det, df_p)

    plot_heater_comparison(df_ok, df_h, y_h, tr_ok, tr_h)
    plot_therm_comparison(df_ok, df_t, y_t, rec_t, tr_ok, tr_t)
    plot_ph_comparison(df_ok, df_p, y_p, rec_p, tr_ok, tr_p)

    for name, d, mask in [
        ("nofaults", df_ok, np.ones(len(df_ok), dtype=bool)),
        ("heater_power_loss", df_h, y_h),
        ("therm_voltage_bias", df_t, y_t),
        ("ph_offset_bias", df_p, y_p),
    ]:
        sub = d.loc[mask]
        q = sub["heater_energy_Wh"] / sub["window.samples"]
        eff = q / sub["actuators_avg.heater_pwm"]
        print(
            f"{name:20s}: rows={len(sub):3d} | eff_med={eff.median():.5f} "
            f"q_med={q.median():.5f} T_min={sub['temperature_C.mean'].min():.2f} "
            f"pH_min={sub['pH.mean'].min():.2f} pH_max={sub['pH.mean'].max():.2f}"
        )


if __name__ == "__main__":
    main()
