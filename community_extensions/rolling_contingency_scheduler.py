"""Idea #07: Rolling-Horizon Instrument Breakdown Rescheduler.

Inspired by mid-run instrument fault recovery challenges in autonomous wet-labs
(the-matter-lab/La-Agente-Optima and Uni-Lab-OS). When one unit in a multi-unit
instrument pool breaks down at t_fault, live biological plates already mid-incubation
cannot simply wait in a naive FIFO queue without violating strict [min_wait, max_wait]
windows. This rescheduler:
  1. Prioritizes in-flight plates by earliest biological window expiration and reroutes
     their downstream steps onto surviving pool capacity.
  2. Applies pre-incubation admission holds to unstarted plates, shifting their coupled
     multi-step chain forward together so zero live plates violate [min_wait, max_wait].
  3. Verifies every repaired schedule with exact checks mirroring LabShiftLean.
"""

from __future__ import annotations

import random
import statistics
import time
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PlateStepSpec:
    """Single step template within a coupled multi-step biological plate workflow."""

    step_idx: int
    pool_id: int
    dur_min: int
    min_wait_after_prev: int = 0
    max_wait_after_prev: int = 0


@dataclass(frozen=True)
class PlateJobSpec:
    """Multi-step biological plate job with coupled inter-step [min_wait, max_wait] windows."""

    plate_id: int
    release_min: int
    deadline_min: int
    steps: tuple[PlateStepSpec, ...]


@dataclass(frozen=True)
class ScheduledPlateOp:
    """Concrete scheduled operation mirroring LabShiftLean.ScheduledOp."""

    op_id: int
    plate_id: int
    step_idx: int
    pool_id: int
    machine_idx: int
    pool_cap: int
    start_min: int
    dur_min: int
    release_min: int
    deadline_min: int

    @property
    def end_min(self) -> int:
        return self.start_min + self.dur_min


@dataclass(frozen=True)
class WaitWindowLink:
    """Precedence and max-wait link mirroring LabShiftLean.WaitLink."""

    plate_id: int
    pred_step_idx: int
    succ_step_idx: int
    pred_end_min: int
    succ_start_min: int
    min_wait_min: int
    max_wait_min: int


@dataclass(frozen=True)
class FaultSpec:
    """Mid-run hardware breakdown event mirroring LabShiftLean.FaultEvent."""

    fault_pool_id: int
    fault_machine_idx: int
    fault_min: int
    repair_min: int


@dataclass(frozen=True)
class ContingencyRepairResult:
    """Outcome of rescheduling after a mid-run instrument breakdown."""

    ops: tuple[ScheduledPlateOp, ...]
    links: tuple[WaitWindowLink, ...]
    violated_plate_ids: tuple[int, ...]
    pre_incubation_holds: int
    rerouted_ops: int
    verification_error: str | None
    latency_ms: float


def build_wait_links(
    jobs: list[PlateJobSpec],
    ops: list[ScheduledPlateOp],
) -> list[WaitWindowLink]:
    """Construct exact inter-step WaitWindowLink records for a list of scheduled operations."""
    by_plate_step: dict[tuple[int, int], ScheduledPlateOp] = {
        (op.plate_id, op.step_idx): op for op in ops
    }
    links: list[WaitWindowLink] = []
    for job in jobs:
        for idx in range(1, len(job.steps)):
            pred_op = by_plate_step[(job.plate_id, idx - 1)]
            succ_op = by_plate_step[(job.plate_id, idx)]
            step_spec = job.steps[idx]
            links.append(
                WaitWindowLink(
                    plate_id=job.plate_id,
                    pred_step_idx=idx - 1,
                    succ_step_idx=idx,
                    pred_end_min=pred_op.end_min,
                    succ_start_min=succ_op.start_min,
                    min_wait_min=step_spec.min_wait_after_prev,
                    max_wait_min=step_spec.max_wait_after_prev,
                )
            )
    return links


