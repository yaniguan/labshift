"""Seeded synthetic instance generator for labshift."""

from __future__ import annotations

import random
from dataclasses import dataclass
from pathlib import Path

from labshift.models import Instance, Job, Operation

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
}


def generate_instance(
    preset: str = "small",
    seed: int = 0,
    name: str | None = None,
) -> Instance:
    """Generate a synthetic lab scheduling instance for the given preset and seed."""
    if preset not in PRESETS:
        raise ValueError(f"Unknown preset '{preset}'. Choose from {tuple(PRESETS)}.")

    cfg = PRESETS[preset]
    rng = random.Random(seed)
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
                    max_wait = min_wait + rng.randint(*cfg.max_wait_range)
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
            slack = int(round(min_chain * cfg.deadline_slack_factor)) + cfg.num_jobs * 3
            deadline = release + slack

        jobs.append(
            Job(
                job_id=f"job_{j_idx + 1:02d}",
                ops=tuple(ops),
                release=release,
                deadline=deadline,
            )
        )

    inst_name = name or f"{preset}_{seed:02d}"
    return Instance(
        name=inst_name,
        preset=preset,
        seed=seed,
        capacities=dict(cfg.capacities),
        jobs=tuple(jobs),
    )


def generate_benchmark_suite(
    out_dir: str | Path,
    count_per_preset: int = 10,
    base_seed: int = 1000,
) -> list[Path]:
    """Generate a fixed benchmark set of small/medium/large instances in out_dir."""
    root = Path(out_dir)
    root.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for p_idx, preset in enumerate(("small", "medium", "large")):
        for idx in range(count_per_preset):
            seed = base_seed + p_idx * 100 + idx
            inst = generate_instance(
                preset=preset,
                seed=seed,
                name=f"{preset}_{idx:02d}",
            )
            path = root / f"{preset}_{idx:02d}.json"
            inst.save_json(path)
            written.append(path)
    return written
