To separate normal closed-loop control adjustments from genuine hardware faults, you must exploit **physical cross-validation** (comparing independent measurements of the same physical process) and **closed-loop control residuals** (comparing what the controller *commanded* against what the system *did*).

Because multiple anomalies can occur simultaneously—specifically `therm_voltage_bias` and `heater_power_loss`, which both affect the thermal subsystem—each detector must be decoupled so one fault does not mask or falsely trigger another.

---

### 1. Thermal Subsystem: `therm_voltage_bias` vs. `heater_power_loss`

In a bioreactor or process vessel, normal temperature adjustments occur when `setpoints.temperature_C` changes or ambient heat loss fluctuates. During normal operation, the PID controller modulates `actuators_avg.heater_pwm`, which draws electrical power (`heater_energy_Wh`) to bring `temperature_C.mean` to the setpoint.

To separate a **sensor fault** (`therm_voltage_bias`) from an **actuator/hardware fault** (`heater_power_loss`)—especially when both happen at once—use two orthogonal checks:

#### A. Detecting `heater_power_loss` (Actuator-to-Energy Decoupling)

* **Signals used:** `actuators_avg.heater_pwm`, `heater_energy_Wh`, `window.seconds`
* **Physical Reasoning:** `heater_energy_Wh` is a direct electrical power measurement (voltage $\times$ current $\times$ time), whereas `heater_pwm` is the duty cycle commanded by the controller. Under healthy hardware conditions, actual power delivered $P_{\text{actual}} = \frac{\text{heater\_energy\_Wh} \times 3600}{\text{window.seconds}}$ is strictly proportional to `heater_pwm` ($P_{\text{actual}} \approx P_{\text{rated}} \cdot \text{heater\_pwm}$).
* **Why this isolates the fault:** Because this relationship depends purely on electrical ohms/watts and not on the temperature sensor, a `therm_voltage_bias` has zero effect on it. Similarly, normal PID adjustments change both `heater_pwm` and `heater_energy_Wh` proportionally, leaving the ratio intact.
* **Detection Method:**
* Fit a linear baseline (or track the rolling ratio) of effective rated power when `heater_pwm > 0.05`:

$$\hat{P}_{\text{rated}} = \frac{\text{heater\_energy\_Wh} \times 3600}{\text{window.seconds} \cdot \text{actuators\_avg.heater\_pwm}}$$


* Trigger `heater_power_loss` when `heater_pwm` is active (e.g., $> 0.1$) and $\hat{P}_{\text{rated}}$ drops significantly below its nominal calibrated value (e.g., $> 15\%$ drop or Z-score $< -3$).



#### B. Detecting `therm_voltage_bias` (Thermodynamic & Step-Change Residuals)

* **Signals used:** `temperature_C.mean`, `temperature_C.min`, `temperature_C.max`, `heater_energy_Wh`, `setpoints.temperature_C`, `rpm.mean`
* **Physical Reasoning:** A thermistor voltage bias shifts the reported temperature ($T_{\text{meas}} = T_{\text{true}} + \Delta T_{\text{bias}}$).
* **Normal adjustment:** When `setpoints.temperature_C` increases, `heater_energy_Wh` rises first, and `temperature_C.mean` follows smoothly according to the vessel's thermal time constant ($\frac{dT}{dt} \propto P_{\text{actual}} - h A (T - T_{\text{amb}})$). At steady state, maintaining a higher `setpoints.temperature_C` strictly requires higher steady-state `heater_energy_Wh`.
* **Fault signature:**
1. **Instantaneous step/jump:** An abrupt voltage offset causes `temperature_C.max - temperature_C.min` within a 1-second window (or $\Delta T$ between consecutive windows) to exceed physically possible thermal slew rates given `heater_energy_Wh` and `rpm.mean` (mixing).
2. **Steady-state energy mismatch:** The PID controller drives `temperature_C.mean` to equal `setpoints.temperature_C` ($T_{\text{meas}} \approx T_{\text{set}}$). If a positive voltage bias ($\Delta T_{\text{bias}} > 0$) occurs, the controller thinks the tank is too hot and cuts `heater_energy_Wh` to near zero even though $T_{\text{meas}} = T_{\text{set}} > T_{\text{ambient}}$. Conversely, a negative bias forces abnormally high `heater_energy_Wh` to hold the same apparent setpoint.




* **Why this works even during `heater_power_loss`:** Notice we regress against actual `heater_energy_Wh` rather than `heater_pwm`. Even if the heater loses 50% of its power, `heater_energy_Wh` reflects the *true* thermal joules entering the liquid, keeping the energy-to-temperature balance valid.
* **Detection Method:**
* **Fast check (abrupt bias):** Flag if $\vert{}\Delta T_{\text{mean}}\vert{} > \Delta T_{\text{max\_physical}}$ between adjacent windows without a corresponding spike in `heater_energy_Wh`.
* **Slow/drift check (thermal balance residual):** Predict expected steady-state energy $\hat{E}_{\text{Wh}}$ as a function of `(temperature_C.mean - T_ambient)` and `rpm.mean` (convective cooling). Flag `therm_voltage_bias` when the residual $\vert{} \text{heater\_energy\_Wh} - \hat{E}_{\text{Wh}} \vert{}$ exceeds historical bounds while the system is near setpoint.



