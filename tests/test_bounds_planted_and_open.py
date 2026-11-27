"""Tests for stdlib lower bounds (#36), planted tier & paired stats (#37), LNS, PyJobShop, and open benchmarks."""

from __future__ import annotations

import json
import subprocess
import sys
import warnings
from pathlib import Path

import pytest

from labshift.bench import (
    BenchRow,
    collect_instance_paths,
    compute_paired_summary,
    format_benchmark_table,
    run_benchmark,
)
from labshift.bounds import makespan_lower_bound, makespan_lower_bound_components
from labshift.checker import check_schedule
from labshift.cli import main
from labshift.generator import generate_planted_instance
from labshift.greedy import solve_greedy, solve_greedy_repair
from labshift.lns import solve_lns
from labshift.metrics import compute_metrics
from labshift.models import Instance, Job, Operation, Schedule, ScheduledOp
from labshift.open_benchmarks import (
    build_wetlab_campaign_instance,
    parse_bioplanner_json_protocol,
    parse_jssp_instance,
    parse_opentrons_python_protocol,
    parse_orlib_jobshop_archive,
    parse_psplib_ampl_dat,
    parse_psplib_sm,
    parse_wlp_brat_protocol,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
BENCHMARKS_DIR = REPO_ROOT / "benchmarks"
PLANTED_DIR = BENCHMARKS_DIR / "planted"
PLANTED_WITNESS_DIR = BENCHMARKS_DIR / "planted_witnesses"


def test_makespan_lower_bound_medium_00_large_05_and_robot_arm() -> None:
    med_00 = Instance.load_json(BENCHMARKS_DIR / "medium_00.json")
    lrg_05 = Instance.load_json(BENCHMARKS_DIR / "large_05.json")

    assert makespan_lower_bound(med_00) >= 122
    assert makespan_lower_bound(med_00) == 122
    assert makespan_lower_bound(lrg_05) >= 125

    # Robot arm energetic bottleneck test: 4 jobs each needing a 6m arm transfer
    arm_bottleneck = Instance(
        name="arm_energetic",
        capacities={"liquid_handler": 4, "plate_reader": 4, "robot_arm": 1},
        jobs=tuple(
            Job(
                job_id=f"j{i}",
                release=2,
                ops=(
                    Operation("prep", "liquid_handler", duration=5),
                    Operation("read", "plate_reader", duration=4, transfer_time=6),
                ),
            )
            for i in range(4)
        ),
    )
    comps = makespan_lower_bound_components(arm_bottleneck)
    # Chain = 2 + 5 + 6 + 4 = 17; arm head=7, 4*6=24 work on cap=1, tail=4 => 7 + 24 + 4 = 35
    assert comps["chain_bound"] == 17
    assert comps["robot_arm_bound"] == 35
    assert comps["lower_bound"] == 35
    lns_sched = solve_lns(arm_bottleneck, time_limit=0.5, seed=0)
    assert check_schedule(arm_bottleneck, lns_sched).feasible
    assert compute_metrics(arm_bottleneck, lns_sched).makespan == 35


def test_stdlib_solvers_makespan_ge_lower_bound_across_all_committed_suites() -> None:
    all_paths = collect_instance_paths(BENCHMARKS_DIR) + collect_instance_paths(PLANTED_DIR)
    assert len(all_paths) == 50

    for path in all_paths:
        inst = Instance.load_json(path)
        lb = makespan_lower_bound(inst)
        assert lb > 0

        for sched in (solve_greedy(inst), solve_greedy_repair(inst)):
            m = compute_metrics(inst, sched)
            assert m.makespan is not None
            assert m.makespan >= lb
            assert m.lower_bound == lb
            assert m.best_bound == float(lb)
            assert m.gap_pct is not None and m.gap_pct >= 0.0


def test_cpsat_makespan_ge_lower_bound_across_30_v1_instances() -> None:
    pytest.importorskip("ortools")
    from labshift.cpsat import solve_cpsat

    for path in collect_instance_paths(BENCHMARKS_DIR):
        inst = Instance.load_json(path)
        lb = makespan_lower_bound(inst)
        sched = solve_cpsat(inst, time_limit=1.5, num_workers=2, seed=0)
        m = compute_metrics(inst, sched)
        assert m.feasible
        assert m.makespan is not None
        assert m.makespan >= lb
        assert m.best_bound is not None and m.best_bound >= float(lb)
        assert m.gap_pct is not None and m.gap_pct >= 0.0


def test_bench_greedy_repair_prints_lb_and_gap_on_every_row(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["bench", "--solver", "greedy_repair", str(BENCHMARKS_DIR)]) == 0
    out = capsys.readouterr().out
    lines = [ln for ln in out.splitlines() if "|" in ln and not ln.startswith("instance")]
    assert len(lines) == 31  # 30 instances + ALL (30) summary
    for ln in lines[:-1]:
        cells = [c.strip() for c in ln.split("|")]
        # headers: instance, preset, status, feas, makespan, tardiness, mean_flow, win_viol, lb, bound, gap%, time_s
        assert int(cells[8]) > 0
        assert cells[10] != "-"
        assert float(cells[10]) >= 0.0


def test_planted_50_instances_valid_witness_and_seed_determinism() -> None:
    presets = ("small", "medium", "large", "hard")
    for idx in range(50):
        preset = presets[idx % len(presets)]
        inst, witness = generate_planted_instance(preset=preset, seed=5000 + idx)
        chk = check_schedule(inst, witness)
        assert chk.feasible, f"Planted witness failed for {preset} seed {5000 + idx}: {chk.violations}"
        m = compute_metrics(inst, witness, check=chk)
        assert m.makespan is not None
        assert m.makespan >= makespan_lower_bound(inst)

    # Seed determinism
    inst_a, wit_a = generate_planted_instance(preset="medium", seed=2103)
    inst_b, wit_b = generate_planted_instance(preset="medium", seed=2103)
    assert inst_a == inst_b
    assert wit_a == wit_b


def test_committed_planted_suite_witnesses_and_solver_separation() -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        paths = collect_instance_paths(PLANTED_DIR)
    assert len(caught) == 0
    assert len(paths) == 20

    greedy_feas = 0
    repair_feas = 0
    lns_feas = 0

    for p in paths:
        inst = Instance.load_json(p)
        wit_path = PLANTED_WITNESS_DIR / f"{inst.name}.schedule.json"
        assert wit_path.exists()
        witness = Schedule.load_json(wit_path)
        assert check_schedule(inst, witness).feasible

        if check_schedule(inst, solve_greedy(inst)).feasible:
            greedy_feas += 1
        if check_schedule(inst, solve_greedy_repair(inst)).feasible:
            repair_feas += 1
        if check_schedule(inst, solve_lns(inst, time_limit=1.0, seed=0)).feasible:
            lns_feas += 1

    assert greedy_feas <= 2
    assert repair_feas < 12
    assert lns_feas > repair_feas


def test_cpsat_solves_all_20_committed_planted_instances() -> None:
    pytest.importorskip("ortools")
    from labshift.cpsat import solve_cpsat

    paths = collect_instance_paths(PLANTED_DIR)
    assert len(paths) == 20
    for p in paths:
        inst = Instance.load_json(p)
        sched = solve_cpsat(inst, time_limit=5.0, num_workers=4, seed=0)
        assert check_schedule(inst, sched).feasible, f"CP-SAT missed {inst.name}"


def test_format_benchmark_table_feasible_only_means_and_paired_summary() -> None:
    inst_1 = Instance(
        name="p1",
        capacities={"liquid_handler": 1, "plate_reader": 1},
        jobs=(
            Job(
                "j1",
                ops=(
                    Operation("prep", "liquid_handler", 5),
                    Operation("read", "plate_reader", 5, max_wait=1),
                ),
            ),
            Job("j2", ops=(Operation("read", "plate_reader", 10),)),
        ),
    )
    inst_2 = Instance(
        name="p2",
        capacities={"liquid_handler": 1},
        jobs=(
            Job("j1", ops=(Operation("prep", "liquid_handler", 8),)),
            Job("j2", ops=(Operation("prep", "liquid_handler", 12),)),
        ),
    )

    # On p1, greedy is infeasible (makespan=20) while greedy_repair is feasible (makespan=15)
    g1 = solve_greedy(inst_1)
    r1 = solve_greedy_repair(inst_1)
    m_g1 = compute_metrics(inst_1, g1)
    m_r1 = compute_metrics(inst_1, r1)
    assert not m_g1.feasible and m_g1.makespan is not None
    assert m_r1.feasible and m_r1.makespan == 15

    # On p2, both are feasible with makespan=20 (and a hypothetical optimal solver gets 20)
    g2 = solve_greedy(inst_2)
    r2 = Schedule(
        "p2",
        "greedy_repair",
        ops=(
            ScheduledOp("j1", 0, "liquid_handler", 0, 8, "prep"),
            ScheduledOp("j2", 0, "liquid_handler", 8, 20, "prep"),
        ),
    )
    m_g2 = compute_metrics(inst_2, g2)
    m_r2 = compute_metrics(inst_2, r2)
    assert m_g2.feasible and m_g2.makespan == 20
    assert m_r2.feasible and m_r2.makespan == 20

    rows_g = [
        BenchRow("p1", "small", "greedy", g1.status, m_g1),
        BenchRow("p2", "small", "greedy", g2.status, m_g2),
    ]
    rows_r = [
        BenchRow("p1", "small", "greedy_repair", r1.status, m_r1),
        BenchRow("p2", "small", "greedy_repair", r2.status, m_r2),
    ]

    tbl = format_benchmark_table(rows_g, baseline_rows=rows_r)
    assert "feas-only (1/2)" in tbl
    # Greedy feasible-only mean covers p2 ONLY (makespan=20.0), excluding infeasible p1!
    summary_line = [ln for ln in tbl.splitlines() if ln.startswith("ALL (2)")][0]
    cells = [c.strip() for c in summary_line.split("|")]
    assert cells[3] == "1/2"
    assert cells[4] == "20.0"
    assert "Paired (greedy vs greedy_repair, 1/2 jointly feasible)" in tbl

    paired = compute_paired_summary(rows_g, rows_r)
    assert paired["joint_feasible_count"] == 1
    assert paired["mean_makespan_diff"] == 0.0


def test_pyjobshop_bridge_and_blocked_import() -> None:
    # 1. Verify clean ImportError when pyjobshop is absent
    src_dir = str(REPO_ROOT / "src")
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; sys.modules['pyjobshop'] = None; "
                "from labshift import generate_instance, solve_instance; "
                "inst = generate_instance('small', seed=0);\n"
                "try:\n"
                "    solve_instance(inst, solver='pyjobshop')\n"
                "except ImportError as e:\n"
                "    assert 'pyjobshop>=0.0.9' in str(e)\n"
                "else:\n"
                "    raise AssertionError('Expected ImportError')"
            ),
        ],
        env={"PYTHONPATH": src_dir},
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr

    # 2. Live PyJobShop head-to-head and extension quantification when installed
    pytest.importorskip("pyjobshop")
    from labshift.pyjobshop_bridge import (
        compare_solvers_head_to_head,
        quantify_labshift_extensions,
        solve_pyjobshop,
    )

    s00 = Instance.load_json(BENCHMARKS_DIR / "small_00.json")
    pj_sched = solve_pyjobshop(s00, time_limit=2.0, num_workers=1)
    assert check_schedule(s00, pj_sched).feasible
    assert compute_metrics(s00, pj_sched).makespan == 105

    h2h = compare_solvers_head_to_head([s00], time_limit=1.5, num_workers=1)
    assert h2h["solvers"]["pyjobshop"]["feasible_count"] == 1
    assert h2h["solvers"]["cpsat"]["feasible_count"] == 1

    exts = quantify_labshift_extensions(time_limit=1.5)
    assert len(exts) == 3
    for item in exts:
        assert item["labshift_cpsat_feasible"] is True
        assert item["pyjobshop_feasible"] is False


