"""Tests for greedy, greedy_repair, CP-SAT solvers, metrics, and summary tables."""

from __future__ import annotations

from pathlib import Path

import pytest

from labshift.bench import BenchRow, format_benchmark_table
from labshift.checker import check_schedule
from labshift.generator import generate_instance
from labshift.greedy import solve_greedy, solve_greedy_repair
from labshift.metrics import compute_metrics
from labshift.models import Instance, Job, Operation, Schedule, ScheduledOp

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_compute_metrics_hand_calculated() -> None:
    inst = Instance(
        name="hand_calc",
        capacities={"liquid_handler": 1, "plate_reader": 1},
        jobs=(
            Job(
                job_id="j1",
                release=5,
                deadline=20,
                ops=(
                    Operation("prep", "liquid_handler", duration=6),
                    Operation("read", "plate_reader", duration=8, min_wait=1),
                ),
            ),
            Job(
                job_id="j2",
                release=2,
                deadline=30,
                ops=(Operation("read", "plate_reader", duration=4),),
            ),
            Job(job_id="j_empty", release=0, deadline=10, ops=()),
        ),
    )

    # j1 finishes at 21 (release 5 => flow 16; deadline 20 => tardiness 1)
    # j2 finishes at 8 (release 2 => flow 6; deadline 30 => tardiness 0)
    # makespan = 21, mean_flow_time = (16 + 6) / 2 = 11.0
    # stdlib lower_bound = 20 (j1 chain: 5 + 6 + 1 + 8 = 20); solver_bound = 14.0
    # best_bound = max(14.0, 20) = 20.0 => gap_pct = 100 * (21 - 20) / 21
    sched = Schedule(
        instance_name="hand_calc",
        solver="manual",
        ops=(
            ScheduledOp("j2", 0, "plate_reader", 4, 8, "read"),
            ScheduledOp("j1", 0, "liquid_handler", 5, 11, "prep"),
            ScheduledOp("j1", 1, "plate_reader", 13, 21, "read"),
        ),
        status="FEASIBLE",
        solve_time_s=0.125,
        best_bound=14.0,
    )
    m = compute_metrics(inst, sched)
    assert m.feasible is False
    assert m.makespan == 21
    assert m.total_tardiness == 1
    assert m.deadline_violations == 1
    assert m.window_violations == 0
    assert m.total_violations == 1
    assert m.mean_flow_time == pytest.approx(11.0)
    assert m.lower_bound == 20
    assert m.solver_bound == 14.0
    assert m.best_bound == 20.0
    assert m.gap_pct == pytest.approx(100.0 * 1.0 / 21.0)

    # Verify invalid best_bound > makespan is NOT masked by a max(0.0, ...) clamp
    bad_bound_sched = Schedule(
        instance_name="hand_calc",
        solver="manual",
        ops=sched.ops,
        status="FEASIBLE",
        best_bound=25.0,
    )
    m_bad = compute_metrics(inst, bad_bound_sched)
    assert m_bad.gap_pct is not None and m_bad.gap_pct < 0.0

    # Verify INFEASIBLE / empty schedule yields None for makespan, mean_flow_time, gap_pct
    infeas_sched = Schedule(
        instance_name="hand_calc",
        solver="cpsat",
        ops=(),
        status="INFEASIBLE",
        best_bound=None,
    )
    m_infeas = compute_metrics(inst, infeas_sched)
    assert m_infeas.feasible is False
    assert m_infeas.makespan is None
    assert m_infeas.mean_flow_time is None
    assert m_infeas.gap_pct is None


