# Revised LLM Instructions (`thoughts/ai.txt` Rephrased + `< 1 MB` & Zero-Storage Streaming Strategy)

## Part 1: Revised Step-by-Step Instructions for an LLM

### Objective
Design, justify, implement, and validate a causal, windowed anomaly detection pipeline for 1 Hz bioreactor telemetry (bootstrapped from a small `< 1 MB` local seed and executed/adapted online against live MQTT streams on `engf0001.cs.ucl.ac.uk:1883`, topic `bioreactor_sim/<DATA_STREAM>/telemetry/summary`, **without storing stream data locally**).

### Constraints & Data Rules
- **Target fault classes:** `therm_voltage_bias`, `heater_power_loss`, and `ph_offset_bias` (multi-label: multiple faults may be active simultaneously).
- **Forbidden inputs:** Never use `faults.last_active` or `faults.counts.*` as detector inputs or update triggers during detection; use them strictly as ground-truth labels for in-memory evaluation (`TP`, `FP`, `TN`, `FN`).
- **Sample normalization:** Normalize window-accumulated quantities (such as `heater_energy_Wh`) by `window.samples`, not `window.seconds` (because `window.seconds` can occasionally jitter to `2` while holding the same ~10–11 samples without doubling energy).
- **Memory & storage cap (`< 1 MB` local, `0 B` stream storage):** Keep total offline local training/test files strictly under `1 MB`. Never persist live MQTT stream records to disk (disable `dump.csv` logging and avoid unbounded `pd.concat` growth). Process live telemetry in $O(1)$ RAM via a fixed-size sliding ring buffer and discard raw records immediately after scoring and updating streaming statistics.
- **Simplicity & explainability:** Keep algorithms lightweight, interpretable, and suitable for real-time streaming (e.g., rolling statistics, robust MAD/EWMA z-scores, physical ratios, Recursive Least Squares). Every feature, update rule, and threshold must state an explicit physical or statistical rationale.

---

### Phase 1: Fault Characterization, Windowed Detection, and Validation Loop

1. **Inspect & Characterize Fault Signatures (Using Small Local Seeds `< 1 MB`)**
   - Analyze the existing lightweight local snapshots (`data/dump.csv`, `data/data.json`, `data/lots_of_data.json`, `source/data_with_fault.csv`, `source/faulty.csv` — ~180 KB total combined) to establish healthy PID control behavior and the distinguishing signatures of `therm_voltage_bias`, `heater_power_loss`, and `ph_offset_bias`.
   - Examine process variables (`temperature_C.*`, `pH.*`, `rpm.*`), actuator duty cycles (`actuators_avg.*`), energy/dosing (`heater_energy_Wh`, `dosing_l.*`), and `window.samples`. Account for closed-loop PID compensation (which can mask a sensor fault at steady state while shifting actuator commands or energy) and verify that signatures remain decoupled when faults overlap.

2. **Devise Windowed Multi-Label Detection & Online Adaptation Algorithms**
   - Build three decoupled, causal sliding-window detectors (one per fault class) operating on a tiny fixed-length in-memory ring buffer (`maxlen` $\approx 15\text{–}30$ windows) over 1 Hz summary messages.
   - Require consecutive-window persistence ($m$ windows above threshold $k$) to reject single-window quantization and sampling noise while keeping onset latency low.
   - Output independent per-fault predictions so overlapping faults (e.g., `therm_voltage_bias` + `heater_power_loss`) are flagged simultaneously without masking each other.
   - Include **online self-adjustment** (bounded-memory EWMA/Welford or small scalar rolling estimators + Recursive Least Squares) so baseline parameters adapt automatically to live stream drift without storing historical telemetry.

