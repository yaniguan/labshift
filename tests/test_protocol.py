"""Tests for twinstep / stepbot protocol adapters, time-unit validation, and workcell queue export."""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from labshift.checker import check_schedule
from labshift.greedy import solve_greedy_repair
from labshift.protocol import (
    STEPBOT_TIP_ACTION_SECONDS,
    export_workcell_queue,
    instance_from_protocols,
    job_from_protocol,
    job_from_stepbot_spec,
)

STEPBOT_FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "stepbot"


def _twinstep_examples() -> dict[str, list[dict[str, object]]]:
    """Realistic twinstep protocol step lists matching bundled twinstep examples."""
    return {
        "dilution_series": [
            {"type": "transfer", "source": "reservoir:buffer", "dest": "plate1:A1", "volume": 100, "unit": "uL"},
            {"type": "transfer", "source": "reservoir:buffer", "dest": "plate1:A2", "volume": 100, "unit": "uL"},
            {"type": "transfer", "source": "reservoir:dye", "dest": "plate1:A1", "volume": 100, "unit": "uL"},
            {"type": "mix", "well": "plate1:A1", "volume": 100, "unit": "uL", "cycles": 5},
            {"type": "transfer", "source": "plate1:A1", "dest": "plate1:A2", "volume": 100, "unit": "uL"},
            {"type": "mix", "well": "plate1:A2", "volume": 100, "unit": "uL", "cycles": 5},
            {"type": "read", "plate": "plate1", "mode": "absorbance", "wavelength": 450},
        ],
        "mix_incubate_read": [
            {"type": "transfer", "source": "reservoir:buffer", "dest": "plate1:C1", "volume": 180, "unit": "uL"},
            {"type": "transfer", "source": "reservoir:dye", "dest": "plate1:C1", "volume": 20, "unit": "uL"},
            {"type": "mix", "well": "plate1:C1", "volume": 100, "unit": "uL", "cycles": 6},
            {"type": "incubate", "plate": "plate1", "temperature": 37, "time": 30, "time_unit": "min"},
            {"type": "read", "plate": "plate1", "mode": "fluorescence", "wavelength": 520},
        ],
        "wash_and_read": [
            {"type": "transfer", "source": "reservoir:buffer", "dest": "plate1:B1", "volume": 150, "unit": "uL"},
            {"type": "mix", "well": "plate1:B1", "volume": 100, "unit": "uL", "cycles": 4},
            {"type": "wash", "plate": "plate1", "volume": 300, "unit": "uL", "cycles": 3},
            {"type": "read", "plate": "plate1", "mode": "absorbance", "wavelength": 595},
        ],
    }


def test_job_from_protocol_groups_contiguous_steps_and_windows() -> None:
    examples = _twinstep_examples()

    job_mir = job_from_protocol(
        {"protocol_id": "mir_01", "steps": examples["mix_incubate_read"]},
        release=5,
        post_incubate_max_wait=12,
        transfer_time=2,
    )
    assert job_mir.job_id == "mir_01"
    assert job_mir.release == 5
    assert [op.instrument for op in job_mir.ops] == [
        "liquid_handler",
        "incubator",
        "plate_reader",
    ]
    assert job_mir.ops[0].name == "transfer_mix"
    assert job_mir.ops[1].duration == 30
    assert job_mir.ops[1].transfer_time == 2
    assert job_mir.ops[2].transfer_time == 2
    assert job_mir.ops[2].max_wait == 14  # transfer_time(2) + post_incubate_max_wait(12)

    with pytest.raises(ValueError, match=r"Unsupported protocol step type 'teleport_plate' at step 0"):
        job_from_protocol([{"type": "teleport_plate"}])


def test_time_unit_table_case_insensitivity_and_volume_unit_rejection() -> None:
    # Issue #35 acceptance table rows 1-4:
    assert job_from_protocol([{"type": "incubate", "time": 2, "time_unit": "hour"}]).ops[0].duration == 120
    assert job_from_protocol([{"type": "incubate", "time": 90, "time_unit": "second"}]).ops[0].duration == 2
    assert job_from_protocol([{"type": "incubate", "time": 500, "time_unit": "ms"}]).ops[0].duration == 1
    assert job_from_protocol([{"type": "incubate", "time": 1, "time_unit": "day"}]).ops[0].duration == 1440

    # Issue #35 row 5: volume unit in `unit` when duration/time is given must raise ValueError asking for time_unit
    for vol_unit in ("uL", "mL", "L", "nL"):
        with pytest.raises(
            ValueError,
            match=rf"Step 0 \(transfer\).*{vol_unit}.*time_unit",
        ):
            job_from_protocol([{"type": "transfer", "volume": 50, "unit": vol_unit, "duration": 30}])

    # Case-insensitive spellings ("MIN", "Minutes", "HRS", "Days", "Secs")
    assert job_from_protocol([{"type": "incubate", "time": 15, "time_unit": "MIN"}]).ops[0].duration == 15
    assert job_from_protocol([{"type": "incubate", "time": 20, "time_unit": "Minutes"}]).ops[0].duration == 20
    assert job_from_protocol([{"type": "incubate", "time": 1, "time_unit": "HRS"}]).ops[0].duration == 60
    assert job_from_protocol([{"type": "incubate", "time": 2, "time_unit": "Days"}]).ops[0].duration == 2880
    assert job_from_protocol([{"type": "incubate", "time": 120, "time_unit": "Secs"}]).ops[0].duration == 2

    # Default when no unit key is given at all stays minutes; twinstep `unit: min` on incubate stays valid
    assert job_from_protocol([{"type": "incubate", "time": 12}]).ops[0].duration == 12
    assert job_from_protocol([{"type": "incubate", "time": 12, "unit": "min"}]).ops[0].duration == 12

    # Unknown unit ("fortnight") raises ValueError naming step index, step type, and bad unit
    with pytest.raises(ValueError, match=r"Step 1 \(incubate\).*'fortnight'"):
        job_from_protocol(
            [
                {"type": "transfer", "volume": 50, "unit": "uL"},
                {"type": "incubate", "time": 1, "time_unit": "fortnight"},
            ]
        )


