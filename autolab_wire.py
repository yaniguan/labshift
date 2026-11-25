"""Auto-Lab 15-repository wire adapter (autolab-wire/v1) for labshift (Layer L5)."""

from __future__ import annotations

import copy
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, NamedTuple

REPO_ID = "labshift"
LAYER = "L5"
SCHEMA_VERSION = "autolab-wire/v1"

REPO_DIR = Path(__file__).resolve().parent
_SRC_DIR = REPO_DIR / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from labshift.checker import CheckResult, check_schedule  # noqa: E402
from labshift.greedy import (  # noqa: E402
    _compute_job_lct_starts,
    _coupled_segment_end,
    _has_infeasible_capacity,
    _place_coupled_segment,
    _PoolTracker,
    solve_greedy,
    solve_greedy_repair,
)
from labshift.metrics import compute_metrics  # noqa: E402
from labshift.models import Instance, Job, Operation, Schedule, ScheduledOp  # noqa: E402

PRIMARY_JOB_ID = "autolab_primary"
BACKGROUND_JOB_ID = "bg_contended_01"
ORTHOGONAL_JOB_ID = "autolab_orthogonal"


class _OrthogonalDirective(NamedTuple):
    """Normalized mid-campaign urgent orthogonal analytical assay specification."""

    modality: str
    instrument: str
    read_op_name: str
    duration_min: int
    max_wait_min: int
    prep_duration_min: int = 1


def _normalize_orthogonal_modality(raw_modality: str) -> tuple[str, str, str, int]:
    """Map orthogonal assay label to (canonical_label, instrument, op_name, default_dur_min)."""
    token = raw_modality.strip().lower().replace("-", "_").replace(" ", "_")
    if token in ("raman", "raman_read", "raman_spectroscopy", "raman_spectrometer", "raman_scan"):
        return ("Raman", "raman_spectrometer", "raman_read", 6)
    if token in ("lc_ms", "lcms", "mass_spec", "lc_ms_read", "ms"):
        return ("LC-MS", "lc_ms", "lc_ms_read", 8)
    clean_inst = token or "raman_spectrometer"
    return (raw_modality.strip() or "Raman", clean_inst, f"{clean_inst}_read", 6)


def _extract_orthogonal_directive(
    packet: Mapping[str, Any],
    default_max_wait: int,
) -> _OrthogonalDirective | None:
    """Detect urgent orthogonal confirmation assay from packet or lab-attrib telemetry."""
    raw_urgent = packet.get("urgent_orthogonal_assay")
    if raw_urgent is None:
        raw_urgent = packet.get("orthogonal_request")

    attrib = packet.get("attribution")
    attrib_map = attrib if isinstance(attrib, Mapping) else {}
    attrib_action = str(attrib_map.get("recommended_action", "")).strip().upper()
    attrib_assay = attrib_map.get("orthogonal_assay", attrib_map.get("urgent_orthogonal_assay"))

    source_map: Mapping[str, Any] = {}
    modality_raw: str | None = None

    if isinstance(raw_urgent, str) and raw_urgent.strip():
        modality_raw = raw_urgent
        source_map = attrib_map
    elif isinstance(raw_urgent, Mapping) and raw_urgent:
        source_map = raw_urgent
        cand = raw_urgent.get(
            "assay",
            raw_urgent.get("orthogonal_assay", raw_urgent.get("modality", raw_urgent.get("instrument"))),
        )
        modality_raw = str(cand) if cand is not None else "Raman"
    elif attrib_action == "RUN_ORTHOGONAL_ASSAY" or (
        isinstance(attrib_assay, str) and attrib_assay.strip()
    ):
        source_map = attrib_map
        modality_raw = str(attrib_assay) if attrib_assay is not None else "Raman"

    if modality_raw is None:
        return None

    label, inst, op_name, default_dur = _normalize_orthogonal_modality(modality_raw)

    raw_dur = source_map.get(
        "orthogonal_duration_min",
        source_map.get("duration_min", packet.get("orthogonal_duration_min", default_dur)),
    )
    dur_min = max(1, int(round(float(raw_dur))))

    default_ortho_wait = min(max(0, default_max_wait), 5)
    raw_wait = source_map.get(
        "orthogonal_max_wait_min",
        source_map.get("max_wait_min", packet.get("orthogonal_max_wait_min", default_ortho_wait)),
    )
    ortho_wait = max(0, int(round(float(raw_wait))))

    raw_prep = source_map.get(
        "orthogonal_prep_duration_min",
        source_map.get("prep_duration_min", packet.get("orthogonal_prep_duration_min", 1)),
    )
    prep_dur = max(1, int(round(float(raw_prep))))

    return _OrthogonalDirective(
        modality=label,
        instrument=inst,
        read_op_name=op_name,
        duration_min=dur_min,
        max_wait_min=ortho_wait,
        prep_duration_min=prep_dur,
    )


