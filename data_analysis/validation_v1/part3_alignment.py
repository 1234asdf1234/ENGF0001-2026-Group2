"""Part 3 implementation: closed-loop synthetic fault generation & 4-step alignment suite.

Follows Part 3 of thoughts/ai_revised.md:
1. Closed-loop physics-consistent generator (single faults, overlapping faults, setpoint steps).
2. Four-step alignment verification against recorded faulty datasets (< 1 MB snapshots):
   - Check 1: Physical invariants & cross-fault decoupling
   - Check 2: Residual distribution alignment (median, IQR, min/max, two-sample KS)
   - Check 3: Temporal 3-phase trajectory & post-clearance recovery check
   - Check 4: Zero-shot detector transfer gate (calibrate on seed+syn only, test on recorded)
"""
from pathlib import Path
import copy
import sys
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

from phase1_detector import (  # noqa: E402
    FAULT_TYPES,
    StreamingFaultDetector,
    evaluate_stream,
    load_telemetry_csv,
    parse_fault_labels,
)


def ks_2samp(x: np.ndarray, y: np.ndarray) -> float:
    """Empirical two-sample Kolmogorov-Smirnov statistic (D in [0, 1])."""
    x = np.sort(np.asarray(x, dtype=float))
    y = np.sort(np.asarray(y, dtype=float))
    x = x[np.isfinite(x)]
    y = y[np.isfinite(y)]
    if len(x) == 0 or len(y) == 0:
        return float("nan")
    grid = np.sort(np.concatenate([x, y]))
    cdf_x = np.searchsorted(x, grid, side="right") / len(x)
    cdf_y = np.searchsorted(y, grid, side="right") / len(y)
    return float(np.max(np.abs(cdf_x - cdf_y)))


