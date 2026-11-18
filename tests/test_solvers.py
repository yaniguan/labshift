"""Tests for greedy and CP-SAT solvers and metrics."""

from __future__ import annotations

import pytest

from labshift.checker import check_schedule
from labshift.cpsat import solve_cpsat
from labshift.generator import generate_instance
from labshift.greedy import solve_greedy
from labshift.metrics import compute_metrics
from labshift.models import Instance, Job, Operation


@pytest.mark.parametrize("seed", [0, 1, 2, 3, 4])
def test_small_instances_feasible_and_cpsat_beats_or_ties_greedy(seed: int) -> None:
    inst = generate_instance(preset="small", seed=seed)

    g_sched = solve_greedy(inst)
    g_check = check_schedule(inst, g_sched)
    assert g_check.feasible, f"Greedy infeasible on seed {seed}: {g_check.violations}"
    g_metrics = compute_metrics(inst, g_sched, check=g_check)

    c_sched = solve_cpsat(inst, time_limit=5.0)
    c_check = check_schedule(inst, c_sched)
    assert c_check.feasible, f"CP-SAT infeasible on seed {seed}: {c_check.violations}"
    assert c_sched.status in ("OPTIMAL", "FEASIBLE")
    c_metrics = compute_metrics(inst, c_sched, check=c_check)

    assert c_metrics.makespan <= g_metrics.makespan
    assert c_metrics.gap_pct is not None and c_metrics.gap_pct >= 0.0


def test_cpsat_respects_tight_max_wait_where_greedy_violates() -> None:
    # Job 1 finishes liquid_handler at t=10 with max_wait=0 into plate_reader.
    # Job 2 has an earlier ready time on plate_reader at t=2 (duration 10) that blocks Job 1
    # if dispatched greedily without lookahead.
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
                ops=(
                    Operation("read_only", "plate_reader", duration=10),
                ),
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


def test_cpsat_reports_infeasible_on_impossible_deadline() -> None:
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
    sched = solve_cpsat(inst, time_limit=2.0)
    assert sched.status == "INFEASIBLE"
    assert sched.ops == ()
