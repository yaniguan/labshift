"""ASCII Gantt chart renderer for labshift schedules."""

from __future__ import annotations

from labshift.checker import check_schedule
from labshift.metrics import compute_metrics
from labshift.models import Instance, Schedule

_INST_SYMBOL: dict[str, str] = {
    "liquid_handler": "L",
    "incubator": "I",
    "plate_reader": "R",
    "centrifuge": "C",
    "washer": "W",
}


def render_gantt(
    instance: Instance,
    schedule: Schedule,
    width: int = 60,
) -> str:
    """Render a job-oriented ASCII Gantt chart with legend and metrics."""
    check = check_schedule(instance, schedule)
    metrics = compute_metrics(instance, schedule, check=check)
    horizon = max(metrics.makespan, 1)
    by_key = schedule.by_key()

    label_w = max((len(j.job_id) for j in instance.jobs), default=6)
    lines: list[str] = [
        f"Instance: {instance.name} | Solver: {schedule.solver} ({schedule.status})",
        (
            f"Makespan: {metrics.makespan}m | Feasible: {metrics.feasible} | "
            f"Tardiness: {metrics.total_tardiness}m | MeanFlow: {metrics.mean_flow_time:.1f}m | "
            f"WinViol: {metrics.window_violations}"
        ),
        "Legend: [L] liquid_handler  [I] incubator  [R] plate_reader  [C] centrifuge  [W] washer",
        "",
    ]

    for job in instance.jobs:
        bar = ["."] * width
        if job.release > 0:
            rel_col = min(width - 1, int(job.release * width / horizon))
            for c in range(rel_col):
                bar[c] = " "
        for idx, _ in enumerate(job.ops):
            sop = by_key.get((job.job_id, idx))
            if sop is None:
                continue
            sym = _INST_SYMBOL.get(sop.instrument, "?")
            c0 = min(width - 1, max(0, int(sop.start * width / horizon)))
            c1 = min(width, max(c0 + 1, int((sop.end * width + horizon - 1) // horizon)))
            for c in range(c0, c1):
                bar[c] = sym
        spans = ", ".join(
            f"{_INST_SYMBOL.get(op.instrument, '?')}[{op.start}-{op.end}]"
            for op in (
                by_key[(job.job_id, idx)]
                for idx in range(len(job.ops))
                if (job.job_id, idx) in by_key
            )
        )
        lines.append(f"{job.job_id:<{label_w}} |{''.join(bar)}| {spans}")

    axis = [" "] * width
    ticks = 5
    for step in range(ticks + 1):
        t_val = round(horizon * step / ticks)
        pos = min(width - 1, round((width - 1) * step / ticks))
        label = str(t_val)
        start_pos = min(pos, max(0, width - len(label)))
        for k, ch in enumerate(label):
            if start_pos + k < width:
                axis[start_pos + k] = ch
    lines.append(f"{'t(m)':<{label_w}} |{''.join(axis)}|")
    return "\n".join(lines)
