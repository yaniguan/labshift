"""Greedy list-scheduling baseline and window-aware local-repair heuristic."""

from __future__ import annotations

import time
from collections import defaultdict

from labshift.checker import check_schedule
from labshift.metrics import compute_metrics
from labshift.models import Instance, Operation, Schedule, ScheduledOp


def _earliest_slot(
    intervals: list[tuple[int, int]],
    capacity: int,
    earliest_start: int,
    duration: int,
) -> int:
    """Find earliest start >= earliest_start with capacity available over [t, t + duration).

    Uses an O(K log K) event-profile sweep over active intervals intersecting [earliest_start, inf).
    """
    if duration <= 0:
        return earliest_start
    if capacity <= 0:
        raise ValueError(f"Cannot schedule positive-duration op on capacity {capacity} <= 0.")

    initial_active = 0
    events: list[tuple[int, int]] = []
    for start, end in intervals:
        if end <= earliest_start or end <= start:
            continue
        if start <= earliest_start:
            initial_active += 1
            events.append((end, -1))
        else:
            events.append((start, 1))
            events.append((end, -1))

    if not events:
        return earliest_start

    events.sort(key=lambda item: item[0])

    active = initial_active
    span_start: int | None = earliest_start if active < capacity else None

    idx = 0
    n_events = len(events)
    while idx < n_events:
        t = events[idx][0]
        delta = 0
        while idx < n_events and events[idx][0] == t:
            delta += events[idx][1]
            idx += 1

        if span_start is not None and t - span_start >= duration:
            return span_start

        active += delta
        if active >= capacity:
            span_start = None
        elif span_start is None:
            span_start = t

    return span_start if span_start is not None else earliest_start


def _has_infeasible_capacity(instance: Instance) -> bool:
    """Return True if any required operation or robotic arm transfer has capacity <= 0."""
    use_arm = "robot_arm" in instance.capacities
    arm_cap = instance.capacities.get("robot_arm", 0)
    for job in instance.jobs:
        for idx, op in enumerate(job.ops):
            if op.instrument not in instance.capacities or instance.capacities[op.instrument] <= 0:
                return True
            if use_arm and idx > 0 and op.transfer_time > 0 and arm_cap <= 0:
                return True
    return False


def _find_op_slot(
    inst_intervals: dict[str, list[tuple[int, int]]],
    capacities: dict[str, int],
    op: Operation,
    op_index: int,
    earliest_start: int,
    use_arm: bool,
    arm_cap: int,
) -> int:
    """Find earliest start >= earliest_start satisfying station and optional robot_arm capacity."""
    inst_cap = capacities[op.instrument]
    if not (use_arm and op_index > 0 and op.transfer_time > 0):
        return _earliest_slot(inst_intervals[op.instrument], inst_cap, earliest_start, op.duration)

    t = earliest_start
    while True:
        arm_s = _earliest_slot(
            inst_intervals["robot_arm"],
            arm_cap,
            t - op.transfer_time,
            op.transfer_time,
        )
        t_after_arm = arm_s + op.transfer_time
        t_inst = _earliest_slot(
            inst_intervals[op.instrument],
            inst_cap,
            t_after_arm,
            op.duration,
        )
        if t_inst == t_after_arm:
            return t_inst
        t = t_inst


