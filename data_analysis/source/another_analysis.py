"""Robust, data-driven anomaly detectors for JSONL process windows.

Expected input: list[dict] records as in data.json, or a pandas DataFrame
created by pd.json_normalize(records). Fault labels are read from
faults.last_active, when present (T/H/P or full enum names).
"""
from enum import Enum
from collections import deque
import json
import numpy as np
import pandas as pd

class Faults(Enum):
    nofaults = 'N'
    therm_voltage_bias = 'T'
    heater_power_loss = 'H'
    ph_offset_bias = 'P'

FAULT_NAMES = {'T':'therm_voltage_bias','H':'heater_power_loss','P':'ph_offset_bias'}

def load_jsonl(path):
    with open(path, encoding='utf-8') as f:
        records = [json.loads(line) for line in f if line.strip()]
    return pd.json_normalize(records)

def robust_sigma(x, floor=1e-6):
    x = np.asarray(x, dtype=float)
    x = x[np.isfinite(x)]
    if not len(x): return floor
    s = 1.4826 * np.median(np.abs(x - np.median(x)))
    return max(float(s), floor)

def label_set(v):
    if v is None or (isinstance(v,float) and np.isnan(v)): return set()
    if isinstance(v, str):
        try: v=json.loads(v)
        except Exception: v=[v]
    if isinstance(v, dict): v=v.get('last_active', [])
    out=set()
    for x in (v or []):
        x=getattr(x,'value',x)
        if x in FAULT_NAMES: out.add(FAULT_NAMES[x])
        elif x in FAULT_NAMES.values(): out.add(x)
    return out

def _series(df, col, default=np.nan):
    return df[col] if col in df else pd.Series(default, index=df.index, dtype=float)

