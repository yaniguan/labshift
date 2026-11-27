"""Tests for schedule feasibility checking across all violation types."""

from __future__ import annotations

from labshift.checker import check_schedule
from labshift.metrics import compute_metrics
from labshift.models import Instance, Job, Operation, Schedule, ScheduledOp


def _sample_instance() -> Instance:
    return Instance(
        name="tiny",
        capacities={"liquid_handler": 1, "incubator": 2, "plate_reader": 1},
        jobs=(
            Job(
                job_id="j1",
                release=2,
                deadline=30,
                ops=(
                    Operation(name="prep", instrument="liquid_handler", duration=5),
                    Operation(
                        name="inc",
                        instrument="incubator",
                        duration=10,
                        min_wait=2,
                        max_wait=5,
                    ),
                ),
            ),
            Job(
                job_id="j2",
                release=0,
                deadline=40,
                ops=(
                    Operation(name="prep", instrument="liquid_handler", duration=5),
                    Operation(name="read", instrument="plate_reader", duration=4),
                ),
            ),
        ),
    )


def _valid_schedule() -> Schedule:
    return Schedule(
        instance_name="tiny",
        solver="manual",
        ops=(
            ScheduledOp("j2", 0, "liquid_handler", 0, 5, "prep"),
            ScheduledOp("j2", 1, "plate_reader", 5, 9, "read"),
            ScheduledOp("j1", 0, "liquid_handler", 5, 10, "prep"),
            ScheduledOp("j1", 1, "incubator", 12, 22, "inc"),
        ),
    )


def test_valid_schedule_and_back_to_back_capacity_one() -> None:
    inst = _sample_instance()
    res = check_schedule(inst, _valid_schedule())
    assert res.feasible is True
    assert res.violations == ()


def test_catches_missing_and_extra_and_duplicate_ops() -> None:
    inst = _sample_instance()
    base = _valid_schedule()

    missing = Schedule("tiny", "manual", base.ops[:-1])
    res_missing = check_schedule(inst, missing)
    assert not res_missing.feasible
    assert "missing_op" in res_missing.counts_by_kind()

    extra = Schedule(
        "tiny",
        "manual",
        base.ops + (ScheduledOp("j9", 0, "plate_reader", 0, 4, "extra"),),
    )
    res_extra = check_schedule(inst, extra)
    assert not res_extra.feasible
    assert "extra_op" in res_extra.counts_by_kind()

    dup = Schedule("tiny", "manual", base.ops + (base.ops[0],))
    res_dup = check_schedule(inst, dup)
    assert not res_dup.feasible
    assert "extra_op" in res_dup.counts_by_kind()


def test_catches_unknown_instrument_wrong_instrument_negative_start_and_duration() -> None:
    inst = _sample_instance()
    base = list(_valid_schedule().ops)

    # Unknown instrument (not in capacities) with valid duration -> unknown_instrument + wrong_instrument, NOT duration
    bad_inst = list(base)
    bad_inst[0] = ScheduledOp("j2", 0, "centrifuge", 0, 5, "prep")
    res_inst = check_schedule(inst, Schedule("tiny", "manual", tuple(bad_inst)))
    counts_inst = res_inst.counts_by_kind()
    assert counts_inst.get("unknown_instrument") == 1
    assert counts_inst.get("wrong_instrument") == 1
    assert "duration" not in counts_inst

    # Known wrong instrument (in capacities) with valid duration -> wrong_instrument only
    wrong_known = list(base)
    wrong_known[0] = ScheduledOp("j2", 0, "incubator", 0, 5, "prep")
    res_wrong = check_schedule(inst, Schedule("tiny", "manual", tuple(wrong_known)))
    counts_wrong = res_wrong.counts_by_kind()
    assert counts_wrong == {"wrong_instrument": 1}

    # Negative start with matching duration -> negative_start (and release if < release)
    neg_start = list(base)
    neg_start[0] = ScheduledOp("j2", 0, "liquid_handler", -2, 3, "prep")
    res_neg = check_schedule(inst, Schedule("tiny", "manual", tuple(neg_start)))
    counts_neg = res_neg.counts_by_kind()
    assert counts_neg.get("negative_start") == 1
    assert "duration" not in counts_neg

    # Bad duration on right instrument -> duration only
    bad_dur = list(base)
    bad_dur[0] = ScheduledOp("j2", 0, "liquid_handler", 0, 7, "prep")
    res_dur = check_schedule(inst, Schedule("tiny", "manual", tuple(bad_dur)))
    assert "duration" in res_dur.counts_by_kind()


