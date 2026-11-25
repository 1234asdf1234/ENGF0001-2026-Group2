"""
Anomaly detection + threshold calibration for the bioreactor window log.

Usage
-----
    df_ok = pd.read_csv("no_fault.csv", index_col=0)      # <= 200 rows, healthy run
    model, params, report = calibrate(df_ok)               # learn baselines + thresholds
    result = detect(df_new, model, params)                 # anomaly_* boolean columns

Key rules (learned from the no-fault data)
------------------------------------------
* window.seconds / window.start / window.end are NOT used. Energy tracks the number of
  samples in a window, not the timestamp gap, so everything is normalised per sample.
* Every check produces a non-negative "robust z-score" (distance from the healthy median in
  units of healthy spread). A check fires when score > k for m consecutive windows.
  k and m are the trainable parameters.
"""
import numpy as np
import pandas as pd

DEFAULTS = dict(pwm_min=0.05, lags=3, dose_win=8, settle=10)
INJECT = dict(heater_loss=0.25, therm_bias_C=0.5, therm_energy_shift=0.10,
              ph_bias=0.3, ph_pump_boost=0.01, seg_len=8, n_seg=3)
FAULTS = {
    "heater_power_loss": ["H1"],
    "therm_voltage_bias": ["T1", "T2", "T3"],
    "ph_offset_bias": ["P1", "P2"],
}


# ----------------------------------------------------------------------------- features
def _scale(x, floor):
    x = pd.Series(x).dropna().to_numpy(float)
    if len(x) == 0:
        return floor
    return max(1.4826 * np.median(np.abs(x - np.median(x))), floor)  # robust sigma


def _basic(df, p):
    df = df.reset_index(drop=True)
    d = pd.DataFrame(index=df.index)
    n = df["window.samples"].astype(float).replace(0, np.nan)
    pwm = df["actuators_avg.heater_pwm"]
    d["e_ps"] = df["heater_energy_Wh"] / n                         # energy per sample
    d["eff"] = (d["e_ps"] / pwm).where(pwm >= p["pwm_min"])        # energy per sample per unit duty
    d["t_dev"] = df["temperature_C.mean"] - df["setpoints.temperature_C"]
    d["t_step"] = df["temperature_C.mean"].diff()
    net = df["actuators_avg.base_pwm"] - df["actuators_avg.acid_pwm"]
    d["dph"] = df["pH.mean"].diff()
    for L in range(1, p["lags"] + 1):
        d[f"net{L}"] = net.shift(L)
    pump = df["actuators_avg.acid_pwm"] + df["actuators_avg.base_pwm"]
    d["pump"] = pump.rolling(p["dose_win"], min_periods=3).mean()
    d["sp_t"] = df["setpoints.temperature_C"]
    return d


def fit_baseline(df, **kw):
    """Learn healthy medians / spreads from a no-fault frame."""
    p = {**DEFAULTS, **kw}
    d = _basic(df, p)
    m = {"p": p}
    m["eff"] = (d.eff.median(), _scale(d.eff, 0.01 * abs(d.eff.median())))
    m["t_dev"] = (d.t_dev.median(), _scale(d.t_dev, 0.01))
    m["t_step"] = (d.t_step.median(), _scale(d.t_step, 0.01))
    m["e_ps"] = {sp: (g.median(), _scale(g, 0.01 * abs(g.median())))
                 for sp, g in d.e_ps.groupby(d.sp_t)}
    m["pump"] = (d.pump.median(), _scale(d.pump, 0.1 * abs(d.pump.median()) + 1e-5))

    # pH step explained by the last few windows of net pump duty (base - acid)
    cols = [f"net{L}" for L in range(1, p["lags"] + 1)]
    ok = d[cols + ["dph"]].dropna()
    if len(ok) >= len(cols) + 10:
        X = np.column_stack([np.ones(len(ok)), ok[cols].to_numpy(float)])
        y = ok["dph"].to_numpy(float)
        beta = np.linalg.lstsq(X, y, rcond=None)[0]
        for _ in range(2):                                          # drop outliers, refit
            r = y - X @ beta
            keep = np.abs(r - np.median(r)) <= 3 * _scale(r, 1e-6)
            beta = np.linalg.lstsq(X[keep], y[keep], rcond=None)[0]
        r = y - X @ beta
        m["r2"] = 1 - r.var() / max(y.var(), 1e-12)
    else:
        beta = np.r_[ok["dph"].mean() if len(ok) else 0.0, np.zeros(len(cols))]
        r = ok["dph"].to_numpy(float) - beta[0] if len(ok) else np.zeros(1)
        m["r2"] = 0.0
    m["beta"] = beta
    m["ph_res"] = (np.median(r), _scale(r, 0.005))
    return m