---

### 2. pH Subsystem: `ph_offset_bias`

* **Signals used:** `pH.mean`, `pH.min`, `pH.max`, `setpoints.pH`, `actuators_avg.acid_pwm`, `actuators_avg.base_pwm`, `dosing_l.acid`, `dosing_l.base`, `rpm.mean`
* **Physical Reasoning:** pH control is an asymmetric chemical titration process.
* **Normal adjustment:** When `setpoints.pH` changes (or biological activity drifts the pH), the controller pulses `acid_pwm` or `base_pwm`, delivering measurable volumes in `dosing_l.acid` or `dosing_l.base`. `pH.mean` moves monotonically in the direction of the dosed reagent (acid lowers pH, base raises pH) at a rate proportional to dosing volume and mixing (`rpm.mean`), settling near `setpoints.pH` with near-zero steady-state dosing (or a small, steady biological consumption rate).
* **Fault signature (`ph_offset_bias`):** A reference electrode drift or ground loop shifts the reading ($\text{pH}_{\text{meas}} = \text{pH}_{\text{true}} + \Delta \text{pH}_{\text{bias}}$).
1. **Uncommanded step:** `pH.mean` shifts (or `pH.max - pH.min` spikes) when `dosing_l.acid == 0` and `dosing_l.base == 0`.
2. **Runaway titration against a phantom error:** Because the PID loop trusts `pH.mean`, an offset bias makes the controller believe the pH is off-setpoint. It continuously doses acid or base (`dosing_l.acid` or `dosing_l.base` stays elevated), pushing the *true* pH into an extreme buffer region where the titration curve flattens—meaning cumulative dosing volume skyrockets compared to historical buffer capacity, or the controller holds `pH.mean == setpoints.pH` only by continuously fighting a non-existent drift.




* **Detection Method:**
* **Zero-dosing step check:** Track $\Delta \text{pH}_{\text{mean}}$ over windows where `dosing_l.acid == 0` and `dosing_l.base == 0` (and for $k$ lag windows afterward to account for mixing delay, scaled inversely by `rpm.mean`). Any sudden jump or abnormal slew rate is `ph_offset_bias`.
* **Titration / Cumulative Dosing Anomaly:** Maintain a rolling window (e.g., 5–15 minutes) of net reagent added ($\sum \text{dosing\_l.base} - \sum \text{dosing\_l.acid}$) versus net observed pH change ($\Delta \text{pH}_{\text{mean}}$). Flag `ph_offset_bias` if cumulative dosing exceeds normal process consumption while $\Delta \text{pH}_{\text{mean}}$ fails to respond proportionally (or if pH moves opposite to the dosed reagent).



---

### Summary Implementation in Pandas

```python
import numpy as np
import pandas as pd


def detect_anomalies(df: pd.DataFrame, rated_heater_watts: float = 120.0) -> pd.DataFrame:
    out = df.copy()
    dt = out["window.seconds"].replace(0, np.nan)

    # ---------------------------------------------------------
    # 1. HEATER POWER LOSS
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
    uncommanded_ph_jump = (recent_dosing == 0) & ((ph_step > 0.08) | (ph_spread > 0.15))

    # Cumulative dosing over 30 windows vs net pH change
    net_dose_30 = (out["dosing_l.base"] - out["dosing_l.acid"]).rolling(30, min_periods=10).sum()
    net_ph_change_30 = out["pH.mean"].diff(30)
    # If controller keeps pumping reagent heavily with near-zero net pH movement
    phantom_titration = (net_dose_30.abs() > 0.002) & (net_ph_change_30.abs() < 0.05)

    out["anomaly_ph_offset_bias"] = uncommanded_ph_jump | phantom_titration

    return out

```

### Summary of Decoupling Logic

| Anomaly | Primary Residual | Why Normal Control Doesn't Trigger It | Why Concurrent Faults Don't Mask It |
| --- | --- | --- | --- |
| **`heater_power_loss`** | $\frac{\text{heater\_energy\_Wh}}{\text{heater\_pwm}}$ | Normal PID adjustments scale `heater_pwm` and `heater_energy_Wh` together. | Depends only on electrical actuator metrics; completely independent of `temperature_C` and `pH`. |
| **`therm_voltage_bias`** | $\Delta T$ slew rate & $T_{\text{mean}}$ vs. `heater_energy_Wh` | Setpoint ramps produce gradual $\Delta T$ preceded by proportional `heater_energy_Wh`. | Uses measured `heater_energy_Wh` rather than `heater_pwm`, so a degraded heater still reflects true heat input. |
| **`ph_offset_bias`** | $\Delta \text{pH}$ when `dosing_l == 0` & $\frac{\sum \text{dosing\_l}}{\Delta \text{pH}}$ | Normal pH adjustments show `dosing_l` pulses followed by proportional $\Delta \text{pH}$ toward `setpoints.pH`. | Chemical mass balance is physically decoupled from the thermal loop. |

*(Note: Calibration thresholds such as `rated_heater_watts`, thermal gain `3.5`, and dosing limits should be fitted on a known-healthy baseline run of your specific vessel.)*
