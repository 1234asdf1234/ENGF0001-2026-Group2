"""Phase 1 windowed multi-label anomaly detector with O(1) streaming & online adaptation.

Implements Phase 1, Part 1 of thoughts/ai_revised.md:
- Causal O(1)-RAM sliding ring buffer (zero disk writes for stream records).
- Sample-normalized features (energy / window.samples, never window.seconds).
- Decoupled detectors for heater_power_loss (H), therm_voltage_bias (T), ph_offset_bias (P).
- Anti-poisoning online baseline adaptation (EWMA scalars + RLS pH regression + RLS thermal slope).
- Closed-loop synthetic fault generator & 4-step alignment check against recorded faulty CSVs.
"""
from collections import deque
import ast
import csv
import math
import numpy as np
import pandas as pd

BASE_COLS = [
    "heater_energy_Wh", "photoevents", "window.start", "window.end",
    "window.seconds", "window.samples",
    "temperature_C.mean", "temperature_C.min", "temperature_C.max",
    "pH.mean", "pH.min", "pH.max", "rpm.mean", "rpm.min", "rpm.max",
    "actuators_avg.heater_pwm", "actuators_avg.motor_pwm",
    "actuators_avg.acid_pwm", "actuators_avg.base_pwm",
    "dosing_l.acid", "dosing_l.base",
    "setpoints.temperature_C", "setpoints.pH", "setpoints.rpm",
    "faults.last_active",
]
FAULT_TYPES = ("heater_power_loss", "therm_voltage_bias", "ph_offset_bias")


def load_telemetry_csv(path: str) -> pd.DataFrame:
    """Load any project CSV (including mixed-width headerless/appended logs) into 25 base columns."""
    with open(path, encoding="utf-8") as f:
        rows = [r[:25] for r in csv.reader(f) if r and r[0] != "heater_energy_Wh"]
    df = pd.DataFrame(rows, columns=BASE_COLS)
    num_cols = [c for c in BASE_COLS if c not in ("window.start", "window.end", "faults.last_active")]
    for c in num_cols:
        df[c] = df[c].astype(float)
    df["ts"] = pd.to_datetime(df["window.start"], utc=True)
    return df


def parse_fault_labels(val) -> set[str]:
    if isinstance(val, list):
        return {x["name"] for x in val if isinstance(x, dict) and "name" in x}
    if not isinstance(val, str) or not val.strip():
        return set()
    try:
        items = ast.literal_eval(val)
        return {x["name"] for x in items if isinstance(x, dict) and "name" in x}
    except Exception:
        return set()


def flatten_mqtt_record(record: dict) -> dict:
    """Flatten one raw MQTT summary JSON object into BASE_COLS format in O(1) memory."""
    w = record.get("window", {})
    t = record.get("temperature_C", {})
    ph = record.get("pH", {})
    rpm = record.get("rpm", {})
    act = record.get("actuators_avg", {})
    dose = record.get("dosing_l", {})
    sp = record.get("setpoints", {})
    flt = record.get("faults", {})
    return {
        "heater_energy_Wh": float(record["heater_energy_Wh"]),
        "photoevents": float(record.get("photoevents", 0.0)),
        "window.start": pd.to_datetime(w["start"], unit="s", utc=True),
        "window.end": pd.to_datetime(w["end"], unit="s", utc=True),
        "window.seconds": float(w.get("seconds", 1.0)),
        "window.samples": float(w["samples"]),
        "temperature_C.mean": float(t["mean"]),
        "temperature_C.min": float(t["min"]),
        "temperature_C.max": float(t["max"]),
        "pH.mean": float(ph["mean"]),
        "pH.min": float(ph["min"]),
        "pH.max": float(ph["max"]),
        "rpm.mean": float(rpm["mean"]),
        "rpm.min": float(rpm["min"]),
        "rpm.max": float(rpm["max"]),
        "actuators_avg.heater_pwm": float(act["heater_pwm"]),
        "actuators_avg.motor_pwm": float(act["motor_pwm"]),
        "actuators_avg.acid_pwm": float(act["acid_pwm"]),
        "actuators_avg.base_pwm": float(act["base_pwm"]),
        "dosing_l.acid": float(dose.get("acid", 0.0)),
        "dosing_l.base": float(dose.get("base", 0.0)),
        "setpoints.temperature_C": float(sp["temperature_C"]),
        "setpoints.pH": float(sp["pH"]),
        "setpoints.rpm": float(sp["rpm"]),
        "faults.last_active": flt.get("last_active", []),
    }


def _robust_sigma(x, floor: float) -> float:
    arr = np.asarray(x, dtype=float)
    arr = arr[np.isfinite(arr)]
    if len(arr) == 0:
        return floor
    return max(float(1.4826 * np.median(np.abs(arr - np.median(arr)))), floor)


