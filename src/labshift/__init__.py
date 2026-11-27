"""labshift: synthetic lab scheduling and planning benchmark."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from labshift.bench import (
    SOLVER_CHOICES,
    BenchRow,
    collect_instance_paths,
    compute_paired_summary,
    format_benchmark_table,
    format_paired_summary,
    run_benchmark,
    solve_instance,
)
from labshift.bounds import makespan_lower_bound, makespan_lower_bound_components
from labshift.checker import CheckResult, Violation, check_schedule
from labshift.gantt import render_gantt
from labshift.generator import (
    DEFAULT_SUITE_PRESETS,
    PLANTED_SUITE_PRESETS,
    PRESETS,
    PresetConfig,
    generate_benchmark_suite,
    generate_instance,
    generate_planted_instance,
)
from labshift.greedy import solve_greedy, solve_greedy_repair
from labshift.lns import solve_lns
from labshift.metrics import Metrics, compute_metrics
from labshift.models import (
    BASELINE_INSTRUMENTS,
    INSTRUMENTS,
    ORTHOGONAL_INSTRUMENTS,
    Instance,
    Job,
    Operation,
    Schedule,
    ScheduledOp,
)
from labshift.protocol import (
    STEPBOT_TIP_ACTION_SECONDS,
    SUPPORTED_STEPBOT_STEP_TYPES,
    export_workcell_queue,
    instance_from_protocols,
    job_from_protocol,
    job_from_stepbot_spec,
    load_protocol_steps,
)

if TYPE_CHECKING:
    from labshift.cpsat import compute_horizon_upper_bound, solve_cpsat
    from labshift.pyjobshop_bridge import solve_pyjobshop

__version__ = "0.1.0"

__all__ = [
    "BASELINE_INSTRUMENTS",
    "DEFAULT_SUITE_PRESETS",
    "INSTRUMENTS",
    "ORTHOGONAL_INSTRUMENTS",
    "PLANTED_SUITE_PRESETS",
    "PRESETS",
    "SOLVER_CHOICES",
    "STEPBOT_TIP_ACTION_SECONDS",
    "SUPPORTED_STEPBOT_STEP_TYPES",
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
    "compute_horizon_upper_bound",
    "compute_metrics",
    "compute_paired_summary",
    "export_workcell_queue",
    "format_benchmark_table",
    "format_paired_summary",
    "generate_benchmark_suite",
    "generate_instance",
    "generate_planted_instance",
    "instance_from_protocols",
    "job_from_protocol",
    "job_from_stepbot_spec",
    "load_protocol_steps",
    "makespan_lower_bound",
    "makespan_lower_bound_components",
    "render_gantt",
    "run_benchmark",
    "solve_cpsat",
    "solve_greedy",
    "solve_greedy_repair",
    "solve_instance",
    "solve_lns",
    "solve_pyjobshop",
]


def __getattr__(name: str) -> Any:
    if name in ("solve_cpsat", "compute_horizon_upper_bound"):
        import labshift.cpsat as _cpsat

        return getattr(_cpsat, name)
    if name == "solve_pyjobshop":
        import labshift.pyjobshop_bridge as _pj

        return _pj.solve_pyjobshop
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(
        list(globals().keys())
        + ["solve_cpsat", "compute_horizon_upper_bound", "solve_pyjobshop"]
    )
