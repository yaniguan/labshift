"""Data models, domain validation, and JSON serialization for labshift."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

BASELINE_INSTRUMENTS: tuple[str, ...] = (
    "liquid_handler",
    "incubator",
    "plate_reader",
    "centrifuge",
    "washer",
)

ORTHOGONAL_INSTRUMENTS: tuple[str, ...] = (
    "raman_spectrometer",
    "lc_ms",
)

INSTRUMENTS: tuple[str, ...] = BASELINE_INSTRUMENTS + ORTHOGONAL_INSTRUMENTS


def _check_nonempty_str(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"'{field_name}' must be a string, got {value!r}.")
    if not value.strip():
        raise ValueError(f"'{field_name}' must be a non-empty string, got {value!r}.")
    return value


def _check_int(value: Any, field_name: str, *, min_value: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"'{field_name}' must be an integer (not bool), got {value!r}.")
    if min_value is not None and value < min_value:
        raise ValueError(f"'{field_name}' must be >= {min_value}, got {value}.")
    return value


def _check_float(value: Any, field_name: str, *, min_value: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"'{field_name}' must be a real number (not bool), got {value!r}.")
    val = float(value)
    if not math.isfinite(val):
        raise ValueError(f"'{field_name}' must be finite, got {value!r}.")
    if min_value is not None and val < min_value:
        raise ValueError(f"'{field_name}' must be >= {min_value}, got {val}.")
    return val


@dataclass(frozen=True)
class Operation:
    """Single operation in a job chain."""

    name: str
    instrument: str
    duration: int
    min_wait: int = 0
    max_wait: int | None = None
    transfer_time: int = 0
    setup_tag: str | None = None
    assay_tag: str | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        _check_nonempty_str(self.name, "name")
        _check_nonempty_str(self.instrument, "instrument")
        _check_int(self.duration, "duration", min_value=1)
        _check_int(self.min_wait, "min_wait", min_value=0)
        _check_int(self.transfer_time, "transfer_time", min_value=0)
        if self.max_wait is not None:
            _check_int(self.max_wait, "max_wait", min_value=0)
            req_min = max(self.min_wait, self.transfer_time)
            if self.max_wait < req_min:
                raise ValueError(
                    f"'max_wait' ({self.max_wait}) must be >= max(min_wait, transfer_time) "
                    f"({req_min})."
                )

        if self.setup_tag is not None and self.assay_tag is not None:
            if self.setup_tag != self.assay_tag:
                raise ValueError(
                    f"Conflicting 'setup_tag' ({self.setup_tag!r}) and "
                    f"'assay_tag' ({self.assay_tag!r})."
                )
        resolved_tag = self.setup_tag if self.setup_tag is not None else self.assay_tag
        if resolved_tag is not None:
            _check_nonempty_str(resolved_tag, "setup_tag")
        object.__setattr__(self, "setup_tag", resolved_tag)
        object.__setattr__(self, "assay_tag", resolved_tag)

    def to_dict(self) -> dict[str, Any]:
        """Serialize operation to a JSON-compatible dictionary."""
        out: dict[str, Any] = {
            "name": self.name,
            "instrument": self.instrument,
            "duration": self.duration,
            "min_wait": self.min_wait,
            "max_wait": self.max_wait,
        }
        if self.transfer_time > 0:
            out["transfer_time"] = self.transfer_time
        if self.setup_tag is not None:
            out["setup_tag"] = self.setup_tag
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Operation:
        """Deserialize operation from a dictionary."""
        if not isinstance(data, dict):
            raise TypeError("Operation data must be a dictionary.")
        raw_max_wait = data.get("max_wait")
        raw_tag = data.get("setup_tag")
        if raw_tag is None:
            raw_tag = data.get("assay_tag", data.get("assay_class"))
        return cls(
            name=_check_nonempty_str(data["name"], "name"),
            instrument=_check_nonempty_str(data["instrument"], "instrument"),
            duration=_check_int(data["duration"], "duration", min_value=1),
            min_wait=_check_int(data.get("min_wait", 0), "min_wait", min_value=0),
            max_wait=(
                _check_int(raw_max_wait, "max_wait", min_value=0)
                if raw_max_wait is not None
                else None
            ),
            transfer_time=_check_int(
                data.get("transfer_time", 0), "transfer_time", min_value=0
            ),
            setup_tag=(
                _check_nonempty_str(raw_tag, "setup_tag") if raw_tag is not None else None
            ),
        )


@dataclass(frozen=True)
class Job:
    """A sequence of operations with release time and optional deadline."""

    job_id: str
    ops: tuple[Operation, ...]
    release: int = 0
    deadline: int | None = None
    assay_tag: str | None = None

    def __post_init__(self) -> None:
        _check_nonempty_str(self.job_id, "job_id")
        if not isinstance(self.ops, tuple):
            if isinstance(self.ops, list):
                object.__setattr__(self, "ops", tuple(self.ops))
            else:
                raise TypeError("'ops' must be a tuple or list of Operation instances.")
        for op in self.ops:
            if not isinstance(op, Operation):
                raise TypeError(f"Expected Operation in 'ops', got {op!r}.")
        _check_int(self.release, "release", min_value=0)
        if self.deadline is not None:
            _check_int(self.deadline, "deadline", min_value=0)
            if self.deadline < self.release:
                raise ValueError(
                    f"Job '{self.job_id}' deadline ({self.deadline}) < release ({self.release})."
                )
        if self.assay_tag is not None:
            _check_nonempty_str(self.assay_tag, "assay_tag")

    def to_dict(self) -> dict[str, Any]:
        """Serialize job to a JSON-compatible dictionary."""
        out: dict[str, Any] = {
            "job_id": self.job_id,
            "release": self.release,
            "deadline": self.deadline,
            "ops": [op.to_dict() for op in self.ops],
        }
        if self.assay_tag is not None:
            out["assay_tag"] = self.assay_tag
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Job:
        """Deserialize job from a dictionary."""
        if not isinstance(data, dict):
            raise TypeError("Job data must be a dictionary.")
        raw_deadline = data.get("deadline")
        raw_ops = data["ops"]
        if not isinstance(raw_ops, (list, tuple)):
            raise TypeError("'ops' must be a list of operation dictionaries.")
        raw_assay = data.get("assay_tag", data.get("assay_class"))
        return cls(
            job_id=_check_nonempty_str(data["job_id"], "job_id"),
            release=_check_int(data.get("release", 0), "release", min_value=0),
            deadline=(
                _check_int(raw_deadline, "deadline", min_value=0)
                if raw_deadline is not None
                else None
            ),
            ops=tuple(Operation.from_dict(op) for op in raw_ops),
            assay_tag=(
                _check_nonempty_str(raw_assay, "assay_tag")
                if raw_assay is not None
                else None
            ),
        )


def _validate_transit_matrix(
    raw: Any,
) -> dict[str, dict[str, int]] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise TypeError("'transit_matrix' must be a dict mapping from_inst -> to_inst -> int.")
    cleaned: dict[str, dict[str, int]] = {}
    for src, row in raw.items():
        src_key = _check_nonempty_str(src, "transit_matrix source key")
        if not isinstance(row, dict):
            raise TypeError(f"'transit_matrix[{src_key!r}]' must be a dictionary.")
        clean_row: dict[str, int] = {}
        for dst, val in row.items():
            dst_key = _check_nonempty_str(dst, f"transit_matrix[{src_key!r}] target key")
            clean_row[dst_key] = _check_int(
                val, f"transit_matrix[{src_key!r}][{dst_key!r}]", min_value=0
            )
        cleaned[src_key] = clean_row
    return cleaned


def _validate_setup_times(
    raw: Any,
) -> dict[str, dict[str, dict[str, int]]] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise TypeError(
            "'setup_times' must be a dict mapping instrument -> from_tag -> to_tag -> int."
        )
    cleaned: dict[str, dict[str, dict[str, int]]] = {}
    for inst, from_map in raw.items():
        inst_key = _check_nonempty_str(inst, "setup_times instrument key")
        if not isinstance(from_map, dict):
            raise TypeError(f"'setup_times[{inst_key!r}]' must be a dictionary.")
        clean_from: dict[str, dict[str, int]] = {}
        for from_tag, to_map in from_map.items():
            f_key = _check_nonempty_str(from_tag, f"setup_times[{inst_key!r}] from_tag")
            if not isinstance(to_map, dict):
                raise TypeError(
                    f"'setup_times[{inst_key!r}][{f_key!r}]' must be a dictionary."
                )
            clean_to: dict[str, int] = {}
            for to_tag, minutes in to_map.items():
                t_key = _check_nonempty_str(
                    to_tag, f"setup_times[{inst_key!r}][{f_key!r}] to_tag"
                )
                clean_to[t_key] = _check_int(
                    minutes,
                    f"setup_times[{inst_key!r}][{f_key!r}][{t_key!r}]",
                    min_value=0,
                )
            clean_from[f_key] = clean_to
        cleaned[inst_key] = clean_from
    return cleaned


@dataclass(frozen=True)
class Instance:
    """Complete scheduling problem instance."""

    name: str
    capacities: dict[str, int]
    jobs: tuple[Job, ...]
    seed: int | None = None
    preset: str | None = None
    transit_matrix: dict[str, dict[str, int]] | None = None
    setup_times: dict[str, dict[str, dict[str, int]]] | None = None
    setup_matrix: dict[str, dict[str, dict[str, int]]] | None = field(
        default=None, repr=False, compare=False
    )
    blocking_handoff: bool = True

    def __post_init__(self) -> None:
        _check_nonempty_str(self.name, "name")
        if not isinstance(self.capacities, dict):
            raise TypeError("'capacities' must be a dict mapping instrument names to ints.")
        clean_caps: dict[str, int] = {}
        for k, v in self.capacities.items():
            inst_key = _check_nonempty_str(k, "capacities key")
            clean_caps[inst_key] = _check_int(v, f"capacities[{inst_key!r}]", min_value=0)
        object.__setattr__(self, "capacities", clean_caps)

        if not isinstance(self.jobs, tuple):
            if isinstance(self.jobs, list):
                object.__setattr__(self, "jobs", tuple(self.jobs))
            else:
                raise TypeError("'jobs' must be a tuple or list of Job instances.")

        seen_ids: set[str] = set()
        for job in self.jobs:
            if not isinstance(job, Job):
                raise TypeError(f"Expected Job in 'jobs', got {job!r}.")
            if job.job_id in seen_ids:
                raise ValueError(f"Duplicate job_id '{job.job_id}' in instance '{self.name}'.")
            seen_ids.add(job.job_id)
            for op in job.ops:
                if op.instrument not in clean_caps:
                    raise ValueError(
                        f"Operation '{op.name}' in job '{job.job_id}' references instrument "
                        f"'{op.instrument}' missing from instance capacities."
                    )

        if self.seed is not None:
            _check_int(self.seed, "seed")
        if self.preset is not None:
            _check_nonempty_str(self.preset, "preset")

        if not isinstance(self.blocking_handoff, bool):
            raise TypeError(
                f"'blocking_handoff' must be a bool, got {self.blocking_handoff!r}."
            )

        clean_transit = _validate_transit_matrix(self.transit_matrix)
        object.__setattr__(self, "transit_matrix", clean_transit)

        if self.setup_times is not None and self.setup_matrix is not None:
            if self.setup_times != self.setup_matrix:
                raise ValueError("Conflicting 'setup_times' and 'setup_matrix' on Instance.")
        raw_setup = self.setup_times if self.setup_times is not None else self.setup_matrix
        clean_setup = _validate_setup_times(raw_setup)
        object.__setattr__(self, "setup_times", clean_setup)
        object.__setattr__(self, "setup_matrix", clean_setup)

    @property
    def total_operations(self) -> int:
        """Total number of operations across all jobs in the instance."""
        return sum(len(job.ops) for job in self.jobs)

    def effective_transfer_time(self, prev_instrument: str, op: Operation) -> int:
        """Return required transfer time between prev_instrument and op.instrument."""
        if prev_instrument == op.instrument:
            return op.transfer_time
        matrix_val = (
            self.transit_matrix.get(prev_instrument, {}).get(op.instrument, 0)
            if self.transit_matrix is not None
            else 0
        )
        return max(op.transfer_time, matrix_val)

    def uses_robot_arm(self, prev_instrument: str, op: Operation) -> bool:
        """Return True when transitioning from prev_instrument to op uses robot_arm."""
        return (
            "robot_arm" in self.capacities
            and prev_instrument != op.instrument
            and self.effective_transfer_time(prev_instrument, op) > 0
        )

    def effective_setup_tag(self, job: Job, op: Operation) -> str | None:
        """Return operation-level setup_tag or inherited job-level assay_tag."""
        return op.setup_tag if op.setup_tag is not None else job.assay_tag

    def required_setup_time(
        self,
        instrument: str,
        from_tag: str | None,
        to_tag: str | None,
    ) -> int:
        """Return sequence-dependent setup/decontamination/cooldown time in minutes."""
        if self.setup_times is None or from_tag is None or to_tag is None:
            return 0
        return self.setup_times.get(instrument, {}).get(from_tag, {}).get(to_tag, 0)

    def has_setup_times_for(self, instrument: str) -> bool:
        """Return True if instrument has any positive sequence-dependent setup times."""
        if self.setup_times is None:
            return False
        inst_map = self.setup_times.get(instrument)
        if not inst_map:
            return False
        return any(val > 0 for row in inst_map.values() for val in row.values())

    def to_dict(self) -> dict[str, Any]:
        """Serialize instance to a JSON-compatible dictionary."""
        out: dict[str, Any] = {
            "name": self.name,
            "preset": self.preset,
            "seed": self.seed,
            "capacities": dict(self.capacities),
            "jobs": [job.to_dict() for job in self.jobs],
        }
        if self.transit_matrix is not None:
            out["transit_matrix"] = {
                src: dict(row) for src, row in self.transit_matrix.items()
            }
        if self.setup_times is not None:
            out["setup_times"] = {
                inst: {f_tag: dict(row) for f_tag, row in f_map.items()}
                for inst, f_map in self.setup_times.items()
            }
        if not self.blocking_handoff:
            out["blocking_handoff"] = False
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Instance:
        """Deserialize instance from a dictionary."""
        if not isinstance(data, dict):
            raise TypeError("Instance data must be a dictionary.")
        raw_caps = data["capacities"]
        if not isinstance(raw_caps, dict):
            raise TypeError("'capacities' must be a dictionary.")
        raw_jobs = data["jobs"]
        if not isinstance(raw_jobs, (list, tuple)):
            raise TypeError("'jobs' must be a list of job dictionaries.")
        raw_seed = data.get("seed")
        raw_preset = data.get("preset")
        raw_transit = data.get("transit_matrix")
        raw_setup = data.get("setup_times", data.get("setup_matrix"))
        raw_blocking = data.get("blocking_handoff", True)
        if not isinstance(raw_blocking, bool):
            raise TypeError(f"'blocking_handoff' must be a bool, got {raw_blocking!r}.")
        return cls(
            name=_check_nonempty_str(data["name"], "name"),
            preset=_check_nonempty_str(raw_preset, "preset") if raw_preset is not None else None,
            seed=_check_int(raw_seed, "seed") if raw_seed is not None else None,
            capacities={
                _check_nonempty_str(k, "capacities key"): _check_int(
                    v, f"capacities[{k!r}]", min_value=0
                )
                for k, v in raw_caps.items()
            },
            jobs=tuple(Job.from_dict(job) for job in raw_jobs),
            transit_matrix=_validate_transit_matrix(raw_transit),
            setup_times=_validate_setup_times(raw_setup),
            blocking_handoff=raw_blocking,
        )

    def save_json(self, path: str | Path) -> None:
        """Write instance as formatted JSON."""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.to_dict(), indent=2) + "\n", encoding="utf-8")

    @classmethod
    def load_json(cls, path: str | Path) -> Instance:
        """Load instance from a JSON file."""
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls.from_dict(data)


@dataclass(frozen=True)
class ScheduledOp:
    """Scheduled timing for one job operation."""

    job_id: str
    op_index: int
    instrument: str
    start: int
    end: int
    name: str = ""
    unit_index: int | None = None

    def __post_init__(self) -> None:
        _check_nonempty_str(self.job_id, "job_id")
        _check_int(self.op_index, "op_index", min_value=0)
        _check_nonempty_str(self.instrument, "instrument")
        _check_int(self.start, "start")
        _check_int(self.end, "end")
        if not isinstance(self.name, str):
            raise TypeError(f"'name' must be a string, got {self.name!r}.")
        if self.unit_index is not None:
            _check_int(self.unit_index, "unit_index", min_value=0)

    def to_dict(self) -> dict[str, Any]:
        """Serialize scheduled operation to a JSON-compatible dictionary."""
        out: dict[str, Any] = {
            "job_id": self.job_id,
            "op_index": self.op_index,
            "name": self.name,
            "instrument": self.instrument,
            "start": self.start,
            "end": self.end,
        }
        if self.unit_index is not None:
            out["unit_index"] = self.unit_index
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ScheduledOp:
        """Deserialize scheduled operation from a dictionary."""
        if not isinstance(data, dict):
            raise TypeError("ScheduledOp data must be a dictionary.")
        raw_name = data.get("name", "")
        if not isinstance(raw_name, str):
            raise TypeError(f"'name' must be a string, got {raw_name!r}.")
        raw_unit = data.get("unit_index")
        return cls(
            job_id=_check_nonempty_str(data["job_id"], "job_id"),
            op_index=_check_int(data["op_index"], "op_index", min_value=0),
            name=raw_name,
            instrument=_check_nonempty_str(data["instrument"], "instrument"),
            start=_check_int(data["start"], "start"),
            end=_check_int(data["end"], "end"),
            unit_index=(
                _check_int(raw_unit, "unit_index", min_value=0)
                if raw_unit is not None
                else None
            ),
        )


@dataclass(frozen=True)
class Schedule:
    """Output schedule produced by a solver."""

    instance_name: str
    solver: str
    ops: tuple[ScheduledOp, ...]
    status: str = "OK"
    solve_time_s: float = 0.0
    best_bound: float | None = None

    def __post_init__(self) -> None:
        _check_nonempty_str(self.instance_name, "instance_name")
        _check_nonempty_str(self.solver, "solver")
        _check_nonempty_str(self.status, "status")
        if not isinstance(self.ops, tuple):
            if isinstance(self.ops, list):
                object.__setattr__(self, "ops", tuple(self.ops))
            else:
                raise TypeError("'ops' must be a tuple or list of ScheduledOp instances.")
        for sop in self.ops:
            if not isinstance(sop, ScheduledOp):
                raise TypeError(f"Expected ScheduledOp in 'ops', got {sop!r}.")
        object.__setattr__(
            self, "solve_time_s", _check_float(self.solve_time_s, "solve_time_s", min_value=0.0)
        )
        if self.best_bound is not None:
            object.__setattr__(
                self, "best_bound", _check_float(self.best_bound, "best_bound")
            )

    def by_key(self) -> dict[tuple[str, int], ScheduledOp]:
        """Map (job_id, op_index) to scheduled operation."""
        return {(op.job_id, op.op_index): op for op in self.ops}

    def to_dict(self) -> dict[str, Any]:
        """Serialize schedule to a JSON-compatible dictionary."""
        return {
            "instance_name": self.instance_name,
            "solver": self.solver,
            "status": self.status,
            "solve_time_s": self.solve_time_s,
            "best_bound": self.best_bound,
            "ops": [op.to_dict() for op in self.ops],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Schedule:
        """Deserialize schedule from a dictionary."""
        if not isinstance(data, dict):
            raise TypeError("Schedule data must be a dictionary.")
        raw_ops = data["ops"]
        if not isinstance(raw_ops, (list, tuple)):
            raise TypeError("'ops' must be a list of scheduled operation dictionaries.")
        raw_bound = data.get("best_bound")
        return cls(
            instance_name=_check_nonempty_str(data["instance_name"], "instance_name"),
            solver=_check_nonempty_str(data["solver"], "solver"),
            status=_check_nonempty_str(data.get("status", "OK"), "status"),
            solve_time_s=_check_float(data.get("solve_time_s", 0.0), "solve_time_s", min_value=0.0),
            best_bound=_check_float(raw_bound, "best_bound") if raw_bound is not None else None,
            ops=tuple(ScheduledOp.from_dict(op) for op in raw_ops),
        )

    def save_json(self, path: str | Path) -> None:
        """Write schedule as formatted JSON."""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.to_dict(), indent=2) + "\n", encoding="utf-8")

    @classmethod
    def load_json(cls, path: str | Path) -> Schedule:
        """Load schedule from a JSON file."""
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls.from_dict(data)
