"""Pure-stdlib combinatorial makespan lower bounds for labshift instances."""

from __future__ import annotations

import math
from collections import defaultdict

from labshift.models import Instance


def _energetic_pool_bound(
    items: list[tuple[int, int, int, str | None]],
    capacity: int,
    *,
    instance: Instance | None = None,
    instrument: str | None = None,
) -> int:
    """Compute exact subset Jackson/Carlier energetic bound over ``(head, dur, tail, tag)`` items.

    Evaluates ``min_{o in S} head_o + ceil(sum_{o in S} dur_o / capacity) + min_{o in S} tail_o``
    across all non-empty subsets ``S`` in ``O(|items|^2)`` time by sweeping canonical ``(r*, q*)``
    head-tail thresholds, plus unconditional full-pool minimum changeover slack when
    sequence-dependent ``setup_times`` are present and ``|items| > capacity``.
    """
    if not items or capacity <= 0:
        return 0

    best = 0
    r_min = min(item[0] for item in items)
    q_min = min(item[2] for item in items)
    total_work = sum(item[1] for item in items)

    setup_lb = 0
    if (
        instance is not None
        and instrument is not None
        and instance.has_setup_times_for(instrument)
        and len(items) > capacity
    ):
        n_items = len(items)
        k_transitions = n_items - capacity
        min_out: list[int] = []
        min_in: list[int] = []
        for i in range(n_items):
            tag_i = items[i][3]
            out_i = min(
                instance.required_setup_time(instrument, tag_i, items[j][3])
                for j in range(n_items)
                if j != i
            )
            in_i = min(
                instance.required_setup_time(instrument, items[j][3], tag_i)
                for j in range(n_items)
                if j != i
            )
            min_out.append(out_i)
            min_in.append(in_i)
        min_out.sort()
        min_in.sort()
        setup_lb = max(sum(min_out[:k_transitions]), sum(min_in[:k_transitions]))

    best = max(best, r_min + math.ceil((total_work + setup_lb) / capacity) + q_min)

    # Subset Jackson/Carlier energetic sweep over all canonical (r*, q*) thresholds.
    items_by_q_desc = sorted(items, key=lambda x: -x[2])
    distinct_heads = sorted({item[0] for item in items})
    for r_star in distinct_heads:
        work = 0
        for r_i, p_i, q_i, _ in items_by_q_desc:
            if r_i >= r_star:
                work += p_i
                cand = r_star + math.ceil(work / capacity) + q_i
                if cand > best:
                    best = cand

    return best


def makespan_lower_bound_components(instance: Instance) -> dict[str, int]:
    """Return decomposed stdlib makespan lower bounds (chain, instrument pools, robot_arm)."""
    chain_bound = 0
    per_inst: dict[str, list[tuple[int, int, int, str | None]]] = defaultdict(list)
    arm_items: list[tuple[int, int, int, str | None]] = []
    has_arm = "robot_arm" in instance.capacities

    for job in instance.jobs:
        m = len(job.ops)
        if m == 0:
            continue

        heads = [0] * m
        heads[0] = job.release
        for idx in range(1, m):
            eff_wait = max(
                job.ops[idx].min_wait,
                instance.effective_transfer_time(job.ops[idx - 1].instrument, job.ops[idx]),
            )
            heads[idx] = heads[idx - 1] + job.ops[idx - 1].duration + eff_wait

        tails = [0] * m
        for idx in range(m - 2, -1, -1):
            eff_next_wait = max(
                job.ops[idx + 1].min_wait,
                instance.effective_transfer_time(job.ops[idx].instrument, job.ops[idx + 1]),
            )
            tails[idx] = tails[idx + 1] + job.ops[idx + 1].duration + eff_next_wait

        chain_len = heads[-1] + job.ops[-1].duration
        if chain_len > chain_bound:
            chain_bound = chain_len

        for idx, op in enumerate(job.ops):
            tag = instance.effective_setup_tag(job, op)
            per_inst[op.instrument].append((heads[idx], op.duration, tails[idx], tag))
            if (
                has_arm
                and idx > 0
                and instance.uses_robot_arm(job.ops[idx - 1].instrument, op)
            ):
                eff_t = instance.effective_transfer_time(job.ops[idx - 1].instrument, op)
                r_arm = heads[idx] - eff_t
                q_arm = op.duration + tails[idx]
                arm_items.append((r_arm, eff_t, q_arm, None))

    instrument_bound = 0
    for inst, items in per_inst.items():
        cap = instance.capacities.get(inst, 0)
        if cap > 0:
            pool_lb = _energetic_pool_bound(
                items, cap, instance=instance, instrument=inst
            )
            if pool_lb > instrument_bound:
                instrument_bound = pool_lb

    robot_arm_bound = 0
    if has_arm and arm_items:
        arm_cap = instance.capacities.get("robot_arm", 0)
        if arm_cap > 0:
            robot_arm_bound = _energetic_pool_bound(arm_items, arm_cap)

    lower_bound = max(chain_bound, instrument_bound, robot_arm_bound)
    return {
        "chain_bound": chain_bound,
        "instrument_bound": instrument_bound,
        "robot_arm_bound": robot_arm_bound,
        "lower_bound": lower_bound,
    }


def makespan_lower_bound(instance: Instance) -> int:
    """Compute a valid stdlib combinatorial lower bound on any schedule's makespan.

    Takes the maximum across:
      1. Longest single-job chain (``release + sum(durations) + sum(max(min_wait, transfer_time))``).
      2. Per-instrument Jackson/Carlier energetic bounds (full pool and subset ``(r*, q*)``
         strengthenings, plus unconditional full-pool changeover slack under ``setup_times``).
      3. ``robot_arm`` transfer pool energetic bound when ``robot_arm`` is modeled.
    """
    return makespan_lower_bound_components(instance)["lower_bound"]
