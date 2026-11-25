I’m running the local validation test now to get the exact therm_voltage_bias and ph_offset_bias error patterns, then I’ll give a LaTeX-only analysis of why those errors occur in this algorithm and propose stronger alternatives without code.

Ran terminal command:  ./env/bin/python validation_v1/test_local_data.py

\[
\textbf{Local validation run completed.}
\]

From the run of test_local_data.py, the main high-error cases are:

\[
\begin{aligned}
&\text{therm\_voltage\_bias on syn\_overlapping: } P=0.628,\ R=0.771,\ F1=0.692 \\
&\text{therm\_voltage\_bias on rec\_mixed\_faulty\_only: } P=0.953,\ R=0.852,\ F1=0.900,\ FN=21 \\
&\text{ph\_offset\_bias on syn\_overlapping: } P=0.583,\ R=0.467,\ F1=0.519 \\
&\text{ph\_offset\_bias on rec\_three\_faults\_contiguous: } P=0.600,\ R=0.692,\ F1=0.643 \\
&\text{ph\_offset\_bias on rec\_mixed\_faulty\_only: } P=0.864,\ R=0.217,\ F1=0.347,\ FN=137
\end{aligned}
\]

Algorithm references are in phase1_detector.py, part3_alignment.py, and method rationale in methods_v1.md.

\[
\textbf{Why these errors happen (algorithm-level diagnosis)}
\]

\[
\text{1) Thermistor bias detector: high overlap sensitivity + model mismatch}
\]

Current logic combines:
\[
\text{therm\_voltage\_bias} \Leftarrow (T_{\text{step}} > k) \lor (T_{\text{bal}} > k)
\]
with persistence mainly on \(T_{\text{bal}}\).

Reasons for errors:
\[
\begin{aligned}
&\bullet\ \text{In overlapping faults, thermal balance residual } q-\hat q \text{ is not uniquely attributable to sensor bias.}\\
&\bullet\ \text{Heater loss and setpoint transients perturb } q,\ \Delta T,\ \text{and effective }k_{\text{loss}},\ \text{causing confounding.}\\
&\bullet\ \text{Fixed thresholds calibrated on seed+synthetic data face domain shift }(\text{KS}=0.407),\\
&\quad \text{so recall drops on recorded mixed data (many FN).}
\end{aligned}
\]

\[
\text{2) pH offset detector: onset is easy, compensated plateau is hard}
\]

Current logic is:
\[
\text{ph\_offset\_bias} \Leftarrow (P1 > k) \lor (P2 > k)
\]
where \(P1\) is unexplained pH step residual and \(P2\) is rolling pump effort.

Reasons for errors:
\[
\begin{aligned}
&\bullet\ P1\ \text{fires strongly at abrupt onset, but during compensated plateau } \Delta \text{pH}\approx 0,\\
&\quad \text{so residual evidence vanishes although fault remains active.}\\
&\bullet\ P2\ \text{depends on sustained pump activity; when duty settles low, detector misses active faults (high FN).}\\
&\bullet\ \text{Post-clearance recovery can look fault-like and create FP due to delayed chemical dynamics.}
\end{aligned}
\]

\[
\textbf{Better algorithm families to reduce these errors}
\]

\[
\text{A) For therm\_voltage\_bias}
\]

\[
\begin{aligned}
&\text{1. Joint state-space observer (Kalman/UKF with bias state):}\\
&\quad x_t=[T_{\text{true},t},\, b_{T,t}]^\top,\quad T_{\text{meas},t}=T_{\text{true},t}+b_{T,t}+\nu_t.\\
&\quad \text{Detect via posterior }|b_{T,t}|>\tau_b,\ \text{not via single residual threshold.}
\end{aligned}
\]

\[
\begin{aligned}
&\text{2. Generalized likelihood ratio test (GLRT) for persistent bias change:}\\
&\quad \Lambda_t=\max_{\delta\in\mathcal{D}} \log \frac{p(y_{1:t}\mid \delta)}{p(y_{1:t}\mid \delta=0)}.\\
&\quad \text{Better separation of step-like bias from normal transients.}
\end{aligned}
\]

\[
\begin{aligned}
&\text{3. Context-conditional thresholds:}\\
&\quad k_t = k_0 + f(\Delta \text{setpoint},\, \text{heater saturation},\, overlap indicators).\\
&\quad \text{Reduces false alarms during overlap and scheduled thermal movement.}
\end{aligned}
\]

\[
\text{B) For ph\_offset\_bias}
\]

\[
\begin{aligned}
&\text{1. Hidden semi-Markov model (HSMM) with explicit dwell times:}\\
&\quad z_t \in \{\text{healthy},\text{bias-active},\text{recovery}\},\\
&\quad p(z_t\mid z_{t-1})\ \text{with duration priors to keep fault active through plateau.}
\end{aligned}
\]

\[
\begin{aligned}
&\text{2. Dual-sensor consistency surrogate via chemical balance model:}\\
&\quad \widehat{\text{pH}}_{\text{true},t+1}=g(\widehat{\text{pH}}_{\text{true},t},u^{\text{acid}}_t,u^{\text{base}}_t),\\
&\quad r_t=\text{pH}_{\text{meas},t}-\widehat{\text{pH}}_{\text{true},t}.\\
&\quad \text{Persistent }r_t\text{ drift indicates sensor offset even when measured pH appears regulated.}
\end{aligned}
\]

\[
\begin{aligned}
&\text{3. CUSUM/Page-Hinkley on latent bias residual, not raw }\Delta \text{pH}:\\
&\quad S_t=\max(0,S_{t-1}+r_t-\kappa),\quad \text{alarm if }S_t>h.\\
&\quad \text{Improves sensitivity to small persistent offsets and reduces plateau misses.}
\end{aligned}
\]

\[
\textbf{Practical recommendation}
\]

\[
\begin{aligned}
&\text{Short term: keep current fast rules, but add an HSMM-style state latch for pH and}\\
&\text{context-conditional thresholds for thermal residuals.}\\
&\text{Medium term: replace rule-only thermal/pH bias detection with observer-based latent bias estimation.}
\end{aligned}
\]

This should primarily increase ph_offset_bias recall (currently the largest error source) while preserving the strong specificity already seen on healthy data.
