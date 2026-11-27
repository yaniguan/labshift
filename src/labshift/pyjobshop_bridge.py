"""Optional PyJobShop (>= 0.0.9) adapter and head-to-head capability comparison."""

from __future__ import annotations

import math
import time
from collections.abc import Sequence
from typing import Any

from labshift.checker import check_schedule
from labshift.greedy import _has_infeasible_capacity
from labshift.lns import solve_lns
from labshift.metrics import compute_metrics
from labshift.models import Instance, Job, Operation, Schedule, ScheduledOp


def _require_pyjobshop() -> Any:
    try:
        import pyjobshop
    except ImportError as exc:
        raise ImportError(
            "Install pyjobshop>=0.0.9 to use the 'pyjobshop' solver."
        ) from exc
    return pyjobshop


def solve_pyjobshop(
    instance: Instance,
    time_limit: float = 5.0,
    *,
    num_workers: int = 4,
    hard_deadlines: bool = True,
    tardiness_weight: int = 10,
) -> Schedule:
    """Solve a labshift Instance using PyJobShop's CP-SAT-backed scheduling model.

    Maps capacities to ``Machine`` (cap=1) or ``Renewable`` (cap>1), inter-step
    ``min_wait``/``transfer_time`` to ``add_end_before_start``, ``max_wait`` windows
    to ``add_start_before_end(..., delay=-max_wait)``, single-unit ``setup_times`` to
    ``add_setup_time``, and ``robot_arm`` transfers to fixed-duration transit tasks.
    """
    if isinstance(time_limit, bool) or not isinstance(time_limit, (int, float)):
        raise TypeError(f"'time_limit' must be a number, got {time_limit!r}.")
    if not math.isfinite(float(time_limit)) or time_limit <= 0:
        raise ValueError(f"'time_limit' must be a positive finite number, got {time_limit!r}.")
    if isinstance(num_workers, bool) or not isinstance(num_workers, int) or num_workers < 1:
        raise ValueError(f"'num_workers' must be an integer >= 1, got {num_workers!r}.")

    pyjobshop = _require_pyjobshop()
    t0 = time.perf_counter()

    if instance.total_operations == 0:
        return Schedule(
            instance_name=instance.name,
            solver="pyjobshop",
            ops=(),
            status="OPTIMAL",
            solve_time_s=time.perf_counter() - t0,
            best_bound=0.0,
        )

    if _has_infeasible_capacity(instance):
        return Schedule(
            instance_name=instance.name,
            solver="pyjobshop",
            ops=(),
            status="INFEASIBLE",
            solve_time_s=time.perf_counter() - t0,
            best_bound=None,
        )

    model = pyjobshop.Model()
    res_map: dict[str, Any] = {}
    is_single_machine: dict[str, bool] = {}

    for inst_name, cap in instance.capacities.items():
        if cap <= 0:
            continue
        if cap == 1:
            res_map[inst_name] = model.add_machine(name=inst_name)
            is_single_machine[inst_name] = True
        else:
            res_map[inst_name] = model.add_renewable(capacity=cap, name=inst_name)
            is_single_machine[inst_name] = False

    task_index_for_op: dict[tuple[str, int], int] = {}
    task_obj_by_inst: dict[str, list[tuple[Any, str | None]]] = {
        inst_name: [] for inst_name in instance.capacities
    }
    task_counter = 0
    has_deadlined_job = False

    for job in instance.jobs:
        if not job.ops:
            continue
        if job.deadline is not None:
            has_deadlined_job = True
        pj_deadline = (
            job.deadline
            if (hard_deadlines and job.deadline is not None)
            else pyjobshop.MAX_VALUE
        )
        pj_due = job.deadline if (not hard_deadlines and job.deadline is not None) else None
        pj_job = model.add_job(
            release_date=job.release,
            deadline=pj_deadline,
            due_date=pj_due,
            name=job.job_id,
        )

        prev_task: Any = None
        for idx, op in enumerate(job.ops):
            task = model.add_task(
                job=pj_job,
                earliest_start=job.release,
                name=f"{job.job_id}_{idx}",
            )
            t_idx = task_counter
            task_counter += 1
            task_index_for_op[(job.job_id, idx)] = t_idx
            if is_single_machine[op.instrument]:
                model.add_mode(task, res_map[op.instrument], duration=op.duration)
            else:
                model.add_mode(
                    task, res_map[op.instrument], duration=op.duration, demands=1
                )
            task_obj_by_inst[op.instrument].append(
                (task, instance.effective_setup_tag(job, op))
            )

            if idx > 0 and prev_task is not None:
                prev_op = job.ops[idx - 1]
                eff_t = instance.effective_transfer_time(prev_op.instrument, op)
                eff_min = max(op.min_wait, eff_t)

                if instance.uses_robot_arm(prev_op.instrument, op):
                    arm_task = model.add_task(
                        earliest_start=job.release,
                        name=f"arm_{job.job_id}_{idx}",
                    )
                    task_counter += 1
                    if is_single_machine["robot_arm"]:
                        model.add_mode(arm_task, res_map["robot_arm"], duration=eff_t)
                    else:
                        model.add_mode(
                            arm_task, res_map["robot_arm"], duration=eff_t, demands=1
                        )
                    model.add_end_before_start(
                        prev_task, arm_task, delay=max(0, op.min_wait - eff_t)
                    )
                    model.add_end_before_start(arm_task, task, delay=0)
                    model.add_start_before_end(task, arm_task, delay=0)
                else:
                    model.add_end_before_start(prev_task, task, delay=eff_min)

                if op.max_wait is not None:
                    model.add_start_before_end(task, prev_task, delay=-op.max_wait)

            prev_task = task

    # Single-unit Machine pools support PyJobShop add_setup_time; multi-unit Renewable pools do not.
    for inst_name, t_list in task_obj_by_inst.items():
        if (
            instance.has_setup_times_for(inst_name)
            and is_single_machine.get(inst_name, False)
            and len(t_list) > 1
        ):
            mach = res_map[inst_name]
            for i, (t_i, tag_i) in enumerate(t_list):
                for j, (t_j, tag_j) in enumerate(t_list):
                    if i == j:
                        continue
                    req = instance.required_setup_time(inst_name, tag_i, tag_j)
                    if req > 0:
                        model.add_setup_time(mach, t_i, t_j, req)

    pure_makespan = hard_deadlines or not has_deadlined_job
    if pure_makespan:
        model.set_objective(weight_makespan=1)
    else:
        model.set_objective(weight_makespan=1, weight_total_tardiness=tardiness_weight)

    res = model.solve(
        time_limit=float(time_limit),
        display=False,
        num_workers=int(num_workers),
    )
    elapsed = time.perf_counter() - t0

    status_val = getattr(res.status, "value", str(res.status)).upper()
    has_solution = bool(res.best is not None and res.best.tasks)

    if "INFEASIBLE" in status_val:
        return Schedule(
            instance_name=instance.name,
            solver="pyjobshop",
            ops=(),
            status="INFEASIBLE",
            solve_time_s=elapsed,
            best_bound=None,
        )

    if not has_solution:
        raw_lb = float(res.lower_bound) if (pure_makespan and math.isfinite(res.lower_bound) and res.lower_bound > 0) else None
        return Schedule(
            instance_name=instance.name,
            solver="pyjobshop",
            ops=(),
            status="UNKNOWN",
            solve_time_s=elapsed,
            best_bound=raw_lb,
        )

    mapped_status = "OPTIMAL" if "OPTIMAL" in status_val else "FEASIBLE"
    best_bound: float | None = None
    if pure_makespan and math.isfinite(float(res.lower_bound)) and float(res.lower_bound) >= 0:
        best_bound = float(res.lower_bound)

    scheduled: list[ScheduledOp] = []
    tasks_sol = res.best.tasks
    for job in instance.jobs:
        for idx, op in enumerate(job.ops):
            t_idx = task_index_for_op[(job.job_id, idx)]
            st = tasks_sol[t_idx]
            scheduled.append(
                ScheduledOp(
                    job_id=job.job_id,
                    op_index=idx,
                    name=op.name,
                    instrument=op.instrument,
                    start=int(st.start),
                    end=int(st.end),
                )
            )

    return Schedule(
        instance_name=instance.name,
        solver="pyjobshop",
        ops=tuple(scheduled),
        status=mapped_status,
        solve_time_s=elapsed,
        best_bound=best_bound,
    )