class StreamingFaultDetector:
    """Causal, O(1)-memory sliding-window detector with anti-poisoning online adaptation."""

    def __init__(
        self,
        t_amb: float = 22.0,
        c_th: float = 0.022,
        u_min: float = 0.05,
        settle_windows: int = 10,
        pump_win: int = 5,
        qres_win: int = 3,
        max_gap_s: float = 5.0,
        ph_sparse_gap_s: float = 20.0,
        k_safe: float = 2.0,
        ewma_alpha: float = 0.01,
        rls_lambda: float = 0.995,
        adapt: bool = True,
        therm_hold_windows: int = 4,
        ph_hold_windows: int = 16,
    ):
        self.t_amb = t_amb
        self.c_th = c_th
        self.u_min = u_min
        self.settle_windows = settle_windows
        self.pump_win = pump_win
        self.qres_win = qres_win
        self.max_gap_s = max_gap_s
        self.ph_sparse_gap_s = ph_sparse_gap_s
        self.k_safe = k_safe
        self.ewma_alpha = ewma_alpha
        self.rls_lambda = rls_lambda
        self.adapt = adapt
        # Small explicit hold windows implement a simple semi-Markov memory:
        # once we have strong evidence, keep the fault active for a short duration
        # so compensated plateaus do not immediately collapse recall.
        self.therm_hold_windows = therm_hold_windows
        self.ph_hold_windows = ph_hold_windows

        # Calibrated baselines (populated by fit_seed)
        self.eff_med = 0.0036
        self.eff_sig = 1e-4
        self.k_loss = 0.000208
        self.theta_th = np.array([0.0, 0.000208])  # [offset, slope] vs (T - T_amb)
        self.P_th = np.eye(2) * 1.0
        self.q_med = 0.00166
        self.qres_med = 0.0
        self.qres_sig = 1e-4
        self.tstep_med = 0.0
        self.tstep_sig = 0.023
        self.tspan_med = 0.14
        self.tspan_sig = 0.04
        self.beta_ph = np.zeros(4)
        self.P_ph = np.eye(4) * 100.0
        self.ph_res_med = 0.0
        self.ph_res_sig = 0.05
        self.ph_dev_med = 0.0
        self.ph_dev_sig = 0.05
        self.pump_med = 0.0024
        self.pump_sig = 0.002

        # Per-check thresholds (k, m) tuned from healthy seed + synthetic faults
        self.params = {
            "H1": (7.0, 1),
            "T_bal": (4.0, 2),
            "T_step": (10.0, 1),
            "P1": (4.5, 1),
            "P2": (4.0, 2),
        }

        # Fixed-capacity streaming state (O(1) RAM)
        self.reset_state()

    def reset_state(self) -> None:
        self.prev_ts = None
        self.prev_T = None
        self.prev_pH = None
        self.prev_sp = None
        self.settle_countdown = 0
        self.netpump_hist = deque(maxlen=3)
        self.pump_hist = deque(maxlen=self.pump_win)
        self.dt_clean_hist = deque(maxlen=5)
        self.qres_hist = deque(maxlen=self.qres_win)
        self.consec = {chk: 0 for chk in self.params}
        # Thermistor-bias hold state.
        self.therm_latch = 0
        # pH-bias hold state + onset polarity for faster recovery clearing.
        self.ph_latch = 0
        self.ph_latch_sign = 0.0
        # Continuous pH fault belief (soft evidence accumulator).
        # This is crucial for non-contiguous faulty-only datasets where strong
        # onset samples may be missing but weak evidence appears repeatedly.
        self.ph_belief = 0.0
        self.ph_noncontig_mode = False

    def fit_seed(self, df_ok: pd.DataFrame) -> "StreamingFaultDetector":
        """Fit initial baselines strictly from a small (< 1 MB) healthy seed."""
        d = df_ok.reset_index(drop=True)
        n = d["window.samples"].astype(float).replace(0, np.nan)
        q = d["heater_energy_Wh"] / n
        pwm = d["actuators_avg.heater_pwm"]
        eff = (q / pwm).where(pwm >= self.u_min)

        self.eff_med = float(eff.median())
        self.eff_sig = _robust_sigma(eff, 0.01 * abs(self.eff_med))

        self.q_med = float(q.median())
        t_above = (d["temperature_C.mean"] - self.t_amb).clip(lower=1.0)
        self.k_loss = float((q / t_above).median())
        self.theta_th = np.array([0.0, self.k_loss], dtype=float)
        # Mild prior covariance around [0, k_loss] so steady-state thermal RLS adapts smoothly
        X_th = np.column_stack([np.ones(len(d)), t_above.to_numpy(float)])
        self.P_th = np.linalg.inv(X_th.T @ X_th + np.diag([10.0, 1.0]))

        tstep = d["temperature_C.mean"].diff()
        tspan = d["temperature_C.max"] - d["temperature_C.min"]
        self.tstep_med = float(tstep.median())
        self.tstep_sig = _robust_sigma(tstep, 0.023)
        self.tspan_med = float(tspan.median())
        self.tspan_sig = _robust_sigma(tspan, 0.023)

        # Thermodynamic residual baseline on healthy seed
        dt_sm = tstep.rolling(5, min_periods=1).median().fillna(0.0)
        dt_sm = dt_sm.where(dt_sm.abs() < 0.3, 0.0)
        q_exp = self.k_loss * (d["temperature_C.mean"] - self.t_amb) + self.c_th * dt_sm
        q_res_raw = q - q_exp
        q_res = q_res_raw.rolling(self.qres_win, min_periods=1).median()
        self.qres_med = float(q_res.median())
        self.qres_sig = _robust_sigma(q_res, 0.06 * abs(self.q_med))

        # pH step regression on 3 prior net-pump windows (with 2x outlier trim)
        net = d["actuators_avg.base_pwm"] - d["actuators_avg.acid_pwm"]
        dph = d["pH.mean"].diff()
        X = np.column_stack([np.ones(len(d)), net.shift(1), net.shift(2), net.shift(3)])
        y = dph.to_numpy(float)
        ok = np.isfinite(X).all(axis=1) & np.isfinite(y)
        if ok.sum() >= 10:
            beta = np.linalg.lstsq(X[ok], y[ok], rcond=None)[0]
            for _ in range(2):
                r = y - X @ beta
                keep = ok & (np.abs(r - np.median(r[ok])) <= 3.0 * _robust_sigma(r[ok], 1e-6))
                if keep.sum() >= 8:
                    beta = np.linalg.lstsq(X[keep], y[keep], rcond=None)[0]
            res = y[ok] - X[ok] @ beta
            # Initialize RLS inverse covariance from healthy design matrix
            xtx = X[ok].T @ X[ok] + 1e-2 * np.eye(4)
            self.P_ph = np.linalg.inv(xtx)
        else:
            beta = np.zeros(4)
            res = np.zeros(1)
            self.P_ph = np.eye(4) * 100.0
        self.beta_ph = beta
        self.ph_res_med = float(np.median(res))
        self.ph_res_sig = _robust_sigma(res, 0.015)

        # Healthy pH level relative to setpoint (used as a static fallback check).
        # This intentionally complements dynamic d(pH) checks for non-contiguous
        # or strongly compensated intervals where derivatives carry little signal.
        ph_dev = d["pH.mean"] - d["setpoints.pH"]
        self.ph_dev_med = float(ph_dev.median())
        self.ph_dev_sig = _robust_sigma(ph_dev, 0.04)

        pump = (d["actuators_avg.acid_pwm"] + d["actuators_avg.base_pwm"]).rolling(self.pump_win, min_periods=3).mean()
        self.pump_med = float(pump.median())
        self.pump_sig = _robust_sigma(pump, max(0.8 * abs(self.pump_med), 1e-4))
        self.init_floors = {
            "eff": 0.5 * self.eff_sig,
            "qres": 0.5 * self.qres_sig,
            "tstep": 0.5 * self.tstep_sig,
            "phres": 0.6 * self.ph_res_sig,
            "phdev": 0.6 * self.ph_dev_sig,
            "pump": 0.7 * self.pump_sig,
        }
        self.reset_state()
        return self

    def calibrate_thresholds(self, df_ok: pd.DataFrame, margin: float = 1.20) -> dict:
        """Calibrate (k, m) using healthy seed (0 FPs) + closed-loop synthetic faults."""
        saved_adapt = self.adapt
        self.adapt = False
        self.reset_state()
        s_ok = pd.DataFrame([self.step(r)["scores"] for r in df_ok.to_dict("records")])

        check_to_fault = {
            "H1": "heater_power_loss",
            "T_bal": "therm_voltage_bias",
            "T_step": "therm_voltage_bias",
            "P1": "ph_offset_bias",
            "P2": "ph_offset_bias",
        }
        syn_cache = {}
        for flt in FAULT_TYPES:
            d_syn, y_syn = generate_closed_loop_faults(df_ok, flt)
            self.reset_state()
            s_syn = pd.DataFrame([self.step(r)["scores"] for r in d_syn.to_dict("records")])
            syn_cache[flt] = (s_syn, y_syn)

        m_choices = {"H1": (1,), "T_bal": (2,), "T_step": (1,), "P1": (1,), "P2": (2,)}
        for chk, flt in check_to_fault.items():
            s_syn, y_syn = syn_cache[flt]
            h_max = float(s_ok[chk].max())
            k_min = max(3.5, round(h_max * margin * 4) / 4)
            best = (k_min, m_choices[chk][0], -1.0)
            for mm in m_choices[chk]:
                for k in np.arange(k_min, 20.01, 0.25):
                    # Require 0 false positives on healthy seed
                    ok_flag = (pd.Series(s_ok[chk] > k).astype(int).rolling(mm, min_periods=mm).min().fillna(0) > 0)
                    if ok_flag.sum() > 0:
                        continue
                    syn_flag = (pd.Series(s_syn[chk] > k).astype(int).rolling(mm, min_periods=mm).min().fillna(0) > 0).to_numpy()
                    tp = int((syn_flag & y_syn).sum())
                    fp = int((syn_flag & ~y_syn).sum())
                    fn = int((~syn_flag & y_syn).sum())
                    f1 = 2 * tp / max(2 * tp + fp + fn, 1)
                    if f1 > best[2] + 1e-4:
                        best = (float(k), int(mm), f1)
            self.params[chk] = (best[0], best[1])

        self.adapt = saved_adapt
        self.reset_state()
        return self.params

    def step(self, row: dict) -> dict:
        """Process one 1-second summary window causally in O(1) RAM."""
        ts = pd.to_datetime(row["window.start"], utc=True)
        contiguous = self.prev_ts is not None and (ts - self.prev_ts).total_seconds() <= self.max_gap_s

        sp = (float(row["setpoints.temperature_C"]), float(row["setpoints.pH"]), float(row["setpoints.rpm"]))
        if self.prev_sp is not None and sp != self.prev_sp:
            self.settle_countdown = self.settle_windows
        elif self.settle_countdown > 0:
            self.settle_countdown -= 1
        in_settle = self.settle_countdown > 0

        if not contiguous:
            # Break temporal differencing/smoothing across recording gaps
            self.dt_clean_hist.clear()
            self.qres_hist.clear()
            self.netpump_hist.clear()
            self.pump_hist.clear()
            for chk in self.consec:
                self.consec[chk] = 0
            if self.prev_ts is not None and (ts - self.prev_ts).total_seconds() >= self.ph_sparse_gap_s:
                self.ph_noncontig_mode = True
        # Decay hold states every step. They will be refreshed by fresh evidence.
        self.therm_latch = max(0, self.therm_latch - 1)
        self.ph_latch = max(0, self.ph_latch - 1)

        n = max(float(row["window.samples"]), 1.0)
        q = float(row["heater_energy_Wh"]) / n
        pwm = float(row["actuators_avg.heater_pwm"])
        T = float(row["temperature_C.mean"])
        tspan = float(row["temperature_C.max"]) - float(row["temperature_C.min"])
        pH = float(row["pH.mean"])
        acid = float(row["actuators_avg.acid_pwm"])
        base = float(row["actuators_avg.base_pwm"])
        net = base - acid
        pump = acid + base

        # --- Heater efficiency check (H1) ---
        if pwm >= self.u_min:
            eff = q / pwm
            s_H1 = max(0.0, (self.eff_med - eff) / self.eff_sig)
        else:
            eff = np.nan
            s_H1 = 0.0

        # --- Temperature bias checks (T_bal + T_step, context weighted) ---
        if contiguous and self.prev_T is not None:
            tstep = T - self.prev_T
            s_Tstep = abs(tstep - self.tstep_med) / self.tstep_sig
            if abs(tstep) < 0.30:
                self.dt_clean_hist.append(tstep)
            else:
                # Step discontinuity: do not smear pre-jump slope into future q_res
                self.dt_clean_hist.clear()
                self.qres_hist.clear()
        else:
            tstep = np.nan
            s_Tstep = 0.0
        # Also catch abrupt intra-window spread even at discontinuity boundaries
        s_Tspan = max(0.0, (tspan - self.tspan_med) / self.tspan_sig)
        s_Tstep_comb = max(s_Tstep, 0.5 * s_Tspan)

        dt_sm = float(np.median(self.dt_clean_hist)) if len(self.dt_clean_hist) else 0.0
        x_th = np.array([1.0, max(T - self.t_amb, 1.0)])
        q_exp = float(x_th @ self.theta_th) + self.c_th * dt_sm
        q_res_raw = q - q_exp
        self.qres_hist.append(q_res_raw)
        q_res_sm = float(np.median(self.qres_hist))
        s_Tbal_raw = abs(q_res_sm - self.qres_med) / self.qres_sig

        # Cross-fault decoupling:
        # therm_voltage_bias keeps heater efficiency roughly invariant, whereas
        # heater_power_loss drives strong efficiency-drop evidence (H1). Rather
        # than fully masking therm detection during overlap, softly down-weight
        # T_bal under high H1 to reduce misattribution and false positives.
        if np.isfinite(eff):
            s_eff = abs(eff - self.eff_med) / self.eff_sig
            therm_weight = 1.0 / (1.0 + 0.35 * max(0.0, s_eff - 1.0))
        else:
            therm_weight = 1.0
        s_Tbal = s_Tbal_raw * therm_weight

        # --- pH offset bias checks (P1 + P2 + hold memory) ---
        if contiguous and self.prev_pH is not None and len(self.netpump_hist) == 3:
            dph = pH - self.prev_pH
            x_ph = np.array([1.0, self.netpump_hist[-1], self.netpump_hist[-2], self.netpump_hist[-3]])
            ph_res = dph - float(x_ph @ self.beta_ph)
            s_P1_dyn = abs(ph_res - self.ph_res_med) / self.ph_res_sig
        else:
            dph, x_ph, ph_res, s_P1_dyn = np.nan, None, np.nan, 0.0

        # Static fallback: pH deviation from setpoint catches cases where derivative
        # information is weak (e.g., non-contiguous windows or compensated plateaus).
        ph_dev = pH - float(row["setpoints.pH"])
        s_P1_static = max(0.0, abs(ph_dev - self.ph_dev_med) / self.ph_dev_sig - 0.5)
        if self.ph_noncontig_mode:
            s_P1 = max(s_P1_dyn, 0.6 * s_P1_static)
        else:
            s_P1 = s_P1_dyn

        self.pump_hist.append(pump)
        if len(self.pump_hist) >= min(3, self.pump_win):
            pump_bar = float(np.mean(self.pump_hist))
            s_P2 = max(0.0, (pump_bar - self.pump_med) / self.pump_sig)
        else:
            pump_bar = pump
            s_P2 = 0.0

        # HSMM-like memory for pH bias:
        # 1) strong onset/effort evidence seeds a hold window,
        # 2) moderate evidence refreshes it,
        # 3) opposite residual + opposite dosing pattern clears it quickly.
        p1_on, p2_on = self.params["P1"][0], self.params["P2"][0]
        if (not in_settle) and (s_P1 > p1_on or s_P2 > p2_on):
            self.ph_latch = self.ph_hold_windows
            if np.isfinite(ph_res) and abs(ph_res) > 0.5 * self.ph_res_sig:
                self.ph_latch_sign = math.copysign(1.0, ph_res)
            elif abs(ph_dev) > 0.5 * self.ph_dev_sig:
                self.ph_latch_sign = math.copysign(1.0, ph_dev)
        elif self.ph_latch > 0 and (s_P1 > 1.5 or s_P2 > 1.5):
            self.ph_latch = min(self.ph_hold_windows, self.ph_latch + 2)

        # Belief-state pH detector:
        # - Converts temporally sparse evidence into a stable latent score.
        # - Strongly gated by dynamic checks to avoid false positives from
        #   healthy pH sawtooth variation around setpoint.
        # - Static pH deviation contributes only when dosing is clearly active.
        if self.ph_noncontig_mode:
            e_dyn = s_P1 / max(p1_on, 1e-6)
            e_eff = s_P2 / max(p2_on, 1e-6)
            e_static = abs(ph_dev - self.ph_dev_med) / max(2.5 * self.ph_dev_sig, 1e-6)
            has_active_dosing = abs(net) > max(0.0015, 1.2 * self.pump_med)
            static_supported = has_active_dosing and e_static > 1.0
            ph_evidence = max(e_dyn, e_eff)
            if static_supported:
                ph_evidence = max(ph_evidence, 0.35 * e_static)
            if in_settle:
                ph_evidence = 0.0
            decay = 0.72 if contiguous else 0.94
            self.ph_belief = min(6.0, decay * self.ph_belief + ph_evidence)
        else:
            # For contiguous healthy/fault streams, keep baseline behavior and
            # disable belief accumulation to preserve strict specificity.
            self.ph_belief = 0.0

        # Clearance pattern: opposite pH movement combined with opposite dosing.
        if self.ph_latch > 0 and self.ph_latch_sign != 0.0 and np.isfinite(ph_res):
            opposite_res = (self.ph_latch_sign * ph_res) < (-1.5 * self.ph_res_sig)
            opposite_dose = (self.ph_latch_sign * net) < -0.01
            if opposite_res and opposite_dose:
                self.ph_latch = 0
                self.ph_latch_sign = 0.0
                self.ph_belief *= 0.35

        scores = {
            "H1": s_H1,
            "T_bal": 0.0 if in_settle else s_Tbal,
            "T_step": 0.0 if in_settle else s_Tstep_comb,
            "P1": 0.0 if in_settle else s_P1,
            "P2": 0.0 if in_settle else s_P2,
        }

        flags = {}
        for chk, (k, m) in self.params.items():
            if scores[chk] > k:
                self.consec[chk] += 1
            else:
                self.consec[chk] = 0
            flags[chk] = self.consec[chk] >= m

        pred_faults = set()
        if flags["H1"]:
            pred_faults.add("heater_power_loss")
        # Therm hold smooths short residual dips after a validated trigger.
        if (not in_settle) and (flags["T_bal"] or flags["T_step"]):
            self.therm_latch = self.therm_hold_windows
        if flags["T_bal"] or flags["T_step"] or self.therm_latch > 0:
            pred_faults.add("therm_voltage_bias")
        # pH hold is the main mechanism for compensated plateau recall.
        if flags["P1"] or flags["P2"] or self.ph_latch > 0 or (self.ph_noncontig_mode and self.ph_belief > 1.35):
            pred_faults.add("ph_offset_bias")

        # --- Anti-poisoning online adaptation ---
        if (
            self.adapt
            and contiguous
            and not in_settle
            and not pred_faults
            and max(scores.values()) < self.k_safe
        ):
            a = self.ewma_alpha
            if np.isfinite(eff):
                self.eff_med = (1 - a) * self.eff_med + a * eff
                self.eff_sig = max((1 - a) * self.eff_sig + a * 1.2533 * abs(eff - self.eff_med), self.init_floors["eff"])
            self.qres_med = (1 - a) * self.qres_med + a * q_res_sm
            self.qres_sig = max((1 - a) * self.qres_sig + a * 1.2533 * abs(q_res_sm - self.qres_med), self.init_floors["qres"])
            if np.isfinite(tstep) and abs(tstep) < 0.30:
                self.tstep_med = (1 - a) * self.tstep_med + a * tstep
                self.tstep_sig = max((1 - a) * self.tstep_sig + a * 1.2533 * abs(tstep - self.tstep_med), self.init_floors["tstep"])
            if np.isfinite(ph_res):
                self.ph_res_med = (1 - a) * self.ph_res_med + a * ph_res
                self.ph_res_sig = max((1 - a) * self.ph_res_sig + a * 1.2533 * abs(ph_res - self.ph_res_med), self.init_floors["phres"])
            self.ph_dev_med = (1 - a) * self.ph_dev_med + a * ph_dev
            self.ph_dev_sig = max((1 - a) * self.ph_dev_sig + a * 1.2533 * abs(ph_dev - self.ph_dev_med), self.init_floors["phdev"])
            self.pump_med = (1 - a) * self.pump_med + a * pump_bar
            self.pump_sig = max((1 - a) * self.pump_sig + a * 1.2533 * abs(pump_bar - self.pump_med), self.init_floors["pump"])
            if x_ph is not None and np.isfinite(ph_res):
                # 4-parameter RLS update for pH step model
                Px = self.P_ph @ x_ph
                denom = self.rls_lambda + float(x_ph @ Px)
                gain = Px / denom
                self.beta_ph = self.beta_ph + gain * ph_res
                self.P_ph = (self.P_ph - np.outer(gain, Px)) / self.rls_lambda
            if abs(dt_sm) < 0.05:
                # 2-parameter RLS update for steady-state thermal baseline q(T) = theta_0 + theta_1*(T - T_amb)
                Px_th = self.P_th @ x_th
                denom_th = self.rls_lambda + float(x_th @ Px_th)
                gain_th = Px_th / denom_th
                err_th = (q - self.c_th * dt_sm) - float(x_th @ self.theta_th)
                self.theta_th = self.theta_th + gain_th * err_th
                self.P_th = (self.P_th - np.outer(gain_th, Px_th)) / self.rls_lambda

        # Advance minimal O(1) state
        self.prev_ts = ts
        self.prev_T = T
        self.prev_pH = pH
        self.prev_sp = sp
        self.netpump_hist.append(net)

        return {"scores": scores, "flags": flags, "faults": pred_faults}


