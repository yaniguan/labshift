"""labshift: synthetic lab scheduling and planning benchmark."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from labshift.bench import (
    SOLVER_CHOICES,
    BenchRow,
    collect_instance_paths,
    format_benchmark_table,
    run_benchmark,
    solve_instance,
)
from labshift.checker import CheckResult, Violation, check_schedule
from labshift.gantt import render_gantt
from labshift.generator import (
    DEFAULT_SUITE_PRESETS,
    PRESETS,
    PresetConfig,
    generate_benchmark_suite,
    generate_instance,
)
from labshift.greedy import solve_greedy, solve_greedy_repair
from labshift.metrics import Metrics, compute_metrics
from labshift.models import INSTRUMENTS, Instance, Job, Operation, Schedule, ScheduledOp
from labshift.protocol import (
    export_workcell_queue,
    instance_from_protocols,
    job_from_protocol,
    load_protocol_steps,
)

if TYPE_CHECKING:
    from labshift.cpsat import solve_cpsat

__version__ = "0.1.0"

__all__ = [
    "DEFAULT_SUITE_PRESETS",
    "INSTRUMENTS",
    "PRESETS",
    "SOLVER_CHOICES",
    "BenchRow",
    "CheckResult",
    "Instance",
    "Job",
    "Metrics",
    "Operation",
    "PresetConfig",
    "Schedule",
    "ScheduledOp",
    "Violation",
    "check_schedule",
    "collect_instance_paths",
    "compute_metrics",
    "export_workcell_queue",
    "format_benchmark_table",
    "generate_benchmark_suite",
    "generate_instance",
    "instance_from_protocols",
    "job_from_protocol",
    "load_protocol_steps",
    "render_gantt",
    "run_benchmark",
    "solve_cpsat",
    "solve_greedy",
    "solve_greedy_repair",
    "solve_instance",
]


def __getattr__(name: str) -> Any:
    if name == "solve_cpsat":
        from labshift.cpsat import solve_cpsat

        return solve_cpsat
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(list(globals().keys()) + ["solve_cpsat"])
