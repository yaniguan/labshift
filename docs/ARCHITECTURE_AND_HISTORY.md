# `labshift` — Architecture, Benchmarks, and Engineering History

> **Auto-Lab Ecosystem Role:** Layer `L5 · Executive Planning, Procurement & Scheduling` in [Robot AI Auto-Lab (`auto-lab`)](https://github.com/yaniguan/auto-lab) ([Project Studio](https://yaniguan.github.io/auto-lab/)).

---

## 1. Object

### Problem Statement
Multi-assay self-driving laboratories must multiplex concurrent liquid-handling workflows across shared, finite-capacity instrument pools (`liquid_handler`, `incubator`, `plate_reader`, `centrifuge`, `washer`, and optional `robot_arm`). Unlike classical unconstrained job-shop problems, wet-lab chains impose **coupled inter-step wait windows**: minimum equilibration delays (`min_wait`), robotic plate-handoff transit delays (`transfer_time`), and upper perishability bounds (`max_wait`, e.g., reading or spinning a plate before thermal drift, evaporation, or signal decay), alongside job release times and hard or soft completion deadlines. Myopic dispatchers frequently stall a job in front of a saturated reader/washer past its `max_wait` window.

`labshift` provides a reproducible synthetic and protocol-bridged lab scheduling benchmark, an independent sweep-line feasibility checker, an `O(K log K)` greedy baseline, a pure-stdlib window-aware coupled right-shift heuristic (`greedy_repair`), a warm-started OR-Tools CP-SAT interval/cumulative solver, optimality-gap metrics, and an ASCII Gantt visualizer.

### Role in the Auto-Lab `L0`–`L6` Hierarchy
Within the [Auto-Lab](https://github.com/yaniguan/auto-lab) Brain/Body architecture:
- **Layer (`L5`)**: Acts as the executive multi-instrument scheduling and resource-allocation engine alongside experimental decision-making ([`lab-decide`](https://github.com/yaniguan/lab-decide)) and procurement planning ([`buy-or-make`](https://github.com/yaniguan/buy-or-make)).
- **Bridge from `L3` Protocol Graphs**: Imports concrete step-level protocols from [`twinstep`](https://github.com/yaniguan/twinstep) / [`stepbot`](https://github.com/yaniguan/stepbot) via `labshift.protocol.job_from_protocol` and exports chronological station + `robot_arm` dispatch queues via `export_workcell_queue`.
- **Escalation from `L3` Tensor Heuristics**: Whereas [`labfill`](https://github.com/yaniguan/labfill) Recipe 3 provides fast tensor-batched parallel simulated annealing for unconstrained single-step assignment, `labshift` handles chained multi-operation jobs with `min_wait`/`max_wait`/`transfer_time` windows, release times, deadlines, and dual optimality bounds via OR-Tools CP-SAT.

### I/O Contract & Scope Boundaries

| Command / Module | Inputs | Outputs |
|---|---|---|
| `labshift generate` (`generator.py`) | `--preset` (`small\|medium\|large\|hard`), `--seed`, `--ensure-feasible`, `--out` or `--suite-dir` | Validated `Instance` JSON (`name`, `preset`, `seed`, `capacities`, `jobs`) |
| `labshift solve` (`cli.py`, `bench.py`) | `Instance` JSON, `--solver` (`greedy\|greedy_repair\|cpsat`), `--time-limit`, `--workers`, `--seed`, `--tie-break-flow`, `--soft-deadlines`, `--out` | `Schedule` JSON (`instance_name`, `solver`, `status`, `solve_time_s`, `best_bound`, `ops`) |
| `labshift bench` (`bench.py`) | Instance file/directory, `--solver`, CP-SAT flags, optional `--out-dir` and `--json` | `BenchRow` ASCII summary table (excluding unsolved rows from averages) + optional JSON report |
| `labshift check` (`checker.py`, `metrics.py`) | `Instance` JSON + `Schedule` JSON | `CheckResult` (`feasible`, typed `Violation` tuple), `Metrics`, exit code `0`/`1` |
| `labshift gantt` (`gantt.py`) | `Instance` JSON (+ optional `--schedule`, `--save-schedule`, `--solver`, `--width >= 10`) | Job-oriented ASCII Gantt timeline (`[L]`, `[I]`, `[R]`, `[C]`, `[W]`, optional `[A]`, `*` on cell collisions) |
| `labshift.protocol` (`protocol.py`) | `twinstep` / `stepbot` step dicts or JSON paths | `Job`, multi-job `Instance`, and chronological `export_workcell_queue` dispatch list |

**Scope Boundaries:**
- Models discrete integer-minute station durations, `transfer_time` gaps, and station/`robot_arm` pool capacities; does not model continuous 3D collision kinematics or tip/reagent inventory depletion.
- `ortools>=9.8` is lazy-loaded via PEP 562 (`labshift.__getattr__` and `bench.solve_instance`) so stdlib-only deployments (`--no-deps`) execute all models, generators, checkers, protocol adapters, and `greedy`/`greedy_repair` solvers without C++ solver wheels.

---

## 2. Data

### Validated Instance & Schedule Schema (`src/labshift/models.py`)
All domain objects are immutable (`frozen=True`) dataclasses enforcing strict type/domain invariants in `__post_init__` and `from_dict` (rejecting `bool` coerced as `int`, empty strings, non-positive durations, inverted windows, negative capacities, missing instruments, and duplicate `job_id`s):
- **`Operation`**: `name: str`, `instrument: str`, `duration: int >= 1` (minutes), `min_wait: int >= 0`, `max_wait: int | None` (`>= max(min_wait, transfer_time)`), `transfer_time: int >= 0` (omitted in JSON when `0` for exact v1 compatibility).
- **`Job`**: `job_id: str`, `ops: tuple[Operation, ...]`, `release: int >= 0`, `deadline: int | None` (`>= release`).
- **`Instance`**: `name: str`, `capacities: dict[str, int]` (`>= 0`, covering every `op.instrument` plus optional `"robot_arm"`), unique-ID `jobs: tuple[Job, ...]`, `seed: int | None`, `preset: str | None`.
- **`ScheduledOp` & `Schedule`**: `(job_id, op_index, name, instrument, start, end)` tuples plus solver `status`, `solve_time_s >= 0`, and optional CP-SAT makespan `best_bound`.

### Seeded Workflow Templates & Presets (`src/labshift/generator.py`)
`generate_instance` samples from 6 canonical 3-to-6-step liquid-handling workflow templates (`plate_prep`, `dilution_series`, `buffer_add`, `dye_dispense`, `mix_wash`, `plate_wash`, `spin_down`, `incubate`, `equilibrate`, `plate_read`). Non-initial operations draw `min_wait in {0, 0, 1, 2, 3}` min; transitions following `incubator`/`centrifuge` or entering `plate_read` receive `max_wait = min_wait + Uniform(max_wait_range)` with probability `max_wait_prob`.

| Preset | Jobs (`J`) | Ops / Job | `release_max` | Capacities `(L, I, R, C, W)` | Reader Arrival Rate | `max_wait_prob` (`range`) | Suite Seeds |
|---|---:|---|---:|---|---:|---|---|
| `small` | `6` | `3..4` | `20` m | `(2, 2, 1, 1, 1)` | `0.300` jobs/m/R | `0.00` (`15..30` m) | `1000..1009` (`benchmarks/small_00..09.json`) |
| `medium` | `12` | `4..5` | `40` m | `(2, 3, 1, 2, 2)` | `0.300` jobs/m/R | `0.50` (`6..16` m) | `1100..1109` (`benchmarks/medium_00..09.json`) |
| `large` | `20` | `4..6` | `60` m | `(3, 4, 2, 2, 2)` | `0.167` jobs/m/R | `0.60` (`6..16` m) | `1200..1209` (`benchmarks/large_00..09.json`) |
| `hard` | `20` | `4..6` | `45` m | `(2, 3, 1, 2, 2)` | `0.444` jobs/m/R | `0.65` (`6..14` m) | On-demand stress tier (e.g. `1300..1309`) |

The committed 30-instance v1 suite in `benchmarks/` (MIT licensed) is bit-for-bit reproducible via `generate_benchmark_suite("benchmarks", count_per_preset=10, base_seed=1000)`. In v1, `large` models a scaled-out workcell with 2 `plate_reader`s (`0.167` jobs/min/reader vs `0.300` in `medium`); the `hard` preset retains a single `plate_reader` across 20 jobs (`0.444` jobs/min/reader) so bottleneck contention scales monotonically above `medium`. Passing `ensure_feasible=True` verifies solvability and widens over-tight sampled windows if needed.

---

## 3. Model

### 3.1 Independent Sweep-Line Feasibility Checker (`src/labshift/checker.py`)
`check_schedule(instance, schedule)` audits any candidate schedule across four checks without relying on solver internals:
1. **Instance Identity**: Verifies `schedule.instance_name == instance.name` (`instance_mismatch`).
2. **Completeness, Routing & Duration Integrity (`_check_completeness_and_durations`)**: Verifies a 1-to-1 bijection over expected `(job_id, op_index)` pairs (`missing_op`, `extra_op`), checks `sop.instrument in instance.capacities` (`unknown_instrument`), `sop.instrument == expected_instrument` (`wrong_instrument`), `sop.start >= 0` (`negative_start`), and `sop.end - sop.start == expected_duration` (`duration`).
3. **Intra-Job Temporal Constraints (`_check_job_constraints`)**: Checks `first.start >= job.release` (`release`); for each consecutive pair `(idx - 1, idx)`, evaluates `wait = curr.start - prev.end` independently against `wait >= 0` (`precedence`), `wait >= min_wait` when `min_wait > 0` (`min_wait`), `wait >= transfer_time` when `transfer_time > 0` (`transfer_time`), and `wait <= max_wait` (`max_wait`); checks `last.end <= job.deadline` (`deadline`).
4. **Interval Event Sweep-Line (`_check_capacities`)**: Checks station intervals `[start, end)` plus `"robot_arm"` transit intervals `[curr.start - transfer_time, curr.start)` when `"robot_arm"` is in `instance.capacities`, rejecting `cap <= 0` usage (`capacity`) and sorting `(timestamp, delta)` with departures `(t, -1)` before arrivals `(t, +1)` to detect `active > cap` excursions.

### 3.2 `O(K log K)` Greedy Baseline & Coupled Right-Shift Heuristic (`src/labshift/greedy.py`)
- **`solve_greedy`**: Dispatches ready operations in `(ready_time, deadline_or_inf, op.duration, job_id)` order using an `O(K log K)` aggregated event-profile sweep (`_earliest_slot`) across station and optional `robot_arm` intervals, returning `INFEASIBLE` immediately if any required pool has `cap <= 0`.
- **`solve_greedy_repair`**: Partitions each job into contiguous `max_wait`-coupled operation segments `[a..b]` and schedules each coupled segment atomically via right-shift propagation (delaying `op_a` whenever a downstream bottleneck would push `op_k` past `e_{k-1} + max_wait_k`), evaluating deterministic priority rules (`earliest`, `tight_first`, `shift_coupled`, `slack`, `edf`) and selecting the minimum-violation, minimum-makespan schedule.

### 3.3 Warm-Started OR-Tools CP-SAT Model (`src/labshift/cpsat.py`)
`solve_cpsat(instance, time_limit=5.0, num_workers=4, seed=None, tie_break_flow=False, hard_deadlines=True, tardiness_weight=10)` formulates constrained scheduling in CP-SAT:
- **Heuristic Warm-Start & Tight Bounds**: Runs `solve_greedy` / `solve_greedy_repair` (< 1 ms) to tighten the horizon upper bound and register `model.add_hint` start/end hints, and propagates per-job forward earliest starts (`est`) and backward latest completions (`lct`) onto every `IntVar` domain (returning `INFEASIBLE` immediately if `est > lct - duration`).
- **Temporal & Arm Coupling**: Enforces `s_{j,k} >= e_{j,k-1} + max(min_wait, transfer_time)`, optional `s_{j,k} <= e_{j,k-1} + max_wait`, optional `robot_arm` `IntervalVar` over `[s_{j,k} - transfer_time, s_{j,k})`, and either hard `e_{j,last} <= deadline_j` (`hard_deadlines=True`) or soft tardiness variables `tard_j >= e_{j,last} - deadline_j` (`hard_deadlines=False`).
- **Objectives & Exact Makespan Bound Unscaling**: Minimizes `makespan` (or `makespan + tardiness_weight * sum(tard_j)`), optionally scaled by `flow_scale = J * H + 1` plus `sum(e_{j,last} - release_j)` when `tie_break_flow=True`, recovering an exact integer makespan dual lower bound `ceil((best_objective_bound - (flow_scale - 1)) / flow_scale)` (including on `UNKNOWN` timeouts when `best_objective_bound > 0`).

---

## 4. Training

### Combinatorial Benchmark & RLVR Reward Oracle Role
`labshift` trains **no neural parameters** internally and requires no GPU. Within Auto-Lab's learning and planning stack, it provides:
1. **Verifiable Scheduling Environment (RLVR)**: `check_schedule` and `compute_metrics` act as a deterministic, millisecond-latency verifier and dense/sparse reward oracle (`feasible`, `makespan`, `window_violations`, `total_tardiness`, and un-clamped `gap_pct` relative to CP-SAT dual bounds) in lightweight `--no-deps` Python environments.
2. **Ground-Truth Solver Supervision**: `solve_cpsat(..., tie_break_flow=True, num_workers=1, seed=0)` and `solve_greedy_repair` generate deterministic feasible trajectories (`30/30` on `benchmarks/`) for imitation learning or preference pair construction against infeasible myopic `greedy` rollouts.

### Measured CPU Benchmark Summary (30-Instance v1 Suite)
The `greedy` and `cpsat` rows were measured on CPU on 2026-11-18 (`--time-limit 5` s per instance for `cpsat`); `greedy_repair` was measured on CPU on 2026-11-22 against the identical 30 committed `benchmarks/*.json` files.

| Solver | Solved | Feasible | `small` Feas | `medium` Feas | `large` Feas | Mean Makespan (m) | Total Tardiness | Mean Flow (m) | Window Viols | Mean `gap%` | Total Time (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `greedy` | `30/30` | **`24/30`** (`80.0%`) | `10/10` | `4/10` | `10/10` | `129.8` | `0` | `72.3` | `13` | — | `0.012` |
| `greedy_repair` (stdlib) | `30/30` | **`30/30`** (`100.0%`) | `10/10` | `10/10` | `10/10` | `129.2` | `0` | `70.6` | **`0`** | — | `0.117` |
| `cpsat` (`5s`, `4` workers) | `30/30` | **`30/30`** (`100.0%`) | `10/10` (`10 OPT`) | `10/10` (`5 OPT`) | `10/10` (`10 FEAS`) | **`116.7`** | `0` | `68.9` | **`0`** | `5.5%` | `78.868` |

**Honest Reproducibility Notes**:
- Under default pure-makespan minimization (`tie_break_flow=False`) with 4 parallel workers, multiple schedules can share the optimal makespan while differing in `mean_flow_time` (e.g., on `small_00`, optimal makespan `105` min yielded `82.2` min in the 2026-11-18 `bench` run vs `70.0` min in `gantt`). Enabling `tie_break_flow=True, num_workers=1, seed=0` deterministically finds the unique optimal flow-minimizing schedule (`makespan = 105`, `mean_flow_time = 66.0` on `small_00`).
- Unsolved (`INFEASIBLE` / `UNKNOWN`) rows report `makespan = None` and `mean_flow_time = None` (`"-"` in tables) and are excluded from summary averages so solver failures never bias suite means downward.

---

## 5. History

| Commit / Branch | Date | Summary of Technical Changes |
|---|---|---|
| `124a6d7` (`main`) | 2026-11-18 | Initial release (`0.1.0`): immutable models (`models.py`), 6-template seeded generator (`generator.py`), 30-instance committed benchmark suite (`benchmarks/`), sweep-line feasibility checker (`checker.py`), greedy list scheduler (`greedy.py`), OR-Tools CP-SAT solver (`cpsat.py`), metrics (`metrics.py`), ASCII Gantt renderer (`gantt.py`), `labshift` CLI (`cli.py`), and 4 test modules. |
| `58ec7f2` (`origin/review-fixes`) | 2026-11-18 | Set `labshift generate --suite-dir` default `--seed` to `1000` (matching committed `benchmarks/` seeds `1000..1209`) and documented `mean_flow_time` non-uniqueness under pure makespan minimization. |
| `fix/resolve-audit-issues` | 2026-11-22 | Raised `ortools>=9.8` with PEP 562 lazy loading; added strict dataclass domain validation, separated checker violation kinds (`wrong_instrument`, `negative_start`, `instance_mismatch`, `transfer_time`), `O(K log K)` event sweep in `greedy`, pure-stdlib `greedy_repair` (`30/30` feasible), CP-SAT chain bound propagation + warm-starting + `--tie-break-flow` + `--soft-deadlines` + `UNKNOWN` bound preservation, `None` metrics for unsolved schedules, `labshift solve` CLI + flag validators + natural-sorted non-recursive instance discovery, `hard` preset + `ensure_feasible`, `robot_arm` `transfer_time` modeling, `twinstep` protocol adapters (`protocol.py`), and expanded Windows/Ubuntu Python 3.11–3.13 + `--no-deps` CI. |

---

## 6. Unique Properties

### Key Invariants & Design Guarantees
1. **Departure-First Half-Open Interval Semantics**: `check_schedule`, `solve_greedy`, `solve_greedy_repair`, and `solve_cpsat` enforce identical half-open `[start, end)` station and `[start - transfer_time, start)` `robot_arm` interval semantics, guaranteeing zero false capacity violations on back-to-back handoffs.
2. **Explicit Perishability (`max_wait`) Stress-Testing & Stdlib Repair**: Coupled post-incubation/post-wash/post-centrifuge `max_wait` windows expose myopic scheduling failures (`6/10` medium and `8/10` hard instances fail under `greedy`), while coupled right-shift `greedy_repair` restores `30/30` feasibility in pure Python without C++ solver dependencies.
3. **Un-clamped Dual Lower-Bound Certification**: Every solved hard-deadline CP-SAT schedule carries a sound makespan `best_bound <= makespan` (unscaled cleanly even when `tie_break_flow=True` and preserved on `UNKNOWN` timeouts), and `compute_metrics` reports exact un-clamped `gap_pct` so invalid dual bounds are immediately caught.

### Known Limits
- **Time-Bounded Multi-Worker Non-Determinism**: Multi-worker CP-SAT (`num_workers > 1`) under wall-clock limits remains subject to OS thread scheduling; pass `num_workers=1, seed=<int>, tie_break_flow=True` for deterministic runs.
- **Station-Level Granularity**: `protocol.py` estimates integer-minute operation durations from step volumes/cycles/times; sub-second pipette gantry kinematics and deck layout geometry belong to downstream `stepbot` and `lab-rehearsal` layers.

### Ecosystem Interfaces
- **Upstream (`L3` & `L5`)**: Imports validated [`twinstep`](https://github.com/yaniguan/twinstep) protocols via `job_from_protocol` / `instance_from_protocols`, serves as the exact CP-SAT scheduling escalation target referenced by [`labfill`](https://github.com/yaniguan/labfill) Recipe 3, and coordinates instrument time-slotting after [`lab-decide`](https://github.com/yaniguan/lab-decide) experiment selection, [`lab-automation-atlas`](https://github.com/yaniguan/lab-automation-atlas) platform grounding, and [`buy-or-make`](https://github.com/yaniguan/buy-or-make) sourcing.
- **Downstream (`L2` / `L4` / Body)**: Exports chronological station and `robot_arm` events via `export_workcell_queue` to [`lab-rehearsal`](https://github.com/yaniguan/lab-rehearsal) deck dry-runs, [`lab-verify`](https://github.com/yaniguan/lab-verify) / [`YLM`](https://github.com/yaniguan/YLM) LabLean protocol gates, [`stepbot`](https://github.com/yaniguan/stepbot) Opentrons v2 execution, and post-execution causal attribution via [`lab-attrib`](https://github.com/yaniguan/lab-attrib).

### 15-Repository Auto-Lab Wire Contract (`autolab-wire/v1`)
`autolab_wire.py` (`REPO_ID = "labshift"`, `LAYER = "L5"`) wires `labshift` directly into the 15-repository [`auto-lab`](https://github.com/yaniguan/auto-lab) packet bus (`lab-ears -> lab-ask-why -> lab-decide -> lab-automation-atlas -> buy-or-make -> labshift -> lab-rehearsal -> lab-verify -> stepbot -> lab-hands -> lab-feet -> lab-attrib -> twinstep -> labfill -> labbench-posttrain`):
- **Upstream Inputs**: Deep-copies incoming `autolab-wire/v1` packets from [`lab-automation-atlas`](https://github.com/yaniguan/lab-automation-atlas) and [`buy-or-make`](https://github.com/yaniguan/buy-or-make), returning halted packets unchanged and extracting `duration_min` from `parsed_spans` alongside `max_wait_min`.
- **Contended Scheduling, Rolling-Horizon Orthogonal Insertion & Independent Audit**: Builds a concrete `Instance` (`liquid_handler -> incubator (duration_min) -> plate_reader` with `min_wait=0, max_wait=max_wait_min` plus contended background load, or mid-campaign rolling-horizon rescheduling when `lab-attrib` triggers `RUN_ORTHOGONAL_ASSAY` for `Raman` / `LC-MS` at `current_time_min`), solves deterministically with `solve_cpsat` (or stdlib rolling/repair fallback when `ortools` is absent), and audits the schedule via `check_schedule` and `compute_metrics` (including `force_window_violation_schedule` fault-injection checks).
- **Downstream Outputs & Lean Invariant**: Emits `out["schedule"] = {"feasible", "makespan_min", "window_violations", "operations"}` for downstream [`lab-rehearsal`](https://github.com/yaniguan/lab-rehearsal) (and downstream bench/post-run stages including [`lab-attrib`](https://github.com/yaniguan/lab-attrib)), classifies halts into `L5_SCHEDULE_WINDOW_BLOCK`, `L5_SCHEDULE_DEADLINE_BLOCK`, and `L5_SCHEDULE_CAPACITY_BLOCK`, and appends `trace` (`PASS` / `BLOCK`) plus `bottom_reasoning_traces` (`invariant="missed_incubation_window_rejected"`, backed by `lean/LabShiftLean.lean`).