def test_stepbot_example_protocols_import_with_exact_hand_computed_durations() -> None:
    # 1. dilution_series.json:
    #    comment (0 s) + dilution_series(n=8, mix_reps=3 -> 60 + 15*8 + 6*7*3 = 306 s) + delay(5 s)
    #    = 311.0 s -> ceil(311 / 60) = 6 min
    ds_data = json.loads((STEPBOT_FIXTURES_DIR / "dilution_series.json").read_text(encoding="utf-8"))
    ds_expected_secs = 0.0 + (60.0 + 15.0 * 8 + 6.0 * 7 * 3) + 5.0
    assert ds_expected_secs == 311.0
    ds_job = job_from_stepbot_spec(ds_data)
    assert ds_job.job_id == "Two-fold dilution series"
    assert len(ds_job.ops) == 1
    assert ds_job.ops[0].instrument == "liquid_handler"
    assert ds_job.ops[0].duration == math.ceil(ds_expected_secs / 60.0) == 6

    # 2. plate_fill.json:
    #    comment (0) + distribute(150 uL -> 37.5 s) + comment (0) + transfer(50 uL -> 32.5 s)
    #    + transfer(50 uL -> 32.5 s) + mix(reps=3 -> 15 + 18 = 33.0 s) + delay(10 s)
    #    = 145.5 s -> ceil(145.5 / 60) = 3 min
    pf_data = json.loads((STEPBOT_FIXTURES_DIR / "plate_fill.json").read_text(encoding="utf-8"))
    pf_expected_secs = (30.0 + 0.05 * 150) + 2 * (30.0 + 0.05 * 50) + (15.0 + 6.0 * 3) + 10.0
    assert pf_expected_secs == 145.5
    pf_job = job_from_stepbot_spec(pf_data)
    assert pf_job.job_id == "Plate fill for absorbance read"
    assert len(pf_job.ops) == 1
    assert pf_job.ops[0].instrument == "liquid_handler"
    assert pf_job.ops[0].duration == math.ceil(pf_expected_secs / 60.0) == 3

    # 3. reservoir_mix.json:
    #    comment (0) + distribute(250 uL -> 42.5 s) + comment (0) + transfer(15 uL -> 30.75 s)
    #    + comment (0) + mix(reps=4 -> 15 + 24 = 39.0 s) + pause(seconds=3 -> 3.0 s)
    #    = 115.25 s -> ceil(115.25 / 60) = 2 min
    rm_data = json.loads((STEPBOT_FIXTURES_DIR / "reservoir_mix.json").read_text(encoding="utf-8"))
    rm_expected_secs = (30.0 + 0.05 * 250) + (30.0 + 0.05 * 15) + (15.0 + 6.0 * 4) + 3.0
    assert rm_expected_secs == 115.25
    rm_job = job_from_protocol(STEPBOT_FIXTURES_DIR / "reservoir_mix.json")
    assert rm_job.job_id == "Reservoir to plate mix"
    assert len(rm_job.ops) == 1
    assert rm_job.ops[0].instrument == "liquid_handler"
    assert rm_job.ops[0].duration == math.ceil(rm_expected_secs / 60.0) == 2

    # Multi-protocol batch from all 3 stepbot fixtures schedules cleanly
    batch = instance_from_protocols(
        [ds_data, pf_data, rm_data],
        name="stepbot_examples_batch",
    )
    assert [j.ops[0].duration for j in batch.jobs] == [6, 3, 2]
    assert check_schedule(batch, solve_greedy_repair(batch)).feasible