def test_format_benchmark_table_excludes_infeasible_and_unknown_from_averages() -> None:
    inst = generate_instance("small", seed=0)
    ok_sched = solve_greedy(inst)
    ok_metrics = compute_metrics(inst, ok_sched)
    assert ok_metrics.makespan is not None
    assert ok_metrics.mean_flow_time is not None

    infeas_sched = Schedule(inst.name, "cpsat", ops=(), status="INFEASIBLE")
    infeas_metrics = compute_metrics(inst, infeas_sched)

    unknown_sched = Schedule(inst.name, "cpsat", ops=(), status="UNKNOWN", best_bound=150.0)
    unknown_metrics = compute_metrics(inst, unknown_sched)

    # Also include an ops-bearing schedule that violates constraints (feasible=False)
    bad_ops = tuple(
        ScheduledOp(op.job_id, op.op_index, op.instrument, op.start + 500, op.end + 500, op.name)
        for op in ok_sched.ops
    )
    viol_sched = Schedule(inst.name, "greedy", ops=bad_ops, status="OK")
    viol_metrics = compute_metrics(inst, viol_sched)
    assert viol_metrics.feasible is False
    assert viol_metrics.makespan is not None

    rows = [
        BenchRow("inst_ok", "small", "cpsat", "OPTIMAL", ok_metrics),
        BenchRow("inst_viol", "small", "cpsat", "OK", viol_metrics),
        BenchRow("inst_infeas", "small", "cpsat", "INFEASIBLE", infeas_metrics),
        BenchRow("inst_unknown", "large", "cpsat", "UNKNOWN", unknown_metrics),
    ]
    table = format_benchmark_table(rows)
    lines = table.splitlines()
    assert "inst_infeas" in table
    assert "150.0" in table  # preserved UNKNOWN best_bound
    summary_cells = [cell.strip() for cell in lines[-1].split("|")]
    assert summary_cells[0] == "ALL (4)"
    assert summary_cells[1] == "feas-only (1/4)"
    assert summary_cells[2] == "2/4"  # solved count (inst_ok + inst_viol)
    assert summary_cells[3] == "1/4"  # feasible count (inst_ok only)
    assert summary_cells[4] == f"{ok_metrics.makespan:.1f}"  # excludes inst_viol's +500 makespan!
    assert summary_cells[6] == f"{ok_metrics.mean_flow_time:.1f}"


def test_greedy_and_repair_zero_capacity_returns_infeasible() -> None:
    inst = Instance(
        name="zero_cap",
        capacities={"liquid_handler": 0},
        jobs=(Job("j1", ops=(Operation("prep", "liquid_handler", duration=5),)),),
    )
    g_sched = solve_greedy(inst)
    assert g_sched.status == "INFEASIBLE"
    assert g_sched.ops == ()
    assert compute_metrics(inst, g_sched).makespan is None

    r_sched = solve_greedy_repair(inst)
    assert r_sched.status == "INFEASIBLE"
    assert r_sched.ops == ()


def test_greedy_and_repair_capacity_above_one_and_tight_windows() -> None:
    # Capacity 2 pool with 3 identical jobs released at t=0: two run [0, 10), third runs [10, 20)
    cap2_inst = Instance(
        name="cap2_pool",
        capacities={"incubator": 2},
        jobs=tuple(
            Job(f"j{i}", release=0, ops=(Operation("inc", "incubator", duration=10),))
            for i in range(1, 4)
        ),
    )
    g_cap2 = solve_greedy(cap2_inst)
    assert check_schedule(cap2_inst, g_cap2).feasible
    assert compute_metrics(cap2_inst, g_cap2).makespan == 20

    # Tight max_wait window where myopic greedy fails and greedy_repair succeeds
    tight_inst = Instance(
        name="tight_window",
        capacities={"liquid_handler": 1, "plate_reader": 1},
        jobs=(
            Job(
                job_id="j1",
                release=0,
                ops=(
                    Operation("prep", "liquid_handler", duration=5),
                    Operation("read", "plate_reader", duration=5, min_wait=0, max_wait=1),
                ),
            ),
            Job(
                job_id="j2",
                release=0,
                ops=(Operation("read_only", "plate_reader", duration=10),),
            ),
        ),
    )
    g_tight = solve_greedy(tight_inst)
    assert not check_schedule(tight_inst, g_tight).feasible

    r_tight = solve_greedy_repair(tight_inst)
    r_metrics = compute_metrics(tight_inst, r_tight)
    assert r_metrics.feasible
    assert r_metrics.window_violations == 0
    assert r_metrics.makespan == 15