3. **Iterative Testing Loop** *(If any stage fails its acceptance criteria, return to Step 1 and refine the features or logic.)*
   - **Step 3.1 — Propose & Calibrate Algorithm:** Specify exact scoring formulas, initial healthy-seed parameters, online update rules, threshold $k$, and persistence $m$ with physical/statistical justification.
   - **Step 3.2 — Fault Recall Check (Offline Snapshot + Synthetic Injection):** Evaluate against the small local fault snapshots (`source/faulty.csv`, `source/data_with_fault.csv`, `source/dump.csv`) and synthetic fault injections applied in-memory to the healthy seed. Confirm all three fault types are reliably detected (maximizing per-fault recall and $F_1$ against `faults.last_active`) without cross-fault misattribution.
   - **Step 3.3 — Specificity Check (No-Fault Seed):** Evaluate against the healthy baseline seed (`data/dump.csv` or a `<= 300`-row `nofaults` seed). Confirm zero false positives (or false-positive rate $\le \alpha$).
   - **Step 3.4 — Zero-Storage Online Streaming Evaluation & Refinement:** Subscribe directly to `bioreactor_sim/three_faults/telemetry/summary` (and `single_fault`), processing each incoming message causally in memory (`O(1)` buffer, `0` bytes saved to disk). Update running `TP`, `FP`, `TN`, `FN` and onset/clearance lag counters on the fly, verify online parameter adaptation stays stable without poisoning, and refine thresholds or decoupling rules as needed.

---

### Phase 2: Robustness Under Scheduled Setpoint Changes (`variable_setpoints`)

1. **Problem Context**
   - Subscribe to `bioreactor_sim/variable_setpoints/telemetry/summary`, where `setpoints.temperature_C`, `setpoints.pH`, and `setpoints.rpm` undergo deterministic, scheduled step changes (on the UTC hour, repeating on a 24-hour cycle).
   - Following each setpoint step, closed-loop PID controllers drive `temperature_C.mean`, `pH.mean`, and `rpm.mean` toward the new setpoints, causing legitimate transient tracking errors, high slew rates, and elevated actuator duty/energy (`actuators_avg.*`, `heater_energy_Wh`, `dosing_l.*`).

2. **Disambiguation & Parametric Generalization Requirement**
   - Distinguish normal setpoint-chasing transients from genuine hardware/sensor faults using setpoint-invariant physical ratios (e.g., heater efficiency `heater_energy_Wh / (window.samples * actuators_avg.heater_pwm)`), transient settling guards triggered by `setpoints.*` step changes, and a **parametric/online-adapted steady-state baseline** (e.g., fitting energy per sample as a continuous linear/affine function of `setpoints.temperature_C` via Recursive Least Squares rather than storing huge per-setpoint lookup tables).

3. **Two-Stage Validation (Without Large Local Storage)**
   - **Small Offline Manipulated Seed (`< 1 MB`):** First test on a compact offline slice (150–300 rows, or synthetic setpoint steps injected into the healthy seed) for fast, deterministic verification of settling guards and parametric tracking.
   - **Live Zero-Storage MQTT Stream:** Then run online against `bioreactor_sim/variable_setpoints/telemetry/summary` in streaming mode with **no local data logging** (keeping only in-memory confusion-matrix counters and learned model parameters). Allow sufficient stream runtime (potentially up to ~1 hour to observe an hourly UTC setpoint step) while using `faults.last_active` strictly in memory for post-hoc scoring.

---

## Part 2: Data Volume Problem & `< 1 MB` + Zero-Storage Solution

### Why Storing Continuous Data Locally Fails
Measured sizes in this repository show:
- Normalized CSV telemetry (`data/dump.csv`, `source/data_with_fault.csv`): **~375–380 bytes/row** (`46.9 KB` for 124 rows; `76.2 KB` for 200 rows).
- Raw MQTT JSONL telemetry (`data/data.json`): **~690 bytes/entry** (`9.7 KB` for 14 entries).

At 1 Hz (3,600 entries/hour, 86,400 entries/day):
- A single 24-hour `variable_setpoints` stream is **~60 MB raw JSONL** (or **~33 MB CSV**), and continuous multi-stream logging or repeated `pd.concat` / in-memory DataFrame expansion rapidly grows into **hundreds of MB to GB** of disk and RAM—unsuitable for long runs and impossible on constrained hardware (such as an ESP32).
- Conversely, simply truncating a static file to `200` rows (as `source/get_dataframe.py` currently does with `MAX_ENTRIES = 200`) is insufficient if you rely on static offline data for everything, because 200 seconds is shorter than a single fault's 300 s MTBF (missing entire fault types, as seen in `source/data_with_fault.csv` where `therm_voltage_bias` never appeared) and shorter than the 1-hour setpoint interval.

**Solution:** Decouple *offline bootstrap data* (kept strictly **`< 1 MB`**) from *online stream adaptation and evaluation* (**`0 bytes`** of stream telemetry stored on disk).

