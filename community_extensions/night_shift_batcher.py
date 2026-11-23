"""Idea #08: Day/Night Dual-Regime Queue Partitioner.

Inspired by OpenLabAI and Osprey overnight autonomous throughput debates:
  - Attended Daytime Window [0, 480) min (8h per 24h cycle): reserved for human-cosigned
    frontier / exploratory runs (plus short Lean-certified backfill in slack gaps).
  - Unattended Overnight Window [480, 1440) min (16h per 24h cycle): strictly gated to
    zero-click Lean-certified protocols, eliminating human-approval bottlenecks overnight
    while guaranteeing zero unverified or unsigned autonomous execution.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any

MINUTES_PER_DAY = 1440
DAYTIME_END_MIN = 480


@dataclass(frozen=True)
class ProtocolRequest:
    """Candidate protocol submitted to a multi-lane automated workcell queue."""

    protocol_id: str
    arrival_min: int
    dur_min: int
    is_frontier_exception: bool
    human_cosigned: bool
    lean_certified: bool
    priority_weight: float = 1.0


@dataclass(frozen=True)
class ScheduledProtocolSlot:
    """Concrete scheduled protocol slot on a workcell lane."""

    protocol_id: str
    lane_idx: int
    start_min: int
    dur_min: int
    is_frontier_exception: bool
    human_cosigned: bool
    lean_certified: bool

    @property
    def end_min(self) -> int:
        return self.start_min + self.dur_min


@dataclass(frozen=True)
class DualRegimePackerResult:
    """72h scheduling summary for either FIFO human-gated or Dual-Regime Night Packer."""

    policy: str
    scheduled_slots: tuple[ScheduledProtocolSlot, ...]
    quarantined_ids: tuple[str, ...]
    deferred_ids: tuple[str, ...]
    completed_protocols: int
    frontier_completed: int
    certified_completed: int
    daytime_utilization_pct: float
    overnight_utilization_pct: float
    overall_utilization_pct: float
    verification_error: str | None


def check_dual_regime_slot(slot: ScheduledProtocolSlot) -> str | None:
    """Exact Python mirror of LabShiftLean.checkDualRegimeSlot across multi-day horizons."""
    if slot.dur_min <= 0:
        return "diurnal_slot_bounds"
    day_idx = slot.start_min // MINUTES_PER_DAY
    end_day_idx = (slot.end_min - 1) // MINUTES_PER_DAY
    if day_idx != end_day_idx:
        return "diurnal_slot_bounds"

    start_in_day = slot.start_min % MINUTES_PER_DAY
    end_in_day = start_in_day + slot.dur_min
    if end_in_day > MINUTES_PER_DAY:
        return "diurnal_slot_bounds"

    if slot.is_frontier_exception:
        if not (end_in_day <= DAYTIME_END_MIN and slot.human_cosigned):
            return "frontier_daytime_cosign_gate"
    else:
        if not slot.lean_certified:
            return "autonomous_requires_lean_cert"
    return None


def verify_dual_regime_schedule(
    slots: list[ScheduledProtocolSlot],
    num_lanes: int,
    horizon_min: int,
) -> str | None:
    """Verify slot regime invariants, horizon bounds, and per-lane mutual exclusion."""
    by_lane: dict[int, list[ScheduledProtocolSlot]] = {}
    for s in slots:
        if not (0 <= s.lane_idx < num_lanes):
            return "invalid_lane_index"
        if s.start_min < 0 or s.end_min > horizon_min:
            return "horizon_bounds_exceeded"
        err = check_dual_regime_slot(s)
        if err is not None:
            return err
        by_lane.setdefault(s.lane_idx, []).append(s)

    for lane_slots in by_lane.values():
        ordered = sorted(lane_slots, key=lambda x: x.start_min)
        for idx in range(1, len(ordered)):
            if ordered[idx - 1].end_min > ordered[idx].start_min:
                return "lane_mutual_exclusion"
    return None


def _is_request_admissible(req: ProtocolRequest) -> bool:
    if req.dur_min <= 0:
        return False
    if req.is_frontier_exception:
        return req.human_cosigned and req.dur_min <= DAYTIME_END_MIN
    return req.lean_certified and req.dur_min <= (MINUTES_PER_DAY - DAYTIME_END_MIN)


def _compute_utilizations(
    slots: list[ScheduledProtocolSlot],
    num_lanes: int,
    horizon_min: int,
) -> tuple[float, float, float]:
    num_days = horizon_min // MINUTES_PER_DAY
    daytime_cap = num_days * DAYTIME_END_MIN * num_lanes
    overnight_cap = num_days * (MINUTES_PER_DAY - DAYTIME_END_MIN) * num_lanes
    total_cap = horizon_min * num_lanes

    day_busy = 0
    night_busy = 0
    for s in slots:
        for d in range(num_days):
            d_start = d * MINUTES_PER_DAY
            d_mid = d_start + DAYTIME_END_MIN
            d_end = d_start + MINUTES_PER_DAY
            day_busy += max(0, min(s.end_min, d_mid) - max(s.start_min, d_start))
            night_busy += max(0, min(s.end_min, d_end) - max(s.start_min, d_mid))

    day_pct = round(100.0 * day_busy / daytime_cap, 2) if daytime_cap else 0.0
    night_pct = round(100.0 * night_busy / overnight_cap, 2) if overnight_cap else 0.0
    total_pct = round(100.0 * (day_busy + night_busy) / total_cap, 2) if total_cap else 0.0
    return day_pct, night_pct, total_pct


def pack_fifo_human_gated_72h(
    requests: list[ProtocolRequest],
    num_lanes: int = 3,
    horizon_hours: int = 72,
) -> DualRegimePackerResult:
    """Baseline FIFO human-gated policy: every run requires daytime human launch in [0, 480)."""
    horizon_min = horizon_hours * 60
    num_days = horizon_min // MINUTES_PER_DAY
    quarantined: list[str] = []
    admissible: list[ProtocolRequest] = []
    for req in requests:
        if not _is_request_admissible(req):
            quarantined.append(req.protocol_id)
        else:
            admissible.append(req)

    admissible.sort(key=lambda r: (r.arrival_min, r.protocol_id))
    # Track lane cursors per day strictly inside [d*1440, d*1440 + 480)
    day_lane_end: dict[tuple[int, int], int] = {
        (d, lane): d * MINUTES_PER_DAY for d in range(num_days) for lane in range(num_lanes)
    }

    scheduled: list[ScheduledProtocolSlot] = []
    deferred: list[str] = []
    for req in admissible:
        placed = False
        for d in range(num_days):
            win_start = d * MINUTES_PER_DAY
            win_end = win_start + DAYTIME_END_MIN
            if req.arrival_min >= win_end:
                continue
            # Pick earliest available lane in this daytime shift
            best_choice: tuple[int, int] | None = None
            for lane in range(num_lanes):
                cand_start = max(day_lane_end[(d, lane)], req.arrival_min, win_start)
                if cand_start + req.dur_min <= win_end:
                    if best_choice is None or cand_start < best_choice[1]:
                        best_choice = (lane, cand_start)
            if best_choice is not None:
                lane, cand_start = best_choice
                slot = ScheduledProtocolSlot(
                    protocol_id=req.protocol_id,
                    lane_idx=lane,
                    start_min=cand_start,
                    dur_min=req.dur_min,
                    is_frontier_exception=req.is_frontier_exception,
                    human_cosigned=req.human_cosigned,
                    lean_certified=req.lean_certified,
                )
                scheduled.append(slot)
                day_lane_end[(d, lane)] = slot.end_min
                placed = True
                break
        if not placed:
            deferred.append(req.protocol_id)

    day_u, night_u, all_u = _compute_utilizations(scheduled, num_lanes, horizon_min)
    err = verify_dual_regime_schedule(scheduled, num_lanes, horizon_min)
    return DualRegimePackerResult(
        policy="fifo_human_gated",
        scheduled_slots=tuple(scheduled),
        quarantined_ids=tuple(quarantined),
        deferred_ids=tuple(deferred),
        completed_protocols=len(scheduled),
        frontier_completed=sum(1 for s in scheduled if s.is_frontier_exception),
        certified_completed=sum(1 for s in scheduled if not s.is_frontier_exception),
        daytime_utilization_pct=day_u,
        overnight_utilization_pct=night_u,
        overall_utilization_pct=all_u,
        verification_error=err,
    )


def pack_dual_regime_72h(
    requests: list[ProtocolRequest],
    num_lanes: int = 3,
    horizon_hours: int = 72,
) -> DualRegimePackerResult:
    """Dual-regime night packer: cosigned frontier in [0, 480), Lean-certified in [480, 1440) + slack."""
    horizon_min = horizon_hours * 60
    num_days = horizon_min // MINUTES_PER_DAY
    quarantined: list[str] = []
    frontier_reqs: list[ProtocolRequest] = []
    certified_reqs: list[ProtocolRequest] = []

    for req in requests:
        if not _is_request_admissible(req):
            quarantined.append(req.protocol_id)
        elif req.is_frontier_exception:
            frontier_reqs.append(req)
        else:
            certified_reqs.append(req)

    frontier_reqs.sort(
        key=lambda r: (r.arrival_min, -r.priority_weight, r.dur_min, r.protocol_id)
    )
    # Sort certified protocols by priority density / shortest-processing-time for dense bin packing
    certified_reqs.sort(
        key=lambda r: (r.arrival_min // MINUTES_PER_DAY, -r.priority_weight / r.dur_min, r.dur_min)
    )

    day_cursors: dict[tuple[int, int], int] = {
        (d, lane): d * MINUTES_PER_DAY for d in range(num_days) for lane in range(num_lanes)
    }
    night_cursors: dict[tuple[int, int], int] = {
        (d, lane): d * MINUTES_PER_DAY + DAYTIME_END_MIN
        for d in range(num_days)
        for lane in range(num_lanes)
    }

    scheduled: list[ScheduledProtocolSlot] = []
    deferred: list[str] = []

    # Phase 1: Pack human-cosigned frontier runs strictly inside daytime [0, 480) windows
    for req in frontier_reqs:
        placed = False
        for d in range(num_days):
            win_start = d * MINUTES_PER_DAY
            win_end = win_start + DAYTIME_END_MIN
            if req.arrival_min >= win_end:
                continue
            best_lane: tuple[int, int, int] | None = None  # (rem_after, cand_start, lane)
            for lane in range(num_lanes):
                cand_start = max(day_cursors[(d, lane)], req.arrival_min, win_start)
                if cand_start + req.dur_min <= win_end:
                    rem_after = win_end - (cand_start + req.dur_min)
                    choice = (rem_after, cand_start, lane)
                    if best_lane is None or choice < best_lane:
                        best_lane = choice
            if best_lane is not None:
                _, cand_start, lane = best_lane
                slot = ScheduledProtocolSlot(
                    protocol_id=req.protocol_id,
                    lane_idx=lane,
                    start_min=cand_start,
                    dur_min=req.dur_min,
                    is_frontier_exception=True,
                    human_cosigned=req.human_cosigned,
                    lean_certified=req.lean_certified,
                )
                scheduled.append(slot)
                day_cursors[(d, lane)] = slot.end_min
                placed = True
                break
        if not placed:
            deferred.append(req.protocol_id)

    # Phase 2: Pack zero-click Lean-certified runs across 16h overnight [480, 1440) windows
    # plus leftover daytime [0, 480) slack gaps
    for req in certified_reqs:
        placed = False
        for d in range(num_days):
            day_start = d * MINUTES_PER_DAY
            day_mid = day_start + DAYTIME_END_MIN
            day_end = day_start + MINUTES_PER_DAY

            # Prefer overnight [480, 1440) autonomous window first, then daytime slack if arrived early
            candidate_windows = [
                ("night", day_mid, day_end, night_cursors),
                ("day", day_start, day_mid, day_cursors),
            ]
            best_pick: tuple[int, int, str, int] | None = None
            for kind, w_start, w_end, cursors in candidate_windows:
                if req.arrival_min >= w_end:
                    continue
                for lane in range(num_lanes):
                    cand_start = max(cursors[(d, lane)], req.arrival_min, w_start)
                    if cand_start + req.dur_min <= w_end:
                        prio = 0 if kind == "night" else 1
                        rem = w_end - (cand_start + req.dur_min)
                        pick = (prio, rem, kind, lane)
                        if best_pick is None or pick < best_pick:
                            best_pick = pick
            if best_pick is not None:
                _, _, kind, lane = best_pick
                cursors = night_cursors if kind == "night" else day_cursors
                w_start = day_mid if kind == "night" else day_start
                cand_start = max(cursors[(d, lane)], req.arrival_min, w_start)
                slot = ScheduledProtocolSlot(
                    protocol_id=req.protocol_id,
                    lane_idx=lane,
                    start_min=cand_start,
                    dur_min=req.dur_min,
                    is_frontier_exception=False,
                    human_cosigned=req.human_cosigned,
                    lean_certified=True,
                )
                scheduled.append(slot)
                cursors[(d, lane)] = slot.end_min
                placed = True
                break
        if not placed:
            deferred.append(req.protocol_id)

    scheduled.sort(key=lambda s: (s.start_min, s.lane_idx))
    day_u, night_u, all_u = _compute_utilizations(scheduled, num_lanes, horizon_min)
    err = verify_dual_regime_schedule(scheduled, num_lanes, horizon_min)
    return DualRegimePackerResult(
        policy="dual_regime_night_packer",
        scheduled_slots=tuple(scheduled),
        quarantined_ids=tuple(quarantined),
        deferred_ids=tuple(deferred),
        completed_protocols=len(scheduled),
        frontier_completed=sum(1 for s in scheduled if s.is_frontier_exception),
        certified_completed=sum(1 for s in scheduled if not s.is_frontier_exception),
        daytime_utilization_pct=day_u,
        overnight_utilization_pct=night_u,
        overall_utilization_pct=all_u,
        verification_error=err,
    )


def _generate_72h_workload(seed: int = 2026) -> list[ProtocolRequest]:
    """Generate a realistic 72h mixed workload of frontier, Lean-certified, and unsafe requests."""
    rng = random.Random(seed)
    requests: list[ProtocolRequest] = []

    # 24 human-cosigned frontier exploration protocols arriving during daytime across 3 days
    for i in range(24):
        day = i % 3
        arrival = day * MINUTES_PER_DAY + rng.randint(0, 210)
        dur = rng.choice([75, 90, 105, 120])
        requests.append(
            ProtocolRequest(
                protocol_id=f"FRONTIER-{i + 1:03d}",
                arrival_min=arrival,
                dur_min=dur,
                is_frontier_exception=True,
                human_cosigned=True,
                lean_certified=False,
                priority_weight=round(rng.uniform(1.5, 3.0), 2),
            )
        )

    # 66 zero-click Lean-certified standard protocols arriving continuously across 72h
    for i in range(66):
        day = i % 3
        arrival = day * MINUTES_PER_DAY + rng.randint(0, 720)
        dur = rng.choice([90, 110, 120, 140, 160])
        requests.append(
            ProtocolRequest(
                protocol_id=f"CERT-{i + 1:03d}",
                arrival_min=arrival,
                dur_min=dur,
                is_frontier_exception=False,
                human_cosigned=False,
                lean_certified=True,
                priority_weight=round(rng.uniform(1.0, 2.0), 2),
            )
        )

    # 6 unsafe requests (unsigned frontier overrides or uncertified standard runs) -> must be blocked
    for i in range(6):
        is_front = i % 2 == 0
        requests.append(
            ProtocolRequest(
                protocol_id=f"UNSAFE-{i + 1:03d}",
                arrival_min=(i % 3) * MINUTES_PER_DAY + 60,
                dur_min=90,
                is_frontier_exception=is_front,
                human_cosigned=False,
                lean_certified=False,
                priority_weight=2.0,
            )
        )

    return requests


def run_benchmark(horizon_hours: int = 72, seed: int = 2026) -> dict[str, Any]:
    """Compare fifo_human_gated vs dual_regime_night_packer over a 72h mixed workload."""
    workload = _generate_72h_workload(seed=seed)
    fifo_res = pack_fifo_human_gated_72h(workload, num_lanes=3, horizon_hours=horizon_hours)
    dual_res = pack_dual_regime_72h(workload, num_lanes=3, horizon_hours=horizon_hours)

    speedup = (
        round(dual_res.completed_protocols / fifo_res.completed_protocols, 3)
        if fifo_res.completed_protocols > 0
        else 0.0
    )

    return {
        "benchmark": "idea_08_night_shift_batcher",
        "horizon_hours": horizon_hours,
        "num_workcell_lanes": 3,
        "total_submitted_protocols": len(workload),
        "quarantined_unsafe_protocols": len(dual_res.quarantined_ids),
        "fifo_human_gated": {
            "completed_protocols_72h": fifo_res.completed_protocols,
            "frontier_protocols_completed": fifo_res.frontier_completed,
            "certified_protocols_completed": fifo_res.certified_completed,
            "daytime_machine_utilization_pct": fifo_res.daytime_utilization_pct,
            "overnight_machine_utilization_pct": fifo_res.overnight_utilization_pct,
            "overall_machine_utilization_pct": fifo_res.overall_utilization_pct,
            "schedule_valid": fifo_res.verification_error is None,
        },
        "dual_regime_night_packer": {
            "completed_protocols_72h": dual_res.completed_protocols,
            "frontier_protocols_completed": dual_res.frontier_completed,
            "certified_protocols_completed": dual_res.certified_completed,
            "daytime_machine_utilization_pct": dual_res.daytime_utilization_pct,
            "overnight_machine_utilization_pct": dual_res.overnight_utilization_pct,
            "overall_machine_utilization_pct": dual_res.overall_utilization_pct,
            "schedule_valid": dual_res.verification_error is None,
            "throughput_multiplier_vs_fifo": speedup,
        },
    }


if __name__ == "__main__":
    import json

    print(json.dumps(run_benchmark(), indent=2))
