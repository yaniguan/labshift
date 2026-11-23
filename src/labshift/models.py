"""Data models, domain validation, and JSON serialization for labshift."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

INSTRUMENTS: tuple[str, ...] = (
    "liquid_handler",
    "incubator",
    "plate_reader",
    "centrifuge",
    "washer",
)


def _check_nonempty_str(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"'{field}' must be a string, got {value!r}.")
    if not value.strip():
        raise ValueError(f"'{field}' must be a non-empty string, got {value!r}.")
    return value


def _check_int(value: Any, field: str, *, min_value: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"'{field}' must be an integer (not bool), got {value!r}.")
    if min_value is not None and value < min_value:
        raise ValueError(f"'{field}' must be >= {min_value}, got {value}.")
    return value


def _check_float(value: Any, field: str, *, min_value: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"'{field}' must be a real number (not bool), got {value!r}.")
    val = float(value)
    if not math.isfinite(val):
        raise ValueError(f"'{field}' must be finite, got {value!r}.")
    if min_value is not None and val < min_value:
        raise ValueError(f"'{field}' must be >= {min_value}, got {val}.")
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
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Operation:
        """Deserialize operation from a dictionary."""
        if not isinstance(data, dict):
            raise TypeError("Operation data must be a dictionary.")
        raw_max_wait = data.get("max_wait")
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
        )


@dataclass(frozen=True)
class Job:
    """A sequence of operations with release time and optional deadline."""

    job_id: str
    ops: tuple[Operation, ...]
    release: int = 0
    deadline: int | None = None

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

    def to_dict(self) -> dict[str, Any]:
        """Serialize job to a JSON-compatible dictionary."""
        return {
            "job_id": self.job_id,
            "release": self.release,
            "deadline": self.deadline,
            "ops": [op.to_dict() for op in self.ops],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Job:
        """Deserialize job from a dictionary."""
        if not isinstance(data, dict):
            raise TypeError("Job data must be a dictionary.")
        raw_deadline = data.get("deadline")
        raw_ops = data["ops"]
        if not isinstance(raw_ops, (list, tuple)):
            raise TypeError("'ops' must be a list of operation dictionaries.")
        return cls(
            job_id=_check_nonempty_str(data["job_id"], "job_id"),
            release=_check_int(data.get("release", 0), "release", min_value=0),
            deadline=(
                _check_int(raw_deadline, "deadline", min_value=0)
                if raw_deadline is not None
                else None
            ),
            ops=tuple(Operation.from_dict(op) for op in raw_ops),
        )


@dataclass(frozen=True)
class Instance:
    """Complete scheduling problem instance."""

    name: str
    capacities: dict[str, int]
    jobs: tuple[Job, ...]
    seed: int | None = None
    preset: str | None = None

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

    @property
    def total_operations(self) -> int:
        """Total number of operations across all jobs in the instance."""
        return sum(len(job.ops) for job in self.jobs)

    def to_dict(self) -> dict[str, Any]:
        """Serialize instance to a JSON-compatible dictionary."""
        return {
            "name": self.name,
            "preset": self.preset,
            "seed": self.seed,
            "capacities": dict(self.capacities),
            "jobs": [job.to_dict() for job in self.jobs],
        }

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

    def __post_init__(self) -> None:
        _check_nonempty_str(self.job_id, "job_id")
        _check_int(self.op_index, "op_index", min_value=0)
        _check_nonempty_str(self.instrument, "instrument")
        _check_int(self.start, "start")
        _check_int(self.end, "end")
        if not isinstance(self.name, str):
            raise TypeError(f"'name' must be a string, got {self.name!r}.")

    def to_dict(self) -> dict[str, Any]:
        """Serialize scheduled operation to a JSON-compatible dictionary."""
        return {
            "job_id": self.job_id,
            "op_index": self.op_index,
            "name": self.name,
            "instrument": self.instrument,
            "start": self.start,
            "end": self.end,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ScheduledOp:
        """Deserialize scheduled operation from a dictionary."""
        if not isinstance(data, dict):
            raise TypeError("ScheduledOp data must be a dictionary.")
        raw_name = data.get("name", "")
        if not isinstance(raw_name, str):
            raise TypeError(f"'name' must be a string, got {raw_name!r}.")
        return cls(
            job_id=_check_nonempty_str(data["job_id"], "job_id"),
            op_index=_check_int(data["op_index"], "op_index", min_value=0),
            name=raw_name,
            instrument=_check_nonempty_str(data["instrument"], "instrument"),
            start=_check_int(data["start"], "start"),
            end=_check_int(data["end"], "end"),
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
