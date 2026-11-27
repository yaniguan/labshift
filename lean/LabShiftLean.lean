/-!
# LabShiftLean: Formal L5 Multi-Instrument Scheduling, Sweep-Line Equivalence & Dual-Regime Gates

Self-contained Lean 4.36.0 formalization for `labshift` (L5 Executive Scheduling Layer).
Every temporal quantity is represented in exact integer minutes (`Nat`) so all release,
deadline, capacity, `opId` uniqueness, mutual-exclusion, departure-first sweep-line pool
occupancy, greedy interval-lane assignment, biological `[min_wait, max_wait]` incubation
windows, mid-run fault rerouting guards, and day/night shift partitions are decidable with
zero floating-point drift.
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

/-! ## 1. Core Multi-Instrument Contended Schedule, `opId` Uniqueness & Incubation Windows -/

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

/-- Executable `opId` duplicate-freedom guard closing the `a.opId = b.opId` self-skip gap. -/
def hasUniqueOpIds : List ScheduledOp → Bool
  | [] => true
  | o :: rest => rest.all (fun x => decide (o.opId ≠ x.opId)) && hasUniqueOpIds rest

theorem hasUniqueOpIds_iff_pairwise (ops : List ScheduledOp) :
    hasUniqueOpIds ops = true ↔ List.Pairwise (· ≠ ·) (ops.map ScheduledOp.opId) := by
  induction ops with
  | nil => simp [hasUniqueOpIds]
  | cons o rest ih =>
    simp only [hasUniqueOpIds, Bool.and_eq_true, List.all_eq_true, decide_eq_true_eq,
      List.map_cons, List.pairwise_cons, List.mem_map]
    constructor
    · rintro ⟨hall, hrest⟩
      refine ⟨?_, ih.mp hrest⟩
      intro y hy
      rcases hy with ⟨x, hx, rfl⟩
      exact hall x hx
    · rintro ⟨hpair, hrest⟩
      refine ⟨?_, ih.mpr hrest⟩
      intro x hx
      exact hpair x.opId ⟨x, hx, rfl⟩

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
  uniqueOpIds  : hasUniqueOpIds ops = true
  boundsHold   : ∀ o ∈ ops, opBoundsOK o = true
  windowsHold  : ∀ w ∈ links, waitLinkOK w = true
  noCollisions : allPairsDisjoint ops = true

