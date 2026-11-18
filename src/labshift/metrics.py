"""Schedule quality metrics computation."""

from __future__ import annotations

from dataclasses import dataclass

from labshift.checker import CheckResult, check_schedule
from labshift.models import Instance, Schedule


@dataclass(frozen=True)
class Metrics:
    """Evaluation metrics for a schedule on an instance."""

    feasible: bool
    makespan: int
    total_tardiness: int
    mean_flow_time: float
    window_violations: int
    deadline_violations: int
    total_violations: int
    solve_time_s: float
    best_bound: float | None = None
    gap_pct: float | None = None


def compute_metrics(
    instance: Instance,
    schedule: Schedule,
    check: CheckResult | None = None,
) -> Metrics:
    """Compute scheduling metrics and feasibility violation counts."""
    result = check if check is not None else check_schedule(instance, schedule)
    counts = result.counts_by_kind()
    by_key = schedule.by_key()

    makespan = max((op.end for op in schedule.ops), default=0)
    total_tardiness = 0
    flow_times: list[int] = []

    for job in instance.jobs:
        if not job.ops:
            continue
        last_op = by_key.get((job.job_id, len(job.ops) - 1))
        if last_op is None:
            continue
        completion = last_op.end
        flow_times.append(completion - job.release)
        if job.deadline is not None and completion > job.deadline:
            total_tardiness += completion - job.deadline

    mean_flow_time = sum(flow_times) / len(flow_times) if flow_times else 0.0
    window_violations = counts.get("min_wait", 0) + counts.get("max_wait", 0)
    deadline_violations = counts.get("deadline", 0)

    gap_pct: float | None = None
    if schedule.best_bound is not None:
        if makespan == 0:
            gap_pct = 0.0
        else:
            gap_pct = max(0.0, 100.0 * (makespan - schedule.best_bound) / makespan)

    return Metrics(
        feasible=result.feasible,
        makespan=makespan,
        total_tardiness=total_tardiness,
        mean_flow_time=mean_flow_time,
        window_violations=window_violations,
        deadline_violations=deadline_violations,
        total_violations=len(result.violations),
        solve_time_s=schedule.solve_time_s,
        best_bound=schedule.best_bound,
        gap_pct=gap_pct,
    )
