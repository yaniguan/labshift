"""Download and evaluate real public scheduling & wet-lab benchmarks into gitignored data/.

Datasets fetched:
  1. OR-Library / Taillard JSSP (`ft06`, `la01`, `la02`, `la03`, `ta01`)
  2. PSPLIB Single-Mode RCPSP (`j30` `.sm` & `.dat`, `j60` `.dat`)
  3. Multi-protocol Wet-Lab Campaign (`Opentrons/Protocols` + `WLP-Dataset` + `BioPlanner`)
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from labshift.bench import BenchRow, format_benchmark_table, solve_instance  # noqa: E402
from labshift.checker import check_schedule  # noqa: E402
from labshift.metrics import compute_metrics  # noqa: E402
from labshift.models import Instance  # noqa: E402
from labshift.open_benchmarks import (  # noqa: E402
    build_wetlab_campaign_instance,
    parse_bioplanner_json_protocol,
    parse_jssp_instance,
    parse_opentrons_python_protocol,
    parse_orlib_jobshop_archive,
    parse_psplib_ampl_dat,
    parse_psplib_sm,
    parse_wlp_brat_protocol,
)

ORLIB_JOBSHOP_URL = "https://people.brunel.ac.uk/~mastjjb/jeb/orlib/files/jobshop1.txt"
JSPLIB_TA01_URL = "https://raw.githubusercontent.com/tamy0612/JSPLIB/master/instances/ta01"

PSPLIB_SOURCES: tuple[tuple[str, str, str], ...] = (
    (
        "psplib_j30_1_1_sm",
        "sm",
        "https://raw.githubusercontent.com/wyvianvalenca/ga-rescheduling/main/tests/fixtures/j301_1.sm",
    ),
    (
        "psplib_j30_13_1",
        "dat",
        "https://raw.githubusercontent.com/Otavio-Novais/rcpsp-baterias/main/ampl/data/j3013_1.dat",
    ),
    (
        "psplib_j30_25_4",
        "dat",
        "https://raw.githubusercontent.com/Otavio-Novais/rcpsp-baterias/main/ampl/data/j3025_4.dat",
    ),
    (
        "psplib_j60_5_4",
        "dat",
        "https://raw.githubusercontent.com/Otavio-Novais/rcpsp-baterias/main/ampl/data/j605_4.dat",
    ),
    (
        "psplib_j60_21_4",
        "dat",
        "https://raw.githubusercontent.com/Otavio-Novais/rcpsp-baterias/main/ampl/data/j6021_4.dat",
    ),
    (
        "psplib_j60_37_2",
        "dat",
        "https://raw.githubusercontent.com/Otavio-Novais/rcpsp-baterias/main/ampl/data/j6037_2.dat",
    ),
)

OPENTRONS_URLS: tuple[tuple[str, str], ...] = (
    (
        "ot2_00222e_dd",
        "https://raw.githubusercontent.com/Opentrons/Protocols/master/protocols/00222e-dd/00222e.ot2.apiv2.py",
    ),
    (
        "ot2_002af3",
        "https://raw.githubusercontent.com/Opentrons/Protocols/master/protocols/002af3/002af3.ot2.apiv2.py",
    ),
    (
        "ot2_0045f1",
        "https://raw.githubusercontent.com/Opentrons/Protocols/master/protocols/0045f1/0045f1.ot2.apiv2.py",
    ),
)

WLP_IDS: tuple[str, ...] = ("protocol_103", "protocol_106", "protocol_122", "protocol_132")
BIOPLANNER_IDS: tuple[str, ...] = ("1007", "10051", "10157", "10176")


def _fetch_text(url: str, cache_path: Path) -> str:
    if cache_path.exists():
        return cache_path.read_text(encoding="utf-8", errors="replace")
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "labshift-open-bench/0.1"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    cache_path.write_text(raw, encoding="utf-8")
    return raw


def fetch_and_build_all(data_dir: Path) -> dict[str, list[Instance]]:
    """Download public benchmark files into data_dir/raw and convert to labshift Instances."""
    raw_dir = data_dir / "raw"
    inst_dir = data_dir / "instances"
    inst_dir.mkdir(parents=True, exist_ok=True)

    # 1. OR-Library + Taillard JSSP
    orlib_txt = _fetch_text(ORLIB_JOBSHOP_URL, raw_dir / "orlib" / "jobshop1.txt")
    jssp_insts = parse_orlib_jobshop_archive(
        orlib_txt, names_filter=("ft06", "la01", "la02", "la03")
    )
    ta01_txt = _fetch_text(JSPLIB_TA01_URL, raw_dir / "jsplib" / "ta01.txt")
    jssp_insts.append(parse_jssp_instance(ta01_txt, name="ta01"))

    # 2. PSPLIB j30 & j60
    psplib_insts: list[Instance] = []
    for name, fmt, url in PSPLIB_SOURCES:
        ext = "sm" if fmt == "sm" else "dat"
        txt = _fetch_text(url, raw_dir / "psplib" / f"{name}.{ext}")
        if fmt == "sm":
            psplib_insts.append(parse_psplib_sm(txt, name=name))
        else:
            psplib_insts.append(parse_psplib_ampl_dat(txt, name=name))

    # 3. Wet-Lab Campaign Workloads (Opentrons/Protocols + WLP + BioPlanner)
    ot_jobs = []
    for pid, url in OPENTRONS_URLS:
        code = _fetch_text(url, raw_dir / "opentrons" / f"{pid}.py")
        ot_jobs.append(parse_opentrons_python_protocol(code, protocol_id=pid))

    wlp_jobs = []
    for wid in WLP_IDS:
        base_wlp = f"https://raw.githubusercontent.com/chaitanya2334/WLP-Dataset/master/test/{wid}"
        txt_w = _fetch_text(f"{base_wlp}.txt", raw_dir / "wlp" / f"{wid}.txt")
        ann_w = _fetch_text(f"{base_wlp}.ann", raw_dir / "wlp" / f"{wid}.ann")
        wlp_jobs.append(parse_wlp_brat_protocol(txt_w, ann_w, protocol_id=f"wlp_{wid}"))

    bp_jobs = []
    for bid in BIOPLANNER_IDS:
        bp_url = f"https://raw.githubusercontent.com/BioPlanner/BioPlanner/main/bioprot/{bid}.json"
        bp_raw = json.loads(_fetch_text(bp_url, raw_dir / "bioplanner" / f"{bid}.json"))
        bp_jobs.append(parse_bioplanner_json_protocol(bp_raw, protocol_id=f"bp_{bid}"))

    wetlab_insts = [
        build_wetlab_campaign_instance(
            ot_jobs + wlp_jobs[:2],
            name="campaign_opentrons_wlp_5jobs",
            stagger_release_min=3,
        ),
        build_wetlab_campaign_instance(
            wlp_jobs + bp_jobs,
            name="campaign_wlp_bioplanner_8jobs",
            stagger_release_min=4,
        ),
        build_wetlab_campaign_instance(
            ot_jobs + wlp_jobs + bp_jobs,
            name="campaign_combined_11jobs",
            stagger_release_min=3,
        ),
    ]

    suites = {
        "jssp": jssp_insts,
        "psplib": psplib_insts,
        "wetlab_campaigns": wetlab_insts,
    }
    for group_name, inst_list in suites.items():
        g_dir = inst_dir / group_name
        g_dir.mkdir(parents=True, exist_ok=True)
        for inst in inst_list:
            inst.save_json(g_dir / f"{inst.name}.json")
    return suites


def evaluate_suites(
    suites: dict[str, list[Instance]],
    solvers: tuple[str, ...],
    time_limit: float,
) -> dict[str, Any]:
    """Run requested solvers across all fetched open benchmark suites."""
    summary: dict[str, Any] = {}
    for suite_name, inst_list in suites.items():
        print(f"\n=== Suite: {suite_name} ({len(inst_list)} instances) ===")
        suite_res: dict[str, Any] = {}
        for s_name in solvers:
            rows: list[BenchRow] = []
            for inst in inst_list:
                sched = solve_instance(
                    inst, solver=s_name, time_limit=time_limit, num_workers=4, seed=0
                )
                chk = check_schedule(inst, sched)
                m = compute_metrics(inst, sched, check=chk)
                rows.append(
                    BenchRow(
                        instance_name=inst.name,
                        preset=inst.preset or suite_name,
                        solver=s_name,
                        status=sched.status,
                        metrics=m,
                    )
                )
            print(f"\n--- Solver: {s_name} ---")
            print(format_benchmark_table(rows))
            feas_ms = [
                r.metrics.makespan
                for r in rows
                if r.metrics.feasible and r.metrics.makespan is not None
            ]
            feas_gaps = [
                r.metrics.gap_pct
                for r in rows
                if r.metrics.feasible and r.metrics.gap_pct is not None
            ]
            suite_res[s_name] = {
                "count": len(rows),
                "feasible_count": sum(1 for r in rows if r.metrics.feasible),
                "mean_makespan": round(sum(feas_ms) / len(feas_ms), 2) if feas_ms else None,
                "mean_gap_pct": round(sum(feas_gaps) / len(feas_gaps), 2) if feas_gaps else None,
                "total_time_s": round(sum(r.metrics.solve_time_s for r in rows), 3),
                "rows": [r.to_dict() for r in rows],
            }
        summary[suite_name] = suite_res
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch and benchmark open scheduling & wet-lab suites.")
    parser.add_argument(
        "--data-dir",
        default=str(REPO_ROOT / "data" / "open_benchmarks"),
        help="Target gitignored directory for downloaded files (default: data/open_benchmarks).",
    )
    parser.add_argument(
        "--time-limit",
        type=float,
        default=3.0,
        help="Per-instance solver time limit in seconds (default: 3.0).",
    )
    parser.add_argument(
        "--solvers",
        nargs="+",
        default=["greedy", "greedy_repair", "lns", "cpsat", "pyjobshop"],
        help="Solvers to benchmark.",
    )
    args = parser.parse_args(argv)

    data_dir = Path(args.data_dir)
    suites = fetch_and_build_all(data_dir)
    report = evaluate_suites(suites, tuple(args.solvers), args.time_limit)
    out_json = data_dir / "open_benchmarks_report.json"
    out_json.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"\nSaved full open benchmark report to {out_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