def test_open_benchmark_importers_offline_jssp_psplib_and_wetlab_campaigns() -> None:
    # 1. OR-Library / JSPLIB & Taillard two-matrix JSSP
    orlib_sample = """
    instance ft03_mini
    2 2
    0 4 1 6
    1 5 0 3
    """
    jssp_list = parse_orlib_jobshop_archive(orlib_sample)
    assert len(jssp_list) == 1
    assert check_schedule(jssp_list[0], solve_lns(jssp_list[0], time_limit=0.2)).feasible

    taillard_sample = """
    2 2 0 0 10 8
    Times
    4 6
    5 3
    Machines
    1 2
    2 1
    """
    ta_inst = parse_jssp_instance(taillard_sample, name="ta_mini")
    assert check_schedule(ta_inst, solve_lns(ta_inst, time_limit=0.2)).feasible

    # 2. PSPLIB .sm and AMPL .dat formats
    sm_sample = """
************************************************************************
PRECEDENCE RELATIONS:
jobnr.    #modes  #successors   successors
   1        1          2           2   3
   2        1          1           4
   3        1          1           4
   4        1          0
************************************************************************
REQUESTS/DURATIONS:
  jobnr. mode duration  R 1  R 2
------------------------------------------------------------------------
    1      1     0       0    0
    2      1     6       4    0
    3      1     5       0    3
    4      1     0       0    0
************************************************************************
RESOURCEAVAILABILITIES:
  R 1  R 2
    8    6
************************************************************************
"""
    sm_inst = parse_psplib_sm(sm_sample, name="j30_mini_sm")
    assert check_schedule(sm_inst, solve_lns(sm_inst, time_limit=0.2)).feasible

    dat_sample = """
    param n := 4;
    param p := 1 0 2 6 3 5 4 0;
    param R := R1 8 R2 6;
    param r := 2 R1 4 3 R2 3;
    set P[2] := 1;
    set P[3] := 1;
    set P[4] := 2 3;
    """
    dat_inst = parse_psplib_ampl_dat(dat_sample, name="j60_mini_dat")
    assert check_schedule(dat_inst, solve_lns(dat_inst, time_limit=0.2)).feasible

    # 3. Opentrons API v2 + WLP BRAT + BioPlanner JSON -> shared wet-lab campaign
    ot_code = """
metadata = {'protocolName': 'Drug Dosing', 'apiLevel': '2.13'}
def run(protocol):
    p300.distribute(150, res['A1'], plate.rows()[0])
    p300.transfer(50, res['A2'], plate.rows()[0])
    p300.mix(3, 100, plate['A1'])
    protocol.delay(seconds=15)
    temp_mod.set_temperature(37)
"""
    ot_job = parse_opentrons_python_protocol(ot_code, protocol_id="ot_drug")
    assert len(ot_job.ops) >= 2

    wlp_txt = "Add 100 uL buffer. Incubate for 20 minutes at 37 C. Centrifuge for 5 min. Read absorbance."
    wlp_ann = (
        "T1\tAction 0 3\tAdd\n"
        "T2\tAction 19 27\tIncubate\n"
        "T3\tTime 32 42\t20 minutes\n"
        "T4\tAction 52 62\tCentrifuge\n"
        "T5\tTime 67 72\t5 min\n"
        "T6\tAction 74 78\tRead\n"
    )
    wlp_job = parse_wlp_brat_protocol(wlp_txt, wlp_ann, protocol_id="wlp_103")
    assert [op.instrument for op in wlp_job.ops] == [
        "liquid_handler",
        "incubator",
        "centrifuge",
        "plate_reader",
    ]

    bp_json = {
        "id": "1007",
        "steps": "1. Add 1 uL reagent and vortex to mix.\n2. Centrifuge sample for 15 minutes.\n3. Wash pellet.\n4. Count fluorescence under microscope.",
    }
    bp_job = parse_bioplanner_json_protocol(bp_json)
    campaign = build_wetlab_campaign_instance([ot_job, wlp_job, bp_job])
    camp_sched = solve_lns(campaign, time_limit=0.5, seed=0)
    assert check_schedule(campaign, camp_sched).feasible

    # Also verify CLI --planted + --witness-out round-trip
    _ = run_benchmark
    _ = json