def test_catches_release_violation() -> None:
    inst = _sample_instance()
    ops = (
        ScheduledOp("j2", 0, "liquid_handler", 0, 5, "prep"),
        ScheduledOp("j2", 1, "plate_reader", 5, 9, "read"),
        ScheduledOp("j1", 0, "liquid_handler", 1, 6, "prep"),
        ScheduledOp("j1", 1, "incubator", 8, 18, "inc"),
    )
    res = check_schedule(inst, Schedule("tiny", "manual", ops))
    assert not res.feasible
    assert "release" in res.counts_by_kind()


def test_catches_precedence_min_wait_max_wait_and_instance_mismatch() -> None:
    inst = _sample_instance()

    prec_ops = (
        ScheduledOp("j2", 0, "liquid_handler", 0, 5, "prep"),
        ScheduledOp("j2", 1, "plate_reader", 4, 8, "read"),
        ScheduledOp("j1", 0, "liquid_handler", 5, 10, "prep"),
        ScheduledOp("j1", 1, "incubator", 12, 22, "inc"),
    )
    res_prec = check_schedule(inst, Schedule("tiny", "manual", prec_ops))
    assert "precedence" in res_prec.counts_by_kind()
    assert "min_wait" not in res_prec.counts_by_kind()  # j2 op 1 has min_wait == 0

    # When wait < 0 AND spec.min_wait > 0 (j1 op 1 has min_wait=2), both precedence and min_wait are recorded
    both_ops = (
        ScheduledOp("j2", 0, "liquid_handler", 0, 5, "prep"),
        ScheduledOp("j2", 1, "plate_reader", 5, 9, "read"),
        ScheduledOp("j1", 0, "liquid_handler", 5, 10, "prep"),
        ScheduledOp("j1", 1, "incubator", 9, 19, "inc"),
    )
    sched_both = Schedule("tiny", "manual", both_ops)
    res_both = check_schedule(inst, sched_both)
    assert res_both.counts_by_kind().get("precedence") == 1
    assert res_both.counts_by_kind().get("min_wait") == 1
    assert compute_metrics(inst, sched_both, check=res_both).window_violations == 1

    min_ops = (
        ScheduledOp("j2", 0, "liquid_handler", 0, 5, "prep"),
        ScheduledOp("j2", 1, "plate_reader", 5, 9, "read"),
        ScheduledOp("j1", 0, "liquid_handler", 5, 10, "prep"),
        ScheduledOp("j1", 1, "incubator", 11, 21, "inc"),
    )
    assert "min_wait" in check_schedule(inst, Schedule("tiny", "manual", min_ops)).counts_by_kind()

    max_ops = (
        ScheduledOp("j2", 0, "liquid_handler", 0, 5, "prep"),
        ScheduledOp("j2", 1, "plate_reader", 5, 9, "read"),
        ScheduledOp("j1", 0, "liquid_handler", 5, 10, "prep"),
        ScheduledOp("j1", 1, "incubator", 18, 28, "inc"),
    )
    assert "max_wait" in check_schedule(inst, Schedule("tiny", "manual", max_ops)).counts_by_kind()

    # Instance mismatch
    mismatch = Schedule("wrong_name", "manual", _valid_schedule().ops)
    res_mm = check_schedule(inst, mismatch)
    assert not res_mm.feasible
    assert res_mm.counts_by_kind() == {"instance_mismatch": 1}


