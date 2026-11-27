"""Protocol importers (twinstep / stepbot) and workcell queue exporters."""

from __future__ import annotations

import dataclasses
import json
import math
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from labshift.models import BASELINE_INSTRUMENTS, Instance, Job, Operation, Schedule

ProtocolInput = Sequence[Mapping[str, Any] | Any] | Mapping[str, Any] | Any | str | Path

# Fixed liquid-handler gantry overhead (seconds) for pick_up_tip, drop_tip, and discard steps.
STEPBOT_TIP_ACTION_SECONDS: float = 5.0

SUPPORTED_STEPBOT_STEP_TYPES: tuple[str, ...] = (
    "comment",
    "delay",
    "pause",
    "pick_up_tip",
    "drop_tip",
    "transfer",
    "distribute",
    "mix",
    "discard",
    "dilution_series",
)

_TIME_UNIT_SECONDS: dict[str, float] = {
    "ms": 0.001,
    "s": 1.0,
    "sec": 1.0,
    "secs": 1.0,
    "second": 1.0,
    "seconds": 1.0,
    "m": 60.0,
    "min": 60.0,
    "mins": 60.0,
    "minute": 60.0,
    "minutes": 60.0,
    "h": 3600.0,
    "hr": 3600.0,
    "hrs": 3600.0,
    "hour": 3600.0,
    "hours": 3600.0,
    "d": 86400.0,
    "day": 86400.0,
    "days": 86400.0,
}

_VOLUME_UNIT_UL: dict[str, float] = {
    "nl": 0.001,
    "ul": 1.0,
    "ml": 1000.0,
    "l": 1_000_000.0,
}

_LIQUID_HANDLER_TYPES = frozenset(
    {"transfer", "mix", "pipette", "dispense", "distribute", "aspirate", "dilution_series"}
)
_STEPBOT_TIP_TYPES = frozenset({"pick_up_tip", "drop_tip", "discard"})
_WASHER_TYPES = frozenset({"wash", "plate_wash"})
_INCUBATOR_TYPES = frozenset({"incubate", "thermocycle", "equilibrate"})
_CENTRIFUGE_TYPES = frozenset({"spin", "centrifuge", "spin_down"})
_READER_TYPES = frozenset({"read", "measure", "plate_read"})
_RAMAN_TYPES = frozenset({"raman", "raman_read", "raman_spectroscopy", "raman_scan"})
_LCMS_TYPES = frozenset({"lc_ms", "lcms", "lc-ms", "mass_spec", "lc_ms_read"})


def _dataclass_step_to_dict(step: Any) -> dict[str, Any] | None:
    """Convert stepbot-like lowered step dataclasses to normalized step dicts without importing stepbot."""
    if not dataclasses.is_dataclass(step) or isinstance(step, type):
        return None
    cls_name = type(step).__name__
    raw = dataclasses.asdict(step)
    name_map = {
        "CommentStep": "comment",
        "DelayStep": "delay",
        "PauseStep": "pause",
        "PickUpTipStep": "pick_up_tip",
        "DropTipStep": "drop_tip",
        "TransferStep": "transfer",
        "DistributeStep": "distribute",
        "MixStep": "mix",
        "DiscardStep": "discard",
    }
    if cls_name in name_map:
        raw.setdefault("type", name_map[cls_name])
        return raw
    if "type" in raw or "action" in raw or "op" in raw:
        return raw
    return None


