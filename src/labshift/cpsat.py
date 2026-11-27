"""OR-Tools CP-SAT exact/time-bounded scheduler."""

from __future__ import annotations

import math
import time
from collections import defaultdict
from typing import Any

try:
    from ortools.sat.python import cp_model
except ImportError as exc:
    raise ImportError("Install ortools>=9.8 to use the 'cpsat' solver.") from exc

from labshift.bounds import makespan_lower_bound
from labshift.checker import CheckResult, check_schedule
from labshift.greedy import (
    _has_infeasible_capacity,
    _run_coupled_pass,
    solve_greedy,
    solve_greedy_repair,
)
from labshift.models import Instance, Job, Schedule, ScheduledOp

_STATUS_NAMES = {
    cp_model.OPTIMAL: "OPTIMAL",
    cp_model.FEASIBLE: "FEASIBLE",
    cp_model.INFEASIBLE: "INFEASIBLE",
    cp_model.MODEL_INVALID: "MODEL_INVALID",
    cp_model.UNKNOWN: "UNKNOWN",
}

# Cap monolithic scalarized flow_scale so objective coefficients never inflate on >100-job batches.
MAX_MONOLITHIC_FLOW_SCALE: int = 50_000


def _max_setup_entry(instance: Instance) -> int:
    if not instance.setup_times:
        return 0
    return max(
        (
            val
            for inst_map in instance.setup_times.values()
            for row in inst_map.values()
            for val in row.values()
        ),
        default=0,
    )


def _sequential_fallback_horizon(instance: Instance) -> int:
    max_release = max((j.release for j in instance.jobs), default=0)
    max_setup_by_inst: dict[str, int] = {}
    if instance.setup_times:
        for inst, inst_map in instance.setup_times.items():
            max_setup_by_inst[inst] = max(
                (val for row in inst_map.values() for val in row.values()),
                default=0,
            )
    sum_work = sum(
        op.duration
        + (
            max(op.min_wait, instance.effective_transfer_time(j.ops[idx - 1].instrument, op))
            if idx > 0
            else 0
        )
        + max_setup_by_inst.get(op.instrument, 0)
        for j in instance.jobs
        for idx, op in enumerate(j.ops)
    )
    max_deadline = max((j.deadline for j in instance.jobs if j.deadline is not None), default=0)
    return max(max_release + 2 * sum_work, max_deadline, 1)


def _schedule_tardiness(instance: Instance, schedule: Schedule) -> int:
    by_key = schedule.by_key()
    tard = 0
    for j in instance.jobs:
        if j.ops and j.deadline is not None:
            last_sop = by_key.get((j.job_id, len(j.ops) - 1))
            if last_sop is not None and last_sop.end > j.deadline:
                tard += last_sop.end - j.deadline
    return tard


