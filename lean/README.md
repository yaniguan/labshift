# LabShiftLean (Lean 4.36.0 Formal Scheduling Guards)

`LabShiftLean` provides machine-checked L5 scheduling invariants for `labshift` in Lean 4 (`v4.36.0`), with zero external mathlib dependencies, zero `sorry`, and zero custom `axiom` declarations.

## Covered Invariants and Theorems

1. **Contended Multi-Pool Schedule & Incubation Windows (`LabShiftLean.lean`)**
   - `ScheduledOp`, `WaitLink`, `ScheduleOK`, and executable checker `checkSchedule`.
   - `checkSchedule_ok_iff`: exact soundness and completeness equivalence.
   - `missed_incubation_window_rejected` & `premature_incubation_window_rejected`: guarantees zero biological plates violate `[min_wait, max_wait]`.
   - `over_capacity_machine_rejected` & `overlapping_same_machine_rejected`: pool capacity and machine mutual-exclusion soundness.
2. **Rolling-Horizon Breakdown Contingency Gate (Idea #07)**
   - `FaultEvent`, `avoidsFaultWindow`, `checkContingencySchedule`.
   - `faulted_machine_post_fault_rejected` & `contingency_schedule_preserves_windows`: mirrors `community_extensions/rolling_contingency_scheduler.py`.
3. **Day/Night Dual-Regime Queue Partition Gate (Idea #08)**
   - `DualRegimeSlot`, `checkDualRegimeSlot`.
   - `unverified_overnight_run_rejected` & `unsigned_or_late_frontier_rejected`: mirrors `community_extensions/night_shift_batcher.py`.

## Verification

```bash
cd lean
lake build
```
