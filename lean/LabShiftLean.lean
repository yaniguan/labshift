/-!
# LabShiftLean: Formal L5 Multi-Instrument Scheduling, Contingency Repair & Dual-Regime Gates

Self-contained Lean 4.36.0 formalization for `labshift` (L5 Executive Scheduling Layer).
Every temporal quantity is represented in exact integer minutes (`Nat`) so all release,
deadline, capacity, mutual-exclusion, biological `[min_wait, max_wait]` incubation windows,
mid-run fault rerouting guards, and day/night shift partitions are decidable with zero
floating-point drift.
-/
namespace LabShiftLean

/-- Structured violation emitted when a candidate schedule or shift slot breaks an L5 invariant. -/
structure RegimeViolation where
  rule   : String
  detail : String
  deriving DecidableEq, Repr

/-- Return the first failing check in an ordered list of `(holds, violation)` guards. -/
def firstViolation : List (Bool × RegimeViolation) → Option RegimeViolation
  | [] => none
  | (true, _) :: rest => firstViolation rest
  | (false, v) :: _ => some v

@[simp] theorem firstViolation_nil : firstViolation [] = none := rfl

@[simp] theorem firstViolation_cons (b : Bool) (v : RegimeViolation)
    (rest : List (Bool × RegimeViolation)) :
    firstViolation ((b, v) :: rest) = none ↔ b = true ∧ firstViolation rest = none := by
  cases b <;> simp [firstViolation]

theorem firstViolation_none_iff (checks : List (Bool × RegimeViolation)) :
    firstViolation checks = none ↔ ∀ p ∈ checks, p.1 = true := by
  induction checks with
  | nil => simp [firstViolation]
  | cons hd tl ih =>
    rcases hd with ⟨b, v⟩
    cases b with
    | false => simp [firstViolation]
    | true  => simp [firstViolation, ih]

/-! ## 1. Core Multi-Instrument Contended Schedule & Incubation Windows -/

/-- A scheduled operation assigned to `(poolId, machineIdx)` over `[startMin, startMin + durMin)`. -/
structure ScheduledOp where
  opId        : Nat
  poolId      : Nat
  machineIdx  : Nat
  poolCap     : Nat
  startMin    : Nat
  durMin      : Nat
  releaseMin  : Nat
  deadlineMin : Nat
  deriving DecidableEq, Repr

def ScheduledOp.endMin (o : ScheduledOp) : Nat := o.startMin + o.durMin

/-- Coupled precedence and biological `[minWaitMin, maxWaitMin]` window between consecutive steps. -/
structure WaitLink where
  predEndMin   : Nat
  succStartMin : Nat
  minWaitMin   : Nat
  maxWaitMin   : Nat
  deriving DecidableEq, Repr

def opBoundsOK (o : ScheduledOp) : Bool :=
  decide (0 < o.durMin ∧ o.machineIdx < o.poolCap ∧
          o.releaseMin ≤ o.startMin ∧ o.endMin ≤ o.deadlineMin)

def waitLinkOK (w : WaitLink) : Bool :=
  decide (w.predEndMin + w.minWaitMin ≤ w.succStartMin ∧
          w.succStartMin ≤ w.predEndMin + w.maxWaitMin)

def pairDisjointOK (a b : ScheduledOp) : Bool :=
  decide (a.opId = b.opId ∨ a.poolId ≠ b.poolId ∨ a.machineIdx ≠ b.machineIdx ∨
          a.endMin ≤ b.startMin ∨ b.endMin ≤ a.startMin)

def allPairsDisjoint (ops : List ScheduledOp) : Bool :=
  ops.all (fun a => ops.all (fun b => pairDisjointOK a b))

/-- Propositional specification satisfied by every valid L5 multi-instrument schedule. -/
structure ScheduleOK (ops : List ScheduledOp) (links : List WaitLink) : Prop where
  boundsHold   : ∀ o ∈ ops, opBoundsOK o = true
  windowsHold  : ∀ w ∈ links, waitLinkOK w = true
  noCollisions : allPairsDisjoint ops = true