def verify_schedule(
    ops: list[ScheduledPlateOp],
    links: list[WaitWindowLink],
) -> str | None:
    """Exact Python mirror of LabShiftLean.checkSchedule. Returns None iff valid."""
    seen_op_ids: set[int] = set()
    for op in ops:
        if op.op_id in seen_op_ids:
            return "duplicate_op_id"
        seen_op_ids.add(op.op_id)

    for op in ops:
        if not (
            op.dur_min > 0
            and 0 <= op.machine_idx < op.pool_cap
            and op.release_min <= op.start_min
            and op.end_min <= op.deadline_min
        ):
            return "op_release_deadline_capacity"

    for link in links:
        earliest = link.pred_end_min + link.min_wait_min
        latest = link.pred_end_min + link.max_wait_min
        if not (earliest <= link.succ_start_min <= latest):
            return "inter_step_wait_window"

    for i, a in enumerate(ops):
        for j, b in enumerate(ops):
            if i == j:
                continue
            if a.pool_id == b.pool_id and a.machine_idx == b.machine_idx:
                if not (a.end_min <= b.start_min or b.end_min <= a.start_min):
                    return "machine_mutual_exclusion"
    return None


def verify_contingency_schedule(
    ops: list[ScheduledPlateOp],
    links: list[WaitWindowLink],
    fault: FaultSpec,
) -> str | None:
    """Exact Python mirror of LabShiftLean.checkContingencySchedule."""
    base_err = verify_schedule(ops, links)
    if base_err is not None:
        return base_err
    for op in ops:
        if op.pool_id == fault.fault_pool_id and op.machine_idx == fault.fault_machine_idx:
            if not (op.end_min <= fault.fault_min or fault.repair_min <= op.start_min):
                return "faulted_machine_quarantine"
    return None


def _intervals_overlap(s1: int, e1: int, s2: int, e2: int) -> bool:
    return s1 < e2 and s2 < e1


def _is_machine_free(
    busy: dict[tuple[int, int], list[tuple[int, int]]],
    pool_id: int,
    machine_idx: int,
    start_min: int,
    dur_min: int,
    fault: FaultSpec | None = None,
) -> bool:
    end_min = start_min + dur_min
    if (
        fault is not None
        and pool_id == fault.fault_pool_id
        and machine_idx == fault.fault_machine_idx
        and _intervals_overlap(start_min, end_min, fault.fault_min, fault.repair_min)
    ):
        return False
    for b_start, b_end in busy.get((pool_id, machine_idx), []):
        if _intervals_overlap(start_min, end_min, b_start, b_end):
            return False
    return True


def _find_coupled_chain_placement(
    job: PlateJobSpec,
    pool_caps: dict[int, int],
    busy: dict[tuple[int, int], list[tuple[int, int]]],
    earliest_step0: int,
    fault: FaultSpec | None = None,
) -> list[tuple[int, int]] | None:
    """Find collision-free (machine_idx, start_min) for all steps of a plate job."""
    max_step0 = job.deadline_min - sum(st.dur_min for st in job.steps)

    def _backtrack(step_pos: int, prev_end: int) -> list[tuple[int, int]] | None:
        if step_pos == len(job.steps):
            return []
        st = job.steps[step_pos]
        cap = pool_caps[st.pool_id]
        if step_pos == 0:
            lo = max(job.release_min, earliest_step0)
            hi = max_step0
        else:
            lo = prev_end + st.min_wait_after_prev
            hi = min(prev_end + st.max_wait_after_prev, job.deadline_min - st.dur_min)
        for cand_start in range(lo, hi + 1):
            for m_idx in range(cap):
                if _is_machine_free(busy, st.pool_id, m_idx, cand_start, st.dur_min, fault):
                    rest = _backtrack(step_pos + 1, cand_start + st.dur_min)
                    if rest is not None:
                        return [(m_idx, cand_start)] + rest
        return None

    return _backtrack(0, 0)