def test_greedy_and_repair_respect_robot_arm_transfer_time() -> None:
    inst = Instance(
        name="arm_ contention",
        capacities={"liquid_handler": 2, "plate_reader": 2, "robot_arm": 1},
        jobs=(
            Job(
                job_id="j1",
                ops=(
                    Operation("prep", "liquid_handler", duration=5),
                    Operation("read", "plate_reader", duration=4, transfer_time=3, max_wait=6),
                ),
            ),
            Job(
                job_id="j2",
                ops=(
                    Operation("prep", "liquid_handler", duration=5),
                    Operation("read", "plate_reader", duration=4, transfer_time=3, max_wait=6),
                ),
            ),
        ),
    )
    g_sched = solve_greedy(inst)
    assert check_schedule(inst, g_sched).feasible
    r_sched = solve_greedy_repair(inst)
    assert check_schedule(inst, r_sched).feasible


@pytest.mark.parametrize(("preset", "seed"), [("small", 0), ("small", 1), ("medium", 1101), ("medium", 1104)])
def test_cpsat_small_and_medium_feasible_and_valid_bounds(preset: str, seed: int) -> None:
    from labshift.cpsat import solve_cpsat

    inst = generate_instance(preset=preset, seed=seed)
    g_sched = solve_greedy(inst)
    g_check = check_schedule(inst, g_sched)
    g_metrics = compute_metrics(inst, g_sched, check=g_check)

    c_sched = solve_cpsat(inst, time_limit=5.0)
    c_check = check_schedule(inst, c_sched)
    assert c_check.feasible, f"CP-SAT infeasible on {preset}/{seed}: {c_check.violations}"
    assert c_sched.status in ("OPTIMAL", "FEASIBLE")
    c_metrics = compute_metrics(inst, c_sched, check=c_check)

    assert c_metrics.makespan is not None
    assert c_sched.best_bound is not None
    assert c_sched.best_bound <= c_metrics.makespan + 1e-6
    assert c_metrics.gap_pct is not None and c_metrics.gap_pct >= 0.0
    if g_check.feasible and g_metrics.makespan is not None:
        assert c_metrics.makespan <= g_metrics.makespan


def test_cpsat_respects_tight_max_wait_where_greedy_violates() -> None:
    from labshift.cpsat import solve_cpsat

    inst = Instance(
        name="tight_window",
        capacities={"liquid_handler": 1, "plate_reader": 1},
        jobs=(
            Job(
                job_id="j1",
                release=0,
                ops=(
                    Operation("prep", "liquid_handler", duration=5),
                    Operation("read", "plate_reader", duration=5, min_wait=0, max_wait=1),
                ),
            ),
            Job(
                job_id="j2",
                release=0,
                ops=(Operation("read_only", "plate_reader", duration=10),),
            ),
        ),
    )

    g_sched = solve_greedy(inst)
    g_metrics = compute_metrics(inst, g_sched)
    assert not g_metrics.feasible
    assert g_metrics.window_violations >= 1

    c_sched = solve_cpsat(inst, time_limit=5.0)
    c_metrics = compute_metrics(inst, c_sched)
    assert c_sched.status == "OPTIMAL"
    assert c_metrics.feasible
    assert c_metrics.window_violations == 0
    assert c_metrics.makespan == 15


def test_cpsat_reports_infeasible_on_impossible_deadline_and_soft_deadlines() -> None:
    from labshift.cpsat import solve_cpsat

    inst = Instance(
        name="impossible",
        capacities={"liquid_handler": 1},
        jobs=(
            Job(
                job_id="j1",
                release=0,
                deadline=5,
                ops=(Operation("prep", "liquid_handler", duration=10),),
            ),
        ),
    )
    hard_sched = solve_cpsat(inst, time_limit=2.0, hard_deadlines=True)
    assert hard_sched.status == "INFEASIBLE"
    assert hard_sched.ops == ()
    assert compute_metrics(inst, hard_sched).makespan is None

    soft_sched = solve_cpsat(inst, time_limit=2.0, hard_deadlines=False)
    assert soft_sched.status == "OPTIMAL"
    soft_metrics = compute_metrics(inst, soft_sched)
    assert soft_metrics.makespan == 10
    assert soft_metrics.total_tardiness == 5
    assert soft_metrics.deadline_violations == 1