def build_wire_instance(
    duration_min: int,
    max_wait_min: int,
    *,
    include_background_job: bool = True,
    deadline_min: int | None = None,
    capacities_override: Mapping[str, int] | None = None,
    orthogonal: _OrthogonalDirective | None = None,
) -> Instance:
    """Construct a real multi-instrument contended Instance for an autolab-wire/v1 packet."""
    safe_duration = max(1, int(duration_min))
    safe_max_wait = max(0, int(max_wait_min))

    primary_ops: list[Operation] = [
        Operation(
            name="liquid_prep",
            instrument="liquid_handler",
            duration=3,
            min_wait=0,
            max_wait=None,
        ),
        Operation(
            name="incubate",
            instrument="incubator",
            duration=safe_duration,
            min_wait=0,
            max_wait=safe_max_wait,
        ),
        Operation(
            name="plate_read",
            instrument="plate_reader",
            duration=4,
            min_wait=0,
            max_wait=safe_max_wait,
        ),
    ]
    if orthogonal is not None:
        primary_ops.extend(
            [
                Operation(
                    name="orthogonal_aliquot",
                    instrument="liquid_handler",
                    duration=orthogonal.prep_duration_min,
                    min_wait=0,
                    max_wait=orthogonal.max_wait_min,
                ),
                Operation(
                    name=orthogonal.read_op_name,
                    instrument=orthogonal.instrument,
                    duration=orthogonal.duration_min,
                    min_wait=0,
                    max_wait=orthogonal.max_wait_min,
                ),
            ]
        )

    primary_job = Job(
        job_id=PRIMARY_JOB_ID,
        release=0,
        deadline=deadline_min,
        ops=tuple(primary_ops),
    )

    jobs: list[Job] = [primary_job]
    if include_background_job:
        bg_max_wait = max(safe_max_wait, 10)
        background_job = Job(
            job_id=BACKGROUND_JOB_ID,
            release=0,
            deadline=None,
            ops=(
                Operation(
                    name="bg_liquid_prep",
                    instrument="liquid_handler",
                    duration=2,
                    min_wait=0,
                    max_wait=None,
                ),
                Operation(
                    name="bg_incubate",
                    instrument="incubator",
                    duration=safe_duration,
                    min_wait=0,
                    max_wait=bg_max_wait,
                ),
                Operation(
                    name="bg_plate_read",
                    instrument="plate_reader",
                    duration=5,
                    min_wait=0,
                    max_wait=bg_max_wait,
                ),
            ),
        )
        jobs.append(background_job)

    caps: dict[str, int] = {
        "liquid_handler": 1,
        "incubator": 2,
        "plate_reader": 1,
    }
    if orthogonal is not None:
        caps.setdefault(orthogonal.instrument, 1)
    if capacities_override is not None:
        for k, v in capacities_override.items():
            caps[str(k)] = int(v)

    return Instance(
        name="autolab_wire_l5",
        capacities=caps,
        jobs=tuple(jobs),
    )


