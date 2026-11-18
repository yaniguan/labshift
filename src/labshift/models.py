"""Data models and JSON serialization for labshift instances and schedules."""

from __future__ import annotations

import json
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


@dataclass(frozen=True)
class Operation:
    """Single operation in a job chain."""

    name: str
    instrument: str
    duration: int
    min_wait: int = 0
    max_wait: int | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize operation to a JSON-compatible dictionary."""
        return {
            "name": self.name,
            "instrument": self.instrument,
            "duration": self.duration,
            "min_wait": self.min_wait,
            "max_wait": self.max_wait,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Operation:
        """Deserialize operation from a dictionary."""
        return cls(
            name=str(data["name"]),
            instrument=str(data["instrument"]),
            duration=int(data["duration"]),
            min_wait=int(data.get("min_wait", 0)),
            max_wait=int(data["max_wait"]) if data.get("max_wait") is not None else None,
        )


@dataclass(frozen=True)
class Job:
    """A sequence of operations with release time and optional deadline."""

    job_id: str
    ops: tuple[Operation, ...]
    release: int = 0
    deadline: int | None = None

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
        return cls(
            job_id=str(data["job_id"]),
            release=int(data.get("release", 0)),
            deadline=int(data["deadline"]) if data.get("deadline") is not None else None,
            ops=tuple(Operation.from_dict(op) for op in data["ops"]),
        )


@dataclass(frozen=True)
class Instance:
    """Complete scheduling problem instance."""

    name: str
    capacities: dict[str, int]
    jobs: tuple[Job, ...]
    seed: int | None = None
    preset: str | None = None

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
        return cls(
            name=str(data["name"]),
            preset=str(data["preset"]) if data.get("preset") is not None else None,
            seed=int(data["seed"]) if data.get("seed") is not None else None,
            capacities={str(k): int(v) for k, v in data["capacities"].items()},
            jobs=tuple(Job.from_dict(job) for job in data["jobs"]),
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
        return cls(
            job_id=str(data["job_id"]),
            op_index=int(data["op_index"]),
            name=str(data.get("name", "")),
            instrument=str(data["instrument"]),
            start=int(data["start"]),
            end=int(data["end"]),
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
        return cls(
            instance_name=str(data["instance_name"]),
            solver=str(data["solver"]),
            status=str(data.get("status", "OK")),
            solve_time_s=float(data.get("solve_time_s", 0.0)),
            best_bound=(
                float(data["best_bound"]) if data.get("best_bound") is not None else None
            ),
            ops=tuple(ScheduledOp.from_dict(op) for op in data["ops"]),
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
