# `labshift` — Architecture, Benchmarks, and Engineering History

> **Auto-Lab Ecosystem Role:** Layer `L5 · Executive Planning, Procurement & Scheduling` in [Robot AI Auto-Lab (`auto-lab`)](https://github.com/yaniguan/auto-lab) ([Project Studio](https://yaniguan.github.io/auto-lab/)).

---

## 1. Object

### Problem Statement
Multi-assay self-driving laboratories must multiplex concurrent liquid-handling workflows across shared, finite-capacity instrument pools (`liquid_handler`, `incubator`, `plate_reader`, `centrifuge`, `washer`). Unlike classical unconstrained job-shop problems, wet-lab chains impose **coupled inter-step wait windows**: both minimum equilibration delays (`min_wait`) and upper perishability bounds (`max_wait`, e.g., reading or spinning a plate before thermal drift, evaporation, or signal decay), alongside job release times and hard completion deadlines. Myopic dispatchers frequently stall a job in front of a saturated reader/washer past its `max_wait` window.

`labshift` provides a reproducible synthetic lab scheduling benchmark, an independent sweep-line feasibility checker, a greedy priority-dispatch baseline, an OR-Tools CP-SAT interval/cumulative solver, optimality-gap metrics, and an ASCII Gantt visualizer.

### Role in the Auto-Lab `L0`–`L6` Hierarchy
Within the [Auto-Lab](https://github.com/yaniguan/auto-lab) Brain/Body architecture:
- **Layer (`L5`)**: Acts as the executive multi-instrument scheduling and resource-allocation engine alongside experimental decision-making ([`lab-decide`](https://github.com/yaniguan/lab-decide)) and procurement planning ([`buy-or-make`](https://github.com/yaniguan/buy-or-make)).
- **Escalation from `L3` Tensor Heuristics**: Whereas [`labfill`](https://github.com/yaniguan/labfill) Recipe 3 provides fast tensor-batched parallel simulated annealing for unconstrained single-step assignment, `labshift` handles chained multi-operation jobs with `min_wait`/`max_wait` windows, release times, deadlines, and dual optimality bounds via OR-Tools CP-SAT.

### I/O Contract & Scope Boundaries

| Command / Module | Inputs | Outputs |
|---|---|---|
| `labshift generate` (`generator.py`) | `--preset` (`small\|medium\|large`), `--seed`, `--out` or `--suite-dir` | `Instance` JSON (`name`, `preset`, `seed`, `capacities`, `jobs`) |
| `labshift bench` (`bench.py`, `greedy.py`, `cpsat.py`) | Instance file/directory, `--solver` (`greedy\|cpsat`), `--time-limit` | `Schedule` objects + formatted `BenchRow` ASCII summary table |
| `labshift check` (`checker.py`, `metrics.py`) | `Instance` JSON + `Schedule` JSON | `CheckResult` (`feasible`, typed `Violation` tuple), `Metrics`, exit code `0`/`1` |
| `labshift gantt` (`gantt.py`) | `Instance` JSON (+ optional `--schedule` or `--solver`, `--width`) | Job-oriented ASCII Gantt timeline (`[L]`, `[I]`, `[R]`, `[C]`, `[W]`) |

**Scope Boundaries:**
- Models discrete integer-minute station durations and pool capacities; does not model 2D deck coordinates, robotic arm transit times between instruments, or tip/reagent inventory depletion.
- `solve_cpsat` enforces job deadlines as **hard constraints** (`INFEASIBLE` when violated) and minimizes `makespan` only.

---

## 2. Data

### Instance & Schedule Schema (`src/labshift/models.py`)
All domain objects are immutable (`frozen=True`) dataclasses with deterministic JSON round-trip serialization:
- **`Operation`**: `name: str`, `instrument: str`, `duration: int` (minutes), `min_wait: int = 0`, `max_wait: int | None = None`.
- **`Job`**: `job_id: str`, `ops: tuple[Operation, ...]`, `release: int = 0`, `deadline: int | None = None`.
- **`Instance`**: `name: str`, `capacities: dict[str, int]` over `INSTRUMENTS` (`liquid_handler`, `incubator`, `plate_reader`, `centrifuge`, `washer`), `jobs: tuple[Job, ...]`, `seed: int | None`, `preset: str | None`.
- **`ScheduledOp` & `Schedule`**: `(job_id, op_index, name, instrument, start, end)` tuples plus solver `status`, `solve_time_s`, and optional CP-SAT dual `best_bound`.

### Seeded Workflow Templates & Presets (`src/labshift/generator.py`)
`generate_instance` samples from 6 canonical 3-to-6-step liquid-handling workflow templates (`plate_prep`, `dilution_series`, `buffer_add`, `dye_dispense`, `mix_wash`, `plate_wash`, `spin_down`, `incubate`, `equilibrate`, `plate_read`). Non-initial operations draw `min_wait in {0, 0, 1, 2, 3}` min; transitions following `incubator`/`centrifuge` or entering `plate_read` receive a tight `max_wait = min_wait + Uniform(max_wait_range)` with probability `max_wait_prob`. Job `0` starts at `release = 0`, while jobs `1..J-1` draw `release in [0, release_max]`.

| Preset | Jobs (`J`) | Ops / Job | `release_max` | Capacities `(L, I, R, C, W)` | `max_wait_prob` (`range`) | `deadline_prob` (`slack`) | Suite Seeds (`benchmarks/`) |
|---|---:|---|---:|---|---|---:|---|
| `small` | `6` | `3..4` | `20` m | `(2, 2, 1, 1, 1)` | `0.0` (`15..30` m) | `0.50` (`2.5x + 18`) | `1000..1009` (`small_00..09.json`) |
| `medium` | `12` | `4..5` | `40` m | `(2, 3, 1, 2, 2)` | `0.5` (`6..16` m) | `0.65` (`2.2x + 36`) | `1100..1109` (`medium_00..09.json`) |
| `large` | `20` | `4..6` | `60` m | `(3, 4, 2, 2, 2)` | `0.6` (`6..16` m) | `0.65` (`2.4x + 60`) | `1200..1209` (`large_00..09.json`) |

The committed 30-instance benchmark suite in `benchmarks/` (MIT licensed) is exact and bit-for-bit reproducible via `generate_benchmark_suite("benchmarks", count_per_preset=10, base_seed=1000)`.

---

## 3. Model

### 3.1 Independent Sweep-Line Feasibility Checker (`src/labshift/checker.py`)
`check_schedule(instance, schedule)` audits any candidate schedule across three stages without relying on solver internals:
1. **Completeness & Duration Integrity (`_check_completeness_and_durations`)**: Verifies a 1-to-1 bijection over expected `(job_id, op_index)` pairs (`missing_op`, `extra_op` on unknown or duplicate keys), checks `instrument in instance.capacities` (`unknown_instrument`), verifies `sop.instrument == expected_instrument` and `sop.start >= 0` with `sop.end - sop.start == expected_duration` (`duration`).
2. **Intra-Job Temporal Constraints (`_check_job_constraints`)**: Checks `first.start >= job.release` (`release`); for each consecutive pair `(idx - 1, idx)`, computes `wait = curr.start - prev.end` and checks `wait >= 0` (`precedence`), `wait >= min_wait` (`min_wait`), and `wait <= max_wait` when set (`max_wait`); checks `last.end <= job.deadline` (`deadline`).
3. **Interval Event Sweep-Line (`_check_capacities`)**: For each instrument pool, emits `(start, +1)` and `(end, -1)` events for positive-duration intervals `[start, end)`, sorting `(timestamp, delta)` so **departures `(t, -1)` are processed before arrivals `(t, +1)`** at identical timestamps (allowing zero-idle back-to-back handoffs on capacity-1 instruments) and flagging each overload excursion `active > cap` (`capacity`).

### 3.2 Priority List-Scheduling Baseline (`src/labshift/greedy.py`)
`solve_greedy` maintains each job's next unscheduled operation index and `ready_time` (initialized to `job.release`, updated to `end + next_op.min_wait`). At each step it dispatches the job minimizing `(ready_time, deadline_or_inf, op.duration, job_id)` onto `_earliest_slot(intervals, cap, ready_time, duration)`, which checks candidate timestamps `{ready_time} U {end > ready_time}` for continuous capacity availability across `[t, t + duration)`. Because `solve_greedy` performs no lookahead or backtracking, contention at downstream single-capacity bottlenecks (such as `plate_reader` on `medium_*`) can delay an operation past `max_wait`.

### 3.3 OR-Tools CP-SAT Interval & Cumulative Model (`src/labshift/cpsat.py`)
`solve_cpsat(instance, time_limit=5.0, num_workers=4)` formulates exact constrained makespan minimization over integer horizon `H = max(max_release + sum(duration + min_wait), max_deadline)`:
- **Variables**: Per operation `(j, k)`, integer start `s_{j,k} in [lb, H]`, end `e_{j,k} in [lb + d_{j,k}, H]`, and fixed-duration `IntervalVar` `iv_{j,k}` (`lb = release_j` for `k = 0`, else `0`).
- **Temporal Coupling**: Linear inequalities `s_{j,k} >= e_{j,k-1} + min_wait_{j,k}`, optional `s_{j,k} <= e_{j,k-1} + max_wait_{j,k}`, and hard deadline `e_{j,last} <= deadline_j`.
- **Resource Constraints**: Disjunctive `model.add_no_overlap(intervals)` when `cap == 1` and `model.add_cumulative(intervals, [1, ..., 1], cap)` when `cap > 1` (immediately returning `INFEASIBLE` if any needed instrument has `cap <= 0`).
- **Objective & Optimality Certificate**: Minimizes `makespan = max_j e_{j,last}` via `model.add_max_equality` and exports `solver.best_objective_bound` to compute `gap_pct = max(0.0, 100.0 * (makespan - best_bound) / makespan)`.

---

## 4. Training

### Combinatorial Benchmark & RLVR Reward Oracle Role
`labshift` trains **no neural parameters** internally and requires no GPU. Within Auto-Lab's learning and planning stack, it provides:
1. **Verifiable Scheduling Environment (RLVR)**: `check_schedule` and `compute_metrics` act as a deterministic, millisecond-latency verifier and dense/sparse reward oracle (`feasible`, `makespan`, `window_violations`, `total_tardiness`, and `gap_pct` relative to CP-SAT dual bounds) for evaluating LLM planners, neural dispatch heuristics, or code-synthesis agents.
2. **Ground-Truth Solver Supervision**: `solve_cpsat` generates optimal or near-optimal feasible trajectories (`30/30` on the benchmark suite) for imitation learning or preference pair construction against infeasible `greedy` rollouts.

### Measured CPU Benchmark Summary (30-Instance Suite, 2026-11-18)
All solver benchmarks run on CPU (`--time-limit 5` s per instance for `cpsat`). Local verification on 2026-11-22 confirmed all 30 `benchmarks/*.json` files match `generate_instance` seeds bit-for-bit and reproduced the exact `greedy` summary (`24/30` feasible, `129.8` mean makespan, `13` window violations). *(Note: `ortools` is required for `cpsat` and top-level `import labshift`; no GPU benchmarks apply.)*

| Solver | Feasible | `small` Feas | `medium` Feas | `large` Feas | Mean Makespan (m) | Total Tardiness | Mean Flow (m) | Window Viols | Mean `gap%` | Total Solve Time (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `greedy` | **`24/30`** (`80.0%`) | `10/10` | `4/10` | `10/10` | `129.8` | `0` | `72.3` | `13` | — | `0.012` |
| `cpsat` (`5s`, `4` workers) | **`30/30`** (`100.0%`) | `10/10` (`10 OPT`) | `10/10` (`5 OPT`) | `10/10` (`10 FEAS`) | **`116.7`** | `0` | `68.9` | **`0`** | `5.5%` | `78.868` |

**Honest Reproducibility Notes** (highlighted in `README.md` and open PR `origin/review-fixes` `58ec7f2`):
- Multi-worker CP-SAT (`num_workers = 4`) under a wall-clock time limit exhibits slight run-to-run thread non-determinism on secondary metrics (e.g., suite mean flow `68.9` vs `68.8` min across runs).
- Because `solve_cpsat` minimizes `makespan` alone, multiple schedules can share the exact optimal makespan while differing in `mean_flow_time` (e.g., on `small_00`, optimal makespan `105` min yields `mean_flow = 82.2` min in the `bench` run vs `70.0` min in the `gantt` run).

---

## 5. History

| Commit | Branch / Ref | Date | Summary of Technical Changes |
|---|---|---|---|
| `124a6d7` | `main` (`origin/main`) | 2026-11-18 | Initial release (`0.1.0`): immutable models (`models.py`), 6-template seeded generator (`generator.py`), 30-instance committed benchmark suite (`benchmarks/`), sweep-line feasibility checker (`checker.py`), greedy list scheduler (`greedy.py`), OR-Tools CP-SAT solver (`cpsat.py`), metrics (`metrics.py`), ASCII Gantt renderer (`gantt.py`), `labshift` CLI (`cli.py`), 4 test modules, and Python 3.11/3.12 CI. |
| `58ec7f2` | `origin/review-fixes` (open PR) | 2026-11-18 | Updated `labshift generate` CLI so `--seed` defaults to `1000` when `--suite-dir` is set (matching committed `benchmarks/` seeds `1000..1009, 1100..1109, 1200..1209`, while keeping `0` for single instances), and documented non-uniqueness of `mean_flow_time` under pure makespan minimization (`small_00`: `82.2` vs `70.0`). |

---

## 6. Unique Properties

### Key Invariants & Design Guarantees
1. **Departure-First Half-Open Interval Semantics**: Both `check_schedule` (`(t, -1)` sorted before `(t, +1)`) and `solve_cpsat` (`IntervalVar` over `[start, end)`) enforce identical half-open interval semantics, guaranteeing zero false capacity violations on back-to-back handoffs.
2. **Explicit Perishability (`max_wait`) Stress-Testing**: Medium and large presets couple capacity-1/capacity-2 bottlenecks (`plate_reader`, `washer`) with bounded post-incubation/post-centrifuge `max_wait` windows, exposing myopic scheduling failures (`6/10` medium instances fail under `greedy`, `0/10` under `cpsat`).
3. **Dual Lower-Bound Certification**: Every feasible CP-SAT schedule carries `best_bound` and `gap_pct`, distinguishing proven global optima (`15/30` instances at `0.0%` gap) from time-bounded feasible solutions (`15/30`).

### Known Limits
- **Hard Deadlines Only in CP-SAT**: Instances with impossible deadlines return `status = "INFEASIBLE"` with empty `ops = ()` rather than optimizing soft tardiness.
- **Single-Objective Ties**: Pure makespan minimization leaves idle-time placement within non-critical chains unregularized unless a secondary flow-time tie-breaker is added.
- **Eager `ortools` Package Import on `main`**: `labshift/__init__.py` eagerly imports `solve_cpsat` (`ortools`), requiring `ortools` even when invoking stdlib-only components (`models`, `checker`, `greedy`, `generator`).

### Ecosystem Interfaces
- **Upstream (`L3` & `L5`)**: Serves as the exact CP-SAT scheduling escalation target referenced by [`labfill`](https://github.com/yaniguan/labfill) Recipe 3 and coordinates instrument time-slotting after [`lab-decide`](https://github.com/yaniguan/lab-decide) experiment selection and [`buy-or-make`](https://github.com/yaniguan/buy-or-make) sourcing.
- **Downstream (`L2` / `L4` / Body)**: Scheduled liquid-handling operations hand off to [`lab-rehearsal`](https://github.com/yaniguan/lab-rehearsal) deck dry-runs, [`lab-verify`](https://github.com/yaniguan/lab-verify) / [`YLM`](https://github.com/yaniguan/YLM) LabLean protocol gates, and [`stepbot`](https://github.com/yaniguan/stepbot) Opentrons v2 execution.