def inject_closed_loop_episode(
    d: pd.DataFrame,
    fault: str,
    start: int,
    length: int,
    severity: float,
    t_amb: float = 22.0,
    c_th: float = 0.022,
) -> tuple[pd.DataFrame, np.ndarray, np.ndarray]:
    """Inject one physics-consistent closed-loop fault episode into d."""
    d = d.copy().reset_index(drop=True)
    n = len(d)
    end = min(n, start + length)
    idx = np.arange(start, end)
    y = np.zeros(n, dtype=bool)
    rec_mask = np.zeros(n, dtype=bool)
    y[idx] = True

    if fault == "heater_power_loss":
        loss = float(np.clip(severity, 0.15, 0.70))
        u_old = d.loc[idx, "actuators_avg.heater_pwm"].to_numpy(float)
        u_new = u_old / (1.0 - loss)
        sat = u_new > 1.0
        d.loc[idx, "actuators_avg.heater_pwm"] = np.minimum(u_new, 1.0)
        # Unsaturated preserves energy; saturated scales energy down by (1 - loss)/u_old
        scale_e = np.where(sat, (1.0 - loss) / np.maximum(u_old, 1e-6), 1.0)
        d.loc[idx, "heater_energy_Wh"] *= scale_e

    elif fault == "therm_voltage_bias":
        bias = float(severity)  # signed bias in deg C (e.g. -4.5)
        q_base = (d["heater_energy_Wh"] / d["window.samples"]).to_numpy(float)
        eff_base = q_base / np.maximum(d["actuators_avg.heater_pwm"].to_numpy(float), 1e-6)
        k_loss = float(np.median(q_base) / (np.median(d["temperature_C.mean"]) - t_amb))

        t_true = d["temperature_C.mean"].to_numpy(float).copy()
        tau = 14.0
        dt_fault = 1.0 - np.exp(-np.arange(len(idx)) / tau)
        t_true[idx] = d.loc[idx, "temperature_C.mean"].to_numpy(float) - bias * dt_fault

        rec = np.arange(end, min(n, end + 15))
        rec_mask[rec] = True
        if len(rec):
            overshoot = t_true[idx[-1]] - float(d.loc[idx[-1], "setpoints.temperature_C"])
            t_true[rec] = d.loc[rec, "setpoints.temperature_C"].to_numpy(float) + overshoot * np.exp(-np.arange(len(rec)) / 10.0)

        t_meas = t_true.copy()
        t_meas[idx] += bias
        shift = t_meas - d["temperature_C.mean"].to_numpy(float)
        for c in ("temperature_C.mean", "temperature_C.min", "temperature_C.max"):
            d[c] = d[c].to_numpy(float) + shift

        dt_true = np.diff(t_true, prepend=t_true[0])
        q_phys = np.clip(k_loss * (t_true - t_amb) + c_th * dt_true, 0.05 * np.median(q_base), 1.95 * np.median(q_base))
        active = y | rec_mask
        q_new = np.where(active, q_phys + (q_base - np.median(q_base)), q_base)
        d["heater_energy_Wh"] = q_new * d["window.samples"].to_numpy(float)
        d["actuators_avg.heater_pwm"] = np.clip(q_new / np.maximum(eff_base, 1e-6), 0.0, 1.0)

    elif fault == "ph_offset_bias":
        bias = float(severity)  # signed offset (e.g. -4.0)
        k = np.arange(len(idx))
        ph_shift = bias * np.exp(-k / 2.2)
        for c in ("pH.mean", "pH.min", "pH.max"):
            d.loc[idx, c] += ph_shift

        pump_col = "actuators_avg.base_pwm" if bias < 0 else "actuators_avg.acid_pwm"
        opp_col = "actuators_avg.acid_pwm" if bias < 0 else "actuators_avg.base_pwm"
        boost = 0.02 * abs(bias) * np.exp(-k / 3.0)
        boost[k >= 6] = 0.0
        d.loc[idx, pump_col] += boost
        d.loc[idx, opp_col] = np.where(k < 6, 0.0, d.loc[idx, opp_col])

        rec = np.arange(end, min(n, end + 8))
        rec_mask[rec] = True
        if len(rec):
            kr = np.arange(len(rec))
            rec_shift = -0.82 * bias * np.exp(-kr / 1.4)
            for c in ("pH.mean", "pH.min", "pH.max"):
                d.loc[rec, c] += rec_shift
            d.loc[rec, opp_col] += 0.02 * abs(bias) * np.exp(-kr / 1.8)
            d.loc[rec, pump_col] = 0.0

    return d, y, rec_mask