def test_cpsat_tie_break_flow_deterministic_on_small_00() -> None:
    from labshift.cpsat import solve_cpsat

    inst = Instance.load_json(REPO_ROOT / "benchmarks" / "small_00.json")
    s1 = solve_cpsat(inst, time_limit=5.0, num_workers=1, seed=0, tie_break_flow=True)
    s2 = solve_cpsat(inst, time_limit=5.0, num_workers=1, seed=0, tie_break_flow=True)
    assert s1.ops == s2.ops
    assert s1.status == "OPTIMAL"
    m = compute_metrics(inst, s1)
    assert m.feasible
    assert m.makespan == 105
    assert m.best_bound == 105.0
    assert m.gap_pct == 0.0
    assert m.mean_flow_time == pytest.approx(66.0)


def test_cpsat_preserves_best_bound_on_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    from ortools.sat.python import cp_model

    import labshift.cpsat as cpsat_mod

    orig_params = cp_model.CpSolver().parameters

    class _FakeUnknownSolver:
        def __init__(self) -> None:
            self.parameters = orig_params

        def solve(self, _model: object) -> int:
            return int(cp_model.UNKNOWN)

        @property
        def best_objective_bound(self) -> float:
            return 42.0

    monkeypatch.setattr(cpsat_mod.cp_model, "CpSolver", _FakeUnknownSolver)
    inst = generate_instance("small", seed=0)
    sched = cpsat_mod.solve_cpsat(inst, time_limit=0.1)
    assert sched.status == "UNKNOWN"
    assert sched.ops == ()
    assert sched.best_bound == 42.0
    metrics = compute_metrics(inst, sched)
    assert metrics.makespan is None
    assert metrics.solver_bound == 42.0
    assert metrics.best_bound == max(42.0, float(metrics.lower_bound))
    assert metrics.gap_pct is None


def test_cpsat_respects_transfer_time_and_robot_arm() -> None:
    from labshift.cpsat import solve_cpsat

    inst = Instance(
        name="cpsat_arm",
        capacities={"liquid_handler": 2, "plate_reader": 2, "robot_arm": 1},
        jobs=(
            Job(
                job_id="j1",
                ops=(
                    Operation("prep", "liquid_handler", duration=5),
                    Operation("read", "plate_reader", duration=4, transfer_time=3, max_wait=6),
                ),
            ),
            Job(
                job_id="j2",
                ops=(
                    Operation("prep", "liquid_handler", duration=5),
                    Operation("read", "plate_reader", duration=4, transfer_time=3, max_wait=6),
                ),
            ),
        ),
    )
    sched = solve_cpsat(inst, time_limit=5.0, num_workers=1, seed=0)
    assert sched.status == "OPTIMAL"
    assert check_schedule(inst, sched).feasible
    assert compute_metrics(inst, sched).makespan == 15

    zero_arm = Instance(
        name="cpsat_zero_arm",
        capacities={"liquid_handler": 1, "plate_reader": 1, "robot_arm": 0},
        jobs=(inst.jobs[0],),
    )
    assert solve_cpsat(zero_arm, time_limit=1.0).status == "INFEASIBLE"


