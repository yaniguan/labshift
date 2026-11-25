"""Tests for the autolab-wire/v1 Layer L5 scheduling adapter (autolab_wire.py)."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from autolab_wire import LAYER, PRIMARY_JOB_ID, REPO_DIR, REPO_ID, process_packet  # noqa: E402

CJK_RE = re.compile(r"[\u3000-\u303f\u4e00-\u9fff\uff00-\uffef]")


def test_wire_metadata_and_valid_parsed_spans_schedule() -> None:
    assert REPO_ID == "labshift"
    assert LAYER == "L5"
    assert REPO_DIR == REPO_ROOT

    packet = {
        "run_id": "wire-run-001",
        "parsed_spans": {
            "volume_ul": 120.0,
            "temp_c": 37.0,
            "duration_min": 18.4,
        },
        "max_wait_min": 1,
    }
    out = process_packet(packet)

    # Input packet must remain unmutated.
    assert "schedule" not in packet
    assert "trace" not in packet

    assert out["schema_version"] == "autolab-wire/v1"
    assert out["halted"] is False
    assert out["halt_reason"] is None

    sched = out["schedule"]
    assert sched["feasible"] is True
    assert sched["window_violations"] == 0
    assert sched["makespan_min"] >= 25

    primary_ops = [op for op in sched["operations"] if op["job_id"] == PRIMARY_JOB_ID]
    primary_ops.sort(key=lambda op: op["op_index"])
    assert [op["instrument"] for op in primary_ops] == [
        "liquid_handler",
        "incubator",
        "plate_reader",
    ]
    # 18.4 rounds to 18 min incubation; post-incubation wait must respect max_wait_min=1.
    assert primary_ops[1]["end"] - primary_ops[1]["start"] == 18
    assert 0 <= primary_ops[2]["start"] - primary_ops[1]["end"] <= 1

    assert len(out["trace"]) == 1
    assert out["trace"][0]["repo"] == "labshift"
    assert out["trace"][0]["layer"] == "L5"
    assert out["trace"][0]["status"] == "PASS"

    assert len(out["bottom_reasoning_traces"]) == 1
    assert out["bottom_reasoning_traces"][0]["repo"] == "labshift"
    assert out["bottom_reasoning_traces"][0]["invariant"] == "missed_incubation_window_rejected"


def test_forced_max_wait_window_violation_blocks_packet() -> None:
    out = process_packet(
        {
            "parsed_spans": {"duration_min": 15.0},
            "max_wait_min": 5,
            "force_window_violation_schedule": True,
        }
    )
    assert out["halted"] is True
    assert out["halt_reason"] == "L5_SCHEDULE_WINDOW_BLOCK"
    assert out["schedule"]["feasible"] is False
    assert out["schedule"]["window_violations"] >= 1
    assert out["trace"][-1]["repo"] == "labshift"
    assert out["trace"][-1]["status"] == "BLOCK"
    assert out["bottom_reasoning_traces"][-1]["invariant"] == "missed_incubation_window_rejected"


def test_custom_violating_schedule_dict_and_myopic_greedy_contention_block() -> None:
    # Explicit schedule dictionary violating post-incubation max_wait=2 (wait = 30 - 18 = 12 > 2).
    explicit_bad = {
        "ops": [
            {
                "job_id": PRIMARY_JOB_ID,
                "op_index": 0,
                "name": "liquid_prep",
                "instrument": "liquid_handler",
                "start": 0,
                "end": 3,
            },
            {
                "job_id": PRIMARY_JOB_ID,
                "op_index": 1,
                "name": "incubate",
                "instrument": "incubator",
                "start": 3,
                "end": 18,
            },
            {
                "job_id": PRIMARY_JOB_ID,
                "op_index": 2,
                "name": "plate_read",
                "instrument": "plate_reader",
                "start": 30,
                "end": 34,
            },
        ]
    }
    out_custom = process_packet(
        {
            "parsed_spans": {"duration_min": 15},
            "max_wait_min": 2,
            "include_background_job": False,
            "force_window_violation_schedule": explicit_bad,
        }
    )
    assert out_custom["halted"] is True
    assert out_custom["halt_reason"] == "L5_SCHEDULE_WINDOW_BLOCK"
    assert out_custom["schedule"]["window_violations"] == 1

    # Myopic greedy under max_wait_min=1 stalls behind background reader contention and is caught.
    out_greedy = process_packet(
        {
            "parsed_spans": {"duration_min": 15},
            "max_wait_min": 1,
            "solver": "greedy",
        }
    )
    assert out_greedy["halted"] is True
    assert out_greedy["halt_reason"] == "L5_SCHEDULE_WINDOW_BLOCK"
    assert out_greedy["schedule"]["window_violations"] >= 1


def test_halted_packet_passthrough_unchanged() -> None:
    halted_in = {
        "schema_version": "autolab-wire/v1",
        "halted": True,
        "halt_reason": "L6_ABSTAIN",
        "trace": [{"repo": "lab-decide", "layer": "L6", "status": "ABSTAIN", "summary": "OOD"}],
        "bottom_reasoning_traces": [
            {
                "repo": "lab-decide",
                "invariant": "out_of_domain_always_abstains",
                "first_principles": "Exchangeable split-conformal abstention.",
            }
        ],
    }
    out = process_packet(halted_in)
    assert out == halted_in
    assert out is not halted_in
    assert "schedule" not in out


def test_stdlib_fallback_when_ortools_absent() -> None:
    script = (
        "import json, sys; "
        "sys.modules['ortools'] = None; "
        "sys.modules['ortools.sat'] = None; "
        "sys.modules['ortools.sat.python'] = None; "
        "import autolab_wire; "
        "res = autolab_wire.process_packet({'parsed_spans': {'duration_min': 12}, 'max_wait_min': 1}); "
        "print(json.dumps({'halted': res['halted'], 'feasible': res['schedule']['feasible'], "
        "'win_viol': res['schedule']['window_violations']}))"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"stderr:\n{proc.stderr}"
    payload = json.loads(proc.stdout.strip())
    assert payload == {"halted": False, "feasible": True, "win_viol": 0}


def test_wire_and_docs_pure_english() -> None:
    for rel_path in (
        "autolab_wire.py",
        "README.md",
        "docs/ARCHITECTURE_AND_HISTORY.md",
        "tests/test_autolab_wire.py",
    ):
        text = (REPO_ROOT / rel_path).read_text(encoding="utf-8")
        assert CJK_RE.search(text) is None, f"Unexpected CJK character in {rel_path}"


def test_rolling_horizon_urgent_raman_and_lcms_rescheduling() -> None:
    import importlib.util

    # Verify dynamic module loading without sys.modules collisions
    spec = importlib.util.spec_from_file_location(
        "_labshift_wire_isolated", REPO_ROOT / "autolab_wire.py"
    )
    assert spec is not None and spec.loader is not None
    isolated_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(isolated_mod)

    # 1. Mid-campaign t=21 Raman confirmation triggered by lab-attrib while bg plate contends
    raman_packet = {
        "run_id": "wire-campaign-042",
        "current_time_min": 21,
        "parsed_spans": {"duration_min": 15},
        "max_wait_min": 5,
        "schedule": {
            "feasible": True,
            "makespan_min": 27,
            "window_violations": 0,
            "operations": [
                {
                    "job_id": "autolab_primary",
                    "op_index": 0,
                    "name": "liquid_prep",
                    "instrument": "liquid_handler",
                    "start": 0,
                    "end": 3,
                },
                {
                    "job_id": "autolab_primary",
                    "op_index": 1,
                    "name": "incubate",
                    "instrument": "incubator",
                    "start": 3,
                    "end": 18,
                },
                {
                    "job_id": "autolab_primary",
                    "op_index": 2,
                    "name": "plate_read",
                    "instrument": "plate_reader",
                    "start": 18,
                    "end": 22,
                },
                {
                    "job_id": "bg_contended_01",
                    "op_index": 0,
                    "name": "bg_liquid_prep",
                    "instrument": "liquid_handler",
                    "start": 22,
                    "end": 24,
                },
                {
                    "job_id": "bg_contended_01",
                    "op_index": 1,
                    "name": "bg_incubate",
                    "instrument": "incubator",
                    "start": 24,
                    "end": 39,
                },
                {
                    "job_id": "bg_contended_01",
                    "op_index": 2,
                    "name": "bg_plate_read",
                    "instrument": "plate_reader",
                    "start": 39,
                    "end": 44,
                },
            ],
        },
        "attribution": {
            "primary_cause": "CONFOUNDED_DESIGN",
            "recommended_action": "RUN_ORTHOGONAL_ASSAY",
            "orthogonal_assay": "Raman",
            "orthogonal_duration_min": 6,
            "orthogonal_max_wait_min": 3,
        },
    }
    out_raman = isolated_mod.process_packet(raman_packet)
    assert out_raman["halted"] is False
    assert out_raman["halt_reason"] is None
    assert set(out_raman["schedule"].keys()) == {
        "feasible",
        "makespan_min",
        "window_violations",
        "operations",
    }
    assert out_raman["schedule"]["feasible"] is True
    assert out_raman["schedule"]["window_violations"] == 0

    ops = out_raman["schedule"]["operations"]
    by_job_op = {(op["job_id"], op["op_index"]): op for op in ops}
    # Frozen operations (start < 21) remain locked at exact historical intervals
    assert (by_job_op[("autolab_primary", 0)]["start"], by_job_op[("autolab_primary", 0)]["end"]) == (0, 3)
    assert (by_job_op[("autolab_primary", 1)]["start"], by_job_op[("autolab_primary", 1)]["end"]) == (3, 18)
    assert (by_job_op[("autolab_primary", 2)]["start"], by_job_op[("autolab_primary", 2)]["end"]) == (18, 22)

    # Raman read starts inside [21, 24] while background coupled chain shifts together (>= 23)
    raman_op = by_job_op[("autolab_primary", 4)]
    assert raman_op["instrument"] == "raman_spectrometer"
    assert 21 <= raman_op["start"] <= 24
    assert raman_op["end"] - raman_op["start"] == 6
    assert by_job_op[("bg_contended_01", 0)]["start"] >= 23
    assert (
        0
        <= by_job_op[("bg_contended_01", 2)]["start"] - by_job_op[("bg_contended_01", 1)]["end"]
        <= 10
    )

    # 2. Urgent LC-MS confirmation via top-level urgent_orthogonal_assay
    lcms_packet = {
        **raman_packet,
        "attribution": {},
        "urgent_orthogonal_assay": {
            "assay": "LC-MS",
            "duration_min": 8,
            "max_wait_min": 4,
        },
    }
    out_lcms = process_packet(lcms_packet)
    assert out_lcms["halted"] is False
    assert out_lcms["schedule"]["feasible"] is True
    assert any(op["instrument"] == "lc_ms" for op in out_lcms["schedule"]["operations"])


def test_halt_reason_distinguishes_deadline_capacity_and_saturated_orthogonal_pool() -> None:
    # 1. Deadline overrun with zero window violations -> L5_SCHEDULE_DEADLINE_BLOCK
    out_dl = process_packet(
        {"parsed_spans": {"duration_min": 15}, "max_wait_min": 10, "deadline_min": 10}
    )
    assert out_dl["halted"] is True
    assert out_dl["halt_reason"] == "L5_SCHEDULE_DEADLINE_BLOCK"
    assert out_dl["schedule"]["window_violations"] == 0

    # 2. Offline orthogonal spectrometer (capacity 0) -> L5_SCHEDULE_CAPACITY_BLOCK
    out_cap = process_packet(
        {
            "current_time_min": 21,
            "urgent_orthogonal_assay": "Raman",
            "capacities": {"raman_spectrometer": 0},
        }
    )
    assert out_cap["halted"] is True
    assert out_cap["halt_reason"] == "L5_SCHEDULE_CAPACITY_BLOCK"

    # 3. Saturated orthogonal spectrometer blocking perishable Raman window -> L5_SCHEDULE_WINDOW_BLOCK
    saturated_packet = {
        "current_time_min": 21,
        "schedule": {
            "feasible": True,
            "makespan_min": 40,
            "window_violations": 0,
            "operations": [
                {
                    "job_id": "autolab_primary",
                    "op_index": 0,
                    "name": "plate_read",
                    "instrument": "plate_reader",
                    "start": 18,
                    "end": 22,
                },
                {
                    "job_id": "cal_run",
                    "op_index": 0,
                    "name": "raman_calibration",
                    "instrument": "raman_spectrometer",
                    "start": 15,
                    "end": 40,
                },
            ],
        },
        "urgent_orthogonal_assay": {
            "assay": "Raman",
            "duration_min": 6,
            "max_wait_min": 3,
        },
    }
    out_sat = process_packet(saturated_packet)
    assert out_sat["halted"] is True
    assert out_sat["halt_reason"] == "L5_SCHEDULE_WINDOW_BLOCK"
    assert out_sat["schedule"]["feasible"] is False
