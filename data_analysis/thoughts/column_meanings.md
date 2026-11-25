Here is every flattened column name translated into plain English:

| Flattened Column Name | Plain English Meaning |
| --- | --- |
| **`window.start`** | Window Start Time (Unix timestamp when the measurement window began) |
| **`window.end`** | Window End Time (Unix timestamp when the measurement window ended) |
| **`window.seconds`** | Window Duration in Seconds |
| **`window.samples`** | Number of Raw Sensor Samples Collected in the Window |
| **`temperature_C.mean`** | Average Temperature (°C) |
| **`temperature_C.min`** | Minimum Temperature (°C) |
| **`temperature_C.max`** | Maximum Temperature (°C) |
| **`pH.mean`** | Average pH Level |
| **`pH.min`** | Minimum pH Level |
| **`pH.max`** | Maximum pH Level |
| **`rpm.mean`** | Average Stirrer Speed (Revolutions Per Minute) |
| **`rpm.min`** | Minimum Stirrer Speed (Revolutions Per Minute) |
| **`rpm.max`** | Maximum Stirrer Speed (Revolutions Per Minute) |
| **`actuators_avg.heater_pwm`** | Average Heater Power Duty Cycle (0 to 100% power signal) |
| **`actuators_avg.motor_pwm`** | Average Stirrer Motor Duty Cycle (0 to 100% power signal) |
| **`actuators_avg.acid_pwm`** | Average Acid Pump Duty Cycle (0 to 100% power signal) |
| **`actuators_avg.base_pwm`** | Average Base Pump Duty Cycle (0 to 100% power signal) |
| **`dosing_l.acid`** | Volume of Acid Dispensed (Liters) |
| **`dosing_l.base`** | Volume of Base Dispensed (Liters) |
| **`heater_energy_Wh`** | Electrical Energy Consumed by the Heater (Watt-hours) |
| **`photoevents`** | Number of Detected Light / Optical Events |
| **`setpoints.temperature_C`** | Target Temperature Setpoint (°C) |
| **`setpoints.pH`** | Target pH Setpoint |
| **`setpoints.rpm`** | Target Stirrer Speed Setpoint (RPM) |
| **`faults.last_active`** | List of Currently Active System Faults |
| **`faults.counts`** | Total Count of Faults Triggered (by fault type) |

### Python Dictionary for Automatic Renaming

If you want to apply these readable names directly in Pandas:

```python
PLAIN_ENGLISH_COLUMNS = {
    "window.start": "Window Start Time",
    "window.end": "Window End Time",
    "window.seconds": "Window Duration (s)",
    "window.samples": "Sample Count",
    "temperature_C.mean": "Average Temperature (°C)",
    "temperature_C.min": "Minimum Temperature (°C)",
    "temperature_C.max": "Maximum Temperature (°C)",
    "pH.mean": "Average pH",
    "pH.min": "Minimum pH",
    "pH.max": "Maximum pH",
    "rpm.mean": "Average Stirrer Speed (RPM)",
    "rpm.min": "Minimum Stirrer Speed (RPM)",
    "rpm.max": "Maximum Stirrer Speed (RPM)",
    "actuators_avg.heater_pwm": "Average Heater Duty Cycle",
    "actuators_avg.motor_pwm": "Average Motor Duty Cycle",
    "actuators_avg.acid_pwm": "Average Acid Pump Duty Cycle",
    "actuators_avg.base_pwm": "Average Base Pump Duty Cycle",
    "dosing_l.acid": "Acid Volume Dosed (L)",
    "dosing_l.base": "Base Volume Dosed (L)",
    "heater_energy_Wh": "Heater Energy Consumed (Wh)",
    "photoevents": "Optical Event Count",
    "setpoints.temperature_C": "Target Temperature (°C)",
    "setpoints.pH": "Target pH",
    "setpoints.rpm": "Target Stirrer Speed (RPM)",
    "faults.last_active": "Active Faults",
    "faults.counts": "Fault Counts",
}

df = df.rename(columns=PLAIN_ENGLISH_COLUMNS)

```
