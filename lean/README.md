# LabShiftLean (Lean 4.36.0 Formal Scheduling Guards)

`LabShiftLean` provides machine-checked L5 scheduling invariants for `labshift` in Lean 4 (`v4.36.0`), with zero external mathlib dependencies, zero `sorry`, and zero custom `axiom` declarations.

## Covered Invariants and Theorems

1. **Contended Multi-Pool Schedule, `opId` Uniqueness & Incubation Windows (`LabShiftLean.lean`)**
   - `ScheduledOp`, `WaitLink`, `hasUniqueOpIds`, `ScheduleOK`, and executable checker `checkSchedule`.
   - `hasUniqueOpIds_iff_pairwise` & `duplicate_opId_rejected`: enforces `List.Pairwise (· ≠ ·)` over `opId`s so distinct operations can never evade mutual-exclusion checks via `opId` collision.
   - `checkSchedule_ok_iff`: exact soundness and completeness equivalence.
   - `missed_incubation_window_rejected` & `premature_incubation_window_rejected`: guarantees zero biological plates violate `[min_wait, max_wait]`.
   - `over_capacity_machine_rejected` & `overlapping_same_machine_rejected`: pool capacity and machine mutual-exclusion soundness.
2. **Discrete-Time Pool Occupancy & Departure-First Sweep-Line Equivalence**
   - `opActiveAt` & `activeCount`: half-open `[startMin, endMin)` discrete-time pool occupancy.
   - `adjacent_handoff_activeCount_le_one`: proves boundary-adjacent handoffs (`a.endMin ≤ b.startMin`, including `a.endMin = b.startMin`) never spike concurrent occupancy above `1`.
   - `distinct_lt_length_le` & `scheduleOK_implies_activeCount_le_poolCap`: finite pigeonhole proof that `ScheduleOK` implies `activeCount poolId t ops ≤ cap` at every minute `t`.
   - `sweepLinePoolOK` & `sweepLinePoolOK_iff_activeCount_le`: formalizes `src/labshift/checker.py`'s departure-first `(endMin, -1)` before `(startMin, +1)` event sweep and proves exact `↔` equivalence with `∀ t, activeCount poolId t ops ≤ cap`.
3. **Verified Pool-to-Lane Coloring Bridge**
   - `PoolOp`, `assignPoolLanes`, and `assignPoolLanes_sound`: greedy interval-lane assignment bridging `machineIdx`-free pool schedules into certified `List ScheduledOp` satisfying `ScheduleOK`.
4. **Rolling-Horizon Breakdown Contingency Gate (Idea #07)**
   - `FaultEvent`, `avoidsFaultWindow`, `checkContingencySchedule`.
   - `faulted_machine_post_fault_rejected` & `contingency_schedule_preserves_windows`: mirrors `community_extensions/rolling_contingency_scheduler.py`.
5. **Day/Night Dual-Regime Queue Partition Gate (Idea #08)**
   - `DualRegimeSlot`, `checkDualRegimeSlot`.
   - `unverified_overnight_run_rejected` & `unsigned_or_late_frontier_rejected`: mirrors `community_extensions/night_shift_batcher.py`.
6. **Energetic Pool Lower Bound (#36) & Planted Witness Construction (#37)**
   - `mkPlantedWaitLink` & `plantedWaitLink_ok`: proves witness-derived `max_wait = planted_wait + slack` transitions satisfy `waitLinkOK`.
   - `twoStepChain_makespan_lb` & `energeticPoolBound_le_makespan`: proves `rMin + ceil(poolWork / cap) + qMin ≤ makespan` over discrete-time occupancy for every `ScheduleOK` schedule (`src/labshift/bounds.py`).

## Verification

```bash
cd lean
lake build
```