/-- Executable schedule verifier checking release/deadline/capacity, wait windows, and mutual exclusion. -/
def checkSchedule (ops : List ScheduledOp) (links : List WaitLink) : Option RegimeViolation :=
  firstViolation
    [ (ops.all opBoundsOK,
        { rule := "op_release_deadline_capacity",
          detail := "scheduled operation violates positive duration, pool capacity, release time, or deadline" }),
      (links.all waitLinkOK,
        { rule := "inter_step_wait_window",
          detail := "inter-step delay violates [min_wait, max_wait] incubation window" }),
      (allPairsDisjoint ops,
        { rule := "machine_mutual_exclusion",
          detail := "two distinct operations overlap on the same instrument instance" }) ]

/-- Soundness and completeness of `checkSchedule` with respect to `ScheduleOK`. -/
theorem checkSchedule_ok_iff (ops : List ScheduledOp) (links : List WaitLink) :
    checkSchedule ops links = none ↔ ScheduleOK ops links := by
  unfold checkSchedule
  simp only [firstViolation_cons, firstViolation_nil, and_true, List.all_eq_true]
  constructor
  · rintro ⟨h1, h2, h3⟩; exact ⟨h1, h2, h3⟩
  · rintro ⟨h1, h2, h3⟩; exact ⟨h1, h2, h3⟩

/-- Hard boundary theorem: Any schedule with a missed `max_wait` incubation window is rejected. -/
theorem missed_incubation_window_rejected (ops : List ScheduledOp) (links : List WaitLink)
    (w : WaitLink) (hw : w ∈ links) (hover : w.predEndMin + w.maxWaitMin < w.succStartMin) :
    checkSchedule ops links ≠ none := by
  intro hok
  have hwin := ((checkSchedule_ok_iff ops links).mp hok).windowsHold w hw
  unfold waitLinkOK at hwin
  have hdec := of_decide_eq_true hwin
  omega

/-- Hard boundary theorem: Any schedule starting a downstream step before `min_wait` elapses is rejected. -/
theorem premature_incubation_window_rejected (ops : List ScheduledOp) (links : List WaitLink)
    (w : WaitLink) (hw : w ∈ links) (hearly : w.succStartMin < w.predEndMin + w.minWaitMin) :
    checkSchedule ops links ≠ none := by
  intro hok
  have hwin := ((checkSchedule_ok_iff ops links).mp hok).windowsHold w hw
  unfold waitLinkOK at hwin
  have hdec := of_decide_eq_true hwin
  omega

/-- Hard boundary theorem: Machine index exceeding `poolCap` is always rejected. -/
theorem over_capacity_machine_rejected (ops : List ScheduledOp) (links : List WaitLink)
    (o : ScheduledOp) (ho : o ∈ ops) (hcap : o.poolCap ≤ o.machineIdx) :
    checkSchedule ops links ≠ none := by
  intro hok
  have hb := ((checkSchedule_ok_iff ops links).mp hok).boundsHold o ho
  unfold opBoundsOK at hb
  have hdec := of_decide_eq_true hb
  omega

/-- Hard boundary theorem: Two distinct overlapping operations on the same `(poolId, machineIdx)` are rejected. -/
theorem overlapping_same_machine_rejected (ops : List ScheduledOp) (links : List WaitLink)
    (a b : ScheduledOp) (ha : a ∈ ops) (hb : b ∈ ops)
    (hne : a.opId ≠ b.opId) (hpool : a.poolId = b.poolId) (hmach : a.machineIdx = b.machineIdx)
    (hover1 : b.startMin < a.endMin) (hover2 : a.startMin < b.endMin) :
    checkSchedule ops links ≠ none := by
  intro hok
  have hdis := ((checkSchedule_ok_iff ops links).mp hok).noCollisions
  unfold allPairsDisjoint at hdis
  rw [List.all_eq_true] at hdis
  have haDis := hdis a ha
  rw [List.all_eq_true] at haDis
  have hab := haDis b hb
  unfold pairDisjointOK at hab
  rcases of_decide_eq_true hab with hid | hp | hm | hle1 | hle2
  · exact hne hid
  · exact hp hpool
  · exact hm hmach
  · omega
  · omega

/-! ## 2. Idea #07: Rolling-Horizon Instrument Breakdown Contingency Gate -/

