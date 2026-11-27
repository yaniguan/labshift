# labshift

A synthetic lab scheduling and planning benchmark for multi-instrument liquid-handling workflows.

## What It Is

`labshift` models scheduling of synthetic and protocol-derived liquid-handling jobs (such as plate preparation, buffer addition, dilution series, washing, spinning, incubation, and plate reads) across shared instrument pools:

- `liquid_handler`
- `incubator`
- `plate_reader`
- `centrifuge`
- `washer`
- optional `robot_arm` (when inter-instrument `transfer_time > 0` is modeled)

Each job is a chain of operations with durations (in positive integer minutes), job release times, optional deadlines, optional `min_wait` / `max_wait` time windows, and optional `transfer_time` transit delays between consecutive operations. The package includes:

- Validated JSON-serializable dataclasses (`Instance`, `Job`, `Operation`, `Schedule`, `ScheduledOp`)
- A seeded synthetic instance generator (`small`, `medium`, `large`, and `hard` presets, plus `--ensure-feasible`) and a fixed 30-instance v1 benchmark suite in `benchmarks/`
- Protocol adapters (`job_from_protocol`, `instance_from_protocols`, `export_workcell_queue`) bridging `twinstep` / `stepbot` step lists into `labshift` instances and robot dispatch queues
- A schedule feasibility checker validating instance identity, completeness, instrument assignment, durations, precedence, time windows, transfer times, release times, deadlines, and exact sweep-line capacities
- Two zero-dependency stdlib heuristics (`greedy` and coupled right-shift `greedy_repair`) plus an lazily loaded OR-Tools CP-SAT solver (`cpsat`, supporting `--tie-break-flow`, `--soft-deadlines`, `--workers`, and `--seed`)
- Metrics (`makespan`, `total_tardiness`, `mean_flow_time`, `window_violations`, `solve_time_s`, and un-clamped dual `gap_pct`) and an ASCII Gantt chart renderer

## Install

Requires Python 3.11+.

```bash
pip install -e .[dev]
```

Pure-stdlib modules (`models`, `generator`, `checker`, `greedy`, `metrics`, `gantt`, `protocol`, `bench`) lazy-load `ortools>=9.8` and also run in lightweight `--no-deps` environments when `cpsat` is not invoked.