def test_catches_capacity_above_one_zero_capacity_and_deadline() -> None:
    inst = _sample_instance()

    cap_ops = (
        ScheduledOp("j2", 0, "liquid_handler", 2, 7, "prep"),
        ScheduledOp("j2", 1, "plate_reader", 7, 11, "read"),
        ScheduledOp("j1", 0, "liquid_handler", 4, 9, "prep"),
        ScheduledOp("j1", 1, "incubator", 11, 21, "inc"),
    )
    res_cap = check_schedule(inst, Schedule("tiny", "manual", cap_ops))
    assert not res_cap.feasible
    assert "capacity" in res_cap.counts_by_kind()

    dead_ops = (
        ScheduledOp("j2", 0, "liquid_handler", 0, 5, "prep"),
        ScheduledOp("j2", 1, "plate_reader", 5, 9, "read"),
        ScheduledOp("j1", 0, "liquid_handler", 15, 20, "prep"),
        ScheduledOp("j1", 1, "incubator", 22, 32, "inc"),
    )
    res_dead = check_schedule(inst, Schedule("tiny", "manual", dead_ops))
    assert not res_dead.feasible
    assert "deadline" in res_dead.counts_by_kind()

    # Capacity > 1 (incubator=2): 2 concurrent ops pass, 3 concurrent ops fail
    multi_inst = Instance(
        name="cap2",
        capacities={"incubator": 2},
        jobs=tuple(
            Job(job_id=f"j{i}", ops=(Operation("inc", "incubator", duration=10),))
            for i in range(1, 4)
        ),
    )
    ok_cap2 = Schedule(
        "cap2",
        "manual",
        ops=(
            ScheduledOp("j1", 0, "incubator", 0, 10, "inc"),
            ScheduledOp("j2", 0, "incubator", 2, 12, "inc"),
            ScheduledOp("j3", 0, "incubator", 10, 20, "inc"),
        ),
    )
    assert check_schedule(multi_inst, ok_cap2).feasible

    bad_cap2 = Schedule(
        "cap2",
        "manual",
        ops=(
            ScheduledOp("j1", 0, "incubator", 0, 10, "inc"),
            ScheduledOp("j2", 0, "incubator", 2, 12, "inc"),
            ScheduledOp("j3", 0, "incubator", 5, 15, "inc"),
        ),
    )
    res_bad_cap2 = check_schedule(multi_inst, bad_cap2)
    assert res_bad_cap2.counts_by_kind() == {"capacity": 1}

    # Zero-capacity instrument flags capacity violation even if scheduled op has 0 span
    zero_inst = Instance(
        name="zero",
        capacities={"liquid_handler": 0},
        jobs=(Job("j1", ops=(Operation("prep", "liquid_handler", duration=5),)),),
    )
    zero_sched = Schedule(
        "zero", "manual", ops=(ScheduledOp("j1", 0, "liquid_handler", 0, 0, "prep"),)
    )
    res_zero = check_schedule(zero_inst, zero_sched)
    assert "capacity" in res_zero.counts_by_kind()
    assert "duration" in res_zero.counts_by_kind()


def test_transfer_time_and_robot_arm_capacity() -> None:
    inst = Instance(
        name="arm_cell",
        capacities={"liquid_handler": 2, "plate_reader": 2, "robot_arm": 1},
        jobs=(
            Job(
                job_id="j1",
                ops=(
                    Operation("prep", "liquid_handler", duration=5),
                    Operation("read", "plate_reader", duration=4, transfer_time=3),
                ),
            ),
            Job(
                job_id="j2",
                ops=(
                    Operation("prep", "liquid_handler", duration=5),
                    Operation("read", "plate_reader", duration=4, transfer_time=3),
                ),
            ),
        ),
    )

    # Insufficient transfer gap (wait=2 < transfer_time=3)
    short_gap = Schedule(
        "arm_cell",
        "manual",
        ops=(
            ScheduledOp("j1", 0, "liquid_handler", 0, 5, "prep"),
            ScheduledOp("j1", 1, "plate_reader", 7, 11, "read"),
            ScheduledOp("j2", 0, "liquid_handler", 0, 5, "prep"),
            ScheduledOp("j2", 1, "plate_reader", 11, 15, "read"),
        ),
    )
    res_gap = check_schedule(inst, short_gap)
    assert res_gap.counts_by_kind() == {"transfer_time": 1}

    # Overlapping robot_arm transfers: [5, 8) and [6, 9) on capacity-1 arm
    arm_clash = Schedule(
        "arm_cell",
        "manual",
        ops=(
            ScheduledOp("j1", 0, "liquid_handler", 0, 5, "prep"),
            ScheduledOp("j1", 1, "plate_reader", 8, 12, "read"),
            ScheduledOp("j2", 0, "liquid_handler", 0, 5, "prep"),
            ScheduledOp("j2", 1, "plate_reader", 9, 13, "read"),
        ),
    )
    res_clash = check_schedule(inst, arm_clash)
    assert res_clash.counts_by_kind() == {"capacity": 1}
    assert res_clash.violations[0].instrument == "robot_arm"

    # Staggered robot_arm transfers: [5, 8) and [8, 11) -> valid!
    staggered = Schedule(
        "arm_cell",
        "manual",
        ops=(
            ScheduledOp("j1", 0, "liquid_handler", 0, 5, "prep"),
            ScheduledOp("j1", 1, "plate_reader", 8, 12, "read"),
            ScheduledOp("j2", 0, "liquid_handler", 0, 5, "prep"),
            ScheduledOp("j2", 1, "plate_reader", 11, 15, "read"),
        ),
    )
    assert check_schedule(inst, staggered).feasible


