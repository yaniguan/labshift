# `labshift` — Architecture, Benchmarks, and Engineering History

> **Auto-Lab Ecosystem Role:** Layer `L5 · Executive Planning, Procurement & Scheduling` in [Robot AI Auto-Lab (`auto-lab`)](https://github.com/yaniguan/auto-lab) ([Project Studio](https://yaniguan.github.io/auto-lab/)).

---

## 1. Object

### Problem Statement
Multi-assay self-driving laboratories multiplex concurrent liquid-handling workflows across shared, finite-capacity instrument pools (`liquid_handler`, `incubator`, `plate_reader`, `centrifuge`, `washer`, orthogonal `raman_spectrometer` / `lc_ms`, and optional `robot_arm`). Unlike classical unconstrained job-shop problems, wet-lab chains impose **coupled inter-step wait windows**: minimum equilibration delays (`min_wait`), blocking robotic plate-handoff transit delays (`transfer_time` / `transit_matrix`), upper perishability bounds (`max_wait`), sequence-dependent decontamination/thermal changeovers (`setup_times`), job release times, and hard or soft completion deadlines.

`labshift` provides a zero-dependency core benchmark, an independent sweep-line feasibility checker, stdlib combinatorial lower bounds (`makespan_lower_bound`), three pure-stdlib schedulers (`greedy`, `greedy_repair`, and Iterated Greedy / LNS `lns`), a warm-started OR-Tools CP-SAT optimizer (`cpsat`), an optional `PyJobShop` (`>= 0.0.9`) adapter (`pyjobshop`), protocol and open-benchmark importers, and a Lean 4.36.0 formalization (`lean/LabShiftLean.lean`).

### Role in the Auto-Lab `L0`–`L6` Hierarchy
- **Layer (`L5`)**: Executive multi-instrument scheduling and resource-allocation engine alongside [`lab-decide`](https://github.com/yaniguan/lab-decide) and [`buy-or-make`](https://github.com/yaniguan/buy-or-make).
- **Bridge from `L3` Protocol Graphs**: Imports concrete step-level protocols from [`twinstep`](https://github.com/yaniguan/twinstep) and [`stepbot`](https://github.com/yaniguan/stepbot) via `job_from_protocol` / `job_from_stepbot_spec` and exports chronological station + `robot_arm` dispatch queues via `export_workcell_queue`.
- **Escalation from `L3` Tensor Heuristics**: Complements [`labfill`](https://github.com/yaniguan/labfill) Recipe 3 by solving chained multi-operation jobs with `min_wait`/`max_wait`/`transfer_time` windows, release times, deadlines, and valid lower bounds.

### I/O Contract & Scope Boundaries

| Command / Module | Inputs | Outputs |
|---|---|---|
| `labshift generate` (`generator.py`) | `--preset` (`small\|medium\|large\|hard`), `--seed`, `--ensure-feasible`, `--planted`, `--witness-out` / `--witness-dir`, `--out` / `--suite-dir` | Validated `Instance` JSON + optional planted witness `Schedule` JSON |
| `labshift solve` (`cli.py`, `bench.py`) | `Instance` JSON, `--solver` (`greedy\|greedy_repair\|lns\|cpsat\|pyjobshop`), `--time-limit`, `--workers`, `--seed`, `--tie-break-flow`, `--soft-deadlines`, `--out` | `Schedule` JSON (`instance_name`, `solver`, `status`, `solve_time_s`, `best_bound`, `ops`) |
| `labshift bench` (`bench.py`) | Instance file/directory, `--solver`, optional `--compare`, solver flags, `--out-dir`, `--json` | Feasible-only `BenchRow` ASCII table (`lb`, `bound`, `gap%`) + paired solver summary |
| `labshift check` (`checker.py`, `metrics.py`) | `Instance` JSON + `Schedule` JSON | `CheckResult` (`feasible`, typed `Violation` tuple), `Metrics`, exit code `0`/`1` |
| `labshift gantt` (`gantt.py`) | `Instance` JSON (+ optional `--schedule`, `--save-schedule`, `--solver`, `--width >= 10`) | Job-oriented ASCII Gantt timeline |
| `labshift.protocol` (`protocol.py`) | `twinstep` / `stepbot` protocol dicts, single-key step lists, or JSON paths | `Job`, multi-job `Instance`, and `export_workcell_queue` dispatch list |
| `labshift.open_benchmarks` (`open_benchmarks.py`) | OR-Library/Taillard JSSP, PSPLIB `.sm`/`.dat`, Opentrons Python API v2, WLP BRAT, BioPlanner JSON | Validated `Instance` / `Job` objects |

---

## 2. Data

### Validated Instance & Schedule Schema (`src/labshift/models.py`)
All domain objects are immutable (`frozen=True`) dataclasses enforcing strict type/domain invariants:
- **`Operation`**: `name: str`, `instrument: str`, `duration: int >= 1`, `min_wait: int >= 0`, `max_wait: int | None` (`>= max(min_wait, transfer_time)`), `transfer_time: int >= 0`, optional `setup_tag: str | None`.
- **`Job`**: `job_id: str`, `ops: tuple[Operation, ...]`, `release: int >= 0`, `deadline: int | None` (`>= release`), optional `assay_tag: str | None`.
- **`Instance`**: `name: str`, `capacities: dict[str, int]`, unique-ID `jobs: tuple[Job, ...]`, `seed: int | None`, `preset: str | None`, optional `transit_matrix`, optional `setup_times`, `blocking_handoff: bool = True`.
- **`ScheduledOp` & `Schedule`**: `(job_id, op_index, name, instrument, start, end, unit_index)` tuples plus solver `status`, `solve_time_s >= 0`, and optional solver `best_bound`.

### Synthetic v1 Presets & Planted Perishability Tier (`src/labshift/generator.py`)
- **v1 Suite (`benchmarks/*.json`, 30 instances)**: `small_00..09`, `medium_00..09`, `large_00..09` (seeds `1000..1209`).
- **Planted Hard Suite (`benchmarks/planted/*.json`, 20 instances + `benchmarks/planted_witnesses/*.schedule.json`)**: `generate_planted_instance` packs a valid multi-instrument schedule first in a seeded random job permutation, then derives `max_wait = planted_wait + U{0..2}` on `70%` of transitions and `deadline = planted_end + U{0..5}`. Every instance is 100% feasible by witness construction (`plantedWaitLink_ok` in `lean/LabShiftLean.lean`), while myopic `greedy` achieves `0/20` and single-pass `greedy_repair` achieves `7/20`.

---

## 3. Model

### 3.1 Independent Sweep-Line Feasibility Checker (`src/labshift/checker.py`)
`check_schedule(instance, schedule)` verifies `instance_name`, completeness, routing, positive durations, release times, inter-step `[max(min_wait, transfer_time), max_wait]` windows, deadlines, source-station blocking until `robot_arm` pickup, sequence-dependent `setup_times`, and departure-first `(t, -1)` before `(t, +1)` pool capacities.

### 3.2 Stdlib Combinatorial Lower Bounds (`src/labshift/bounds.py`)
`makespan_lower_bound(instance) -> int` computes the maximum across:
1. **Longest job chain**: `release + sum(durations) + sum(max(min_wait, effective_transfer_time))`.
2. **Per-instrument Jackson/Carlier energetic bounds**: Evaluates `min_{o in S} head_o + ceil(sum_{o in S} dur_o / capacity) + min_{o in S} tail_o` across all canonical `(r*, q*)` head-tail subsets in `O(|O_m|^2)` time (returning `122` on `medium_00` and `134` on `large_05`, exceeding CP-SAT's 60 s LP/branch bound of `113`), plus unconditional full-pool minimum changeover slack under `setup_times`.
3. **`robot_arm` transfer pool energetic bound** over `[head_k - eff_t, head_k)` transit items when `robot_arm` is modeled.

`compute_metrics` combines `best_bound = max(schedule.best_bound, makespan_lower_bound(instance))` while preserving `solver_bound = schedule.best_bound` and `lower_bound = makespan_lower_bound(instance)` separately.

### 3.3 Solvers (`greedy`, `greedy_repair`, `lns`, `cpsat`, `pyjobshop`)
- **`solve_greedy` & `solve_greedy_repair` (`greedy.py`)**: `O(K log K)` event-profile list scheduler and recursive intra-chain right-shift coupled-segment heuristic.
- **`solve_lns` (`lns.py`)**: Pure-stdlib Ruiz-Stuetzle Iterated Greedy / Large Neighborhood Search combining violation-directed Destroy-and-Best-Insertion over job permutations with critical-path biased segment priorities and early optimal stopping at `makespan == makespan_lower_bound(instance)`.
- **`solve_cpsat` (`cpsat.py`)**: Warm-started OR-Tools CP-SAT `IntervalVar` + `no_overlap`/`cumulative` + per-unit `add_circuit` solver tightened with `makespan >= makespan_lower_bound(instance)`.
- **`solve_pyjobshop` (`pyjobshop_bridge.py`)**: Optional `PyJobShop >= 0.0.9` adapter for head-to-head benchmarking on standard time-window instances and quantifying `labshift`'s lab-specific extensions.

---

## 4. Training & Measured Benchmarks

`labshift` trains **no neural parameters** internally; `check_schedule` and `compute_metrics` act as a millisecond-latency RLVR reward and verification oracle.

Measured on **2026-11-26** on **AMD Ryzen AI 7 PRO 350** (Python 3.11.15):

| Suite | `greedy` Feas (Makespan) | `greedy_repair` Feas (Makespan) | `lns` (`2s`, stdlib) Feas (Makespan / `gap%`) | `cpsat` (`5s`) Feas (Makespan / `gap%`) | `pyjobshop` (`5s`) Feas (Makespan / `gap%`) |
|---|---|---|---|---|---|
| **v1 Suite (`30` instances)** | `24/30` (`124.4` m) | `30/30` (`129.2` m) | **`30/30` (`122.4` m / `10.1%`)** | **`30/30` (`116.5` m / `4.0%`)** | `30/30` (`116.9` m / `4.3%`) |
| **Planted Suite (`20` instances)** | `0/20` (—) | `7/20` (`152.0` m) | **`17/20` (`153.7` m / `18.1%`)** | **`20/20` (`146.2` m / `0.0%`)** | `20/20` (`146.2` m / `0.0%`) |
| **OR-Lib / Taillard JSSP (`5`)** | `5/5` (`819.8`) | `5/5` (`819.8`) | `5/5` (`720.2` / `12.1%`) | **`5/5` (`649.4` / `1.7%`)** | `5/5` (`655.6` / `2.1%`) |
| **PSPLIB `j30`/`j60` (`6`)** | `6/6` (`51.5`) | `6/6` (`51.5`) | **`6/6` (`50.8` / `0.0%`)** | **`6/6` (`50.8` / `0.0%`)** | `6/6` (`50.8` / `0.0%`) |
| **Wet-Lab Campaigns (`3`)** | `1/3` (`357.0`) | `3/3` (`349.0`) | **`3/3` (`349.0` / `0.0%`)** | **`3/3` (`349.0` / `0.0%`)** | `1/3` (`352.0`) |

---

## 5. History

| Commit / Branch | Date | Summary of Technical Changes |
|---|---|---|
| `124a6d7` (`main`) | 2026-11-18 | Initial release (`0.1.0`): frozen models, 6-template generator, 30-instance v1 suite, sweep-line checker, greedy baseline, CP-SAT solver, metrics, Gantt renderer, CLI, and 4 test modules. |
| `58ec7f2` (`origin/review-fixes`) | 2026-11-18 | Set `labshift generate --suite-dir` default `--seed` to `1000` and documented `mean_flow_time` non-uniqueness under pure makespan minimization. |
| `6985986` (`main`) | 2026-11-25 | Added `greedy_repair`, blocking `robot_arm` handoffs, sequence-dependent `setup_times`, `autolab-wire/v1` rolling orthogonal assays (`Raman` / `LC-MS`), community extensions, and Lean 4 sweep-line proofs. |
| `feat/frontier-bounds-planted-lns-and-benchmarks` | 2026-11-26 | Resolved `#35` (explicit case-insensitive time-unit table, volume-unit rejection), `#36` (`bounds.py` Jackson/Carlier energetic lower bounds, `lb` + `gap%` for all solvers, CP-SAT bound tightening), `#37` (`--planted` tier, 20 committed planted instances + witnesses, feasible-only summary means & paired comparisons), `#38` (native `stepbot` protocol import & 3 stepbot fixtures), plus zero-dependency core (`dependencies = []`), `solve_lns`, `PyJobShop` head-to-head & extension quantifier, Lean 4 energetic/planted theorems, and JSSP/PSPLIB/Opentrons/WLP/BioPlanner open benchmarks. |

---

## 6. Unique Properties

### Key Invariants & Design Guarantees
1. **Departure-First Half-Open Interval Semantics**: `check_schedule`, `solve_greedy`, `solve_greedy_repair`, `solve_lns`, and `solve_cpsat` enforce identical `[start, end)` station and `[start - transfer_time, start)` `robot_arm` semantics (`sweepLinePoolOK_iff_activeCount_le` in `lean/LabShiftLean.lean`).
2. **Planted Perishability Stress-Testing & Stdlib Metaheuristic Repair**: Witness-constructed `--planted` instances guarantee 100% solvability (`plantedWaitLink_ok`) while separating myopic `greedy` (`0/20`), single-pass `greedy_repair` (`7/20`), stdlib `solve_lns` (`17/20`), and exact `solve_cpsat` (`20/20 OPTIMAL`).
3. **Valid Lower-Bound Certification**: Every schedule is evaluated against `best_bound = max(solver_bound, makespan_lower_bound(instance))` (`energeticPoolBound_le_makespan` in `lean/LabShiftLean.lean`), reporting an un-clamped `gap_pct` even in stdlib-only environments without C++ solvers.

### 15-Repository Auto-Lab Wire Contract (`autolab-wire/v1`)
`autolab_wire.py` (`REPO_ID = "labshift"`, `LAYER = "L5"`) wires `labshift` into the 15-repository [`auto-lab`](https://github.com/yaniguan/auto-lab) packet bus, handling contended window scheduling and mid-campaign `RUN_ORTHOGONAL_ASSAY` rolling-horizon rescheduling with independent `check_schedule` verification and `missed_incubation_window_rejected` traces.
