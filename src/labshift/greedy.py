"""Greedy list-scheduling baseline and window-aware local-repair heuristic."""

from __future__ import annotations

import bisect
import time
from collections import defaultdict

from labshift.checker import check_schedule
from labshift.metrics import compute_metrics
from labshift.models import Instance, Job, Schedule, ScheduledOp


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


class _PoolTracker:
    """Fast interval and per-unit setup state tracker for an instrument pool."""

    def __init__(self, instance: Instance, instrument: str) -> None:
        self.instance = instance
        self.instrument = instrument
        self.capacity = instance.capacities.get(instrument, 0)
        self.has_setup = instance.has_setup_times_for(instrument)
        self.intervals: list[tuple[int, int]] = []
        self._dirty = False
        self._times: list[int] = []
        self._active: list[int] = []
        # Per-unit sorted list of (start, res_end, setup_tag) when has_setup is True
        self.unit_slots: list[list[tuple[int, int, str | None]]] = [
            [] for _ in range(max(0, self.capacity))
        ]
        # Stack of (mode, unit_idx, pos, orig_entry_or_none) for LIFO rollback
        self._history: list[tuple[str, int, int, tuple[int, int, str | None] | None]] = []

    def _ensure_profile(self) -> None:
        if not self._dirty:
            return
        events: list[tuple[int, int]] = []
        for s, e in self.intervals:
            if e > s:
                events.append((s, 1))
                events.append((e, -1))
        events.sort(key=lambda item: item[0])
        times: list[int] = []
        active: list[int] = []
        cur = 0
        idx = 0
        n = len(events)
        while idx < n:
            t = events[idx][0]
            delta = 0
            while idx < n and events[idx][0] == t:
                delta += events[idx][1]
                idx += 1
            if delta != 0:
                cur += delta
                times.append(t)
                active.append(cur)
        self._times = times
        self._active = active
        self._dirty = False

    def earliest_slot(
        self,
        earliest_start: int,
        duration: int,
        setup_tag: str | None = None,
    ) -> tuple[int, int | None, int]:
        """Return (start, unit_index, setup_incurred) >= earliest_start."""
        if duration <= 0:
            return earliest_start, (0 if self.has_setup and self.capacity > 0 else None), 0
        if self.capacity <= 0:
            raise ValueError(
                f"Cannot schedule positive-duration op on '{self.instrument}' "
                f"with capacity {self.capacity} <= 0."
            )

        if not self.has_setup:
            self._ensure_profile()
            times = self._times
            active = self._active
            if not times or earliest_start >= times[-1]:
                return earliest_start, None, 0

            pos = bisect.bisect_right(times, earliest_start) - 1
            cur_act = active[pos] if pos >= 0 else 0
            span_start: int | None = earliest_start if cur_act < self.capacity else None

            for i in range(pos + 1, len(times)):
                t = times[i]
                if span_start is not None and t - span_start >= duration:
                    return span_start, None, 0
                act = active[i]
                if act >= self.capacity:
                    span_start = None
                elif span_start is None:
                    span_start = t

            return (span_start if span_start is not None else times[-1]), None, 0

        best_choice: tuple[int, int, int, int] | None = None
        for u_idx in range(self.capacity):
            slots = self.unit_slots[u_idx]
            if not slots:
                cand_tuple = (earliest_start, 0, 0, u_idx)
                if best_choice is None or cand_tuple < best_choice:
                    best_choice = cand_tuple
                continue

            # Try before first operation on unit u_idx
            s0, _, tag0 = slots[0]
            req_after0 = self.instance.required_setup_time(self.instrument, setup_tag, tag0)
            if earliest_start + duration + req_after0 <= s0:
                cand_tuple = (earliest_start, 0, 1, u_idx)
                if best_choice is None or cand_tuple < best_choice:
                    best_choice = cand_tuple

            # Try after each operation i on unit u_idx
            for i, (_, e_i, tag_i) in enumerate(slots):
                req_before = self.instance.required_setup_time(self.instrument, tag_i, setup_tag)
                cand_s = max(earliest_start, e_i + req_before)
                same_family_rank = 0 if (tag_i is not None and tag_i == setup_tag) else 1
                if i + 1 < len(slots):
                    s_next, _, tag_next = slots[i + 1]
                    req_after = self.instance.required_setup_time(
                        self.instrument, setup_tag, tag_next
                    )
                    if cand_s + duration + req_after > s_next:
                        continue
                cand_tuple = (cand_s, req_before, same_family_rank, u_idx)
                if best_choice is None or cand_tuple < best_choice:
                    best_choice = cand_tuple

        assert best_choice is not None
        return best_choice[0], best_choice[3], best_choice[1]

    def max_valid_extension_end(
        self,
        orig_end: int,
        target_res_end: int,
        unit_index: int | None,
        setup_tag: str | None,
    ) -> int:
        """Return max timestamp in [orig_end, target_res_end] to which an op ending at orig_end
        can hold its station without colliding with other scheduled intervals or setups."""
        if target_res_end <= orig_end:
            return target_res_end

        if not self.has_setup:
            self._ensure_profile()
            times = self._times
            active = self._active
            if not times or orig_end >= times[-1]:
                return target_res_end
            pos = bisect.bisect_right(times, orig_end) - 1
            cur_act = active[pos] if pos >= 0 else 0
            if cur_act >= self.capacity:
                return orig_end
            for i in range(pos + 1, len(times)):
                t = times[i]
                if t >= target_res_end:
                    return target_res_end
                if active[i] >= self.capacity:
                    return t
            return target_res_end

        assert unit_index is not None
        slots = self.unit_slots[unit_index]
        for i, (_, e_i, _) in enumerate(slots):
            if e_i == orig_end:
                if i + 1 < len(slots):
                    s_next, _, tag_next = slots[i + 1]
                    req_after = self.instance.required_setup_time(
                        self.instrument, setup_tag, tag_next
                    )
                    return min(target_res_end, max(orig_end, s_next - req_after))
                return target_res_end
        return target_res_end

    def push_interval(
        self,
        start: int,
        end: int,
        unit_index: int | None = None,
        setup_tag: str | None = None,
    ) -> None:
        if not self.has_setup:
            self.intervals.append((start, end))
            if not self._dirty and end > start:
                if not self._times or start > self._times[-1]:
                    self._times.append(start)
                    self._active.append(1)
                    self._times.append(end)
                    self._active.append(0)
                    self._history.append(("anon_tail2", -1, -1, None))
                    return
                if start == self._times[-1]:
                    prev_act = self._active[-1]
                    self._active[-1] = prev_act + 1
                    self._times.append(end)
                    self._active.append(0)
                    self._history.append(("anon_tail1", prev_act, -1, None))
                    return
            self._dirty = True
            self._history.append(("anon", -1, len(self.intervals) - 1, None))
            return

        u_idx = unit_index if unit_index is not None else 0
        slots = self.unit_slots[u_idx]
        ins_pos = bisect.bisect_left(slots, (start, end, setup_tag))
        slots.insert(ins_pos, (start, end, setup_tag))
        self._history.append(("unit", u_idx, ins_pos, None))

    def extend_residence(
        self,
        orig_start: int,
        orig_end: int,
        new_end: int,
        unit_index: int | None = None,
        setup_tag: str | None = None,
    ) -> None:
        if new_end <= orig_end:
            self._history.append(("noop", -1, -1, None))
            return
        if not self.has_setup:
            self.intervals.append((orig_end, new_end))
            self._dirty = True
            self._history.append(("anon", -1, len(self.intervals) - 1, None))
            return

        u_idx = unit_index if unit_index is not None else 0
        slots = self.unit_slots[u_idx]
        for idx, (s_i, e_i, tag_i) in enumerate(slots):
            if s_i == orig_start and e_i == orig_end:
                slots[idx] = (orig_start, new_end, tag_i)
                self._history.append(("extend_unit", u_idx, idx, (s_i, e_i, tag_i)))
                return
        self._history.append(("noop", -1, -1, None))

    def pop_last(self) -> None:
        kind, u_idx, pos, prev_entry = self._history.pop()
        if kind == "noop":
            return
        if kind == "anon_tail2":
            self._intervals_pop_clean(2, None)
            return
        if kind == "anon_tail1":
            self._intervals_pop_clean(1, u_idx)
            return
        if kind == "anon":
            self.intervals.pop()
            self._dirty = True
            return
        if kind == "unit":
            self.unit_slots[u_idx].pop(pos)
            return
        if kind == "extend_unit":
            assert prev_entry is not None
            self.unit_slots[u_idx][pos] = prev_entry

    def _intervals_pop_clean(self, pop_count: int, restored_last_act: int | None) -> None:
        self.intervals.pop()
        for _ in range(pop_count):
            self._times.pop()
            self._active.pop()
        if restored_last_act is not None:
            self._active[-1] = restored_last_act