# ----------------------------------------------------------------- Synthetic Generator & Alignment
def generate_closed_loop_faults(
    df_seed: pd.DataFrame,
    fault: str,
    seed: int = 42,
    seg_len: int = 12,
    n_seg: int = 3,
    t_amb: float = 22.0,
) -> tuple[pd.DataFrame, np.ndarray]:
    """Closed-loop physics-consistent fault injection (Part 3, Section 1)."""
    rng = np.random.default_rng(seed)
    d = df_seed.reset_index(drop=True).copy()
    n = len(d)
    y = np.zeros(n, dtype=bool)
    starts = np.linspace(8, max(9, n - seg_len - 12), n_seg).astype(int)

    for j, st in enumerate(starts):
        idx = np.arange(st, min(st + seg_len, n))
        y[idx] = True
        if fault == "heater_power_loss":
            loss = float(rng.uniform(0.25, 0.55))
            u_new = d.loc[idx, "actuators_avg.heater_pwm"] / (1.0 - loss)
            sat = u_new > 1.0
            d.loc[idx, "actuators_avg.heater_pwm"] = np.minimum(u_new, 1.0)
            # Unsaturated keeps delivered energy; saturated scales energy down
            scale_e = np.where(sat, (1.0 - loss) / d.loc[idx, "actuators_avg.heater_pwm"].replace(0, 1.0) * 1.0, 1.0)
            d.loc[idx, "heater_energy_Wh"] *= scale_e
        elif fault == "therm_voltage_bias":
            sign = -1.0 if j % 2 == 0 else 1.0
            bias = sign * float(rng.uniform(1.5, 4.5))
            tau = 4.0
            t_sp = d.loc[idx, "setpoints.temperature_C"].to_numpy(float)
            relax = np.exp(-np.arange(len(idx)) / tau)
            # Onset step + PID pulls T_meas back toward T_sp
            for c in ("temperature_C.mean", "temperature_C.min", "temperature_C.max"):
                d.loc[idx, c] += bias * relax
            # Power shifts toward (T_sp - bias - T_amb)/(T_sp - T_amb) while eff stays constant
            target_g = np.clip(1.0 - bias / np.maximum(t_sp - t_amb, 2.0), 0.1, 1.9)
            ramp = 1.0 - relax
            g = 1.0 + (target_g - 1.0) * ramp
            d.loc[idx, "heater_energy_Wh"] *= g
            d.loc[idx, "actuators_avg.heater_pwm"] = np.clip(d.loc[idx, "actuators_avg.heater_pwm"] * g, 0.0, 1.0)
            # Post-clearance recovery transient (labeled healthy)
            rec_end = min(n, idx[-1] + 7)
            rec = np.arange(idx[-1] + 1, rec_end)
            if len(rec):
                rec_rel = np.exp(-np.arange(len(rec)) / tau)
                for c in ("temperature_C.mean", "temperature_C.min", "temperature_C.max"):
                    d.loc[rec, c] -= bias * rec_rel
        elif fault == "ph_offset_bias":
            sign = -1.0 if j % 2 == 0 else 1.0
            bias = sign * float(rng.uniform(1.0, 4.0))
            tau = 2.5
            relax = np.exp(-np.arange(len(idx)) / tau)
            for c in ("pH.mean", "pH.min", "pH.max"):
                d.loc[idx, c] += bias * relax
            pump_col = "actuators_avg.base_pwm" if bias < 0 else "actuators_avg.acid_pwm"
            opp_col = "actuators_avg.acid_pwm" if bias < 0 else "actuators_avg.base_pwm"
            d.loc[idx, pump_col] += 0.04 * abs(bias) / 2.0 * relax
            # Post-clearance opposite jump + counter-titration (labeled healthy)
            rec = np.arange(idx[-1] + 1, min(n, idx[-1] + 5))
            if len(rec):
                rec_rel = np.exp(-np.arange(len(rec)) / 1.5)
                for c in ("pH.mean", "pH.min", "pH.max"):
                    d.loc[rec, c] -= bias * rec_rel
                d.loc[rec, opp_col] += 0.04 * abs(bias) / 2.0 * rec_rel
    return d, y