def build_nominal_schedule(
    jobs: list[PlateJobSpec],
    pool_caps: dict[int, int],
) -> tuple[list[ScheduledPlateOp], list[WaitWindowLink]]:
    """Construct a conflict-free nominal schedule prior to any hardware fault."""
    busy: dict[tuple[int, int], list[tuple[int, int]]] = {}
    ops: list[ScheduledPlateOp] = []
    next_op_id = 1
    for job in sorted(jobs, key=lambda j: (j.release_min, j.plate_id)):
        # Balance initial machine assignments across pool units via plate_id modulo cap
        max_step0 = job.deadline_min - sum(st.dur_min for st in job.steps)
        placed: list[tuple[int, int]] | None = None
        for t0 in range(job.release_min, max_step0 + 1):
            attempt: list[tuple[int, int]] = []
            prev_end = 0
            ok = True
            for idx, st in enumerate(job.steps):
                cap = pool_caps[st.pool_id]
                lo = t0 if idx == 0 else prev_end + st.min_wait_after_prev
                hi = t0 if idx == 0 else prev_end + st.max_wait_after_prev
                chosen: tuple[int, int] | None = None
                for cand_t in range(lo, hi + 1):
                    if cand_t + st.dur_min > job.deadline_min:
                        continue
                    for offset in range(cap):
                        m_idx = (job.plate_id + offset) % cap
                        if _is_machine_free(busy, st.pool_id, m_idx, cand_t, st.dur_min):
                            chosen = (m_idx, cand_t)
                            break
                    if chosen is not None:
                        break
                if chosen is None:
                    ok = False
                    break
                attempt.append(chosen)
                prev_end = chosen[1] + st.dur_min
            if ok:
                placed = attempt
                break
        if placed is None:
            raise ValueError(f"Unable to build nominal schedule for plate {job.plate_id}")
        for st, (m_idx, s_min) in zip(job.steps, placed):
            busy.setdefault((st.pool_id, m_idx), []).append((s_min, s_min + st.dur_min))
            ops.append(
                ScheduledPlateOp(
                    op_id=next_op_id,
                    plate_id=job.plate_id,
                    step_idx=st.step_idx,
                    pool_id=st.pool_id,
                    machine_idx=m_idx,
                    pool_cap=pool_caps[st.pool_id],
                    start_min=s_min,
                    dur_min=st.dur_min,
                    release_min=job.release_min,
                    deadline_min=job.deadline_min,
                )
            )
            next_op_id += 1
    links = build_wait_links(jobs, ops)
    return ops, links


def _count_violated_plates(
    jobs: list[PlateJobSpec],
    ops: list[ScheduledPlateOp],
    links: list[WaitWindowLink],
    fault: FaultSpec,
) -> tuple[int, ...]:
    violated: set[int] = set()
    job_deadlines = {j.plate_id: j.deadline_min for j in jobs}
    for link in links:
        if not (
            link.pred_end_min + link.min_wait_min
            <= link.succ_start_min
            <= link.pred_end_min + link.max_wait_min
        ):
            violated.add(link.plate_id)
    for op in ops:
        if op.end_min > job_deadlines[op.plate_id]:
            violated.add(op.plate_id)
        if op.pool_id == fault.fault_pool_id and op.machine_idx == fault.fault_machine_idx:
            if _intervals_overlap(op.start_min, op.end_min, fault.fault_min, fault.repair_min):
                violated.add(op.plate_id)
    return tuple(sorted(violated))