def test_greedy_repair_coupled_slack_deadlock_and_deep_max_wait_chains() -> None:
    # Issue #28 exact reproducer: intermediate step op_1 absorbs 4 min wait in [0, 10]
    inst = Instance(
        name="coupled_slack_deadlock",
        capacities={"liquid_handler": 1, "incubator": 2, "plate_reader": 1},
        jobs=(
            Job(
                job_id="j_coupled",
                release=0,
                deadline=22,
                ops=(
                    Operation("prep", "liquid_handler", duration=5),
                    Operation("incubate", "incubator", duration=5, min_wait=0, max_wait=10),
                    Operation("read", "plate_reader", duration=5, min_wait=0, max_wait=1),
                ),
            ),
            Job(
                job_id="j_reader_busy",
                release=0,
                deadline=15,
                ops=(Operation("long_read", "plate_reader", duration=15),),
            ),
            Job(
                job_id="j_lh_busy",
                release=5,
                deadline=35,
                ops=(Operation("bulk_prep", "liquid_handler", duration=30),),
            ),
        ),
    )
    rep = solve_greedy_repair(inst)
    assert check_schedule(inst, rep).feasible is True

    # 4-step (3 max_wait links) chain with competing upstream and downstream single-unit stations
    deep_chain = Instance(
        name="deep_4step_chain",
        capacities={"liquid_handler": 1, "incubator": 2, "washer": 2, "plate_reader": 1},
        jobs=(
            Job(
                job_id="j_deep",
                release=0,
                deadline=30,
                ops=(
                    Operation("prep", "liquid_handler", duration=4),
                    Operation("incubate", "incubator", duration=5, min_wait=0, max_wait=6),
                    Operation("wash", "washer", duration=4, min_wait=0, max_wait=6),
                    Operation("read", "plate_reader", duration=5, min_wait=0, max_wait=1),
                ),
            ),
            Job(
                job_id="j_reader_hold",
                release=0,
                deadline=20,
                ops=(Operation("cal_read", "plate_reader", duration=20),),
            ),
            Job(
                job_id="j_lh_hold",
                release=4,
                deadline=34,
                ops=(Operation("long_prep", "liquid_handler", duration=30),),
            ),
        ),
    )
    rep_deep = solve_greedy_repair(deep_chain)
    assert check_schedule(deep_chain, rep_deep).feasible is True


def test_greedy_repair_source_blocking_and_setup_batching() -> None:
    # Issue #29: source-station blocking on cap-1 centrifuge + asymmetric transit_matrix
    inst_block = Instance(
        name="arm_blocking_repair",
        capacities={"centrifuge": 1, "plate_reader": 1, "robot_arm": 1},
        transit_matrix={"centrifuge": {"plate_reader": 3}},
        jobs=(
            Job(
                job_id="plate_A",
                ops=(
                    Operation("spin", "centrifuge", duration=10),
                    Operation("read", "plate_reader", duration=4, max_wait=10),
                ),
            ),
            Job(
                job_id="plate_B",
                ops=(Operation("spin", "centrifuge", duration=5),),
            ),
            Job(
                job_id="plate_C",
                ops=(Operation("read_first", "plate_reader", duration=13),),
            ),
        ),
    )
    rep_block = solve_greedy_repair(inst_block)
    assert check_schedule(inst_block, rep_block).feasible is True

    # Issue #30: asymmetric decontamination & thermal cooldown (running clean 25C ELISA first saves 7 min)
    inst_setup = Instance(
        name="asymmetric_assays",
        capacities={"washer": 1, "plate_reader": 1},
        setup_times={
            "washer": {
                "bacterial_lysis": {"mammalian_elisa": 10},
                "mammalian_elisa": {"bacterial_lysis": 2},
            },
            "plate_reader": {"37C": {"25C": 12}, "25C": {"37C": 4}},
        },
        jobs=(
            Job(
                "job_bsl2",
                release=0,
                ops=(
                    Operation("wash", "washer", 8, setup_tag="bacterial_lysis"),
                    Operation("read_37c", "plate_reader", 5, max_wait=6, setup_tag="37C"),
                ),
            ),
            Job(
                "job_elisa",
                release=1,
                ops=(
                    Operation("wash", "washer", 7, setup_tag="mammalian_elisa"),
                    Operation("read_25c", "plate_reader", 5, max_wait=6, setup_tag="25C"),
                ),
            ),
        ),
    )
    rep_setup = solve_greedy_repair(inst_setup)
    assert check_schedule(inst_setup, rep_setup).feasible is True
    assert compute_metrics(inst_setup, rep_setup).makespan == 23

    # Multi-batch family grouping under tight max_wait windows (2 ELISA + 2 BSL2)
    inst_families = Instance(
        name="family_batching",
        capacities={"washer": 1, "plate_reader": 2},
        setup_times={
            "washer": {"bsl2": {"elisa": 12}, "elisa": {"bsl2": 3}},
        },
        jobs=(
            Job(
                "b1",
                release=0,
                deadline=42,
                assay_tag="bsl2",
                ops=(
                    Operation("wash", "washer", 5),
                    Operation("read", "plate_reader", 4, max_wait=4),
                ),
            ),
            Job(
                "e1",
                release=1,
                deadline=42,
                assay_tag="elisa",
                ops=(
                    Operation("wash", "washer", 5),
                    Operation("read", "plate_reader", 4, max_wait=4),
                ),
            ),
            Job(
                "b2",
                release=2,
                deadline=42,
                assay_tag="bsl2",
                ops=(
                    Operation("wash", "washer", 5),
                    Operation("read", "plate_reader", 4, max_wait=4),
                ),
            ),
            Job(
                "e2",
                release=2,
                deadline=42,
                assay_tag="elisa",
                ops=(
                    Operation("wash", "washer", 5),
                    Operation("read", "plate_reader", 4, max_wait=4),
                ),
            ),
        ),
    )
    rep_fam = solve_greedy_repair(inst_families)
    assert check_schedule(inst_families, rep_fam).feasible is True
    assert compute_metrics(inst_families, rep_fam).makespan <= 28