class StreamingEvaluator:
    """Zero-storage O(1)-RAM live confusion matrix, episode count, and onset delay tracker."""

    def __init__(self) -> None:
        self.counts = {
            f: {
                "TP": 0, "FP": 0, "FN": 0, "TN": 0,
                "episodes": 0, "detected_episodes": 0,
                "onset_delay_sum": 0,
                "_in_ep": False, "_ep_detected": False, "_ep_age": 0,
            }
            for f in FAULT_TYPES
        }

    def update(self, actual: set[str], pred: set[str]) -> None:
        for f in FAULT_TYPES:
            c = self.counts[f]
            in_a, in_p = f in actual, f in pred
            if in_a and in_p:
                c["TP"] += 1
            elif (not in_a) and in_p:
                c["FP"] += 1
            elif in_a and (not in_p):
                c["FN"] += 1
            else:
                c["TN"] += 1

            if in_a and not c["_in_ep"]:
                c["_in_ep"] = True
                c["_ep_detected"] = False
                c["_ep_age"] = 0
                c["episodes"] += 1
            elif not in_a:
                c["_in_ep"] = False

            if c["_in_ep"]:
                if in_p and not c["_ep_detected"]:
                    c["_ep_detected"] = True
                    c["detected_episodes"] += 1
                    c["onset_delay_sum"] += c["_ep_age"]
                c["_ep_age"] += 1

    def summary(self) -> dict:
        out = {}
        for f, c in self.counts.items():
            tp, fp, fn, tn = c["TP"], c["FP"], c["FN"], c["TN"]
            det_ep = c["detected_episodes"]
            out[f] = {
                "TP": tp, "FP": fp, "FN": fn, "TN": tn,
                "precision": tp / (tp + fp) if (tp + fp) else 0.0,
                "recall": tp / (tp + fn) if (tp + fn) else 0.0,
                "F1": 2 * tp / (2 * tp + fp + fn) if (2 * tp + fp + fn) else 0.0,
                "episodes": c["episodes"],
                "detected_episodes": det_ep,
                "mean_onset_delay": (c["onset_delay_sum"] / det_ep) if det_ep else 0.0,
            }
        return out