/-- Mid-run hardware breakdown event disabling `(faultPoolId, faultMachineIdx)` over `[faultMin, repairMin)`. -/
structure FaultEvent where
  faultPoolId     : Nat
  faultMachineIdx : Nat
  faultMin        : Nat
  repairMin       : Nat
  deriving DecidableEq, Repr

/-- An operation avoids the fault window if it uses another pool/unit, finishes by `faultMin`, or starts at `repairMin`. -/
def avoidsFaultWindow (f : FaultEvent) (o : ScheduledOp) : Bool :=
  decide (o.poolId ≠ f.faultPoolId ∨ o.machineIdx ≠ f.faultMachineIdx ∨
          o.endMin ≤ f.faultMin ∨ f.repairMin ≤ o.startMin)

def checkContingencySchedule (ops : List ScheduledOp) (links : List WaitLink)
    (f : FaultEvent) : Option RegimeViolation :=
  match checkSchedule ops links with
  | some v => some v
  | none =>
    firstViolation
      [ (ops.all (avoidsFaultWindow f),
          { rule := "faulted_machine_quarantine",
            detail := "repaired schedule assigns work to a faulted instrument inside [faultMin, repairMin)" }) ]

/-- Any operation overlapping `[faultMin, repairMin)` on the faulted unit is blocked by `checkContingencySchedule`. -/
theorem faulted_machine_post_fault_rejected (ops : List ScheduledOp) (links : List WaitLink)
    (f : FaultEvent) (o : ScheduledOp) (ho : o ∈ ops)
    (hpool : o.poolId = f.faultPoolId) (hmach : o.machineIdx = f.faultMachineIdx)
    (hafter : f.faultMin < o.endMin) (hbefore : o.startMin < f.repairMin) :
    checkContingencySchedule ops links f ≠ none := by
  intro hok
  unfold checkContingencySchedule at hok
  cases hbase : checkSchedule ops links with
  | some v => simp [hbase] at hok
  | none =>
    simp only [hbase, firstViolation_cons, firstViolation_nil, and_true, List.all_eq_true] at hok
    have havoid := hok o ho
    unfold avoidsFaultWindow at havoid
    rcases of_decide_eq_true havoid with hp | hm | he | hs
    · exact hp hpool
    · exact hm hmach
    · omega
    · omega

/-- Every accepted rolling-contingency repair guarantees `ScheduleOK` (including zero missed `max_wait` windows). -/
theorem contingency_schedule_preserves_windows (ops : List ScheduledOp) (links : List WaitLink)
    (f : FaultEvent) (hok : checkContingencySchedule ops links f = none) :
    ScheduleOK ops links ∧ ∀ o ∈ ops, avoidsFaultWindow f o = true := by
  unfold checkContingencySchedule at hok
  cases hbase : checkSchedule ops links with
  | some v => simp [hbase] at hok
  | none =>
    simp only [hbase, firstViolation_cons, firstViolation_nil, and_true, List.all_eq_true] at hok
    exact ⟨(checkSchedule_ok_iff ops links).mp hbase, hok⟩

/-! ## 3. Idea #08: Day/Night Dual-Regime Queue Partition Gate -/

/-- A scheduled protocol slot inside a 1440-minute diurnal cycle (`[0, 480)` attended day, `[480, 1440)` overnight). -/
structure DualRegimeSlot where
  startMinInDay       : Nat
  durMin              : Nat
  isFrontierException : Bool
  humanCosigned       : Bool
  leanCertified       : Bool
  deriving DecidableEq, Repr

def DualRegimeSlot.endMinInDay (s : DualRegimeSlot) : Nat := s.startMinInDay + s.durMin

/-- Daytime `[0, 480)` permits cosigned frontier runs; overnight `[480, 1440)` strictly requires Lean certification. -/
def checkDualRegimeSlot (s : DualRegimeSlot) : Option RegimeViolation :=
  firstViolation
    [ (decide (0 < s.durMin ∧ s.endMinInDay ≤ 1440),
        { rule := "diurnal_slot_bounds",
          detail := "protocol slot must have positive duration and fit within the 1440-minute day cycle" }),
      (decide (!s.isFrontierException ∨ (s.endMinInDay ≤ 480 ∧ s.humanCosigned = true)),
        { rule := "frontier_daytime_cosign_gate",
          detail := "frontier protocol must finish inside [0, 480) attended daytime window with human co-signature" }),
      (decide (s.isFrontierException ∨ s.leanCertified = true),
        { rule := "autonomous_requires_lean_cert",
          detail := "non-frontier autonomous or overnight run requires a verified Lean proof certificate" }) ]

