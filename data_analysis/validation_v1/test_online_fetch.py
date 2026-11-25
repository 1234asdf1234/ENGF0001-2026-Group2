"""Test 2: Fetch online MQTT data into pandas using get_dataframe_port (ported from source/get_dataframe.py)."""
from pathlib import Path
from types import SimpleNamespace
import json
import sys

import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.append(str(HERE.parent / "source"))

import get_dataframe_port as gdf  # noqa: E402
from phase1_detector import (  # noqa: E402
    FAULT_TYPES,
    StreamingEvaluator,
    StreamingFaultDetector,
    load_telemetry_csv,
    parse_fault_labels,
)


def run_live_mqtt_fetch(stream: str = "nofaults", max_messages: int = 5) -> pd.DataFrame:
    """Attempt live MQTT fetch via the ported module (requires UCL network / VPN)."""
    gdf.TOPIC = f"bioreactor_sim/{stream}/telemetry/summary"
    return gdf.main(max_messages=max_messages, dump_csv=False)


def replay_mqtt_payloads_through_port(json_path: Path, topic: str) -> pd.DataFrame:
    """Drive the exact on_message -> append_record pipeline from get_dataframe_port with raw MQTT JSON."""
    gdf.TOPIC = topic
    gdf.df = pd.DataFrame()
    gdf.disconnected.clear()
    gdf._target_messages = None
    gdf._received_messages = 0

    with open(json_path, encoding="utf-8") as f:
        lines = [line.strip() for line in f if line.strip()]

    for line in lines:
        msg = SimpleNamespace(payload=line.encode("utf-8"))
        gdf.on_message(None, None, msg)
    return gdf.df.copy()


def main() -> None:
    # 1. Try live broker fetch using ported module (variable_setpoints stores all records)
    live_df = run_live_mqtt_fetch(stream="variable_setpoints", max_messages=3)
    if len(live_df) > 0:
        print(f"LIVE MQTT FETCH SUCCEEDED: fetched {len(live_df)} records into pandas DataFrame.")
        fetched_df = live_df
    else:
        print("Live MQTT broker unreachable from current environment; validating ported on_message -> pandas pipeline using recorded JSONL MQTT payloads.")
        fetched_df = replay_mqtt_payloads_through_port(
            HERE / "data" / "lots_of_data.json",
            topic="bioreactor_sim/variable_setpoints/telemetry/summary",
        )

    assert isinstance(fetched_df, pd.DataFrame) and len(fetched_df) > 0
    assert "window.start" in fetched_df.columns
    assert pd.api.types.is_datetime64_any_dtype(fetched_df["window.start"])
    assert len(fetched_df) <= gdf.MAX_ENTRIES

    # 2. Verify "fault" in TOPIC filtering behavior matches source/get_dataframe.py
    filtered_df = replay_mqtt_payloads_through_port(
        HERE / "data" / "data.json",
        topic="bioreactor_sim/three_faults/telemetry/summary",
    )
    assert len(filtered_df) == 0, "Healthy data.json records should be filtered when 'fault' in TOPIC"
    # Also verify a faulty JSON message passes the filter into DataFrame
    with open(HERE / "data" / "data.json", encoding="utf-8") as f:
        faulty_rec = json.loads(next(line for line in f if line.strip().startswith("{")))
    faulty_rec["faults"] = {"last_active": [{"name": "heater_power_loss", "id": None}], "counts": {"heater_power_loss": 10}}
    gdf.on_message(None, None, SimpleNamespace(payload=json.dumps(faulty_rec).encode("utf-8")))
    assert len(gdf.df) == 1, "Faulty payload should be stored when 'fault' in TOPIC"
    print(f"Filtered faulty-topic fetch check passed: healthy skipped, faulty stored ({len(gdf.df)} entry).")

    # 3. Verify MAX_ENTRIES rolling cap matches source/get_dataframe.py
    with open(HERE / "data" / "data.json", encoding="utf-8") as f:
        sample_line = next(line.strip() for line in f if line.strip().startswith("{"))
    gdf.TOPIC = "bioreactor_sim/variable_setpoints/telemetry/summary"
    gdf.df = pd.DataFrame()
    for _ in range(gdf.MAX_ENTRIES + 15):
        gdf.on_message(None, None, SimpleNamespace(payload=sample_line.encode("utf-8")))
    assert len(gdf.df) == gdf.MAX_ENTRIES, f"Expected cap {gdf.MAX_ENTRIES}, got {len(gdf.df)}"

    # 4. Evaluate fetched DataFrame causally with StreamingFaultDetector (zero disk writes)
    df_seed = load_telemetry_csv(str(HERE / "data" / "nofaults.csv"))
    det = StreamingFaultDetector(adapt=True).fit_seed(df_seed)
    det.calibrate_thresholds(df_seed)
    ev = StreamingEvaluator()
    for row in fetched_df.to_dict("records"):
        row.setdefault("faults.last_active", [])
        out = det.step(row)
        ev.update(parse_fault_labels(row["faults.last_active"]), out["faults"])

    stats = ev.summary()
    print("\n=== Fetched DataFrame Detector Statistics ===")
    for flt in FAULT_TYPES:
        c = stats[flt]
        print(f"  {flt:20s}: TP={c['TP']:3d} FP={c['FP']:3d} FN={c['FN']:3d} TN={c['TN']:3d}")

    print("\nPASS: test_online_fetch verified get_dataframe_port DataFrame ingestion, filtering, rolling cap, and causal evaluation.")


if __name__ == "__main__":
    main()