def scores(df, m):
    """Non-negative anomaly score per check (bigger = more abnormal)."""
    p = m["p"]
    df = df.reset_index(drop=True)
    d = _basic(df, p)
    s = pd.DataFrame(index=df.index)
    mu, sc = m["eff"]
    s["H1"] = ((mu - d.eff) / sc).clip(lower=0)                     # efficiency DROP only
    mu, sc = m["t_dev"]
    s["T1"] = ((d.t_dev - mu) / sc).abs()
    mu, sc = m["t_step"]
    s["T2"] = ((d.t_step - mu) / sc).abs()
    e = pd.Series(np.nan, index=df.index)                           # unknown setpoint -> no check
    for sp, (mu, sc) in m["e_ps"].items():
        msk = np.isclose(d.sp_t, sp)
        e[msk] = (d.e_ps[msk] - mu).abs() / sc
    s["T3"] = e
    cols = [f"net{L}" for L in range(1, p["lags"] + 1)]
    X = np.column_stack([np.ones(len(d)), d[cols].to_numpy(float)])
    res = d["dph"].to_numpy(float) - X @ m["beta"]
    mu, sc = m["ph_res"]
    s["P1"] = np.abs(res - mu) / sc
    mu, sc = m["pump"]
    s["P2"] = ((d.pump - mu) / sc).clip(lower=0)
    # legitimate setpoint changes -> suppress everything except the heater-efficiency check
    chg = ((df["setpoints.temperature_C"].diff().abs() > 0) | (df["setpoints.pH"].diff().abs() > 0)
           | (df["setpoints.rpm"].diff().abs() > 0))
    settle = chg.astype(int).rolling(p["settle"], min_periods=1).max().astype(bool)
    s.loc[settle.to_numpy(), [c for c in s.columns if c != "H1"]] = 0
    return s.fillna(0)


def _flag(x, k, m):
    a = pd.Series(np.asarray(x) > k).astype(int)
    return (a.rolling(m, min_periods=m).min().fillna(0) > 0).to_numpy()


def detect(df, m, params):
    """params = {check: (k, m)}. Returns df + anomaly_<fault> columns + score_<check> columns."""
    s = scores(df, m)
    f = pd.DataFrame({c: _flag(s[c], k, mm) for c, (k, mm) in params.items()}, index=s.index)
    out = df.reset_index(drop=True).copy()
    for fault, cs in FAULTS.items():
        out["anomaly_" + fault] = f[cs].any(axis=1)
    return out.join(s.add_prefix("score_"))