def _build_rolling_horizon_instance(
    packet: Mapping[str, Any],
    *,
    current_time_min: int,
    safe_max_wait: int,
    deadline_min: int | None,
    orthogonal: _OrthogonalDirective | None,
    capacities_override: Mapping[str, int] | None,
) -> tuple[Instance, dict[tuple[str, int], tuple[int, int]]]:
    """Freeze in-flight ops (start < current_time_min), apply pre-incubation holds to unstarted
    background plates, and insert any urgent orthogonal assay within its perishability window."""
    prev_sched = packet.get("schedule")
    raw_ops = prev_sched.get("operations") if isinstance(prev_sched, Mapping) else None
    if not isinstance(raw_ops, Sequence) or isinstance(raw_ops, (str, bytes)) or not raw_ops:
        inst = build_wire_instance(
            duration_min=max(
                1,
                int(
                    round(
                        float(
                            (packet.get("parsed_spans") or {}).get("duration_min", 15)
                            if isinstance(packet.get("parsed_spans"), Mapping)
                            else 15
                        )
                    )
                ),
            ),
            max_wait_min=safe_max_wait,
            include_background_job=bool(packet.get("include_background_job", True)),
            deadline_min=deadline_min,
            capacities_override=capacities_override,
            orthogonal=orthogonal,
        )
        return inst, {}

    by_job_raw: dict[str, list[dict[str, Any]]] = {}
    job_order: list[str] = []
    for item in raw_ops:
        if not isinstance(item, Mapping):
            continue
        jid = str(item["job_id"])
        if jid not in by_job_raw:
            by_job_raw[jid] = []
            job_order.append(jid)
        by_job_raw[jid].append(dict(item))

    for jid in job_order:
        by_job_raw[jid].sort(key=lambda d: int(d["op_index"]))

    caps: dict[str, int] = {
        "liquid_handler": 1,
        "incubator": 2,
        "plate_reader": 1,
    }
    if orthogonal is not None:
        caps.setdefault(orthogonal.instrument, 1)
    for raw_list in by_job_raw.values():
        for op_d in raw_list:
            caps.setdefault(str(op_d["instrument"]), 1)
    if capacities_override is not None:
        for k, v in capacities_override.items():
            caps[str(k)] = int(v)

    fixed_ops: dict[tuple[str, int], tuple[int, int]] = {}
    jobs: list[Job] = []
    attached_orthogonal = False

    for jid in job_order:
        raw_list = by_job_raw[jid]
        is_primary = jid == PRIMARY_JOB_ID
        job_max_wait = safe_max_wait if is_primary else max(safe_max_wait, 10)

        first_start = int(raw_list[0]["start"])
        in_flight = first_start < current_time_min
        release = first_start if in_flight else max(current_time_min, first_start)

        ops_built: list[Operation] = []
        last_frozen_end: int | None = None

        for pos, op_d in enumerate(raw_list):
            s_val = int(op_d["start"])
            e_val = int(op_d["end"])
            dur = max(1, e_val - s_val)
            inst_name = str(op_d["instrument"])
            op_name = str(op_d.get("name") or inst_name)

            if s_val < current_time_min:
                fixed_ops[(jid, pos)] = (s_val, e_val)
                if pos == 0:
                    min_w = 0
                    max_w: int | None = None
                else:
                    prev_e = int(raw_list[pos - 1]["end"])
                    gap = max(0, s_val - prev_e)
                    min_w = gap
                    max_w = gap
                last_frozen_end = e_val
            else:
                if pos == 0:
                    min_w = 0
                    max_w = None
                elif pos - 1 in [k_idx for (k_jid, k_idx) in fixed_ops if k_jid == jid]:
                    assert last_frozen_end is not None
                    min_w = max(0, current_time_min - last_frozen_end)
                    max_w = max(min_w, job_max_wait)
                else:
                    min_w = 0
                    max_w = job_max_wait if inst_name in ("incubator", "plate_reader") else None

            ops_built.append(
                Operation(
                    name=op_name,
                    instrument=inst_name,
                    duration=dur,
                    min_wait=min_w,
                    max_wait=max_w,
                )
            )

        job_dl = deadline_min if is_primary else None

        # Attach urgent orthogonal confirmation assay to PRIMARY_JOB_ID when primary is present
        if is_primary and orthogonal is not None:
            attached_orthogonal = True
            n_prev = len(ops_built)
            all_prev_frozen = (jid, n_prev - 1) in fixed_ops
            if all_prev_frozen and last_frozen_end is not None:
                min_aliq_wait = max(0, current_time_min - last_frozen_end)
                # Ensure orthogonal read starts inside [current_time_min, current_time_min + max_wait_min]
                # whenever physically reachable after the frozen step completes.
                target_latest_read_start = current_time_min + orthogonal.max_wait_min
                if last_frozen_end + orthogonal.prep_duration_min <= target_latest_read_start:
                    aliq_max_wait = max(
                        min_aliq_wait,
                        target_latest_read_start - orthogonal.prep_duration_min - last_frozen_end,
                    )
                    read_max_wait = max(
                        0,
                        target_latest_read_start
                        - (last_frozen_end + min_aliq_wait + orthogonal.prep_duration_min),
                    )
                    ortho_dl_cap = target_latest_read_start + orthogonal.duration_min
                    if job_dl is None or ortho_dl_cap < job_dl:
                        job_dl = ortho_dl_cap if deadline_min is not None else job_dl
                else:
                    aliq_max_wait = max(min_aliq_wait, orthogonal.max_wait_min)
                    read_max_wait = orthogonal.max_wait_min
            else:
                min_aliq_wait = 0
                aliq_max_wait = orthogonal.max_wait_min
                read_max_wait = orthogonal.max_wait_min

            ops_built.extend(
                [
                    Operation(
                        name="orthogonal_aliquot",
                        instrument="liquid_handler",
                        duration=orthogonal.prep_duration_min,
                        min_wait=min_aliq_wait,
                        max_wait=aliq_max_wait,
                    ),
                    Operation(
                        name=orthogonal.read_op_name,
                        instrument=orthogonal.instrument,
                        duration=orthogonal.duration_min,
                        min_wait=0,
                        max_wait=read_max_wait,
                    ),
                ]
            )

        if job_dl is not None and job_dl < release:
            # Keep Job construction valid so solver/checker cleanly diagnoses deadline infeasibility
            pass

        jobs.append(
            Job(
                job_id=jid,
                release=release,
                deadline=(
                    job_dl
                    if (job_dl is None or job_dl >= release)
                    else release
                ),
                ops=tuple(ops_built),
            )
        )

    if orthogonal is not None and not attached_orthogonal:
        ortho_id = ORTHOGONAL_JOB_ID
        suffix = 2
        existing_ids = {j.job_id for j in jobs}
        while ortho_id in existing_ids:
            ortho_id = f"{ORTHOGONAL_JOB_ID}_{suffix:02d}"
            suffix += 1
        max_read_start = current_time_min + max(
            orthogonal.prep_duration_min, orthogonal.max_wait_min
        )
        jobs.append(
            Job(
                job_id=ortho_id,
                release=current_time_min,
                deadline=max_read_start + orthogonal.duration_min,
                ops=(
                    Operation(
                        name="orthogonal_aliquot",
                        instrument="liquid_handler",
                        duration=orthogonal.prep_duration_min,
                        min_wait=0,
                        max_wait=None,
                    ),
                    Operation(
                        name=orthogonal.read_op_name,
                        instrument=orthogonal.instrument,
                        duration=orthogonal.duration_min,
                        min_wait=0,
                        max_wait=orthogonal.max_wait_min,
                    ),
                ),
            )
        )

    return (
        Instance(
            name="autolab_wire_l5_rolling",
            capacities=caps,
            jobs=tuple(jobs),
        ),
        fixed_ops,
    )


