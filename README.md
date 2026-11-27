# labshift

A multi-instrument laboratory scheduling and planning benchmark with perishability time windows, sequence-dependent setups, robotic arm handoffs, stdlib bounds/metaheuristics, and exact CP-SAT optimization.

## What It Is

`labshift` models scheduling of synthetic and protocol-derived liquid-handling jobs (plate preparation, buffer addition, serial dilution, washing, centrifugation, incubation, plate reads, and orthogonal Raman / LC-MS confirmation) across shared finite-capacity instrument pools:

- `liquid_handler`
- `incubator`
- `plate_reader`
- `centrifuge`
- `washer`
- `raman_spectrometer` / `lc_ms` (orthogonal analytical confirmation)
- optional `robot_arm` (inter-instrument pickup-hold-transit when `transfer_time > 0` or `transit_matrix` is modeled)

Each job is a chain of operations with integer-minute durations (`>= 1`), release times, optional deadlines, optional `min_wait` / `max_wait` inter-step perishability windows, optional `transfer_time` / `transit_matrix` arm transit delays, and optional sequence-dependent decontamination/thermal `setup_times`. The package includes:

- Validated JSON-serializable dataclasses (`Instance`, `Job`, `Operation`, `Schedule`, `ScheduledOp`)
- A seeded synthetic and **planted-schedule** instance generator (`small`, `medium`, `large`, `hard`, `--ensure-feasible`, and `--planted`), a fixed 30-instance v1 suite in `benchmarks/`, and a 20-instance planted hard suite in `benchmarks/planted/` (with witness schedules in `benchmarks/planted_witnesses/`)
- Protocol adapters (`job_from_protocol`, `job_from_stepbot_spec`, `instance_from_protocols`, `export_workcell_queue`) bridging `twinstep` and `stepbot` protocols into `labshift` instances and robot dispatch queues, plus open benchmark importers (`labshift.open_benchmarks`) for **PSPLIB (`j30`/`j60`)**, **OR-Library / Taillard JSSP**, and **Opentrons / WLP / BioPlanner** wet-lab campaigns
- An independent departure-first sweep-line feasibility checker (`check_schedule`) and stdlib combinatorial makespan lower bounds (`makespan_lower_bound` in `labshift.bounds`)
- Three zero-dependency stdlib solvers (`greedy`, coupled right-shift `greedy_repair`, and Iterated Greedy / Large Neighborhood Search `lns`), a warm-started OR-Tools CP-SAT solver (`cpsat`), and an optional `PyJobShop` (`>= 0.0.9`) head-to-head adapter (`pyjobshop`)
- Feasible-only summary metrics, valid lower-bound `gap_pct` for every solver, paired feasible solver comparisons (`--compare`), and an ASCII Gantt chart renderer

## Install

Requires Python 3.11+. Core `labshift` has **zero third-party runtime dependencies** (`dependencies = []`):

```bash
# Pure-stdlib install (models, bounds, checker, generator, protocol, open_benchmarks, greedy, greedy_repair, lns)
pip install -e .

# With OR-Tools CP-SAT, pytest, ruff, and mypy
pip install -e ".[dev]"

# Optional PyJobShop >= 0.0.9 head-to-head benchmark adapter
pip install -e ".[cpsat,pyjobshop]"
```

