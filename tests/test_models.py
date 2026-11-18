"""Tests for models, JSON serialization, and seeded instance generation."""

from __future__ import annotations

from pathlib import Path

import pytest

from labshift.generator import PRESETS, generate_instance
from labshift.models import Instance, Schedule, ScheduledOp


def test_seeded_generator_reproducibility_and_presets() -> None:
    for preset in PRESETS:
        inst_a = generate_instance(preset=preset, seed=42)
        inst_b = generate_instance(preset=preset, seed=42)
        assert inst_a == inst_b
        assert inst_a.preset == preset
        assert len(inst_a.jobs) == PRESETS[preset].num_jobs

    with pytest.raises(ValueError):
        generate_instance(preset="unknown", seed=0)


def test_instance_and_schedule_json_roundtrip(tmp_path: Path) -> None:
    inst = generate_instance(preset="small", seed=7)
    inst_path = tmp_path / "inst.json"
    inst.save_json(inst_path)
    loaded_inst = Instance.load_json(inst_path)
    assert loaded_inst == inst

    sched = Schedule(
        instance_name=inst.name,
        solver="manual",
        status="OK",
        solve_time_s=0.01,
        best_bound=10.0,
        ops=(ScheduledOp("job_01", 0, "liquid_handler", 0, 6, "plate_prep"),),
    )
    sched_path = tmp_path / "sched.json"
    sched.save_json(sched_path)
    loaded_sched = Schedule.load_json(sched_path)
    assert loaded_sched == sched
