import ast
import pandas as pd

# Dummy detection function
def detect(df):
    # add empty lists to predicted_faults column
    df["predicted_faults"] = [[] for _ in range(len(df))]
    return df

# 1. Load the dataset and run your detection function
df = pd.read_csv("three_faults.csv")
df = detect(df)

fault_names = [
    "therm_voltage_bias",
    "heater_power_loss",
    "ph_offset_bias",
]

# 2. Extract the list of actual fault names from 'faults.last_active'
# Each row in 'faults.last_active' looks like: "[{'name': 'heater_power_loss', 'id': None}]"
actual_faults = []
for row_value in df["faults.last_active"]:
    items = ast.literal_eval(row_value)
    names = [item["name"] for item in items]
    actual_faults.append(names)

predicted_faults = df["predicted_faults"].tolist()

# 3. Count TP, FP, TN, FN for each fault type
for fault in fault_names:
    tp = 0
    fp = 0
    tn = 0
    fn = 0

    for actual, predicted in zip(actual_faults, predicted_faults):
        is_actual = fault in actual
        is_predicted = fault in predicted

        if is_actual and is_predicted:
            tp += 1
        elif not is_actual and is_predicted:
            fp += 1
        elif not is_actual and not is_predicted:
            tn += 1
        else:
            fn += 1

    print(f"{fault}: TP={tp}, FP={fp}, TN={tn}, FN={fn}")

# 4. Overall binary counts (any fault active vs. no fault active)
tp = 0
fp = 0
tn = 0
fn = 0

for actual, predicted in zip(actual_faults, predicted_faults):
    has_actual = len(actual) > 0
    has_predicted = len(predicted) > 0

    if has_actual and has_predicted:
        tp += 1
    elif not has_actual and has_predicted:
        fp += 1
    elif not has_actual and not has_predicted:
        tn += 1
    else:
        fn += 1

print(f"\nOverall (Any Fault): TP={tp}, FP={fp}, TN={tn}, FN={fn}")