---

### 1. Local Offline Data Budget (Strictly `< 1 MB` Total)

Only tiny, one-time local seed/smoke-test slices are needed on disk:

| Offline Purpose | Recommended Local Entries | Estimated Disk Size | Why This Small Amount Is Enough |
| :--- | :--- | :--- | :--- |
| **`nofaults` bootstrap seed** | **150–300 rows** (~2.5–5 min) | **~56–115 KB** (CSV) | Sufficient to initialize healthy medians/MADs, fit the 3-lag pH regression (`>= 13` samples needed), and capture 1–2 pH sawtooth cycles. |
| **Synthetic fault & setpoint stress tests** | **0 extra rows** (generated in-memory from seed) | **0 KB** | Injecting synthetic `heater_power_loss`, `therm_voltage_bias`, `ph_offset_bias`, and `setpoints.*` steps into the 150–300 row healthy seed tests all failure modes without storing extra data. |
| **Offline fault smoke-test snapshots** *(optional)* | **200–400 rows** across fault snapshots | **~75–150 KB** (CSV) | Verifies JSON/CSV schema parsing and sanity-checks all 3 fault labels offline (already satisfied by the existing ~180 KB in `data/` + `source/`). |
| **Offline setpoint-step snippet** *(optional)* | **150–300 rows** spanning 1–2 setpoint steps | **~56–115 KB** (CSV) | Rapidly unit-tests settling-guard and parametric baseline behavior before connecting to live MQTT. |
| **Total local dataset footprint** | **~500–1,000 rows total** | **~190–380 KB** (**well under `1 MB`**) | Leaves huge headroom under the `1 MB` cap even if both CSV and JSONL seeds are kept. |

---

### 2. Zero-Storage Online Streaming & Automatic Adjustment Architecture

Instead of saving hours of `three_faults` or `variable_setpoints` telemetry to disk, analyze and adapt **inline as each 1 Hz MQTT message arrives**, keeping only bounded $O(1)$ state in RAM:

1. **Fixed-Size Ring Buffer (`O(1)` RAM, `0 B` Disk):**
   - Replace `pd.concat` + `df.to_csv("dump.csv")` in `source/get_dataframe.py` with a fixed-capacity ring buffer (`collections.deque(maxlen=W)` with $W \approx 15\text{–}30$ windows, **< 20 KB RAM**).
   - Each incoming message computes features (`e_ps`, `eff`, `t_dev`, `t_step`, `dph`, lagged `netpump`, `pump_roll`), runs the detector, updates running evaluation counters (`TP`, `FP`, `TN`, `FN`), and is **immediately evicted** once it ages out of the $W$-window buffer.

2. **Automatic Online Baseline Adjustment (No Stored History Needed):**
   - **Scalar baselines (`eff`, `t_dev`, `t_step`, `ph_res`, `pump`):** Replace batch `np.median` / `MAD` over giant arrays with $O(1)$ streaming updates—either:
     - **EWMA location & scale:** $\mu_t = (1-\alpha_u)\mu_{t-1} + \alpha_u x_t$ and $\sigma_t = (1-\alpha_u)\sigma_{t-1} + \alpha_u \cdot 1.2533\,|x_t - \mu_{t-1}|$ (with floor guards and $\alpha_u \approx 0.005\text{–}0.02$), or
     - **Bounded healthy reservoir / sliding deque** of just the last $M = 120\text{–}200$ *confirmed-clean scalar residuals* per check (~8 KB RAM total), allowing exact median/MAD updates in $O(1)$ space.
   - **pH step regression ($\beta_0 \dots \beta_3$ on lagged `netpump`):** Replace batch `np.linalg.lstsq` over a growing DataFrame with **Recursive Least Squares (RLS)** with forgetting factor $\lambda \in [0.99, 0.999]$ (maintaining only a $4$-element weight vector $\beta$ and a $4\times 4$ covariance matrix $P$ — 20 floats total).
   - **Thermal energy baseline across `variable_setpoints`:** Instead of storing 24 hours (86,400 rows) to build a discrete per-setpoint lookup table `m["e_ps"][sp]`, model steady-state energy per sample parametrically as $q(T^{sp}) \approx \theta_0 + \theta_1\,T^{sp}$ (reflecting $P = hA(T^{sp} - T_{\text{amb}})$) and update $(\theta_0, \theta_1)$ online via 2-parameter RLS. This immediately generalizes to new setpoints after a brief settling period without storing historical tables.

