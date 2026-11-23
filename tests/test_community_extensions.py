"""Tests for labshift community extensions (Ideas #07 and #08) and Lean guard parity."""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from community_extensions.night_shift_batcher import (  # noqa: E402
    ProtocolRequest,
    ScheduledProtocolSlot,
    check_dual_regime_slot,
    pack_dual_regime_72h,
    pack_fifo_human_gated_72h,
    run_benchmark as run_night_shift_benchmark,
    verify_dual_regime_schedule,
)
from community_extensions.rolling_contingency_scheduler import (  # noqa: E402
    FaultSpec,
    PlateJobSpec,
    PlateStepSpec,
    ScheduledPlateOp,
    WaitWindowLink,
    build_nominal_schedule,
    naive_fifo_delay_reschedule,
    rolling_contingency_repair,
    run_benchmark as run_contingency_benchmark,
    verify_contingency_schedule,
    verify_schedule,
)

CJK_RE = re.compile(r"[\u3000-\u303f\u4e00-\u9fff\uff00-\uffef]")


def test_verify_schedule_accepts_valid_and_rejects_missed_incubation_window() -> None:
    op1 = ScheduledPlateOp(
        op_id=1,
        plate_id=1,
        step_idx=0,
        pool_id=0,
        machine_idx=0,
        pool_cap=2,
        start_min=0,
        dur_min=10,
        release_min=0,
        deadline_min=60,
    )
    op2 = ScheduledPlateOp(
        op_id=2,
        plate_id=1,
        step_idx=1,
        pool_id=1,
        machine_idx=0,
        pool_cap=2,
        start_min=15,
        dur_min=10,
        release_min=0,
        deadline_min=60,
    )
    ok_link = WaitWindowLink(
        plate_id=1,
        pred_step_idx=0,
        succ_step_idx=1,
        pred_end_min=10,
        succ_start_min=15,
        min_wait_min=2,
        max_wait_min=6,
    )
    assert verify_schedule([op1, op2], [ok_link]) is None

    bad_link = WaitWindowLink(
        plate_id=1,
        pred_step_idx=0,
        succ_step_idx=1,
        pred_end_min=10,
        succ_start_min=15,
        min_wait_min=2,
        max_wait_min=3,
    )
    assert verify_schedule([op1, op2], [bad_link]) == "inter_step_wait_window"


def test_verify_schedule_rejects_machine_collision_and_capacity_overflow() -> None:
    op_a = ScheduledPlateOp(
        op_id=1,
        plate_id=1,
        step_idx=0,
        pool_id=0,
        machine_idx=0,
        pool_cap=2,
        start_min=5,
        dur_min=10,
        release_min=0,
        deadline_min=40,
    )
    op_b = ScheduledPlateOp(
        op_id=2,
        plate_id=2,
        step_idx=0,
        pool_id=0,
        machine_idx=0,
        pool_cap=2,
        start_min=12,
        dur_min=8,
        release_min=0,
        deadline_min=40,
    )
    assert verify_schedule([op_a, op_b], []) == "machine_mutual_exclusion"

    over_cap = ScheduledPlateOp(
        op_id=3,
        plate_id=3,
        step_idx=0,
        pool_id=0,
        machine_idx=2,
        pool_cap=2,
        start_min=0,
        dur_min=5,
        release_min=0,
        deadline_min=40,
    )
    assert verify_schedule([over_cap], []) == "op_release_deadline_capacity"