def _has_infeasible_capacity(instance: Instance) -> bool:
    """Return True if any required operation or robotic arm transfer has capacity <= 0."""
    arm_cap = instance.capacities.get("robot_arm", 0)
    for job in instance.jobs:
        for idx, op in enumerate(job.ops):
            if op.instrument not in instance.capacities or instance.capacities[op.instrument] <= 0:
                return True
            if idx > 0 and instance.uses_robot_arm(job.ops[idx - 1].instrument, op) and arm_cap <= 0:
                return True
    return False


def _find_op_slot_tracked(
    trackers: dict[str, _PoolTracker],
    instance: Instance,
    job: Job,
    op_index: int,
    earliest_start: int,
) -> tuple[int, int | None, int]:
    """Find earliest (start, unit_index, setup_incurred) >= earliest_start for job.ops[op_index]."""
    op = job.ops[op_index]
    tag = instance.effective_setup_tag(job, op)
    inst_tracker = trackers[op.instrument]

    if op_index == 0 or not instance.uses_robot_arm(job.ops[op_index - 1].instrument, op):
        return inst_tracker.earliest_slot(earliest_start, op.duration, tag)

    eff_t = instance.effective_transfer_time(job.ops[op_index - 1].instrument, op)
    arm_tracker = trackers["robot_arm"]
    t = earliest_start
    while True:
        arm_s, _, _ = arm_tracker.earliest_slot(t - eff_t, eff_t, None)
        t_after_arm = arm_s + eff_t
        t_inst, u_idx, setup_inc = inst_tracker.earliest_slot(t_after_arm, op.duration, tag)
        if t_inst == t_after_arm:
            return t_inst, u_idx, setup_inc
        t = t_inst