def _solve_rolling_stdlib(
    instance: Instance,
    fixed_ops: Mapping[tuple[str, int], tuple[int, int]],
) -> Schedule:
    """Pure-stdlib rolling-horizon solver freezing in-flight ops and holding unstarted plates."""
    if _has_infeasible_capacity(instance):
        return Schedule(
            instance_name=instance.name,
            solver="greedy_repair",
            ops=(),
            status="INFEASIBLE",
        )

    trackers: dict[str, _PoolTracker] = {
        inst: _PoolTracker(instance, inst) for inst in instance.capacities
    }
    scheduled: list[ScheduledOp] = []
    next_op_idx: dict[str, int] = {}
    ready_time: dict[str, int] = {}
    prev_placed: dict[str, tuple[int, int, int | None, str | None]] = {}
    job_map = {j.job_id: j for j in instance.jobs}
    job_lcts = {j.job_id: _compute_job_lct_starts(instance, j) for j in instance.jobs}

    for job in instance.jobs:
        idx = 0
        while idx < len(job.ops) and (job.job_id, idx) in fixed_ops:
            fix_s, fix_e = fixed_ops[(job.job_id, idx)]
            op = job.ops[idx]
            tag = instance.effective_setup_tag(job, op)
            trackers[op.instrument].push_interval(fix_s, fix_e, None, tag)
            scheduled.append(
                ScheduledOp(
                    job_id=job.job_id,
                    op_index=idx,
                    name=op.name,
                    instrument=op.instrument,
                    start=fix_s,
                    end=fix_e,
                )
            )
            prev_placed[job.job_id] = (fix_s, fix_e, None, tag)
            idx += 1

        next_op_idx[job.job_id] = idx
        if idx == 0:
            ready_time[job.job_id] = job.release
        elif idx < len(job.ops):
            last_e = prev_placed[job.job_id][1]
            next_op = job.ops[idx]
            eff_t = instance.effective_transfer_time(job.ops[idx - 1].instrument, next_op)
            ready_time[job.job_id] = last_e + max(next_op.min_wait, eff_t)
        else:
            ready_time[job.job_id] = 0

    active_jobs = [j.job_id for j in instance.jobs if next_op_idx[j.job_id] < len(j.ops)]

    while active_jobs:
        # Prioritize in-flight/urgent jobs (with frozen prefix or tight max_wait) ahead of
        # unstarted background jobs (pre-incubation admission hold).
        def _rolling_priority(jid: str) -> tuple[int, int, float, int, str]:
            job = job_map[jid]
            a = next_op_idx[jid]
            b = _coupled_segment_end(instance, job, a)
            has_frozen_prefix = 0 if a > 0 else 1
            is_urgent = 0 if jid in (PRIMARY_JOB_ID, ORTHOGONAL_JOB_ID) else 1
            dl = float(job.deadline) if job.deadline is not None else float("inf")
            max_w_a = job.ops[a].max_wait if job.ops[a].max_wait is not None else 10**6
            _ = b
            return (has_frozen_prefix, is_urgent, dl, max_w_a, jid)

        chosen_id = min(active_jobs, key=_rolling_priority)
        job = job_map[chosen_id]
        a = next_op_idx[chosen_id]
        b = _coupled_segment_end(instance, job, a)

        placed = _place_coupled_segment(
            trackers,
            instance,
            job,
            a,
            b,
            ready_time[chosen_id],
            prev_placed.get(chosen_id),
            job_lcts[chosen_id],
        )
        for k, s_k, e_k, u_k in placed:
            op_k = job.ops[k]
            scheduled.append(
                ScheduledOp(
                    job_id=chosen_id,
                    op_index=k,
                    name=op_k.name,
                    instrument=op_k.instrument,
                    start=s_k,
                    end=e_k,
                    unit_index=(
                        u_k
                        if (u_k is not None and instance.capacities.get(op_k.instrument, 0) > 1)
                        else None
                    ),
                )
            )
        last_k, last_s, last_e, last_u = placed[-1]
        prev_placed[chosen_id] = (
            last_s,
            last_e,
            last_u,
            instance.effective_setup_tag(job, job.ops[last_k]),
        )
        nxt = b + 1
        next_op_idx[chosen_id] = nxt
        if nxt < len(job.ops):
            next_op = job.ops[nxt]
            eff_t = instance.effective_transfer_time(job.ops[b].instrument, next_op)
            ready_time[chosen_id] = last_e + max(next_op.min_wait, eff_t)
        else:
            active_jobs.remove(chosen_id)

    return Schedule(
        instance_name=instance.name,
        solver="greedy_repair",
        ops=tuple(scheduled),
        status="OK",
    )