def test_source_station_blocking_same_pool_skip_and_transit_matrix() -> None:
    # Issue #29: plate_A sits in capacity-1 centrifuge over [10, 13) awaiting arm pickup at t=13
    inst_block = Instance(
        name="arm_blocking_bug",
        capacities={"centrifuge": 1, "plate_reader": 2, "robot_arm": 1},
        jobs=(
            Job(
                job_id="plate_A",
                ops=(
                    Operation("spin", "centrifuge", duration=10),
                    Operation("read", "plate_reader", duration=4, transfer_time=3, max_wait=10),
                ),
            ),
            Job(
                job_id="plate_B",
                ops=(Operation("spin", "centrifuge", duration=5),),
            ),
        ),
    )
    overlap_before_pickup = Schedule(
        instance_name="arm_blocking_bug",
        solver="manual",
        ops=(
            ScheduledOp("plate_A", 0, "centrifuge", 0, 10, "spin"),
            ScheduledOp("plate_A", 1, "plate_reader", 16, 20, "read"),
            ScheduledOp("plate_B", 0, "centrifuge", 10, 15, "spin"),
        ),
    )
    res_block = check_schedule(inst_block, overlap_before_pickup)
    assert res_block.feasible is False
    assert res_block.counts_by_kind() == {"capacity": 1}
    assert res_block.violations[0].instrument == "centrifuge"

    # Once plate_B starts at t=13 (right when robot_arm picks up plate_A), handoff is valid!
    valid_after_pickup = Schedule(
        instance_name="arm_blocking_bug",
        solver="manual",
        ops=(
            ScheduledOp("plate_A", 0, "centrifuge", 0, 10, "spin"),
            ScheduledOp("plate_A", 1, "plate_reader", 16, 20, "read"),
            ScheduledOp("plate_B", 0, "centrifuge", 13, 18, "spin"),
        ),
    )
    assert check_schedule(inst_block, valid_after_pickup).feasible is True

    # Same-instrument consecutive operations do NOT reserve robot_arm capacity
    same_pool = Instance(
        name="same_lh",
        capacities={"liquid_handler": 2, "robot_arm": 1},
        jobs=(
            Job(
                "j1",
                ops=(
                    Operation("dilute", "liquid_handler", 5),
                    Operation("dispense", "liquid_handler", 4, transfer_time=2),
                ),
            ),
            Job(
                "j2",
                ops=(
                    Operation("dilute", "liquid_handler", 5),
                    Operation("dispense", "liquid_handler", 4, transfer_time=2),
                ),
            ),
        ),
    )
    same_pool_sched = Schedule(
        "same_lh",
        "manual",
        ops=(
            ScheduledOp("j1", 0, "liquid_handler", 0, 5, "dilute"),
            ScheduledOp("j1", 1, "liquid_handler", 7, 11, "dispense"),
            ScheduledOp("j2", 0, "liquid_handler", 0, 5, "dilute"),
            ScheduledOp("j2", 1, "liquid_handler", 7, 11, "dispense"),
        ),
    )
    assert check_schedule(same_pool, same_pool_sched).feasible is True

    # Asymmetric transit_matrix enforces pool-pair transit time without per-op transfer_time
    inst_matrix = Instance(
        name="matrix_cell",
        capacities={"centrifuge": 1, "plate_reader": 1, "robot_arm": 1},
        transit_matrix={"centrifuge": {"plate_reader": 4}},
        jobs=(
            Job(
                "j1",
                ops=(
                    Operation("spin", "centrifuge", 6),
                    Operation("read", "plate_reader", 3),
                ),
            ),
        ),
    )
    bad_matrix_sched = Schedule(
        "matrix_cell",
        "manual",
        ops=(
            ScheduledOp("j1", 0, "centrifuge", 0, 6, "spin"),
            ScheduledOp("j1", 1, "plate_reader", 8, 11, "read"),
        ),
    )
    assert check_schedule(inst_matrix, bad_matrix_sched).counts_by_kind() == {"transfer_time": 1}


