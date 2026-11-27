"""Tests for models, domain validation, JSON serialization, and benchmark suite integrity."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from labshift.bench import collect_instance_paths
from labshift.checker import check_schedule
from labshift.generator import PRESETS, PresetConfig, _sample_from_config, generate_instance
from labshift.greedy import solve_greedy, solve_greedy_repair
from labshift.metrics import compute_metrics
from labshift.models import Instance, Job, Operation, Schedule, ScheduledOp

REPO_ROOT = Path(__file__).resolve().parents[1]
BENCHMARKS_DIR = REPO_ROOT / "benchmarks"


def test_seeded_generator_reproducibility_and_presets() -> None:
    assert set(PRESETS) >= {"small", "medium", "large", "hard"}
    for preset in PRESETS:
        inst_a = generate_instance(preset=preset, seed=42)
        inst_b = generate_instance(preset=preset, seed=42)
        assert inst_a == inst_b
        assert inst_a.preset == preset
        assert len(inst_a.jobs) == PRESETS[preset].num_jobs

    with pytest.raises(ValueError, match="Unknown preset"):
        generate_instance(preset="unknown", seed=0)
    with pytest.raises(TypeError):
        generate_instance(preset="small", seed=True)  # type: ignore[arg-type]


def test_instance_and_schedule_json_roundtrip(tmp_path: Path) -> None:
    inst = generate_instance(preset="small", seed=7)
    inst_path = tmp_path / "inst.json"
    inst.save_json(inst_path)
    loaded_inst = Instance.load_json(inst_path)
    assert loaded_inst == inst
    # Default transfer_time=0 omits 'transfer_time' key for v1 schema compatibility.
    assert "transfer_time" not in inst.jobs[0].ops[0].to_dict()

    # Non-zero transfer_time round-trips cleanly.
    op_transfer = Operation("read", "plate_reader", duration=5, min_wait=1, max_wait=6, transfer_time=2)
    assert op_transfer.to_dict()["transfer_time"] == 2
    assert Operation.from_dict(op_transfer.to_dict()) == op_transfer

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

    # Optional transit_matrix, setup_times, assay_tag, setup_tag, unit_index, and blocking_handoff round-trip
    rich_inst = Instance(
        name="rich_workcell",
        capacities={"washer": 2, "plate_reader": 1, "robot_arm": 1},
        transit_matrix={"washer": {"plate_reader": 2}},
        setup_times={"washer": {"bsl2": {"elisa": 10}, "elisa": {"bsl2": 2}}},
        blocking_handoff=False,
        jobs=(
            Job(
                job_id="j_bsl2",
                assay_tag="bsl2",
                ops=(
                    Operation("wash", "washer", duration=6),
                    Operation("read", "plate_reader", duration=4, max_wait=5, setup_tag="37C"),
                ),
            ),
        ),
    )
    assert rich_inst.setup_matrix == rich_inst.setup_times
    assert rich_inst.jobs[0].ops[1].assay_tag == "37C"
    assert rich_inst.effective_setup_tag(rich_inst.jobs[0], rich_inst.jobs[0].ops[0]) == "bsl2"
    assert rich_inst.effective_setup_tag(rich_inst.jobs[0], rich_inst.jobs[0].ops[1]) == "37C"
    assert rich_inst.effective_transfer_time("washer", rich_inst.jobs[0].ops[1]) == 2
    rich_path = tmp_path / "rich_inst.json"
    rich_inst.save_json(rich_path)
    assert Instance.load_json(rich_path) == rich_inst

    unit_sched = Schedule(
        instance_name="rich_workcell",
        solver="manual",
        ops=(ScheduledOp("j_bsl2", 0, "washer", 0, 6, "wash", unit_index=1),),
    )
    assert Schedule.from_dict(unit_sched.to_dict()) == unit_sched

    with pytest.raises(ValueError, match="Conflicting"):
        Operation("wash", "washer", duration=5, setup_tag="A", assay_tag="B")
    with pytest.raises(ValueError, match="setup_times"):
        Instance(
            "bad_setup",
            capacities={"washer": 1},
            setup_times={"washer": {"a": {"b": -1}}},
            jobs=(Job("j1", ops=(Operation("wash", "washer", 5),)),),
        )
    with pytest.raises(ValueError, match="transit_matrix"):
        Instance(
            "bad_transit",
            capacities={"washer": 1},
            transit_matrix={"washer": {"washer": -2}},
            jobs=(Job("j1", ops=(Operation("wash", "washer", 5),)),),
        )


def test_domain_validation_operation_job_instance_and_schedule() -> None:
    # Operation validation: durations >= 1, non-negative waits, max_wait >= max(min_wait, transfer_time), no bools
    with pytest.raises(ValueError, match="duration"):
        Operation("prep", "liquid_handler", duration=0)
    with pytest.raises(ValueError, match="duration"):
        Operation("prep", "liquid_handler", duration=-5)
    with pytest.raises(TypeError, match="duration"):
        Operation("prep", "liquid_handler", duration=True)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="duration"):
        Operation.from_dict({"name": "prep", "instrument": "liquid_handler", "duration": True})
    with pytest.raises(ValueError, match="min_wait"):
        Operation("prep", "liquid_handler", duration=5, min_wait=-1)
    with pytest.raises(ValueError, match="transfer_time"):
        Operation("prep", "liquid_handler", duration=5, transfer_time=-2)
    with pytest.raises(ValueError, match="max_wait"):
        Operation("prep", "liquid_handler", duration=5, min_wait=10, max_wait=2)
    with pytest.raises(ValueError, match="max_wait"):
        Operation("prep", "liquid_handler", duration=5, min_wait=0, transfer_time=4, max_wait=2)
    with pytest.raises(ValueError, match="name"):
        Operation("  ", "liquid_handler", duration=5)
    with pytest.raises(ValueError, match="instrument"):
        Operation("prep", "", duration=5)

    valid_op = Operation("prep", "liquid_handler", duration=5)

    # Job validation: non-empty job_id, release >= 0, deadline >= release, no bools
    with pytest.raises(ValueError, match="job_id"):
        Job("", ops=(valid_op,))
    with pytest.raises(ValueError, match="release"):
        Job("j1", ops=(valid_op,), release=-1)
    with pytest.raises(TypeError, match="release"):
        Job("j1", ops=(valid_op,), release=False)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="deadline"):
        Job("j1", ops=(valid_op,), release=10, deadline=5)

    # Instance validation: unique job_ids, capacities >= 0, no missing instruments
    j1 = Job("j1", ops=(valid_op,))
    j1_dup = Job("j1", ops=(valid_op,), release=3)
    with pytest.raises(ValueError, match="Duplicate job_id"):
        Instance("dup_inst", capacities={"liquid_handler": 1}, jobs=(j1, j1_dup))
    with pytest.raises(ValueError, match="capacities"):
        Instance("neg_cap", capacities={"liquid_handler": -1}, jobs=(j1,))
    with pytest.raises(TypeError, match="capacities"):
        Instance("bool_cap", capacities={"liquid_handler": True}, jobs=(j1,))  # type: ignore[dict-item]
    with pytest.raises(ValueError, match="missing from instance capacities"):
        Instance("missing_inst", capacities={"incubator": 1}, jobs=(j1,))

    # ScheduledOp and Schedule validation
    with pytest.raises(TypeError, match="start"):
        ScheduledOp("j1", 0, "liquid_handler", True, 5)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="op_index"):
        ScheduledOp("j1", -1, "liquid_handler", 0, 5)
    with pytest.raises(ValueError, match="solve_time_s"):
        Schedule("inst", "greedy", ops=(), solve_time_s=-0.1)


def _reference_earliest_slot(
    intervals: list[tuple[int, int]],
    capacity: int,
    earliest_start: int,
    duration: int,
) -> int:
    candidates = {earliest_start}
    for _, end in intervals:
        if end > earliest_start:
            candidates.add(end)
    for t in sorted(candidates):
        t_end = t + duration
        if sum(1 for s, e in intervals if s <= t < e) >= capacity:
            continue
        if any(
            t < start < t_end and sum(1 for s, e in intervals if s <= start < e) >= capacity
            for start, _ in intervals
        ):
            continue
        return t
    return earliest_start


def test_committed_benchmarks_suite_integrity_and_greedy_baselines() -> None:
    paths = collect_instance_paths(BENCHMARKS_DIR)
    assert len(paths) == 30

    greedy_feasible = 0
    repair_feasible = 0
    greedy_makespans: list[int] = []
    total_win_viol = 0

    for path in paths:
        raw_dict = json.loads(path.read_text(encoding="utf-8"))
        inst = Instance.from_dict(raw_dict)
        assert inst.preset in ("small", "medium", "large")
        assert inst.seed is not None
        regenerated = generate_instance(preset=inst.preset, seed=inst.seed, name=inst.name)
        assert inst == regenerated
        assert regenerated.to_dict() == raw_dict

        g_sched = solve_greedy(inst)
        g_check = check_schedule(inst, g_sched)
        g_metrics = compute_metrics(inst, g_sched, check=g_check)
        assert g_metrics.makespan is not None
        greedy_makespans.append(g_metrics.makespan)
        total_win_viol += g_metrics.window_violations
        if g_check.feasible:
            greedy_feasible += 1

        # Verify O(K log K) greedy matches reference interval check on every scheduled op
        inst_ivs: dict[str, list[tuple[int, int]]] = {k: [] for k in inst.capacities}
        for sop in g_sched.ops:
            job = next(j for j in inst.jobs if j.job_id == sop.job_id)
            if sop.op_index == 0:
                r_time = job.release
            else:
                prev_sop = g_sched.by_key()[(sop.job_id, sop.op_index - 1)]
                r_time = prev_sop.end + job.ops[sop.op_index].min_wait
            exp_start = _reference_earliest_slot(
                inst_ivs[sop.instrument],
                inst.capacities[sop.instrument],
                r_time,
                sop.end - sop.start,
            )
            assert sop.start == exp_start
            inst_ivs[sop.instrument].append((sop.start, sop.end))

        r_sched = solve_greedy_repair(inst)
        r_check = check_schedule(inst, r_sched)
        if r_check.feasible:
            repair_feasible += 1

    assert greedy_feasible == 24
    assert total_win_viol == 13
    assert greedy_makespans[0] == 122  # small_00
    assert sum(greedy_makespans) / 30 == pytest.approx(129.7666667, abs=1e-4)
    assert repair_feasible == 30


def test_hard_preset_difficulty_and_ensure_feasible() -> None:
    med = PRESETS["medium"]
    lrg = PRESETS["large"]
    hrd = PRESETS["hard"]

    med_rate = med.num_jobs / (med.release_max * med.capacities["plate_reader"])
    lrg_rate = lrg.num_jobs / (lrg.release_max * lrg.capacities["plate_reader"])
    hrd_rate = hrd.num_jobs / (hrd.release_max * hrd.capacities["plate_reader"])
    assert hrd_rate > med_rate > lrg_rate

    inst_ok = generate_instance(preset="hard", seed=1300, ensure_feasible=True)
    assert check_schedule(inst_ok, solve_greedy_repair(inst_ok)).feasible

    # Verify ensure_feasible relaxes impossible tight config via _sample_from_config / generate_instance
    tight_cfg = PresetConfig(
        num_jobs=4,
        ops_min=3,
        ops_max=4,
        release_max=0,
        capacities={
            "liquid_handler": 1,
            "incubator": 1,
            "plate_reader": 1,
            "centrifuge": 1,
            "washer": 1,
        },
        max_wait_prob=1.0,
        max_wait_range=(0, 0),
        deadline_prob=1.0,
        deadline_slack_factor=0.5,
    )
    raw_tight = _sample_from_config(tight_cfg, "tight", 0, 0, "tight_00")
    assert not check_schedule(raw_tight, solve_greedy_repair(raw_tight)).feasible

    PRESETS["_test_tight"] = tight_cfg
    try:
        repaired_inst = generate_instance(preset="_test_tight", seed=0, ensure_feasible=True)
        assert check_schedule(repaired_inst, solve_greedy_repair(repaired_inst)).feasible
    finally:
        PRESETS.pop("_test_tight", None)


def test_lazy_ortools_import_when_blocked() -> None:
    src_dir = str(REPO_ROOT / "src")
    code = """