def solve_wire_instance(
    instance: Instance,
    *,
    solver_preference: str = "auto",
    time_limit_s: float = 2.0,
    fixed_ops: Mapping[tuple[str, int], tuple[int, int]] | None = None,
) -> Schedule:
    """Solve instance with CP-SAT when installed, falling back to window-aware repair or greedy."""
    if fixed_ops:
        if solver_preference == "greedy":
            return _solve_rolling_stdlib(instance, fixed_ops)
        if solver_preference == "greedy_repair":
            return _solve_rolling_stdlib(instance, fixed_ops)
        if solver_preference in ("auto", "cpsat"):
            try:
                from labshift.cpsat import solve_cpsat
            except ImportError:
                if solver_preference == "cpsat":
                    raise
                return _solve_rolling_stdlib(instance, fixed_ops)
            cp_sched = solve_cpsat(
                instance,
                time_limit=max(0.1, float(time_limit_s)),
                num_workers=1,
                seed=0,
                tie_break_flow=True,
                fixed_ops=dict(fixed_ops),
            )
            if cp_sched.status == "INFEASIBLE" and not _has_infeasible_capacity(instance):
                # Emit diagnostic rolling attempt if window/deadline was violated so check_schedule
                # records exact violation kind counts.
                diag = _solve_rolling_stdlib(instance, fixed_ops)
                diag_chk = check_schedule(instance, diag)
                if not diag_chk.feasible:
                    return Schedule(
                        instance_name=instance.name,
                        solver="cpsat",
                        ops=diag.ops,
                        status="INFEASIBLE",
                        solve_time_s=cp_sched.solve_time_s,
                    )
            return cp_sched

    if solver_preference == "greedy":
        return solve_greedy(instance)
    if solver_preference == "greedy_repair":
        return solve_greedy_repair(instance)
    if solver_preference in ("auto", "cpsat"):
        try:
            from labshift.cpsat import solve_cpsat
        except ImportError:
            if solver_preference == "cpsat":
                raise
            schedule = solve_greedy_repair(instance)
            if not schedule.ops and instance.total_operations > 0:
                return solve_greedy(instance)
            return schedule
        return solve_cpsat(
            instance,
            time_limit=max(0.1, float(time_limit_s)),
            num_workers=1,
            seed=0,
            tie_break_flow=True,
        )
    raise ValueError(
        f"Unsupported solver_preference {solver_preference!r}; "
        "expected 'auto', 'cpsat', 'greedy_repair', or 'greedy'."
    )


