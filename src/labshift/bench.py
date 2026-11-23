"""Benchmark runner and table formatter for labshift."""

from __future__ import annotations

import json
import re
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from labshift.checker import check_schedule
from labshift.greedy import solve_greedy, solve_greedy_repair
from labshift.metrics import Metrics, compute_metrics
from labshift.models import Instance, Schedule

_PRESET_ORDER = {"small": 0, "medium": 1, "large": 2, "hard": 3}
SOLVER_CHOICES: tuple[str, ...] = ("greedy", "greedy_repair", "cpsat")


@dataclass(frozen=True)
class BenchRow:
    """Single benchmark evaluation row."""

    instance_name: str
    preset: str
    solver: str
    status: str
    metrics: Metrics

    def to_dict(self) -> dict[str, Any]:
        """Serialize benchmark row to a JSON-compatible dictionary."""
        m = self.metrics
        return {
            "instance_name": self.instance_name,
            "preset": self.preset,
            "solver": self.solver,
            "status": self.status,
            "feasible": m.feasible,
            "makespan": m.makespan,
            "total_tardiness": m.total_tardiness,
            "mean_flow_time": m.mean_flow_time,
            "window_violations": m.window_violations,
            "deadline_violations": m.deadline_violations,
            "total_violations": m.total_violations,
            "best_bound": m.best_bound,
            "gap_pct": m.gap_pct,
            "solve_time_s": m.solve_time_s,
        }


def _natural_sort_key(text: str) -> tuple[tuple[int, int | str], ...]:
    parts: list[tuple[int, int | str]] = []
    for chunk in re.split(r"(\d+)", text):
        if not chunk:
            continue
        if chunk.isdigit():
            parts.append((0, int(chunk)))
        else:
            parts.append((1, chunk.lower()))
    return tuple(parts)


def _is_instance_json(path: Path) -> bool:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        warnings.warn(
            f"Skipping unreadable JSON file '{path}': {exc}",
            UserWarning,
            stacklevel=3,
        )
        return False
    if isinstance(raw, dict) and "capacities" in raw and "jobs" in raw:
        return True
    warnings.warn(
        f"Skipping non-instance JSON file '{path}' (missing 'capacities'/'jobs').",
        UserWarning,
        stacklevel=3,
    )
    return False


def collect_instance_paths(target: str | Path) -> list[Path]:
    """Collect JSON instance file paths from a file or directory (non-recursive)."""
    path = Path(target)
    if path.is_file():
        return [path]
    if not path.is_dir():
        raise FileNotFoundError(f"Benchmark path not found: {target}")

    candidates = [p for p in path.glob("*.json") if p.is_file() and _is_instance_json(p)]
    return sorted(
        candidates,
        key=lambda p: (
            _PRESET_ORDER.get(p.stem.split("_")[0], 99),
            _natural_sort_key(p.name),
        ),
    )


def solve_instance(
    instance: Instance,
    solver: str,
    time_limit: float = 5.0,
    *,
    num_workers: int = 4,
    seed: int | None = None,
    tie_break_flow: bool = False,
    hard_deadlines: bool = True,
) -> Schedule:
    """Dispatch an instance to the selected solver."""
    if solver == "greedy":
        return solve_greedy(instance)
    if solver == "greedy_repair":
        return solve_greedy_repair(instance)
    if solver == "cpsat":
        try:
            from labshift.cpsat import solve_cpsat
        except ImportError as exc:
            raise ImportError(
                "Install ortools>=9.8 to use the 'cpsat' solver."
            ) from exc
        return solve_cpsat(
            instance,
            time_limit=time_limit,
            num_workers=num_workers,
            seed=seed,
            tie_break_flow=tie_break_flow,
            hard_deadlines=hard_deadlines,
        )
    raise ValueError(
        f"Unknown solver '{solver}'. Expected one of {SOLVER_CHOICES}."
    )


def run_benchmark(
    target: str | Path,
    solver: str,
    time_limit: float = 5.0,
    *,
    num_workers: int = 4,
    seed: int | None = None,
    tie_break_flow: bool = False,
    hard_deadlines: bool = True,
    out_dir: str | Path | None = None,
) -> list[BenchRow]:
    """Run a solver on all instances under target and collect metrics."""
    rows: list[BenchRow] = []
    sched_dir = Path(out_dir) if out_dir is not None else None
    if sched_dir is not None:
        sched_dir.mkdir(parents=True, exist_ok=True)

    for path in collect_instance_paths(target):
        inst = Instance.load_json(path)
        sched = solve_instance(
            inst,
            solver=solver,
            time_limit=time_limit,
            num_workers=num_workers,
            seed=seed,
            tie_break_flow=tie_break_flow,
            hard_deadlines=hard_deadlines,
        )
        if sched_dir is not None:
            sched.save_json(sched_dir / f"{inst.name}.schedule.json")
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


def _fmt_int(val: int | None) -> str:
    return "-" if val is None else str(val)


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
                _fmt_int(m.makespan),
                str(m.total_tardiness),
                _fmt_opt(m.mean_flow_time, ".1f"),
                str(m.window_violations),
                _fmt_opt(m.best_bound, ".1f"),
                _fmt_opt(m.gap_pct, ".1f"),
                f"{m.solve_time_s:.3f}",
            )
        )

    summary: tuple[str, ...] | None = None
    if rows:
        n = len(rows)
        feas_count = sum(1 for r in rows if r.metrics.feasible)
        valid_ms = [r.metrics.makespan for r in rows if r.metrics.makespan is not None]
        valid_flow = [
            r.metrics.mean_flow_time
            for r in rows
            if r.metrics.mean_flow_time is not None
        ]
        solved_count = len(valid_ms)
        avg_ms = sum(valid_ms) / len(valid_ms) if valid_ms else None
        sum_tard = sum(r.metrics.total_tardiness for r in rows)
        avg_flow = sum(valid_flow) / len(valid_flow) if valid_flow else None
        sum_win = sum(r.metrics.window_violations for r in rows)
        gaps = [r.metrics.gap_pct for r in rows if r.metrics.gap_pct is not None]
        avg_gap = sum(gaps) / len(gaps) if gaps else None
        tot_time = sum(r.metrics.solve_time_s for r in rows)

        summary = (
            f"ALL ({n})",
            "-",
            f"{solved_count}/{n}",
            f"{feas_count}/{n}",
            _fmt_opt(avg_ms, ".1f"),
            str(sum_tard),
            _fmt_opt(avg_flow, ".1f"),
            str(sum_win),
            "-",
            _fmt_opt(avg_gap, ".1f"),
            f"{tot_time:.3f}",
        )

    widths = [len(h) for h in headers]
    for row in table_rows:
        for idx, cell in enumerate(row):
            widths[idx] = max(widths[idx], len(cell))
    if summary is not None:
        for idx, cell in enumerate(summary):
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
    if summary is not None:
        lines.append(sep)
        lines.append(fmt_line(summary))

    return "\n".join(lines)
