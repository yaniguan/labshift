# labshift

A synthetic lab scheduling and planning benchmark for multi-instrument liquid-handling workflows.

## What It Is

`labshift` models scheduling of synthetic liquid-handling jobs (such as plate preparation, buffer addition, dilution series, washing, spinning, incubation, and plate reads) across shared instrument pools:

- `liquid_handler`
- `incubator`
- `plate_reader`
- `centrifuge`
- `washer`

Each job is a chain of operations with durations (in integer minutes), job release times, optional deadlines, and optional `min_wait` / `max_wait` time windows between consecutive operations. The package includes:

- JSON-serializable dataclasses (`Instance`, `Job`, `Operation`, `Schedule`, `ScheduledOp`)
- A seeded synthetic instance generator (`small`, `medium`, `large` presets) and a fixed 30-instance benchmark suite in `benchmarks/`
- A schedule feasibility checker validating completeness, durations, precedence, time windows, release times, deadlines, and instrument capacities
- A greedy list-scheduling baseline (`greedy`) and an OR-Tools CP-SAT solver (`cpsat`)
- Metrics (`makespan`, `total_tardiness`, `mean_flow_time`, `window_violations`, `solve_time_s`, and gap to CP-SAT best bound) and an ASCII Gantt chart renderer

## Install

Requires Python 3.11+.

```bash
pip install -e .[dev]
```