Part of **[Robot AI Auto-Lab (`auto-lab`)](https://github.com/yaniguan/auto-lab)** ([Project Studio](https://yaniguan.github.io/auto-lab/)) — **Layer `L5 · Executive Planning, Procurement & Scheduling`**.

## System Profile: Object · Data · Model · Training · History · Unique Properties

Full code-level architecture, CP-SAT formulation, benchmark analysis, and engineering history are documented in **[`docs/ARCHITECTURE_AND_HISTORY.md`](docs/ARCHITECTURE_AND_HISTORY.md)**:

- **Object**: Executive multi-instrument lab scheduling benchmark, sweep-line verifier, `O(K log K)` greedy and coupled right-shift heuristics, protocol bridge, and OR-Tools CP-SAT solver over shared `liquid_handler`, `incubator`, `plate_reader`, `centrifuge`, `washer`, and optional `robot_arm` pools with coupled `min_wait`/`max_wait` perishability windows, release times, and hard or soft deadlines.
- **Data**: Domain-validated frozen JSON-serializable models (`Instance`, `Job`, `Operation`, `Schedule`, `ScheduledOp`), 6 seeded assay workflow templates across `small`/`medium`/`large`/`hard` presets, `twinstep`/`stepbot` protocol importers, and a committed 30-instance MIT-licensed v1 benchmark suite (`small_00..09`, `medium_00..09`, `large_00..09`, seeds `1000..1209`).
- **Model**: Independent departure-first `(t, -1)` sweep-line feasibility checker (`labshift.checker`), `O(K log K)` priority list-scheduling baseline (`solve_greedy`), pure-stdlib coupled-segment right-shift heuristic (`solve_greedy_repair`), and warm-started OR-Tools CP-SAT `IntervalVar` + `no_overlap`/`cumulative` optimizer (`solve_cpsat`) with tight chain bounds, optional flow-time tie-breaking, soft-deadline tardiness mode, and dual `best_bound` gap certificates.
- **Training**: Verifiable combinatorial planning & RLVR reward environment (no internal neural weights; on the 30-instance CPU benchmark, `cpsat` achieves `30/30` feasibility and `116.7` min mean makespan, pure-stdlib `greedy_repair` achieves `30/30` feasibility and `129.2` min mean makespan in `0.12 s`, and myopic `greedy` achieves `24/30` feasibility, `129.8` min makespan, and `13` `max_wait` violations).
- **History**: Initial release `124a6d7` on `main` (`0.1.0`, 30 benchmark instances), review clarifications `58ec7f2` (`origin/review-fixes`, `--suite-dir` default `--seed 1000` and `mean_flow` qualification), and audit resolution adding lazy `ortools>=9.8` imports, domain validation, `greedy_repair`, `labshift solve`, CP-SAT warm-starting/flow tie-breaking/soft deadlines, `transfer_time` arm modeling, and `twinstep` protocol adapters.
- **Unique Properties**: Exact half-open interval sweep-line verification, perishability window stress-testing, proven dual optimality bounds, stdlib-only deployability, and clean interfaces with [`twinstep`](https://github.com/yaniguan/twinstep), [`labfill`](https://github.com/yaniguan/labfill), [`lab-decide`](https://github.com/yaniguan/lab-decide), [`buy-or-make`](https://github.com/yaniguan/buy-or-make), [`lab-rehearsal`](https://github.com/yaniguan/lab-rehearsal), [`lab-verify`](https://github.com/yaniguan/lab-verify), [`YLM`](https://github.com/yaniguan/YLM) LabLean, and [`stepbot`](https://github.com/yaniguan/stepbot).

## Repository Layout

```text
labshift/
├── autolab_wire.py              # autolab-wire/v1 Layer L5 scheduling & verification node
├── benchmarks/                  # 30 seeded v1 JSON instances (small_00..09, medium_00..09, large_00..09)
├── community_extensions/        # Rolling breakdown repair (Idea #07) & night-shift packer (Idea #08)
├── lean/                        # Lean 4 formal window, contingency & dual-regime theorems
├── src/labshift/
│   ├── __init__.py              # Public API exports with PEP 562 lazy solve_cpsat loading
│   ├── models.py                # Frozen validated dataclasses & JSON serialization
│   ├── generator.py             # 6 workflow templates & small/medium/large/hard presets
│   ├── protocol.py              # twinstep/stepbot protocol importers & workcell queue exporter
│   ├── checker.py               # Completeness, temporal window, arm, and sweep-line capacity checker
│   ├── greedy.py                # O(K log K) greedy baseline & coupled right-shift greedy_repair
│   ├── cpsat.py                 # Warm-started OR-Tools CP-SAT solver (hard/soft deadlines, tie-break)
│   ├── metrics.py               # Makespan, tardiness, mean flow, violations, and dual gap%
│   ├── gantt.py                 # Job-oriented ASCII Gantt chart renderer
│   ├── bench.py                 # Suite runner, JSON exporter, & ASCII summary table formatter
│   └── cli.py                   # `labshift solve | bench | gantt | generate | check` CLI
├── docs/
│   └── ARCHITECTURE_AND_HISTORY.md
└── tests/                       # Checker, model, solver, CLI/Gantt, protocol, extension & wire tests
```

## Quickstart

Run the benchmark suite with the myopic greedy baseline, the window-aware `greedy_repair` heuristic, or OR-Tools CP-SAT:

```bash
labshift bench --solver greedy benchmarks/
labshift bench --solver greedy_repair benchmarks/
labshift bench --solver cpsat --time-limit 5 benchmarks/
```

Solve an instance to a saved `Schedule` JSON, validate it with the independent checker, and render an ASCII Gantt chart:

```bash
labshift solve benchmarks/small_00.json --solver cpsat --workers 1 --seed 0 --tie-break-flow --out schedule.json
labshift check benchmarks/small_00.json schedule.json
labshift gantt --schedule schedule.json benchmarks/small_00.json
```

Generate a new synthetic instance (optionally verifying feasibility or using the congested `hard` preset):

```bash
labshift generate --preset hard --seed 42 --ensure-feasible --out hard_42.json
```

## Example Output

The `greedy` and `cpsat` tables below were measured on a CPU-only Linux box on 2026-11-18; the `greedy_repair` summary was measured on CPU on 2026-11-22 against the identical 30 committed `benchmarks/*.json` instances.

### Greedy Baseline (`labshift bench --solver greedy --time-limit 5 benchmarks/`)

```text
instance  | preset | status | feas | makespan | tardiness | mean_flow | win_viol | bound | gap% | time_s
----------+--------+--------+------+----------+-----------+-----------+----------+-------+------+-------
small_00  | small  | OK     | yes  |      122 |         0 |      78.7 |        0 |     - |    - |  0.000
small_01  | small  | OK     | yes  |       72 |         0 |      44.5 |        0 |     - |    - |  0.000
small_02  | small  | OK     | yes  |       69 |         0 |      47.3 |        0 |     - |    - |  0.000
small_03  | small  | OK     | yes  |      116 |         0 |      72.0 |        0 |     - |    - |  0.000
small_04  | small  | OK     | yes  |       78 |         0 |      58.2 |        0 |     - |    - |  0.000
small_05  | small  | OK     | yes  |       92 |         0 |      56.7 |        0 |     - |    - |  0.000
small_06  | small  | OK     | yes  |       70 |         0 |      49.7 |        0 |     - |    - |  0.000
small_07  | small  | OK     | yes  |       76 |         0 |      46.8 |        0 |     - |    - |  0.000
small_08  | small  | OK     | yes  |       90 |         0 |      55.7 |        0 |     - |    - |  0.000
small_09  | small  | OK     | yes  |       83 |         0 |      56.2 |        0 |     - |    - |  0.000
medium_00 | medium | OK     | yes  |      144 |         0 |      76.1 |        0 |     - |    - |  0.000
medium_01 | medium | OK     | no   |      143 |         0 |      75.2 |        1 |     - |    - |  0.000
medium_02 | medium | OK     | no   |      150 |         0 |      94.8 |        1 |     - |    - |  0.000
medium_03 | medium | OK     | no   |      155 |         0 |      99.9 |        3 |     - |    - |  0.000
medium_04 | medium | OK     | no   |      165 |         0 |     105.1 |        4 |     - |    - |  0.000
medium_05 | medium | OK     | no   |      130 |         0 |      64.6 |        1 |     - |    - |  0.000
medium_06 | medium | OK     | yes  |      148 |         0 |      84.7 |        0 |     - |    - |  0.000
medium_07 | medium | OK     | no   |      165 |         0 |     104.1 |        3 |     - |    - |  0.000
medium_08 | medium | OK     | yes  |      128 |         0 |      61.3 |        0 |     - |    - |  0.000
medium_09 | medium | OK     | yes  |      133 |         0 |      72.7 |        0 |     - |    - |  0.000
large_00  | large  | OK     | yes  |      152 |         0 |      71.7 |        0 |     - |    - |  0.001
large_01  | large  | OK     | yes  |      155 |         0 |      72.3 |        0 |     - |    - |  0.001
large_02  | large  | OK     | yes  |      155 |         0 |      84.6 |        0 |     - |    - |  0.001
large_03  | large  | OK     | yes  |      187 |         0 |      92.2 |        0 |     - |    - |  0.001
large_04  | large  | OK     | yes  |      148 |         0 |      75.3 |        0 |     - |    - |  0.001
large_05  | large  | OK     | yes  |      162 |         0 |      81.1 |        0 |     - |    - |  0.001
large_06  | large  | OK     | yes  |      151 |         0 |      67.9 |        0 |     - |    - |  0.001
large_07  | large  | OK     | yes  |      156 |         0 |      71.7 |        0 |     - |    - |  0.001
large_08  | large  | OK     | yes  |      151 |         0 |      81.8 |        0 |     - |    - |  0.001
large_09  | large  | OK     | yes  |      147 |         0 |      67.3 |        0 |     - |    - |  0.001
----------+--------+--------+------+----------+-----------+-----------+----------+-------+------+-------
ALL (30)  | -      | 30/30  | 24/30 |    129.8 |         0 |      72.3 |       13 |     - |    - |  0.012
```

### Window-Aware Coupled Heuristic (`labshift bench --solver greedy_repair benchmarks/`)

Measured on CPU (2026-11-22, Python 3.11, stdlib-only): `30/30` solved, `30/30` feasible (`0` window violations), `129.2` min mean makespan, `70.6` min mean flow time, `0.117 s` total across all 30 instances.

### OR-Tools CP-SAT (`labshift bench --solver cpsat --time-limit 5 benchmarks/`)

```text
instance  | preset | status   | feas | makespan | tardiness | mean_flow | win_viol | bound | gap% | time_s
----------+--------+----------+------+----------+-----------+-----------+----------+-------+------+-------
small_00  | small  | OPTIMAL  | yes  |      105 |         0 |      82.2 |        0 | 105.0 |  0.0 |  0.047
small_01  | small  | OPTIMAL  | yes  |       72 |         0 |      44.0 |        0 |  72.0 |  0.0 |  0.008
small_02  | small  | OPTIMAL  | yes  |       69 |         0 |      47.7 |        0 |  69.0 |  0.0 |  0.010
small_03  | small  | OPTIMAL  | yes  |      105 |         0 |      66.7 |        0 | 105.0 |  0.0 |  0.013
small_04  | small  | OPTIMAL  | yes  |       73 |         0 |      53.3 |        0 |  73.0 |  0.0 |  0.017
small_05  | small  | OPTIMAL  | yes  |       82 |         0 |      53.0 |        0 |  82.0 |  0.0 |  0.013
small_06  | small  | OPTIMAL  | yes  |       67 |         0 |      48.8 |        0 |  67.0 |  0.0 |  0.012
small_07  | small  | OPTIMAL  | yes  |       66 |         0 |      45.3 |        0 |  66.0 |  0.0 |  0.007
small_08  | small  | OPTIMAL  | yes  |       79 |         0 |      54.2 |        0 |  79.0 |  0.0 |  0.015
small_09  | small  | OPTIMAL  | yes  |       83 |         0 |      56.0 |        0 |  83.0 |  0.0 |  0.008
medium_00 | medium | FEASIBLE | yes  |      131 |         0 |      77.2 |        0 | 114.0 | 13.0 |  5.009
medium_01 | medium | OPTIMAL  | yes  |      131 |         0 |      65.7 |        0 | 131.0 |  0.0 |  0.053
medium_02 | medium | FEASIBLE | yes  |      132 |         0 |      82.7 |        0 | 127.0 |  3.8 |  5.014
medium_03 | medium | FEASIBLE | yes  |      141 |         0 |      89.0 |        0 | 134.0 |  5.0 |  5.028
medium_04 | medium | OPTIMAL  | yes  |      136 |         0 |      80.2 |        0 | 136.0 |  0.0 |  2.630
medium_05 | medium | OPTIMAL  | yes  |      126 |         0 |      64.7 |        0 | 126.0 |  0.0 |  0.042
medium_06 | medium | FEASIBLE | yes  |      133 |         0 |      78.2 |        0 | 127.0 |  4.5 |  5.022
medium_07 | medium | FEASIBLE | yes  |      137 |         0 |      84.3 |        0 | 128.0 |  6.6 |  5.012
medium_08 | medium | OPTIMAL  | yes  |      119 |         0 |      60.9 |        0 | 119.0 |  0.0 |  0.432
medium_09 | medium | OPTIMAL  | yes  |      127 |         0 |      68.9 |        0 | 127.0 |  0.0 |  0.321
large_00  | large  | FEASIBLE | yes  |      137 |         0 |      73.4 |        0 | 116.0 | 15.3 |  5.013
large_01  | large  | FEASIBLE | yes  |      143 |         0 |      73.0 |        0 | 120.0 | 16.1 |  5.024
large_02  | large  | FEASIBLE | yes  |      143 |         0 |      84.0 |        0 | 124.0 | 13.3 |  5.014
large_03  | large  | FEASIBLE | yes  |      148 |         0 |      86.2 |        0 | 135.0 |  8.8 |  5.015
large_04  | large  | FEASIBLE | yes  |      131 |         0 |      75.4 |        0 | 113.0 | 13.7 |  5.011
large_05  | large  | FEASIBLE | yes  |      146 |         0 |      82.5 |        0 | 113.0 | 22.6 |  5.014
large_06  | large  | FEASIBLE | yes  |      132 |         0 |      72.4 |        0 | 124.0 |  6.1 |  5.022
large_07  | large  | FEASIBLE | yes  |      136 |         0 |      71.5 |        0 | 121.0 | 11.0 |  5.027
large_08  | large  | FEASIBLE | yes  |      134 |         0 |      77.0 |        0 | 119.0 | 11.2 |  5.008
large_09  | large  | FEASIBLE | yes  |      136 |         0 |      69.8 |        0 | 118.0 | 13.2 |  5.007
----------+--------+----------+------+----------+-----------+-----------+----------+-------+------+-------
ALL (30)  | -      | 30/30    | 30/30 |    116.7 |         0 |      68.9 |        0 |     - |  5.5 | 78.868
```

### ASCII Gantt Chart (`labshift gantt --solver cpsat --time-limit 5 benchmarks/small_00.json`)

```text
Instance: small_00 | Solver: cpsat (OPTIMAL)
Makespan: 105m | Feasible: True | Tardiness: 0m | MeanFlow: 70.0m | WinViol: 0
Legend: [L] liquid_handler  [I] incubator  [R] plate_reader  [C] centrifuge  [W] washer

job_01 |LLLLLLLLLLLLLLLLIIIIIIIIIIIIIIII.RRR........................| L[0-18], L[18-28], I[29-56], R[58-62]
job_02 |  LLLLLLLLLLLLIIIIIIIIIIIIIIIIRRRR..........................| L[4-15], L[15-24], I[26-52], R[53-58]
job_03 |        ............LLLLLL......IIIIIIIIIIIIIIIIRRRRRR......| L[36-44], I[56-85], R[85-94]
job_04 |   ......................LLLLLLLLLLLLL.....IIIIIIIIIIIIIRRRR| L[44-60], L[60-65], I[76-99], R[99-105]
job_05 |    ............LLLLL.CCCCCCCWWWWW.RRRRR....................| L[28-36], C[39-52], W[52-58], R[62-69]
job_06 |          ...LLLLLLLLLLLLLLL.IIIIIIIIIIIIIIIRRRR............| L[24-39], L[39-49], I[52-76], R[77-84]
t(m)   |0           21          42         63          84        105|
```

**Reproducibility & Tie-Breaking Notes:**
- In the 2026-11-18 runs above (with default pure-makespan objective and 4 parallel workers), the Gantt chart and CP-SAT table for `small_00` both achieved optimal makespan `105` but reported different `mean_flow` (`70.0` vs `82.2`), because two schedules can share the optimal makespan with different secondary flow times. Passing `--tie-break-flow --workers 1 --seed 0` makes `solve_cpsat` deterministic and lexicographically minimizes flow time beneath makespan (achieving `makespan = 105` and proven minimal `mean_flow = 66.0` on `small_00`).
- Unsolved (`INFEASIBLE` / `UNKNOWN`) rows report `"-"` for `makespan` and `mean_flow` and are excluded from the `ALL (n)` column averages; the `status` summary cell explicitly reports `<solved>/<n>`.
- The committed files in `benchmarks/` were generated with `labshift generate --suite-dir benchmarks --seed 1000` (seeds `1000–1009`, `1100–1109`, `1200–1209`), which is the default seed when `--suite-dir` is set.
- Note on v1 suite bottleneck contention: `medium` (`12` jobs, `release_max=40`, `1` `plate_reader` => `0.30` jobs/min/reader) has higher per-reader arrival intensity than `large` (`20` jobs, `release_max=60`, `2` `plate_reader`s => `0.167` jobs/min/reader), explaining why myopic `greedy` is `4/10` feasible on `medium` vs `10/10` on `large`. Use `--preset hard` (`20` jobs, `release_max=45`, `1` `plate_reader` => `0.44` jobs/min/reader; `2/10` greedy feasible vs `10/10` `greedy_repair` on seeds `1300–1309`) to stress-test high-contention workcells.

## How It Works

1. **Models (`labshift.models`)**: Frozen validated dataclasses define `Operation` (with optional `transfer_time` and `setup_tag` / `assay_tag`), `Job`, `Instance` (with optional `transit_matrix`, sequence-dependent `setup_times`, and `blocking_handoff`), `ScheduledOp` (with optional `unit_index`), and `Schedule` with JSON round-trip helpers.
2. **Generator & Protocol Adapters (`labshift.generator`, `labshift.protocol`)**: `generator.py` samples seeded liquid-handling workflows across `small`, `medium`, `large`, and `hard` presets (with optional `ensure_feasible=True`). `protocol.py` converts concrete `twinstep` / `stepbot` protocol step sequences (including orthogonal `raman` and `lc_ms` confirmation modalities) into `Job` / `Instance` objects and exports scheduled timelines to workcell execution queues.
3. **Checker (`labshift.checker`)**: Independently verifies `instance_name` match, completeness, `unknown_instrument` / `wrong_instrument` routing, `negative_start`, `duration`, `release`, `precedence`, `min_wait` / `max_wait` / `transfer_time` windows, `deadline`, sequence-dependent `setup_time` changeovers, source-station blocking until `robot_arm` pickup, and departure-first sweep-line `capacity` across instruments and `robot_arm`.
4. **Solvers (`labshift.greedy`, `labshift.cpsat`)**:
   - `greedy` dispatches ready operations in `(ready_time, deadline, duration, job_id)` order via a cached event-profile sweep and per-unit setup tracker.
   - `greedy_repair` schedules contiguous `max_wait`-coupled and blocking-arm segments via recursive intra-chain right-shift propagation and setup-aware batching across deterministic priority rules (`30/30` feasible on `benchmarks/`).
   - `cpsat` tightens `IntervalVar` domains via `compute_horizon_upper_bound` and forward/backward chain propagation, injects full or partial per-job warm-start hints from `greedy` / `greedy_repair`, encodes sequence-dependent changeovers via `add_circuit`, enforces `no_overlap` or `cumulative` across stations and `robot_arm`, and supports pure makespan, capped/two-stage lexicographic `--tie-break-flow`, and `--soft-deadlines` tardiness optimization.

## 15-Repository Auto-Lab Wire Contract (`autolab-wire/v1`)

`autolab_wire.py` (`REPO_ID = "labshift"`, `LAYER = "L5"`) exposes `process_packet(packet: dict) -> dict` for the 15-repository **[`auto-lab`](https://github.com/yaniguan/auto-lab)** autonomous experimentation pipeline:

- **Upstream (`L5` / `L6` / `L2`)**: Accepts packets grounded by [`lab-automation-atlas`](https://github.com/yaniguan/lab-automation-atlas) and triaged by [`buy-or-make`](https://github.com/yaniguan/buy-or-make), consuming `parsed_spans.duration_min` from [`lab-ears`](https://github.com/yaniguan/lab-ears) following [`lab-ask-why`](https://github.com/yaniguan/lab-ask-why) and [`lab-decide`](https://github.com/yaniguan/lab-decide), as well as mid-campaign `RUN_ORTHOGONAL_ASSAY` (`Raman` / `LC-MS`) directives from [`lab-attrib`](https://github.com/yaniguan/lab-attrib). Halted packets pass through unchanged.
- **L5 Scheduling, Rolling-Horizon Insertion & Independent Verification**: Constructs a contended `Instance` (freezing in-flight operations `start < current_time_min`, applying pre-incubation holds to unstarted background plates, and inserting urgent orthogonal assays within perishability windows), solves deterministically via `solve_cpsat` (with automatic stdlib fallback), and audits the schedule with `check_schedule` and `compute_metrics`. Infeasible schedules halt with granular telemetry (`L5_SCHEDULE_WINDOW_BLOCK`, `L5_SCHEDULE_DEADLINE_BLOCK`, or `L5_SCHEDULE_CAPACITY_BLOCK`), writing `out["schedule"] = {"feasible", "makespan_min", "window_violations", "operations"}` and recording Lean invariant `missed_incubation_window_rejected` (`lean/LabShiftLean.lean`).
- **Downstream (`L4` -> `L1`/`L0` -> `L4`/`L3`)**: Forwards verified `out["schedule"]` packets to [`lab-rehearsal`](https://github.com/yaniguan/lab-rehearsal), continuing across [`lab-verify`](https://github.com/yaniguan/lab-verify), [`stepbot`](https://github.com/yaniguan/stepbot), [`lab-hands`](https://github.com/yaniguan/lab-hands), [`lab-feet`](https://github.com/yaniguan/lab-feet), post-execution causal blame & SHA-256 certified ledger [`lab-attrib`](https://github.com/yaniguan/lab-attrib), [`twinstep`](https://github.com/yaniguan/twinstep), [`labfill`](https://github.com/yaniguan/labfill), and [`labbench-posttrain`](https://github.com/yaniguan/labbench-posttrain).

## Limits

- **Multi-worker time-limit variation**: Multi-worker CP-SAT (`--workers 4`) under a wall-clock time limit can vary slightly across machines; pass `--workers 1 --seed <int> --tie-break-flow` when bit-exact CP-SAT reproducibility is required.
- **Abstracted station mechanics**: Durations, capacities, and optional `transfer_time` represent discrete integer-minute station and arm reservations rather than continuous 3D trajectory planning or tip/reagent inventory depletion.
- **Heuristic optimality gap**: `greedy_repair` eliminates `max_wait` violations on `benchmarks/` (`30/30` feasible) without external dependencies, but does not prove global makespan optimality (`129.2` min vs `116.7` min for `cpsat`).
