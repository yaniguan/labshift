"""OR-Tools CP-SAT exact/time-bounded scheduler."""

from __future__ import annotations

import math
import time
from collections import defaultdict

try:
    from ortools.sat.python import cp_model
except ImportError as exc:
    raise ImportError("Install ortools>=9.8 to use the 'cpsat' solver.") from exc

from labshift.checker import check_schedule
from labshift.greedy import solve_greedy, solve_greedy_repair
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
    seed: int | None = None,
    tie_break_flow: bool = False,
    hard_deadlines: bool = True,
    tardiness_weight: int = 10,
) -> Schedule:
    """Solve an instance using OR-Tools CP-SAT interval and cumulative constraints.

    Args:
        instance: Problem instance to schedule.
        time_limit: Wall-clock time budget in seconds (> 0).
        num_workers: Parallel CP-SAT search workers (>= 1). Set to 1 with ``seed``
            for deterministic execution.
        seed: Optional random seed passed to ``solver.parameters.random_seed``.
        tie_break_flow: When True, lexicographically minimizes total job flow time
            as a subordinate tie-breaker beneath makespan.
        hard_deadlines: When True (default), job deadlines are enforced as hard
            constraints. When False, deadlines are relaxed and weighted total
            tardiness is penalized in the objective.
        tardiness_weight: Per-minute tardiness weight relative to makespan when
            ``hard_deadlines=False`` (>= 1).
    """
    if isinstance(time_limit, bool) or not isinstance(time_limit, (int, float)):
        raise TypeError(f"'time_limit' must be a number, got {time_limit!r}.")
    if not math.isfinite(float(time_limit)) or time_limit <= 0:
        raise ValueError(f"'time_limit' must be a positive finite number, got {time_limit!r}.")
    if isinstance(num_workers, bool) or not isinstance(num_workers, int):
        raise TypeError(f"'num_workers' must be an integer, got {num_workers!r}.")
    if num_workers < 1:
        raise ValueError(f"'num_workers' must be >= 1, got {num_workers}.")
    if seed is not None and (isinstance(seed, bool) or not isinstance(seed, int)):
        raise TypeError(f"'seed' must be an integer or None, got {seed!r}.")
    if isinstance(tardiness_weight, bool) or not isinstance(tardiness_weight, int):
        raise TypeError(f"'tardiness_weight' must be an integer, got {tardiness_weight!r}.")
    if tardiness_weight < 1:
        raise ValueError(f"'tardiness_weight' must be >= 1, got {tardiness_weight}.")

    t0 = time.perf_counter()

    if instance.total_operations == 0:
        return Schedule(
            instance_name=instance.name,
            solver="cpsat",
            ops=(),
            status="OPTIMAL",
            solve_time_s=time.perf_counter() - t0,
            best_bound=0.0,
        )

    use_arm = "robot_arm" in instance.capacities
    for job in instance.jobs:
        for idx, op in enumerate(job.ops):
            if op.instrument not in instance.capacities or instance.capacities[op.instrument] <= 0:
                return Schedule(
                    instance_name=instance.name,
                    solver="cpsat",
                    ops=(),
                    status="INFEASIBLE",
                    solve_time_s=time.perf_counter() - t0,
                    best_bound=None,
                )
            if use_arm and idx > 0 and op.transfer_time > 0 and instance.capacities["robot_arm"] <= 0:
                return Schedule(
                    instance_name=instance.name,
                    solver="cpsat",
                    ops=(),
                    status="INFEASIBLE",
                    solve_time_s=time.perf_counter() - t0,
                    best_bound=None,
                )

    # Fast heuristic warm-start (< 1 ms) for upper-bound tightening and solver hints.
    hint_sched = solve_greedy(instance)
    hint_check = check_schedule(instance, hint_sched)
    hint_valid = (
        hint_check.feasible
        if hard_deadlines
        else (
            bool(hint_sched.ops)
            and all(v.kind == "deadline" for v in hint_check.violations)
        )
    )
    if not hint_valid:
        hint_sched = solve_greedy_repair(instance)
        hint_check = check_schedule(instance, hint_sched)
        hint_valid = (
            hint_check.feasible
            if hard_deadlines
            else (
                bool(hint_sched.ops)
                and all(v.kind == "deadline" for v in hint_check.violations)
            )
        )

    max_release = max((j.release for j in instance.jobs), default=0)
    sum_work = sum(
        op.duration + (max(op.min_wait, op.transfer_time) if idx > 0 else 0)
        for j in instance.jobs
        for idx, op in enumerate(j.ops)
    )
    max_deadline = max((j.deadline for j in instance.jobs if j.deadline is not None), default=0)
    base_horizon = max(max_release + 2 * sum_work, max_deadline, 1)

    hint_ms = max((sop.end for sop in hint_sched.ops), default=0) if hint_valid else 0
    if hint_valid and hard_deadlines:
        horizon = max(hint_ms, 1)
    elif hint_valid and not hard_deadlines:
        hint_tard = 0
        hint_by_key = hint_sched.by_key()
        for j in instance.jobs:
            if j.ops and j.deadline is not None:
                last_sop = hint_by_key.get((j.job_id, len(j.ops) - 1))
                if last_sop is not None and last_sop.end > j.deadline:
                    hint_tard += last_sop.end - j.deadline
        horizon = max(min(base_horizon, hint_ms + tardiness_weight * hint_tard), hint_ms, 1)
    else:
        horizon = base_horizon

    model = cp_model.CpModel()
    starts: dict[tuple[str, int], cp_model.IntVar] = {}
    ends: dict[tuple[str, int], cp_model.IntVar] = {}
    var_bounds: dict[tuple[str, int], tuple[int, int]] = {}
    arm_starts: dict[tuple[str, int], cp_model.IntVar] = {}
    by_instrument: dict[str, list[cp_model.IntervalVar]] = defaultdict(list)
    job_ends: list[cp_model.IntVar] = []
    flow_terms: list[cp_model.LinearExpr] = []
    tard_vars: list[cp_model.IntVar] = []

    for job in instance.jobs:
        m = len(job.ops)
        if m == 0:
            continue

        # Forward pass: earliest start time per operation.
        est = [0] * m
        est[0] = job.release
        for idx in range(1, m):
            eff_wait = max(job.ops[idx].min_wait, job.ops[idx].transfer_time)
            est[idx] = est[idx - 1] + job.ops[idx - 1].duration + eff_wait

        # Backward pass: latest completion time per operation.
        job_ub = (
            min(horizon, job.deadline)
            if (hard_deadlines and job.deadline is not None)
            else horizon
        )
        lct = [job_ub] * m
        for idx in range(m - 2, -1, -1):
            eff_next_wait = max(
                job.ops[idx + 1].min_wait, job.ops[idx + 1].transfer_time
            )
            lct[idx] = lct[idx + 1] - job.ops[idx + 1].duration - eff_next_wait

        prev_end: cp_model.IntVar | None = None
        for idx, op in enumerate(job.ops):
            s_lb = est[idx]
            s_ub = lct[idx] - op.duration
            e_lb = est[idx] + op.duration
            e_ub = lct[idx]
            if s_lb > s_ub or e_lb > e_ub:
                return Schedule(
                    instance_name=instance.name,
                    solver="cpsat",
                    ops=(),
                    status="INFEASIBLE",
                    solve_time_s=time.perf_counter() - t0,
                    best_bound=None,
                )

            s_var = model.new_int_var(s_lb, s_ub, f"s_{job.job_id}_{idx}")
            e_var = model.new_int_var(e_lb, e_ub, f"e_{job.job_id}_{idx}")
            iv_var = model.new_interval_var(
                s_var, op.duration, e_var, f"iv_{job.job_id}_{idx}"
            )
            key = (job.job_id, idx)
            starts[key] = s_var
            ends[key] = e_var
            var_bounds[key] = (s_lb, s_ub)
            by_instrument[op.instrument].append(iv_var)

            if prev_end is not None:
                eff_min_wait = max(op.min_wait, op.transfer_time)
                model.add(s_var >= prev_end + eff_min_wait)
                if op.max_wait is not None:
                    model.add(s_var <= prev_end + op.max_wait)
                if use_arm and op.transfer_time > 0:
                    arm_s = model.new_int_var(
                        max(0, s_lb - op.transfer_time),
                        max(0, s_ub - op.transfer_time),
                        f"arm_s_{job.job_id}_{idx}",
                    )
                    model.add(arm_s == s_var - op.transfer_time)
                    arm_iv = model.new_interval_var(
                        arm_s, op.transfer_time, s_var, f"arm_iv_{job.job_id}_{idx}"
                    )
                    arm_starts[key] = arm_s
                    by_instrument["robot_arm"].append(arm_iv)
            prev_end = e_var

        assert prev_end is not None
        job_ends.append(prev_end)
        flow_terms.append(prev_end - job.release)
        if job.deadline is not None:
            if hard_deadlines:
                model.add(prev_end <= job.deadline)
            else:
                max_tard = max(0, horizon - job.deadline)
                t_var = model.new_int_var(0, max_tard, f"tard_{job.job_id}")
                model.add(t_var >= prev_end - job.deadline)
                tard_vars.append(t_var)

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

    primary_obj: cp_model.LinearExpr | cp_model.IntVar
    if not hard_deadlines and tard_vars:
        primary_obj = makespan + tardiness_weight * sum(tard_vars)
    else:
        primary_obj = makespan

    flow_scale = 1
    if tie_break_flow and flow_terms:
        flow_scale = len(flow_terms) * horizon + 1
        model.minimize(primary_obj * flow_scale + sum(flow_terms))
    else:
        model.minimize(primary_obj)

    if hint_valid:
        hint_by_key = hint_sched.by_key()
        all_in_bounds = all(
            key in hint_by_key
            and var_bounds[key][0] <= hint_by_key[key].start <= var_bounds[key][1]
            for key in starts
        )
        if all_in_bounds:
            for job in instance.jobs:
                for idx, op in enumerate(job.ops):
                    key = (job.job_id, idx)
                    sop = hint_by_key[key]
                    model.add_hint(starts[key], sop.start)
                    model.add_hint(ends[key], sop.end)
                    if key in arm_starts:
                        model.add_hint(arm_starts[key], sop.start - op.transfer_time)
            model.add_hint(makespan, hint_ms)
            if tard_vars:
                tard_idx = 0
                for job in instance.jobs:
                    if job.ops and job.deadline is not None:
                        last_sop = hint_by_key[(job.job_id, len(job.ops) - 1)]
                        model.add_hint(tard_vars[tard_idx], max(0, last_sop.end - job.deadline))
                        tard_idx += 1

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = float(time_limit)
    solver.parameters.num_workers = int(num_workers)
    if seed is not None:
        solver.parameters.random_seed = int(seed)

    status_code = solver.solve(model)
    elapsed = time.perf_counter() - t0
    status_name = _STATUS_NAMES.get(status_code, "UNKNOWN")

    def _extract_makespan_bound(raw_bound: float) -> float | None:
        if not hard_deadlines and tard_vars:
            return None
        if not math.isfinite(raw_bound):
            return None
        if flow_scale > 1:
            unscaled = max(
                0, math.ceil((raw_bound - (flow_scale - 1) - 1e-7) / flow_scale)
            )
            return float(unscaled)
        return float(raw_bound)

    if status_code not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        unknown_bound: float | None = None
        if status_code == cp_model.UNKNOWN and solver.best_objective_bound > 0:
            cand_bound = _extract_makespan_bound(float(solver.best_objective_bound))
            if cand_bound is not None and cand_bound > 0:
                unknown_bound = cand_bound
        return Schedule(
            instance_name=instance.name,
            solver="cpsat",
            ops=(),
            status=status_name,
            solve_time_s=elapsed,
            best_bound=unknown_bound,
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

    best_bound = _extract_makespan_bound(float(solver.best_objective_bound))
    return Schedule(
        instance_name=instance.name,
        solver="cpsat",
        ops=tuple(scheduled),
        status=status_name,
        solve_time_s=elapsed,
        best_bound=best_bound,
    )