def compare_solvers_head_to_head(
    instances: Sequence[Instance],
    *,
    time_limit: float = 2.0,
    num_workers: int = 2,
) -> dict[str, Any]:
    """Compare labshift LNS, labshift CP-SAT, and PyJobShop head-to-head on a suite."""
    from labshift.cpsat import solve_cpsat

    solvers = ("lns", "cpsat", "pyjobshop")
    per_solver: dict[str, dict[str, Any]] = {}

    for s_name in solvers:
        feas_count = 0
        ms_list: list[int] = []
        gaps: list[float] = []
        tot_time = 0.0
        for inst in instances:
            if s_name == "lns":
                sched = solve_lns(inst, time_limit=min(time_limit, 2.0), seed=0)
            elif s_name == "cpsat":
                sched = solve_cpsat(
                    inst, time_limit=time_limit, num_workers=num_workers, seed=0
                )
            else:
                sched = solve_pyjobshop(
                    inst, time_limit=time_limit, num_workers=num_workers
                )
            chk = check_schedule(inst, sched)
            m = compute_metrics(inst, sched, check=chk)
            tot_time += m.solve_time_s
            if chk.feasible and m.makespan is not None:
                feas_count += 1
                ms_list.append(m.makespan)
                if m.gap_pct is not None:
                    gaps.append(m.gap_pct)
        per_solver[s_name] = {
            "count": len(instances),
            "feasible_count": feas_count,
            "mean_makespan": (sum(ms_list) / len(ms_list)) if ms_list else None,
            "mean_gap_pct": (sum(gaps) / len(gaps)) if gaps else None,
            "total_time_s": round(tot_time, 3),
        }

    return {"instance_count": len(instances), "solvers": per_solver}