def solve_greedy(instance: Instance) -> Schedule:
    """Schedule operations via earliest-start list scheduling.

    Dispatches ready operations in priority order of (ready_time, deadline, duration, job_id).
    Respects release times, precedence, min_wait, transfer_time, and instrument capacities;
    may violate max_wait windows or deadlines on constrained instances.
    """
    t0 = time.perf_counter()
    if _has_infeasible_capacity(instance):
        return Schedule(
            instance_name=instance.name,
            solver="greedy",
            ops=(),
            status="INFEASIBLE",
            solve_time_s=time.perf_counter() - t0,
            best_bound=None,
        )

    next_op_idx: dict[str, int] = {job.job_id: 0 for job in instance.jobs}
    ready_time: dict[str, int] = {job.job_id: job.release for job in instance.jobs}
    job_map = {job.job_id: job for job in instance.jobs}
    inst_intervals: dict[str, list[tuple[int, int]]] = defaultdict(list)
    scheduled: list[ScheduledOp] = []
    use_arm = "robot_arm" in instance.capacities
    arm_cap = instance.capacities.get("robot_arm", 0)

    while True:
        ready_jobs = [
            job_id
            for job_id, idx in next_op_idx.items()
            if idx < len(job_map[job_id].ops)
        ]
        if not ready_jobs:
            break

        def priority_key(job_id: str) -> tuple[int, float, int, str]:
            job = job_map[job_id]
            op = job.ops[next_op_idx[job_id]]
            deadline = float(job.deadline) if job.deadline is not None else float("inf")
            return (ready_time[job_id], deadline, op.duration, job_id)

        chosen_id = min(ready_jobs, key=priority_key)
        job = job_map[chosen_id]
        idx = next_op_idx[chosen_id]
        op = job.ops[idx]

        start = _find_op_slot(
            inst_intervals,
            instance.capacities,
            op,
            idx,
            ready_time[chosen_id],
            use_arm,
            arm_cap,
        )
        end = start + op.duration

        inst_intervals[op.instrument].append((start, end))
        if use_arm and idx > 0 and op.transfer_time > 0:
            inst_intervals["robot_arm"].append((start - op.transfer_time, start))
        scheduled.append(
            ScheduledOp(
                job_id=chosen_id,
                op_index=idx,
                name=op.name,
                instrument=op.instrument,
                start=start,
                end=end,
            )
        )

        next_idx = idx + 1
        next_op_idx[chosen_id] = next_idx
        if next_idx < len(job.ops):
            next_op = job.ops[next_idx]
            ready_time[chosen_id] = end + max(next_op.min_wait, next_op.transfer_time)

    elapsed = time.perf_counter() - t0
    return Schedule(
        instance_name=instance.name,
        solver="greedy",
        ops=tuple(scheduled),
        status="OK",
        solve_time_s=elapsed,
        best_bound=None,
    )


def _coupled_segment_end(ops: tuple[Operation, ...], start_idx: int) -> int:
    """Return inclusive end index of the contiguous max_wait-coupled segment from start_idx."""
    end_idx = start_idx
    while end_idx + 1 < len(ops) and ops[end_idx + 1].max_wait is not None:
        end_idx += 1
    return end_idx


def _run_coupled_pass(instance: Instance, mode: str) -> list[ScheduledOp]:
    """Schedule max_wait-coupled chain segments atomically via right-shift propagation."""
    next_op_idx: dict[str, int] = {job.job_id: 0 for job in instance.jobs}
    ready_time: dict[str, int] = {job.job_id: job.release for job in instance.jobs}
    job_map = {job.job_id: job for job in instance.jobs}
    rem_work: dict[str, list[int]] = {}
    for job in instance.jobs:
        m = len(job.ops)
        rem = [0] * (m + 1)
        for k in range(m - 1, -1, -1):
            wait_after = (
                max(job.ops[k + 1].min_wait, job.ops[k + 1].transfer_time)
                if k + 1 < m
                else 0
            )
            rem[k] = job.ops[k].duration + wait_after + rem[k + 1]
        rem_work[job.job_id] = rem

    inst_intervals: dict[str, list[tuple[int, int]]] = defaultdict(list)
    scheduled: list[ScheduledOp] = []
    use_arm = "robot_arm" in instance.capacities
    arm_cap = instance.capacities.get("robot_arm", 0)

    while True:
        ready_jobs = [
            job_id
            for job_id, idx in next_op_idx.items()
            if idx < len(job_map[job_id].ops)
        ]
        if not ready_jobs:
            break

        def seg_priority(job_id: str) -> tuple[float, float, float, int, str]:
            job = job_map[job_id]
            a = next_op_idx[job_id]
            b = _coupled_segment_end(job.ops, a)
            seg_dur = sum(job.ops[r].duration for r in range(a, b + 1))
            r_t = float(ready_time[job_id])
            dl = float(job.deadline) if job.deadline is not None else float("inf")
            slack = dl - (r_t + rem_work[job_id][a])
            coupled_flag = 0.0 if b > a else 1.0
            if mode == "tight_first":
                return (r_t, coupled_flag, slack, seg_dur, job_id)
            if mode == "shift_coupled":
                return (r_t, 1.0 - coupled_flag, dl, seg_dur, job_id)
            if mode == "slack":
                return (slack, r_t, coupled_flag, seg_dur, job_id)
            if mode == "edf":
                return (dl, r_t, coupled_flag, seg_dur, job_id)
            return (r_t, dl, coupled_flag, seg_dur, job_id)

        chosen_id = min(ready_jobs, key=seg_priority)
        job = job_map[chosen_id]
        a = next_op_idx[chosen_id]
        b = _coupled_segment_end(job.ops, a)

        cand_a = ready_time[chosen_id]
        while True:
            op_a = job.ops[a]
            s_a = _find_op_slot(
                inst_intervals, instance.capacities, op_a, a, cand_a, use_arm, arm_cap
            )
            e_a = s_a + op_a.duration
            placed: list[tuple[int, int, int]] = [(a, s_a, e_a)]
            temp_keys: list[str] = [op_a.instrument]
            inst_intervals[op_a.instrument].append((s_a, e_a))
            if use_arm and a > 0 and op_a.transfer_time > 0:
                inst_intervals["robot_arm"].append((s_a - op_a.transfer_time, s_a))
                temp_keys.append("robot_arm")

            shift_to: int | None = None
            for k in range(a + 1, b + 1):
                prev_end = placed[-1][2]
                op_k = job.ops[k]
                eff_min = max(op_k.min_wait, op_k.transfer_time)
                lo_k = prev_end + eff_min
                assert op_k.max_wait is not None
                hi_k = prev_end + op_k.max_wait
                s_k = _find_op_slot(
                    inst_intervals, instance.capacities, op_k, k, lo_k, use_arm, arm_cap
                )
                if s_k > hi_k:
                    max_span = sum(job.ops[r].duration for r in range(a, k)) + sum(
                        job.ops[r].max_wait or 0 for r in range(a + 1, k)
                    )
                    needed_a = s_k - op_k.max_wait - max_span
                    shift_to = max(s_a + 1, needed_a)
                    break
                e_k = s_k + op_k.duration
                placed.append((k, s_k, e_k))
                inst_intervals[op_k.instrument].append((s_k, e_k))
                temp_keys.append(op_k.instrument)
                if use_arm and op_k.transfer_time > 0:
                    inst_intervals["robot_arm"].append((s_k - op_k.transfer_time, s_k))
                    temp_keys.append("robot_arm")

            if shift_to is None:
                break
            for key in reversed(temp_keys):
                inst_intervals[key].pop()
            cand_a = shift_to

        for k, s_k, e_k in placed:
            scheduled.append(
                ScheduledOp(
                    job_id=chosen_id,
                    op_index=k,
                    name=job.ops[k].name,
                    instrument=job.ops[k].instrument,
                    start=s_k,
                    end=e_k,
                )
            )

        next_idx = b + 1
        next_op_idx[chosen_id] = next_idx
        if next_idx < len(job.ops):
            next_op = job.ops[next_idx]
            ready_time[chosen_id] = placed[-1][2] + max(next_op.min_wait, next_op.transfer_time)

    return scheduled


