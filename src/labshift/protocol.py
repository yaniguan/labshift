"""Protocol importers (twinstep / stepbot) and workcell queue exporters."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from labshift.models import INSTRUMENTS, Instance, Job, Operation, Schedule

ProtocolInput = Sequence[Mapping[str, Any] | Any] | Mapping[str, Any] | Any | str | Path

_LIQUID_HANDLER_TYPES = frozenset(
    {"transfer", "mix", "pipette", "dispense", "distribute", "aspirate", "dilution_series"}
)
_WASHER_TYPES = frozenset({"wash", "plate_wash"})
_INCUBATOR_TYPES = frozenset({"incubate", "thermocycle", "equilibrate"})
_CENTRIFUGE_TYPES = frozenset({"spin", "centrifuge", "spin_down"})
_READER_TYPES = frozenset({"read", "measure", "plate_read"})


def _step_to_dict(step: Any) -> dict[str, Any]:
    if isinstance(step, Mapping):
        return dict(step)
    to_dict = getattr(step, "to_dict", None)
    if callable(to_dict):
        res = to_dict()
        if isinstance(res, Mapping):
            return dict(res)
    raise ValueError(f"Protocol step must be a mapping or expose to_dict(), got {step!r}.")


def load_protocol_steps(source: ProtocolInput) -> tuple[str, list[dict[str, Any]]]:
    """Load protocol identifier and raw step dictionaries from a path, mapping, or list."""
    if isinstance(source, (str, Path)):
        path = Path(source)
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, list):
            return path.stem, [_step_to_dict(item) for item in raw]
        if isinstance(raw, Mapping):
            proto_id = str(raw.get("protocol_id") or raw.get("name") or path.stem)
            steps_raw = raw.get("steps") if "steps" in raw else raw.get("commands")
            if not isinstance(steps_raw, Sequence) or isinstance(steps_raw, (str, bytes)):
                raise TypeError(f"Protocol mapping in '{path}' must contain a 'steps' list.")
            return proto_id, [_step_to_dict(item) for item in steps_raw]
        raise TypeError(f"Protocol JSON '{path}' must be a list of steps or a mapping.")

    if isinstance(source, Mapping):
        proto_id = str(source.get("protocol_id") or source.get("name") or "protocol")
        steps_raw = source.get("steps") if "steps" in source else source.get("commands")
        if not isinstance(steps_raw, Sequence) or isinstance(steps_raw, (str, bytes)):
            raise TypeError("Protocol mapping must contain a 'steps' or 'commands' list.")
        return proto_id, [_step_to_dict(item) for item in steps_raw]

    to_dicts = getattr(source, "to_dicts", None)
    if callable(to_dicts):
        proto_id = str(getattr(source, "protocol_id", None) or "protocol")
        return proto_id, [_step_to_dict(item) for item in to_dicts()]

    if isinstance(source, Sequence) and not isinstance(source, (str, bytes)):
        return "protocol", [_step_to_dict(item) for item in source]

    raise ValueError(f"Unsupported protocol input: {source!r}.")


def _volume_ul(step: Mapping[str, Any], default: float = 100.0) -> float:
    raw_vol = step.get("volume", default)
    if isinstance(raw_vol, bool) or not isinstance(raw_vol, (int, float)) or raw_vol < 0:
        raise ValueError(f"Invalid step volume: {raw_vol!r}.")
    unit = str(step.get("unit", "uL"))
    if unit == "mL":
        return float(raw_vol) * 1000.0
    if unit == "uL":
        return float(raw_vol)
    raise ValueError(f"Unsupported volume unit '{unit}'. Expected 'uL' or 'mL'.")


def _time_seconds(step: Mapping[str, Any], default_min: float) -> float:
    raw_time = step.get("time", step.get("duration"))
    if raw_time is None:
        return default_min * 60.0
    if isinstance(raw_time, bool) or not isinstance(raw_time, (int, float)) or raw_time <= 0:
        raise ValueError(f"Invalid step time/duration: {raw_time!r}.")
    unit = str(step.get("time_unit", step.get("unit", "min")))
    if unit in ("s", "sec", "seconds"):
        return float(raw_time)
    if unit in ("min", "m", "minutes"):
        return float(raw_time) * 60.0
    if unit in ("h", "hr", "hours"):
        return float(raw_time) * 3600.0
    return float(raw_time) * 60.0


def _classify_step(step: Mapping[str, Any]) -> tuple[str, str, float]:
    """Return (step_type, instrument, estimated_seconds) for one protocol step."""
    raw_type = step.get("type", step.get("action", step.get("op")))
    if not isinstance(raw_type, str) or not raw_type.strip():
        raise ValueError(f"Protocol step missing non-empty 'type': {step!r}.")
    step_type = raw_type.strip().lower()

    if step_type in _LIQUID_HANDLER_TYPES:
        if "duration" in step or "time" in step:
            secs = _time_seconds(step, default_min=1.0)
        elif step_type == "mix":
            cycles = int(step.get("cycles", 3))
            if cycles < 1:
                raise ValueError(f"Invalid mix cycles: {cycles}.")
            _ = _volume_ul(step, default=100.0)
            secs = 15.0 + 6.0 * cycles
        elif step_type == "dilution_series":
            cols = int(step.get("steps", step.get("columns", 8)))
            secs = 60.0 + 15.0 * max(1, cols)
        else:
            vol_ul = _volume_ul(step, default=100.0)
            secs = 30.0 + 0.05 * vol_ul
        return step_type, "liquid_handler", secs

    if step_type in _WASHER_TYPES:
        if "duration" in step or "time" in step:
            secs = _time_seconds(step, default_min=2.0)
        else:
            cycles = int(step.get("cycles", 3))
            if cycles < 1:
                raise ValueError(f"Invalid wash cycles: {cycles}.")
            _ = _volume_ul(step, default=300.0)
            secs = 60.0 + 30.0 * cycles
        return step_type, "washer", secs

    if step_type in _INCUBATOR_TYPES:
        secs = _time_seconds(step, default_min=15.0)
        return step_type, "incubator", secs

    if step_type in _CENTRIFUGE_TYPES:
        secs = _time_seconds(step, default_min=5.0)
        return step_type, "centrifuge", secs

    if step_type in _READER_TYPES:
        secs = _time_seconds(step, default_min=2.0)
        return step_type, "plate_reader", secs

    raise ValueError(f"Unsupported protocol step type '{step_type}'.")


def job_from_protocol(
    protocol: ProtocolInput,
    *,
    job_id: str | None = None,
    release: int = 0,
    deadline: int | None = None,
    post_incubate_max_wait: int | None = 15,
    post_wash_max_wait: int | None = 10,
    transfer_time: int = 0,
) -> Job:
    """Convert a twinstep / stepbot protocol into a scheduled labshift Job.

    Groups contiguous liquid-handling steps (transfer/mix/dispense) onto
    ``liquid_handler``, wash steps onto ``washer``, incubation steps onto
    ``incubator``, spin steps onto ``centrifuge``, and read steps onto
    ``plate_reader``, estimating integer-minute durations (>= 1 min).
    """
    if post_incubate_max_wait is not None and (
        isinstance(post_incubate_max_wait, bool)
        or not isinstance(post_incubate_max_wait, int)
        or post_incubate_max_wait < 0
    ):
        raise ValueError("'post_incubate_max_wait' must be an int >= 0 or None.")
    if post_wash_max_wait is not None and (
        isinstance(post_wash_max_wait, bool)
        or not isinstance(post_wash_max_wait, int)
        or post_wash_max_wait < 0
    ):
        raise ValueError("'post_wash_max_wait' must be an int >= 0 or None.")
    if isinstance(transfer_time, bool) or not isinstance(transfer_time, int) or transfer_time < 0:
        raise ValueError("'transfer_time' must be an int >= 0.")

    proto_id, steps = load_protocol_steps(protocol)
    if not steps:
        raise ValueError("Protocol must contain at least one step.")

    resolved_job_id = job_id if job_id is not None else proto_id

    groups: list[tuple[str, list[str], float]] = []
    for step in steps:
        s_type, inst, secs = _classify_step(step)
        if groups and groups[-1][0] == inst:
            prev_inst_g, prev_types, prev_secs = groups[-1]
            if s_type not in prev_types:
                prev_types.append(s_type)
            groups[-1] = (prev_inst_g, prev_types, prev_secs + secs)
        else:
            groups.append((inst, [s_type], secs))

    ops: list[Operation] = []
    for idx, (inst, step_types, total_secs) in enumerate(groups):
        op_name = "_".join(step_types[:3])
        duration = max(1, math.ceil(total_secs / 60.0))
        min_wait = 0
        op_transfer = 0
        max_wait: int | None = None

        if idx > 0:
            prev_inst = groups[idx - 1][0]
            if inst != prev_inst and transfer_time > 0:
                op_transfer = transfer_time
            base_gap = max(min_wait, op_transfer)
            if prev_inst in ("incubator", "centrifuge") and post_incubate_max_wait is not None:
                max_wait = base_gap + post_incubate_max_wait
            elif prev_inst == "washer" and post_wash_max_wait is not None:
                max_wait = base_gap + post_wash_max_wait

        ops.append(
            Operation(
                name=op_name,
                instrument=inst,
                duration=duration,
                min_wait=min_wait,
                max_wait=max_wait,
                transfer_time=op_transfer,
            )
        )

    return Job(
        job_id=resolved_job_id,
        ops=tuple(ops),
        release=release,
        deadline=deadline,
    )


def instance_from_protocols(
    protocols: Sequence[ProtocolInput],
    *,
    name: str = "protocol_batch",
    capacities: Mapping[str, int] | None = None,
    releases: Sequence[int] | None = None,
    deadlines: Sequence[int | None] | None = None,
    post_incubate_max_wait: int | None = 15,
    post_wash_max_wait: int | None = 10,
    transfer_time: int = 0,
) -> Instance:
    """Build a multi-job labshift Instance from a collection of protocols."""
    if not protocols:
        raise ValueError("'protocols' must contain at least one protocol.")
    if releases is not None and len(releases) != len(protocols):
        raise ValueError("Length of 'releases' must match length of 'protocols'.")
    if deadlines is not None and len(deadlines) != len(protocols):
        raise ValueError("Length of 'deadlines' must match length of 'protocols'.")

    jobs: list[Job] = []
    used_ids: set[str] = set()
    for idx, proto in enumerate(protocols):
        proto_id, _ = load_protocol_steps(proto)
        cand_id = proto_id if proto_id != "protocol" else f"job_{idx + 1:02d}"
        if cand_id in used_ids:
            suffix = 2
            while f"{cand_id}_{suffix:02d}" in used_ids:
                suffix += 1
            cand_id = f"{cand_id}_{suffix:02d}"
        used_ids.add(cand_id)

        rel = releases[idx] if releases is not None else 0
        dl = deadlines[idx] if deadlines is not None else None
        jobs.append(
            job_from_protocol(
                proto,
                job_id=cand_id,
                release=rel,
                deadline=dl,
                post_incubate_max_wait=post_incubate_max_wait,
                post_wash_max_wait=post_wash_max_wait,
                transfer_time=transfer_time,
            )
        )

    if capacities is not None:
        caps = dict(capacities)
    else:
        caps = {inst: (2 if inst == "incubator" else 1) for inst in INSTRUMENTS}
        if transfer_time > 0:
            caps["robot_arm"] = 1

    return Instance(
        name=name,
        capacities=caps,
        jobs=tuple(jobs),
    )


def export_workcell_queue(
    schedule: Schedule,
    instance: Instance | None = None,
) -> list[dict[str, Any]]:
    """Export chronologically ordered station and robot_arm dispatch events."""
    events: list[tuple[tuple[int, int, int, str, int], dict[str, Any]]] = []
    by_key = schedule.by_key()

    job_lookup = {job.job_id: job for job in instance.jobs} if instance is not None else {}

    for sop in schedule.ops:
        job = job_lookup.get(sop.job_id)
        if job is not None and 0 < sop.op_index < len(job.ops):
            spec = job.ops[sop.op_index]
            prev_sop = by_key.get((sop.job_id, sop.op_index - 1))
            if spec.transfer_time > 0 and prev_sop is not None:
                t_start = sop.start - spec.transfer_time
                t_end = sop.start
                transfer_event: dict[str, Any] = {
                    "event": "transfer",
                    "job_id": sop.job_id,
                    "op_index": sop.op_index,
                    "operation": f"transfer_to_{sop.name or sop.instrument}",
                    "instrument": "robot_arm",
                    "from_instrument": prev_sop.instrument,
                    "to_instrument": sop.instrument,
                    "start": t_start,
                    "end": t_end,
                    "duration": spec.transfer_time,
                }
                events.append(
                    ((t_start, 0, t_end, sop.job_id, sop.op_index), transfer_event)
                )

        exec_event: dict[str, Any] = {
            "event": "execute",
            "job_id": sop.job_id,
            "op_index": sop.op_index,
            "operation": sop.name,
            "instrument": sop.instrument,
            "start": sop.start,
            "end": sop.end,
            "duration": sop.end - sop.start,
        }
        events.append(((sop.start, 1, sop.end, sop.job_id, sop.op_index), exec_event))

    events.sort(key=lambda item: item[0])
    return [payload for _, payload in events]
