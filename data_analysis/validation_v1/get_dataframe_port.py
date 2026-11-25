"""Port of source/get_dataframe.py with minimal change in code style and logic."""
import json
import sys
import threading

import pandas as pd
import paho.mqtt as paho_mqtt
import paho.mqtt.client as mqtt

BROKER = "engf0001.cs.ucl.ac.uk"
TOPIC = "bioreactor_sim/variable_setpoints/telemetry/summary"
MAX_ENTRIES = 200

# This DataFrame contains the most recent MAX_ENTRIES MQTT messages.
df = pd.DataFrame()
disconnected = threading.Event()
_target_messages = None
_received_messages = 0


def append_record(record: dict) -> None:
    """Flatten one decoded JSON message and append it to the rolling DataFrame."""
    global df

    row = pd.json_normalize([record], sep=".")

    # Match the timestamp and fault-count handling in analysis.py.
    for time_col in ("window.start", "window.end"):
        if time_col in row.columns:
            row[time_col] = pd.to_datetime(row[time_col], unit="s", utc=True)

    # Determine whether this record contains any faults.
    last_active = record.get("faults", {}).get("last_active", [])

    # only extract faulty entries with faulty streams, except variable_setpoints
    if "fault" in TOPIC:
        # Faults listed in last_active
        has_active_fault = (
            last_active is not None
            and isinstance(last_active, (list, tuple, set))
            and len(last_active) > 0
        )

        # Fault counts (e.g. heater_power_loss, ph_offset_bias)
        fault_count_columns = [
            col for col in row.columns
            if col.startswith("faults.counts.")
        ]

        has_counted_fault = False
        if fault_count_columns:
            counts = row[fault_count_columns].fillna(0)
            has_counted_fault = (counts > 0).any(axis=None)

        # Ignore records without faults
        if not (has_active_fault or has_counted_fault):
            print(f"Received but ignored due to no faults occur, now have {len(df)} entries.")
            return

    print(f"Received and stored, now have {len(df)} entries")
    # concat aligns columns, so messages with different JSON fields are supported.
    df = pd.concat([df, row], ignore_index=True)

    # Keep only the newest MAX_ENTRIES rows.
    if len(df) > MAX_ENTRIES:
        df = df.iloc[-MAX_ENTRIES:].reset_index(drop=True)


def on_connect(client, userdata, flags, reason_code, *extra):
    """Subscribe once the broker connection succeeds (Paho v1/v2 compatible)."""
    if reason_code == 0:
        result, _ = client.subscribe(TOPIC)
        if result != mqtt.MQTT_ERR_SUCCESS:
            print(f"Could not subscribe to {TOPIC} (error {result}).")
            disconnected.set()
        else:
            print(f"Connected; subscribed to {TOPIC}.")
    else:
        print(f"MQTT connection was rejected (reason code {reason_code}).")
        disconnected.set()


def on_message(client, userdata, msg):
    global _received_messages
    try:
        record = json.loads(msg.payload.decode("utf-8"))
        if not isinstance(record, dict):
            raise ValueError("Expected the MQTT JSON payload to be an object")
        append_record(record)
        _received_messages += 1
        if _target_messages is not None and _received_messages >= _target_messages:
            disconnected.set()

        # execute the analysis function
        ''' fault = analysis.analyse(df)
            '''
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError) as exc:
        print(f"Ignoring invalid JSON message: {exc}")


def on_disconnect(client, userdata, *args):
    # Do not reconnect: the main loop sees this event and exits.
    print("MQTT connection lost; shutting down.")
    disconnected.set()


def main(max_messages: int | None = None, dump_csv: bool = False):
    global df, _target_messages, _received_messages
    df = pd.DataFrame()
    disconnected.clear()
    _target_messages = max_messages
    _received_messages = 0

    # Use the default callback API so this works with Paho 1.x and 2.x.
    client = mqtt.Client()
    client.on_connect = on_connect
    client.on_message = on_message
    client.on_disconnect = on_disconnect

    try:
        client.connect(BROKER, 1883, 60)
    except (OSError, getattr(paho_mqtt, "MQTTException", OSError)) as exc:
        print(f"Could not connect to MQTT broker: {exc}")
        return df

    try:
        # client.loop() processes network traffic but does not automatically
        # reconnect. Any disconnect or loop error terminates this loop.
        while not disconnected.is_set():
            result = client.loop(timeout=1.0)
            if result != mqtt.MQTT_ERR_SUCCESS:
                print(f"MQTT network loop failed (error {result}); shutting down.")
                break
    except KeyboardInterrupt:
        print("Interrupted by user.")
    finally:
        client.disconnect()

    # dump the dataframe for inspection
    if dump_csv:
        df.to_csv("dump.csv", mode="a", index=False, header=False)
    return df


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Provide the stream name.")
        sys.exit(1)
    TOPIC = f"bioreactor_sim/{sys.argv[1]}/telemetry/summary"
    main(dump_csv=True)