def _normalize_step(step: Any, step_index: int) -> dict[str, Any]:
    """Normalize a twinstep or stepbot step entry into a dictionary with a ``type`` key."""
    if isinstance(step, str):
        return {"type": "comment", "text": step}

    dc_dict = _dataclass_step_to_dict(step)
    if dc_dict is not None:
        return dc_dict

    if not isinstance(step, Mapping):
        to_dict = getattr(step, "to_dict", None)
        if callable(to_dict):
            res = to_dict()
            if isinstance(res, Mapping):
                step = res
            else:
                raise ValueError(
                    f"Step {step_index}: step.to_dict() must return a mapping, got {res!r}."
                )
        else:
            raise ValueError(
                f"Step {step_index}: protocol step must be a mapping or expose to_dict(), got {step!r}."
            )

    if any(k in step for k in ("type", "action", "op")):
        return dict(step)

    if len(step) == 1:
        kind, body = next(iter(step.items()))
        if not isinstance(kind, str) or not kind.strip():
            raise ValueError(
                f"Step {step_index}: protocol step key must be a non-empty string, got {kind!r}."
            )
        k_low = kind.strip().lower()
        if isinstance(body, Mapping):
            out = dict(body)
            out.setdefault("type", kind)
            return out
        if k_low == "comment" and (body is None or isinstance(body, str)):
            return {"type": kind, "text": body or ""}
        if (
            k_low in ("delay", "pause")
            and isinstance(body, (int, float))
            and not isinstance(body, bool)
        ):
            return {"type": kind, "seconds": body}
        if k_low == "pause" and (body is None or isinstance(body, str)):
            out_pause: dict[str, Any] = {"type": kind}
            if isinstance(body, str):
                out_pause["message"] = body
            return out_pause
        if k_low in _STEPBOT_TIP_TYPES and (body is None or isinstance(body, str)):
            out_tip: dict[str, Any] = {"type": kind}
            if isinstance(body, str):
                out_tip["pipette"] = body
            return out_tip
        raise ValueError(
            f"Step {step_index} ({kind!r}): step parameters must be a mapping, got {body!r}."
        )

    raise ValueError(
        f"Protocol step {step_index} missing non-empty 'type' (or single-key stepbot form): {dict(step)!r}."
    )


def _extract_mapping_proto_id(raw: Mapping[str, Any], fallback: str) -> str:
    direct = raw.get("protocol_id") or raw.get("name")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    meta = raw.get("metadata")
    if isinstance(meta, Mapping):
        meta_name = meta.get("name") or meta.get("protocolName")
        if isinstance(meta_name, str) and meta_name.strip():
            return meta_name.strip()
    return fallback


def load_protocol_steps(source: ProtocolInput) -> tuple[str, list[dict[str, Any]]]:
    """Load protocol identifier and normalized step dictionaries from a path, mapping, spec, or list."""
    if isinstance(source, (str, Path)):
        path = Path(source)
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, list):
            return path.stem, [_normalize_step(item, idx) for idx, item in enumerate(raw)]
        if isinstance(raw, Mapping):
            proto_id = _extract_mapping_proto_id(raw, path.stem)
            steps_raw = raw.get("steps") if "steps" in raw else raw.get("commands")
            if not isinstance(steps_raw, Sequence) or isinstance(steps_raw, (str, bytes)):
                raise TypeError(f"Protocol mapping in '{path}' must contain a 'steps' list.")
            return proto_id, [_normalize_step(item, idx) for idx, item in enumerate(steps_raw)]
        raise TypeError(f"Protocol JSON '{path}' must be a list of steps or a mapping.")

    if isinstance(source, Mapping):
        proto_id = _extract_mapping_proto_id(source, "protocol")
        steps_raw = source.get("steps") if "steps" in source else source.get("commands")
        if not isinstance(steps_raw, Sequence) or isinstance(steps_raw, (str, bytes)):
            raise TypeError("Protocol mapping must contain a 'steps' or 'commands' list.")
        return proto_id, [_normalize_step(item, idx) for idx, item in enumerate(steps_raw)]

    to_dicts = getattr(source, "to_dicts", None)
    if callable(to_dicts):
        proto_id = str(getattr(source, "protocol_id", None) or getattr(source, "name", None) or "protocol")
        return proto_id, [_normalize_step(item, idx) for idx, item in enumerate(to_dicts())]

    spec_steps = getattr(source, "steps", None)
    if isinstance(spec_steps, Sequence) and not isinstance(spec_steps, (str, bytes)):
        proto_id = str(getattr(source, "protocol_id", None) or getattr(source, "name", None) or "protocol")
        return proto_id, [_normalize_step(item, idx) for idx, item in enumerate(spec_steps)]

    if isinstance(source, Sequence) and not isinstance(source, (str, bytes)):
        return "protocol", [_normalize_step(item, idx) for idx, item in enumerate(source)]

    raise ValueError(f"Unsupported protocol input: {source!r}.")