/-- Any run extending into the `[480, 1440)` overnight shift (`480 < endMinInDay`) without `leanCertified = true` is rejected. -/
theorem unverified_overnight_run_rejected (s : DualRegimeSlot)
    (hnight : 480 < s.endMinInDay) (hunverified : s.leanCertified = false) :
    checkDualRegimeSlot s ≠ none := by
  intro hok
  unfold checkDualRegimeSlot at hok
  simp only [firstViolation_cons, firstViolation_nil, and_true, decide_eq_true_eq] at hok
  rcases hok with ⟨_, hfront, hcert⟩
  cases hf : s.isFrontierException with
  | true =>
    simp [hf] at hfront
    omega
  | false =>
    simp [hf, hunverified] at hcert

/-- Any frontier exception lacking human co-signature or spilling past minute 480 is rejected. -/
theorem unsigned_or_late_frontier_rejected (s : DualRegimeSlot)
    (hf : s.isFrontierException = true) (hbad : s.humanCosigned = false ∨ 480 < s.endMinInDay) :
    checkDualRegimeSlot s ≠ none := by
  intro hok
  unfold checkDualRegimeSlot at hok
  simp only [firstViolation_cons, firstViolation_nil, and_true, decide_eq_true_eq] at hok
  rcases hok with ⟨_, hfront, _⟩
  simp [hf] at hfront
  rcases hbad with hsig | hlate
  · rw [hsig] at hfront; simp at hfront
  · omega

/-! ## Compile-Time `#guard` Verification Witnesses -/

private def sampleOp1 : ScheduledOp :=
  { opId := 1, poolId := 0, machineIdx := 0, poolCap := 2,
    startMin := 0, durMin := 15, releaseMin := 0, deadlineMin := 60 }

private def sampleOp2 : ScheduledOp :=
  { opId := 2, poolId := 1, machineIdx := 0, poolCap := 2,
    startMin := 20, durMin := 10, releaseMin := 0, deadlineMin := 60 }

#guard (checkSchedule [sampleOp1, sampleOp2]
         [{ predEndMin := 15, succStartMin := 20, minWaitMin := 2, maxWaitMin := 8 }]).isNone == true

#guard (checkSchedule [sampleOp1, sampleOp2]
         [{ predEndMin := 15, succStartMin := 20, minWaitMin := 2, maxWaitMin := 3 }]).isSome == true

#guard (checkSchedule
         [sampleOp1, { sampleOp1 with opId := 3, startMin := 10 }] []).isSome == true

#guard (checkContingencySchedule
         [sampleOp1, { sampleOp2 with machineIdx := 1 }]
         [{ predEndMin := 15, succStartMin := 20, minWaitMin := 2, maxWaitMin := 8 }]
         { faultPoolId := 1, faultMachineIdx := 0, faultMin := 12, repairMin := 120 }).isNone == true

#guard (checkContingencySchedule
         [sampleOp1, sampleOp2]
         [{ predEndMin := 15, succStartMin := 20, minWaitMin := 2, maxWaitMin := 8 }]
         { faultPoolId := 1, faultMachineIdx := 0, faultMin := 12, repairMin := 120 }).isSome == true

#guard (checkDualRegimeSlot
         { startMinInDay := 500, durMin := 180, isFrontierException := false,
           humanCosigned := false, leanCertified := true }).isNone == true

#guard (checkDualRegimeSlot
         { startMinInDay := 500, durMin := 180, isFrontierException := false,
           humanCosigned := false, leanCertified := false }).isSome == true

#guard (checkDualRegimeSlot
         { startMinInDay := 420, durMin := 90, isFrontierException := true,
           humanCosigned := true, leanCertified := false }).isSome == true

end LabShiftLean