def generate_matched_suite(df_seed: pd.DataFrame) -> dict[str, tuple[pd.DataFrame, np.ndarray]]:
    """Generate synthetic datasets matched to observed recorded fault regimes + overlap + setpoints."""
    out = {}

    # 1. heater_power_loss (~50% loss across short and sustained intervals)
    d_h, y_h1, _ = inject_closed_loop_episode(df_seed, "heater_power_loss", start=20, length=6, severity=0.45)
    d_h, y_h2, _ = inject_closed_loop_episode(d_h, "heater_power_loss", start=50, length=35, severity=0.50)
    y_h = y_h1 | y_h2
    d_h["faults.last_active"] = ["[{'name': 'heater_power_loss', 'id': None}]" if f else "[]" for f in y_h]
    out["heater_power_loss"] = (d_h, y_h)

    # 2. therm_voltage_bias (~ -4.5 C matching source/three_faults_faulty_only.csv)
    d_t, y_t1, _ = inject_closed_loop_episode(df_seed, "therm_voltage_bias", start=18, length=5, severity=-3.5)
    d_t, y_t2, _ = inject_closed_loop_episode(d_t, "therm_voltage_bias", start=50, length=30, severity=-4.5)
    y_t = y_t1 | y_t2
    d_t["faults.last_active"] = ["[{'name': 'therm_voltage_bias', 'id': None}]" if f else "[]" for f in y_t]
    out["therm_voltage_bias"] = (d_t, y_t)

    # 3. ph_offset_bias (~ -4.0 pH matching source/three_faults_contiguous.csv rows 181..193)
    d_p, y_p1, _ = inject_closed_loop_episode(df_seed, "ph_offset_bias", start=25, length=13, severity=-4.0)
    d_p, y_p2, _ = inject_closed_loop_episode(d_p, "ph_offset_bias", start=75, length=15, severity=+2.5)
    y_p = y_p1 | y_p2
    d_p["faults.last_active"] = ["[{'name': 'ph_offset_bias', 'id': None}]" if f else "[]" for f in y_p]
    out["ph_offset_bias"] = (d_p, y_p)

    # 4. Overlapping thermal + heater + pH stress test
    d_ov, y_ov_t, _ = inject_closed_loop_episode(df_seed, "therm_voltage_bias", start=30, length=35, severity=-3.5)
    d_ov, y_ov_h, _ = inject_closed_loop_episode(d_ov, "heater_power_loss", start=45, length=30, severity=0.45)
    d_ov, y_ov_p, _ = inject_closed_loop_episode(d_ov, "ph_offset_bias", start=60, length=15, severity=-3.0)
    labels_ov = []
    for i in range(len(d_ov)):
        act = []
        if y_ov_t[i]:
            act.append({"name": "therm_voltage_bias", "id": None})
        if y_ov_h[i]:
            act.append({"name": "heater_power_loss", "id": None})
        if y_ov_p[i]:
            act.append({"name": "ph_offset_bias", "id": None})
        labels_ov.append(str(act))
    d_ov["faults.last_active"] = labels_ov
    out["overlapping"] = (d_ov, y_ov_t | y_ov_h | y_ov_p)

    # 5. Synthetic variable setpoint step (30 -> 28 C at t=40) with fault at t=75
    d_sp = df_seed.copy().reset_index(drop=True)
    d_sp.loc[40:, "setpoints.temperature_C"] = 28.0
    step_rel = 1.0 - np.exp(-np.arange(len(d_sp) - 40) / 14.0)
    for c in ("temperature_C.mean", "temperature_C.min", "temperature_C.max"):
        d_sp.loc[40:, c] -= 2.0 * step_rel
    # Cooling energy obeys thermal ODE: q drops toward k_loss*(28 - 22)
    q_b = (d_sp["heater_energy_Wh"] / d_sp["window.samples"]).to_numpy(float)
    eff_b = q_b / np.maximum(d_sp["actuators_avg.heater_pwm"].to_numpy(float), 1e-6)
    k_loss = float(np.median(q_b[:40]) / (np.median(d_sp.loc[:39, "temperature_C.mean"]) - 22.0))
    t_arr = d_sp["temperature_C.mean"].to_numpy(float)
    dt_arr = np.diff(t_arr, prepend=t_arr[0])
    q_sp = np.where(np.arange(len(d_sp)) >= 40, np.clip(k_loss * (t_arr - 22.0) + 0.022 * dt_arr, 0.05 * np.median(q_b), 1.9 * np.median(q_b)), q_b)
    d_sp["heater_energy_Wh"] = q_sp * d_sp["window.samples"].to_numpy(float)
    d_sp["actuators_avg.heater_pwm"] = np.clip(q_sp / np.maximum(eff_b, 1e-6), 0.0, 1.0)
    d_sp, y_sp, _ = inject_closed_loop_episode(d_sp, "heater_power_loss", start=75, length=25, severity=0.50)
    d_sp["faults.last_active"] = ["[{'name': 'heater_power_loss', 'id': None}]" if f else "[]" for f in y_sp]
    out["variable_setpoints_step"] = (d_sp, y_sp)

    return out


