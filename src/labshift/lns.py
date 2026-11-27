"""Pure-stdlib Iterated Greedy & Large Neighborhood Search (LNS) scheduler."""

from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass

from labshift.bounds import makespan_lower_bound
from labshift.checker import check_schedule
from labshift.greedy import (
    _compute_job_lct_starts,
    _coupled_segment_end,
    _has_infeasible_capacity,
    _place_coupled_segment,
    _PoolTracker,
    solve_greedy_repair,
)
from labshift.models import Instance, Job, Schedule, ScheduledOp


@dataclass
class _PrecomputedLNS:
    job_map: dict[str, Job]
    nonempty_ids: list[str]
    seg_ends: dict[tuple[str, int], int]
    job_lcts: dict[str, list[int] | None]
    chain_lens: dict[str, int]
    inst_sets: dict[str, frozenset[str]]


def _precompute(instance: Instance) -> _PrecomputedLNS:
    job_map = {j.job_id: j for j in instance.jobs}
    nonempty_ids = [j.job_id for j in instance.jobs if j.ops]
    seg_ends: dict[tuple[str, int], int] = {}
    job_lcts: dict[str, list[int] | None] = {}
    chain_lens: dict[str, int] = {}
    inst_sets: dict[str, frozenset[str]] = {}

    for job in instance.jobs:
        m = len(job.ops)
        if m == 0:
            continue
        chain = 0
        insts: set[str] = set()
        for k, op in enumerate(job.ops):
            insts.add(op.instrument)
            seg_ends[(job.job_id, k)] = _coupled_segment_end(instance, job, k)
            wait_k = (
                max(
                    op.min_wait,
                    instance.effective_transfer_time(job.ops[k - 1].instrument, op),
                )
                if k > 0
                else 0
            )
            chain += wait_k + op.duration
        chain_lens[job.job_id] = chain
        job_lcts[job.job_id] = _compute_job_lct_starts(instance, job)
        inst_sets[job.job_id] = frozenset(insts)

    return _PrecomputedLNS(
        job_map=job_map,
        nonempty_ids=nonempty_ids,
        seg_ends=seg_ends,
        job_lcts=job_lcts,
        chain_lens=chain_lens,
        inst_sets=inst_sets,
    )


def _decode_job_order(
    instance: Instance,
    job_order: list[str],
    pre: _PrecomputedLNS,
    *,
    allow_deadline_prune: bool = True,
) -> tuple[list[ScheduledOp], int, int, int, int, list[str]]:
    """Decode a job permutation into coupled-segment placements and fast violation stats.

    Returns ``(ops, win_viols, dl_viols, tardiness, makespan, violating_job_ids)``.
    """
    trackers: dict[str, _PoolTracker] = {
        inst: _PoolTracker(instance, inst) for inst in instance.capacities
    }
    scheduled: list[ScheduledOp] = []
    win_viols = 0
    dl_viols = 0
    tardiness = 0
    makespan = 0
    violating: list[str] = []

    for jid in job_order:
        job = pre.job_map[jid]
        m = len(job.ops)
        a = 0
        ready_a = job.release
        prev_placed: tuple[int, int, int | None, str | None] | None = None
        job_violated = False
        lcts = pre.job_lcts[jid]

        while a < m:
            b = pre.seg_ends[(jid, a)]
            placed = _place_coupled_segment(
                trackers,
                instance,
                job,
                a,
                b,
                ready_a,
                prev_placed,
                lcts,
                allow_deadline_prune=allow_deadline_prune,
            )
            prev_e_in_job = prev_placed[1] if prev_placed is not None else None
            for k, s_k, e_k, u_k in placed:
                op_k = job.ops[k]
                if k > 0 and prev_e_in_job is not None and op_k.max_wait is not None:
                    if s_k - prev_e_in_job > op_k.max_wait:
                        win_viols += 1
                        job_violated = True
                prev_e_in_job = e_k
                if e_k > makespan:
                    makespan = e_k
                emit_unit = (
                    u_k
                    if (u_k is not None and instance.capacities.get(op_k.instrument, 0) > 1)
                    else None
                )
                scheduled.append(
                    ScheduledOp(
                        job_id=jid,
                        op_index=k,
                        name=op_k.name,
                        instrument=op_k.instrument,
                        start=s_k,
                        end=e_k,
                        unit_index=emit_unit,
                    )
                )

            last_k, last_s, last_e, last_u = placed[-1]
            prev_placed = (
                last_s,
                last_e,
                last_u,
                instance.effective_setup_tag(job, job.ops[last_k]),
            )
            a = b + 1
            if a < m:
                next_op = job.ops[a]
                eff_t = instance.effective_transfer_time(job.ops[b].instrument, next_op)
                ready_a = last_e + max(next_op.min_wait, eff_t)

        assert prev_placed is not None
        if job.deadline is not None and prev_placed[1] > job.deadline:
            dl_viols += 1
            tardiness += prev_placed[1] - job.deadline
            job_violated = True
        if job_violated:
            violating.append(jid)

    return scheduled, win_viols, dl_viols, tardiness, makespan, violating


