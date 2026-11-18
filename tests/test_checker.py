"""Tests for schedule feasibility checking across all violation types."""

from __future__ import annotations

from labshift.checker import check_schedule
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


def test_catches_unknown_instrument_and_duration_mismatch() -> None:
    inst = _sample_instance()
    base = list(_valid_schedule().ops)

    bad_inst = list(base)
    bad_inst[0] = ScheduledOp("j2", 0, "centrifuge", 0, 5, "prep")
    res_inst = check_schedule(inst, Schedule("tiny", "manual", tuple(bad_inst)))
    assert "unknown_instrument" in res_inst.counts_by_kind()

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


def test_catches_precedence_min_wait_and_max_wait() -> None:
    inst = _sample_instance()

    prec_ops = (
        ScheduledOp("j2", 0, "liquid_handler", 0, 5, "prep"),
        ScheduledOp("j2", 1, "plate_reader", 4, 8, "read"),
        ScheduledOp("j1", 0, "liquid_handler", 5, 10, "prep"),
        ScheduledOp("j1", 1, "incubator", 12, 22, "inc"),
    )
    assert "precedence" in check_schedule(inst, Schedule("tiny", "manual", prec_ops)).counts_by_kind()

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


def test_catches_capacity_and_deadline() -> None:
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
