"""Seeded synthetic and planted-schedule instance generator for labshift."""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from labshift.checker import check_schedule
from labshift.greedy import _PoolTracker, solve_greedy_repair
from labshift.models import Instance, Job, Operation, Schedule, ScheduledOp

# Generic liquid-handling workflow steps: (op_name, instrument, (min_dur, max_dur))
_WORKFLOW_TEMPLATES: tuple[tuple[tuple[str, str, tuple[int, int]], ...], ...] = (
    (
        ("plate_prep", "liquid_handler", (6, 14)),
        ("incubate", "incubator", (15, 35)),
        ("plate_read", "plate_reader", (4, 10)),
    ),
    (
        ("dilution_series", "liquid_handler", (8, 18)),
        ("dye_dispense", "liquid_handler", (4, 10)),
        ("incubate", "incubator", (15, 30)),
        ("plate_read", "plate_reader", (4, 10)),
    ),
    (
        ("buffer_add", "liquid_handler", (4, 12)),
        ("spin_down", "centrifuge", (5, 14)),
        ("plate_wash", "washer", (5, 12)),
        ("plate_read", "plate_reader", (4, 9)),
    ),
    (
        ("plate_prep", "liquid_handler", (6, 14)),
        ("incubate", "incubator", (12, 28)),
        ("mix_wash", "washer", (5, 11)),
        ("dye_dispense", "liquid_handler", (4, 9)),
        ("plate_read", "plate_reader", (4, 10)),
    ),
    (
        ("buffer_add", "liquid_handler", (5, 11)),
        ("dilution_series", "liquid_handler", (8, 16)),
        ("equilibrate", "incubator", (10, 24)),
        ("spin_down", "centrifuge", (5, 12)),
        ("plate_read", "plate_reader", (4, 9)),
    ),
    (
        ("plate_prep", "liquid_handler", (6, 12)),
        ("mix_wash", "washer", (5, 10)),
        ("dye_dispense", "liquid_handler", (4, 9)),
        ("incubate", "incubator", (15, 30)),
        ("spin_down", "centrifuge", (5, 12)),
        ("plate_read", "plate_reader", (4, 10)),
    ),
)


@dataclass(frozen=True)
class PresetConfig:
    """Configuration parameters for an instance size preset."""

    num_jobs: int
    ops_min: int
    ops_max: int
    release_max: int
    capacities: dict[str, int]
    max_wait_prob: float
    max_wait_range: tuple[int, int]
    deadline_prob: float
    deadline_slack_factor: float


# Note on v1 suite arrival intensity (benchmarks/small_00..large_09):
# - "small" (6 jobs, release_max=20, 1 plate_reader) sets max_wait_prob=0.0.
# - "medium" (12 jobs, release_max=40, 1 plate_reader => 0.30 jobs/min/reader)
#   creates heavy single-reader contention under max_wait_prob=0.5 (4/10 greedy feasible).
# - "large" (20 jobs, release_max=60, 2 plate_readers => 0.167 jobs/min/reader)
#   models a scaled-out two-reader workcell (10/10 greedy feasible on v1 seeds).
# - "hard" (20 jobs, release_max=45, 1 plate_reader => 0.44 jobs/min/reader)
#   scales bottleneck arrival intensity monotonically above "medium" for stress-testing.
PRESETS: dict[str, PresetConfig] = {
    "small": PresetConfig(
        num_jobs=6,
        ops_min=3,
        ops_max=4,
        release_max=20,
        capacities={
            "liquid_handler": 2,
            "incubator": 2,
            "plate_reader": 1,
            "centrifuge": 1,
            "washer": 1,
        },
        max_wait_prob=0.0,
        max_wait_range=(15, 30),
        deadline_prob=0.5,
        deadline_slack_factor=2.5,
    ),
    "medium": PresetConfig(
        num_jobs=12,
        ops_min=4,
        ops_max=5,
        release_max=40,
        capacities={
            "liquid_handler": 2,
            "incubator": 3,
            "plate_reader": 1,
            "centrifuge": 2,
            "washer": 2,
        },
        max_wait_prob=0.5,
        max_wait_range=(6, 16),
        deadline_prob=0.65,
        deadline_slack_factor=2.2,
    ),
    "large": PresetConfig(
        num_jobs=20,
        ops_min=4,
        ops_max=6,
        release_max=60,
        capacities={
            "liquid_handler": 3,
            "incubator": 4,
            "plate_reader": 2,
            "centrifuge": 2,
            "washer": 2,
        },
        max_wait_prob=0.6,
        max_wait_range=(6, 16),
        deadline_prob=0.65,
        deadline_slack_factor=2.4,
    ),
    "hard": PresetConfig(
        num_jobs=20,
        ops_min=4,
        ops_max=6,
        release_max=45,
        capacities={
            "liquid_handler": 2,
            "incubator": 3,
            "plate_reader": 1,
            "centrifuge": 2,
            "washer": 2,
        },
        max_wait_prob=0.65,
        max_wait_range=(6, 14),
        deadline_prob=0.65,
        deadline_slack_factor=2.8,
    ),
}

