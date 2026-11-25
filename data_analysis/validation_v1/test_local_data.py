"""Test 1: Evaluate local copied datasets in validation_v1/data/ and print statistics."""
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.append(str(HERE.parent / "source"))

from phase1_detector import FAULT_TYPES  # noqa: E402
from part3_alignment import run_part3_suite  # noqa: E402


def main() -> None:
    data_dir = HERE / "data"
    res = run_part3_suite(data_dir)

    print("=== Calibrated Parameters (Healthy Seed + Synthetic Only) ===")
    print("params =", res["params"])

    print("\n=== Part 3 Alignment Statistics (Synthetic vs Recorded Faulty Data) ===")
    for flt, st in res["alignment"].items():
        print(
            f"  {flt:20s}: syn_n={st['syn_n']:3d} rec_n={st['rec_n']:3d} "
            f"| eff_med syn={st['syn_eff_med']:.5f} rec={st['rec_eff_med']:.5f} "
            f"| key_med syn={st['syn_key_med']:.4f} rec={st['rec_key_med']:.4f} "
            f"| KS={st['ks_stat']:.3f}"
        )

    print("\n=== Zero-Shot & Local Dataset Evaluation Statistics ===")
    for name, counts in res["evaluations"].items():
        print(f"\n[{name}]")
        for flt in FAULT_TYPES:
            c = counts[flt]
            print(
                f"  {flt:20s}: TP={c['TP']:3d} FP={c['FP']:3d} FN={c['FN']:3d} TN={c['TN']:3d} "
                f"| P={c['precision']:.3f} R={c['recall']:.3f} F1={c['F1']:.3f} "
                f"| ep={c['detected_episodes']}/{c['episodes']} lag={c['mean_onset_delay']:.2f}s"
            )

    # Quantitative assertions
    nf = res["evaluations"]["nofaults"]
    for flt in ("heater_power_loss", "therm_voltage_bias"):
        assert nf[flt]["FP"] == 0, f"Expected 0 FPs on nofaults.csv for {flt}, got {nf[flt]['FP']}"

    # Invariance checks (therm_voltage_bias & ph_offset_bias leave eff ~ 0.00360; heater_power_loss drops eff ~ 50%)
    al = res["alignment"]
    assert 0.0015 <= al["heater_power_loss"]["syn_eff_med"] <= 0.0022
    assert 0.0034 <= al["therm_voltage_bias"]["syn_eff_med"] <= 0.0038
    assert 0.0034 <= al["ph_offset_bias"]["syn_eff_med"] <= 0.0038

    # Zero-shot transfer checks on recorded datasets
    assert res["evaluations"]["rec_three_faults_faulty_only"]["heater_power_loss"]["F1"] >= 0.95
    assert res["evaluations"]["rec_three_faults_faulty_only"]["therm_voltage_bias"]["F1"] >= 0.85
    assert res["evaluations"]["rec_three_faults_contiguous"]["therm_voltage_bias"]["FP"] == 0
    assert res["evaluations"]["rec_mixed_faulty_only"]["heater_power_loss"]["F1"] >= 0.95

    # ph_offset_bias objective: reduce high false negatives on rec_mixed_faulty_only.
    ph_mix = res["evaluations"]["rec_mixed_faulty_only"]["ph_offset_bias"]
    assert ph_mix["recall"] >= 0.60, f"Expected ph_offset_bias recall >= 0.60 on mixed dataset, got {ph_mix['recall']:.3f}"
    assert ph_mix["FN"] <= 60, f"Expected ph_offset_bias FN <= 60 on mixed dataset, got {ph_mix['FN']}"

    print("\nPASS: test_local_data completed with all alignment and zero-shot assertions satisfied.")


if __name__ == "__main__":
    main()
