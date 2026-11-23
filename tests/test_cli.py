"""Tests for Gantt rendering, path collection, and CLI commands."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from labshift.bench import collect_instance_paths
from labshift.cli import main
from labshift.gantt import render_gantt
from labshift.generator import generate_instance
from labshift.greedy import solve_greedy_repair
from labshift.models import Instance, Job, Operation, Schedule, ScheduledOp

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_render_gantt_contains_jobs_and_metrics() -> None:
    inst = generate_instance(preset="small", seed=11)
    sched = solve_greedy_repair(inst)
    chart = render_gantt(inst, sched, width=40)
    assert inst.name in chart
    assert "Makespan:" in chart
    assert "job_01" in chart
    assert "Legend:" in chart


def test_render_gantt_guards_width_infeasible_and_collision_cells() -> None:
    inst = Instance(
        name="gantt_edge",
        capacities={"liquid_handler": 1, "plate_reader": 1},
        jobs=(
            Job(
                job_id="j1",
                release=10,
                ops=(
                    Operation("prep", "liquid_handler", duration=1),
                    Operation("read", "plate_reader", duration=1),
                ),
            ),
            Job(
                job_id="j2",
                release=0,
                ops=(Operation("prep", "liquid_handler", duration=100),),
            ),
        ),
    )

    with pytest.raises(ValueError, match=">= 10"):
        render_gantt(inst, Schedule("gantt_edge", "manual", ops=()), width=5)
    with pytest.raises(TypeError):
        render_gantt(inst, Schedule("gantt_edge", "manual", ops=()), width=True)  # type: ignore[arg-type]

    # Empty INFEASIBLE schedule does not crash and renders '-' for makespan/mean_flow
    infeas_chart = render_gantt(
        inst, Schedule("gantt_edge", "cpsat", ops=(), status="INFEASIBLE"), width=20
    )
    assert "Makespan: -" in infeas_chart
    assert "MeanFlow: -" in infeas_chart
    assert "10" in infeas_chart.splitlines()[-1]

    # Two rapid ops [10, 11) and [11, 12) on horizon=100, width=20 map to column 2 -> collision '*'
    colliding = Schedule(
        "gantt_edge",
        "manual",
        ops=(
            ScheduledOp("j1", 0, "liquid_handler", 10, 11, "prep"),
            ScheduledOp("j1", 1, "plate_reader", 11, 12, "read"),
            ScheduledOp("j2", 0, "liquid_handler", 0, 100, "prep"),
        ),
    )
    col_chart = render_gantt(inst, colliding, width=20)
    j1_line = next(line for line in col_chart.splitlines() if line.startswith("j1"))
    assert "*" in j1_line


def test_cli_pure_pipeline_generate_solve_check_bench_and_gantt(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    inst_path = tmp_path / "inst.json"
    sched_path = tmp_path / "sched.json"
    bench_json = tmp_path / "bench_report.json"
    sched_dir = tmp_path / "saved_schedules"

    assert main(["generate", "--preset", "medium", "--seed", "1101", "--out", str(inst_path)]) == 0
    assert inst_path.exists()
    capsys.readouterr()

    assert (
        main(["solve", str(inst_path), "--solver", "greedy_repair", "--out", str(sched_path)]) == 0
    )
    assert sched_path.exists()
    solve_out = capsys.readouterr().out
    assert "feasible=True" in solve_out

    assert main(["check", str(inst_path), str(sched_path)]) == 0
    chk_out = capsys.readouterr().out
    assert "feasible=True" in chk_out

    assert main(["gantt", "--schedule", str(sched_path), "--width", "40", str(inst_path)]) == 0
    gantt_out = capsys.readouterr().out
    assert "Legend:" in gantt_out

    assert (
        main(
            [
                "bench",
                "--solver",
                "greedy_repair",
                "--out-dir",
                str(sched_dir),
                "--json",
                str(bench_json),
                str(inst_path),
            ]
        )
        == 0
    )
    bench_out = capsys.readouterr().out
    assert "ALL (1)" in bench_out
    report = json.loads(bench_json.read_text(encoding="utf-8"))
    assert report["feasible_count"] == 1
    assert (sched_dir / "medium_1101.schedule.json").exists()


def test_cli_generate_suite_default_seed_1000(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    suite_dir = tmp_path / "suite"
    assert main(["generate", "--suite-dir", str(suite_dir), "--count", "1"]) == 0
    capsys.readouterr()
    gen_s00 = Instance.load_json(suite_dir / "small_00.json")
    ref_s00 = Instance.load_json(REPO_ROOT / "benchmarks" / "small_00.json")
    assert gen_s00 == ref_s00


def test_collect_instance_paths_skips_non_instance_jsons_and_sorts_naturally(
    tmp_path: Path,
) -> None:
    inst_10 = generate_instance("small", seed=10, name="small_10")
    inst_2 = generate_instance("small", seed=2, name="small_2")
    inst_10.save_json(tmp_path / "small_10.json")
    inst_2.save_json(tmp_path / "small_2.json")

    # Save a non-instance Schedule JSON in the same directory and a nested JSON in a subdir
    sched = solve_greedy_repair(inst_2)
    sched.save_json(tmp_path / "saved_schedule.json")
    nested_dir = tmp_path / "nested"
    nested_dir.mkdir()
    inst_2.save_json(nested_dir / "small_99.json")

    with pytest.warns(UserWarning, match="Skipping non-instance JSON"):
        paths = collect_instance_paths(tmp_path)

    assert [p.name for p in paths] == ["small_2.json", "small_10.json"]


@pytest.mark.parametrize(
    "bad_args",
    [
        ["bench", "--solver", "greedy", "--time-limit", "0", "inst.json"],
        ["bench", "--solver", "greedy", "--time-limit", "-2.5", "inst.json"],
        ["bench", "--solver", "greedy", "--workers", "0", "inst.json"],
        ["generate", "--suite-dir", "out", "--count", "0"],
        ["generate", "--suite-dir", "out", "--count", "-3"],
        ["gantt", "--width", "9", "inst.json"],
        ["gantt", "--width", "0", "inst.json"],
    ],
)
def test_cli_numeric_flag_validation(bad_args: list[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(bad_args)
    assert exc_info.value.code == 2


def test_cli_cpsat_solve_and_soft_deadlines(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    pytest.importorskip("ortools")
    inst = Instance(
        name="cli_soft",
        capacities={"liquid_handler": 1},
        jobs=(
            Job(
                job_id="j1",
                release=0,
                deadline=5,
                ops=(Operation("prep", "liquid_handler", duration=10),),
            ),
        ),
    )
    inst_path = tmp_path / "cli_soft.json"
    sched_path = tmp_path / "cli_soft_sched.json"
    inst.save_json(inst_path)

    # Hard deadlines -> exit code 1 (INFEASIBLE)
    assert main(["solve", str(inst_path), "--solver", "cpsat", "--time-limit", "2"]) == 1
    capsys.readouterr()

    # Soft deadlines -> exit code 0 (OPTIMAL with tardiness)
    assert (
        main(
            [
                "solve",
                str(inst_path),
                "--solver",
                "cpsat",
                "--time-limit",
                "2",
                "--workers",
                "1",
                "--seed",
                "0",
                "--tie-break-flow",
                "--soft-deadlines",
                "--out",
                str(sched_path),
            ]
        )
        == 0
    )
    loaded = Schedule.load_json(sched_path)
    assert loaded.status == "OPTIMAL"
    assert len(loaded.ops) == 1