def _classify_halt_reason(
    instance: Instance,
    schedule: Schedule,
    check: CheckResult,
    violations_count: int,
    raw_deadline: int | None,
) -> str:
    """Classify L5 halt reason accurately into WINDOW, CAPACITY, or DEADLINE blocks."""
    if violations_count > 0:
        return "L5_SCHEDULE_WINDOW_BLOCK"

    counts = check.counts_by_kind()
    if (
        _has_infeasible_capacity(instance)
        or counts.get("capacity", 0) > 0
        or counts.get("unknown_instrument", 0) > 0
        or counts.get("setup_time", 0) > 0
    ):
        return "L5_SCHEDULE_CAPACITY_BLOCK"

    if counts.get("deadline", 0) > 0:
        return "L5_SCHEDULE_DEADLINE_BLOCK"

    if raw_deadline is not None or any(j.deadline is not None for j in instance.jobs):
        # Check whether relaxing hard deadlines restores window/capacity feasibility.
        relaxed_jobs = tuple(
            Job(
                job_id=j.job_id,
                ops=j.ops,
                release=j.release,
                deadline=None,
                assay_tag=j.assay_tag,
            )
            for j in instance.jobs
        )
        relaxed_inst = Instance(
            name=instance.name,
            capacities=dict(instance.capacities),
            jobs=relaxed_jobs,
            transit_matrix=instance.transit_matrix,
            setup_times=instance.setup_times,
            blocking_handoff=instance.blocking_handoff,
        )
        rel_sched = solve_greedy_repair(relaxed_inst)
        if check_schedule(relaxed_inst, rel_sched).feasible:
            return "L5_SCHEDULE_DEADLINE_BLOCK"

    _ = schedule
    return "L5_SCHEDULE_WINDOW_BLOCK"