3. **Anti-Poisoning Update Guard (Crucial for Safe Self-Adjustment):**
   - Never adapt baselines blindly on every incoming record, or a sustained fault (e.g., 60 s `heater_power_loss` or `therm_voltage_bias`) will drag the baseline toward the fault!
   - **Rule:** Update baseline estimators (`mu`, `sigma`, RLS weights) **only when**:
     1. The setpoint-settling countdown (`settle = 10` windows after any change in `setpoints.*`) is `0`; and
     2. **All** current detector scores are well below the anomaly threshold (e.g., $\max_c S_t^{(c)} < k_{\text{safe}}$, such as $k_{\text{safe}} = 2.0$, and no fault flag is active).
   - Keep a minimum scale floor (`floors`) so quiet periods cannot collapse $\sigma_t \to 0$.

4. **Zero-Storage Live Evaluation (For `single_fault`, `three_faults`, `variable_setpoints`):**
   - While running live against any labeled MQTT stream, evaluate predictions against `faults.last_active` **in real time per message** and maintain only scalar counters (`TP`, `FP`, `TN`, `FN`, episode count, onset delay sum) per fault class.
   - Because nothing is stored on disk and RAM usage is constant (~tens of KB), the detector can run continuously for 30 minutes (`single_fault`), 1 hour (`three_faults`), or 24+ hours (`variable_setpoints`) to observe every rare/hourly event while keeping local storage strictly `< 1 MB` (only ~200–400 KB of seed/test snapshots and < 10 KB if saving the learned model parameters as JSON).

   ### Phase 1 Part 2 Completion Check (Validated 2026-11-25)

   Status: Completed.

   Validation evidence against Phase 1, Part 2 requirements:
   - Three decoupled causal detectors are implemented for `heater_power_loss`, `therm_voltage_bias`, `ph_offset_bias` in `source/phase1_detector.py`.
   - Sliding-window state is fixed-size (`deque`), giving bounded memory behavior suitable for online use.
   - Consecutive-window persistence (`m`) and per-check thresholds (`k`) are implemented and calibrated.
   - Multi-label output is independent per fault class, so overlapping faults can be flagged simultaneously.
   - Online adaptation is implemented with anti-poisoning guards (adapt only when not settling and no active anomaly), EWMA scalar updates, and RLS updates.

   Execution checks completed in `validation_v1`:
   - `validation_v1/test_local_data.py` passed with zero false positives on `nofaults.csv` and successful local alignment/zero-shot assertions.
   - `validation_v1/test_online_fetch.py` passed. Live MQTT was unavailable in this run (timeout), and the test correctly fell back to replaying recorded JSON payloads through the ported fetch module while verifying pandas ingestion, faulty-topic filtering, and rolling-cap behavior.

   Remaining work for Phase 1 Part 2:
   - None identified from current local validation scope.


---

## Part 3: Systematic Method to Generate Faulty Data from Seed & Verify Alignment with Recorded Faulty Data

### Why Naive Perturbation Is Not Enough
A naive synthetic injector (e.g., multiplying `heater_energy_Wh` by a fixed `0.75` or adding a constant decaying bias to `pH.mean` in `source/analysis_alt.py`) often diverges from real simulator behavior:
- **Empirical observations from local recorded faulty slices** show much richer closed-loop dynamics:
  - In `source/faulty.csv`, `heater_power_loss` reduces delivery efficiency $\eta = e_{\text{ps}} / u_{\text{heater}}$ from `~0.00360` down to **`0.00177–0.00215` (median `0.00180`, ~50% loss)**, not just 25%.
  - In `source/faulty.csv`, `therm_voltage_bias` leaves $\eta$ invariant (**`0.00353–0.00363`**), while `t_dev` shifts to **`-5.24` to `-4.06 °C`** and delivered energy per sample $q = e_{\text{ps}}$ surges from `~0.00167` up to **`0.00253–0.00284`** as the PID drives the heater harder.
  - In `source/data_with_fault.csv` (rows 181–196), `ph_offset_bias` starts with an abrupt step (`pH.mean`: `5.04 -> 0.98`, $\approx -4.06$), activates `base_pwm` (`~0.080`) until `pH_meas` returns to `~5.0` (rows 186–192), and—crucially—when the fault clears at row 193, `pH.mean` jumps to **`8.30`** (`pH.max = 9.18`) because the *true* solution was over-alkalized, triggering heavy `acid_pwm` (`~0.079`) during `faults.last_active == []` recovery (rows 194–196).

