"""Command-line interface for labshift."""

from __future__ import annotations

import argparse
from pathlib import Path

from labshift.bench import format_benchmark_table, run_benchmark, solve_instance
from labshift.checker import check_schedule
from labshift.gantt import render_gantt
from labshift.generator import PRESETS, generate_benchmark_suite, generate_instance
from labshift.metrics import compute_metrics
from labshift.models import Instance, Schedule


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="labshift",
        description="Synthetic lab scheduling and planning benchmark.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    bench_p = sub.add_parser("bench", help="Run a solver on a benchmark file or directory.")
    bench_p.add_argument("target", help="Path to a JSON instance or directory of instances.")
    bench_p.add_argument(
        "--solver",
        choices=("greedy", "cpsat"),
        required=True,
        help="Solver to evaluate.",
    )
    bench_p.add_argument(
        "--time-limit",
        type=float,
        default=5.0,
        help="Time limit in seconds per instance for cpsat (default: 5.0).",
    )

    gantt_p = sub.add_parser("gantt", help="Print an ASCII Gantt chart for one instance.")
    gantt_p.add_argument("instance", help="Path to instance JSON file.")
    gantt_p.add_argument(
        "--solver",
        choices=("greedy", "cpsat"),
        default="cpsat",
        help="Solver to run if --schedule is not given (default: cpsat).",
    )
    gantt_p.add_argument(
        "--time-limit",
        type=float,
        default=5.0,
        help="Time limit in seconds for cpsat (default: 5.0).",
    )
    gantt_p.add_argument("--schedule", help="Optional precomputed schedule JSON file.")
    gantt_p.add_argument("--width", type=int, default=60, help="Chart width in characters.")

    gen_p = sub.add_parser("generate", help="Generate synthetic scheduling instance(s).")
    gen_p.add_argument(
        "--preset",
        choices=tuple(PRESETS),
        default="small",
        help="Instance size preset (default: small).",
    )
    gen_p.add_argument("--seed", type=int, default=0, help="RNG seed (default: 0).")
    gen_p.add_argument("--out", help="Output JSON file path for a single instance.")
    gen_p.add_argument(
        "--suite-dir",
        help="Output directory to write a full small/medium/large benchmark suite.",
    )
    gen_p.add_argument(
        "--count",
        type=int,
        default=10,
        help="Instances per preset when using --suite-dir (default: 10).",
    )

    chk_p = sub.add_parser("check", help="Validate a schedule JSON against an instance JSON.")
    chk_p.add_argument("instance", help="Path to instance JSON file.")
    chk_p.add_argument("schedule", help="Path to schedule JSON file.")

    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "bench":
        rows = run_benchmark(args.target, solver=args.solver, time_limit=args.time_limit)
        print(format_benchmark_table(rows))
        return 0

    if args.command == "gantt":
        inst = Instance.load_json(args.instance)
        if args.schedule:
            sched = Schedule.load_json(args.schedule)
        else:
            sched = solve_instance(inst, solver=args.solver, time_limit=args.time_limit)
        print(render_gantt(inst, sched, width=args.width))
        return 0

    if args.command == "generate":
        if args.suite_dir:
            written = generate_benchmark_suite(
                args.suite_dir,
                count_per_preset=args.count,
                base_seed=args.seed,
            )
            print(f"Generated {len(written)} instances in {Path(args.suite_dir)}")
            return 0
        inst = generate_instance(preset=args.preset, seed=args.seed)
        if args.out:
            inst.save_json(args.out)
            print(f"Wrote {args.out}")
        else:
            import json

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