def evaluate_stream(detector: StreamingFaultDetector, df: pd.DataFrame, reset: bool = True) -> dict:
    """Evaluate detector causally row-by-row using O(1) memory."""
    if reset:
        detector.reset_state()
    ev = StreamingEvaluator()
    for row in df.to_dict("records"):
        out = detector.step(row)
        ev.update(parse_fault_labels(row["faults.last_active"]), out["faults"])
    return ev.summary()


def stream_mqtt(
    stream_name: str = "three_faults",
    seed_csv: str = "data/nofaults.csv",
    broker: str = "engf0001.cs.ucl.ac.uk",
    port: int = 1883,
    max_messages: int | None = None,
) -> dict:
    """Zero-storage online MQTT scoring & baseline adaptation (Phase 1 Step 3.4 / Part 2)."""
    import json
    import paho.mqtt.client as mqtt

    df_ok = load_telemetry_csv(seed_csv)
    det = StreamingFaultDetector(adapt=True).fit_seed(df_ok)
    det.calibrate_thresholds(df_ok)
    ev = StreamingEvaluator()
    topic = f"bioreactor_sim/{stream_name}/telemetry/summary"
    state = {"n": 0}

    def on_connect(client, userdata, flags, rc, *extra):
        if rc == 0:
            client.subscribe(topic)

    def on_message(client, userdata, msg):
        rec = json.loads(msg.payload.decode("utf-8"))
        flat = flatten_mqtt_record(rec)
        out = det.step(flat)
        ev.update(parse_fault_labels(flat["faults.last_active"]), out["faults"])
        state["n"] += 1
        if max_messages is not None and state["n"] >= max_messages:
            client.disconnect()

    client = mqtt.Client()
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(broker, port, 60)
    client.loop_forever()
    return ev.summary()