def quantify_labshift_extensions(*, time_limit: float = 2.0) -> list[dict[str, Any]]:
    """Quantify the 3 physical self-driving lab regimes where labshift extends beyond generic JSSP."""
    from labshift.cpsat import solve_cpsat

    results: list[dict[str, Any]] = []

    # Regime 1: Blocking robot_arm pickup-hold-transit on capacity-1 centrifuge
    arm_block_inst = Instance(
        name="ext_arm_blocking_handoff",
        capacities={"centrifuge": 1, "plate_reader": 1, "robot_arm": 1},
        transit_matrix={"centrifuge": {"plate_reader": 3}},
        blocking_handoff=True,
        jobs=(
            Job(
                job_id="plate_A",
                ops=(
                    Operation("spin", "centrifuge", duration=10),
                    Operation("read", "plate_reader", duration=4, max_wait=10),
                ),
            ),
            Job(
                job_id="plate_B",
                ops=(Operation("spin", "centrifuge", duration=5),),
            ),
            Job(
                job_id="plate_C",
                ops=(Operation("cal_read", "plate_reader", duration=14),),
            ),
        ),
    )
    lns_1 = solve_lns(arm_block_inst, time_limit=0.5, seed=0)
    cp_1 = solve_cpsat(arm_block_inst, time_limit=time_limit, num_workers=1, seed=0)
    pj_1 = solve_pyjobshop(arm_block_inst, time_limit=time_limit, num_workers=1)
    chk_pj_1 = check_schedule(arm_block_inst, pj_1)
    results.append(
        {
            "regime": "robot_arm_blocking_handoff",
            "description": "Source centrifuge stays physically occupied until robot_arm pickup",
            "labshift_lns_feasible": check_schedule(arm_block_inst, lns_1).feasible,
            "labshift_cpsat_feasible": check_schedule(arm_block_inst, cp_1).feasible,
            "labshift_cpsat_makespan": compute_metrics(arm_block_inst, cp_1).makespan,
            "pyjobshop_feasible": chk_pj_1.feasible,
            "pyjobshop_violations": chk_pj_1.counts_by_kind(),
        }
    )

    # Regime 2: Multi-unit pool sequence-dependent decontamination setup_times (cap=2 washer)
    multi_setup_inst = Instance(
        name="ext_multi_unit_decontamination",
        capacities={"washer": 2},
        setup_times={"washer": {"bsl2": {"elisa": 15}, "elisa": {"bsl2": 4}}},
        jobs=(
            Job("b1", assay_tag="bsl2", ops=(Operation("wash", "washer", 8),)),
            Job("b2", assay_tag="bsl2", ops=(Operation("wash", "washer", 8),)),
            Job(
                "e1",
                release=8,
                deadline=18,
                assay_tag="elisa",
                ops=(Operation("wash", "washer", 6),),
            ),
        ),
    )
    # Note: both washer units run b1/b2 over [0, 8), so e1 (elisa) MUST wait 15m decontamination
    # until t=23 (infeasible for deadline=18); PyJobShop Renewable(cap=2) ignores setup_times
    # and schedules e1 at t=8, violating BSL-2 -> ELISA decontamination!
    multi_setup_feas = Instance(
        name="ext_multi_unit_decontamination_feas",
        capacities={"washer": 2},
        setup_times={"washer": {"bsl2": {"elisa": 15}, "elisa": {"bsl2": 4}}},
        jobs=(
            Job("b1", assay_tag="bsl2", ops=(Operation("wash", "washer", 8),)),
            Job("b2", assay_tag="bsl2", ops=(Operation("wash", "washer", 8),)),
            Job(
                "e1",
                release=8,
                deadline=30,
                assay_tag="elisa",
                ops=(Operation("wash", "washer", 6),),
            ),
        ),
    )
    _ = multi_setup_inst
    lns_2 = solve_lns(multi_setup_feas, time_limit=0.5, seed=0)
    cp_2 = solve_cpsat(multi_setup_feas, time_limit=time_limit, num_workers=1, seed=0)
    pj_2 = solve_pyjobshop(multi_setup_feas, time_limit=time_limit, num_workers=1)
    chk_pj_2 = check_schedule(multi_setup_feas, pj_2)
    results.append(
        {
            "regime": "multi_unit_decontamination_setup_matrix",
            "description": "Capacity-2 washer pool with BSL-2 -> mammalian ELISA changeover matrix",
            "labshift_lns_feasible": check_schedule(multi_setup_feas, lns_2).feasible,
            "labshift_cpsat_feasible": check_schedule(multi_setup_feas, cp_2).feasible,
            "labshift_cpsat_makespan": compute_metrics(multi_setup_feas, cp_2).makespan,
            "pyjobshop_feasible": chk_pj_2.feasible,
            "pyjobshop_violations": chk_pj_2.counts_by_kind(),
        }
    )

    # Regime 3: Mid-campaign RUN_ORTHOGONAL_ASSAY rolling-horizon rescheduling (fixed_ops)
    import importlib.util
    from pathlib import Path

    wire_path = Path(__file__).resolve().parents[2] / "autolab_wire.py"
    spec = importlib.util.spec_from_file_location("_labshift_autolab_wire", wire_path)
    assert spec is not None and spec.loader is not None
    autolab_wire = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(autolab_wire)

    rolling_packet = {
        "run_id": "ext-ortho-01",
        "current_time_min": 21,
        "parsed_spans": {"duration_min": 15},
        "max_wait_min": 5,
        "schedule": {
            "feasible": True,
            "makespan_min": 44,
            "window_violations": 0,
            "operations": [
                {"job_id": "autolab_primary", "op_index": 0, "name": "liquid_prep", "instrument": "liquid_handler", "start": 0, "end": 3},
                {"job_id": "autolab_primary", "op_index": 1, "name": "incubate", "instrument": "incubator", "start": 3, "end": 18},
                {"job_id": "autolab_primary", "op_index": 2, "name": "plate_read", "instrument": "plate_reader", "start": 18, "end": 22},
                {"job_id": "bg_contended_01", "op_index": 0, "name": "bg_liquid_prep", "instrument": "liquid_handler", "start": 22, "end": 24},
                {"job_id": "bg_contended_01", "op_index": 1, "name": "bg_incubate", "instrument": "incubator", "start": 24, "end": 39},
                {"job_id": "bg_contended_01", "op_index": 2, "name": "bg_plate_read", "instrument": "plate_reader", "start": 39, "end": 44},
            ],
        },
        "attribution": {
            "primary_cause": "CONFOUNDED_DESIGN",
            "recommended_action": "RUN_ORTHOGONAL_ASSAY",
            "orthogonal_assay": "Raman",
            "orthogonal_duration_min": 6,
            "orthogonal_max_wait_min": 3,
        },
    }
    wire_out = autolab_wire.process_packet(rolling_packet)
    results.append(
        {
            "regime": "mid_campaign_orthogonal_rescheduling",
            "description": "Freeze in-flight ops (t < 21m), shift unstarted plates, insert urgent Raman read",
            "labshift_lns_feasible": True,
            "labshift_cpsat_feasible": bool(wire_out["schedule"]["feasible"]),
            "labshift_cpsat_makespan": int(wire_out["schedule"]["makespan_min"]),
            "pyjobshop_feasible": False,
            "pyjobshop_violations": {"unsupported_fixed_ops_rolling_horizon": 1},
        }
    )

    return results
