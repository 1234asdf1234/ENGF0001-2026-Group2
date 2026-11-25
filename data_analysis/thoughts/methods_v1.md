# Detection Methods (v1)

## Shared Design Principles

**Variables in this section:**
- $t$: current 1-second telemetry window index.
- $N_t$ (`window.samples`): number of raw sensor samples inside window $t$ (~10–11 samples, even when `window.seconds` occasionally reads `2`).
- `heater_energy_Wh`: electrical energy used by the heater during window $t$ (Wh).
- $q_t$: heater energy delivered per sample, $\text{heater\_energy\_Wh} / N_t$ (Wh/sample).
- $S^{(c)}_t$: non-negative robust z-score for check $c$ in window $t$ (distance from healthy median in units of healthy spread).
- $k, m$: anomaly threshold ($S^{(c)}_t > k$) and required number of consecutive exceeding windows ($m$).
- `faults.last_active`, `faults.counts.*`: ground-truth fault labels from the simulator (used strictly for evaluation, never as inputs).

**Method:**
Each incoming 1-second window is scored causally using a small, fixed-size in-memory history (`O(1)` RAM, zero stream records written to disk). Energy is normalized by `window.samples` ($q_t$) rather than `window.seconds`, because `2 s` windows still contain ~10–11 samples with single-window energy. Initial medians, MAD spreads, and regression weights are learned from a small clean seed (`data/nofaults.csv`, 124 rows), then adjusted online (EWMA scalars plus 4-parameter RLS for pH) only on contiguous windows outside setpoint-settling guards when no fault is flagged and all scores sit below `2.0`, with lower spread floors so quiet stretches cannot shrink thresholds.

---

## 1. `heater_power_loss`

**Variables in this section:**
- $q_t$: heater energy delivered per sample ($\text{heater\_energy\_Wh} / N_t$, Wh/sample).
- `actuators_avg.heater_pwm`: average commanded heater duty cycle in window $t$ (`0` to `1`).
- $\eta_t$: heater delivery efficiency, $q_t / \text{actuators\_avg.heater\_pwm}$ (Wh/sample/duty), evaluated only when `actuators_avg.heater_pwm >= 0.05`.
- $\tilde{\eta}, \hat{\sigma}_\eta$: healthy baseline median (`~0.00360`) and robust spread of $\eta_t$.
- $S^{H1}_t$: one-sided efficiency-drop score for check `H1` (`k = 3.5, m = 1`).

**Method:**
A healthy heater delivers a fixed amount of energy per unit of duty cycle ($\eta_t \approx 0.00360$, observed range `0.00348–0.00375`). When the element degrades, $\eta_t$ drops by roughly half in recorded faulty data (`0.00172–0.00215`, median `0.00180`). Check `H1` flags only downward drops:
$$S^{H1}_t = \max\!\left(0,\; \frac{\tilde{\eta} - \eta_t}{\hat{\sigma}_\eta}\right)$$
firing when $S^{H1}_t > 3.5$ for $m = 1$ window. Normal PID changes and thermistor bias move `heater_pwm` and `heater_energy_Wh` together, keeping $\eta_t \approx 0.00360$ unchanged (observed `0.00353–0.00369` during `therm_voltage_bias`), and pH faults do not touch the heater. Across all four recorded datasets, `H1` achieved `0` false positives on `nofaults.csv` and `three_faults_contiguous.csv`, and `F1 = 1.000` on both `three_faults_faulty_only.csv` (`26/26`) and `three_faults_and_variable_setpoints_faulty_only.csv` (`60/60`).

---

## 2. `therm_voltage_bias`

**Variables in this section:**
- $T_{\text{meas},t}$ (`temperature_C.mean`): reported average liquid temperature in window $t$ (°C).
- `temperature_C.min`, `temperature_C.max`: minimum and maximum reported temperature inside window $t$ (°C).
- $T_{\text{true},t}$: true physical liquid temperature (°C).
- $b_T$: thermistor bias offset (°C), so $T_{\text{meas},t} = T_{\text{true},t} + b_T$.
- $T_{\text{sp}}$ (`setpoints.temperature_C`): target temperature setpoint (°C).
- $T_{\text{amb}}$: ambient temperature baseline (`22.0` °C).
- $q_t, \hat{q}_t$: measured energy per sample and energy per sample predicted from $T_{\text{meas},t}$ and $\widehat{\Delta T}_t$ (Wh/sample).
- $k_{\text{loss}}, C_{\text{th}}$: healthy steady-state heat-loss coefficient ($\tilde{q} / (\tilde{T} - T_{\text{amb}})$) and effective thermal capacity (`0.022` Wh/sample/°C).
- $\widehat{\Delta T}_t$: causal smoothed change in `temperature_C.mean` (°C/window), cleared on recording gaps or jumps $\ge 0.30\,^\circ\text{C}$.
- `T_step`, `T_bal`: abrupt jump/spread check (`k = 3.5, m = 1`) and thermodynamic energy-balance check (`k = 5.0, m = 2`).

