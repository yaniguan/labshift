"""Feasibility checker for labshift schedules."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass

from labshift.models import Instance, Schedule, ScheduledOp


@dataclass(frozen=True)
class Violation:
    """Single constraint violation found in a schedule."""

    kind: str
    message: str
    job_id: str | None = None
    op_index: int | None = None
    instrument: str | None = None
    time: int | None = None


@dataclass(frozen=True)
class CheckResult:
    """Summary of schedule feasibility verification."""

    feasible: bool
    violations: tuple[Violation, ...]

    def counts_by_kind(self) -> dict[str, int]:
        """Count violations grouped by violation kind."""
        return dict(Counter(v.kind for v in self.violations))


def _check_completeness_and_durations(
    instance: Instance,
    schedule: Schedule,
) -> tuple[dict[tuple[str, int], ScheduledOp], list[Violation]]:
    violations: list[Violation] = []
    expected: dict[tuple[str, int], tuple[str, int]] = {}
    for job in instance.jobs:
        for idx, op in enumerate(job.ops):
            expected[(job.job_id, idx)] = (op.instrument, op.duration)

    by_key: dict[tuple[str, int], ScheduledOp] = {}
    for sop in schedule.ops:
        key = (sop.job_id, sop.op_index)
        if key not in expected:
            violations.append(
                Violation(
                    kind="extra_op",
                    message=f"Unknown operation ({sop.job_id}, {sop.op_index}).",
                    job_id=sop.job_id,
                    op_index=sop.op_index,
                )
            )
            continue
        if key in by_key:
            violations.append(
                Violation(
                    kind="extra_op",
                    message=f"Duplicate operation ({sop.job_id}, {sop.op_index}).",
                    job_id=sop.job_id,
                    op_index=sop.op_index,
                )
            )
            continue
        by_key[key] = sop

        exp_inst, exp_dur = expected[key]
        if sop.instrument not in instance.capacities:
            violations.append(
                Violation(
                    kind="unknown_instrument",
                    message=f"Unknown instrument '{sop.instrument}' for {key}.",
                    job_id=sop.job_id,
                    op_index=sop.op_index,
                    instrument=sop.instrument,
                )
            )
        if sop.instrument != exp_inst:
            violations.append(
                Violation(
                    kind="duration",
                    message=(
                        f"Op {key} instrument '{sop.instrument}' != expected '{exp_inst}'."
                    ),
                    job_id=sop.job_id,
                    op_index=sop.op_index,
                    instrument=sop.instrument,
                )
            )
        if sop.start < 0 or sop.end - sop.start != exp_dur:
            violations.append(
                Violation(
                    kind="duration",
                    message=(
                        f"Op {key} interval [{sop.start}, {sop.end}) invalid for duration {exp_dur}."
                    ),
                    job_id=sop.job_id,
                    op_index=sop.op_index,
                    time=sop.start,
                )
            )

    for key in expected:
        if key not in by_key:
            violations.append(
                Violation(
                    kind="missing_op",
                    message=f"Missing operation ({key[0]}, {key[1]}).",
                    job_id=key[0],
                    op_index=key[1],
                )
            )

    return by_key, violations


def _check_job_constraints(
    instance: Instance,
    by_key: dict[tuple[str, int], ScheduledOp],
) -> list[Violation]:
    violations: list[Violation] = []
    for job in instance.jobs:
        if not job.ops:
            continue
        first = by_key.get((job.job_id, 0))
        if first is not None and first.start < job.release:
            violations.append(
                Violation(
                    kind="release",
                    message=(
                        f"Job {job.job_id} starts at {first.start} before release {job.release}."
                    ),
                    job_id=job.job_id,
                    op_index=0,
                    time=first.start,
                )
            )

        for idx in range(1, len(job.ops)):
            prev_op = by_key.get((job.job_id, idx - 1))
            curr_op = by_key.get((job.job_id, idx))
            if prev_op is None or curr_op is None:
                continue
            spec = job.ops[idx]
            wait = curr_op.start - prev_op.end
            if wait < 0:
                violations.append(
                    Violation(
                        kind="precedence",
                        message=(
                            f"Job {job.job_id} op {idx} starts at {curr_op.start} "
                            f"before op {idx - 1} ends at {prev_op.end}."
                        ),
                        job_id=job.job_id,
                        op_index=idx,
                        time=curr_op.start,
                    )
                )
                continue
            if wait < spec.min_wait:
                violations.append(
                    Violation(
                        kind="min_wait",
                        message=(
                            f"Job {job.job_id} op {idx} wait {wait} < min_wait {spec.min_wait}."
                        ),
                        job_id=job.job_id,
                        op_index=idx,
                        time=curr_op.start,
                    )
                )
            if spec.max_wait is not None and wait > spec.max_wait:
                violations.append(
                    Violation(
                        kind="max_wait",
                        message=(
                            f"Job {job.job_id} op {idx} wait {wait} > max_wait {spec.max_wait}."
                        ),
                        job_id=job.job_id,
                        op_index=idx,
                        time=curr_op.start,
                    )
                )

        if job.deadline is not None:
            last_idx = len(job.ops) - 1
            last_op = by_key.get((job.job_id, last_idx))
            if last_op is not None and last_op.end > job.deadline:
                violations.append(
                    Violation(
                        kind="deadline",
                        message=(
                            f"Job {job.job_id} ends at {last_op.end} after deadline {job.deadline}."
                        ),
                        job_id=job.job_id,
                        op_index=last_idx,
                        time=last_op.end,
                    )
                )

    return violations


def _check_capacities(
    instance: Instance,
    ops: tuple[ScheduledOp, ...],
) -> list[Violation]:
    violations: list[Violation] = []
    by_inst: dict[str, list[ScheduledOp]] = defaultdict(list)
    for sop in ops:
        by_inst[sop.instrument].append(sop)

    for inst, inst_ops in sorted(by_inst.items()):
        cap = instance.capacities.get(inst)
        if cap is None:
            continue
        if cap <= 0 and any(op.end > op.start for op in inst_ops):
            violations.append(
                Violation(
                    kind="capacity",
                    message=f"Instrument '{inst}' has capacity {cap} but is used.",
                    instrument=inst,
                )
            )
            continue

        events: list[tuple[int, int]] = []
        for op in inst_ops:
            if op.end > op.start:
                events.append((op.start, 1))
                events.append((op.end, -1))
        # Process departures (-1) before arrivals (+1) at identical timestamps.
        events.sort(key=lambda item: (item[0], item[1]))

        active = 0
        overloaded = False
        idx = 0
        while idx < len(events):
            t = events[idx][0]
            while idx < len(events) and events[idx][0] == t:
                active += events[idx][1]
                idx += 1
            if active > cap and not overloaded:
                violations.append(
                    Violation(
                        kind="capacity",
                        message=(
                            f"Instrument '{inst}' concurrency {active} > capacity {cap} at t={t}."
                        ),
                        instrument=inst,
                        time=t,
                    )
                )
                overloaded = True
            elif active <= cap:
                overloaded = False

    return violations


def check_schedule(instance: Instance, schedule: Schedule) -> CheckResult:
    """Validate precedence, capacity, time windows, release times, and deadlines."""
    by_key, violations = _check_completeness_and_durations(instance, schedule)
    violations.extend(_check_job_constraints(instance, by_key))
    violations.extend(_check_capacities(instance, schedule.ops))
    return CheckResult(feasible=(len(violations) == 0), violations=tuple(violations))