def _volume_ul(step: Mapping[str, Any], default: float = 100.0) -> float:
    raw_vol = step.get("volume", default)
    if (
        isinstance(raw_vol, bool)
        or not isinstance(raw_vol, (int, float))
        or not math.isfinite(float(raw_vol))
        or raw_vol < 0
    ):
        raise ValueError(f"Invalid step volume: {raw_vol!r}.")
    raw_unit = step.get("unit", "uL")
    if not isinstance(raw_unit, str) or not raw_unit.strip():
        raise ValueError(f"Unsupported volume unit {raw_unit!r}. Expected 'nL', 'uL', 'mL', or 'L'.")
    factor = _VOLUME_UNIT_UL.get(raw_unit.strip().lower())
    if factor is None:
        raise ValueError(
            f"Unsupported volume unit '{raw_unit}'. Expected 'nL', 'uL', 'mL', or 'L'."
        )
    return float(raw_vol) * factor


def _resolve_time_unit_factor(
    step: Mapping[str, Any],
    *,
    step_index: int,
    step_type: str,
) -> float:
    """Resolve case-insensitive time unit multiplier in seconds, defaulting to minutes (60.0)
    only when neither ``time_unit`` nor ``unit`` is provided."""
    if "time_unit" in step and step["time_unit"] is not None:
        raw_unit = step["time_unit"]
        if isinstance(raw_unit, str):
            factor = _TIME_UNIT_SECONDS.get(raw_unit.strip().lower())
            if factor is not None:
                return factor
        raise ValueError(
            f"Step {step_index} ({step_type}): unsupported time unit {raw_unit!r}; "
            "expected ms, s/sec/secs/second/seconds, m/min/mins/minute/minutes, "
            "h/hr/hrs/hour/hours, or d/day/days."
        )

    if "unit" in step and step["unit"] is not None:
        raw_unit = step["unit"]
        if isinstance(raw_unit, str):
            norm = raw_unit.strip().lower()
            if norm in _TIME_UNIT_SECONDS:
                return _TIME_UNIT_SECONDS[norm]
            if norm in _VOLUME_UNIT_UL:
                raise ValueError(
                    f"Step {step_index} ({step_type}): unit {raw_unit!r} is a volume unit, "
                    "not a time unit; specify 'time_unit' explicitly for step time/duration."
                )
        raise ValueError(
            f"Step {step_index} ({step_type}): unsupported time unit {raw_unit!r}; "
            "specify a valid 'time_unit'."
        )

    return 60.0


def _time_seconds(
    step: Mapping[str, Any],
    default_min: float,
    *,
    step_index: int = 0,
    step_type: str = "step",
) -> float:
    """Return step duration in seconds from ``time`` or ``duration``.

    Uses an explicit case-insensitive time-unit table (``ms``, ``s``..``seconds``,
    ``m``..``minutes``, ``h``..``hours``, ``d``..``days``). When a step specifies ``time``
    or ``duration`` with no unit key at all, the value defaults to **minutes**. Volume units
    (``nL``, ``uL``, ``mL``, ``L``) in ``unit`` are rejected with a ``ValueError`` asking
    for ``time_unit``.
    """
    raw_time = step.get("time", step.get("duration"))
    if raw_time is None:
        if "time_unit" in step and step["time_unit"] is not None:
            _ = _resolve_time_unit_factor(step, step_index=step_index, step_type=step_type)
        return default_min * 60.0
    if (
        isinstance(raw_time, bool)
        or not isinstance(raw_time, (int, float))
        or not math.isfinite(float(raw_time))
        or raw_time <= 0
    ):
        raise ValueError(
            f"Step {step_index} ({step_type}): invalid step time/duration {raw_time!r}."
        )
    factor = _resolve_time_unit_factor(step, step_index=step_index, step_type=step_type)
    return float(raw_time) * factor