def solve_greedy(instance: Instance) -> Schedule:
    """Schedule operations via earliest-start list scheduling.

    Dispatches ready operations in priority order of (ready_time, deadline, duration, job_id).
    Respects release times, precedence, min_wait, transfer_time, setup_times, and instrument
    capacities; may violate max_wait windows or deadlines on constrained instances.
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
    prev_placed: dict[str, tuple[int, int, int | None, str | None]] = {}
    job_map = {job.job_id: job for job in instance.jobs}
    trackers: dict[str, _PoolTracker] = {
        inst: _PoolTracker(instance, inst) for inst in instance.capacities
    }
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
        tag = instance.effective_setup_tag(job, op)

        start, unit_idx, _ = _find_op_slot_tracked(
            trackers, instance, job, idx, ready_time[chosen_id]
        )
        end = start + op.duration

        trackers[op.instrument].push_interval(start, end, unit_idx, tag)
        if idx > 0 and instance.uses_robot_arm(job.ops[idx - 1].instrument, op):
            eff_t = instance.effective_transfer_time(job.ops[idx - 1].instrument, op)
            pickup = start - eff_t
            trackers["robot_arm"].push_interval(pickup, start)
            if instance.blocking_handoff and chosen_id in prev_placed:
                p_s, p_e, p_u, p_tag = prev_placed[chosen_id]
                if pickup > p_e:
                    trackers[job.ops[idx - 1].instrument].extend_residence(
                        p_s, p_e, pickup, p_u, p_tag
                    )

        prev_placed[chosen_id] = (start, end, unit_idx, tag)
        emit_unit = (
            unit_idx
            if (unit_idx is not None and instance.capacities.get(op.instrument, 0) > 1)
            else None
        )
        scheduled.append(
            ScheduledOp(
                job_id=chosen_id,
                op_index=idx,
                name=op.name,
                instrument=op.instrument,
                start=start,
                end=end,
                unit_index=emit_unit,
            )
        )

        next_idx = idx + 1
        next_op_idx[chosen_id] = next_idx
        if next_idx < len(job.ops):
            next_op = job.ops[next_idx]
            eff_next_t = instance.effective_transfer_time(op.instrument, next_op)
            ready_time[chosen_id] = end + max(next_op.min_wait, eff_next_t)

    elapsed = time.perf_counter() - t0
    return Schedule(
        instance_name=instance.name,
        solver="greedy",
        ops=tuple(scheduled),
        status="OK",
        solve_time_s=elapsed,
        best_bound=None,
    )


def _coupled_segment_end(instance: Instance, job: Job, start_idx: int) -> int:
    """Return inclusive end index of the coupled segment from start_idx.

    Couples contiguous operations linked by either max_wait windows or blocking
    robot_arm source-station handoffs so source occupancy is never overbooked.
    """
    ops = job.ops
    end_idx = start_idx
    while end_idx + 1 < len(ops):
        next_op = ops[end_idx + 1]
        blocking_arm = (
            instance.blocking_handoff
            and instance.uses_robot_arm(ops[end_idx].instrument, next_op)
        )
        if next_op.max_wait is not None or blocking_arm:
            end_idx += 1
        else:
            break
    return end_idx


def _compute_job_lct_starts(instance: Instance, job: Job) -> list[int] | None:
    """Compute latest feasible start per operation under job.deadline, or None if un-deadlined."""
    if job.deadline is None or not job.ops:
        return None
    m = len(job.ops)
    lct_starts = [0] * m
    cur_end_ub = job.deadline
    for r in range(m - 1, -1, -1):
        lct_starts[r] = cur_end_ub - job.ops[r].duration
        if r > 0:
            eff_wait = max(
                job.ops[r].min_wait,
                instance.effective_transfer_time(job.ops[r - 1].instrument, job.ops[r]),
            )
            cur_end_ub = lct_starts[r] - eff_wait
    return lct_starts


def _place_coupled_segment(
    trackers: dict[str, _PoolTracker],
    instance: Instance,
    job: Job,
    a: int,
    b: int,
    ready_a: int,
    prev_job_op: tuple[int, int, int | None, str | None] | None,
    lct_starts: list[int] | None,
    *,
    allow_deadline_prune: bool = True,
) -> list[tuple[int, int, int, int | None]]:
    """Place job.ops[a..b] using recursive intra-chain right-shift propagation.

    When downstream step k > a overflows its max_wait window or source-station
    blocking window, recursively right-shifts immediate predecessors k - 1 .. a + 1
    within their own [lo_r, hi_r] slack before advancing cand_a (op_a).
    """
    ops = job.ops
    INF = 10**12

    def _search(
        enforce_dl: bool,
        max_visits: int,
    ) -> list[tuple[int, int, int, int | None]] | None:
        visits = 0

        def _rec(
            r: int,
            prev_info: tuple[int, int, int | None, str | None] | None,
        ) -> tuple[list[tuple[int, int, int, int | None]] | None, int | None]:
            """Return (placed_tail, needed_prev_end).

            needed_prev_end is None when pruned by deadline_hi or visit cap.
            """
            nonlocal visits
            op_r = ops[r]
            tag_r = instance.effective_setup_tag(job, op_r)
            dl_hi = (
                lct_starts[r]
                if (enforce_dl and lct_starts is not None)
                else INF
            )

            if r == a:
                lo_r = ready_a
                win_hi = INF
            else:
                assert prev_info is not None
                prev_s, prev_e, prev_u, prev_tag = prev_info
                eff_t = instance.effective_transfer_time(ops[r - 1].instrument, op_r)
                eff_min = max(op_r.min_wait, eff_t)
                lo_r = prev_e + eff_min
                win_hi = (prev_e + op_r.max_wait) if op_r.max_wait is not None else INF

                # Under blocking handoff on r - 1 -> r, pickup = s_r - eff_t cannot
                # exceed the earliest timestamp where another job occupies ops[r - 1].instrument.
                if instance.blocking_handoff and instance.uses_robot_arm(
                    ops[r - 1].instrument, op_r
                ):
                    max_hold = trackers[ops[r - 1].instrument].max_valid_extension_end(
                        prev_e,
                        (win_hi - eff_t) if win_hi < INF else prev_e + 10**9,
                        prev_u,
                        prev_tag,
                    )
                    win_hi = min(win_hi, max_hold + eff_t)

            if lo_r > dl_hi:
                return None, None
            if lo_r > win_hi:
                # Happens when blocking handoff on prev_op cannot even hold until lo_r - eff_t
                eff_t_r = (
                    instance.effective_transfer_time(ops[r - 1].instrument, op_r)
                    if r > a
                    else 0
                )
                return None, lo_r - eff_t_r + 1

            cand_r = lo_r
            while True:
                visits += 1
                if visits > max_visits:
                    return None, None

                s_r, u_r, _ = _find_op_slot_tracked(trackers, instance, job, r, cand_r)
                if s_r > dl_hi:
                    return None, None

                # Also check blocking handoff from pre-segment op a - 1 if r == a > 0
                if (
                    r == a
                    and a > 0
                    and prev_info is not None
                    and instance.blocking_handoff
                    and instance.uses_robot_arm(ops[a - 1].instrument, op_r)
                ):
                    eff_t_a = instance.effective_transfer_time(ops[a - 1].instrument, op_r)
                    pickup_a = s_r - eff_t_a
                    _, prev_e_a, prev_u_a, prev_tag_a = prev_info
                    if pickup_a > prev_e_a:
                        max_hold_a = trackers[ops[a - 1].instrument].max_valid_extension_end(
                            prev_e_a, pickup_a, prev_u_a, prev_tag_a
                        )
                        # Pre-segment op a - 1 is already committed; if blocked, take earliest anyway.
                        _ = max_hold_a

                if s_r > win_hi:
                    assert r > a and prev_info is not None
                    if op_r.max_wait is not None and s_r > (prev_info[1] + op_r.max_wait):
                        return None, s_r - op_r.max_wait
                    eff_t_r = instance.effective_transfer_time(ops[r - 1].instrument, op_r)
                    return None, s_r - eff_t_r

                e_r = s_r + op_r.duration
                if r == b:
                    # Commit r == b and any arm / residence extensions up the call stack on unwind
                    return [(r, s_r, e_r, u_r)], None

                # Tentatively push op_r (and any arm / blocking extension on r - 1 -> r)
                pushed: list[str] = []
                if r > a and prev_info is not None and instance.uses_robot_arm(
                    ops[r - 1].instrument, op_r
                ):
                    eff_t_r = instance.effective_transfer_time(ops[r - 1].instrument, op_r)
                    pickup_r = s_r - eff_t_r
                    trackers["robot_arm"].push_interval(pickup_r, s_r)
                    pushed.append("robot_arm")
                    if instance.blocking_handoff and pickup_r > prev_info[1]:
                        trackers[ops[r - 1].instrument].extend_residence(
                            prev_info[0], prev_info[1], pickup_r, prev_info[2], prev_info[3]
                        )
                        pushed.append(ops[r - 1].instrument)
                elif r == a and a > 0 and instance.uses_robot_arm(ops[a - 1].instrument, op_r):
                    eff_t_a = instance.effective_transfer_time(ops[a - 1].instrument, op_r)
                    pickup_a = s_r - eff_t_a
                    trackers["robot_arm"].push_interval(pickup_a, s_r)
                    pushed.append("robot_arm")
                    if (
                        instance.blocking_handoff
                        and prev_info is not None
                        and pickup_a > prev_info[1]
                    ):
                        trackers[ops[a - 1].instrument].extend_residence(
                            prev_info[0], prev_info[1], pickup_a, prev_info[2], prev_info[3]
                        )
                        pushed.append(ops[a - 1].instrument)

                trackers[op_r.instrument].push_interval(s_r, e_r, u_r, tag_r)
                pushed.append(op_r.instrument)

                tail, needed_end_r = _rec(r + 1, (s_r, e_r, u_r, tag_r))

                for inst_name in reversed(pushed):
                    trackers[inst_name].pop_last()

                if tail is not None:
                    return [(r, s_r, e_r, u_r)] + tail, None

                if needed_end_r is None:
                    return None, None

                cand_r = max(s_r + 1, needed_end_r - op_r.duration)
                if cand_r > dl_hi:
                    return None, None
                if cand_r > win_hi:
                    assert r > a and prev_info is not None
                    if op_r.max_wait is not None and cand_r > (prev_info[1] + op_r.max_wait):
                        return None, cand_r - op_r.max_wait
                    eff_t_r = instance.effective_transfer_time(ops[r - 1].instrument, op_r)
                    return None, cand_r - eff_t_r

        sol, _ = _rec(a, prev_job_op)
        return sol

    placed: list[tuple[int, int, int, int | None]] | None = None
    if allow_deadline_prune and lct_starts is not None and ready_a <= lct_starts[a]:
        placed = _search(enforce_dl=True, max_visits=2500)

    if placed is None:
        placed = _search(enforce_dl=False, max_visits=5000)

    if placed is None:
        # Guaranteed tail placement after all currently scheduled operations clear
        horizon_tail = ready_a
        for trk in trackers.values():
            if not trk.has_setup:
                trk._ensure_profile()
                if trk._times:
                    horizon_tail = max(horizon_tail, trk._times[-1])
            else:
                for slots in trk.unit_slots:
                    if slots:
                        horizon_tail = max(horizon_tail, slots[-1][1])
        max_setup = 0
        if instance.setup_times:
            max_setup = max(
                (
                    val
                    for inst_m in instance.setup_times.values()
                    for row in inst_m.values()
                    for val in row.values()
                ),
                default=0,
            )
        cur_t = max(ready_a, horizon_tail + max_setup)
        fallback: list[tuple[int, int, int, int | None]] = []
        for r in range(a, b + 1):
            op_r = ops[r]
            if r > a:
                eff_t = instance.effective_transfer_time(ops[r - 1].instrument, op_r)
                cur_t = fallback[-1][2] + max(op_r.min_wait, eff_t)
            s_r, u_r, _ = _find_op_slot_tracked(trackers, instance, job, r, cur_t)
            fallback.append((r, s_r, s_r + op_r.duration, u_r))
        placed = fallback

    # Permanently commit placed[0..b-a] to trackers
    cur_prev = prev_job_op
    for idx_in_seg, (r, s_r, e_r, u_r) in enumerate(placed):
        op_r = ops[r]
        tag_r = instance.effective_setup_tag(job, op_r)
        if r > 0 and instance.uses_robot_arm(ops[r - 1].instrument, op_r):
            eff_t_r = instance.effective_transfer_time(ops[r - 1].instrument, op_r)
            pickup_r = s_r - eff_t_r
            trackers["robot_arm"].push_interval(pickup_r, s_r)
            if instance.blocking_handoff and cur_prev is not None and pickup_r > cur_prev[1]:
                trackers[ops[r - 1].instrument].extend_residence(
                    cur_prev[0], cur_prev[1], pickup_r, cur_prev[2], cur_prev[3]
                )
        trackers[op_r.instrument].push_interval(s_r, e_r, u_r, tag_r)
        cur_prev = (s_r, e_r, u_r, tag_r)

    return placed


def _run_coupled_pass(
    instance: Instance,
    mode: str,
    *,
    allow_deadline_prune: bool = True,
) -> list[ScheduledOp]:
    """Schedule max_wait-coupled chain segments atomically via recursive slack propagation."""
    next_op_idx: dict[str, int] = {job.job_id: 0 for job in instance.jobs}
    ready_time: dict[str, int] = {job.job_id: job.release for job in instance.jobs}
    prev_placed: dict[str, tuple[int, int, int | None, str | None]] = {}
    job_map = {job.job_id: job for job in instance.jobs}
    rem_work: dict[str, list[int]] = {}
    job_lcts: dict[str, list[int] | None] = {}
    seg_end_cache: dict[tuple[str, int], int] = {}
    seg_meta: dict[tuple[str, int], tuple[int, int, float, float]] = {}

    for job in instance.jobs:
        m = len(job.ops)
        rem = [0] * (m + 1)
        dl_f = float(job.deadline) if job.deadline is not None else float("inf")
        for k in range(m - 1, -1, -1):
            wait_after = (
                max(
                    job.ops[k + 1].min_wait,
                    instance.effective_transfer_time(job.ops[k].instrument, job.ops[k + 1]),
                )
                if k + 1 < m
                else 0
            )
            rem[k] = job.ops[k].duration + wait_after + rem[k + 1]
            b_k = _coupled_segment_end(instance, job, k)
            seg_end_cache[(job.job_id, k)] = b_k
            s_dur = sum(job.ops[r].duration for r in range(k, b_k + 1))
            c_flag = 0.0 if b_k > k else 1.0
            seg_meta[(job.job_id, k)] = (b_k, s_dur, c_flag, dl_f)
        rem_work[job.job_id] = rem
        job_lcts[job.job_id] = _compute_job_lct_starts(instance, job)

    # Precompute outgoing setup aggressiveness per job for setup-aware modes
    outgoing_setup_score: dict[str, float] = {job.job_id: 0.0 for job in instance.jobs}
    if instance.setup_times:
        tags_by_inst: dict[str, set[str]] = defaultdict(set)
        for job in instance.jobs:
            for op in job.ops:
                t = instance.effective_setup_tag(job, op)
                if t is not None:
                    tags_by_inst[op.instrument].add(t)
        for job in instance.jobs:
            total_out = 0.0
            for op in job.ops:
                t = instance.effective_setup_tag(job, op)
                if t is not None and op.instrument in instance.setup_times:
                    others = [ot for ot in tags_by_inst[op.instrument] if ot != t]
                    if others:
                        total_out += max(
                            instance.required_setup_time(op.instrument, t, ot)
                            for ot in others
                        )
            outgoing_setup_score[job.job_id] = total_out

    trackers: dict[str, _PoolTracker] = {
        inst: _PoolTracker(instance, inst) for inst in instance.capacities
    }
    scheduled: list[ScheduledOp] = []
    active_jobs: list[str] = [j.job_id for j in instance.jobs if j.ops]

    while active_jobs:
        def seg_priority(job_id: str) -> tuple[float, float, float, int, str]:
            a = next_op_idx[job_id]
            _, seg_dur, coupled_flag, dl = seg_meta[(job_id, a)]
            r_t = float(ready_time[job_id])
            slack = dl - (r_t + rem_work[job_id][a])

            if mode in ("setup_batch", "setup_clean_first"):
                job = job_map[job_id]
                est_s, _, inc_setup = _find_op_slot_tracked(
                    trackers, instance, job, a, ready_time[job_id]
                )
                out_pen = outgoing_setup_score[job_id]
                if mode == "setup_batch":
                    return (float(inc_setup), float(est_s) + 0.5 * out_pen, slack, seg_dur, job_id)
                return (out_pen, float(est_s), float(inc_setup), seg_dur, job_id)

            if mode == "tight_first":
                return (r_t, coupled_flag, slack, seg_dur, job_id)
            if mode == "shift_coupled":
                return (r_t, 1.0 - coupled_flag, dl, seg_dur, job_id)
            if mode == "slack":
                return (slack, r_t, coupled_flag, seg_dur, job_id)
            if mode == "edf":
                return (dl, r_t, coupled_flag, seg_dur, job_id)
            return (r_t, dl, coupled_flag, seg_dur, job_id)

        chosen_id = min(active_jobs, key=seg_priority)
        job = job_map[chosen_id]
        a = next_op_idx[chosen_id]
        b = seg_end_cache[(chosen_id, a)]

        placed = _place_coupled_segment(
            trackers,
            instance,
            job,
            a,
            b,
            ready_time[chosen_id],
            prev_placed.get(chosen_id),
            job_lcts[chosen_id],
            allow_deadline_prune=allow_deadline_prune,
        )

        for k, s_k, e_k, u_k in placed:
            op_k = job.ops[k]
            emit_unit = (
                u_k
                if (u_k is not None and instance.capacities.get(op_k.instrument, 0) > 1)
                else None
            )
            scheduled.append(
                ScheduledOp(
                    job_id=chosen_id,
                    op_index=k,
                    name=op_k.name,
                    instrument=op_k.instrument,
                    start=s_k,
                    end=e_k,
                    unit_index=emit_unit,
                )
            )

        last_k, last_s, last_e, last_u = placed[-1]
        prev_placed[chosen_id] = (
            last_s,
            last_e,
            last_u,
            instance.effective_setup_tag(job, job.ops[last_k]),
        )

        next_idx = b + 1
        next_op_idx[chosen_id] = next_idx
        if next_idx < len(job.ops):
            next_op = job.ops[next_idx]
            eff_next_t = instance.effective_transfer_time(job.ops[b].instrument, next_op)
            ready_time[chosen_id] = last_e + max(next_op.min_wait, eff_next_t)
        else:
            active_jobs.remove(chosen_id)

    return scheduled


def solve_greedy_repair(instance: Instance) -> Schedule:
    """Pure-stdlib window-aware scheduling heuristic with coupled right-shift repair.

    Schedules contiguous max_wait-coupled operation segments atomically by right-shifting
    intermediate and upstream operations whenever a downstream bottleneck would force
    wait > max_wait, selecting the best schedule across deterministic priority rules.
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

    modes: list[str] = ["earliest", "tight_first", "shift_coupled", "slack", "edf"]
    if instance.setup_times:
        modes.extend(["setup_batch", "setup_clean_first"])

    for mode in modes:
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