/-- Executable schedule verifier checking unique `opId`s, bounds, wait windows, and mutual exclusion. -/
def checkSchedule (ops : List ScheduledOp) (links : List WaitLink) : Option RegimeViolation :=
  firstViolation
    [ (hasUniqueOpIds ops,
        { rule := "duplicate_op_id",
          detail := "every scheduled operation must carry a unique opId" }),
      (ops.all opBoundsOK,
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
  · rintro ⟨h0, h1, h2, h3⟩; exact ⟨h0, h1, h2, h3⟩
  · rintro ⟨h0, h1, h2, h3⟩; exact ⟨h0, h1, h2, h3⟩

/-- Hard boundary theorem: Duplicate `opId` entries are always rejected by `checkSchedule`. -/
theorem duplicate_opId_rejected (pre mid suf : List ScheduledOp) (a b : ScheduledOp)
    (links : List WaitLink) (hdup : a.opId = b.opId) :
    checkSchedule (pre ++ (a :: (mid ++ b :: suf))) links ≠ none := by
  intro hok
  have huniq := ((checkSchedule_ok_iff _ links).mp hok).uniqueOpIds
  have hpw := (hasUniqueOpIds_iff_pairwise _).mp huniq
  rw [List.map_append, List.pairwise_append] at hpw
  rcases hpw with ⟨_, hpw_tail, _⟩
  rw [List.map_cons, List.pairwise_cons] at hpw_tail
  rcases hpw_tail with ⟨hhead, _⟩
  have hb_mem : b.opId ∈ List.map ScheduledOp.opId (mid ++ b :: suf) := by
    rw [List.map_append, List.mem_append]
    exact Or.inr (List.Mem.head _)
  exact hhead b.opId hb_mem hdup

/-- Hard boundary theorem: Any schedule with a missed `max_wait` incubation window is rejected. -/
theorem missed_incubation_window_rejected (ops : List ScheduledOp) (links : List WaitLink)
    (w : WaitLink) (hw : w ∈ links) (hover : w.predEndMin + w.maxWaitMin < w.succStartMin) :
    checkSchedule ops links ≠ none := by
  intro hok
  have hwin := ((checkSchedule_ok_iff ops links).mp hok).windowsHold w hw
  unfold waitLinkOK at hwin
  have _ := of_decide_eq_true hwin
  omega

/-- Hard boundary theorem: Any schedule starting a downstream step before `min_wait` elapses is rejected. -/
theorem premature_incubation_window_rejected (ops : List ScheduledOp) (links : List WaitLink)
    (w : WaitLink) (hw : w ∈ links) (hearly : w.succStartMin < w.predEndMin + w.minWaitMin) :
    checkSchedule ops links ≠ none := by
  intro hok
  have hwin := ((checkSchedule_ok_iff ops links).mp hok).windowsHold w hw
  unfold waitLinkOK at hwin
  have _ := of_decide_eq_true hwin
  omega

/-- Hard boundary theorem: Machine index exceeding `poolCap` is always rejected. -/
theorem over_capacity_machine_rejected (ops : List ScheduledOp) (links : List WaitLink)
    (o : ScheduledOp) (ho : o ∈ ops) (hcap : o.poolCap ≤ o.machineIdx) :
    checkSchedule ops links ≠ none := by
  intro hok
  have hb := ((checkSchedule_ok_iff ops links).mp hok).boundsHold o ho
  unfold opBoundsOK at hb
  have _ := of_decide_eq_true hb
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

/-! ## 2. Discrete-Time Pool Occupancy & Departure-First Sweep-Line Equivalence -/

/-- Half-open interval membership `t ∈ [o.startMin, o.endMin)` on `poolId`. -/
def opActiveAt (poolId t : Nat) (o : ScheduledOp) : Bool :=
  decide (o.poolId = poolId ∧ o.startMin ≤ t ∧ t < o.endMin)

/-- Discrete-time concurrent occupancy on `poolId` at minute `t`. -/
def activeCount (poolId t : Nat) (ops : List ScheduledOp) : Nat :=
  (ops.filter (opActiveAt poolId t)).length

/-- Tight back-to-back handoff (`a.endMin ≤ b.startMin`, including `a.endMin = b.startMin`)
    never spikes discrete occupancy above `1` at any timestamp `t`. -/
theorem adjacent_handoff_activeCount_le_one (poolId t : Nat) (a b : ScheduledOp)
    (hhand : a.endMin ≤ b.startMin) :
    activeCount poolId t [a, b] ≤ 1 := by
  unfold activeCount opActiveAt
  by_cases ha : a.poolId = poolId ∧ a.startMin ≤ t ∧ t < a.endMin
  · have hb : ¬(b.poolId = poolId ∧ b.startMin ≤ t ∧ t < b.endMin) := by
      rcases ha with ⟨_, _, hta⟩
      intro ⟨_, htb, _⟩
      omega
    simp [ha, hb]
  · by_cases hb : b.poolId = poolId ∧ b.startMin ≤ t ∧ t < b.endMin <;> simp [ha, hb]

/-- Executable duplicate-free predicate on natural-number lists (for machine index pigeonhole). -/
def allDistinctNat : List Nat → Bool
  | [] => true
  | x :: xs => xs.all (fun y => decide (x ≠ y)) && allDistinctNat xs

private theorem allDistinctNat_filter (p : Nat → Bool) (xs : List Nat)
    (hd : allDistinctNat xs = true) :
    allDistinctNat (xs.filter p) = true := by
  induction xs with
  | nil => rfl
  | cons x rest ih =>
    simp only [allDistinctNat, Bool.and_eq_true, List.all_eq_true, decide_eq_true_eq] at hd
    rcases hd with ⟨hne, hrest⟩
    cases hp : p x with
    | false =>
      simp [List.filter, hp, ih hrest]
    | true =>
      simp only [List.filter, hp, allDistinctNat, Bool.and_eq_true, List.all_eq_true,
        decide_eq_true_eq, List.mem_filter]
      exact ⟨fun y hy => hne y hy.1, ih hrest⟩

private theorem distinct_length_le_filter_ne_succ (v : Nat) (xs : List Nat)
    (hd : allDistinctNat xs = true) :
    xs.length ≤ (xs.filter (fun x => decide (x ≠ v))).length + 1 := by
  induction xs with
  | nil => simp
  | cons x rest ih =>
    simp only [allDistinctNat, Bool.and_eq_true, List.all_eq_true, decide_eq_true_eq] at hd
    rcases hd with ⟨hne, hrest⟩
    by_cases hxv : x = v
    · subst hxv
      have hkeep : rest.filter (fun y => decide (y ≠ x)) = rest := by
        rw [List.filter_eq_self]
        intro y hy
        simp only [decide_eq_true_eq]
        exact Ne.symm (hne y hy)
      have hx_false : decide (x ≠ x) = false := by simp
      simp only [List.filter, hx_false, List.length_cons]
      rw [hkeep]
      omega
    · have hih := ih hrest
      simp only [List.filter, decide_eq_true hxv, List.length_cons]
      omega

/-- Finite pigeonhole principle: duplicate-free `Nat` values strictly below `cap` have length `≤ cap`. -/
theorem distinct_lt_length_le (cap : Nat) :
    ∀ xs : List Nat, allDistinctNat xs = true → (∀ x ∈ xs, x < cap) → xs.length ≤ cap := by
  induction cap with
  | zero =>
    intro xs _ hlt
    cases xs with
    | nil => simp
    | cons x _ =>
      have hx := hlt x (List.Mem.head _)
      omega
  | succ c ih =>
    intro xs hd hlt
    have hys_dist : allDistinctNat (xs.filter (fun x => decide (x ≠ c))) = true :=
      allDistinctNat_filter _ xs hd
    have hys_lt : ∀ y ∈ xs.filter (fun x => decide (x ≠ c)), y < c := by
      intro y hy
      simp only [List.mem_filter, decide_eq_true_eq] at hy
      have h1 := hlt y hy.1
      omega
    have hys_len := ih (xs.filter (fun x => decide (x ≠ c))) hys_dist hys_lt
    have hstep := distinct_length_le_filter_ne_succ c xs hd
    omega

private theorem hasUniqueOpIds_filter (p : ScheduledOp → Bool) (ops : List ScheduledOp)
    (hu : hasUniqueOpIds ops = true) :
    hasUniqueOpIds (ops.filter p) = true := by
  induction ops with
  | nil => rfl
  | cons o rest ih =>
    simp only [hasUniqueOpIds, Bool.and_eq_true, List.all_eq_true, decide_eq_true_eq] at hu
    rcases hu with ⟨hne, hrest⟩
    cases hp : p o with
    | false => simp [List.filter, hp, ih hrest]
    | true =>
      simp only [List.filter, hp, hasUniqueOpIds, Bool.and_eq_true, List.all_eq_true,
        decide_eq_true_eq, List.mem_filter]
      exact ⟨fun x hx => hne x hx.1, ih hrest⟩

private theorem allPairsDisjoint_sublist {sub ops : List ScheduledOp}
    (hsub : ∀ x ∈ sub, x ∈ ops) (hdis : allPairsDisjoint ops = true) :
    allPairsDisjoint sub = true := by
  unfold allPairsDisjoint at hdis ⊢
  rw [List.all_eq_true] at hdis ⊢
  intro a ha
  rw [List.all_eq_true]
  intro b hb
  have ha' := hdis a (hsub a ha)
  rw [List.all_eq_true] at ha'
  exact ha' b (hsub b hb)

private theorem active_machines_distinct (poolId t : Nat) (act : List ScheduledOp)
    (hu : hasUniqueOpIds act = true)
    (hdis : allPairsDisjoint act = true)
    (hact : ∀ o ∈ act, opActiveAt poolId t o = true) :
    allDistinctNat (act.map ScheduledOp.machineIdx) = true := by
  induction act with
  | nil => rfl
  | cons o rest ih =>
    simp only [hasUniqueOpIds, Bool.and_eq_true, List.all_eq_true, decide_eq_true_eq] at hu
    rcases hu with ⟨hne, hrest⟩
    have hdis_rest : allPairsDisjoint rest = true :=
      allPairsDisjoint_sublist (fun x hx => List.Mem.tail _ hx) hdis
    have hact_rest : ∀ x ∈ rest, opActiveAt poolId t x = true :=
      fun x hx => hact x (List.Mem.tail _ hx)
    simp only [List.map_cons, allDistinctNat, Bool.and_eq_true, List.all_eq_true,
      decide_eq_true_eq, List.mem_map]
    refine ⟨?_, ih hrest hdis_rest hact_rest⟩
    intro m hm
    rcases hm with ⟨b, hb, rfl⟩
    unfold allPairsDisjoint at hdis
    rw [List.all_eq_true] at hdis
    have ho_dis := hdis o (List.Mem.head _)
    rw [List.all_eq_true] at ho_dis
    have hob := ho_dis b (List.Mem.tail _ hb)
    have ho_act := of_decide_eq_true (hact o (List.Mem.head _))
    have hb_act := of_decide_eq_true (hact b (List.Mem.tail _ hb))
    have hne_id := hne b hb
    unfold pairDisjointOK at hob
    rcases of_decide_eq_true hob with hid | hp | hm_ne | hle1 | hle2
    · exact absurd hid hne_id
    · exact absurd (ho_act.1.trans hb_act.1.symm) hp
    · exact hm_ne
    · omega
    · omega

/-- Every `ScheduleOK` schedule obeys `activeCount poolId t ops ≤ cap` at every discrete minute `t`. -/
theorem scheduleOK_implies_activeCount_le_poolCap (ops : List ScheduledOp) (links : List WaitLink)
    (poolId cap : Nat) (hok : ScheduleOK ops links)
    (hcap : ∀ o ∈ ops, o.poolId = poolId → o.poolCap ≤ cap) (t : Nat) :
    activeCount poolId t ops ≤ cap := by
  let act := ops.filter (opActiveAt poolId t)
  have hu_act : hasUniqueOpIds act = true :=
    hasUniqueOpIds_filter _ ops hok.uniqueOpIds
  have hdis_act : allPairsDisjoint act = true :=
    allPairsDisjoint_sublist (fun x hx => (List.mem_filter.mp hx).1) hok.noCollisions
  have hact_mem : ∀ o ∈ act, opActiveAt poolId t o = true :=
    fun o ho => (List.mem_filter.mp ho).2
  have hdist := active_machines_distinct poolId t act hu_act hdis_act hact_mem
  have hlt : ∀ m ∈ act.map ScheduledOp.machineIdx, m < cap := by
    intro m hm
    rcases List.mem_map.mp hm with ⟨o, ho, rfl⟩
    have ho_ops := (List.mem_filter.mp ho).1
    have ho_act := of_decide_eq_true (List.mem_filter.mp ho).2
    have hb := of_decide_eq_true (hok.boundsHold o ho_ops)
    have hc := hcap o ho_ops ho_act.1
    omega
  have hlen := distinct_lt_length_le cap (act.map ScheduledOp.machineIdx) hdist hlt
  simp only [List.length_map] at hlen
  exact hlen

/-- Sweep-line event (`isArrival = false` is departure `-1`; `isArrival = true` is arrival `+1`). -/
structure SweepEvent where
  time      : Nat
  isArrival : Bool
  deriving DecidableEq, Repr

def SweepEvent.delta (e : SweepEvent) : Int :=
  if e.isArrival then 1 else -1

/-- Departure-first ordering matching `checker.py`: `(end, -1)` strictly precedes `(start, +1)` at equal `t`. -/
def eventLe (a b : SweepEvent) : Bool :=
  decide (a.time < b.time ∨ (a.time = b.time ∧ (a.isArrival = false ∨ b.isArrival = true)))

theorem eventLe_total (a b : SweepEvent) : eventLe a b = true ∨ eventLe b a = true := by
  unfold eventLe
  by_cases hlt : a.time < b.time
  · left; exact decide_eq_true (Or.inl hlt)
  · by_cases hgt : b.time < a.time
    · right; exact decide_eq_true (Or.inl hgt)
    · have heq : a.time = b.time := by omega
      cases ha : a.isArrival <;> cases hb : b.isArrival
      · left; exact decide_eq_true (Or.inr ⟨heq, Or.inl rfl⟩)
      · left; exact decide_eq_true (Or.inr ⟨heq, Or.inl rfl⟩)
      · right; exact decide_eq_true (Or.inr ⟨heq.symm, Or.inl rfl⟩)
      · left; exact decide_eq_true (Or.inr ⟨heq, Or.inr rfl⟩)

theorem eventLe_trans {a b c : SweepEvent}
    (hab : eventLe a b = true) (hbc : eventLe b c = true) : eventLe a c = true := by
  unfold eventLe at hab hbc ⊢
  have h1 := of_decide_eq_true hab
  have h2 := of_decide_eq_true hbc
  apply decide_eq_true
  rcases h1 with hlt1 | ⟨heq1, harr1⟩
  · rcases h2 with hlt2 | ⟨heq2, _⟩
    · exact Or.inl (by omega)
    · exact Or.inl (by omega)
  · rcases h2 with hlt2 | ⟨heq2, harr2⟩
    · exact Or.inl (by omega)
    · refine Or.inr ⟨by omega, ?_⟩
      rcases harr1 with ha0 | hb1
      · exact Or.inl ha0
      · rcases harr2 with hb0 | hc1
        · rw [hb1] at hb0; cases hb0
        · exact Or.inr hc1

def insertEvent (e : SweepEvent) : List SweepEvent → List SweepEvent
  | [] => [e]
  | x :: xs => if eventLe e x then e :: x :: xs else x :: insertEvent e xs

def sortEvents : List SweepEvent → List SweepEvent
  | [] => []
  | e :: es => insertEvent e (sortEvents es)

def rawPoolEvents (poolId : Nat) : List ScheduledOp → List SweepEvent
  | [] => []
  | o :: rest =>
    if o.poolId == poolId && decide (0 < o.durMin) then
      { time := o.startMin, isArrival := true } ::
      { time := o.endMin, isArrival := false } ::
      rawPoolEvents poolId rest
    else
      rawPoolEvents poolId rest

def sweepRunOK (cap : Nat) (acc : Int) : List SweepEvent → Bool
  | [] => decide (acc ≤ (cap : Int))
  | e :: rest => decide (acc ≤ (cap : Int)) && sweepRunOK cap (acc + e.delta) rest

/-- Executable departure-first sweep-line pool capacity checker. -/
def sweepLinePoolOK (poolId cap : Nat) (ops : List ScheduledOp) : Bool :=
  sweepRunOK cap 0 (sortEvents (rawPoolEvents poolId ops))

def sumDeltas : List SweepEvent → Int
  | [] => 0
  | e :: es => e.delta + sumDeltas es

private theorem mem_insertEvent (x e : SweepEvent) (es : List SweepEvent) :
    x ∈ insertEvent e es ↔ x = e ∨ x ∈ es := by
  induction es with
  | nil => simp [insertEvent]
  | cons y ys ih =>
    unfold insertEvent
    split <;> simp [ih, or_left_comm]

private theorem insertEvent_pairwise (e : SweepEvent) (es : List SweepEvent)
    (hpw : List.Pairwise (fun a b => eventLe a b = true) es) :
    List.Pairwise (fun a b => eventLe a b = true) (insertEvent e es) := by
  induction es with
  | nil => simp [insertEvent]
  | cons y ys ih =>
    rw [List.pairwise_cons] at hpw
    rcases hpw with ⟨hy_all, hys_pw⟩
    unfold insertEvent
    split
    · next hey =>
      rw [List.pairwise_cons]
      refine ⟨?_, List.pairwise_cons.mpr ⟨hy_all, hys_pw⟩⟩
      intro z hz
      rcases List.mem_cons.mp hz with rfl | hzy
      · exact hey
      · exact eventLe_trans hey (hy_all z hzy)
    · next hnot =>
      have hye : eventLe y e = true := by
        rcases eventLe_total e y with h1 | h2
        · exact absurd h1 hnot
        · exact h2
      rw [List.pairwise_cons]
      refine ⟨?_, ih hys_pw⟩
      intro z hz
      rcases (mem_insertEvent z e ys).mp hz with rfl | hzy
      · exact hye
      · exact hy_all z hzy

private theorem sortEvents_pairwise (es : List SweepEvent) :
    List.Pairwise (fun a b => eventLe a b = true) (sortEvents es) := by
  induction es with
  | nil => simp [sortEvents]
  | cons e rest ih => exact insertEvent_pairwise e (sortEvents rest) ih

private theorem insertEvent_filter_sum (p : SweepEvent → Bool) (e : SweepEvent)
    (es : List SweepEvent) :
    sumDeltas ((insertEvent e es).filter p) =
      (if p e then e.delta else 0) + sumDeltas (es.filter p) := by
  induction es with
  | nil =>
    cases hp : p e <;> simp [insertEvent, List.filter, hp, sumDeltas]
  | cons y ys ih =>
    unfold insertEvent
    split
    · cases hp : p e <;> cases hpy : p y <;> simp [List.filter, hp, hpy, sumDeltas] <;> omega
    · cases hp : p e <;> cases hpy : p y <;> simp [List.filter, hp, hpy, sumDeltas, ih] <;> omega

private theorem sortEvents_filter_sum (p : SweepEvent → Bool) (es : List SweepEvent) :
    sumDeltas ((sortEvents es).filter p) = sumDeltas (es.filter p) := by
  induction es with
  | nil => rfl
  | cons e rest ih =>
    simp only [sortEvents, insertEvent_filter_sum, ih]
    cases hp : p e <;> simp [List.filter, hp, sumDeltas]

private theorem rawPoolEvents_filter_time_le_sum (poolId t : Nat) (ops : List ScheduledOp) :
    sumDeltas ((rawPoolEvents poolId ops).filter (fun e => decide (e.time ≤ t))) =
      (activeCount poolId t ops : Int) := by
  induction ops with
  | nil => rfl
  | cons o rest ih =>
    unfold activeCount at ih
    unfold rawPoolEvents activeCount
    by_cases hvalid : o.poolId = poolId ∧ 0 < o.durMin
    · rcases hvalid with ⟨hp, hdur⟩
      have hv_bool : (o.poolId == poolId && decide (0 < o.durMin)) = true := by
        simp [hp, hdur]
      simp only [hv_bool, ↓reduceIte]
      unfold ScheduledOp.endMin at *
      by_cases hs : o.startMin ≤ t
      · by_cases he : o.startMin + o.durMin ≤ t
        · have hact : opActiveAt poolId t o = false := by
            unfold opActiveAt ScheduledOp.endMin
            apply decide_eq_false
            intro ⟨_, _, hlt⟩
            omega
          simp only [List.filter, decide_eq_true hs, decide_eq_true he, hact,
            sumDeltas, SweepEvent.delta, ↓reduceIte, Bool.false_eq_true, ih]
          omega
        · have hact : opActiveAt poolId t o = true := by
            unfold opActiveAt ScheduledOp.endMin
            apply decide_eq_true
            exact ⟨hp, hs, by omega⟩
          simp only [List.filter, decide_eq_true hs, decide_eq_false he, hact,
            sumDeltas, SweepEvent.delta, ↓reduceIte, List.length_cons, ih]
          omega
      · have he : ¬(o.startMin + o.durMin ≤ t) := by omega
        have hact : opActiveAt poolId t o = false := by
          unfold opActiveAt
          apply decide_eq_false
          intro ⟨_, h1, _⟩
          exact hs h1
        simp only [List.filter, decide_eq_false hs, decide_eq_false he, hact, ih]
    · have hv_bool : (o.poolId == poolId && decide (0 < o.durMin)) = false := by
        cases h1 : (o.poolId == poolId) with
        | false => rfl
        | true =>
          simp only [beq_iff_eq] at h1
          simp only [Bool.true_and, decide_eq_false_iff_not]
          intro hdur
          exact hvalid ⟨h1, hdur⟩
      simp only [hv_bool, Bool.false_eq_true, ↓reduceIte, List.filter]
      have hact : opActiveAt poolId t o = false := by
        unfold opActiveAt ScheduledOp.endMin
        apply decide_eq_false
        intro ⟨hp, _, hlt⟩
        apply hvalid
        exact ⟨hp, by omega⟩
      simp only [hact, ih]

private theorem sweepRunOK_iff_takes (cap : Nat) (es : List SweepEvent) (acc : Int) :
    sweepRunOK cap acc es = true ↔
      ∀ k, k ≤ es.length → acc + sumDeltas (es.take k) ≤ (cap : Int) := by
  induction es generalizing acc with
  | nil =>
    simp only [sweepRunOK, decide_eq_true_eq, List.length_nil]
    constructor
    · intro h k hk
      have hk0 : k = 0 := by omega
      subst hk0
      simp [sumDeltas, h]
    · intro h
      have h0 := h 0 (Nat.le_refl 0)
      simpa [sumDeltas] using h0
  | cons e rest ih =>
    simp only [sweepRunOK, Bool.and_eq_true, decide_eq_true_eq, ih, List.length_cons]
    constructor
    · rintro ⟨h0, hrest⟩ k hk
      cases k with
      | zero => simpa [sumDeltas] using h0
      | succ k' =>
        have hk' : k' ≤ rest.length := by omega
        have hr := hrest k' hk'
        simp only [List.take_succ_cons, sumDeltas]
        omega
    · intro hall
      refine ⟨?_, ?_⟩
      · simpa [sumDeltas] using hall 0 (Nat.zero_le _)
      · intro k' hk'
        have hr := hall (k' + 1) (by omega)
        simp only [List.take_succ_cons, sumDeltas] at hr
        omega

private theorem eventLe_time_le {a b : SweepEvent} (h : eventLe a b = true) :
    a.time ≤ b.time := by
  rcases of_decide_eq_true h with hlt | ⟨heq, _⟩ <;> omega

private theorem sorted_filter_time_le_eq_take (t : Nat) (es : List SweepEvent)
    (hpw : List.Pairwise (fun a b => eventLe a b = true) es) :
    es.filter (fun x => decide (x.time ≤ t)) =
      es.take ((es.filter (fun x => decide (x.time ≤ t))).length) := by
  induction es with
  | nil => rfl
  | cons e rest ih =>
    rw [List.pairwise_cons] at hpw
    rcases hpw with ⟨he_all, hrest_pw⟩
    by_cases het : e.time ≤ t
    · simp only [List.filter, decide_eq_true het, List.length_cons, List.take_succ_cons]
      exact congrArg (e :: ·) (ih hrest_pw)
    · have hnone : rest.filter (fun x => decide (x.time ≤ t)) = [] := by
        rw [List.filter_eq_nil_iff]
        intro x hx
        simp only [decide_eq_true_eq]
        have hle := eventLe_time_le (he_all x hx)
        omega
      simp [List.filter, decide_eq_false het, hnone]

private theorem sorted_arrival_head_filter_nonneg (t : Nat) (rest : List SweepEvent)
    (hall : ∀ y ∈ rest, eventLe { time := t, isArrival := true } y = true) :
    0 ≤ sumDeltas (rest.filter (fun y => decide (y.time ≤ t))) := by
  induction rest with
  | nil => simp [sumDeltas]
  | cons y ys ih =>
    have hy := hall y (List.Mem.head _)
    have hys : ∀ z ∈ ys, eventLe { time := t, isArrival := true } z = true :=
      fun z hz => hall z (List.Mem.tail _ hz)
    by_cases hyt : y.time ≤ t
    · have hy_arr : y.isArrival = true := by
        have hy_dec := of_decide_eq_true hy
        dsimp only at hy_dec
        rcases hy_dec with hlt | ⟨_, h2⟩
        · omega
        · rcases h2 with hbad | hok
          · cases hbad
          · exact hok
      have hih := ih hys
      simp only [List.filter, decide_eq_true hyt, sumDeltas, SweepEvent.delta, hy_arr, ↓reduceIte]
      omega
    · simp only [List.filter, decide_eq_false hyt]
      exact ih hys

private theorem sorted_take_le_filter_at_some_event (es : List SweepEvent)
    (hpw : List.Pairwise (fun a b => eventLe a b = true) es) (k : Nat) (hk : k ≤ es.length) :
    sumDeltas (es.take k) ≤ 0 ∨
      ∃ x ∈ es, sumDeltas (es.take k) ≤ sumDeltas (es.filter (fun y => decide (y.time ≤ x.time))) := by
  induction es generalizing k with
  | nil => left; simp [sumDeltas]
  | cons e rest ih =>
    cases k with
    | zero => left; simp [sumDeltas]
    | succ k' =>
      rw [List.pairwise_cons] at hpw
      rcases hpw with ⟨he_all, hrest_pw⟩
      have hk' : k' ≤ rest.length := by
        simp only [List.length_cons] at hk
        omega
      rcases ih hrest_pw k' hk' with hneg | ⟨x, hx_mem, hx_le⟩
      · cases harr : e.isArrival with
        | false =>
          left
          simp only [List.take_succ_cons, sumDeltas, SweepEvent.delta, harr, Bool.false_eq_true, ↓reduceIte]
          omega
        | true =>
          right
          refine ⟨e, List.Mem.head _, ?_⟩
          have he_self : e = { time := e.time, isArrival := true } := by
            cases e; simp_all
          have hall' : ∀ y ∈ rest, eventLe { time := e.time, isArrival := true } y = true := by
            intro y hy
            rw [← he_self]
            exact he_all y hy
          have hnonneg := sorted_arrival_head_filter_nonneg e.time rest hall'
          simp only [List.take_succ_cons, List.filter, decide_eq_true (Nat.le_refl e.time),
            sumDeltas, SweepEvent.delta, harr, ↓reduceIte]
          omega
      · right
        refine ⟨x, List.Mem.tail _ hx_mem, ?_⟩
        have hex : e.time ≤ x.time := eventLe_time_le (he_all x hx_mem)
        simp only [List.take_succ_cons, List.filter, decide_eq_true hex, sumDeltas]
        omega

/-- Exact equivalence between the departure-first sweep-line checker `sweepLinePoolOK`
    and discrete-time half-open occupancy `∀ t, activeCount poolId t ops ≤ cap`. -/
theorem sweepLinePoolOK_iff_activeCount_le (poolId cap : Nat) (ops : List ScheduledOp) :
    sweepLinePoolOK poolId cap ops = true ↔ ∀ t : Nat, activeCount poolId t ops ≤ cap := by
  unfold sweepLinePoolOK
  have hpw : List.Pairwise (fun a b => eventLe a b = true) (sortEvents (rawPoolEvents poolId ops)) :=
    sortEvents_pairwise (rawPoolEvents poolId ops)
  rw [sweepRunOK_iff_takes cap (sortEvents (rawPoolEvents poolId ops)) 0]
  constructor
  · intro htakes t
    have hlen : ((sortEvents (rawPoolEvents poolId ops)).filter (fun x => decide (x.time ≤ t))).length ≤
        (sortEvents (rawPoolEvents poolId ops)).length :=
      List.length_filter_le _ _
    have hbound := htakes _ hlen
    have htake_eq := sorted_filter_time_le_eq_take t (sortEvents (rawPoolEvents poolId ops)) hpw
    rw [← htake_eq] at hbound
    have hsum1 := sortEvents_filter_sum (fun x => decide (x.time ≤ t)) (rawPoolEvents poolId ops)
    have hsum2 := rawPoolEvents_filter_time_le_sum poolId t ops
    omega
  · intro hall k hk
    rcases sorted_take_le_filter_at_some_event (sortEvents (rawPoolEvents poolId ops)) hpw k hk with
      hle0 | ⟨x, _, hx_le⟩
    · omega
    · have hsum1 := sortEvents_filter_sum (fun y => decide (y.time ≤ x.time)) (rawPoolEvents poolId ops)
      have hsum2 := rawPoolEvents_filter_time_le_sum poolId x.time ops
      have hact := hall x.time
      omega

/-! ## 3. Verified Greedy Pool-to-Lane Coloring Bridge (`PoolOp` -> `ScheduledOp`) -/

/-- `machineIdx`-free pool-scheduled operation mirroring `labshift.models.ScheduledOp`. -/
structure PoolOp where
  opId        : Nat
  poolId      : Nat
  poolCap     : Nat
  startMin    : Nat
  durMin      : Nat
  releaseMin  : Nat
  deadlineMin : Nat
  deriving DecidableEq, Repr

def PoolOp.endMin (p : PoolOp) : Nat := p.startMin + p.durMin

def poolOpBoundsOK (p : PoolOp) : Bool :=
  decide (0 < p.durMin ∧ 0 < p.poolCap ∧ p.releaseMin ≤ p.startMin ∧ p.endMin ≤ p.deadlineMin)

def hasUniquePoolOpIds : List PoolOp → Bool
  | [] => true
  | p :: rest => rest.all (fun q => decide (p.opId ≠ q.opId)) && hasUniquePoolOpIds rest

def laneFreeFor (poolId startMin endMin lane : Nat) (placed : List ScheduledOp) : Bool :=
  placed.all (fun o =>
    decide (o.poolId ≠ poolId ∨ o.machineIdx ≠ lane ∨ o.endMin ≤ startMin ∨ endMin ≤ o.startMin))

def findFreeLaneAux (poolId startMin endMin cap : Nat) (placed : List ScheduledOp) :
    Nat → Option Nat
  | 0 => none
  | rem + 1 =>
    if laneFreeFor poolId startMin endMin (cap - (rem + 1)) placed then
      some (cap - (rem + 1))
    else
      findFreeLaneAux poolId startMin endMin cap placed rem

def findFreeLane (p : PoolOp) (placed : List ScheduledOp) : Option Nat :=
  findFreeLaneAux p.poolId p.startMin p.endMin p.poolCap placed p.poolCap

def toScheduledOp (p : PoolOp) (lane : Nat) : ScheduledOp :=
  { opId := p.opId
    poolId := p.poolId
    machineIdx := lane
    poolCap := p.poolCap
    startMin := p.startMin
    durMin := p.durMin
    releaseMin := p.releaseMin
    deadlineMin := p.deadlineMin }

def assignPoolLanesAux (placed : List ScheduledOp) : List PoolOp → Option (List ScheduledOp)
  | [] => some placed.reverse
  | p :: rest =>
    match findFreeLane p placed with
    | none => none
    | some lane => assignPoolLanesAux (toScheduledOp p lane :: placed) rest

/-- Greedy interval-lane coloring assigning `machineIdx < poolCap` to `PoolOp`s. -/
def assignPoolLanes (pops : List PoolOp) : Option (List ScheduledOp) :=
  assignPoolLanesAux [] pops

private theorem findFreeLaneAux_sound (poolId startMin endMin cap : Nat)
    (placed : List ScheduledOp) (rem lane : Nat) (hrem : rem ≤ cap)
    (hres : findFreeLaneAux poolId startMin endMin cap placed rem = some lane) :
    lane < cap ∧ laneFreeFor poolId startMin endMin lane placed = true := by
  induction rem with
  | zero => simp [findFreeLaneAux] at hres
  | succ r ih =>
    unfold findFreeLaneAux at hres
    split at hres
    · next hfree =>
      simp only [Option.some.injEq] at hres
      subst hres
      exact ⟨by omega, hfree⟩
    · exact ih (by omega) hres

private theorem pairDisjointOK_symm (a b : ScheduledOp) :
    pairDisjointOK a b = true → pairDisjointOK b a = true := by
  intro h
  unfold pairDisjointOK at h ⊢
  apply decide_eq_true
  rcases of_decide_eq_true h with hid | hp | hm | h1 | h2
  · exact Or.inl hid.symm
  · exact Or.inr (Or.inl hp.symm)
  · exact Or.inr (Or.inr (Or.inl hm.symm))
  · exact Or.inr (Or.inr (Or.inr (Or.inr h1)))
  · exact Or.inr (Or.inr (Or.inr (Or.inl h2)))

private theorem allPairsDisjoint_cons (o : ScheduledOp) (placed : List ScheduledOp)
    (hdis : allPairsDisjoint placed = true)
    (hfree : laneFreeFor o.poolId o.startMin o.endMin o.machineIdx placed = true) :
    allPairsDisjoint (o :: placed) = true := by
  unfold allPairsDisjoint at hdis ⊢
  rw [List.all_eq_true] at hdis ⊢
  unfold laneFreeFor at hfree
  rw [List.all_eq_true] at hfree
  have hhead : ∀ x ∈ placed, pairDisjointOK o x = true := by
    intro x hx
    have hfx := of_decide_eq_true (hfree x hx)
    unfold pairDisjointOK
    apply decide_eq_true
    rcases hfx with hp | hm | h1 | h2
    · exact Or.inr (Or.inl hp.symm)
    · exact Or.inr (Or.inr (Or.inl hm.symm))
    · exact Or.inr (Or.inr (Or.inr (Or.inr h1)))
    · exact Or.inr (Or.inr (Or.inr (Or.inl h2)))
  intro a ha
  rw [List.all_eq_true]
  intro b hb
  rcases List.mem_cons.mp ha with rfl | ha_in
  · rcases List.mem_cons.mp hb with rfl | hb_in
    · unfold pairDisjointOK; exact decide_eq_true (Or.inl rfl)
    · exact hhead b hb_in
  · rcases List.mem_cons.mp hb with rfl | hb_in
    · exact pairDisjointOK_symm b a (hhead a ha_in)
    · have ha' := hdis a ha_in
      rw [List.all_eq_true] at ha'
      exact ha' b hb_in

private theorem hasUniqueOpIds_reverse (ops : List ScheduledOp)
    (hu : hasUniqueOpIds ops = true) :
    hasUniqueOpIds ops.reverse = true := by
  rw [hasUniqueOpIds_iff_pairwise] at hu ⊢
  rw [List.map_reverse, List.pairwise_reverse]
  exact List.Pairwise.imp (fun h => Ne.symm h) hu

private theorem allPairsDisjoint_reverse (ops : List ScheduledOp)
    (hdis : allPairsDisjoint ops = true) :
    allPairsDisjoint ops.reverse = true :=
  allPairsDisjoint_sublist (fun _ hx => List.mem_reverse.mp hx) hdis

private theorem assignPoolLanesAux_sound (pops : List PoolOp) (placed ops : List ScheduledOp)
    (hp_bounds : ∀ p ∈ pops, poolOpBoundsOK p = true)
    (hp_uniq : hasUniquePoolOpIds pops = true)
    (hcross : ∀ p ∈ pops, ∀ o ∈ placed, p.opId ≠ o.opId)
    (hpl_bounds : ∀ o ∈ placed, opBoundsOK o = true)
    (hpl_uniq : hasUniqueOpIds placed = true)
    (hpl_dis : allPairsDisjoint placed = true)
    (hassign : assignPoolLanesAux placed pops = some ops) :
    hasUniqueOpIds ops = true ∧ (∀ o ∈ ops, opBoundsOK o = true) ∧ allPairsDisjoint ops = true := by
  induction pops generalizing placed with
  | nil =>
    simp only [assignPoolLanesAux, Option.some.injEq] at hassign
    subst hassign
    refine ⟨hasUniqueOpIds_reverse placed hpl_uniq, ?_, allPairsDisjoint_reverse placed hpl_dis⟩
    intro o ho
    exact hpl_bounds o (List.mem_reverse.mp ho)
  | cons p rest ih =>
    unfold assignPoolLanesAux at hassign
    cases hlane : findFreeLane p placed with
    | none => simp [hlane] at hassign
    | some lane =>
      simp only [hlane] at hassign
      have ⟨hlane_lt, hlane_free⟩ :=
        findFreeLaneAux_sound p.poolId p.startMin p.endMin p.poolCap placed p.poolCap lane
          (Nat.le_refl _) hlane
      have hpb := of_decide_eq_true (hp_bounds p (List.Mem.head _))
      simp only [hasUniquePoolOpIds, Bool.and_eq_true, List.all_eq_true, decide_eq_true_eq] at hp_uniq
      rcases hp_uniq with ⟨hp_ne_rest, hrest_uniq⟩
      let o_new := toScheduledOp p lane
      have ho_bounds : opBoundsOK o_new = true := by
        unfold opBoundsOK o_new toScheduledOp ScheduledOp.endMin PoolOp.endMin at *
        apply decide_eq_true
        exact ⟨hpb.1, hlane_lt, hpb.2.2.1, hpb.2.2.2⟩
      have hnew_uniq : hasUniqueOpIds (o_new :: placed) = true := by
        simp only [hasUniqueOpIds, Bool.and_eq_true, List.all_eq_true, decide_eq_true_eq]
        exact ⟨fun x hx => hcross p (List.Mem.head _) x hx, hpl_uniq⟩
      have hnew_dis : allPairsDisjoint (o_new :: placed) = true :=
        allPairsDisjoint_cons o_new placed hpl_dis hlane_free
      apply ih (o_new :: placed)
      · exact fun q hq => hp_bounds q (List.Mem.tail _ hq)
      · exact hrest_uniq
      · intro q hq x hx
        rcases List.mem_cons.mp hx with rfl | hx_pl
        · exact Ne.symm (hp_ne_rest q hq)
        · exact hcross q (List.Mem.tail _ hq) x hx_pl
      · intro x hx
        rcases List.mem_cons.mp hx with rfl | hx_pl
        · exact ho_bounds
        · exact hpl_bounds x hx_pl
      · exact hnew_uniq
      · exact hnew_dis
      · exact hassign

/-- Soundness of the pool-to-lane bridge: any schedule colored by `assignPoolLanes` satisfies
    `ScheduleOK` and passes `checkSchedule = none`. -/
theorem assignPoolLanes_sound (pops : List PoolOp) (ops : List ScheduledOp) (links : List WaitLink)
    (hp_bounds : ∀ p ∈ pops, poolOpBoundsOK p = true)
    (hp_uniq : hasUniquePoolOpIds pops = true)
    (hw_ok : ∀ w ∈ links, waitLinkOK w = true)
    (hassign : assignPoolLanes pops = some ops) :
    ScheduleOK ops links ∧ checkSchedule ops links = none := by
  have ⟨huniq, hbounds, hdis⟩ :=
    assignPoolLanesAux_sound pops [] ops
      hp_bounds
      hp_uniq
      (fun _ _ _ ho => nomatch ho)
      (fun _ ho => nomatch ho)
      rfl
      rfl
      hassign
  have hok : ScheduleOK ops links := ⟨huniq, hbounds, hw_ok, hdis⟩
  exact ⟨hok, (checkSchedule_ok_iff ops links).mpr hok⟩

/-! ## 4. Idea #07: Rolling-Horizon Instrument Breakdown Contingency Gate -/

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

/-! ## 5. Idea #08: Day/Night Dual-Regime Queue Partition Gate -/

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

-- Duplicate opId collision reproducer (#31) is now rejected with rule "duplicate_op_id"
#guard (checkSchedule
         [ { sampleOp1 with opId := 1, poolCap := 1, startMin := 0, durMin := 15 },
           { sampleOp1 with opId := 1, poolCap := 1, startMin := 5, durMin := 15 } ] [])
       == some { rule := "duplicate_op_id",
                 detail := "every scheduled operation must carry a unique opId" }

-- Departure-first sweep-line accepts tight back-to-back handoff [0, 15) -> [15, 30) on cap=1
#guard sweepLinePoolOK 0 1
         [ { sampleOp1 with opId := 10, poolCap := 1, startMin := 0, durMin := 15 },
           { sampleOp1 with opId := 11, poolCap := 1, startMin := 15, durMin := 15 } ] == true

-- Departure-first sweep-line rejects overlapping [0, 15) and [14, 25) on cap=1
#guard sweepLinePoolOK 0 1
         [ { sampleOp1 with opId := 10, poolCap := 1, startMin := 0, durMin := 15 },
           { sampleOp1 with opId := 11, poolCap := 1, startMin := 14, durMin := 11 } ] == false

-- Greedy pool-to-lane bridge colors overlapping incubator runs onto lanes 0 and 1, then reuses lane 0
#guard (match assignPoolLanes
          [ { opId := 1, poolId := 1, poolCap := 2, startMin := 0, durMin := 10, releaseMin := 0, deadlineMin := 40 },
            { opId := 2, poolId := 1, poolCap := 2, startMin := 2, durMin := 10, releaseMin := 0, deadlineMin := 40 },
            { opId := 3, poolId := 1, poolCap := 2, startMin := 10, durMin := 10, releaseMin := 0, deadlineMin := 40 } ] with
        | some colored => (colored.map ScheduledOp.machineIdx == [0, 1, 0]) && (checkSchedule colored []).isNone
        | none => false) == true

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