def _stepbot_seconds_minutes(
    step: Mapping[str, Any],
    *,
    step_index: int,
    step_type: str,
) -> float:
    """Parse stepbot ``seconds`` + ``60 * minutes`` fields into seconds."""
    has_sec = "seconds" in step and step["seconds"] is not None
    has_min = "minutes" in step and step["minutes"] is not None
    if not has_sec and not has_min:
        raise ValueError(
            f"Step {step_index} ({step_type}): requires 'seconds' and/or 'minutes'."
        )
    sec_val = 0.0
    min_val = 0.0
    if has_sec:
        raw_s = step["seconds"]
        if (
            isinstance(raw_s, bool)
            or not isinstance(raw_s, (int, float))
            or not math.isfinite(float(raw_s))
            or raw_s < 0
        ):
            raise ValueError(
                f"Step {step_index} ({step_type}): 'seconds' must be a finite number >= 0, got {raw_s!r}."
            )
        sec_val = float(raw_s)
    if has_min:
        raw_m = step["minutes"]
        if (
            isinstance(raw_m, bool)
            or not isinstance(raw_m, (int, float))
            or not math.isfinite(float(raw_m))
            or raw_m < 0
        ):
            raise ValueError(
                f"Step {step_index} ({step_type}): 'minutes' must be a finite number >= 0, got {raw_m!r}."
            )
        min_val = float(raw_m)
    return sec_val + 60.0 * min_val


