"""Tests for twinstep / stepbot protocol adapters and workcell queue export."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from labshift.checker import check_schedule
from labshift.greedy import solve_greedy_repair
from labshift.protocol import (
    export_workcell_queue,
    instance_from_protocols,
    job_from_protocol,
)


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

    with pytest.raises(ValueError, match="Unsupported protocol step type"):
        job_from_protocol([{"type": "teleport_plate"}])


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