def compute_horizon_upper_bound(
    instance: Instance,
    *,
    hint_sched: Schedule | None = None,
    hint_check: CheckResult | None = None,
    hard_deadlines: bool = True,
    tardiness_weight: int = 10,
) -> int:
    """Compute a tight, mathematically sound CP-SAT time-horizon upper bound.

    Avoids the 10x-15x ``max_release + 2 * sum_work`` blowup on >100-job batches when a
    warm-start misses one or more hard deadlines by combining:
      1. A constructive deadline-relaxed window/capacity/setup-valid schedule ``ub_sched``.
      2. For ``hard_deadlines=True``, the exact tail bound: every deadlined job must finish
         by ``max_deadline``, and scheduling all un-deadlined jobs starting at
         ``max(release, max_deadline + max_setup)`` yields a constructive upper bound ``tail_ms``
         that no optimal hard-deadline schedule can exceed.
      3. For ``hard_deadlines=False``, the incumbent soft-deadline objective bound.
    """
    if instance.total_operations == 0:
        return 1

    seq_horizon = _sequential_fallback_horizon(instance)
    if _has_infeasible_capacity(instance):
        return seq_horizon

    if hint_sched is None:
        hint_sched = solve_greedy(instance)
        hint_check = check_schedule(instance, hint_sched)
        non_dl_ok = bool(hint_sched.ops) and all(
            v.kind == "deadline" for v in hint_check.violations
        )
        if not (hint_check.feasible if hard_deadlines else non_dl_ok):
            hint_sched = solve_greedy_repair(instance)
            hint_check = check_schedule(instance, hint_sched)
    elif hint_check is None:
        hint_check = check_schedule(instance, hint_sched)

    if hint_check.feasible and hard_deadlines:
        hint_ms = max((sop.end for sop in hint_sched.ops), default=0)
        return max(hint_ms, 1)

    ub_sched = hint_sched
    ub_check = hint_check
    if not (bool(ub_sched.ops) and all(v.kind == "deadline" for v in ub_check.violations)):
        ub_ops = _run_coupled_pass(instance, "earliest", allow_deadline_prune=False)
        ub_sched = Schedule(
            instance_name=instance.name,
            solver="ub_constructive",
            ops=tuple(ub_ops),
            status="OK",
        )
        ub_check = check_schedule(instance, ub_sched)

    if not (bool(ub_sched.ops) and all(v.kind == "deadline" for v in ub_check.violations)):
        return seq_horizon

    ub_ms = max((sop.end for sop in ub_sched.ops), default=0)
    max_deadline = max((j.deadline for j in instance.jobs if j.deadline is not None), default=0)
    max_setup = _max_setup_entry(instance)

    undeadlined_jobs = [j for j in instance.jobs if j.deadline is None and j.ops]

    if hard_deadlines:
        if not undeadlined_jobs:
            # Every non-empty job has a hard deadline <= max_deadline.
            return max(max_deadline, 1)

        shifted_tail_jobs = tuple(
            Job(
                job_id=j.job_id,
                ops=j.ops,
                release=max(j.release, max_deadline + max_setup),
                deadline=None,
                assay_tag=j.assay_tag,
            )
            for j in undeadlined_jobs
        )
        tail_inst = Instance(
            name=f"{instance.name}_tail",
            capacities=dict(instance.capacities),
            jobs=shifted_tail_jobs,
            transit_matrix=instance.transit_matrix,
            setup_times=instance.setup_times,
            blocking_handoff=instance.blocking_handoff,
        )
        tail_ops = _run_coupled_pass(tail_inst, "earliest", allow_deadline_prune=False)
        tail_ms = max((sop.end for sop in tail_ops), default=max_deadline)
        return min(seq_horizon, max(ub_ms, max_deadline, tail_ms, 1))

    # Soft deadlines: ub_sched is feasible with objective incumbent_obj.
    ub_tard = _schedule_tardiness(instance, ub_sched)
    incumbent_obj = ub_ms + tardiness_weight * ub_tard
    if not undeadlined_jobs:
        soft_d_max = max_deadline + (incumbent_obj // max(1, tardiness_weight))
        return min(seq_horizon, incumbent_obj, max(ub_ms, soft_d_max, 1))

    soft_d_max = max_deadline + (incumbent_obj // max(1, tardiness_weight))
    shifted_soft_jobs = tuple(
        Job(
            job_id=j.job_id,
            ops=j.ops,
            release=max(j.release, soft_d_max + max_setup),
            deadline=None,
            assay_tag=j.assay_tag,
        )
        for j in undeadlined_jobs
    )
    soft_tail_inst = Instance(
        name=f"{instance.name}_soft_tail",
        capacities=dict(instance.capacities),
        jobs=shifted_soft_jobs,
        transit_matrix=instance.transit_matrix,
        setup_times=instance.setup_times,
        blocking_handoff=instance.blocking_handoff,
    )
    soft_tail_ops = _run_coupled_pass(soft_tail_inst, "earliest", allow_deadline_prune=False)
    soft_tail_ms = max((sop.end for sop in soft_tail_ops), default=soft_d_max)
    return min(seq_horizon, incumbent_obj, max(ub_ms, soft_tail_ms, 1))


def select_valid_hint_job_ids(
    instance: Instance,
    hint_sched: Schedule,
    hint_check: CheckResult,
    var_bounds: dict[tuple[str, int], tuple[int, int]],
    end_bounds: dict[tuple[str, int], tuple[int, int]],
    *,
    hard_deadlines: bool = True,
) -> set[str]:
    """Identify the subset of jobs whose warm-start operations satisfy all constraints and bounds."""
    if not hint_sched.ops:
        return set()

    # If any global instrument-level violation (capacity / setup without job_id) occurred,
    # exclude jobs touching that instrument; job-scoped violations exclude only that job_id.
    violated_jobs: set[str] = set()
    tainted_instruments: set[str] = set()
    for v in hint_check.violations:
        if not hard_deadlines and v.kind == "deadline":
            continue
        if v.job_id is not None:
            violated_jobs.add(v.job_id)
        elif v.instrument is not None:
            tainted_instruments.add(v.instrument)

    hint_by_key = hint_sched.by_key()
    valid_ids: set[str] = set()
    for job in instance.jobs:
        if not job.ops or job.job_id in violated_jobs:
            continue
        if any(op.instrument in tainted_instruments for op in job.ops):
            continue
        if "robot_arm" in tainted_instruments and any(
            idx > 0 and instance.uses_robot_arm(job.ops[idx - 1].instrument, op)
            for idx, op in enumerate(job.ops)
        ):
            continue

        in_bounds = True
        for idx, _ in enumerate(job.ops):
            key = (job.job_id, idx)
            sop = hint_by_key.get(key)
            if sop is None or key not in var_bounds or key not in end_bounds:
                in_bounds = False
                break
            s_lb, s_ub = var_bounds[key]
            e_lb, e_ub = end_bounds[key]
            if not (s_lb <= sop.start <= s_ub and e_lb <= sop.end <= e_ub):
                in_bounds = False
                break
        if in_bounds:
            valid_ids.add(job.job_id)

    return valid_ids


def solve_cpsat(
    instance: Instance,
    time_limit: float = 5.0,
    num_workers: int = 4,
    seed: int | None = None,
    tie_break_flow: bool = False,
    hard_deadlines: bool = True,
    tardiness_weight: int = 10,
    fixed_ops: dict[tuple[str, int], tuple[int, int]] | None = None,
) -> Schedule:
    """Solve an instance using OR-Tools CP-SAT interval, cumulative, and circuit constraints.

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
        fixed_ops: Optional mapping ``(job_id, op_index) -> (start, end)`` locking
            already-started in-flight operations for rolling-horizon rescheduling.
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

    if _has_infeasible_capacity(instance):
        return Schedule(
            instance_name=instance.name,
            solver="cpsat",
            ops=(),
            status="INFEASIBLE",
            solve_time_s=time.perf_counter() - t0,
            best_bound=None,
        )

    # Heuristic warm-start for tight horizon computation and full/partial solver hints.
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
    if fixed_ops:
        hint_by_k = hint_sched.by_key()
        if any(
            k not in hint_by_k or (hint_by_k[k].start, hint_by_k[k].end) != span
            for k, span in fixed_ops.items()
        ):
            hint_valid = False

    if fixed_ops:
        horizon = _sequential_fallback_horizon(instance)
    else:
        horizon = compute_horizon_upper_bound(
            instance,
            hint_sched=hint_sched,
            hint_check=hint_check,
            hard_deadlines=hard_deadlines,
            tardiness_weight=tardiness_weight,
        )

    hint_ms = max((sop.end for sop in hint_sched.ops), default=0) if hint_sched.ops else 0
    incumbent_soft_obj: int | None = None
    if not hard_deadlines and hint_valid and not fixed_ops:
        incumbent_soft_obj = hint_ms + tardiness_weight * _schedule_tardiness(instance, hint_sched)

    model = cp_model.CpModel()
    starts: dict[tuple[str, int], cp_model.IntVar] = {}
    ends: dict[tuple[str, int], cp_model.IntVar] = {}
    var_bounds: dict[tuple[str, int], tuple[int, int]] = {}
    end_bounds: dict[tuple[str, int], tuple[int, int]] = {}
    arm_starts: dict[tuple[str, int], cp_model.IntVar] = {}
    res_ends: dict[tuple[str, int], cp_model.IntVar] = {}
    res_durs: dict[tuple[str, int], cp_model.IntVar | int] = {}
    res_end_bounds: dict[tuple[str, int], tuple[int, int]] = {}
    by_instrument: dict[str, list[cp_model.IntervalVar]] = defaultdict(list)
    inst_op_keys: dict[str, list[tuple[tuple[str, int], str | None]]] = defaultdict(list)
    unit_pres: dict[tuple[str, int], list[cp_model.IntVar]] = {}
    job_ends: list[cp_model.IntVar] = []
    flow_terms: list[cp_model.LinearExpr] = []
    flow_span_sum = 0
    tard_vars: list[cp_model.IntVar] = []

    for job in instance.jobs:
        m = len(job.ops)
        if m == 0:
            continue

        # Forward pass: earliest start time per operation.
        est = [0] * m
        est[0] = job.release
        if fixed_ops and (job.job_id, 0) in fixed_ops:
            est[0] = max(est[0], fixed_ops[(job.job_id, 0)][0])
        for idx in range(1, m):
            eff_wait = max(
                job.ops[idx].min_wait,
                instance.effective_transfer_time(job.ops[idx - 1].instrument, job.ops[idx]),
            )
            est[idx] = est[idx - 1] + job.ops[idx - 1].duration + eff_wait
            if fixed_ops and (job.job_id, idx) in fixed_ops:
                est[idx] = max(est[idx], fixed_ops[(job.job_id, idx)][0])

        # Backward pass: latest completion time per operation.
        job_ub = (
            min(horizon, job.deadline)
            if (hard_deadlines and job.deadline is not None)
            else horizon
        )
        lct = [job_ub] * m
        if fixed_ops and (job.job_id, m - 1) in fixed_ops:
            lct[m - 1] = min(lct[m - 1], fixed_ops[(job.job_id, m - 1)][1])
        for idx in range(m - 2, -1, -1):
            eff_next_wait = max(
                job.ops[idx + 1].min_wait,
                instance.effective_transfer_time(job.ops[idx].instrument, job.ops[idx + 1]),
            )
            lct[idx] = lct[idx + 1] - job.ops[idx + 1].duration - eff_next_wait
            if fixed_ops and (job.job_id, idx) in fixed_ops:
                lct[idx] = min(lct[idx], fixed_ops[(job.job_id, idx)][1])

        # Create start/end/arm variables for all ops in this job first so blocking handoff
        # on op idx can reference arm_starts[(job_id, idx + 1)].
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

            key = (job.job_id, idx)
            s_var = model.new_int_var(s_lb, s_ub, f"s_{job.job_id}_{idx}")
            e_var = model.new_int_var(e_lb, e_ub, f"e_{job.job_id}_{idx}")
            model.add(e_var == s_var + op.duration)
            if fixed_ops and key in fixed_ops:
                fix_s, fix_e = fixed_ops[key]
                model.add(s_var == fix_s)
                model.add(e_var == fix_e)

            starts[key] = s_var
            ends[key] = e_var
            var_bounds[key] = (s_lb, s_ub)
            end_bounds[key] = (e_lb, e_ub)

            if idx > 0:
                prev_e = ends[(job.job_id, idx - 1)]
                eff_t = instance.effective_transfer_time(job.ops[idx - 1].instrument, op)
                eff_min_wait = max(op.min_wait, eff_t)
                model.add(s_var >= prev_e + eff_min_wait)
                if op.max_wait is not None:
                    model.add(s_var <= prev_e + op.max_wait)
                if instance.uses_robot_arm(job.ops[idx - 1].instrument, op):
                    arm_lb = max(0, s_lb - eff_t)
                    arm_ub = max(0, s_ub - eff_t)
                    arm_s = model.new_int_var(arm_lb, arm_ub, f"arm_s_{job.job_id}_{idx}")
                    model.add(arm_s == s_var - eff_t)
                    arm_iv = model.new_interval_var(
                        arm_s, eff_t, s_var, f"arm_iv_{job.job_id}_{idx}"
                    )
                    arm_starts[key] = arm_s
                    by_instrument["robot_arm"].append(arm_iv)

        # Build station residence IntervalVars (accounting for source-station blocking until pickup)
        for idx, op in enumerate(job.ops):
            key = (job.job_id, idx)
            s_var = starts[key]
            e_var = ends[key]
            s_lb, _ = var_bounds[key]
            e_lb, e_ub = end_bounds[key]
            next_key = (job.job_id, idx + 1)
            blocking_arm = (
                idx + 1 < m
                and instance.blocking_handoff
                and next_key in arm_starts
            )
            if blocking_arm:
                next_op = job.ops[idx + 1]
                eff_next_t = instance.effective_transfer_time(op.instrument, next_op)
                arm_next_s = arm_starts[next_key]
                next_s_ub = var_bounds[next_key][1]
                r_ub = max(e_ub, next_s_ub - eff_next_t)
                r_dur_ub = max(op.duration, r_ub - s_lb)
                r_dur = model.new_int_var(op.duration, r_dur_ub, f"rdur_{job.job_id}_{idx}")
                model.add(r_dur == arm_next_s - s_var)
                iv_var = model.new_interval_var(
                    s_var, r_dur, arm_next_s, f"iv_{job.job_id}_{idx}"
                )
                res_ends[key] = arm_next_s
                res_durs[key] = r_dur
                res_end_bounds[key] = (e_lb, r_ub)
            else:
                iv_var = model.new_interval_var(
                    s_var, op.duration, e_var, f"iv_{job.job_id}_{idx}"
                )
                res_ends[key] = e_var
                res_durs[key] = op.duration
                res_end_bounds[key] = (e_lb, e_ub)

            by_instrument[op.instrument].append(iv_var)
            inst_op_keys[op.instrument].append((key, instance.effective_setup_tag(job, op)))

        last_key = (job.job_id, m - 1)
        last_end = ends[last_key]
        last_e_lb, last_e_ub = end_bounds[last_key]
        job_ends.append(last_end)
        # Subtract constant lower bound last_e_lb so 0 <= flow_excess <= (last_e_ub - last_e_lb).
        flow_terms.append(last_end - last_e_lb)
        flow_span_sum += max(0, last_e_ub - last_e_lb)

        if job.deadline is not None:
            if hard_deadlines:
                model.add(last_end <= job.deadline)
            else:
                max_tard = max(0, horizon - job.deadline)
                t_var = model.new_int_var(0, max_tard, f"tard_{job.job_id}")
                model.add(t_var >= last_end - job.deadline)
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

        has_setup = instance.has_setup_times_for(inst)
        op_list = inst_op_keys.get(inst, [])

        if not has_setup or len(op_list) <= 1:
            if cap == 1:
                model.add_no_overlap(intervals)
            else:
                model.add_cumulative(intervals, [1] * len(intervals), cap)
            continue

        if cap == 1:
            model.add_no_overlap(intervals)
            # Exact transition circuit over depot 0 and operations 1..K
            arcs: list[tuple[int, int, cp_model.IntVar]] = []
            for i, (key_i, tag_i) in enumerate(op_list):
                arcs.append((0, i + 1, model.new_bool_var(f"c0_{inst}_{i}")))
                arcs.append((i + 1, 0, model.new_bool_var(f"c_{inst}_{i}_0")))
                re_i = res_ends[key_i]
                re_i_lb = res_end_bounds[key_i][0]
                for j, (key_j, tag_j) in enumerate(op_list):
                    if i == j:
                        continue
                    req_ij = instance.required_setup_time(inst, tag_i, tag_j)
                    s_j_ub = var_bounds[key_j][1]
                    if s_j_ub < re_i_lb + req_ij:
                        continue
                    lit_ij = model.new_bool_var(f"c_{inst}_{i}_{j}")
                    arcs.append((i + 1, j + 1, lit_ij))
                    model.add(starts[key_j] >= re_i + req_ij).only_enforce_if(lit_ij)
            model.add_circuit(arcs)
        else:
            model.add_cumulative(intervals, [1] * len(intervals), cap)
            for i, (key_i, _) in enumerate(op_list):
                p_vars: list[cp_model.IntVar] = []
                for u in range(cap):
                    p_var = model.new_bool_var(f"u_{inst}_{i}_{u}")
                    if i == 0 and u > 0:
                        # Symmetry breaking: anchor first operation to unit 0
                        model.add(p_var == 0)
                    p_vars.append(p_var)
                model.add_exactly_one(p_vars)
                unit_pres[key_i] = p_vars

            for u in range(cap):
                u_ivs: list[cp_model.IntervalVar] = []
                u_arcs: list[tuple[int, int, Any]] = [
                    (0, 0, model.new_bool_var(f"u_empty_{inst}_{u}"))
                ]
                for i, (key_i, tag_i) in enumerate(op_list):
                    p_iu = unit_pres[key_i][u]
                    u_iv = model.new_optional_interval_var(
                        starts[key_i],
                        res_durs[key_i],
                        res_ends[key_i],
                        p_iu,
                        f"u_iv_{inst}_{i}_{u}",
                    )
                    u_ivs.append(u_iv)
                    # Self-loop when op i is not assigned to unit u
                    u_arcs.append((i + 1, i + 1, p_iu.negated()))

                    f_iu = model.new_bool_var(f"uf0_{inst}_{u}_{i}")
                    l_iu = model.new_bool_var(f"ul0_{inst}_{u}_{i}")
                    model.add_implication(f_iu, p_iu)
                    model.add_implication(l_iu, p_iu)
                    u_arcs.append((0, i + 1, f_iu))
                    u_arcs.append((i + 1, 0, l_iu))

                    re_i = res_ends[key_i]
                    re_i_lb = res_end_bounds[key_i][0]
                    for j, (key_j, tag_j) in enumerate(op_list):
                        if i == j:
                            continue
                        req_ij = instance.required_setup_time(inst, tag_i, tag_j)
                        if var_bounds[key_j][1] < re_i_lb + req_ij:
                            continue
                        lit_u_ij = model.new_bool_var(f"uc_{inst}_{u}_{i}_{j}")
                        model.add_implication(lit_u_ij, p_iu)
                        model.add_implication(lit_u_ij, unit_pres[key_j][u])
                        model.add(starts[key_j] >= re_i + req_ij).only_enforce_if(lit_u_ij)
                        u_arcs.append((i + 1, j + 1, lit_u_ij))

                model.add_no_overlap(u_ivs)
                model.add_circuit(u_arcs)

    lb = makespan_lower_bound(instance)
    if not hard_deadlines:
        horizon = max(horizon, lb)

    makespan = model.new_int_var(0, max(horizon, lb), "makespan")
    model.add_max_equality(makespan, job_ends)
    model.add(makespan <= horizon)
    model.add(makespan >= lb)

    primary_obj: cp_model.LinearExpr | cp_model.IntVar
    if not hard_deadlines and tard_vars:
        primary_obj = makespan + tardiness_weight * sum(tard_vars)
        if incumbent_soft_obj is not None:
            model.add(primary_obj <= incumbent_soft_obj)
    else:
        primary_obj = makespan

    # Inject either full warm-start hints or partial conflict-free hints for non-violating jobs.
    hint_job_ids = select_valid_hint_job_ids(
        instance,
        hint_sched,
        hint_check,
        var_bounds,
        end_bounds,
        hard_deadlines=hard_deadlines,
    )
    if hint_job_ids and not fixed_ops:
        hint_by_key = hint_sched.by_key()
        for job in instance.jobs:
            if job.job_id not in hint_job_ids:
                continue
            for idx, op in enumerate(job.ops):
                key = (job.job_id, idx)
                sop = hint_by_key[key]
                model.add_hint(starts[key], sop.start)
                model.add_hint(ends[key], sop.end)
                if key in arm_starts:
                    eff_t = instance.effective_transfer_time(job.ops[idx - 1].instrument, op)
                    model.add_hint(arm_starts[key], sop.start - eff_t)

        all_jobs_hinted = hint_valid and len(hint_job_ids) == sum(
            1 for j in instance.jobs if j.ops
        )
        if all_jobs_hinted and 0 <= hint_ms <= horizon:
            model.add_hint(makespan, hint_ms)
            if tard_vars:
                tard_idx = 0
                for job in instance.jobs:
                    if job.ops and job.deadline is not None:
                        last_sop = hint_by_key[(job.job_id, len(job.ops) - 1)]
                        model.add_hint(tard_vars[tard_idx], max(0, last_sop.end - job.deadline))
                        tard_idx += 1

    use_two_stage_flow = False
    flow_scale = 1
    if tie_break_flow and flow_terms:
        candidate_scale = flow_span_sum + 1
        if candidate_scale <= MAX_MONOLITHIC_FLOW_SCALE:
            flow_scale = max(1, candidate_scale)
            model.minimize(primary_obj * flow_scale + sum(flow_terms))
        else:
            use_two_stage_flow = True
            model.minimize(primary_obj)
    else:
        model.minimize(primary_obj)

    def _make_solver(budget_s: float) -> cp_model.CpSolver:
        s = cp_model.CpSolver()
        s.parameters.max_time_in_seconds = max(0.01, float(budget_s))
        s.parameters.num_workers = int(num_workers)
        if seed is not None:
            s.parameters.random_seed = int(seed)
        return s

    def _extract_makespan_bound(raw_bound: float, scale: int) -> float | None:
        if not hard_deadlines and tard_vars:
            return None
        if not math.isfinite(raw_bound):
            return None
        if scale > 1:
            unscaled = max(
                0, math.ceil((raw_bound - (scale - 1) - 1e-7) / scale)
            )
            return float(unscaled)
        return float(raw_bound)

    stage1_budget = float(time_limit) * 0.75 if use_two_stage_flow else float(time_limit)
    solver = _make_solver(stage1_budget)
    status_code = solver.solve(model)

    if status_code not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        elapsed = time.perf_counter() - t0
        status_name = _STATUS_NAMES.get(status_code, "UNKNOWN")
        unknown_bound: float | None = None
        if status_code == cp_model.UNKNOWN and solver.best_objective_bound > 0:
            cand_bound = _extract_makespan_bound(
                float(solver.best_objective_bound), flow_scale
            )
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

    best_bound = _extract_makespan_bound(float(solver.best_objective_bound), flow_scale)
    active_solver = solver
    final_status = _STATUS_NAMES.get(status_code, "FEASIBLE")

    if use_two_stage_flow:
        stage1_primary_val = int(round(solver.objective_value))
        stage1_optimal = status_code == cp_model.OPTIMAL
        rem_budget = float(time_limit) - (time.perf_counter() - t0)
        if rem_budget > 0.05:
            model.clear_hints()
            for key, s_var in starts.items():
                model.add_hint(s_var, int(solver.value(s_var)))
            for key, e_var in ends.items():
                model.add_hint(e_var, int(solver.value(e_var)))
            for key, a_var in arm_starts.items():
                model.add_hint(a_var, int(solver.value(a_var)))
            model.add_hint(makespan, int(solver.value(makespan)))
            for t_var in tard_vars:
                model.add_hint(t_var, int(solver.value(t_var)))

            model.add(primary_obj <= stage1_primary_val)
            model.minimize(sum(flow_terms))

            solver2 = _make_solver(rem_budget)
            status2 = solver2.solve(model)
            if status2 in (cp_model.OPTIMAL, cp_model.FEASIBLE):
                active_solver = solver2
                final_status = (
                    "OPTIMAL"
                    if (stage1_optimal and status2 == cp_model.OPTIMAL)
                    else "FEASIBLE"
                )
            else:
                final_status = "FEASIBLE"
        else:
            final_status = "FEASIBLE"

    scheduled: list[ScheduledOp] = []
    for job in instance.jobs:
        for idx, op in enumerate(job.ops):
            key = (job.job_id, idx)
            s_val = int(active_solver.value(starts[key]))
            e_val = int(active_solver.value(ends[key]))
            u_val: int | None = None
            if key in unit_pres:
                p_list = unit_pres[key]
                for u_idx, p_var in enumerate(p_list):
                    if active_solver.boolean_value(p_var):
                        u_val = u_idx
                        break
            scheduled.append(
                ScheduledOp(
                    job_id=job.job_id,
                    op_index=idx,
                    name=op.name,
                    instrument=op.instrument,
                    start=s_val,
                    end=e_val,
                    unit_index=u_val,
                )
            )

    elapsed = time.perf_counter() - t0
    return Schedule(
        instance_name=instance.name,
        solver="cpsat",
        ops=tuple(scheduled),
        status=final_status,
        solve_time_s=elapsed,
        best_bound=best_bound,
    )