If synthetic faults ignore PID compensation, actuator saturation, sign polarity, or post-clearance recovery, thresholds $(k, m)$ tuned on synthetic data will either overfit unrealistically clean step edges or trigger unexpected false positives after real faults clear.

---

### 1. Systematic Closed-Loop Fault Generation Protocol (From `< 1 MB` Healthy Seed)

Start from a clean `< 1 MB` `nofaults` seed (e.g., 150–300 rows) and generate fault episodes parameterized by **polarity**, **physical severity**, **duration** (covering both short 2–5 s bursts and sustained 30–60+ s faults consistent with MTTR = 60 s), and **mid-window onset/clearance**:

1. **`heater_power_loss` (power loss fraction $\ell \in [0.15, 0.60]$)**
   - **Physical law:** True delivered efficiency drops $\eta_t \to (1 - \ell)\eta_t$.
   - **Closed-loop response:** The PID raises commanded `actuators_avg.heater_pwm` toward $\min\!\left(1.0,\; \frac{u_t}{1-\ell}\right)$ to preserve the required heat input $q_t$:
     - *Unsaturated regime* ($u_t / (1-\ell) \le 1$): `actuators_avg.heater_pwm` scales up by $\frac{1}{1-\ell}$ while `heater_energy_Wh` and `temperature_C.*` stay close to baseline.
     - *Saturated regime* ($u_t / (1-\ell) > 1$): `actuators_avg.heater_pwm` clips at `1.0`, `heater_energy_Wh` drops to $(1-\ell)\,\eta_0\,N_t$, and `temperature_C.*` sags below setpoint.
   - **Invariant preserved:** $\eta_t = \frac{\text{heater\_energy\_Wh}}{N_t \cdot \text{heater\_pwm}}$ drops by factor $(1-\ell)$ in both regimes.

2. **`therm_voltage_bias` (sensor bias $b_T \in \pm[0.3, 5.0]\,^\circ\text{C}$)**
   - **Physical law:** $T_{\text{meas}} = T_{\text{true}} + b_T$, while heater efficiency $\eta_t$ stays unchanged.
   - **Closed-loop trajectory over fault interval $[t_0, t_1)$:**
     - *Onset ($t \approx t_0$):* `temperature_C.mean` steps by $b_T$ (and `temperature_C.max - temperature_C.min` widens if onset occurs mid-window).
     - *PID compensation ($t_0 < t < t_1$):* $T_{\text{meas}}$ relaxes exponentially with thermal time constant $\tau_T$ back toward $T_{\text{sp}}$, driving $T_{\text{true}} \to T_{\text{sp}} - b_T$. Consequently, `heater_energy_Wh` and `actuators_avg.heater_pwm` scale **together** by $g = \text{clip}\!\left(1 - \frac{b_T}{T_{\text{sp}} - T_{\text{amb}}},\; 0,\; \frac{1.0}{u_t}\right)$ so $\eta_t$ remains unchanged.
     - *Clearance recovery ($t \ge t_1$, labeled healthy):* When $b_T$ disappears, $T_{\text{meas}}$ jumps by $-b_T$ (exposing the overheated/underheated $T_{\text{true}}$) and decays back toward $T_{\text{sp}}$ with corresponding heater cut-off or boost.

