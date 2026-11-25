import json
from enum import Enum
import pandas as pd

class Faults(Enum):
    nofaults = 'N'
    therm_voltage_bias = "T"
    heater_power_loss = "H"
    ph_offset_bias = "P"

'''
def analyse(df):
    .....
    # return one of Faults for this frame

'''

def load_telemetry(f) -> pd.DataFrame:
    records = []

    file = open(f)

    # JSON Lines (NDJSON): one JSON object per line
    for line in file:
        if line.strip():
            records.append(json.loads(line))

    if not records:
        return pd.DataFrame()

    # Flatten nested dicts (e.g., "temperature_C.mean", "faults.counts.overtemp")
    # Missing keys across variations automatically become NaN
    df = pd.json_normalize(records, sep=".")

    # Convert Unix timestamps to UTC datetime
    for time_col in ["window.start", "window.end"]:
        if time_col in df.columns:
            df[time_col] = pd.to_datetime(df[time_col], unit="s", utc=True)

    # Sort chronologically if start time exists
    if "window.start" in df.columns:
        df = df.sort_values("window.start").reset_index(drop=True)

    # Fill missing fault counts with 0 instead of NaN
    fault_count_cols = [
        c for c in df.columns if c.startswith("faults.counts.")
    ]
    if fault_count_cols:
        df[fault_count_cols] = df[fault_count_cols].fillna(0).astype(int)

    file.close()

    return df


# --- Example Usage ---
# Works for a single file ("data.json") or wildcard batches ("logs/*.jsonl")
df = load_telemetry("lots_of_data.json")
print(df.info())

