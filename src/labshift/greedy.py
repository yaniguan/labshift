"""Greedy list-scheduling baseline solver."""

from __future__ import annotations

import time
from collections import defaultdict

from labshift.models import Instance, Schedule, ScheduledOp


def _count_covering(intervals: list[tuple[int, int]], point: int) -> int:
    """Count intervals [start, end) covering a timestamp."""
    return sum(1 for start, end in intervals if start <= point < end)


def _earliest_slot(
    intervals: list[tuple[int, int]],
    capacity: int,
    earliest_start: int,
    duration: int,
) -> int:
    """Find the earliest start >= earliest_start with capacity available over [t, t + duration)."""
    if duration == 0 or capacity <= 0:
        return earliest_start

    candidates = {earliest_start}
    for _, end in intervals:
        if end > earliest_start:
            candidates.add(end)

    for t in sorted(candidates):
        t_end = t + duration
        if _count_covering(intervals, t) >= capacity:
            continue
        conflict = False
        for start, _ in intervals:
            if t < start < t_end and _count_covering(intervals, start) >= capacity:
                conflict = True
                break
        if not conflict:
            return t

    return earliest_start


def solve_greedy(instance: Instance) -> Schedule:
    """Schedule operations via earliest-start list scheduling.

    Dispatches ready operations in priority order of (ready_time, deadline, duration, job_id).
    Respects release times, precedence, min_wait, and instrument capacities; may violate
    max_wait windows or deadlines on constrained instances.
    """
    t0 = time.perf_counter()
    next_op_idx: dict[str, int] = {job.job_id: 0 for job in instance.jobs}
    ready_time: dict[str, int] = {job.job_id: job.release for job in instance.jobs}
    job_map = {job.job_id: job for job in instance.jobs}
    inst_intervals: dict[str, list[tuple[int, int]]] = defaultdict(list)
    scheduled: list[ScheduledOp] = []

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

        cap = instance.capacities.get(op.instrument, 0)
        start = _earliest_slot(inst_intervals[op.instrument], cap, ready_time[chosen_id], op.duration)
        end = start + op.duration

        inst_intervals[op.instrument].append((start, end))
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
            ready_time[chosen_id] = end + job.ops[next_idx].min_wait

    elapsed = time.perf_counter() - t0
    return Schedule(
        instance_name=instance.name,
        solver="greedy",
        ops=tuple(scheduled),
        status="OK",
        solve_time_s=elapsed,
        best_bound=None,
    )