def naive_fifo_delay_reschedule(
    jobs: list[PlateJobSpec],
    nominal_ops: list[ScheduledPlateOp],
    pool_caps: dict[int, int],
    fault: FaultSpec,
) -> ContingencyRepairResult:
    """Baseline reactive FIFO policy: starts all plates on nominal release without pre-incubation
    holds, and queues faulted downstream steps FIFO onto surviving units as they arrive."""
    t0 = time.perf_counter()
    job_map = {j.plate_id: j for j in jobs}
    busy: dict[tuple[int, int], list[tuple[int, int]]] = {}

    frozen: dict[tuple[int, int], ScheduledPlateOp] = {}
    for op in nominal_ops:
        hits_fault = (
            op.pool_id == fault.fault_pool_id
            and op.machine_idx == fault.fault_machine_idx
            and _intervals_overlap(op.start_min, op.end_min, fault.fault_min, fault.repair_min)
        )
        if op.start_min < fault.fault_min and not hits_fault:
            frozen[(op.plate_id, op.step_idx)] = op
            busy.setdefault((op.pool_id, op.machine_idx), []).append((op.start_min, op.end_min))

    pending = [op for op in nominal_ops if (op.plate_id, op.step_idx) not in frozen]
    pending.sort(key=lambda o: (o.start_min, o.plate_id, o.step_idx))

    placed_map = dict(frozen)
    rerouted_ops = 0
    for op in pending:
        job = job_map[op.plate_id]
        st_spec = job.steps[op.step_idx]
        earliest = max(op.start_min, fault.fault_min)
        if op.step_idx > 0:
            pred = placed_map[(op.plate_id, op.step_idx - 1)]
            earliest = max(earliest, pred.end_min + st_spec.min_wait_after_prev)
        cap = pool_caps[op.pool_id]
        cand_t = earliest
        chosen_m = op.machine_idx
        while True:
            # Reactive FIFO tries original unit first, then any surviving unit at cand_t
            unit_order = [op.machine_idx] + [m for m in range(cap) if m != op.machine_idx]
            picked: int | None = None
            for m_idx in unit_order:
                if _is_machine_free(busy, op.pool_id, m_idx, cand_t, op.dur_min, fault):
                    picked = m_idx
                    break
            if picked is not None:
                chosen_m = picked
                break
            cand_t += 1
        if chosen_m != op.machine_idx or cand_t != op.start_min:
            rerouted_ops += 1
        updated = ScheduledPlateOp(
            op_id=op.op_id,
            plate_id=op.plate_id,
            step_idx=op.step_idx,
            pool_id=op.pool_id,
            machine_idx=chosen_m,
            pool_cap=cap,
            start_min=cand_t,
            dur_min=op.dur_min,
            release_min=op.release_min,
            deadline_min=op.deadline_min,
        )
        placed_map[(op.plate_id, op.step_idx)] = updated
        busy.setdefault((op.pool_id, chosen_m), []).append((updated.start_min, updated.end_min))

    repaired_ops = sorted(placed_map.values(), key=lambda o: o.op_id)
    links = build_wait_links(jobs, repaired_ops)
    violated = _count_violated_plates(jobs, repaired_ops, links, fault)
    err = verify_contingency_schedule(repaired_ops, links, fault)
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    return ContingencyRepairResult(
        ops=tuple(repaired_ops),
        links=tuple(links),
        violated_plate_ids=violated,
        pre_incubation_holds=0,
        rerouted_ops=rerouted_ops,
        verification_error=err,
        latency_ms=elapsed_ms,
    )