def test_cpsat_source_blocking_and_sequence_dependent_setup_times() -> None:
    from labshift.cpsat import solve_cpsat

    # 1. Source-station blocking + transit_matrix + same-pool skip
    inst_block = Instance(
        name="cpsat_arm_blocking",
        capacities={"centrifuge": 1, "plate_reader": 1, "robot_arm": 1},
        transit_matrix={"centrifuge": {"plate_reader": 3}},
        jobs=(
            Job(
                job_id="plate_A",
                ops=(
                    Operation("spin", "centrifuge", duration=10),
                    Operation("read", "plate_reader", duration=4, max_wait=10),
                ),
            ),
            Job(
                job_id="plate_B",
                ops=(Operation("spin", "centrifuge", duration=5),),
            ),
            Job(
                job_id="plate_C",
                ops=(Operation("read_busy", "plate_reader", duration=13),),
            ),
        ),
    )
    sched_block = solve_cpsat(inst_block, time_limit=3.0, num_workers=1, seed=0)
    assert sched_block.status == "OPTIMAL"
    assert check_schedule(inst_block, sched_block).feasible is True

    # 2. Cap=1 asymmetric setup optimization (optimal makespan = 23)
    inst_setup1 = Instance(
        name="cpsat_setup_cap1",
        capacities={"washer": 1, "plate_reader": 1},
        setup_times={
            "washer": {"bsl2": {"elisa": 10}, "elisa": {"bsl2": 2}},
            "plate_reader": {"37C": {"25C": 12}, "25C": {"37C": 4}},
        },
        jobs=(
            Job(
                "job_bsl2",
                release=0,
                ops=(
                    Operation("wash", "washer", 8, setup_tag="bsl2"),
                    Operation("read_37c", "plate_reader", 5, max_wait=6, setup_tag="37C"),
                ),
            ),
            Job(
                "job_elisa",
                release=1,
                ops=(
                    Operation("wash", "washer", 7, setup_tag="elisa"),
                    Operation("read_25c", "plate_reader", 5, max_wait=6, setup_tag="25C"),
                ),
            ),
        ),
    )
    sched_setup1 = solve_cpsat(inst_setup1, time_limit=3.0, num_workers=1, seed=0)
    assert sched_setup1.status == "OPTIMAL"
    assert check_schedule(inst_setup1, sched_setup1).feasible is True
    assert compute_metrics(inst_setup1, sched_setup1).makespan == 23

    # 3. Cap=2 multi-unit setup optimization (segregates incompatible families onto unit 0 & unit 1)
    inst_setup2 = Instance(
        name="cpsat_setup_cap2",
        capacities={"washer": 2},
        setup_times={"washer": {"bsl2": {"elisa": 15}, "elisa": {"bsl2": 15}}},
        jobs=(
            Job("b1", assay_tag="bsl2", ops=(Operation("wash", "washer", 6),)),
            Job("b2", assay_tag="bsl2", ops=(Operation("wash", "washer", 6),)),
            Job("e1", assay_tag="elisa", ops=(Operation("wash", "washer", 6),)),
            Job("e2", assay_tag="elisa", ops=(Operation("wash", "washer", 6),)),
        ),
    )
    sched_setup2 = solve_cpsat(inst_setup2, time_limit=3.0, num_workers=1, seed=0)
    assert sched_setup2.status == "OPTIMAL"
    assert check_schedule(inst_setup2, sched_setup2).feasible is True
    assert compute_metrics(inst_setup2, sched_setup2).makespan == 12