class AnomalyDetector:
    def __init__(self, k=4.0, m=2, alpha=0.0, u_min=0.05, setpoint_guard=10,
                 ph_rolling=8, floors=None):
        self.k, self.m, self.alpha = k, m, alpha
        self.u_min, self.setpoint_guard, self.ph_rolling = u_min, setpoint_guard, ph_rolling
        self.floors = floors or {'temp_dev':0.023, 'temp_step':0.023, 'q':1e-5,
                                 'eta':1e-7, 'ph_resid':0.005, 'pump':1e-5}
        self.fitted=False

    def _prepare(self, df):
        # Accept flat normalized columns or nested dicts converted by json_normalize.
        d=df.copy().reset_index(drop=True)
        aliases={'temperature_C.mean':'T','pH.mean':'pH','rpm.mean':'rpm',
          'actuators_avg.heater_pwm':'heater','actuators_avg.acid_pwm':'acid',
          'actuators_avg.base_pwm':'base','heater_energy_Wh':'energy',
          'window.samples':'N','setpoints.temperature_C':'Tsp','setpoints.pH':'pHsp'}
        for src,dst in aliases.items():
            if dst not in d and src in d: d[dst]=d[src]
        # Handle manually flattened names / direct columns too.
        for c in ['T','pH','heater','acid','base','energy','N','Tsp','pHsp']:
            if c not in d: d[c]=np.nan
        d['Tsp']=d['Tsp'].fillna(d['T'].median())
        d['pHsp']=d['pHsp'].fillna(d['pH'].median())
        d['N']=d['N'].fillna(1).clip(lower=1)
        d['q']=d['energy']/d['N']
        d['eta']=d['energy']/(d['N']*d['heater'].clip(lower=1e-12))
        d['dev']=d['T']-d['Tsp']
        d['Tstep']=d['T'].diff()
        d['pstep']=d['pH'].diff()
        d['netpump']=d['base']-d['acid']
        d['pump']=d['base']+d['acid']
        # Guard any check (except heater efficiency) for 10 windows after setpoint changes.
        changed=(d['Tsp'].ne(d['Tsp'].shift()) | d['pHsp'].ne(d['pHsp'].shift()))
        guard=np.zeros(len(d), dtype=bool); countdown=0
        for i,chg in enumerate(changed.fillna(False)):
            if chg: countdown=self.setpoint_guard
            guard[i]=countdown>0
            if countdown>0: countdown-=1
        d['guard']=guard
        d['pump_roll']=d['pump'].rolling(self.ph_rolling,min_periods=self.ph_rolling).mean()
        return d

    def fit(self, df, healthy_mask=None):
        d=self._prepare(df)
        if healthy_mask is None:
            if 'faults.last_active' in df.columns:
                healthy_mask=df['faults.last_active'].apply(lambda x: len(label_set(x))==0).to_numpy()
            else: healthy_mask=np.ones(len(d), dtype=bool)
        healthy_mask=np.asarray(healthy_mask,dtype=bool)
        h=d.loc[healthy_mask].copy()
        # Calibration stats: medians and robust spreads from healthy windows only.
        self.eta_med=np.nanmedian(h.loc[h['heater']>=self.u_min,'eta'])
        self.eta_sig=robust_sigma(h.loc[h['heater']>=self.u_min,'eta'],self.floors['eta'])
        self.dev_med=np.nanmedian(h['dev']); self.dev_sig=robust_sigma(h['dev'],self.floors['temp_dev'])
        self.step_med=np.nanmedian(h['Tstep']); self.step_sig=robust_sigma(h['Tstep'],self.floors['temp_step'])
        self.q_med=np.nanmedian(h['q']); self.q_sig=robust_sigma(h['q'],self.floors['q'])
        # pH step regression on three prior net-pump windows, fit healthy observations.
        X=np.column_stack([np.ones(len(d)), d['netpump'].shift(1),d['netpump'].shift(2),d['netpump'].shift(3)])
        y=d['pstep'].to_numpy(); valid=healthy_mask & np.isfinite(X).all(axis=1) & np.isfinite(y)
        self.beta=np.linalg.lstsq(X[valid],y[valid],rcond=None)[0] if valid.sum()>=5 else np.zeros(4)
        residual=y-X@self.beta
        rv=residual[valid]; cutoff=3*robust_sigma(rv,self.floors['ph_resid'])
        valid2=valid & (np.abs(residual-np.nanmedian(rv))<=cutoff)
        if valid2.sum()>=5: self.beta=np.linalg.lstsq(X[valid2],y[valid2],rcond=None)[0]
        residual=y-X@self.beta
        self.ph_res_med=np.nanmedian(residual[healthy_mask & np.isfinite(residual)])
        self.ph_res_sig=robust_sigma(residual[healthy_mask & np.isfinite(residual)],self.floors['ph_resid'])
        pr=h['pump_roll'].dropna()
        self.pump_med=np.nanmedian(pr) if len(pr) else np.nan
        self.pump_sig=robust_sigma(pr,self.floors['pump']) if len(pr) else self.floors['pump']
        self.fitted=True
        return self

    def scores(self, df):
        if not self.fitted: raise RuntimeError('Call fit() first')
        d=self._prepare(df)
        d['H_score']=np.maximum(0,(self.eta_med-d['eta'])/self.eta_sig)
        d['T1_score']=np.abs(d['dev']-self.dev_med)/self.dev_sig
        d['T2_score']=np.abs(d['Tstep']-self.step_med)/self.step_sig
        d['T3_score']=np.abs(d['q']-self.q_med)/self.q_sig
        X=np.column_stack([np.ones(len(d)),d['netpump'].shift(1),d['netpump'].shift(2),d['netpump'].shift(3)])
        pred=X@self.beta
        d['P1_score']=np.abs(d['pstep']-pred-self.ph_res_med)/self.ph_res_sig
        d['P2_score']=np.maximum(0,(d['pump_roll']-self.pump_med)/self.pump_sig)
        d.loc[d['guard'],['T1_score','T2_score','T3_score','P1_score','P2_score']]=np.nan
        d.loc[d['heater']<self.u_min,'H_score']=np.nan
        return d

    def predict(self, df, k=None, m=None):
        k=self.k if k is None else k; m=self.m if m is None else m
        s=self.scores(df); detections=[]
        checks={'H':['H_score'],'T':['T1_score','T2_score','T3_score'],'P':['P1_score','P2_score']}
        # Each individual score must exceed k for m consecutive windows; detectors are OR-combined.
        for code,cols in checks.items():
            active=np.zeros(len(s),bool)
            for col in cols:
                x=s[col].fillna(0).to_numpy()
                active |= pd.Series(x>k).rolling(m,min_periods=m).sum().fillna(0).to_numpy()>=m
            detections.append(active)
        out=[]
        for i in range(len(s)):
            faults=[]
            if detections[0][i]: faults.append(Faults.heater_power_loss)
            if detections[1][i]: faults.append(Faults.therm_voltage_bias)
            if detections[2][i]: faults.append(Faults.ph_offset_bias)
            out.append(faults or [Faults.nofaults])
        return out

# Label-aware validation. Computes per-fault window metrics; label matching is multi-label.
def evaluate(df, predicted):
    actual=[label_set(x) for x in df.get('faults.last_active',pd.Series([[]]*len(df)))]
    pred=[{f.value for f in fs if f is not Faults.nofaults} for fs in predicted]
    rows=[]
    for code,name in FAULT_NAMES.items():
        tp=sum(name in a and name in p for a,p in zip(actual,pred))
        fp=sum(name not in a and name in p for a,p in zip(actual,pred))
        fn=sum(name in a and name not in p for a,p in zip(actual,pred))
        tn=sum(name not in a and name not in p for a,p in zip(actual,pred))
        precision=tp/(tp+fp) if tp+fp else 0
        recall=tp/(tp+fn) if tp+fn else 0
        f1=2*precision*recall/(precision+recall) if precision+recall else 0
        rows.append({'fault':name,'TP':tp,'FP':fp,'FN':fn,'TN':tn,'precision':precision,'recall':recall,'F1':f1})
    return pd.DataFrame(rows)

if __name__=='__main__':
    df = pd.read_csv("dump.csv")
    detector=AnomalyDetector(k=4,m=2).fit(df)
    pred=detector.predict(df)
    print(pd.DataFrame({'prediction':[[f.value for f in x] for x in pred]}))
    print(evaluate(df,pred))