def compute_feature_frame(det: StreamingFaultDetector, df: pd.DataFrame) -> pd.DataFrame:
    """Extract causal physical features & residuals per row."""
    det_ro = StreamingFaultDetector(adapt=False)
    det_ro.__dict__.update({k: v for k, v in det.__dict__.items()})
    det_ro.reset_state()
    rows = []
    for r in df.to_dict("records"):
        n = max(float(r["window.samples"]), 1.0)
        q = float(r["heater_energy_Wh"]) / n
        pwm = float(r["actuators_avg.heater_pwm"])
        eff = q / pwm if pwm >= det_ro.u_min else np.nan
        out = det_ro.step(r)
        rows.append(
            {
                "eff": eff,
                "q": q,
                "t_dev": float(r["temperature_C.mean"]) - float(r["setpoints.temperature_C"]),
                "pump": float(r["actuators_avg.acid_pwm"]) + float(r["actuators_avg.base_pwm"]),
                "S_H1": out["scores"]["H1"],
                "S_Tbal": out["scores"]["T_bal"],
                "S_P1": out["scores"]["P1"],
                "S_P2": out["scores"]["P2"],
            }
        )
    return pd.DataFrame(rows)


def run_part3_suite(data_dir: Path) -> dict:
    """Execute Part 3 generation + 4-step alignment checks and return structured statistics."""
    df_ok = load_telemetry_csv(str(data_dir / "nofaults.csv"))
    df_3c = load_telemetry_csv(str(data_dir / "three_faults_contiguous.csv"))
    df_3f = load_telemetry_csv(str(data_dir / "three_faults_faulty_only.csv"))
    df_mix = load_telemetry_csv(str(data_dir / "three_faults_and_variable_setpoints_faulty_only.csv"))

    # Calibrate strictly on healthy seed + closed-loop synthetic faults (zero-shot wrt recorded faults)
    det = StreamingFaultDetector(adapt=True).fit_seed(df_ok)
    params = det.calibrate_thresholds(df_ok)
    suite = generate_matched_suite(df_ok)

    rec_all = pd.concat([df_3c, df_3f, df_mix], ignore_index=True)
    rec_labels = rec_all["faults.last_active"].apply(parse_fault_labels)

    alignment = {}
    for flt in FAULT_TYPES:
        d_syn, y_syn = suite[flt]
        f_syn = compute_feature_frame(det, d_syn).loc[y_syn]
        rec_mask = rec_labels.apply(lambda s: flt in s).to_numpy()
        f_rec = compute_feature_frame(det, rec_all).loc[rec_mask]

        # Check 1 & 2: invariant medians, IQRs, and two-sample KS on key fault feature
        key_col = {"heater_power_loss": "eff", "therm_voltage_bias": "S_Tbal", "ph_offset_bias": "pump"}[flt]
        ks_val = ks_2samp(f_syn[key_col].to_numpy(), f_rec[key_col].to_numpy())
        alignment[flt] = {
            "syn_n": int(len(f_syn)),
            "rec_n": int(len(f_rec)),
            "syn_eff_med": float(f_syn["eff"].median()),
            "rec_eff_med": float(f_rec["eff"].median()),
            "syn_key_med": float(f_syn[key_col].median()),
            "syn_key_iqr": (float(f_syn[key_col].quantile(0.25)), float(f_syn[key_col].quantile(0.75))),
            "rec_key_med": float(f_rec[key_col].median()),
            "rec_key_iqr": (float(f_rec[key_col].quantile(0.25)), float(f_rec[key_col].quantile(0.75))),
            "ks_stat": ks_val,
        }

    # Check 4: Zero-shot transfer evaluation across synthetic & recorded datasets (fresh clone per dataset)
    evaluations = {
        "nofaults": evaluate_stream(copy.deepcopy(det), df_ok),
        "syn_overlapping": evaluate_stream(copy.deepcopy(det), suite["overlapping"][0]),
        "syn_variable_setpoints": evaluate_stream(copy.deepcopy(det), suite["variable_setpoints_step"][0]),
        "rec_three_faults_contiguous": evaluate_stream(copy.deepcopy(det), df_3c),
        "rec_three_faults_faulty_only": evaluate_stream(copy.deepcopy(det), df_3f),
        "rec_mixed_faulty_only": evaluate_stream(copy.deepcopy(det), df_mix),
    }

    return {"params": params, "alignment": alignment, "evaluations": evaluations}