def _print_eval(title: str, counts: dict) -> None:
    print(f"\n=== {title} ===")
    for f in FAULT_TYPES:
        c = counts[f]
        print(
            f"  {f:20s}: TP={c['TP']:3d} FP={c['FP']:3d} FN={c['FN']:3d} TN={c['TN']:3d} "
            f"| P={c['precision']:.3f} R={c['recall']:.3f} F1={c['F1']:.3f} "
            f"| ep={c['detected_episodes']}/{c['episodes']} lag={c['mean_onset_delay']:.2f}s"
        )


def run_alignment_check(det: StreamingFaultDetector, df_ok: pd.DataFrame, df_rec: dict[str, pd.DataFrame]) -> None:
    """Part 3, Section 2: check synthetic vs recorded physical invariants & residual ranges."""
    print("\n=== Alignment Check: Synthetic Closed-Loop Faults vs Recorded Faults ===")
    rec_all = pd.concat(df_rec.values(), ignore_index=True)
    rec_all["faults_set"] = rec_all["faults.last_active"].apply(parse_fault_labels)

    for flt in FAULT_TYPES:
        d_syn, y_syn = generate_closed_loop_faults(df_ok, flt)
        syn_f = d_syn.loc[y_syn].copy()
        rec_f = rec_all[rec_all["faults_set"].apply(lambda s: flt in s)].copy()

        for tag, g in [("synthetic", syn_f), ("recorded ", rec_f)]:
            n = g["window.samples"].replace(0, np.nan)
            q = g["heater_energy_Wh"] / n
            eff = (q / g["actuators_avg.heater_pwm"]).where(g["actuators_avg.heater_pwm"] >= det.u_min)
            tdev = g["temperature_C.mean"] - g["setpoints.temperature_C"]
            pump = g["actuators_avg.acid_pwm"] + g["actuators_avg.base_pwm"]
            print(
                f"  {flt:20s} [{tag} n={len(g):3d}]: "
                f"eff_med={eff.median():.5f} q_med={q.median():.5f} "
                f"tdev_iqr=[{tdev.quantile(0.25):+.2f},{tdev.quantile(0.75):+.2f}] "
                f"pump_max={pump.max():.4f}"
            )


