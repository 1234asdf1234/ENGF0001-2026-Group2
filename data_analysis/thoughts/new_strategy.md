# How each anomaly is detected

All three detectors use the same recipe. For a quantity $x_t$ in window $t$, its healthy median $\tilde{x}$ and a robust spread $\hat\sigma = 1.4826\,\mathrm{MAD}(x)$ are learned from the no-fault data. The anomaly score is the distance from the healthy median in units of healthy spread:

$$S_t = \frac{|x_t - \tilde{x}|}{\hat\sigma}$$

A check fires when $S_t > k$ for $m$ consecutive windows. The two numbers $k$ and $m$ are tuned per check, as described at the end.

Window length is never used. The three "2 s" windows in your data hold 10 or 11 samples, like every other window, and their energy is not doubled. So energy is always divided by the sample count $N_t$, not by `window.seconds`.

---

## 1. `heater_power_loss`

**Idea.** A healthy heater delivers a fixed amount of energy per unit of commanded duty. A degraded heater delivers less for the same command.

**Quantity.** With $e_t$ the heater energy in Wh and $u_t$ the average heater duty, the delivery efficiency is

$$\eta_t = \frac{e_t}{N_t\,u_t}, \qquad \text{evaluated only when } u_t \ge u_{\min}.$$

**Healthy observation.** $\eta_t \approx 0.0036$ Wh per sample per unit duty, varying by only about $\pm 2$–$3\%$. Duty and energy move together during normal control.

**Fault signature.** If a fraction $\ell$ of the power is lost, then $\eta_t \to (1-\ell)\,\eta_t$.

**Score.** Only a drop counts, so the score is one-sided:

$$S^{H}_t = \max\!\left(0,\; \frac{\tilde\eta - \eta_t}{\hat\sigma_\eta}\right)$$

A loss $\ell$ gives $S^H \approx \ell\,\tilde\eta/\hat\sigma_\eta$. With a spread near 2%, a 25% loss scores around 12, far above the healthy maximum.

**Independence from the other faults.** A temperature sensor bias changes duty and energy by the same factor, so $\eta_t$ is unchanged. The pH fault does not touch the heater at all.

---

## 2. `therm_voltage_bias`

**Idea.** A voltage bias $b$ shifts the reported temperature, $T_{\text{meas}} = T_{\text{true}} + b$. The controller works from the biased reading, which gives two signatures: a jump at onset and a lasting shift in the heater energy needed.

**(a) Abrupt onset.** In healthy data the mean temperature stays within about 0.03 °C of the setpoint and never moves more than about 0.03 °C between windows. Two scores use this:

$$S^{T1}_t = \frac{\big|(\bar T_t - T^{sp}_t) - \mu_{dev}\big|}{\hat\sigma_{dev}}, \qquad
S^{T2}_t = \frac{\big|(\bar T_t - \bar T_{t-1}) - \mu_{step}\big|}{\hat\sigma_{step}}$$

T1 catches the reading leaving the setpoint, and T2 catches a sudden jump. Both are sensitive but short-lived. Once the controller has pulled the measured value back, they go quiet. Your temperature sensor also quantises in steps of about 0.023 °C, so the spreads are given a floor.

**(b) Sustained bias.** At steady state the heat delivered balances the heat lost:

$$P = hA\,(T_{\text{true}} - T_{amb})$$

The controller holds $T_{\text{meas}} = T^{sp}$, so $T_{\text{true}} = T^{sp} - b$. The required power therefore shifts by $-hA\,b$, which in relative terms is

$$\frac{\Delta q}{q} \approx -\frac{b}{T^{sp} - T_{amb}}, \qquad q_t = \frac{e_t}{N_t}.$$

The score compares delivered energy per sample with the healthy level at that setpoint:

$$S^{T3}_t = \frac{|q_t - \tilde q(T^{sp})|}{\hat\sigma_q}$$

The smallest bias this can detect is roughly

$$|b|_{\min} \approx k\,(T^{sp} - T_{amb})\,\frac{\hat\sigma_q}{\tilde q}.$$

Small biases sit inside the natural $\pm 3.5\%$ spread of delivered energy and cannot be detected this way. How small depends on $k$ and on the ambient temperature.

**Independence from heater loss.** The check uses delivered energy $q_t$, not duty. When the heater weakens, the controller raises duty until the same energy is delivered, so $q_t$ stays put. A heater loss and a sensor bias can therefore be active together and are flagged separately.

The fault is flagged if any of T1, T2, or T3 fires.

---

## 3. `ph_offset_bias`

**Idea.** A pH offset $b$ means $p_{\text{meas}} = p_{\text{true}} + b$. This produces a pH change that the pumps did not cause, and later a controller that doses against an error that does not exist.

**Healthy behaviour.** pH follows a sawtooth:
1. It drifts down slowly.
2. The base pump duty ramps up.
3. A few windows later pH jumps up by as much as +0.12.

Pump duty is never exactly zero. A rule like "pH moved while nothing was dosed" would therefore fail, and the pH change must be explained by the last few windows of pump activity. Define the net pump drive as $n_t = a^{base}_t - a^{acid}_t$ (average duties). The healthy pH step is modelled as

$$\Delta p_t = \bar p_t - \bar p_{t-1} = \beta_0 + \sum_{j=1}^{3}\beta_j\, n_{t-j} + \varepsilon_t.$$

The $\beta$ coefficients come from least squares on healthy data. Residuals larger than $3\hat\sigma$ are dropped and the model is refit once.

**(a) Unexplained step (P1).** After the fit, $\hat\varepsilon_t$ is the residual for window $t$, and $\tilde\varepsilon$ and $\hat\sigma_\varepsilon$ are the median and robust spread of the healthy residuals. A sensor offset appears as a step that the pump history cannot explain:

$$S^{P1}_t = \frac{|\hat\varepsilon_t - \tilde\varepsilon|}{\hat\sigma_\varepsilon}$$

**(b) Runaway pumping (P2).** Once the controller sees the phantom error, it keeps dosing. Pump activity averaged over the last $W = 8$ windows rises above its healthy level:

$$\bar a_t = \frac1W\sum_{i=0}^{W-1}\big(a^{acid}_{t-i} + a^{base}_{t-i}\big), \qquad
S^{P2}_t = \max\!\left(0,\;\frac{\bar a_t - \tilde a}{\hat\sigma_a}\right)$$

**Why `dosing_l` is not used.** In the healthy data it stays near $10^{-5}$ to $10^{-4}$ L regardless of pump duty, so it carries no usable signal. The pump duty columns are used instead.

The fault is flagged if either P1 or P2 fires. Its physics is separate from the thermal loop, so it cannot interfere with the other two detectors.

---

## Setpoint changes and tuning

**Setpoint changes.** For 10 windows after any setpoint change, every check except the heater-efficiency check is switched off. Deviations in that period are legitimate settling, not faults. The heater check can stay on because $\eta$ does not depend on the setpoint.

**Tuning $k$ and $m$.** For each check, only values giving no false positives on the healthy data are allowed, and $\alpha$ can relax that limit:

$$\#\{t:\ \text{check fires on healthy data}\} \le \alpha N$$

Among those values, the pair maximising the F1 score on faulty windows is chosen:

$$F_1 = \frac{2\,TP}{2\,TP + FP + FN}$$

The faulty windows are synthetic injections of each fault, or real labelled faults if you have them. Where several $k$ values score equally, the middle of that range is taken for margin.

The healthy data fixes how many false alarms the thresholds allow, but only injected or labelled faults can say how sensitive the thresholds should be.