import sys
sys.modules["ortools"] = None
sys.modules["ortools.sat"] = None
sys.modules["ortools.sat.python"] = None

import labshift
from labshift import (
    check_schedule,
    compute_metrics,
    generate_instance,
    job_from_protocol,
    render_gantt,
    solve_greedy,
    solve_greedy_repair,
    solve_instance,
)

inst = generate_instance("medium", seed=1101)
sched = solve_greedy_repair(inst)
assert check_schedule(inst, sched).feasible
m = compute_metrics(inst, sched)
assert m.makespan is not None and m.makespan > 0
chart = render_gantt(inst, sched, width=30)
assert inst.name in chart

job = job_from_protocol([{"type": "transfer", "source": "r:a", "dest": "p:A1", "volume": 50}])
assert len(job.ops) == 1

try:
    _ = labshift.solve_cpsat
except ImportError as exc:
    assert "ortools>=9.8" in str(exc)
else:
    raise AssertionError("Expected ImportError when accessing labshift.solve_cpsat")

try:
    solve_instance(inst, solver="cpsat")
except ImportError as exc:
    assert "ortools>=9.8" in str(exc)
else:
    raise AssertionError("Expected ImportError when calling solve_instance(..., 'cpsat')")
"""
    proc = subprocess.run(
        [sys.executable, "-c", code],
        env={"PYTHONPATH": src_dir},
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"stderr:\n{proc.stderr}\nstdout:\n{proc.stdout}"