def test_rolling_contingency_reroutes_live_plates_and_holds_pre_incubation() -> None:
    pool_caps = {0: 2, 1: 6, 2: 2}
    jobs = [
        PlateJobSpec(
            plate_id=p + 1,
            release_min=(p // 2) * 6,
            deadline_min=220,
            steps=(
                PlateStepSpec(step_idx=0, pool_id=0, dur_min=4),
                PlateStepSpec(
                    step_idx=1,
                    pool_id=1,
                    dur_min=22,
                    min_wait_after_prev=1,
                    max_wait_after_prev=5,
                ),
                PlateStepSpec(
                    step_idx=2,
                    pool_id=2,
                    dur_min=5,
                    min_wait_after_prev=1,
                    max_wait_after_prev=6,
                ),
            ),
        )
        for p in range(10)
    ]
    nominal_ops, nominal_links = build_nominal_schedule(jobs, pool_caps)
    assert verify_schedule(nominal_ops, nominal_links) is None

    fault = FaultSpec(fault_pool_id=2, fault_machine_idx=0, fault_min=10, repair_min=65)
    naive = naive_fifo_delay_reschedule(jobs, nominal_ops, pool_caps, fault)
    repaired = rolling_contingency_repair(jobs, nominal_ops, pool_caps, fault)

    assert len(naive.violated_plate_ids) > 0
    assert len(repaired.violated_plate_ids) == 0
    assert repaired.verification_error is None
    assert verify_contingency_schedule(list(repaired.ops), list(repaired.links), fault) is None


def test_idea_07_benchmark_40_scenarios_saves_all_live_plates() -> None:
    report = run_contingency_benchmark(num_scenarios=40, seed=2026)
    assert report["num_scenarios"] == 40
    assert report["total_plates"] > 300
    naive = report["naive_fifo_delay"]
    repair = report["rolling_contingency_repair"]
    assert naive["window_violation_rate"] > 0.25
    assert repair["violated_plates"] == 0
    assert repair["window_violation_rate"] == 0.0
    assert repair["valid_schedule_rate"] == 1.0
    assert repair["plates_saved_over_naive"] > 100
    assert repair["mean_recovery_latency_ms"] < 100.0


def test_dual_regime_slot_guards_match_lean_theorems() -> None:
    # Uncertified overnight run -> rejected
    bad_night = ScheduledProtocolSlot(
        protocol_id="P1",
        lane_idx=0,
        start_min=520,
        dur_min=120,
        is_frontier_exception=False,
        human_cosigned=False,
        lean_certified=False,
    )
    assert check_dual_regime_slot(bad_night) == "autonomous_requires_lean_cert"

    # Frontier run spilling past 480 min into night -> rejected
    late_frontier = ScheduledProtocolSlot(
        protocol_id="P2",
        lane_idx=0,
        start_min=420,
        dur_min=90,
        is_frontier_exception=True,
        human_cosigned=True,
        lean_certified=False,
    )
    assert check_dual_regime_slot(late_frontier) == "frontier_daytime_cosign_gate"

    # Unsigned daytime frontier run -> rejected
    unsigned_frontier = ScheduledProtocolSlot(
        protocol_id="P3",
        lane_idx=0,
        start_min=60,
        dur_min=90,
        is_frontier_exception=True,
        human_cosigned=False,
        lean_certified=False,
    )
    assert check_dual_regime_slot(unsigned_frontier) == "frontier_daytime_cosign_gate"

    # Valid Lean-certified overnight run -> accepted
    good_night = ScheduledProtocolSlot(
        protocol_id="P4",
        lane_idx=0,
        start_min=500,
        dur_min=180,
        is_frontier_exception=False,
        human_cosigned=False,
        lean_certified=True,
    )
    assert check_dual_regime_slot(good_night) is None


def test_dual_regime_quarantines_unsafe_and_packs_overnight_lanes() -> None:
    reqs = [
        ProtocolRequest("F-OK", 0, 120, True, True, False),
        ProtocolRequest("F-BAD", 0, 120, True, False, False),
        ProtocolRequest("C-OK-1", 30, 240, False, False, True),
        ProtocolRequest("C-OK-2", 60, 300, False, False, True),
        ProtocolRequest("C-BAD", 60, 120, False, False, False),
    ]
    fifo = pack_fifo_human_gated_72h(reqs, num_lanes=1, horizon_hours=24)
    dual = pack_dual_regime_72h(reqs, num_lanes=1, horizon_hours=24)
    assert set(dual.quarantined_ids) == {"F-BAD", "C-BAD"}
    assert verify_dual_regime_schedule(list(dual.scheduled_slots), 1, 1440) is None
    assert dual.completed_protocols == 3
    assert dual.overnight_utilization_pct > 50.0
    assert fifo.overnight_utilization_pct == 0.0


def test_idea_08_benchmark_72h_workload_metrics() -> None:
    report = run_night_shift_benchmark(horizon_hours=72, seed=2026)
    assert report["horizon_hours"] == 72
    assert report["quarantined_unsafe_protocols"] == 6
    fifo = report["fifo_human_gated"]
    dual = report["dual_regime_night_packer"]
    assert fifo["schedule_valid"] is True
    assert dual["schedule_valid"] is True
    assert fifo["overnight_machine_utilization_pct"] == 0.0
    assert dual["overnight_machine_utilization_pct"] > 70.0
    assert dual["completed_protocols_72h"] > fifo["completed_protocols_72h"] * 1.6


def test_pure_english_and_lean_file_integrity() -> None:
    lean_file = REPO_ROOT / "lean" / "LabShiftLean.lean"
    assert lean_file.exists()
    lean_src = lean_file.read_text(encoding="utf-8")
    assert "sorry" not in re.findall(r"\bsorry\b", lean_src)
    assert "axiom" not in re.findall(r"\baxiom\b", lean_src)

    checked_paths = [
        lean_file,
        REPO_ROOT / "lean" / "README.md",
        REPO_ROOT / "lean" / "lakefile.toml",
        REPO_ROOT / "community_extensions" / "__init__.py",
        REPO_ROOT / "community_extensions" / "rolling_contingency_scheduler.py",
        REPO_ROOT / "community_extensions" / "night_shift_batcher.py",
        Path(__file__).resolve(),
    ]
    for path in checked_paths:
        text = path.read_text(encoding="utf-8")
        assert CJK_RE.search(text) is None, f"CJK character found in {path}"