DEFAULT_SUITE_PRESETS: tuple[str, ...] = ("small", "medium", "large")
PLANTED_SUITE_PRESETS: tuple[str, ...] = ("medium", "large")


def _sample_from_config(
    cfg: PresetConfig,
    preset: str,
    rng_seed: int,
    recorded_seed: int,
    inst_name: str,
    *,
    slack_bonus: int = 0,
    window_bonus: int = 0,
) -> Instance:
    rng = random.Random(rng_seed)
    eligible = [
        tpl
        for tpl in _WORKFLOW_TEMPLATES
        if cfg.ops_min <= len(tpl) <= cfg.ops_max
    ]
    if not eligible:
        eligible = list(_WORKFLOW_TEMPLATES)

    jobs: list[Job] = []
    for j_idx in range(cfg.num_jobs):
        tpl = rng.choice(eligible)
        release = rng.randint(0, cfg.release_max) if j_idx > 0 else 0
        ops: list[Operation] = []
        total_dur = 0

        for op_idx, (op_name, inst, (d_min, d_max)) in enumerate(tpl):
            dur = rng.randint(d_min, d_max)
            total_dur += dur
            if op_idx == 0:
                min_wait = 0
                max_wait = None
            else:
                min_wait = rng.choice((0, 0, 1, 2, 3))
                prev_inst = tpl[op_idx - 1][1]
                tight_step = prev_inst in ("incubator", "centrifuge") or op_name == "plate_read"
                if tight_step and rng.random() < cfg.max_wait_prob:
                    max_wait = min_wait + rng.randint(*cfg.max_wait_range) + window_bonus
                else:
                    max_wait = None
            ops.append(
                Operation(
                    name=op_name,
                    instrument=inst,
                    duration=dur,
                    min_wait=min_wait,
                    max_wait=max_wait,
                )
            )

        deadline: int | None = None
        if rng.random() < cfg.deadline_prob:
            min_chain = total_dur + sum(op.min_wait for op in ops)
            slack = (
                round(min_chain * cfg.deadline_slack_factor)
                + cfg.num_jobs * 3
                + slack_bonus
            )
            deadline = release + slack

        jobs.append(
            Job(
                job_id=f"job_{j_idx + 1:02d}",
                ops=tuple(ops),
                release=release,
                deadline=deadline,
            )
        )

    return Instance(
        name=inst_name,
        preset=preset,
        seed=recorded_seed,
        capacities=dict(cfg.capacities),
        jobs=tuple(jobs),
    )


