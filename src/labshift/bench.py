"""Benchmark runner and table formatter for labshift."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from labshift.checker import check_schedule
from labshift.cpsat import solve_cpsat
from labshift.greedy import solve_greedy
from labshift.metrics import Metrics, compute_metrics
from labshift.models import Instance, Schedule

_PRESET_ORDER = {"small": 0, "medium": 1, "large": 2}


@dataclass(frozen=True)
class BenchRow:
    """Single benchmark evaluation row."""

    instance_name: str
    preset: str
    solver: str
    status: str
    metrics: Metrics


def collect_instance_paths(target: str | Path) -> list[Path]:
    """Collect JSON instance file paths from a file or directory."""
    path = Path(target)
    if path.is_file():
        return [path]
    if not path.is_dir():
        raise FileNotFoundError(f"Benchmark path not found: {target}")
    files = sorted(
        path.rglob("*.json"),
        key=lambda p: (
            _PRESET_ORDER.get(p.stem.split("_")[0], 99),
            p.name,
        ),
    )
    return files


def solve_instance(
    instance: Instance,
    solver: str,
    time_limit: float = 5.0,
) -> Schedule:
    """Dispatch an instance to the selected solver."""
    if solver == "greedy":
        return solve_greedy(instance)
    if solver == "cpsat":
        return solve_cpsat(instance, time_limit=time_limit)
    raise ValueError(f"Unknown solver '{solver}'. Expected 'greedy' or 'cpsat'.")


def run_benchmark(
    target: str | Path,
    solver: str,
    time_limit: float = 5.0,
) -> list[BenchRow]:
    """Run a solver on all instances under target and collect metrics."""
    rows: list[BenchRow] = []
    for path in collect_instance_paths(target):
        inst = Instance.load_json(path)
        sched = solve_instance(inst, solver=solver, time_limit=time_limit)
        check = check_schedule(inst, sched)
        metrics = compute_metrics(inst, sched, check=check)
        rows.append(
            BenchRow(
                instance_name=inst.name,
                preset=inst.preset or "-",
                solver=solver,
                status=sched.status,
                metrics=metrics,
            )
        )
    return rows


def _fmt_opt(val: float | None, spec: str) -> str:
    return "-" if val is None else format(val, spec)


def format_benchmark_table(rows: list[BenchRow]) -> str:
    """Format benchmark rows and summary statistics as an ASCII table."""
    headers = (
        "instance",
        "preset",
        "status",
        "feas",
        "makespan",
        "tardiness",
        "mean_flow",
        "win_viol",
        "bound",
        "gap%",
        "time_s",
    )
    table_rows: list[tuple[str, ...]] = []
    for r in rows:
        m = r.metrics
        table_rows.append(
            (
                r.instance_name,
                r.preset,
                r.status,
                "yes" if m.feasible else "no",
                str(m.makespan),
                str(m.total_tardiness),
                f"{m.mean_flow_time:.1f}",
                str(m.window_violations),
                _fmt_opt(m.best_bound, ".1f"),
                _fmt_opt(m.gap_pct, ".1f"),
                f"{m.solve_time_s:.3f}",
            )
        )

    widths = [len(h) for h in headers]
    for row in table_rows:
        for idx, cell in enumerate(row):
            widths[idx] = max(widths[idx], len(cell))

    def fmt_line(cells: tuple[str, ...]) -> str:
        parts = []
        for idx, cell in enumerate(cells):
            if idx <= 3:
                parts.append(cell.ljust(widths[idx]))
            else:
                parts.append(cell.rjust(widths[idx]))
        return " | ".join(parts)

    sep = "-+-".join("-" * w for w in widths)
    lines = [fmt_line(headers), sep]
    for row in table_rows:
        lines.append(fmt_line(row))

    if rows:
        lines.append(sep)
        n = len(rows)
        feas_count = sum(1 for r in rows if r.metrics.feasible)
        avg_ms = sum(r.metrics.makespan for r in rows) / n
        sum_tard = sum(r.metrics.total_tardiness for r in rows)
        avg_flow = sum(r.metrics.mean_flow_time for r in rows) / n
        sum_win = sum(r.metrics.window_violations for r in rows)
        gaps = [r.metrics.gap_pct for r in rows if r.metrics.gap_pct is not None]
        avg_gap = sum(gaps) / len(gaps) if gaps else None
        tot_time = sum(r.metrics.solve_time_s for r in rows)

        summary = (
            f"ALL ({n})",
            "-",
            "-",
            f"{feas_count}/{n}",
            f"{avg_ms:.1f}",
            str(sum_tard),
            f"{avg_flow:.1f}",
            str(sum_win),
            "-",
            _fmt_opt(avg_gap, ".1f"),
            f"{tot_time:.3f}",
        )
        lines.append(fmt_line(summary))

    return "\n".join(lines)
