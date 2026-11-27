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
                    kind="wrong_instrument",
                    message=(
                        f"Op {key} instrument '{sop.instrument}' != expected '{exp_inst}'."
                    ),
                    job_id=sop.job_id,
                    op_index=sop.op_index,
                    instrument=sop.instrument,
                )
            )
        if sop.start < 0:
            violations.append(
                Violation(
                    kind="negative_start",
                    message=f"Op {key} starts at negative time {sop.start}.",
                    job_id=sop.job_id,
                    op_index=sop.op_index,
                    time=sop.start,
                )
            )
        if sop.end - sop.start != exp_dur:
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
            prev_spec = job.ops[idx - 1]
            eff_transfer = instance.effective_transfer_time(prev_spec.instrument, spec)
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
            if spec.min_wait > 0 and wait < spec.min_wait:
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
            if eff_transfer > 0 and wait < eff_transfer:
                violations.append(
                    Violation(
                        kind="transfer_time",
                        message=(
                            f"Job {job.job_id} op {idx} wait {wait} "
                            f"< transfer_time {eff_transfer}."
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


def _compute_residence_ends(
    instance: Instance,
    by_key: dict[tuple[str, int], ScheduledOp],
) -> dict[tuple[str, int], int]:
    """Compute physical station release timestamp for each scheduled operation.

    Under blocking robot_arm handoffs, a plate occupies prev_op.instrument until
    robot_arm picks it up at (curr_op.start - effective_transfer_time).
    """
    res_ends: dict[tuple[str, int], int] = {
        key: sop.end for key, sop in by_key.items()
    }
    if not instance.blocking_handoff or "robot_arm" not in instance.capacities:
        return res_ends

    for job in instance.jobs:
        for idx in range(len(job.ops) - 1):
            prev_key = (job.job_id, idx)
            next_key = (job.job_id, idx + 1)
            prev_sop = by_key.get(prev_key)
            next_sop = by_key.get(next_key)
            if prev_sop is None or next_sop is None:
                continue
            next_spec = job.ops[idx + 1]
            if instance.uses_robot_arm(job.ops[idx].instrument, next_spec):
                eff_t = instance.effective_transfer_time(job.ops[idx].instrument, next_spec)
                pickup_time = next_sop.start - eff_t
                if pickup_time > prev_sop.end:
                    res_ends[prev_key] = pickup_time
    return res_ends


def _check_capacities(
    instance: Instance,
    ops: tuple[ScheduledOp, ...],
    by_key: dict[tuple[str, int], ScheduledOp],
    res_ends: dict[tuple[str, int], int],
) -> list[Violation]:
    violations: list[Violation] = []
    by_inst: dict[str, list[tuple[int, int]]] = defaultdict(list)
    by_unit: dict[tuple[str, int], list[tuple[int, int, ScheduledOp]]] = defaultdict(list)

    for sop in ops:
        key = (sop.job_id, sop.op_index)
        r_end = res_ends.get(key, sop.end)
        by_inst[sop.instrument].append((sop.start, r_end))
        if sop.unit_index is not None:
            cap = instance.capacities.get(sop.instrument)
            if cap is not None and cap > 0 and sop.unit_index >= cap:
                violations.append(
                    Violation(
                        kind="capacity",
                        message=(
                            f"Op {key} unit_index {sop.unit_index} >= capacity {cap} "
                            f"on '{sop.instrument}'."
                        ),
                        job_id=sop.job_id,
                        op_index=sop.op_index,
                        instrument=sop.instrument,
                        time=sop.start,
                    )
                )
            else:
                by_unit[(sop.instrument, sop.unit_index)].append((sop.start, r_end, sop))

    if "robot_arm" in instance.capacities:
        for job in instance.jobs:
            for idx in range(1, len(job.ops)):
                spec = job.ops[idx]
                prev_spec = job.ops[idx - 1]
                if not instance.uses_robot_arm(prev_spec.instrument, spec):
                    continue
                prev_op = by_key.get((job.job_id, idx - 1))
                curr_op = by_key.get((job.job_id, idx))
                if prev_op is None or curr_op is None:
                    continue
                eff_t = instance.effective_transfer_time(prev_spec.instrument, spec)
                by_inst["robot_arm"].append((curr_op.start - eff_t, curr_op.start))

    for (inst, unit_idx), unit_items in sorted(by_unit.items()):
        unit_items.sort(key=lambda item: (item[0], item[1], item[2].job_id, item[2].op_index))
        for i in range(1, len(unit_items)):
            prev_s, prev_e, _ = unit_items[i - 1]
            curr_s, curr_e, curr_sop = unit_items[i]
            if prev_e > prev_s and curr_e > curr_s and curr_s < prev_e:
                violations.append(
                    Violation(
                        kind="capacity",
                        message=(
                            f"Instrument '{inst}' unit {unit_idx} overlap at t={curr_s}."
                        ),
                        job_id=curr_sop.job_id,
                        op_index=curr_sop.op_index,
                        instrument=inst,
                        time=curr_s,
                    )
                )

    for inst, intervals in sorted(by_inst.items()):
        cap = instance.capacities.get(inst)
        if cap is None:
            continue
        if cap <= 0 and intervals:
            violations.append(
                Violation(
                    kind="capacity",
                    message=f"Instrument '{inst}' has capacity {cap} but is used.",
                    instrument=inst,
                )
            )
            continue

        events: list[tuple[int, int]] = []
        for start, end in intervals:
            if end > start:
                events.append((start, 1))
                events.append((end, -1))
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


def _check_setup_times(
    instance: Instance,
    by_key: dict[tuple[str, int], ScheduledOp],
    res_ends: dict[tuple[str, int], int],
) -> list[Violation]:
    if not instance.setup_times:
        return []

    violations: list[Violation] = []
    job_map = {job.job_id: job for job in instance.jobs}

    by_inst: dict[str, list[tuple[int, int, str | None, ScheduledOp]]] = defaultdict(list)
    for (job_id, op_idx), sop in by_key.items():
        job = job_map.get(job_id)
        if job is None or op_idx >= len(job.ops):
            continue
        if not instance.has_setup_times_for(sop.instrument):
            continue
        tag = instance.effective_setup_tag(job, job.ops[op_idx])
        r_end = res_ends.get((job_id, op_idx), sop.end)
        by_inst[sop.instrument].append((sop.start, r_end, tag, sop))

    for inst in sorted(by_inst):
        cap = instance.capacities.get(inst, 0)
        items = by_inst[inst]
        if cap <= 0 or len(items) <= 1:
            continue
        items.sort(key=lambda x: (x[0], x[1], x[3].job_id, x[3].op_index))

        if cap == 1:
            for i in range(1, len(items)):
                _, prev_e, prev_tag, _ = items[i - 1]
                curr_s, _, curr_tag, curr_sop = items[i]
                req = instance.required_setup_time(inst, prev_tag, curr_tag)
                if req > 0 and curr_s - prev_e < req:
                    violations.append(
                        Violation(
                            kind="setup_time",
                            message=(
                                f"Instrument '{inst}' changeover '{prev_tag}' -> '{curr_tag}' "
                                f"gap {curr_s - prev_e} < setup_time {req} before "
                                f"({curr_sop.job_id}, {curr_sop.op_index})."
                            ),
                            job_id=curr_sop.job_id,
                            op_index=curr_sop.op_index,
                            instrument=inst,
                            time=curr_s,
                        )
                    )
            continue

        if all(item[3].unit_index is not None and item[3].unit_index < cap for item in items):
            per_unit: dict[int, list[tuple[int, int, str | None, ScheduledOp]]] = defaultdict(list)
            for item in items:
                assert item[3].unit_index is not None
                per_unit[item[3].unit_index].append(item)
            for u_idx in sorted(per_unit):
                u_items = per_unit[u_idx]
                for i in range(1, len(u_items)):
                    _, prev_e, prev_tag, _ = u_items[i - 1]
                    curr_s, _, curr_tag, curr_sop = u_items[i]
                    req = instance.required_setup_time(inst, prev_tag, curr_tag)
                    if req > 0 and curr_s - prev_e < req:
                        violations.append(
                            Violation(
                                kind="setup_time",
                                message=(
                                    f"Instrument '{inst}' unit {u_idx} changeover "
                                    f"'{prev_tag}' -> '{curr_tag}' gap {curr_s - prev_e} "
                                    f"< setup_time {req} before "
                                    f"({curr_sop.job_id}, {curr_sop.op_index})."
                                ),
                                job_id=curr_sop.job_id,
                                op_index=curr_sop.op_index,
                                instrument=inst,
                                time=curr_s,
                            )
                        )
            continue

        # Multi-unit pool without full explicit unit_index annotations:
        # exact best-fit per-tag unit-chain decomposition with memoization.
        visited: set[tuple[int, tuple[tuple[int, str | None], ...]]] = set()
        deepest_failed = 0

        def _can_assign(
            pos: int,
            units: tuple[tuple[int, str | None], ...],
        ) -> bool:
            nonlocal deepest_failed
            if pos == len(items):
                return True
            canon_key = (pos, tuple(sorted(units, key=lambda u: (u[1] or "", u[0]))))
            if canon_key in visited:
                return False
            visited.add(canon_key)

            curr_s, curr_e, curr_tag, curr_sop = items[pos]
            if curr_sop.unit_index is not None:
                u_idx = curr_sop.unit_index
                if u_idx >= cap:
                    deepest_failed = max(deepest_failed, pos)
                    return False
                f_time, l_tag = units[u_idx]
                req = instance.required_setup_time(inst, l_tag, curr_tag)
                if curr_s < f_time + req:
                    deepest_failed = max(deepest_failed, pos)
                    return False
                next_units = list(units)
                next_units[u_idx] = (curr_e, curr_tag)
                return _can_assign(pos + 1, tuple(next_units))

            # Among units sharing the same last_tag that can accept curr_sop,
            # selecting the largest free_time (best-fit) strictly dominates smaller free_time.
            best_by_tag: dict[str | None, tuple[int, int]] = {}
            for u_idx, (f_time, l_tag) in enumerate(units):
                req = instance.required_setup_time(inst, l_tag, curr_tag)
                if f_time <= curr_s and f_time + req <= curr_s:
                    prev_best = best_by_tag.get(l_tag)
                    if prev_best is None or f_time > prev_best[0]:
                        best_by_tag[l_tag] = (f_time, u_idx)

            if not best_by_tag:
                deepest_failed = max(deepest_failed, pos)
                return False

            # Prefer matching tag (0 setup) or tightest fit first.
            ordered_choices = sorted(
                best_by_tag.items(),
                key=lambda kv: (0 if kv[0] == curr_tag else 1, -kv[1][0]),
            )
            for _, (_, u_idx) in ordered_choices:
                next_units = list(units)
                next_units[u_idx] = (curr_e, curr_tag)
                if _can_assign(pos + 1, tuple(next_units)):
                    return True

            deepest_failed = max(deepest_failed, pos)
            return False

        init_units: tuple[tuple[int, str | None], ...] = tuple((0, None) for _ in range(cap))
        if not _can_assign(0, init_units):
            fail_s, _, fail_tag, fail_sop = items[deepest_failed]
            violations.append(
                Violation(
                    kind="setup_time",
                    message=(
                        f"Instrument '{inst}' (capacity {cap}) admits no valid unit assignment "
                        f"satisfying sequence-dependent setup_times before "
                        f"({fail_sop.job_id}, {fail_sop.op_index}) (tag={fail_tag!r}, t={fail_s})."
                    ),
                    job_id=fail_sop.job_id,
                    op_index=fail_sop.op_index,
                    instrument=inst,
                    time=fail_s,
                )
            )

    return violations


def check_schedule(instance: Instance, schedule: Schedule) -> CheckResult:
    """Validate instance match, completeness, precedence, capacity, windows, and deadlines."""
    violations: list[Violation] = []
    if schedule.instance_name != instance.name:
        violations.append(
            Violation(
                kind="instance_mismatch",
                message=(
                    f"Schedule instance_name '{schedule.instance_name}' != "
                    f"instance name '{instance.name}'."
                ),
            )
        )
    by_key, comp_violations = _check_completeness_and_durations(instance, schedule)
    violations.extend(comp_violations)
    violations.extend(_check_job_constraints(instance, by_key))
    res_ends = _compute_residence_ends(instance, by_key)
    violations.extend(_check_capacities(instance, schedule.ops, by_key, res_ends))
    violations.extend(_check_setup_times(instance, by_key, res_ends))
    return CheckResult(feasible=(len(violations) == 0), violations=tuple(violations))