def generate_planted_instance(
    preset: str = "medium",
    seed: int = 0,
    name: str | None = None,
    *,
    window_prob: float = 0.70,
    window_slack_max: int = 2,
    deadline_slack_max: int = 5,
) -> tuple[Instance, Schedule]:
    """Generate a guaranteed-feasible hard instance and its planted witness Schedule.

    Constructs a valid multi-instrument schedule first in a seeded random job priority
    permutation, then sets ``max_wait = planted_wait + U{0..window_slack_max}`` on
    ``window_prob`` of inter-step transitions and ``deadline = planted_end + U{0..deadline_slack_max}``
    on every job. Feasibility is guaranteed by construction without calling any solver.
    """
    if preset not in PRESETS:
        raise ValueError(f"Unknown preset '{preset}'. Choose from {tuple(PRESETS)}.")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise TypeError(f"'seed' must be an integer, got {seed!r}.")

    cfg = PRESETS[preset]
    rng = random.Random(seed)
    inst_name = name or f"planted_{preset}_{seed:02d}"
    eligible = [
        tpl
        for tpl in _WORKFLOW_TEMPLATES
        if cfg.ops_min <= len(tpl) <= cfg.ops_max
    ]
    if not eligible:
        eligible = list(_WORKFLOW_TEMPLATES)

    raw_jobs: list[tuple[str, int, list[tuple[str, str, int, int]]]] = []
    for j_idx in range(cfg.num_jobs):
        tpl = rng.choice(eligible)
        release = rng.randint(0, cfg.release_max) if j_idx > 0 else 0
        ops_spec: list[tuple[str, str, int, int]] = []
        for op_idx, (op_name, inst, (d_min, d_max)) in enumerate(tpl):
            dur = rng.randint(d_min, d_max)
            min_w = 0 if op_idx == 0 else rng.choice((0, 0, 1, 2, 3))
            ops_spec.append((op_name, inst, dur, min_w))
        raw_jobs.append((f"job_{j_idx + 1:02d}", release, ops_spec))

    # Build a packed multi-instrument witness schedule in a random job permutation.
    dummy_inst = Instance(name="_planted_tmp", capacities=dict(cfg.capacities), jobs=())
    trackers: dict[str, _PoolTracker] = {
        inst: _PoolTracker(dummy_inst, inst) for inst in cfg.capacities
    }
    order = list(range(cfg.num_jobs))
    rng.shuffle(order)

    placed_by_job: dict[str, list[tuple[int, int]]] = {}
    for j_idx in order:
        jid, release, ops_spec = raw_jobs[j_idx]
        cur_ready = release
        j_placed: list[tuple[int, int]] = []
        for op_idx, (_, inst, dur, min_w) in enumerate(ops_spec):
            if op_idx > 0:
                cur_ready = j_placed[-1][1] + min_w
            start, _, _ = trackers[inst].earliest_slot(cur_ready, dur, None)
            end = start + dur
            trackers[inst].push_interval(start, end)
            j_placed.append((start, end))
        placed_by_job[jid] = j_placed

    jobs: list[Job] = []
    witness_ops: list[ScheduledOp] = []
    for jid, release, ops_spec in raw_jobs:
        j_placed = placed_by_job[jid]
        ops: list[Operation] = []
        for op_idx, (op_name, inst, dur, min_w) in enumerate(ops_spec):
            s_val, e_val = j_placed[op_idx]
            witness_ops.append(
                ScheduledOp(
                    job_id=jid,
                    op_index=op_idx,
                    name=op_name,
                    instrument=inst,
                    start=s_val,
                    end=e_val,
                )
            )
            if op_idx == 0:
                max_w: int | None = None
            else:
                planted_wait = s_val - j_placed[op_idx - 1][1]
                if rng.random() < window_prob:
                    max_w = planted_wait + rng.randint(0, window_slack_max)
                else:
                    max_w = None
            ops.append(
                Operation(
                    name=op_name,
                    instrument=inst,
                    duration=dur,
                    min_wait=min_w,
                    max_wait=max_w,
                )
            )
        deadline = j_placed[-1][1] + rng.randint(0, deadline_slack_max)
        jobs.append(
            Job(
                job_id=jid,
                ops=tuple(ops),
                release=release,
                deadline=deadline,
            )
        )

    instance = Instance(
        name=inst_name,
        preset=f"planted_{preset}",
        seed=seed,
        capacities=dict(cfg.capacities),
        jobs=tuple(jobs),
    )
    witness = Schedule(
        instance_name=instance.name,
        solver="planted_witness",
        ops=tuple(witness_ops),
        status="OK",
        solve_time_s=0.0,
        best_bound=None,
    )
    return instance, witness