3. **`ph_offset_bias` (sensor offset $b_{\text{pH}} \in \pm[0.2, 4.5]$)**
   - **Physical law:** $\text{pH}_{\text{meas}} = \text{pH}_{\text{true}} + b_{\text{pH}}$.
   - **Closed-loop trajectory over fault interval $[t_0, t_1)$:**
     - *Onset ($t \approx t_0$):* `pH.mean` jumps by $b_{\text{pH}}$ (with widened `pH.max - pH.min` for partial-window onset).
     - *PID titration ($t_0 < t < t_1$):* Controller activates the opposing reagent pump (`base_pwm` when $b_{\text{pH}} < 0$; `acid_pwm` when $b_{\text{pH}} > 0$) proportionally to $(\text{pH}_{\text{sp}} - \text{pH}_{\text{meas}})$ until $\text{pH}_{\text{meas}}$ settles near $\text{pH}_{\text{sp}}$ while $\text{pH}_{\text{true}} \to \text{pH}_{\text{sp}} - b_{\text{pH}}$.
     - *Clearance recovery ($t \ge t_1$, labeled healthy):* Removing $b_{\text{pH}}$ reveals $\text{pH}_{\text{true}} = \text{pH}_{\text{sp}} - b_{\text{pH}}$ (an opposite step $-b_{\text{pH}}$), causing sustained counter-dosing back to $\text{pH}_{\text{sp}}$.

4. **Overlapping & Dynamic-Setpoint Stress Generation**
   - Sample independent onset/duration schedules for all three faults so intervals intentionally overlap (especially `heater_power_loss` + `therm_voltage_bias`, applying thermal power shift $g$ and efficiency loss $(1-\ell)$ together cleanly).
   - Apply the same generator to seeds containing synthetic `setpoints.*` steps (simulating `variable_setpoints` trajectories) to generate faults occurring *before, during, and after* setpoint transitions.

---

### 2. Four-Step Alignment Check Against Recorded Faulty Data (`< 1 MB` Snapshots)

Compare the synthetically generated dataset $\mathcal{D}_{\text{syn}}$ against the recorded faulty snapshots $\mathcal{D}_{\text{rec}}$ (`source/faulty.csv`, `source/data_with_fault.csv`, `source/dump.csv`, or small `< 1 MB` live captures) across four objective criteria:

1. **Physical Invariant & Decoupling Check**
   - Confirm that during synthetic `heater_power_loss`, $\eta_t$ drops by $(1-\ell)$ just as in $\mathcal{D}_{\text{rec}}$, while during `therm_voltage_bias` and `ph_offset_bias`, $\eta_t$ stays within healthy bounds (`±3%` around `~0.0036`).
   - Confirm pump PWM signals (`acid_pwm`, `base_pwm`) drive pH changes in the same direction and lag window (`1–3` windows) as $\mathcal{D}_{\text{rec}}$.

2. **Residual Distribution Alignment (Per Matched Fault Regime)**
   - Extract detector feature vectors ($\eta_t$, $q_t$, $\bar T_t - T^{sp}_t$, $\Delta \bar T_t$, $\Delta \bar p_t$, $\bar a_t$) on faulty segments of $\mathcal{D}_{\text{syn}}$ (at matched severities, e.g., $\ell \approx 0.50$, $b_T \approx -4.9\,^\circ\text{C}$, $b_{\text{pH}} \approx -4.0$) and on $\mathcal{D}_{\text{rec}}$.
   - Compare **median, IQR, and min/max quantiles** (and run a distribution check such as two-sample KS or quantile overlap) so synthetic residuals are neither artificially cleaner nor louder than $\mathcal{D}_{\text{rec}}$.

3. **Temporal Trajectory & Post-Clearance Check**
   - Verify three time-domain phases match $\mathcal{D}_{\text{rec}}$:
     1. *Onset transient:* Initial step + intra-window `max - min` spike;
     2. *Compensated steady state:* `T_meas` / `pH_meas` returning near setpoint while `heater_energy_Wh`, `heater_pwm`, or `acid/base_pwm` stay shifted;
     3. *Clearance recovery:* Opposite jump and decaying actuator effort while `faults.last_active == []`.

4. **Zero-Shot Detector Transfer Gate**
   - Calibrate thresholds $(k, m)$ **solely** on the healthy seed + $\mathcal{D}_{\text{syn}}$, then evaluate **zero-shot (no retuning)** on $\mathcal{D}_{\text{rec}}$.
   - **Pass criterion:** Per-fault recall, precision, and post-clearance false-positive patterns on $\mathcal{D}_{\text{rec}}$ must align with $\mathcal{D}_{\text{syn}}$ (all recorded fault episodes detected, zero cross-fault mislabeling). If transfer fails, refine the synthetic generator's closed-loop dynamics or severity distribution—never overfit thresholds to $\mathcal{D}_{\text{rec}}$.
