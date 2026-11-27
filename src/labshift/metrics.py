"""Schedule quality metrics computation."""

from __future__ import annotations

from dataclasses import dataclass

from labshift.bounds import makespan_lower_bound
from labshift.checker import CheckResult, check_schedule
from labshift.models import Instance, Schedule

_UNSOLVED_STATUSES = frozenset({"INFEASIBLE", "UNKNOWN", "MODEL_INVALID"})


@dataclass(frozen=True)
class Metrics:
    """Evaluation metrics for a schedule on an instance."""

    feasible: bool
    makespan: int | None
    total_tardiness: int
    mean_flow_time: float | None
    window_violations: int
    deadline_violations: int
    total_violations: int
    solve_time_s: float
    best_bound: float | None = None
    gap_pct: float | None = None
    lower_bound: int = 0
    solver_bound: float | None = None


def compute_metrics(
    instance: Instance,
    schedule: Schedule,
    check: CheckResult | None = None,
) -> Metrics:
    """Compute scheduling metrics, valid lower bounds, and optimality gap percentages."""
    result = check if check is not None else check_schedule(instance, schedule)
    counts = result.counts_by_kind()
    by_key = schedule.by_key()

    window_violations = (
        counts.get("min_wait", 0)
        + counts.get("max_wait", 0)
        + counts.get("transfer_time", 0)
    )
    deadline_violations = counts.get("deadline", 0)
    lb = makespan_lower_bound(instance)
    solver_bound = schedule.best_bound

    is_unsolved = schedule.status in _UNSOLVED_STATUSES or (
        not schedule.ops and instance.total_operations > 0
    )
    if is_unsolved:
        unsolved_best_bound: float | None
        if schedule.status in ("INFEASIBLE", "MODEL_INVALID"):
            unsolved_best_bound = solver_bound
        elif solver_bound is not None:
            unsolved_best_bound = max(solver_bound, float(lb))
        else:
            unsolved_best_bound = float(lb) if instance.total_operations > 0 else 0.0
        return Metrics(
            feasible=result.feasible,
            makespan=None,
            total_tardiness=0,
            mean_flow_time=None,
            window_violations=window_violations,
            deadline_violations=deadline_violations,
            total_violations=len(result.violations),
            solve_time_s=schedule.solve_time_s,
            best_bound=unsolved_best_bound,
            gap_pct=None,
            lower_bound=lb,
            solver_bound=solver_bound,
        )

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

    best_bound = max(solver_bound, float(lb)) if solver_bound is not None else float(lb)

    gap_pct: float | None = None
    if makespan == 0:
        gap_pct = 0.0 if abs(best_bound) <= 1e-9 else -float("inf")
    else:
        raw_gap = 100.0 * (makespan - best_bound) / makespan
        gap_pct = 0.0 if abs(raw_gap) < 1e-9 else raw_gap

    return Metrics(
        feasible=result.feasible,
        makespan=makespan,
        total_tardiness=total_tardiness,
        mean_flow_time=mean_flow_time,
        window_violations=window_violations,
        deadline_violations=deadline_violations,
        total_violations=len(result.violations),
        solve_time_s=schedule.solve_time_s,
        best_bound=best_bound,
        gap_pct=gap_pct,
        lower_bound=lb,
        solver_bound=solver_bound,
    )
