"""OR-Tools CP-SAT exact/time-bounded scheduler."""

from __future__ import annotations

import time
from collections import defaultdict

from ortools.sat.python import cp_model

from labshift.models import Instance, Schedule, ScheduledOp

_STATUS_NAMES = {
    cp_model.OPTIMAL: "OPTIMAL",
    cp_model.FEASIBLE: "FEASIBLE",
    cp_model.INFEASIBLE: "INFEASIBLE",
    cp_model.MODEL_INVALID: "MODEL_INVALID",
    cp_model.UNKNOWN: "UNKNOWN",
}


def solve_cpsat(
    instance: Instance,
    time_limit: float = 5.0,
    num_workers: int = 4,
) -> Schedule:
    """Solve an instance using OR-Tools CP-SAT interval and cumulative constraints."""
    t0 = time.perf_counter()

    if not instance.jobs or all(len(j.ops) == 0 for j in instance.jobs):
        return Schedule(
            instance_name=instance.name,
            solver="cpsat",
            ops=(),
            status="OPTIMAL",
            solve_time_s=time.perf_counter() - t0,
            best_bound=0.0,
        )

    max_release = max((j.release for j in instance.jobs), default=0)
    sum_work = sum(op.duration + op.min_wait for j in instance.jobs for op in j.ops)
    max_deadline = max((j.deadline for j in instance.jobs if j.deadline is not None), default=0)
    horizon = max(max_release + sum_work, max_deadline)

    model = cp_model.CpModel()
    starts: dict[tuple[str, int], cp_model.IntVar] = {}
    ends: dict[tuple[str, int], cp_model.IntVar] = {}
    by_instrument: dict[str, list[cp_model.IntervalVar]] = defaultdict(list)
    job_ends: list[cp_model.IntVar] = []

    for job in instance.jobs:
        if not job.ops:
            continue
        prev_end: cp_model.IntVar | None = None
        for idx, op in enumerate(job.ops):
            lb = job.release if idx == 0 else 0
            s_var = model.new_int_var(lb, horizon, f"s_{job.job_id}_{idx}")
            e_var = model.new_int_var(lb + op.duration, horizon, f"e_{job.job_id}_{idx}")
            iv_var = model.new_interval_var(
                s_var, op.duration, e_var, f"iv_{job.job_id}_{idx}"
            )
            starts[(job.job_id, idx)] = s_var
            ends[(job.job_id, idx)] = e_var
            by_instrument[op.instrument].append(iv_var)

            if prev_end is not None:
                model.add(s_var >= prev_end + op.min_wait)
                if op.max_wait is not None:
                    model.add(s_var <= prev_end + op.max_wait)
            prev_end = e_var

        if prev_end is not None:
            job_ends.append(prev_end)
            if job.deadline is not None:
                model.add(prev_end <= job.deadline)

    for inst, intervals in by_instrument.items():
        cap = instance.capacities.get(inst, 0)
        if cap <= 0:
            return Schedule(
                instance_name=instance.name,
                solver="cpsat",
                ops=(),
                status="INFEASIBLE",
                solve_time_s=time.perf_counter() - t0,
                best_bound=None,
            )
        if cap == 1:
            model.add_no_overlap(intervals)
        else:
            model.add_cumulative(intervals, [1] * len(intervals), cap)

    makespan = model.new_int_var(0, horizon, "makespan")
    model.add_max_equality(makespan, job_ends)
    model.minimize(makespan)

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = float(time_limit)
    if num_workers > 0:
        solver.parameters.num_workers = int(num_workers)

    status_code = solver.solve(model)
    elapsed = time.perf_counter() - t0
    status_name = _STATUS_NAMES.get(status_code, "UNKNOWN")

    if status_code not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return Schedule(
            instance_name=instance.name,
            solver="cpsat",
            ops=(),
            status=status_name,
            solve_time_s=elapsed,
            best_bound=None,
        )

    scheduled: list[ScheduledOp] = []
    for job in instance.jobs:
        for idx, op in enumerate(job.ops):
            s_val = int(solver.value(starts[(job.job_id, idx)]))
            e_val = int(solver.value(ends[(job.job_id, idx)]))
            scheduled.append(
                ScheduledOp(
                    job_id=job.job_id,
                    op_index=idx,
                    name=op.name,
                    instrument=op.instrument,
                    start=s_val,
                    end=e_val,
                )
            )

    best_bound = float(solver.best_objective_bound)
    return Schedule(
        instance_name=instance.name,
        solver="cpsat",
        ops=tuple(scheduled),
        status=status_name,
        solve_time_s=elapsed,
        best_bound=best_bound,
    )