def _is_verified_feasible(instance: Instance) -> bool:
    """Return True if a feasible schedule can be verified via greedy_repair or CP-SAT."""
    rep_sched = solve_greedy_repair(instance)
    if check_schedule(instance, rep_sched).feasible:
        return True
    try:
        from labshift.cpsat import solve_cpsat
    except ImportError:
        return False
    cp_sched = solve_cpsat(instance, time_limit=2.0, num_workers=2)
    return bool(cp_sched.ops) and check_schedule(instance, cp_sched).feasible


def generate_instance(
    preset: str = "small",
    seed: int = 0,
    name: str | None = None,
    *,
    ensure_feasible: bool = False,
    planted: bool = False,
    witness_out: str | Path | None = None,
) -> Instance:
    """Generate a synthetic lab scheduling instance for the given preset and seed."""
    if preset not in PRESETS:
        raise ValueError(f"Unknown preset '{preset}'. Choose from {tuple(PRESETS)}.")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise TypeError(f"'seed' must be an integer, got {seed!r}.")

    if planted:
        inst_planted, witness = generate_planted_instance(
            preset=preset, seed=seed, name=name
        )
        if witness_out is not None:
            witness.save_json(witness_out)
        return inst_planted

    cfg = PRESETS[preset]
    inst_name = name or f"{preset}_{seed:02d}"
    inst = _sample_from_config(cfg, preset, seed, seed, inst_name)
    if not ensure_feasible:
        return inst

    if _is_verified_feasible(inst):
        return inst

    for attempt in range(1, 16):
        relaxed = _sample_from_config(
            cfg,
            preset,
            seed,
            seed,
            inst_name,
            slack_bonus=attempt * max(15, cfg.num_jobs * 4),
            window_bonus=attempt * 4,
        )
        if _is_verified_feasible(relaxed):
            return relaxed

    raise RuntimeError(
        f"Unable to verify a feasible instance for preset='{preset}', seed={seed}."
    )


def generate_benchmark_suite(
    out_dir: str | Path,
    count_per_preset: int = 10,
    base_seed: int = 1000,
    *,
    presets: Sequence[str] = DEFAULT_SUITE_PRESETS,
    ensure_feasible: bool = False,
    planted: bool = False,
    witness_dir: str | Path | None = None,
) -> list[Path]:
    """Generate a fixed benchmark set of instances in out_dir."""
    if isinstance(count_per_preset, bool) or not isinstance(count_per_preset, int):
        raise TypeError(f"'count_per_preset' must be an integer, got {count_per_preset!r}.")
    if count_per_preset < 1:
        raise ValueError(f"'count_per_preset' must be >= 1, got {count_per_preset}.")
    if isinstance(base_seed, bool) or not isinstance(base_seed, int):
        raise TypeError(f"'base_seed' must be an integer, got {base_seed!r}.")

    root = Path(out_dir)
    root.mkdir(parents=True, exist_ok=True)
    wit_root = Path(witness_dir) if witness_dir is not None else None
    if wit_root is not None:
        wit_root.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    for p_idx, preset in enumerate(presets):
        for idx in range(count_per_preset):
            seed = base_seed + p_idx * 100 + idx
            stem = f"planted_{preset}_{idx:02d}" if planted else f"{preset}_{idx:02d}"
            wit_path = (wit_root / f"{stem}.schedule.json") if (planted and wit_root is not None) else None
            inst = generate_instance(
                preset=preset,
                seed=seed,
                name=stem,
                ensure_feasible=ensure_feasible,
                planted=planted,
                witness_out=wit_path,
            )
            path = root / f"{stem}.json"
            inst.save_json(path)
            written.append(path)
    return written