def rolling_contingency_repair(
    jobs: list[PlateJobSpec],
    nominal_ops: list[ScheduledPlateOp],
    pool_caps: dict[int, int],
    fault: FaultSpec,
) -> ContingencyRepairResult:
    """Rolling-horizon repair: reroutes live plates first and holds pre-incubation jobs."""
    t0 = time.perf_counter()
    job_map = {j.plate_id: j for j in jobs}
    nom_by_plate: dict[int, list[ScheduledPlateOp]] = {}
    for op in nominal_ops:
        nom_by_plate.setdefault(op.plate_id, []).append(op)
    for p_ops in nom_by_plate.values():
        p_ops.sort(key=lambda o: o.step_idx)

    busy: dict[tuple[int, int], list[tuple[int, int]]] = {}
    placed_map: dict[tuple[int, int], ScheduledPlateOp] = {}
    rerouted_ops = 0
    pre_inc_holds = 0

    in_flight_plates: list[int] = []
    unstarted_plates: list[int] = []

    for job in jobs:
        p_ops = nom_by_plate[job.plate_id]
        if p_ops[0].start_min < fault.fault_min:
            in_flight_plates.append(job.plate_id)
            for op in p_ops:
                hits_fault = (
                    op.pool_id == fault.fault_pool_id
                    and op.machine_idx == fault.fault_machine_idx
                    and _intervals_overlap(
                        op.start_min, op.end_min, fault.fault_min, fault.repair_min
                    )
                )
                if op.start_min < fault.fault_min and not hits_fault:
                    placed_map[(job.plate_id, op.step_idx)] = op
                    busy.setdefault((op.pool_id, op.machine_idx), []).append(
                        (op.start_min, op.end_min)
                    )
        else:
            unstarted_plates.append(job.plate_id)

    # 1. Rank live in-flight plates by deepest locked stage first (least remaining slack),
    # then by tightest maximum-feasible start time on the final downstream step.
    def _live_urgency(pid: int) -> tuple[int, int, int]:
        job = job_map[pid]
        frozen_count = sum(1 for st in job.steps if (pid, st.step_idx) in placed_map)
        first_missing = next(
            (st.step_idx for st in job.steps if (pid, st.step_idx) not in placed_map),
            len(job.steps),
        )
        if first_missing == len(job.steps):
            return (-frozen_count, 10**9, pid)
        max_cur_end = (
            placed_map[(pid, first_missing - 1)].end_min
            if first_missing > 0
            else job.release_min
        )
        for s_idx in range(first_missing, len(job.steps)):
            st = job.steps[s_idx]
            if s_idx == 0:
                max_cur_end = max_cur_end + st.dur_min
            else:
                max_cur_end = max_cur_end + st.max_wait_after_prev + st.dur_min
        return (-frozen_count, max_cur_end, pid)

    in_flight_plates.sort(key=_live_urgency)

    for pid in in_flight_plates:
        job = job_map[pid]
        p_ops = nom_by_plate[pid]
        missing_indices = [
            st.step_idx for st in job.steps if (pid, st.step_idx) not in placed_map
        ]
        if not missing_indices:
            continue

        # Backtracking search over remaining steps of this live plate across surviving capacity
        def _solve_remaining(pos: int, prev_end: int) -> list[tuple[int, int]] | None:
            if pos == len(missing_indices):
                return []
            s_idx = missing_indices[pos]
            st = job.steps[s_idx]
            cap = pool_caps[st.pool_id]
            nom_op = p_ops[s_idx]
            if s_idx == 0:
                lo = max(fault.fault_min, job.release_min)
                hi = job.deadline_min - st.dur_min
            else:
                lo = max(fault.fault_min, prev_end + st.min_wait_after_prev)
                hi = min(prev_end + st.max_wait_after_prev, job.deadline_min - st.dur_min)
            machine_order = sorted(
                range(cap),
                key=lambda m: (
                    1 if (st.pool_id == fault.fault_pool_id and m == fault.fault_machine_idx) else 0,
                    0 if m == nom_op.machine_idx else 1,
                    m,
                ),
            )
            for cand_t in range(lo, hi + 1):
                for m_idx in machine_order:
                    if _is_machine_free(busy, st.pool_id, m_idx, cand_t, st.dur_min, fault):
                        tail = _solve_remaining(pos + 1, cand_t + st.dur_min)
                        if tail is not None:
                            return [(m_idx, cand_t)] + tail
            return None

        first_missing = missing_indices[0]
        anchor_end = (
            placed_map[(pid, first_missing - 1)].end_min if first_missing > 0 else 0
        )
        rem_sol = _solve_remaining(0, anchor_end)
        if rem_sol is not None:
            for s_idx, (m_idx, cand_t) in zip(missing_indices, rem_sol):
                orig = p_ops[s_idx]
                if m_idx != orig.machine_idx or cand_t != orig.start_min:
                    rerouted_ops += 1
                upd = ScheduledPlateOp(
                    op_id=orig.op_id,
                    plate_id=pid,
                    step_idx=s_idx,
                    pool_id=orig.pool_id,
                    machine_idx=m_idx,
                    pool_cap=pool_caps[orig.pool_id],
                    start_min=cand_t,
                    dur_min=orig.dur_min,
                    release_min=orig.release_min,
                    deadline_min=orig.deadline_min,
                )
                placed_map[(pid, s_idx)] = upd
                busy.setdefault((upd.pool_id, upd.machine_idx), []).append(
                    (upd.start_min, upd.end_min)
                )
        else:
            # Fallback earliest feasible machine if plate was already physically irrecoverable
            cur_prev_end = anchor_end
            for s_idx in missing_indices:
                orig = p_ops[s_idx]
                st = job.steps[s_idx]
                cap = pool_caps[st.pool_id]
                cand_t = max(
                    fault.fault_min,
                    cur_prev_end + st.min_wait_after_prev if s_idx > 0 else job.release_min,
                )
                chosen_m = 0
                found = False
                while not found:
                    for m_idx in range(cap):
                        if _is_machine_free(busy, st.pool_id, m_idx, cand_t, st.dur_min, fault):
                            chosen_m = m_idx
                            found = True
                            break
                    if not found:
                        cand_t += 1
                upd = ScheduledPlateOp(
                    op_id=orig.op_id,
                    plate_id=pid,
                    step_idx=s_idx,
                    pool_id=orig.pool_id,
                    machine_idx=chosen_m,
                    pool_cap=pool_caps[orig.pool_id],
                    start_min=cand_t,
                    dur_min=orig.dur_min,
                    release_min=orig.release_min,
                    deadline_min=orig.deadline_min,
                )
                placed_map[(pid, s_idx)] = upd
                busy.setdefault((upd.pool_id, upd.machine_idx), []).append(
                    (upd.start_min, upd.end_min)
                )
                cur_prev_end = upd.end_min

    # 2. Pre-incubation admission hold for unstarted plates: shift coupled chain together
    unstarted_plates.sort(key=lambda pid: (job_map[pid].deadline_min, job_map[pid].release_min, pid))
    for pid in unstarted_plates:
        job = job_map[pid]
        p_ops = nom_by_plate[pid]
        chain = _find_coupled_chain_placement(
            job=job,
            pool_caps=pool_caps,
            busy=busy,
            earliest_step0=max(fault.fault_min, p_ops[0].start_min),
            fault=fault,
        )
        if chain is None:
            raise RuntimeError(f"Insufficient horizon slack for unstarted plate {pid}")
        if chain[0][1] > p_ops[0].start_min:
            pre_inc_holds += 1
        for orig, (m_idx, cand_t) in zip(p_ops, chain):
            if m_idx != orig.machine_idx or cand_t != orig.start_min:
                rerouted_ops += 1
            upd = ScheduledPlateOp(
                op_id=orig.op_id,
                plate_id=pid,
                step_idx=orig.step_idx,
                pool_id=orig.pool_id,
                machine_idx=m_idx,
                pool_cap=pool_caps[orig.pool_id],
                start_min=cand_t,
                dur_min=orig.dur_min,
                release_min=orig.release_min,
                deadline_min=orig.deadline_min,
            )
            placed_map[(pid, orig.step_idx)] = upd
            busy.setdefault((upd.pool_id, upd.machine_idx), []).append(
                (upd.start_min, upd.end_min)
            )

    repaired_ops = sorted(placed_map.values(), key=lambda o: o.op_id)
    links = build_wait_links(jobs, repaired_ops)
    violated = _count_violated_plates(jobs, repaired_ops, links, fault)
    err = verify_contingency_schedule(repaired_ops, links, fault)
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    return ContingencyRepairResult(
        ops=tuple(repaired_ops),
        links=tuple(links),
        violated_plate_ids=violated,
        pre_incubation_holds=pre_inc_holds,
        rerouted_ops=rerouted_ops,
        verification_error=err,
        latency_ms=elapsed_ms,
    )