def _decode_biased_segments(
    instance: Instance,
    biases: dict[str, float],
    rank_tie: dict[str, int],
    pre: _PrecomputedLNS,
) -> list[ScheduledOp]:
    """Interleaved coupled-segment dispatch using learned per-job ready-time biases."""
    trackers: dict[str, _PoolTracker] = {
        inst: _PoolTracker(instance, inst) for inst in instance.capacities
    }
    next_op_idx: dict[str, int] = {jid: 0 for jid in pre.nonempty_ids}
    ready_time: dict[str, int] = {
        jid: pre.job_map[jid].release for jid in pre.nonempty_ids
    }
    prev_placed: dict[str, tuple[int, int, int | None, str | None]] = {}
    active = list(pre.nonempty_ids)
    scheduled: list[ScheduledOp] = []

    while active:
        chosen_id = min(
            active,
            key=lambda jid: (
                ready_time[jid] + biases.get(jid, 0.0),
                rank_tie.get(jid, 0),
                jid,
            ),
        )
        job = pre.job_map[chosen_id]
        a = next_op_idx[chosen_id]
        b = pre.seg_ends[(chosen_id, a)]
        placed = _place_coupled_segment(
            trackers,
            instance,
            job,
            a,
            b,
            ready_time[chosen_id],
            prev_placed.get(chosen_id),
            pre.job_lcts[chosen_id],
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
        nxt = b + 1
        next_op_idx[chosen_id] = nxt
        if nxt < len(job.ops):
            next_op = job.ops[nxt]
            eff_t = instance.effective_transfer_time(job.ops[b].instrument, next_op)
            ready_time[chosen_id] = last_e + max(next_op.min_wait, eff_t)
        else:
            active.remove(chosen_id)

    return scheduled


def _score_candidate(
    instance: Instance,
    ops: tuple[ScheduledOp, ...] | list[ScheduledOp],
) -> tuple[tuple[int, int, int, int, int, int], bool, int]:
    """Exact checker-verified lexicographic score:
    ``(infeasible_flag, total_viols, win_viols, tardiness, makespan, sum_flow)``."""
    sched = Schedule(instance_name=instance.name, solver="lns", ops=tuple(ops), status="OK")
    chk = check_schedule(instance, sched)
    counts = chk.counts_by_kind()
    win_v = (
        counts.get("min_wait", 0)
        + counts.get("max_wait", 0)
        + counts.get("transfer_time", 0)
    )
    by_key = sched.by_key()
    ms = max((op.end for op in sched.ops), default=0)
    tard = 0
    flow_sum = 0
    for job in instance.jobs:
        if not job.ops:
            continue
        last_op = by_key.get((job.job_id, len(job.ops) - 1))
        if last_op is not None:
            flow_sum += last_op.end - job.release
            if job.deadline is not None and last_op.end > job.deadline:
                tard += last_op.end - job.deadline
    score = (
        0 if chk.feasible else 1,
        len(chk.violations),
        win_v,
        tard,
        ms,
        flow_sum,
    )
    return score, chk.feasible, ms


def _fast_order_tuple_score(
    win_v: int,
    dl_v: int,
    tard: int,
    ms: int,
) -> tuple[int, int, int, int]:
    return (win_v + dl_v, win_v, tard, ms)


def solve_lns(
    instance: Instance,
    time_limit: float = 2.0,
    *,
    seed: int | None = 0,
) -> Schedule:
    """Solve an instance with pure-stdlib Iterated Greedy & Large Neighborhood Search (LNS).

    Combines:
      1. Warm-start portfolio from ``solve_greedy_repair`` and deterministic job-order rules
         (EDF, minimum slack, release order, LPT, SPT).
      2. Violation-directed Ruiz-Stuetzle Destroy-and-Best-Insertion over job permutations to
         untangle tight planted ``max_wait`` and ``deadline`` bottlenecks without C++ solvers.
      3. Critical-path job destruction/reinsertion and biased segment-priority neighborhood
         search to compact makespan and flow time toward ``makespan_lower_bound(instance)``.
    """
    if isinstance(time_limit, bool) or not isinstance(time_limit, (int, float)):
        raise TypeError(f"'time_limit' must be a number, got {time_limit!r}.")
    if not math.isfinite(float(time_limit)) or time_limit <= 0:
        raise ValueError(f"'time_limit' must be a positive finite number, got {time_limit!r}.")
    if seed is not None and (isinstance(seed, bool) or not isinstance(seed, int)):
        raise TypeError(f"'seed' must be an integer or None, got {seed!r}.")

    t0 = time.perf_counter()

    if instance.total_operations == 0:
        return Schedule(
            instance_name=instance.name,
            solver="lns",
            ops=(),
            status="OPTIMAL",
            solve_time_s=time.perf_counter() - t0,
            best_bound=0.0,
        )

    if _has_infeasible_capacity(instance):
        return Schedule(
            instance_name=instance.name,
            solver="lns",
            ops=(),
            status="INFEASIBLE",
            solve_time_s=time.perf_counter() - t0,
            best_bound=None,
        )

    rng = random.Random(0 if seed is None else seed)
    lb = makespan_lower_bound(instance)
    pre = _precompute(instance)
    n_jobs = len(pre.nonempty_ids)

    # 1. Evaluate greedy_repair baseline
    rep_sched = solve_greedy_repair(instance)
    best_ops: tuple[ScheduledOp, ...] = rep_sched.ops
    best_score, best_feasible, best_ms = _score_candidate(instance, best_ops)

    if best_feasible and best_ms <= lb:
        return Schedule(
            instance_name=instance.name,
            solver="lns",
            ops=best_ops,
            status="OPTIMAL",
            solve_time_s=time.perf_counter() - t0,
            best_bound=float(lb),
        )

    if n_jobs <= 1:
        return Schedule(
            instance_name=instance.name,
            solver="lns",
            ops=best_ops,
            status="OPTIMAL" if (best_feasible and best_ms == lb) else "OK",
            solve_time_s=time.perf_counter() - t0,
            best_bound=float(lb) if best_feasible else None,
        )

    # 2. Seed initial job-order permutations
    INF_DL = 10**9

    def _dl_or(jid: str, fallback: int = INF_DL) -> int:
        dl = pre.job_map[jid].deadline
        return dl if dl is not None else fallback

    seed_orders: list[list[str]] = [
        sorted(
            pre.nonempty_ids,
            key=lambda jid: (
                _dl_or(jid),
                pre.job_map[jid].release,
                jid,
            ),
        ),
        sorted(
            pre.nonempty_ids,
            key=lambda jid: (
                _dl_or(jid) - (pre.job_map[jid].release + pre.chain_lens[jid]),
                pre.job_map[jid].release,
                jid,
            ),
        ),
        sorted(
            pre.nonempty_ids,
            key=lambda jid: (pre.job_map[jid].release, -pre.chain_lens[jid], jid),
        ),
        sorted(
            pre.nonempty_ids,
            key=lambda jid: (-pre.chain_lens[jid], pre.job_map[jid].release, jid),
        ),
    ]
    # Also derive job completion order and job first-start order from best_ops
    if best_ops:
        by_k = {(op.job_id, op.op_index): op for op in best_ops}
        seed_orders.append(
            sorted(
                pre.nonempty_ids,
                key=lambda jid: (by_k[(jid, 0)].start, by_k[(jid, len(pre.job_map[jid].ops) - 1)].end, jid),
            )
        )
        seed_orders.append(
            sorted(
                pre.nonempty_ids,
                key=lambda jid: (by_k[(jid, len(pre.job_map[jid].ops) - 1)].end, by_k[(jid, 0)].start, jid),
            )
        )

    cur_order = list(seed_orders[0])
    cur_ops, cur_wv, cur_dv, cur_tard, cur_ms, cur_viol_ids = _decode_job_order(
        instance, cur_order, pre, allow_deadline_prune=False
    )
    cur_fast_score = _fast_order_tuple_score(cur_wv, cur_dv, cur_tard, cur_ms)

    for order in seed_orders:
        if time.perf_counter() - t0 >= time_limit:
            break
        for prune_dl in (False, True):
            ops_o, wv_o, dv_o, tard_o, ms_o, viol_o = _decode_job_order(
                instance, order, pre, allow_deadline_prune=prune_dl
            )
            f_score = _fast_order_tuple_score(wv_o, dv_o, tard_o, ms_o)
            if f_score < cur_fast_score:
                cur_order = list(order)
                cur_ops, cur_wv, cur_dv, cur_tard, cur_ms, cur_viol_ids = (
                    ops_o,
                    wv_o,
                    dv_o,
                    tard_o,
                    ms_o,
                    viol_o,
                )
                cur_fast_score = f_score
            if wv_o + dv_o == 0 or not best_feasible:
                sc_o, feas_o, real_ms_o = _score_candidate(instance, ops_o)
                if sc_o < best_score:
                    best_score = sc_o
                    best_feasible = feas_o
                    best_ms = real_ms_o
                    best_ops = tuple(ops_o)
                    if best_feasible and best_ms <= lb:
                        return Schedule(
                            instance_name=instance.name,
                            solver="lns",
                            ops=best_ops,
                            status="OPTIMAL",
                            solve_time_s=time.perf_counter() - t0,
                            best_bound=float(lb),
                        )

    best_order = list(cur_order)
    best_order_fast = cur_fast_score
    best_order_viol = list(cur_viol_ids)
    stagnation = 0
    iteration = 0

    # 3. Main Iterated Greedy / Destroy-and-Repair LNS loop
    while time.perf_counter() - t0 < time_limit:
        iteration += 1

        # Periodically explore biased interleaved segment priorities when feasible to pack idle gaps
        if best_feasible and iteration % 4 == 0 and time.perf_counter() - t0 < time_limit:
            rank_tie = {jid: idx for idx, jid in enumerate(best_order)}
            biases: dict[str, float] = {}
            by_k_best = {(op.job_id, op.op_index): op for op in best_ops}
            crit_ids = [
                jid
                for jid in pre.nonempty_ids
                if by_k_best[(jid, len(pre.job_map[jid].ops) - 1)].end >= best_ms - 5
            ]
            for jid in pre.nonempty_ids:
                if jid in crit_ids:
                    biases[jid] = float(rng.randint(-25, -2))
                else:
                    biases[jid] = float(rng.randint(-6, 18))
            cand_seg_ops = _decode_biased_segments(instance, biases, rank_tie, pre)
            sc_seg, feas_seg, ms_seg = _score_candidate(instance, cand_seg_ops)
            if sc_seg < best_score:
                best_score = sc_seg
                best_feasible = feas_seg
                best_ms = ms_seg
                best_ops = tuple(cand_seg_ops)
                by_k_new = {(op.job_id, op.op_index): op for op in best_ops}
                best_order = sorted(
                    pre.nonempty_ids,
                    key=lambda jid: (by_k_new[(jid, 0)].start, jid),
                )
                stagnation = 0
                if best_feasible and best_ms <= lb:
                    break

        # Restart / diversification kick if stagnated
        if stagnation >= 14:
            if rng.random() < 0.5:
                cur_order = list(best_order)
                cur_viol_ids = list(best_order_viol)
                # Random block swap / kick
                k_swaps = min(n_jobs // 2, rng.randint(2, 4))
                for _ in range(max(1, k_swaps)):
                    i1, i2 = rng.sample(range(n_jobs), 2)
                    cur_order[i1], cur_order[i2] = cur_order[i2], cur_order[i1]
            else:
                # Noisy slack/deadline order for global basin escape on planted instances
                cur_order = sorted(
                    pre.nonempty_ids,
                    key=lambda jid: (
                        _dl_or(jid, INF_DL // 1000) * (0.75 + 0.5 * rng.random())
                        + pre.job_map[jid].release * rng.uniform(-0.5, 0.8),
                        jid,
                    ),
                )
            cur_ops, cur_wv, cur_dv, cur_tard, cur_ms, cur_viol_ids = _decode_job_order(
                instance, cur_order, pre, allow_deadline_prune=False
            )
            cur_fast_score = _fast_order_tuple_score(cur_wv, cur_dv, cur_tard, cur_ms)
            stagnation = 0

        # Destroy step: select d jobs (targeting violating or critical-end jobs + blockers)
        d_size = min(n_jobs - 1, rng.randint(2, min(5, max(2, n_jobs // 3))))
        removed: list[str] = []
        pos_map = {jid: idx for idx, jid in enumerate(cur_order)}

        if cur_viol_ids:
            v_pick = list(cur_viol_ids)
            rng.shuffle(v_pick)
            for v_id in v_pick[: max(1, (d_size + 1) // 2)]:
                if v_id not in removed:
                    removed.append(v_id)
            v_first = removed[0]
            v_pos = pos_map[v_first]
            v_insts = pre.inst_sets[v_first]
            blockers = [
                cur_order[idx]
                for idx in range(v_pos)
                if cur_order[idx] not in removed and (pre.inst_sets[cur_order[idx]] & v_insts)
            ]
            if blockers:
                rng.shuffle(blockers)
                for b_id in blockers[: d_size - len(removed)]:
                    removed.append(b_id)
        else:
            # Feasible order: target jobs finishing near makespan + predecessors
            end_by_job: dict[str, int] = {}
            for sop in cur_ops:
                if sop.op_index == len(pre.job_map[sop.job_id].ops) - 1:
                    end_by_job[sop.job_id] = sop.end
            crit_pool = [
                jid for jid, e_val in end_by_job.items() if e_val >= cur_ms - 8
            ]
            if crit_pool:
                removed.append(rng.choice(crit_pool))

        while len(removed) < d_size:
            cand_j = rng.choice(cur_order)
            if cand_j not in removed:
                removed.append(cand_j)

        partial = [jid for jid in cur_order if jid not in removed]

        # Re-insert tightest-deadline / violating jobs first across candidate positions
        removed.sort(
            key=lambda jid: (
                0 if jid in cur_viol_ids else 1,
                pre.job_map[jid].deadline if pre.job_map[jid].deadline is not None else INF_DL,
                -pre.chain_lens[jid],
            )
        )

        for ins_jid in removed:
            if time.perf_counter() - t0 >= time_limit:
                partial.append(ins_jid)
                continue
            n_positions = len(partial) + 1
            if n_positions <= 14:
                cand_positions = list(range(n_positions))
            else:
                orig_p = min(len(partial), pos_map.get(ins_jid, len(partial)))
                focus = {0, len(partial), orig_p}
                for delta in (-6, -4, -3, -2, -1, 1, 2, 4):
                    p_try = orig_p + delta
                    if 0 <= p_try <= len(partial):
                        focus.add(p_try)
                extra_pool = [p for p in range(n_positions) if p not in focus]
                if extra_pool:
                    focus.update(rng.sample(extra_pool, min(4, len(extra_pool))))
                cand_positions = sorted(focus)

            best_pos = cand_positions[0]
            best_pos_score: tuple[int, int, int, int] | None = None
            for pos in cand_positions:
                trial = partial[:pos] + [ins_jid] + partial[pos:]
                _, wv_t, dv_t, tard_t, ms_t, _ = _decode_job_order(
                    instance, trial, pre, allow_deadline_prune=False
                )
                sc_t = _fast_order_tuple_score(wv_t, dv_t, tard_t, ms_t)
                if best_pos_score is None or sc_t < best_pos_score:
                    best_pos_score = sc_t
                    best_pos = pos
            partial.insert(best_pos, ins_jid)

        new_ops, new_wv, new_dv, new_tard, new_ms, new_viol = _decode_job_order(
            instance, partial, pre, allow_deadline_prune=False
        )
        new_fast = _fast_order_tuple_score(new_wv, new_dv, new_tard, new_ms)

        improved_global = False
        if new_wv + new_dv == 0 or not best_feasible:
            sc_new, feas_new, real_ms_new = _score_candidate(instance, new_ops)
            if sc_new < best_score:
                best_score = sc_new
                best_feasible = feas_new
                best_ms = real_ms_new
                best_ops = tuple(new_ops)
                improved_global = True

        if new_fast < best_order_fast:
            best_order = list(partial)
            best_order_fast = new_fast
            best_order_viol = list(new_viol)
            improved_global = True

        # Metropolis / threshold acceptance on job order
        accept = False
        if new_fast <= cur_fast_score:
            accept = True
        elif new_wv + new_dv <= cur_wv + cur_dv and rng.random() < 0.20:
            accept = True
        elif not best_feasible and rng.random() < 0.08:
            accept = True

        if accept:
            cur_order = partial
            cur_ops, cur_wv, cur_dv, cur_tard, cur_ms, cur_viol_ids = (
                new_ops,
                new_wv,
                new_dv,
                new_tard,
                new_ms,
                new_viol,
            )
            cur_fast_score = new_fast

        if improved_global:
            stagnation = 0
            if best_feasible and best_ms <= lb:
                break
        else:
            stagnation += 1

    elapsed = time.perf_counter() - t0
    final_status = "OPTIMAL" if (best_feasible and best_ms == lb) else "OK"
    return Schedule(
        instance_name=instance.name,
        solver="lns",
        ops=best_ops,
        status=final_status,
        solve_time_s=elapsed,
        best_bound=float(lb) if (best_feasible and best_ms == lb) else None,
    )