def _classify_step(
    step: Mapping[str, Any],
    *,
    step_index: int = 0,
    pause_minutes: float | None = None,
) -> tuple[str, str | None, float]:
    """Return ``(step_type, instrument_or_none, estimated_seconds)`` for one protocol step."""
    raw_type = step.get("type", step.get("action", step.get("op")))
    if not isinstance(raw_type, str) or not raw_type.strip():
        raise ValueError(f"Protocol step {step_index} missing non-empty 'type': {step!r}.")
    step_type = raw_type.strip().lower()

    if step_type == "comment":
        return "comment", None, 0.0

    if step_type == "delay":
        if ("seconds" in step and step["seconds"] is not None) or (
            "minutes" in step and step["minutes"] is not None
        ):
            secs = _stepbot_seconds_minutes(step, step_index=step_index, step_type="delay")
        elif "time" in step or "duration" in step:
            secs = _time_seconds(
                step, default_min=1.0, step_index=step_index, step_type="delay"
            )
        else:
            raise ValueError(
                f"Step {step_index} (delay): requires 'seconds', 'minutes', 'time', or 'duration'."
            )
        return "delay", "liquid_handler", secs

    if step_type == "pause":
        if ("seconds" in step and step["seconds"] is not None) or (
            "minutes" in step and step["minutes"] is not None
        ):
            secs = _stepbot_seconds_minutes(step, step_index=step_index, step_type="pause")
        elif ("time" in step and step["time"] is not None) or (
            "duration" in step and step["duration"] is not None
        ):
            secs = _time_seconds(
                step, default_min=1.0, step_index=step_index, step_type="pause"
            )
        elif pause_minutes is not None:
            secs = float(pause_minutes) * 60.0
        else:
            raise ValueError(
                f"Step {step_index} (pause): operator pause has no duration ('seconds'/'minutes'); "
                "pass pause_minutes to estimate resume time."
            )
        return "pause", "liquid_handler", secs

    if step_type in _STEPBOT_TIP_TYPES:
        if "duration" in step or "time" in step:
            secs = _time_seconds(
                step,
                default_min=STEPBOT_TIP_ACTION_SECONDS / 60.0,
                step_index=step_index,
                step_type=step_type,
            )
        else:
            if "volume" in step:
                _ = _volume_ul(step, default=100.0)
            secs = STEPBOT_TIP_ACTION_SECONDS
        return step_type, "liquid_handler", secs

    if step_type in _LIQUID_HANDLER_TYPES:
        if "duration" in step or "time" in step:
            secs = _time_seconds(
                step, default_min=1.0, step_index=step_index, step_type=step_type
            )
        elif step_type == "mix":
            raw_cycles = step.get("cycles", step.get("reps", step.get("repetitions", 3)))
            if isinstance(raw_cycles, bool) or not isinstance(raw_cycles, int) or raw_cycles < 1:
                raise ValueError(f"Step {step_index} (mix): invalid mix cycles/reps: {raw_cycles!r}.")
            _ = _volume_ul(step, default=100.0)
            secs = 15.0 + 6.0 * raw_cycles
        elif step_type == "dilution_series":
            raw_n = step.get("n", step.get("count", step.get("steps", step.get("columns", 8))))
            if isinstance(raw_n, bool) or not isinstance(raw_n, int) or raw_n < 1:
                raise ValueError(
                    f"Step {step_index} (dilution_series): invalid well count 'n': {raw_n!r}."
                )
            default_mix_reps = 3 if ("n" in step or "count" in step or "first_well" in step) else 0
            raw_mix_reps = step.get("mix_reps", default_mix_reps)
            if isinstance(raw_mix_reps, bool) or not isinstance(raw_mix_reps, int) or raw_mix_reps < 0:
                raise ValueError(
                    f"Step {step_index} (dilution_series): invalid 'mix_reps': {raw_mix_reps!r}."
                )
            secs = 60.0 + 15.0 * raw_n + 6.0 * max(0, raw_n - 1) * raw_mix_reps
        else:
            vol_ul = _volume_ul(step, default=100.0)
            secs = 30.0 + 0.05 * vol_ul
        return step_type, "liquid_handler", secs

    if step_type in _WASHER_TYPES:
        if "duration" in step or "time" in step:
            secs = _time_seconds(
                step, default_min=2.0, step_index=step_index, step_type=step_type
            )
        else:
            raw_cycles = step.get("cycles", step.get("reps", 3))
            if isinstance(raw_cycles, bool) or not isinstance(raw_cycles, int) or raw_cycles < 1:
                raise ValueError(f"Step {step_index} ({step_type}): invalid wash cycles: {raw_cycles!r}.")
            _ = _volume_ul(step, default=300.0)
            secs = 60.0 + 30.0 * raw_cycles
        return step_type, "washer", secs

    if step_type in _INCUBATOR_TYPES:
        secs = _time_seconds(step, default_min=15.0, step_index=step_index, step_type=step_type)
        return step_type, "incubator", secs

    if step_type in _CENTRIFUGE_TYPES:
        secs = _time_seconds(step, default_min=5.0, step_index=step_index, step_type=step_type)
        return step_type, "centrifuge", secs

    if step_type in _READER_TYPES:
        secs = _time_seconds(step, default_min=2.0, step_index=step_index, step_type=step_type)
        return step_type, "plate_reader", secs

    if step_type in _RAMAN_TYPES:
        secs = _time_seconds(step, default_min=5.0, step_index=step_index, step_type=step_type)
        return step_type, "raman_spectrometer", secs

    if step_type in _LCMS_TYPES:
        secs = _time_seconds(step, default_min=8.0, step_index=step_index, step_type=step_type)
        return step_type, "lc_ms", secs

    raise ValueError(f"Unsupported protocol step type '{step_type}' at step {step_index}.")


