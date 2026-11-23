"""Command-line interface for labshift."""

from __future__ import annotations

import argparse
import json
import math
from collections.abc import Callable
from pathlib import Path

from labshift.bench import (
    SOLVER_CHOICES,
    format_benchmark_table,
    run_benchmark,
    solve_instance,
)
from labshift.checker import check_schedule
from labshift.gantt import render_gantt
from labshift.generator import PRESETS, generate_benchmark_suite, generate_instance
from labshift.metrics import compute_metrics
from labshift.models import Instance, Schedule


def _positive_float(raw: str) -> float:
    try:
        val = float(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid float value: {raw!r}") from exc
    if not math.isfinite(val) or val <= 0.0:
        raise argparse.ArgumentTypeError(f"must be a finite number > 0, got {raw!r}")
    return val


def _min_int(minimum: int) -> Callable[[str], int]:
    def _validator(raw: str) -> int:
        try:
            val = int(raw)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"invalid int value: {raw!r}") from exc
        if val < minimum:
            raise argparse.ArgumentTypeError(f"must be >= {minimum}, got {val}")
        return val

    return _validator


def _add_cpsat_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--time-limit",
        type=_positive_float,
        default=5.0,
        help="Time limit in seconds (> 0) for cpsat (default: 5.0).",
    )
    parser.add_argument(
        "--workers",
        type=_min_int(1),
        default=4,
        help="Number of CP-SAT search workers (>= 1, default: 4).",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="Optional random seed for CP-SAT search determinism.",
    )
    parser.add_argument(
        "--tie-break-flow",
        action="store_true",
        help="Minimize total job flow time as a secondary tie-breaker in cpsat.",
    )
    parser.add_argument(
        "--soft-deadlines",
        action="store_true",
        help="Relax hard job deadlines in cpsat and penalize total tardiness instead.",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="labshift",
        description="Synthetic lab scheduling and planning benchmark.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    solve_p = sub.add_parser("solve", help="Solve one instance and output or save a Schedule JSON.")
    solve_p.add_argument("instance", help="Path to instance JSON file.")
    solve_p.add_argument(
        "--solver",
        choices=SOLVER_CHOICES,
        default="cpsat",
        help="Solver to run (default: cpsat).",
    )
    _add_cpsat_options(solve_p)
    solve_p.add_argument("--out", help="Output file path for solved Schedule JSON.")

    bench_p = sub.add_parser("bench", help="Run a solver on a benchmark file or directory.")
    bench_p.add_argument("target", help="Path to a JSON instance or directory of instances.")
    bench_p.add_argument(
        "--solver",
        choices=SOLVER_CHOICES,
        required=True,
        help="Solver to evaluate.",
    )
    _add_cpsat_options(bench_p)
    bench_p.add_argument(
        "--out-dir",
        help="Optional directory to save per-instance Schedule JSON files.",
    )
    bench_p.add_argument(
        "--json",
        dest="json_out",
        help="Optional output path to save benchmark summary metrics as JSON.",
    )

    gantt_p = sub.add_parser("gantt", help="Print an ASCII Gantt chart for one instance.")
    gantt_p.add_argument("instance", help="Path to instance JSON file.")
    gantt_p.add_argument(
        "--solver",
        choices=SOLVER_CHOICES,
        default="cpsat",
        help="Solver to run if --schedule is not given (default: cpsat).",
    )
    _add_cpsat_options(gantt_p)
    gantt_p.add_argument("--schedule", help="Optional precomputed schedule JSON file.")
    gantt_p.add_argument(
        "--save-schedule",
        help="Optional output path to save the solved schedule JSON.",
    )
    gantt_p.add_argument(
        "--width",
        type=_min_int(10),
        default=60,
        help="Chart width in characters (>= 10, default: 60).",
    )

    gen_p = sub.add_parser("generate", help="Generate synthetic scheduling instance(s).")
    gen_p.add_argument(
        "--preset",
        choices=tuple(PRESETS),
        default="small",
        help="Instance size preset (default: small).",
    )
    gen_p.add_argument(
        "--seed",
        type=int,
        default=None,
        help="RNG seed. Default: 1000 with --suite-dir, else 0.",
    )
    gen_p.add_argument("--out", help="Output JSON file path for a single instance.")
    gen_p.add_argument(
        "--suite-dir",
        help="Output directory to write a full small/medium/large benchmark suite.",
    )
    gen_p.add_argument(
        "--count",
        type=_min_int(1),
        default=10,
        help="Instances per preset when using --suite-dir (>= 1, default: 10).",
    )
    gen_p.add_argument(
        "--ensure-feasible",
        action="store_true",
        help="Verify generated instances are feasible (widening tight windows if needed).",
    )

    chk_p = sub.add_parser("check", help="Validate a schedule JSON against an instance JSON.")
    chk_p.add_argument("instance", help="Path to instance JSON file.")
    chk_p.add_argument("schedule", help="Path to schedule JSON file.")

    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "solve":
        inst = Instance.load_json(args.instance)
        sched = solve_instance(
            inst,
            solver=args.solver,
            time_limit=args.time_limit,
            num_workers=args.workers,
            seed=args.seed,
            tie_break_flow=args.tie_break_flow,
            hard_deadlines=not args.soft_deadlines,
        )
        check = check_schedule(inst, sched)
        metrics = compute_metrics(inst, sched, check=check)
        if args.out:
            sched.save_json(args.out)
            print(
                f"Wrote {args.out} (status={sched.status}, "
                f"feasible={check.feasible}, makespan={metrics.makespan})"
            )
        else:
            print(json.dumps(sched.to_dict(), indent=2))
        return 0 if sched.status not in ("INFEASIBLE", "UNKNOWN", "MODEL_INVALID") else 1

    if args.command == "bench":
        rows = run_benchmark(
            args.target,
            solver=args.solver,
            time_limit=args.time_limit,
            num_workers=args.workers,
            seed=args.seed,
            tie_break_flow=args.tie_break_flow,
            hard_deadlines=not args.soft_deadlines,
            out_dir=args.out_dir,
        )
        print(format_benchmark_table(rows))
        if args.json_out:
            out_path = Path(args.json_out)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "solver": args.solver,
                "count": len(rows),
                "feasible_count": sum(1 for r in rows if r.metrics.feasible),
                "solved_count": sum(1 for r in rows if r.metrics.makespan is not None),
                "rows": [r.to_dict() for r in rows],
            }
            out_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return 0

    if args.command == "gantt":
        inst = Instance.load_json(args.instance)
        if args.schedule:
            sched = Schedule.load_json(args.schedule)
        else:
            sched = solve_instance(
                inst,
                solver=args.solver,
                time_limit=args.time_limit,
                num_workers=args.workers,
                seed=args.seed,
                tie_break_flow=args.tie_break_flow,
                hard_deadlines=not args.soft_deadlines,
            )
        if args.save_schedule:
            sched.save_json(args.save_schedule)
        print(render_gantt(inst, sched, width=args.width))
        return 0

    if args.command == "generate":
        if args.suite_dir:
            written = generate_benchmark_suite(
                args.suite_dir,
                count_per_preset=args.count,
                base_seed=1000 if args.seed is None else args.seed,
                ensure_feasible=args.ensure_feasible,
            )
            print(f"Generated {len(written)} instances in {Path(args.suite_dir)}")
            return 0
        inst = generate_instance(
            preset=args.preset,
            seed=0 if args.seed is None else args.seed,
            ensure_feasible=args.ensure_feasible,
        )
        if args.out:
            inst.save_json(args.out)
            print(f"Wrote {args.out}")
        else:
            print(json.dumps(inst.to_dict(), indent=2))
        return 0

    if args.command == "check":
        inst = Instance.load_json(args.instance)
        sched = Schedule.load_json(args.schedule)
        result = check_schedule(inst, sched)
        metrics = compute_metrics(inst, sched, check=result)
        print(
            f"feasible={result.feasible} makespan={metrics.makespan} "
            f"violations={metrics.total_violations}"
        )
        for v in result.violations:
            print(f"- [{v.kind}] {v.message}")
        return 0 if result.feasible else 1

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
