"""Benchmark runner, paired solver comparison, and table formatter for labshift."""

from __future__ import annotations

import json
import re
import warnings
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from labshift.checker import check_schedule
from labshift.greedy import solve_greedy, solve_greedy_repair
from labshift.lns import solve_lns
from labshift.metrics import Metrics, compute_metrics
from labshift.models import Instance, Schedule

_PRESET_ORDER = {
    "small": 0,
    "medium": 1,
    "large": 2,
    "hard": 3,
    "planted_small": 4,
    "planted_medium": 5,
    "planted_large": 6,
    "planted_hard": 7,
    "planted": 8,
}
SOLVER_CHOICES: tuple[str, ...] = (
    "greedy",
    "greedy_repair",
    "lns",
    "cpsat",
    "pyjobshop",
)


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
            "lower_bound": m.lower_bound,
            "solver_bound": m.solver_bound,
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


def _preset_rank(stem: str) -> int:
    if stem.startswith("planted_"):
        parts = stem.split("_")
        key = "_".join(parts[:2])
        return _PRESET_ORDER.get(key, 8)
    return _PRESET_ORDER.get(stem.split("_")[0], 99)


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
            _preset_rank(p.stem),
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
    if solver == "lns":
        return solve_lns(
            instance,
            time_limit=time_limit,
            seed=0 if seed is None else seed,
        )
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
    if solver == "pyjobshop":
        try:
            from labshift.pyjobshop_bridge import solve_pyjobshop
        except ImportError as exc:
            raise ImportError(
                "Install pyjobshop>=0.0.9 to use the 'pyjobshop' solver."
            ) from exc
        return solve_pyjobshop(
            instance,
            time_limit=time_limit,
            num_workers=num_workers,
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


def compute_paired_summary(
    rows_a: Sequence[BenchRow],
    rows_b: Sequence[BenchRow],
) -> dict[str, Any]:
    """Compute paired makespan and flow differences on instances where BOTH solvers are feasible."""
    solver_a = rows_a[0].solver if rows_a else "solver_a"
    solver_b = rows_b[0].solver if rows_b else "solver_b"
    by_inst_a = {r.instance_name: r for r in rows_a}
    by_inst_b = {r.instance_name: r for r in rows_b}
    shared_names = [name for name in by_inst_a if name in by_inst_b]

    ms_a_list: list[int] = []
    ms_b_list: list[int] = []
    diffs: list[int] = []
    flow_diffs: list[float] = []
    a_better = 0
    b_better = 0
    ties = 0

    for name in shared_names:
        ra = by_inst_a[name]
        rb = by_inst_b[name]
        if (
            ra.metrics.feasible
            and rb.metrics.feasible
            and ra.metrics.makespan is not None
            and rb.metrics.makespan is not None
        ):
            ma = ra.metrics.makespan
            mb = rb.metrics.makespan
            ms_a_list.append(ma)
            ms_b_list.append(mb)
            diffs.append(ma - mb)
            if ra.metrics.mean_flow_time is not None and rb.metrics.mean_flow_time is not None:
                flow_diffs.append(ra.metrics.mean_flow_time - rb.metrics.mean_flow_time)
            if ma < mb:
                a_better += 1
            elif mb < ma:
                b_better += 1
            else:
                ties += 1

    k = len(diffs)
    return {
        "solver_a": solver_a,
        "solver_b": solver_b,
        "shared_total": len(shared_names),
        "joint_feasible_count": k,
        "mean_makespan_a": (sum(ms_a_list) / k) if k else None,
        "mean_makespan_b": (sum(ms_b_list) / k) if k else None,
        "mean_makespan_diff": (sum(diffs) / k) if k else None,
        "mean_flow_diff": (sum(flow_diffs) / len(flow_diffs)) if flow_diffs else None,
        "a_better_count": a_better,
        "b_better_count": b_better,
        "tie_count": ties,
    }


def format_paired_summary(
    rows_a: Sequence[BenchRow],
    rows_b: Sequence[BenchRow],
) -> str:
    """Format a one-line paired summary over jointly feasible instances."""
    stats = compute_paired_summary(rows_a, rows_b)
    sa = stats["solver_a"]
    sb = stats["solver_b"]
    k = stats["joint_feasible_count"]
    tot = stats["shared_total"]
    if k == 0:
        return f"Paired ({sa} vs {sb}): 0/{tot} jointly feasible instances."
    ma = stats["mean_makespan_a"]
    mb = stats["mean_makespan_b"]
    diff = stats["mean_makespan_diff"]
    ab = stats["a_better_count"]
    bb = stats["b_better_count"]
    ties = stats["tie_count"]
    return (
        f"Paired ({sa} vs {sb}, {k}/{tot} jointly feasible): "
        f"mean makespan {ma:.1f} vs {mb:.1f} "
        f"(diff {diff:+.1f}m; {sa}<{sb}: {ab}, {sb}<{sa}: {bb}, equal: {ties})"
    )


def _fmt_int(val: int | None) -> str:
    return "-" if val is None else str(val)


def _fmt_opt(val: float | None, spec: str) -> str:
    return "-" if val is None else format(val, spec)


def format_benchmark_table(
    rows: list[BenchRow],
    *,
    baseline_rows: Sequence[BenchRow] | None = None,
) -> str:
    """Format benchmark rows and feasible-only summary statistics as an ASCII table.

    Makespan, mean_flow, and gap% in the summary row are averaged exclusively over
    feasible rows (indicated by ``feas-only (k/n)`` in the summary preset column).
    When ``baseline_rows`` is given (or ``rows`` spans multiple solvers), appends a
    paired mean makespan difference summary over jointly feasible instances.
    """
    headers = (
        "instance",
        "preset",
        "status",
        "feas",
        "makespan",
        "tardiness",
        "mean_flow",
        "win_viol",
        "lb",
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
                str(m.lower_bound),
                _fmt_opt(m.best_bound, ".1f"),
                _fmt_opt(m.gap_pct, ".1f"),
                f"{m.solve_time_s:.3f}",
            )
        )

    summary: tuple[str, ...] | None = None
    if rows:
        n = len(rows)
        solved_count = sum(1 for r in rows if r.metrics.makespan is not None)
        feas_rows = [r for r in rows if r.metrics.feasible and r.metrics.makespan is not None]
        feas_count = sum(1 for r in rows if r.metrics.feasible)
        feas_ms = [
            r.metrics.makespan for r in feas_rows if r.metrics.makespan is not None
        ]
        feas_flow = [
            r.metrics.mean_flow_time
            for r in feas_rows
            if r.metrics.mean_flow_time is not None
        ]
        feas_gaps = [
            r.metrics.gap_pct for r in feas_rows if r.metrics.gap_pct is not None
        ]
        feas_lbs = [r.metrics.lower_bound for r in feas_rows]

        avg_ms = sum(feas_ms) / len(feas_ms) if feas_ms else None
        sum_tard = sum(r.metrics.total_tardiness for r in rows)
        avg_flow = sum(feas_flow) / len(feas_flow) if feas_flow else None
        sum_win = sum(r.metrics.window_violations for r in rows)
        avg_lb = sum(feas_lbs) / len(feas_lbs) if feas_lbs else None
        avg_gap = sum(feas_gaps) / len(feas_gaps) if feas_gaps else None
        tot_time = sum(r.metrics.solve_time_s for r in rows)

        summary = (
            f"ALL ({n})",
            f"feas-only ({feas_count}/{n})",
            f"{solved_count}/{n}",
            f"{feas_count}/{n}",
            _fmt_opt(avg_ms, ".1f"),
            str(sum_tard),
            _fmt_opt(avg_flow, ".1f"),
            str(sum_win),
            _fmt_opt(avg_lb, ".1f"),
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

    if baseline_rows is not None and rows:
        lines.append(format_paired_summary(rows, baseline_rows))
    elif rows:
        solvers_ordered: list[str] = []
        for r in rows:
            if r.solver not in solvers_ordered:
                solvers_ordered.append(r.solver)
        if len(solvers_ordered) >= 2:
            s_first = solvers_ordered[0]
            first_group = [r for r in rows if r.solver == s_first]
            for s_other in solvers_ordered[1:]:
                other_group = [r for r in rows if r.solver == s_other]
                lines.append(format_paired_summary(first_group, other_group))

    return "\n".join(lines)
