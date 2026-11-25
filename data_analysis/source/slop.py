from analysis import df
import numpy as np
import pandas as pd

def detect_anomalies(df: pd.DataFrame, rated_heater_watts: float = 120.0) -> pd.DataFrame:
    out = df.copy()
    dt = out["window.seconds"].replace(0, np.nan)
    # ---------------------------------------------------------
    # 1. HEATER POWER LOSS
    3
    # Compare actual electrical power (Wh -> W) against commanded PWM
    # ---------------------------------------------------------
    actual_watts = (out["heater_energy_Wh"] * 3600.0) / dt
    pwm = out["actuators_avg.heater_pwm"]
    # Only evaluate when PWM is high enough to avoid division-by-zero noise
    active_mask = pwm > 0.10
    implied_rated_watts = np.where(active_mask, actual_watts / pwm, np.nan)
    # Smooth over a short rolling window to ignore 1s PWM phase jitter
    rolling_rated = pd.Series(implied_rated_watts).rolling(5, min_periods=1).median()
    out["anomaly_heater_power_loss"] = rolling_rated < (0.80 * rated_heater_watts)
    # ---------------------------------------------------------
    # 2. THERM VOLTAGE BIAS
    # Check A: Unphysical instantaneous jump / intra-window spread
    # Check B: Steady-state energy mismatch (using ACTUAL Wh, decoupling power loss)
    # ---------------------------------------------------------
    temp_step = out["temperature_C.mean"].diff().abs()
    temp_spread = out["temperature_C.max"] - out["temperature_C.min"]
    # Max physically plausible slew rate (tune based on vessel thermal mass)
    unphysical_temp_jump = (temp_step > 0.25) | (temp_spread > 0.40)
    # Steady-state check: near setpoint, energy required should correlate with T_meas
    near_temp_sp = (out["temperature_C.mean"] - out["setpoints.temperature_C"]).abs() < 0.2
    # Example empirical baseline: expected Wh/s per degree above 22C ambient
    expected_watts = (out["temperature_C.mean"] - 22.0) * 3.5
    energy_residual = (actual_watts - expected_watts).rolling(15, min_periods=5).mean()
    thermal_balance_fault = near_temp_sp & (energy_residual.abs() > 15.0)
    out["anomaly_therm_voltage_bias"] = unphysical_temp_jump | thermal_balance_fault
    # ---------------------------------------------------------
    # 3. PH OFFSET BIAS
    # Check A: pH jumps when no acid/base was recently dosed
    # Check B: Excessive cumulative dosing without expected pH shift
    # ---------------------------------------------------------
    total_dosing = out["dosing_l.acid"] + out["dosing_l.base"]
    # Account for mixing lag (e.g., last 5 windows)
    recent_dosing = total_dosing.rolling(5, min_periods=1).sum()
    ph_step = out["pH.mean"].diff().abs()
    ph_spread = out["pH.max"] - out["pH.min"]
    # pH shouldn't jump when zero reagent has been added recently
    4
    uncommanded_ph_jump = (recent_dosing == 0) & ((ph_step > 0.08) | (ph_spread > 0.15))
    # Cumulative dosing over 30 windows vs net pH change
    net_dose_30 = (out["dosing_l.base"] - out["dosing_l.acid"]).rolling(30, min_periods=10)
    net_ph_change_30 = out["pH.mean"].diff(30)
    # If controller keeps pumping reagent heavily with near-zero net pH movement
    phantom_titration = (net_dose_30.abs() > 0.002) & (net_ph_change_30.abs() < 0.05)
    out["anomaly_ph_offset_bias"] = uncommanded_ph_jump | phantom_titration
    return out

detect_anomalies(df)