def test_stepbot_shorthands_tip_constants_timeless_pause_and_unknown_kinds() -> None:
    # Bare-string comment, bare-number delay, pick_up_tip / discard / drop_tip named constant,
    # and timeless pause resolved via pause_minutes
    steps = [
        "Bare comment string",
        {"pick_up_tip": {"pipette": "left"}},
        {"discard": {"src": "plate:A1", "volume": 50}},
        {"drop_tip": "left"},
        {"delay": 45},
        {"pause": {"message": "Inspect deck"}},
    ]
    # Timeless pause at index 5 without pause_minutes raises ValueError naming step index
    with pytest.raises(ValueError, match=r"Step 5 \(pause\)"):
        job_from_stepbot_spec({"steps": steps})

    # Passing pause_minutes=2.0 resolves step 5 to 120 s:
    # total = 3 * STEPBOT_TIP_ACTION_SECONDS (15 s) + 45 s + 120 s = 180 s -> 3 min
    job = job_from_stepbot_spec({"steps": steps}, job_id="shorthand_job", pause_minutes=2.0)
    expected_secs = 3 * STEPBOT_TIP_ACTION_SECONDS + 45.0 + 120.0
    assert expected_secs == 180.0
    assert len(job.ops) == 1
    assert job.ops[0].duration == 3

    # Unknown single-key step kind raises ValueError naming step index and kind
    with pytest.raises(ValueError, match=r"Unsupported protocol step type 'vortex_plate' at step 2"):
        job_from_stepbot_spec(
            [
                {"comment": "ok"},
                {"delay": {"minutes": 1, "seconds": 10}},
                {"vortex_plate": {"rpm": 1200}},
            ]
        )


def test_instance_from_protocols_and_workcell_queue_export(tmp_path: Path) -> None:
    examples = _twinstep_examples()
    paths: list[Path] = []
    for name, steps in examples.items():
        p = tmp_path / f"{name}.json"
        p.write_text(json.dumps(steps, indent=2), encoding="utf-8")
        paths.append(p)

    inst = instance_from_protocols(
        paths,
        name="twinstep_batch",
        releases=[0, 2, 4],
        transfer_time=1,
    )
    assert len(inst.jobs) == 3
    assert "robot_arm" in inst.capacities

    sched = solve_greedy_repair(inst)
    assert check_schedule(inst, sched).feasible

    queue = export_workcell_queue(sched, inst)
    assert any(ev["event"] == "transfer" and ev["instrument"] == "robot_arm" for ev in queue)
    assert any(ev["event"] == "execute" and ev["instrument"] == "plate_reader" for ev in queue)
    # Events are sorted chronologically by start timestamp
    starts = [int(ev["start"]) for ev in queue]
    assert starts == sorted(starts)


def test_cpsat_solves_multi_protocol_instance() -> None:
    pytest.importorskip("ortools")
    from labshift.cpsat import solve_cpsat

    examples = _twinstep_examples()
    inst = instance_from_protocols(
        [
            {"protocol_id": k, "steps": v}
            for k, v in examples.items()
        ],
        name="cpsat_protocols",
        transfer_time=1,
    )
    sched = solve_cpsat(inst, time_limit=5.0, num_workers=1, seed=0, tie_break_flow=True)
    assert sched.status == "OPTIMAL"
    assert check_schedule(inst, sched).feasible


def test_orthogonal_protocol_steps_transit_matrix_and_same_pool_queue() -> None:
    from labshift.models import Instance, Job, Operation, Schedule, ScheduledOp

    ortho_proto = {
        "protocol_id": "ortho_confirm",
        "assay_tag": "catalysis_screen",
        "steps": [
            {"type": "transfer", "volume": 50, "unit": "uL"},
            {"type": "raman", "duration": 6, "time_unit": "min"},
            {"type": "lc_ms", "duration": 8, "time_unit": "min"},
        ],
    }
    transit = {
        "liquid_handler": {"raman_spectrometer": 1},
        "raman_spectrometer": {"lc_ms": 3},
    }
    inst = instance_from_protocols([ortho_proto], name="ortho_batch", transit_matrix=transit)
    assert "raman_spectrometer" in inst.capacities
    assert "lc_ms" in inst.capacities
    assert "robot_arm" in inst.capacities
    job = inst.jobs[0]
    assert [op.instrument for op in job.ops] == [
        "liquid_handler",
        "raman_spectrometer",
        "lc_ms",
    ]
    assert job.ops[1].transfer_time == 1
    assert job.ops[2].transfer_time == 3

    sched = solve_greedy_repair(inst)
    assert check_schedule(inst, sched).feasible is True

    # Same-instrument operations with transfer_time > 0 must not emit robot_arm transfer events
    same_inst = Instance(
        name="same_inst_queue",
        capacities={"liquid_handler": 1, "robot_arm": 1},
        jobs=(
            Job(
                "j1",
                ops=(
                    Operation("prep", "liquid_handler", 4),
                    Operation("mix", "liquid_handler", 3, transfer_time=2),
                ),
            ),
        ),
    )
    same_sched = Schedule(
        "same_inst_queue",
        "manual",
        ops=(
            ScheduledOp("j1", 0, "liquid_handler", 0, 4, "prep"),
            ScheduledOp("j1", 1, "liquid_handler", 6, 9, "mix"),
        ),
    )
    queue = export_workcell_queue(same_sched, same_inst)
    assert [ev["event"] for ev in queue] == ["execute", "execute"]