def _apply_forced_window_violation(
    instance: Instance,
    base_schedule: Schedule,
    forced: Any,
    safe_max_wait: int,
) -> Schedule:
    """Build or deserialize a candidate schedule to test independent verifier rejection."""
    if isinstance(forced, Schedule):
        return forced

    if isinstance(forced, Mapping):
        sched_dict = {
            "instance_name": str(forced.get("instance_name", instance.name)),
            "solver": str(forced.get("solver", "forced_override")),
            "status": str(forced.get("status", "OK")),
            "solve_time_s": float(forced.get("solve_time_s", 0.0)),
            "best_bound": forced.get("best_bound"),
            "ops": list(forced["ops"]),
        }
        return Schedule.from_dict(sched_dict)

    if isinstance(forced, Sequence) and not isinstance(forced, (str, bytes)):
        ops = tuple(
            item if isinstance(item, ScheduledOp) else ScheduledOp.from_dict(dict(item))
            for item in forced
        )
        return Schedule(
            instance_name=instance.name,
            solver="forced_override",
            ops=ops,
            status="OK",
            solve_time_s=0.0,
        )

    extra_delay = (
        int(forced)
        if (isinstance(forced, int) and not isinstance(forced, bool) and forced > 0)
        else 10
    )

    source_schedule = base_schedule if base_schedule.ops else solve_greedy_repair(instance)
    by_key = source_schedule.by_key()
    inc_op = by_key.get((PRIMARY_JOB_ID, 1))
    read_op = by_key.get((PRIMARY_JOB_ID, 2))
    if inc_op is None or read_op is None:
        return source_schedule

    horizon_end = max((op.end for op in source_schedule.ops), default=inc_op.end)
    read_duration = read_op.end - read_op.start
    late_start = max(inc_op.end, horizon_end) + safe_max_wait + extra_delay
    mutated_ops = tuple(
        ScheduledOp(
            job_id=PRIMARY_JOB_ID,
            op_index=2,
            name=read_op.name,
            instrument=read_op.instrument,
            start=late_start,
            end=late_start + read_duration,
        )
        if (op.job_id == PRIMARY_JOB_ID and op.op_index == 2)
        else op
        for op in source_schedule.ops
    )
    return Schedule(
        instance_name=instance.name,
        solver="forced_window_violation",
        ops=mutated_ops,
        status="OK",
        solve_time_s=source_schedule.solve_time_s,
    )