def job_from_protocol(
    protocol: ProtocolInput,
    *,
    job_id: str | None = None,
    release: int = 0,
    deadline: int | None = None,
    pause_minutes: float | None = None,
    post_incubate_max_wait: int | None = 15,
    post_wash_max_wait: int | None = 10,
    transfer_time: int = 0,
    transit_matrix: Mapping[str, Mapping[str, int]] | None = None,
    assay_tag: str | None = None,
) -> Job:
    """Convert a twinstep or stepbot protocol into a scheduled labshift Job.

    Groups contiguous liquid-handling steps (``transfer``, ``distribute``, ``mix``,
    ``dispense``, ``dilution_series``, ``pick_up_tip``, ``drop_tip``, ``discard``,
    ``delay``, ``pause``) onto ``liquid_handler``, wash steps onto ``washer``,
    incubation steps onto ``incubator``, spin steps onto ``centrifuge``, read steps
    onto ``plate_reader``, and orthogonal assays onto ``raman_spectrometer`` / ``lc_ms``,
    estimating integer-minute durations (>= 1 min). ``comment`` steps take zero time.

    When a step specifies ``time`` or ``duration`` without ``time_unit`` or ``unit``,
    the value defaults to **minutes**.
    """
    if pause_minutes is not None and (
        isinstance(pause_minutes, bool)
        or not isinstance(pause_minutes, (int, float))
        or not math.isfinite(float(pause_minutes))
        or pause_minutes < 0
    ):
        raise ValueError("'pause_minutes' must be a finite number >= 0 or None.")
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
    resolved_assay_tag = assay_tag
    if resolved_assay_tag is None and isinstance(protocol, Mapping):
        raw_assay = protocol.get("assay_tag", protocol.get("assay_class"))
        if isinstance(raw_assay, str) and raw_assay.strip():
            resolved_assay_tag = raw_assay

    groups: list[tuple[str, list[str], float, str | None]] = []
    for step_idx, step in enumerate(steps):
        s_type, inst, secs = _classify_step(
            step, step_index=step_idx, pause_minutes=pause_minutes
        )
        if inst is None:
            continue
        raw_step_tag = step.get("setup_tag", step.get("assay_tag", step.get("assay_class")))
        step_tag = (
            str(raw_step_tag)
            if isinstance(raw_step_tag, str) and raw_step_tag.strip()
            else None
        )
        if groups and groups[-1][0] == inst and groups[-1][3] == step_tag:
            prev_inst_g, prev_types, prev_secs, prev_tag = groups[-1]
            if s_type not in prev_types:
                prev_types.append(s_type)
            groups[-1] = (prev_inst_g, prev_types, prev_secs + secs, prev_tag)
        else:
            groups.append((inst, [s_type], secs, step_tag))

    if not groups:
        raise ValueError("Protocol must contain at least one executable (non-comment) step.")

    ops: list[Operation] = []
    for idx, (inst, step_types, total_secs, step_tag) in enumerate(groups):
        op_name = "_".join(step_types[:3])
        duration = max(1, math.ceil(total_secs / 60.0))
        min_wait = 0
        op_transfer = 0
        max_wait: int | None = None

        if idx > 0:
            prev_inst = groups[idx - 1][0]
            if inst != prev_inst:
                matrix_t = (
                    int(transit_matrix.get(prev_inst, {}).get(inst, 0))
                    if transit_matrix is not None
                    else 0
                )
                op_transfer = max(transfer_time, matrix_t)
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
                setup_tag=step_tag,
            )
        )

    return Job(
        job_id=resolved_job_id,
        ops=tuple(ops),
        release=release,
        deadline=deadline,
        assay_tag=resolved_assay_tag,
    )