# ----------------------------------------------------------------------------- training
def inject(df, fault, cfg=INJECT):
    """Corrupt a copy of healthy data with a synthetic fault; returns (frame, label array)."""
    d = df.reset_index(drop=True).copy()
    n, L = len(d), cfg["seg_len"]
    y = np.zeros(n, bool)
    for st in np.linspace(8, max(9, n - L - 2), cfg["n_seg"]).astype(int):
        idx = np.arange(st, min(st + L, n))
        y[idx] = True
        if fault == "heater_power_loss":
            d.loc[idx, "heater_energy_Wh"] *= (1 - cfg["heater_loss"])
        elif fault == "therm_voltage_bias":
            g = 1 + cfg["therm_energy_shift"]              # delivered power shifts, efficiency unchanged
            d.loc[idx, "heater_energy_Wh"] *= g
            d.loc[idx, "actuators_avg.heater_pwm"] *= g
            first = idx[:3]                                  # controller pulls reading back after onset
            for c in ["temperature_C.mean", "temperature_C.min", "temperature_C.max"]:
                d.loc[first, c] += cfg["therm_bias_C"]
        elif fault == "ph_offset_bias":
            off = cfg["ph_bias"] * 0.7 ** np.arange(len(idx))
            for c in ["pH.mean", "pH.min", "pH.max"]:
                d.loc[idx, c] += off
            d.loc[idx, "actuators_avg.acid_pwm"] += cfg["ph_pump_boost"]
    return d, y


def _f1(flag, y):
    tp, fp, fn = (flag & y).sum(), (flag & ~y).sum(), (~flag & y).sum()
    return 2 * tp / max(2 * tp + fp + fn, 1)


def calibrate(df_ok, alpha=0.0, cfg=INJECT, labelled=None,
              k_grid=np.arange(1.0, 20.01, 0.25), m_grid=(1, 2, 3), **fit_kw):
    """
    1. Fit healthy baselines.
    2. For every check pick (k, m) that
         - gives at most alpha * N false positives on the healthy data   (FP constraint)
         - then maximises F1 on faults (synthetic injections, or `labelled` real data
           given as (df_faulty, {fault_name: boolean Series}))
         - ties (plateau of equally good k) -> middle of the plateau, smallest m.
    Returns (model, params, report_df).
    """
    m = fit_baseline(df_ok, **fit_kw)
    s_ok = scores(df_ok, m)
    if labelled is not None:
        df_l, labels = labelled
        s_l = scores(df_l, m)
        frames = {f: (s_l, np.asarray(labels[f], bool)) for f in FAULTS}
    else:
        frames = {}
        for f in FAULTS:
            dd, y = inject(df_ok, f, cfg)
            frames[f] = (scores(dd, m), y)

    params, rows = {}, []
    for fault, cs in FAULTS.items():
        s_f, y = frames[fault]
        for c in cs:
            cand = []
            for mm in m_grid:
                for k in k_grid:
                    fp = int(_flag(s_ok[c], k, mm).sum())
                    if fp > alpha * len(df_ok):
                        continue
                    fl = _flag(s_f[c], k, mm)
                    cand.append((_f1(fl, y), k, mm, fp, (fl & y).sum() / max(y.sum(), 1)))
            best = None
            if cand:
                top = max(c_[0] for c_ in cand)
                tied = [c_ for c_ in cand if c_[0] >= top - 1e-4]
                mm = min(c_[2] for c_ in tied)                      # fastest persistence among ties
                ks = sorted(c_[1] for c_ in tied if c_[2] == mm)
                k = ks[len(ks) // 2]                                # middle of the best plateau
                best = next(c_ for c_ in tied if c_[2] == mm and c_[1] == k)
                best = (best[1], best[2], best[3], best[0], best[4])
            if best is None:                                       # nothing satisfies FP limit
                best = (k_grid[-1], 1, int(_flag(s_ok[c], k_grid[-1], 1).sum()), 0.0, 0.0)
            params[c] = (float(best[0]), int(best[1]))
            rows.append(dict(fault=fault, check=c, k=best[0], m=best[1],
                             false_pos_on_healthy=best[2], f1_on_faults=round(best[3], 3),
                             recall_on_faults=round(best[4], 3),
                             healthy_max_score=round(float(s_ok[c].max()), 2)))
    return m, params, pd.DataFrame(rows)

def change_params(df):
    # import sys
    # df = pd.read_csv(sys.argv[1], index_col=0)
    model, params, rep = calibrate(df)
    print("pH-step regression R^2 on healthy data: %.2f" % model["r2"])
    print(rep.to_string(index=False))
    print("\nparams =", params)


if __name__ == "__main__":
    from pre_read import df
    change_params(df)
