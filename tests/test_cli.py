"""Tests for Gantt rendering and CLI commands."""

from __future__ import annotations

from pathlib import Path

from labshift.cli import main
from labshift.cpsat import solve_cpsat
from labshift.gantt import render_gantt
from labshift.generator import generate_instance


def test_render_gantt_contains_jobs_and_metrics() -> None:
    inst = generate_instance(preset="small", seed=11)
    sched = solve_cpsat(inst, time_limit=5.0)
    chart = render_gantt(inst, sched, width=40)
    assert inst.name in chart
    assert "Makespan:" in chart
    assert "job_01" in chart
    assert "Legend:" in chart


def test_cli_generate_bench_gantt_and_check(
    tmp_path: Path,
    capsys,
) -> None:
    inst_path = tmp_path / "inst.json"
    assert main(["generate", "--preset", "small", "--seed", "3", "--out", str(inst_path)]) == 0
    assert inst_path.exists()

    assert main(["bench", "--solver", "greedy", str(inst_path)]) == 0
    bench_out = capsys.readouterr().out
    assert "small_03" in bench_out
    assert "ALL (1)" in bench_out

    assert main(["gantt", "--solver", "cpsat", "--time-limit", "2", str(inst_path)]) == 0
    gantt_out = capsys.readouterr().out
    assert "Legend:" in gantt_out

    sched = solve_cpsat(generate_instance(preset="small", seed=3), time_limit=2.0)
    sched_path = tmp_path / "sched.json"
    sched.save_json(sched_path)
    assert main(["check", str(inst_path), str(sched_path)]) == 0
    chk_out = capsys.readouterr().out
    assert "feasible=True" in chk_out