def job_from_stepbot_spec(
    spec: ProtocolInput,
    *,
    job_id: str | None = None,
    release: int = 0,
    deadline: int | None = None,
    pause_minutes: float | None = None,
    post_incubate_max_wait: int | None = 15,
    post_wash_max_wait: int | None = 10,
    transfer_time: int = 0,
    transit_matrix: Mapping[str, Mapping[str, int]] | None = None,
    assay_tag: str | None = None,
) -> Job:
    """Import an already-parsed stepbot protocol dict, step list, JSON path, or ProtocolSpec into a Job.

    Caller parses YAML externally (e.g. ``yaml.safe_load``) so core ``labshift`` stays stdlib-only.
    Supports single-key stepbot steps (``- transfer: {...}``, ``- delay: 5``, ``- comment: ...``),
    ``dilution_series`` (``n``, ``mix_reps``), ``pick_up_tip``/``drop_tip``/``discard``, and ``pause``.
    """
    return job_from_protocol(
        spec,
        job_id=job_id,
        release=release,
        deadline=deadline,
        pause_minutes=pause_minutes,
        post_incubate_max_wait=post_incubate_max_wait,
        post_wash_max_wait=post_wash_max_wait,
        transfer_time=transfer_time,
        transit_matrix=transit_matrix,
        assay_tag=assay_tag,
    )


def instance_from_protocols(
    protocols: Sequence[ProtocolInput],
    *,
    name: str = "protocol_batch",
    capacities: Mapping[str, int] | None = None,
    releases: Sequence[int] | None = None,
    deadlines: Sequence[int | None] | None = None,
    pause_minutes: float | None = None,
    post_incubate_max_wait: int | None = 15,
    post_wash_max_wait: int | None = 10,
    transfer_time: int = 0,
    transit_matrix: Mapping[str, Mapping[str, int]] | None = None,
    setup_times: Mapping[str, Mapping[str, Mapping[str, int]]] | None = None,
) -> Instance:
    """Build a multi-job labshift Instance from a collection of twinstep or stepbot protocols."""
    if not protocols:
        raise ValueError("'protocols' must contain at least one protocol.")
    if releases is not None and len(releases) != len(protocols):
        raise ValueError("Length of 'releases' must match length of 'protocols'.")
    if deadlines is not None and len(deadlines) != len(protocols):
        raise ValueError("Length of 'deadlines' must match length of 'protocols'.")

    clean_transit: dict[str, dict[str, int]] | None = (
        {
            str(src): {str(dst): int(val) for dst, val in row.items()}
            for src, row in transit_matrix.items()
        }
        if transit_matrix is not None
        else None
    )
    clean_setup: dict[str, dict[str, dict[str, int]]] | None = (
        {
            str(inst): {
                str(f_tag): {str(t_tag): int(val) for t_tag, val in row.items()}
                for f_tag, row in f_map.items()
            }
            for inst, f_map in setup_times.items()
        }
        if setup_times is not None
        else None
    )

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
                pause_minutes=pause_minutes,
                post_incubate_max_wait=post_incubate_max_wait,
                post_wash_max_wait=post_wash_max_wait,
                transfer_time=transfer_time,
                transit_matrix=clean_transit,
            )
        )

    if capacities is not None:
        caps = dict(capacities)
    else:
        caps = {inst: (2 if inst == "incubator" else 1) for inst in BASELINE_INSTRUMENTS}
        for job in jobs:
            for op in job.ops:
                caps.setdefault(op.instrument, 1)
        has_matrix_transfer = clean_transit is not None and any(
            val > 0 for row in clean_transit.values() for val in row.values()
        )
        if transfer_time > 0 or has_matrix_transfer:
            caps["robot_arm"] = 1

    return Instance(
        name=name,
        capacities=caps,
        jobs=tuple(jobs),
        transit_matrix=clean_transit,
        setup_times=clean_setup,
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
        if job is not None and instance is not None and 0 < sop.op_index < len(job.ops):
            spec = job.ops[sop.op_index]
            prev_sop = by_key.get((sop.job_id, sop.op_index - 1))
            if prev_sop is not None and sop.instrument != prev_sop.instrument:
                eff_transfer = instance.effective_transfer_time(prev_sop.instrument, spec)
                if eff_transfer > 0:
                    t_start = sop.start - eff_transfer
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
                        "duration": eff_transfer,
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