def run_phase1_validation() -> None:
    df_ok = load_telemetry_csv("data/nofaults.csv")
    df_3c = load_telemetry_csv("source/three_faults_contiguous.csv")
    df_3f = load_telemetry_csv("source/three_faults_faulty_only.csv")
    df_mix = load_telemetry_csv("source/three_faults_and_variable_setpoints_faulty_only.csv")

    det = StreamingFaultDetector(adapt=True).fit_seed(df_ok)
    params = det.calibrate_thresholds(df_ok)
    print("Step 3.1 Calibrated thresholds (0 FP on healthy seed):", params)

    # Alignment check between synthetic generator and recorded faulty CSVs
    run_alignment_check(det, df_ok, {"3f_contig": df_3c, "3f_faulty": df_3f, "mixed": df_mix})

    # 1. Specificity check on healthy seed
    _print_eval("Step 3.3: Specificity on data/nofaults.csv (124 rows)", evaluate_stream(det, df_ok))

    # 2. Synthetic recall check
    print("\n=== Step 3.2a: Synthetic Closed-Loop Injections on data/nofaults.csv ===")
    for flt in FAULT_TYPES:
        d_syn, y_syn = generate_closed_loop_faults(df_ok, flt)
        d_syn["faults.last_active"] = [f"[{{'name': '{flt}', 'id': None}}]" if flag else "[]" for flag in y_syn]
        c = evaluate_stream(det, d_syn)[flt]
        print(f"  {flt:20s} (syn): TP={c['TP']:3d} FP={c['FP']:3d} FN={c['FN']:3d} | P={c['precision']:.3f} R={c['recall']:.3f} F1={c['F1']:.3f}")

    # 3. Zero-shot evaluation on recorded datasets
    _print_eval("Step 3.2b / 3.4: source/three_faults_contiguous.csv (200 rows, contiguous)", evaluate_stream(det, df_3c))
    _print_eval("Step 3.2c: source/three_faults_faulty_only.csv (42 rows, non-contiguous)", evaluate_stream(det, df_3f))
    _print_eval("Step 3.2d: source/three_faults_and_variable_setpoints_faulty_only.csv (280 rows, mixed non-contiguous)", evaluate_stream(det, df_mix))


if __name__ == "__main__":
    run_phase1_validation()