Part of **[Robot AI Auto-Lab (`auto-lab`)](https://github.com/yaniguan/auto-lab)** ([Project Studio](https://yaniguan.github.io/auto-lab/)) — **Layer `L5 · Executive Planning, Procurement & Scheduling`**.

## System Profile: Object · Data · Model · Training · History · Unique Properties

Full code-level architecture, mathematical formulations, benchmark analysis, and engineering history are documented in **[`docs/ARCHITECTURE_AND_HISTORY.md`](docs/ARCHITECTURE_AND_HISTORY.md)**:

- **Object**: Executive multi-instrument lab scheduling benchmark, sweep-line verifier, stdlib Jackson/Carlier energetic lower bounds, `greedy` / `greedy_repair` / `lns` stdlib solvers, `twinstep` / `stepbot` / public-benchmark bridges, and OR-Tools CP-SAT + `PyJobShop` optimizers over shared station and `robot_arm` pools.
- **Data**: Domain-validated frozen dataclasses (`Instance`, `Job`, `Operation`, `Schedule`, `ScheduledOp`), 30 committed v1 instances (`benchmarks/*.json`), 20 committed planted hard instances (`benchmarks/planted/*.json` + `benchmarks/planted_witnesses/*.schedule.json`), 3 MIT-licensed `stepbot` protocol JSON fixtures, and `scripts/fetch_open_benchmarks.py` fetching OR-Library/Taillard JSSP, PSPLIB `j30`/`j60`, and Opentrons + WLP + BioPlanner campaigns into gitignored `data/`.
- **Model**: Independent departure-first `(t, -1)` sweep-line checker (`labshift.checker`), stdlib makespan lower bound (`makespan_lower_bound`), list-scheduling `solve_greedy`, coupled-segment `solve_greedy_repair`, Ruiz-Stuetzle Destroy-and-Best-Insertion `solve_lns`, warm-started CP-SAT (`solve_cpsat` tightened with `makespan >= makespan_lower_bound`), and `solve_pyjobshop`.
- **Training**: Deterministic combinatorial planning & RLVR reward environment (no internal neural weights; every solver receives an un-clamped `gap_pct` against a valid lower bound `max(solver_bound, makespan_lower_bound)`).
- **History**: Initial `0.1.0` 30-instance suite (`124a6d7`), review clarifications (`58ec7f2`), audit hardening (`greedy_repair`, blocking `robot_arm`, `setup_times`, `autolab-wire/v1` rolling orthogonal assays), and frontier upgrade resolving `#35`–`#38` (explicit time-unit table, stdlib energetic lower bounds, planted-schedule tier & paired stats, native `stepbot` protocol import, `solve_lns`, `PyJobShop` head-to-head, Lean 4 energetic/planted proofs, and public open benchmarks).
- **Unique Properties**: Exact half-open interval sweep-line verification, perishability window stress-testing, valid lower bound gap reporting for every solver, zero-dependency stdlib deployability, and clean interfaces with [`twinstep`](https://github.com/yaniguan/twinstep), [`labfill`](https://github.com/yaniguan/labfill), [`lab-decide`](https://github.com/yaniguan/lab-decide), [`buy-or-make`](https://github.com/yaniguan/buy-or-make), [`lab-rehearsal`](https://github.com/yaniguan/lab-rehearsal), [`lab-verify`](https://github.com/yaniguan/lab-verify), [`YLM`](https://github.com/yaniguan/YLM) LabLean, and [`stepbot`](https://github.com/yaniguan/stepbot).

## Repository Layout

```text
labshift/
├── autolab_wire.py              # autolab-wire/v1 Layer L5 scheduling & verification node
├── benchmarks/                  # 30 v1 instances + planted/ (20 instances) + planted_witnesses/
├── community_extensions/        # Rolling breakdown repair (Idea #07) & night-shift packer (Idea #08)
├── lean/                        # Lean 4.36.0 sweep-line, energetic LB, planted & dual-regime proofs
├── scripts/
│   └── fetch_open_benchmarks.py # Downloads & benchmarks JSSP, PSPLIB j30/j60, & wet-lab campaigns
├── src/labshift/
│   ├── __init__.py              # Public API exports with PEP 562 lazy cpsat/pyjobshop loading
│   ├── models.py                # Frozen validated dataclasses & JSON serialization
│   ├── bounds.py                # Stdlib chain, Jackson/Carlier energetic, & robot_arm lower bounds
│   ├── generator.py             # Workflow templates, presets, & --planted witness generator
│   ├── protocol.py              # Strict time-unit table & twinstep/stepbot protocol importers
│   ├── open_benchmarks.py       # JSSP, PSPLIB (.sm/.dat), Opentrons, WLP, & BioPlanner importers
│   ├── checker.py               # Completeness, temporal window, arm, setup, & sweep-line checker
│   ├── greedy.py                # O(K log K) greedy baseline & coupled right-shift greedy_repair
│   ├── lns.py                   # Pure-stdlib Iterated Greedy / Large Neighborhood Search (solve_lns)
│   ├── cpsat.py                 # Warm-started OR-Tools CP-SAT solver tightened with stdlib bounds
│   ├── pyjobshop_bridge.py      # Optional PyJobShop >= 0.0.9 adapter & extension quantifier
│   ├── metrics.py               # Makespan, tardiness, flow, lower_bound, solver_bound, & gap%
│   ├── gantt.py                 # Job-oriented ASCII Gantt chart renderer
│   ├── bench.py                 # Feasible-only summary tables & paired solver comparisons
│   └── cli.py                   # `labshift solve | bench | gantt | generate | check` CLI
├── docs/
│   └── ARCHITECTURE_AND_HISTORY.md
└── tests/                       # Protocol, bounds, planted, LNS, CP-SAT, PyJobShop, open & wire tests
```

## Quickstart

Run any solver (`greedy`, `greedy_repair`, `lns`, `cpsat`, or `pyjobshop`) with paired comparison:

```bash
labshift bench --solver greedy_repair --compare cpsat --time-limit 5 benchmarks/
labshift bench --solver lns --compare greedy_repair --time-limit 2 benchmarks/planted/
```

Generate a guaranteed-feasible hard planted instance alongside its witness schedule and verify it:

```bash
labshift generate --preset medium --planted --seed 2100 --out planted_m0.json --witness-out witness_m0.json
labshift check planted_m0.json witness_m0.json
```

Fetch and evaluate public scheduling and wet-lab benchmarks (cached in gitignored `data/open_benchmarks/`):

```bash
python scripts/fetch_open_benchmarks.py --time-limit 2.0
```

## Protocol Bridge: Time Units & Supported `stepbot` Step Kinds

`labshift.protocol` (`job_from_protocol`, `job_from_stepbot_spec`, `instance_from_protocols`) stays 100% stdlib-only (YAML parsing via `yaml.safe_load` stays with the caller):

- **Explicit case-insensitive time-unit table**:
  - `ms`
  - `s`, `sec`, `secs`, `second`, `seconds`
  - `m`, `min`, `mins`, `minute`, `minutes`
  - `h`, `hr`, `hrs`, `hour`, `hours`
  - `d`, `day`, `days`
  - When `time` or `duration` is given with **no unit key at all**, it defaults to **minutes**. Volume units (`nL`, `uL`, `mL`, `L`) in `unit` are never read as time units and raise a `ValueError` naming the step index, step type, and bad unit.
- **Supported `stepbot` step kinds** (both single-key `- transfer: {...}` and bare-number/bare-string shorthands):
  - `comment` (`0` s; does not split contiguous instrument groups)
  - `delay` (`seconds + 60 * minutes`, or bare number as seconds, added to the current `liquid_handler` group)
  - `pause` (`seconds + 60 * minutes` when given; timeless operator pauses require `pause_minutes=` or raise `ValueError` naming the step index)
  - `pick_up_tip`, `drop_tip`, `discard` (fixed `STEPBOT_TIP_ACTION_SECONDS = 5.0` s on `liquid_handler`)
  - `transfer`, `distribute` (`30 + 0.05 * volume_ul` s on `liquid_handler`)
  - `mix` (`15 + 6 * reps` s on `liquid_handler`, supporting `reps`, `repetitions`, and `cycles`)
  - `dilution_series` (`60 + 15 * n + 6 * max(0, n - 1) * mix_reps` s on `liquid_handler`)

## Benchmark Results

All tables below were measured on **2026-11-26** on an **AMD Ryzen AI 7 PRO 350** CPU (Python 3.11.15, `ortools 9.15.6755`, `pyjobshop 0.0.9`).

**Bound & Summary Column Definitions:**
- `lb`: Pure-stdlib combinatorial makespan lower bound `makespan_lower_bound(instance)` (`max` of longest job chain, per-instrument Jackson/Carlier subset `(r*, q*)` energetic bound + changeover slack, and `robot_arm` energetic bound).
- `bound`: Effective valid lower bound `best_bound = max(solver_bound, lb)` used to compute `gap%` (`solver_bound` is kept separately in `Metrics.solver_bound`).
- `gap%`: `100 * (makespan - bound) / makespan` against `best_bound`, reported for every solver.
- `ALL (n)` summary row: `makespan`, `mean_flow`, `lb`, and `gap%` are averaged **over feasible rows only**, explicitly labeled `feas-only (k/n)` in the summary `preset` column.

### 1. Committed 30-Instance v1 Suite (`benchmarks/*.json`)

| Solver | Feasible | Feas-Only Mean Makespan (m) | Feas-Only Mean Flow (m) | Window Viols | Mean `gap%` (vs `bound`) | Total Time (s) |
|---|---:|---:|---:|---:|---:|---:|
| `greedy` (stdlib) | `24/30` | `124.4` | `67.8` | `13` | `15.2%` | `0.073` |
| `greedy_repair` (stdlib) | **`30/30`** | `129.2` | `70.6` | **`0`** | `14.7%` | `0.735` |
| `lns` (`2s`, stdlib) | **`30/30`** | **`122.4`** | `68.5` | **`0`** | **`10.1%`** | `58.548` |
| `cpsat` (`5s`, `4` workers) | **`30/30`** | **`116.5`** | `68.4` | **`0`** | **`4.0%`** | `81.879` |
| `pyjobshop` (`5s`, `4` workers) | **`30/30`** | `116.9` | `68.4` | **`0`** | `4.3%` | `83.734` |

- **Paired differences on jointly feasible instances**:
  - `greedy` vs `greedy_repair` (`24/30` shared feasible): `124.4` vs `123.5` m (`+0.9` m; `greedy_repair` better on `5`, equal on `19`).
  - `greedy_repair` vs `lns` (`30/30` shared feasible): `129.2` vs `122.4` m (`+6.8` m; `lns` better on `24`, equal on `6`).
  - `cpsat` vs `pyjobshop` (`30/30` shared feasible): `116.5` vs `116.9` m (`-0.4` m; `cpsat` better on `8`, `pyjobshop` on `1`, equal on `21`).

#### `greedy_repair` Sample Output (`labshift bench --solver greedy_repair benchmarks/`)

```text
instance  | preset            | status | feas  | makespan | tardiness | mean_flow | win_viol |    lb | bound | gap% | time_s
----------+-------------------+--------+-------+----------+-----------+-----------+----------+-------+-------+------+-------
small_00  | small             | OK     | yes   |      116 |         0 |      72.8 |        0 |    96 |  96.0 | 17.2 |  0.010
...
medium_00 | medium            | OK     | yes   |      144 |         0 |      76.1 |        0 |   122 | 122.0 | 15.3 |  0.020
...
large_05  | large             | OK     | yes   |      162 |         0 |      81.1 |        0 |   134 | 134.0 | 17.3 |  0.058
...
----------+-------------------+--------+-------+----------+-----------+-----------+----------+-------+-------+------+-------
ALL (30)  | feas-only (30/30) | 30/30  | 30/30 |    129.2 |         0 |      70.6 |        0 | 109.0 |     - | 14.7 |  0.735
```

### 2. Committed 20-Instance Planted Suite (`benchmarks/planted/*.json`)

Constructed via `labshift generate --suite-dir benchmarks/planted --planted --witness-dir benchmarks/planted_witnesses` (`10` planted `medium` seeds `2100..2109` + `10` planted `large` seeds `2200..2209`, with `max_wait = planted_wait + U{0..2}` on `70%` of transitions and `deadline = planted_end + U{0..5}`). Measured on **AMD Ryzen AI 7 PRO 350** on **2026-11-26**:

| Solver | Total Feasible | `planted_medium` | `planted_large` | Feas-Only Mean Makespan (m) | Window / Deadline Viols | Mean `gap%` | Total Time (s) |
|---|---:|---:|---:|---:|---:|---:|---:|
| Planted Witnesses | **`20/20`** | `10/10` | `10/10` | `162.8` | `0 / 0` | `21.9%` | — |
| `greedy` (stdlib) | **`0/20`** | `0/10` | `0/10` | — | `256 / 174` | — | `0.044` |
| `greedy_repair` (stdlib) | **`7/20`** | `7/10` | `0/10` | `152.0` | `0 / 36` | `17.3%` | `0.604` |
| `lns` (`2s`, stdlib) | **`17/20`** | **`10/10`** | **`7/10`** | `153.7` | `0 / 3` | `18.1%` | `40.328` |
| `cpsat` (`5s`, `4` workers) | **`20/20`** (`20 OPT`) | **`10/10`** | **`10/10`** | **`146.2`** | **`0 / 0`** | **`0.0%`** | **`6.016`** |
| `pyjobshop` (`5s`, `4` workers) | **`20/20`** (`20 OPT`) | **`10/10`** | **`10/10`** | **`146.2`** | **`0 / 0`** | **`0.0%`** | `8.058` |

- Paired on the `7` planted instances where `greedy_repair` is feasible: `greedy_repair` `152.0` m vs `lns` `147.4` m (`-4.6` m) vs `cpsat` `143.1` m (`-8.9` m).

### 3. Public Open Benchmarks (`scripts/fetch_open_benchmarks.py`, `2s` limit)

| Public Benchmark Suite | Instances | `greedy` Feas / Makespan | `greedy_repair` Feas / Makespan | `lns` (stdlib) Feas / Makespan (`gap%`) | `cpsat` Feas / Makespan (`gap%`) | `pyjobshop` Feas / Makespan (`gap%`) |
|---|---:|---|---|---|---|---|
| **OR-Library / Taillard JSSP** (`ft06, la01..la03, ta01`) | `5` | `5/5` / `819.8` | `5/5` / `819.8` | `5/5` / **`720.2`** (`12.1%`) | `5/5` / **`649.4`** (`1.7%`, `4 OPT`) | `5/5` / `655.6` (`2.1%`, `4 OPT`) |
| **PSPLIB RCPSP (`j30` & `j60`)** | `6` | `6/6` / `51.5` | `6/6` / `51.5` | **`6/6`** / **`50.8`** (**`0.0%`**, `6 OPT`) | **`6/6`** / **`50.8`** (**`0.0%`**, `6 OPT`) | **`6/6`** / **`50.8`** (**`0.0%`**, `6 OPT`) |
| **Wet-Lab Multi-Protocol Campaigns** (`Opentrons/Protocols` + `WLP` + `BioPlanner`) | `3` | `1/3` / `357.0` (`2` win viol) | **`3/3`** / **`349.0`** (`0.0%`) | **`3/3`** / **`349.0`** (**`0.0%`**, `3 OPT`) | **`3/3`** / **`349.0`** (**`0.0%`**, `3 OPT`) | `1/3` / `352.0` (`2` arm-blocking viols) |

### 4. Where `labshift` Extends Beyond Generic JSSP (`quantify_labshift_extensions`)

| Physical Self-Driving Lab Regime | `labshift` (`solve_lns` / `solve_cpsat`) | Generic JSSP (`PyJobShop 0.0.9`) | Why Generic JSSP Violates `check_schedule` |
|---|---|---|---|
| **`robot_arm` Pickup-Hold-Transit (`blocking_handoff=True`)** | **Feasible (`18` m `OPTIMAL`)** | Infeasible (`capacity` violation) | Upstream centrifuge/incubator remains physically occupied by the plate until `robot_arm` pickup (`rdur = pickup - start`) |
| **Multi-Unit Decontamination Matrix (`cap > 1` + `setup_times`)** | **Feasible (`16` m `OPTIMAL`)** | Infeasible (`setup_time` violation) | `PyJobShop.add_setup_time` only supports `Machine` (`cap=1`), whereas `labshift` models per-unit `add_circuit` changeovers on `cap > 1` pools |
| **Mid-Campaign `RUN_ORTHOGONAL_ASSAY` (`fixed_ops` Rolling Horizon)** | **Feasible (`45` m, `0` window viols)** | Unsupported | Freezes in-flight operations (`start < current_time_min`), holds unstarted background plates, and splices urgent Raman/LC-MS confirmation |

## 15-Repository Auto-Lab Wire Contract (`autolab-wire/v1`)

`autolab_wire.py` (`REPO_ID = "labshift"`, `LAYER = "L5"`) exposes `process_packet(packet: dict) -> dict` for the 15-repository **[`auto-lab`](https://github.com/yaniguan/auto-lab)** autonomous experimentation pipeline:

- **Upstream (`L5` / `L6` / `L2`)**: Accepts packets grounded by [`lab-automation-atlas`](https://github.com/yaniguan/lab-automation-atlas) and triaged by [`buy-or-make`](https://github.com/yaniguan/buy-or-make), consuming `parsed_spans.duration_min` from [`lab-ears`](https://github.com/yaniguan/lab-ears) following [`lab-ask-why`](https://github.com/yaniguan/lab-ask-why) and [`lab-decide`](https://github.com/yaniguan/lab-decide), plus mid-campaign `RUN_ORTHOGONAL_ASSAY` (`Raman` / `LC-MS`) directives from [`lab-attrib`](https://github.com/yaniguan/lab-attrib). Halted packets pass through unchanged.
- **L5 Scheduling, Rolling-Horizon Insertion & Independent Verification**: Freezes in-flight operations, solves deterministically via `solve_cpsat` (with automatic stdlib fallback), and audits the schedule with `check_schedule` and `compute_metrics`. Infeasible schedules halt with `L5_SCHEDULE_WINDOW_BLOCK`, `L5_SCHEDULE_DEADLINE_BLOCK`, or `L5_SCHEDULE_CAPACITY_BLOCK`, recording Lean 4 invariants (`lean/LabShiftLean.lean`).
- **Downstream (`L4` -> `L1`/`L0` -> `L4`/`L3`)**: Forwards verified `out["schedule"]` packets to [`lab-rehearsal`](https://github.com/yaniguan/lab-rehearsal), [`lab-verify`](https://github.com/yaniguan/lab-verify), [`stepbot`](https://github.com/yaniguan/stepbot), [`lab-hands`](https://github.com/yaniguan/lab-hands), [`lab-feet`](https://github.com/yaniguan/lab-feet), [`lab-attrib`](https://github.com/yaniguan/lab-attrib), [`twinstep`](https://github.com/yaniguan/twinstep), [`labfill`](https://github.com/yaniguan/labfill), and [`labbench-posttrain`](https://github.com/yaniguan/labbench-posttrain).

## Limits

- **Multi-worker time-limit variation**: Multi-worker CP-SAT (`--workers 4`) under a wall-clock time limit can vary slightly across machines; pass `--workers 1 --seed <int> --tie-break-flow` when bit-exact CP-SAT reproducibility is required.
- **Abstracted station mechanics**: Durations, capacities, and `transfer_time` represent discrete integer-minute station and arm reservations rather than continuous 3D trajectory planning or tip/reagent inventory depletion.
- **Stdlib metaheuristic gap on dense large instances**: `solve_lns` raises planted-suite stdlib feasibility from `7/20` (`greedy_repair`) to `17/20` without C++ solvers, while exact `solve_cpsat` reaches `20/20 OPTIMAL` in `6.0 s`.