def solve_greedy_repair(instance: Instance) -> Schedule:
    """Pure-stdlib window-aware scheduling heuristic with coupled right-shift repair.

    Schedules contiguous max_wait-coupled operation segments atomically by right-shifting
    upstream operations whenever a downstream bottleneck would force wait > max_wait,
    selecting the best schedule across deterministic dispatch priority rules.
    """
    t0 = time.perf_counter()
    if _has_infeasible_capacity(instance):
        return Schedule(
            instance_name=instance.name,
            solver="greedy_repair",
            ops=(),
            status="INFEASIBLE",
            solve_time_s=time.perf_counter() - t0,
            best_bound=None,
        )

    candidates: list[Schedule] = []
    base = solve_greedy(instance)
    candidates.append(
        Schedule(
            instance_name=instance.name,
            solver="greedy_repair",
            ops=base.ops,
            status=base.status,
            solve_time_s=0.0,
            best_bound=None,
        )
    )

    for mode in ("earliest", "tight_first", "shift_coupled", "slack", "edf"):
        ops = _run_coupled_pass(instance, mode)
        candidates.append(
            Schedule(
                instance_name=instance.name,
                solver="greedy_repair",
                ops=tuple(ops),
                status="OK",
                solve_time_s=0.0,
                best_bound=None,
            )
        )

    def score_schedule(sched: Schedule) -> tuple[int, int, int, int, int, float]:
        chk = check_schedule(instance, sched)
        m = compute_metrics(instance, sched, check=chk)
        return (
            0 if chk.feasible else 1,
            m.total_violations,
            m.window_violations,
            m.total_tardiness,
            m.makespan if m.makespan is not None else 10**9,
            m.mean_flow_time if m.mean_flow_time is not None else float("inf"),
        )

    best = min(candidates, key=score_schedule)
    elapsed = time.perf_counter() - t0
    return Schedule(
        instance_name=instance.name,
        solver="greedy_repair",
        ops=best.ops,
        status=best.status,
        solve_time_s=elapsed,
        best_bound=None,
    )
