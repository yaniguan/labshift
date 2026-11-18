"""labshift: synthetic lab scheduling and planning benchmark."""

from labshift.bench import BenchRow, format_benchmark_table, run_benchmark, solve_instance
from labshift.checker import CheckResult, Violation, check_schedule
from labshift.cpsat import solve_cpsat
from labshift.gantt import render_gantt
from labshift.generator import PRESETS, generate_benchmark_suite, generate_instance
from labshift.greedy import solve_greedy
from labshift.metrics import Metrics, compute_metrics
from labshift.models import INSTRUMENTS, Instance, Job, Operation, Schedule, ScheduledOp

__version__ = "0.1.0"

__all__ = [
    "BenchRow",
    "CheckResult",
    "INSTRUMENTS",
    "Instance",
    "Job",
    "Metrics",
    "Operation",
    "PRESETS",
    "Schedule",
    "ScheduledOp",
    "Violation",
    "check_schedule",
    "compute_metrics",
    "format_benchmark_table",
    "generate_benchmark_suite",
    "generate_instance",
    "render_gantt",
    "run_benchmark",
    "solve_cpsat",
    "solve_greedy",
    "solve_instance",
]