Part of **[Robot AI Auto-Lab (`auto-lab`)](https://github.com/yaniguan/auto-lab)** ([Project Studio](https://yaniguan.github.io/auto-lab/)) — **Layer `L5 · Executive Planning, Procurement & Scheduling`**.

## System Profile: Object · Data · Model · Training · History · Unique Properties

Full code-level architecture, CP-SAT formulation, benchmark analysis (`main` vs open PR `origin/review-fixes`), and history are documented in **[`docs/ARCHITECTURE_AND_HISTORY.md`](docs/ARCHITECTURE_AND_HISTORY.md)**:

- **Object**: Executive multi-instrument lab scheduling benchmark, sweep-line verifier, greedy baseline, and OR-Tools CP-SAT solver over shared `liquid_handler`, `incubator`, `plate_reader`, `centrifuge`, and `washer` pools with coupled `min_wait`/`max_wait` perishability windows, release times, and deadlines.
- **Data**: Frozen JSON-serializable models (`Instance`, `Job`, `Operation`, `Schedule`, `ScheduledOp`), 6 seeded assay workflow templates, and a committed 30-instance MIT-licensed benchmark suite (`small_00..09`, `medium_00..09`, `large_00..09`, seeds `1000..1209`).
- **Model**: Independent departure-first `(t, -1)` sweep-line feasibility checker (`labshift.checker`), priority list-scheduling baseline (`labshift.greedy`), and OR-Tools CP-SAT `IntervalVar` + `no_overlap`/`cumulative` makespan optimizer (`labshift.cpsat`) with dual `best_bound` gap certificates.
- **Training**: Verifiable combinatorial planning & RLVR reward environment (no internal neural weights; on the 30-instance CPU benchmark, `cpsat` achieves `30/30` feasibility and `116.7` min mean makespan with `5.5%` mean optimality gap vs `greedy` at `24/30` feasibility, `129.8` min makespan, and `13` `max_wait` violations).
- **History**: Initial release `124a6d7` on `main` (`0.1.0`, 30 benchmark instances, 4 test suites) and open PR `origin/review-fixes` (`58ec7f2`, aligning `--suite-dir` default `--seed 1000` and documenting `mean_flow` non-uniqueness under pure makespan minimization).
- **Unique Properties**: Exact half-open interval sweep-line verification, perishability window stress-testing, dual optimality bounds, and clean interfaces with [`labfill`](https://github.com/yaniguan/labfill), [`lab-decide`](https://github.com/yaniguan/lab-decide), [`buy-or-make`](https://github.com/yaniguan/buy-or-make), [`lab-rehearsal`](https://github.com/yaniguan/lab-rehearsal), [`lab-verify`](https://github.com/yaniguan/lab-verify), [`YLM`](https://github.com/yaniguan/YLM) LabLean, and [`stepbot`](https://github.com/yaniguan/stepbot).

## Repository Layout

```text
labshift/
├── benchmarks/                  # 30 seeded JSON instances (small_00..09, medium_00..09, large_00..09)
├── src/labshift/
│   ├── __init__.py              # Public API exports
│   ├── models.py                # Frozen dataclasses & JSON serialization
│   ├── generator.py             # 6 workflow templates & small/medium/large presets
│   ├── checker.py               # Completeness, temporal window, and sweep-line capacity checker
│   ├── greedy.py                # Earliest-slot priority list-scheduling baseline
│   ├── cpsat.py                 # OR-Tools CP-SAT interval & cumulative solver
│   ├── metrics.py               # Makespan, tardiness, mean flow, violations, and dual gap%
│   ├── gantt.py                 # Job-oriented ASCII Gantt chart renderer
│   ├── bench.py                 # Suite runner & ASCII summary table formatter
│   └── cli.py                   # `labshift bench | gantt | generate | check` CLI
├── docs/
│   └── ARCHITECTURE_AND_HISTORY.md
└── tests/                       # Checker, model/generator, solver, and CLI/Gantt tests
```

## Quickstart

Run the benchmark suite with the greedy baseline or OR-Tools CP-SAT:

```bash
labshift bench --solver greedy benchmarks/
labshift bench --solver cpsat --time-limit 5 benchmarks/
```

Render an ASCII Gantt chart for a single instance:

```bash
labshift gantt --solver cpsat --time-limit 5 benchmarks/small_00.json
```

Generate a new synthetic instance or check an existing schedule:

```bash
labshift generate --preset medium --seed 42 --out /tmp/medium_42.json
labshift check instance.json schedule.json
```

## Example Output

All outputs below were measured on a CPU-only Linux box, 2026-11-18.

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
ALL (30)  | -      | -      | 24/30 |    129.8 |         0 |      72.3 |       13 |     - |    - |  0.012
```

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
ALL (30)  | -      | -        | 30/30 |    116.7 |         0 |      68.9 |        0 |     - |  5.5 | 78.868
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

The Gantt chart and the CP-SAT table above are both for `small_00` at makespan 105, but they report different mean flow (70.0 in the chart, 82.2 in the table). CP-SAT minimizes makespan only, so two optimal schedules can have the same makespan and different mean flow. The mean_flow column is therefore not a unique result. These numbers were not re-measured here.

The committed files in `benchmarks/` were generated with `labshift generate --suite-dir benchmarks --seed 1000` (seeds 1000–1009, 1100–1109, 1200–1209). That is also the default seed when `--suite-dir` is set. There is no committed raw bench log behind the tables.

## How It Works

1. **Models (`labshift.models`)**: Frozen dataclasses define `Operation`, `Job`, `Instance`, `ScheduledOp`, and `Schedule` with JSON round-trip helpers.
2. **Generator (`labshift.generator`)**: Combines generic liquid-handling workflow templates (`plate_prep`, `dilution_series`, `buffer_add`, `dye_dispense`, `mix_wash`, `plate_wash`, `spin_down`, `incubate`, `equilibrate`, `plate_read`) with seeded release times, wait windows, and deadlines.
3. **Checker (`labshift.checker`)**: Verifies completeness, operation durations, job release times, precedence, `min_wait`/`max_wait` windows, deadlines, and exact interval sweep-line capacity per instrument pool.
4. **Solvers (`labshift.greedy`, `labshift.cpsat`)**:
   - `greedy` dispatches ready operations in `(ready_time, deadline, duration, job_id)` order onto the earliest feasible instrument slot. Because it acts without lookahead, it can violate `max_wait` windows under contention.
   - `cpsat` models each operation as an OR-Tools CP-SAT interval variable, enforces `no_overlap` (capacity 1) or `cumulative` (capacity > 1) per instrument pool, enforces precedence, time windows, and hard deadlines, and minimizes `makespan`.

## Limits

- **Run-to-run variation**: CP-SAT with a time limit is not fully deterministic. A second run of the table above gave the same makespan and gap, with mean flow 68.8 instead of 68.9.
- **Synthetic workloads only**: Durations, capacities, and templates are synthetic integer-minute abstractions and do not model deck layouts, tip racks, or physical transfer setup times.
- **Hard deadlines in CP-SAT**: `cpsat` treats job deadlines as hard constraints and returns `INFEASIBLE` if no schedule satisfies every deadline, rather than minimizing tardiness softly.
- **Single makespan objective**: `cpsat` minimizes `makespan` only; alternate optimal solutions with the same makespan can exhibit different `mean_flow_time` values. That is why the `small_00` table (82.2) and Gantt chart (70.0) disagree. Do not treat the CP-SAT mean_flow column as a stable result.
- **Myopic greedy baseline**: `greedy` does not perform lookahead or repair for `max_wait` windows or deadlines; violations are reported explicitly by the checker.