def test_cpsat_overnight_120_jobs_tight_horizon_partial_hints_and_flow_scale() -> None:
    from labshift.cpsat import (
        MAX_MONOLITHIC_FLOW_SCALE,
        compute_horizon_upper_bound,
        select_valid_hint_job_ids,
        solve_cpsat,
    )
    from labshift.generator import PresetConfig, _sample_from_config

    overnight_cfg = PresetConfig(
        num_jobs=120,
        ops_min=4,
        ops_max=5,
        release_max=240,
        capacities={
            "liquid_handler": 2,
            "incubator": 5,
            "plate_reader": 1,
            "centrifuge": 2,
            "washer": 2,
        },
        max_wait_prob=0.65,
        max_wait_range=(6, 14),
        deadline_prob=0.7,
        deadline_slack_factor=1.6,
    )
    inst = _sample_from_config(overnight_cfg, "overnight", 2026, 2026, "overnight_120")
    assert inst.total_operations > 500

    rep = solve_greedy_repair(inst)
    rep_chk = check_schedule(inst, rep)
    assert rep_chk.feasible is False  # misses tight deadlines on a small subset of jobs
    assert rep_chk.counts_by_kind().get("max_wait", 0) == 0

    # 1. Horizon upper bound is ~1,180 min instead of 12,658 min sequential blowup
    tight_ub = compute_horizon_upper_bound(inst, hint_sched=rep, hint_check=rep_chk, hard_deadlines=True)
    assert 1000 <= tight_ub <= 2000

    # 2. Partial warm-start hints retain all >95 non-violating jobs instead of dropping to 0
    wide_var_bounds = {(j.job_id, idx): (0, tight_ub) for j in inst.jobs for idx in range(len(j.ops))}
    hinted_ids = select_valid_hint_job_ids(
        inst, rep, rep_chk, wide_var_bounds, wide_var_bounds, hard_deadlines=True
    )
    assert len(hinted_ids) >= 95
    assert MAX_MONOLITHIC_FLOW_SCALE <= 100_000

    # 3. Soft-deadline + tie_break_flow solve on 120 jobs produces valid low-tardiness schedule
    cp_soft = solve_cpsat(
        inst,
        time_limit=4.0,
        num_workers=2,
        seed=0,
        tie_break_flow=True,
        hard_deadlines=False,
    )
    assert cp_soft.status in ("OPTIMAL", "FEASIBLE")
    soft_chk = check_schedule(inst, cp_soft)
    assert soft_chk.counts_by_kind().get("max_wait", 0) == 0
    assert soft_chk.counts_by_kind().get("capacity", 0) == 0
    soft_m = compute_metrics(inst, cp_soft, check=soft_chk)
    rep_m = compute_metrics(inst, rep, check=rep_chk)
    assert soft_m.makespan is not None and rep_m.makespan is not None
    assert (soft_m.makespan + 10 * soft_m.total_tardiness) <= (
        rep_m.makespan + 10 * rep_m.total_tardiness
    )