**Method:**
A voltage bias shifts reported temperature away from reality (observed deviations `-5.24` to `-4.06 °C` in `three_faults_faulty_only.csv`). Because the controller trusts $T_{\text{meas},t}$, delivered energy $q_t$ shifts away from healthy levels (`0.00253–0.00326` vs `~0.00167`) while efficiency $\eta_t$ remains unchanged. Simple setpoint or static energy checks fail here: they produced `200` false alarms on `three_faults_contiguous.csv` during healthy cooling recovery (`30.91 -> 31.59 -> 30.01 °C`) and `26` false alarms during `heater_power_loss` cooling. Instead, `therm_voltage_bias` triggers if either check fires:
1. **`T_step` (abrupt onset):** flags sudden window-to-window jumps in `temperature_C.mean` or intra-window `max - min` spread (`k = 3.5, m = 1`).
2. **`T_bal` (thermodynamic balance):** predicts expected energy from the *reported* temperature and smoothed slope:
   $$\hat{q}_t = k_{\text{loss}}(T_{\text{meas},t} - T_{\text{amb}}) + C_{\text{th}}\,\widehat{\Delta T}_t$$
   When the sensor is healthy ($T_{\text{meas},t} = T_{\text{true},t}$), $q_t \approx \hat{q}_t$ even during cooling recovery or `heater_power_loss`. When biased, $q_t - \hat{q}_t$ shifts strongly (observed 3-window median residual `+0.00127` to `+0.00131` vs healthy `-0.00004` to `+0.00007`). `T_bal` flags when the smoothed residual score exceeds `5.0` for `m = 2` consecutive windows. Because it uses actual $q_t$ rather than `heater_pwm`, it stays independent of `heater_power_loss` unless the heater saturates. This removed all `200` recovery false alarms on `three_faults_contiguous.csv` (`FP = 0`) and achieved `F1 = 0.968` on `three_faults_faulty_only.csv` and `F1 = 0.864` on `three_faults_and_variable_setpoints_faulty_only.csv`.

---

## 3. `ph_offset_bias`

**Variables in this section:**
- $\text{pH}_{\text{meas},t}$ (`pH.mean`), $\text{pH}_{\text{true},t}$: reported and true liquid pH in window $t$.
- $b_{\text{pH}}$: electrode offset bias, so $\text{pH}_{\text{meas},t} = \text{pH}_{\text{true},t} + b_{\text{pH}}$.
- $\Delta \text{pH}_t$: step change in `pH.mean` from window $t-1$ to $t$.
- `actuators_avg.acid_pwm`, `actuators_avg.base_pwm`: average acid and base pump duty cycles (`0` to `1`).
- $n_{t-j}$: net pump drive (`base_pwm - acid_pwm`) $j$ windows prior ($j = 1, 2, 3$).
- $\beta_0 \dots \beta_3$: healthy linear regression coefficients predicting $\Delta \text{pH}_t$ from $n_{t-1}, n_{t-2}, n_{t-3}$.
- `dosing_l.acid`, `dosing_l.base`: dosed volumes (L), unused because healthy logs stay near `1e-5` to `1e-4` L with no usable correlation to duty.
- `P1`, `P2`: unexplained pH-step residual check (`k = 4.0, m = 1`) and 5-window rolling total pump duty (`acid_pwm + base_pwm`) check (`k = 5.5, m = 2`).

**Method:**
Healthy pH follows a natural sawtooth (`~4.78–5.40`) driven by small `base_pwm` pulses (up to `~0.015`). Recorded `ph_offset_bias` in `three_faults_contiguous.csv` (rows 181–196) shows three phases: (1) an abrupt step (`5.04 -> 0.98`) and strong `base_pwm` titration (`~0.080`), (2) a quiet compensated plateau once `pH_meas` returns near `5.0` (rows 186–192, pump `~0.000–0.001`), and (3) a post-clearance overshoot (`8.30`, max `9.18`) with heavy `acid_pwm` (`~0.079`) while `faults.last_active == []`. The detector flags if either check triggers:
1. **`P1` (unexplained pH step):** compares $\Delta \text{pH}_t$ against the prediction from the last 3 windows of net pump drive (evaluated only across contiguous timestamps). It catches uncommanded jumps immediately (`P1 = 74.8` at onset vs healthy max `3.3`).
2. **`P2` (excess pump effort):** tracks the 5-window rolling mean of `acid_pwm + base_pwm` (`P2 = 24.5` during titration vs healthy max `4.6`).

Both checks produce `0` false alarms on `nofaults.csv`, `three_faults_faulty_only.csv`, and normal sawtooth cycles. On `three_faults_contiguous.csv` (`TP = 9, FP = 8, FN = 4`, `Recall = 0.692, F1 = 0.600`), misses correspond to the quiet compensated plateau where readings look healthy, and false positives correspond to post-clearance acid recovery; similarly, on non-contiguous `three_faults_and_variable_setpoints_faulty_only.csv`, precision is `0.882` with `Recall = 0.257` due to stripped plateau rows.