def process_packet(packet: dict[str, Any]) -> dict[str, Any]:
    """Execute the L5 multi-instrument window scheduling and verification stage."""
    out: dict[str, Any] = copy.deepcopy(packet)
    out.setdefault("schema_version", SCHEMA_VERSION)
    out.setdefault("halted", False)
    out.setdefault("halt_reason", None)
    if out.get("trace") is None:
        out["trace"] = []
    if out.get("bottom_reasoning_traces") is None:
        out["bottom_reasoning_traces"] = []

    if out["halted"] is True:
        return out

    parsed_spans = out.get("parsed_spans")
    spans_map = parsed_spans if isinstance(parsed_spans, Mapping) else {}
    duration_min = max(1, int(round(float(spans_map.get("duration_min", 15)))))
    max_wait_min = int(out.get("max_wait_min", 15))
    negative_window = max_wait_min < 0
    safe_max_wait = max(0, max_wait_min)

    include_bg = bool(out.get("include_background_job", True))
    raw_deadline = out.get("deadline_min")
    deadline_min = int(raw_deadline) if raw_deadline is not None else None
    negative_deadline = deadline_min is not None and deadline_min < 0

    raw_caps = out.get("capacities")
    caps_override = (
        {str(k): int(v) for k, v in raw_caps.items()}
        if isinstance(raw_caps, Mapping)
        else None
    )

    raw_now = out.get("current_time_min")
    current_time_min = max(0, int(round(float(raw_now)))) if raw_now is not None else 0
    orthogonal = _extract_orthogonal_directive(out, safe_max_wait)

    prev_sched = out.get("schedule")
    has_active_ops = (
        isinstance(prev_sched, Mapping)
        and isinstance(prev_sched.get("operations"), Sequence)
        and not isinstance(prev_sched.get("operations"), (str, bytes))
        and len(prev_sched["operations"]) > 0
    )
    use_rolling = has_active_ops and (current_time_min > 0 or orthogonal is not None)

    fixed_ops: dict[tuple[str, int], tuple[int, int]] = {}
    if use_rolling:
        instance, fixed_ops = _build_rolling_horizon_instance(
            out,
            current_time_min=current_time_min,
            safe_max_wait=safe_max_wait,
            deadline_min=(deadline_min if not negative_deadline else 0),
            orthogonal=orthogonal,
            capacities_override=caps_override,
        )
    else:
        instance = build_wire_instance(
            duration_min=duration_min,
            max_wait_min=safe_max_wait,
            include_background_job=include_bg,
            deadline_min=(deadline_min if not negative_deadline else 0),
            capacities_override=caps_override,
            orthogonal=orthogonal,
        )

    solver_pref = str(out.get("solver", "auto"))
    time_limit_s = float(out.get("time_limit_s", 2.0))
    schedule = solve_wire_instance(
        instance,
        solver_preference=solver_pref,
        time_limit_s=time_limit_s,
        fixed_ops=fixed_ops if fixed_ops else None,
    )

    forced = out.get("force_window_violation_schedule")
    if forced is not None and forced is not False:
        schedule = _apply_forced_window_violation(instance, schedule, forced, safe_max_wait)

    check = check_schedule(instance, schedule)
    metrics = compute_metrics(instance, schedule, check=check)

    violations_count = int(metrics.window_violations) + (1 if negative_window else 0)
    feasible = bool(
        check.feasible
        and violations_count == 0
        and not negative_deadline
        and (
            deadline_min is None
            or all(
                sop.end <= deadline_min
                for sop in schedule.ops
                if sop.job_id == PRIMARY_JOB_ID
            )
        )
    )
    makespan_min = (
        int(metrics.makespan)
        if metrics.makespan is not None
        else max((op.end for op in schedule.ops), default=0)
    )
    ordered_ops = sorted(
        schedule.ops,
        key=lambda op: (op.start, op.end, op.job_id, op.op_index),
    )
    ops_summary = [op.to_dict() for op in ordered_ops]

    if not feasible or violations_count > 0:
        out["halted"] = True
        if negative_deadline:
            out["halt_reason"] = "L5_SCHEDULE_DEADLINE_BLOCK"
        else:
            out["halt_reason"] = _classify_halt_reason(
                instance, schedule, check, violations_count, deadline_min
            )

    out["schedule"] = {
        "feasible": feasible,
        "makespan_min": makespan_min,
        "window_violations": violations_count,
        "operations": ops_summary,
    }

    if use_rolling or orthogonal is not None:
        out["rolling_reschedule"] = {
            "current_time_min": current_time_min,
            "frozen_operations": len(fixed_ops),
            "orthogonal_modality": orthogonal.modality if orthogonal is not None else None,
            "orthogonal_instrument": orthogonal.instrument if orthogonal is not None else None,
        }

    status = "BLOCK" if out["halted"] else "PASS"
    if out["halted"]:
        detail = (
            check.violations[0].message
            if check.violations
            else (
                f"deadline_min={deadline_min} unsatisfied"
                if out["halt_reason"] == "L5_SCHEDULE_DEADLINE_BLOCK"
                else f"max_wait_min={max_wait_min} unsatisfied"
            )
        )
        summary = (
            f"L5 schedule verification blocked ({schedule.solver}): "
            f"reason={out['halt_reason']}, feasible={feasible}, "
            f"window_violations={violations_count} ({detail})"
        )
    elif orthogonal is not None:
        summary = (
            f"Rolling-horizon rescheduled {len(instance.jobs)}-job workflow via {schedule.solver} "
            f"at t={current_time_min} min (orthogonal={orthogonal.modality} on "
            f"{orthogonal.instrument}): makespan={makespan_min} min, window_violations=0."
        )
    else:
        summary = (
            f"Scheduled {len(instance.jobs)}-job workflow via {schedule.solver}: "
            f"makespan={makespan_min} min, incubate={duration_min} min, "
            f"max_wait={max_wait_min} min, window_violations=0."
        )

    out["trace"].append(
        {
            "repo": REPO_ID,
            "layer": LAYER,
            "status": status,
            "summary": summary,
        }
    )
    out["bottom_reasoning_traces"].append(
        {
            "repo": REPO_ID,
            "layer": LAYER,
            "invariant": "missed_incubation_window_rejected",
            "first_principles": (
                "Biological post-incubation [min_wait, max_wait] windows and half-open "
                "[start, end) shared instrument capacities are solved in exact integer "
                "minutes and independently audited by sweep-line verification so reader "
                "contention never permits decayed or over-incubated plate reads."
            ),
        }
    )
    return out