def test_sequence_dependent_setup_times_single_and_multi_unit() -> None:
    # Issue #30: asymmetric decontamination on cap=1 and cap=2 pools
    inst_cap1 = Instance(
        name="cip_cap1",
        capacities={"washer": 1},
        setup_times={"washer": {"bsl2": {"elisa": 10}, "elisa": {"bsl2": 2}}},
        jobs=(
            Job("j_bsl2", ops=(Operation("wash", "washer", 8, setup_tag="bsl2"),)),
            Job("j_elisa", ops=(Operation("wash", "washer", 7, setup_tag="elisa"),)),
        ),
    )
    # bsl2 [0, 8) -> elisa [10, 17): gap=2 < required 10 -> setup_time violation
    bad_cip = Schedule(
        "cip_cap1",
        "manual",
        ops=(
            ScheduledOp("j_bsl2", 0, "washer", 0, 8, "wash"),
            ScheduledOp("j_elisa", 0, "washer", 10, 17, "wash"),
        ),
    )
    res_bad = check_schedule(inst_cap1, bad_cip)
    assert res_bad.feasible is False
    assert res_bad.counts_by_kind() == {"setup_time": 1}

    # Reverse order: elisa [0, 7) -> bsl2 [9, 17): gap=2 >= required 2 -> feasible!
    good_cip = Schedule(
        "cip_cap1",
        "manual",
        ops=(
            ScheduledOp("j_elisa", 0, "washer", 0, 7, "wash"),
            ScheduledOp("j_bsl2", 0, "washer", 9, 17, "wash"),
        ),
    )
    assert check_schedule(inst_cap1, good_cip).feasible is True

    # Multi-unit (cap=2): two bsl2 ops occupy both units [0, 8), then elisa starts at t=10 (< 8+10)
    inst_cap2 = Instance(
        name="cip_cap2",
        capacities={"washer": 2},
        setup_times={"washer": {"bsl2": {"elisa": 10}, "elisa": {"bsl2": 2}}},
        jobs=(
            Job("b1", ops=(Operation("wash", "washer", 8, setup_tag="bsl2"),)),
            Job("b2", ops=(Operation("wash", "washer", 8, setup_tag="bsl2"),)),
            Job("e1", ops=(Operation("wash", "washer", 7, setup_tag="elisa"),)),
        ),
    )
    bad_cap2 = Schedule(
        "cip_cap2",
        "manual",
        ops=(
            ScheduledOp("b1", 0, "washer", 0, 8, "wash"),
            ScheduledOp("b2", 0, "washer", 0, 8, "wash"),
            ScheduledOp("e1", 0, "washer", 12, 19, "wash"),
        ),
    )
    assert check_schedule(inst_cap2, bad_cap2).counts_by_kind() == {"setup_time": 1}

    # Dedicated unit assignment: b1 & b2 share unit 0 ([0,8), [8,16)), e1 uses clean unit 1 ([0,7))
    good_cap2 = Schedule(
        "cip_cap2",
        "manual",
        ops=(
            ScheduledOp("b1", 0, "washer", 0, 8, "wash", unit_index=0),
            ScheduledOp("b2", 0, "washer", 8, 16, "wash", unit_index=0),
            ScheduledOp("e1", 0, "washer", 0, 7, "wash", unit_index=1),
        ),
    )
    assert check_schedule(inst_cap2, good_cap2).feasible is True