def _generate_breakdown_scenario(
    scenario_idx: int,
    rng: random.Random,
) -> tuple[list[PlateJobSpec], dict[int, int], list[ScheduledPlateOp], FaultSpec]:
    """Generate a deterministic multi-plate schedule and mid-run reader/washer breakdown."""
    pool_caps = {0: 2, 1: 8, 2: 2}  # 0=liquid_handler, 1=incubator, 2=plate_reader
    num_plates = 10 + (scenario_idx % 5)  # 10 to 14 plates
    inc_dur = rng.choice([20, 24, 28])
    read_dur = rng.choice([4, 5])
    disp_dur = rng.choice([3, 4])
    # (N-1)-survivable biological luminescence window: 1 + read_dur <= max_wait
    max_wait = 1 + read_dur + rng.choice([0, 1])

    jobs: list[PlateJobSpec] = []
    for p_idx in range(num_plates):
        release = (p_idx // 2) * (disp_dur + 1)
        jobs.append(
            PlateJobSpec(
                plate_id=p_idx + 1,
                release_min=release,
                deadline_min=release + inc_dur + 220,
                steps=(
                    PlateStepSpec(step_idx=0, pool_id=0, dur_min=disp_dur),
                    PlateStepSpec(
                        step_idx=1,
                        pool_id=1,
                        dur_min=inc_dur,
                        min_wait_after_prev=1,
                        max_wait_after_prev=1 + read_dur + 1,
                    ),
                    PlateStepSpec(
                        step_idx=2,
                        pool_id=2,
                        dur_min=read_dur,
                        min_wait_after_prev=1,
                        max_wait_after_prev=max_wait,
                    ),
                ),
            )
        )

    nominal_ops, _ = build_nominal_schedule(jobs, pool_caps)
    # Trigger mid-run breakdown on plate_reader unit 0 right after wave 0 enters incubation
    fault_min = disp_dur + 3 + (scenario_idx % 2)
    repair_min = fault_min + rng.choice([55, 65, 75])
    fault = FaultSpec(
        fault_pool_id=2,
        fault_machine_idx=0,
        fault_min=fault_min,
        repair_min=repair_min,
    )
    return jobs, pool_caps, nominal_ops, fault


def run_benchmark(num_scenarios: int = 40, seed: int = 2026) -> dict[str, Any]:
    """Evaluate naive_fifo_delay vs rolling_contingency_repair across 40 mid-run faults."""
    rng = random.Random(seed)
    total_plates = 0
    naive_violated_plates = 0
    naive_valid_schedules = 0
    repair_violated_plates = 0
    repair_valid_schedules = 0
    total_pre_inc_holds = 0
    total_rerouted_ops = 0
    repair_latencies_ms: list[float] = []

    for idx in range(num_scenarios):
        jobs, pool_caps, nominal_ops, fault = _generate_breakdown_scenario(idx, rng)
        total_plates += len(jobs)

        naive_res = naive_fifo_delay_reschedule(jobs, nominal_ops, pool_caps, fault)
        naive_violated_plates += len(naive_res.violated_plate_ids)
        if naive_res.verification_error is None:
            naive_valid_schedules += 1

        repair_res = rolling_contingency_repair(jobs, nominal_ops, pool_caps, fault)
        repair_violated_plates += len(repair_res.violated_plate_ids)
        if repair_res.verification_error is None:
            repair_valid_schedules += 1
        total_pre_inc_holds += repair_res.pre_incubation_holds
        total_rerouted_ops += repair_res.rerouted_ops
        repair_latencies_ms.append(repair_res.latency_ms)

    naive_saved = total_plates - naive_violated_plates
    repair_saved = total_plates - repair_violated_plates
    sorted_lat = sorted(repair_latencies_ms)
    p95_idx = min(len(sorted_lat) - 1, max(0, int(0.95 * len(sorted_lat)) - 1))

    return {
        "benchmark": "idea_07_rolling_contingency_scheduler",
        "num_scenarios": num_scenarios,
        "total_plates": total_plates,
        "naive_fifo_delay": {
            "violated_plates": naive_violated_plates,
            "saved_plates": naive_saved,
            "window_violation_rate": round(naive_violated_plates / total_plates, 4),
            "valid_schedule_rate": round(naive_valid_schedules / num_scenarios, 4),
        },
        "rolling_contingency_repair": {
            "violated_plates": repair_violated_plates,
            "saved_plates": repair_saved,
            "window_violation_rate": round(repair_violated_plates / total_plates, 4),
            "valid_schedule_rate": round(repair_valid_schedules / num_scenarios, 4),
            "plates_saved_over_naive": repair_saved - naive_saved,
            "pre_incubation_holds_triggered": total_pre_inc_holds,
            "rerouted_ops_total": total_rerouted_ops,
            "mean_recovery_latency_ms": round(statistics.mean(repair_latencies_ms), 3),
            "p95_recovery_latency_ms": round(sorted_lat[p95_idx], 3),
        },
    }


if __name__ == "__main__":
    import json

    print(json.dumps(run_benchmark(), indent=2))
