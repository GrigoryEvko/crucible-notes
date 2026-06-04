# LLO Bundle Packing

> *Every address, offset, and constant on this page was read byte-exactly from `libtpu.so` in the `libtpu-0.0.40-cp314` wheel (BuildID md5 `89edbbe81c5b328a958fe628a9f2207d`). Other versions differ. `.text` VMA == file offset for this binary; all addresses are virtual.*

## Abstract

Bundle packing is the final scheduling-and-legalization stage before raw bundle byte emission: it takes the flat list of `LloInstruction`s for a region — the last IR above the wire form — and assigns each one to a slot inside a fixed-width VLIW [bundle](../isa/bundle-model-overview.md), respecting the per-generation resource budget (functional-unit count, vector source ports, immediate slots, predicate fields). The output is a `vector<Bundle>` whose typed sub-fields the per-gen `Encoder<gen>::EncodeBundleInternal` then serializes ([MC-Emitter](../isa/mc-emitter.md)). This page documents the slot-fill / list-scheduling algorithm, the per-slot legality rules, the resource-conflict check that decides whether an op fits the current bundle or starts a new one, and the bundle-emission loop.

The algorithm is **forward greedy list scheduling**, not ILP and not modulo scheduling. Inner-loop software pipelining is a separate path ([Bundle Modulo Scheduling](bundle-modulo-scheduling.md)); the macro-level async/critical-path reordering above LLO is a separate pass ([LatencyHidingScheduler Core](latency-hiding-scheduler-core.md)). The packer documented here is per-region and local: it walks LLO ops in IR order, computes each op's earliest legal bundle from its read-after-write dependencies, asks a resource-counter (`SlotTracker`) for the first bundle at or after that point whose remaining capacity admits the op's `BundleRequirement`, appends empty bundles when none fits, and commits. The "max" the scheduler above it uses to *price* a bundle ([Bundle-Aware Cost](../cost/bundle-aware-cost.md)) is mirrored here as a hard *legality* check: the bundle's accumulated requirement must stay inside the gen's limit vector, field by field.

The reader who knows LLVM should hold one analogy and one divergence. The analogy: this is the same job LLVM's `VLIWPacketizer` / `DFAPacketizer` does — pack independent machine ops into one VLIW packet subject to a resource automaton. The divergence: libtpu carries **two** packers for **two** IRs. Subsystem (A), `xla::jellyfish::BundlePacker`, packs `LloInstruction*` and is the canonical, primary-detail path documented here. Subsystem (B), the LLVM-target `(anonymous)::BundlePacker` MachineFunctionPass, packs `MachineInstr*` after instruction selection and produces the same bundle byte layout via `ResourceSolver`. Both exist because TPU code has two IR representations on the way to bytes.

For reimplementation, the contract is:

- **The drain is `GlobalBundlePacker::Pack` over regions in topo order**: `PackPhis` first (collects the PHI-edge member ops — opcodes `233..236` — and feeds each through `PackInstruction`), then the region body via `PackInstruction`, which itself re-dispatches branch opcodes (`0x87`/`0x88`/`0xEF`) to `PackBranch`, then `ConsumeBundles`.
- **`BundlePacker::Feed` computes `MinSafeHoistBundleNo`** as the running `max` over every dependency floor (operand producer bundle + `LatencyBetween`, control sources/targets, register/arch-register feasibility, vmem fences), then places the op there or appends NOP bundles until it fits.
- **`SlotTracker::FindFeasibleBundleAfter` is the resource-conflict check**: it asserts the op's `BundleRequirement` is within the empty-bundle limit, then scans a per-requirement feasible-bundle bitmap for the first index ≥ start whose `CombinationIsWithin(bundle_req, requirement, limits)` holds.
- **`PlaceInstructionInBundle` commits**: `Bundle::AddInstruction(instr, value_set, multi)`, set `scheduled_bundleno`, add the complement primary (one LLO op → two slots) when `LloOpcodeIsComplement`, and tag the bundle as a terminator on branch opcode `135` with a non-null target.
- **Per-slot legality is a virtual `TpuBundleRestrictions` per gen** (`SetLimits` / `AddXluRequirements` / `AddMxuRequirements` / `MatchScalar`), keyed at limit-fetch by `(TpuVersion, TpuSequencerType)`.

| | |
|---|---|
| **Top-level entry** | `deepsea_compiler_backend::(anon)::PackBundles` @ `0x10a30a20` |
| **Orchestrator** | `GlobalBundlePacker::Pack` @ `0x10a86420` (src `global_bundle_packer.cc`) |
| **Schedule one op** | `BundlePacker::Feed(LloInstruction*, latency)` @ `0x14021f20` (src `bundle_packer.cc`) |
| **Resource-conflict check** | `SlotTracker::FindFeasibleBundleAfter` @ `0x140340a0` (src `sched/slot_tracker.cc`) |
| **Commit** | `BundlePacker::PlaceInstructionInBundle` @ `0x1401fe80`; `PlaceInstruction` @ `0x14020a00` |
| **Mutate slot** | `Bundle::AddInstruction(instr, value_set, multi)` @ `0x1c455e60` |
| **Per-gen legality** | `TpuBundleRestrictions::SetLimits` (vtable+16), `MatchScalar` (vtable+80) |
| **Limit fetch** | `BundleRequirementTracker::GetBundleLimits(Target, TpuSequencerType)` @ `0x1c6232e0` |
| **Finalize** | `BundlePacker::ConsumeBundles` @ `0x14026c40`; `ValidatePacking` @ `0x14026ca0` |
| **Bundle struct stride** | 112 bytes (`vector<Bundle>` element); out-of-line block 160 bytes (`0xA0`) |
| **Confidence** | CONFIRMED (byte-anchored) unless a row says otherwise |

---

## Algorithm Class — Forward Greedy List Scheduling

The packer is a single forward sweep with explicit per-bundle resource accounting. There is no backtracking and no spill-to-next-bundle search: if the resource counter says no existing bundle admits the op, the algorithm **always** appends fresh empty bundles and retries; only if `Feed` returns an error after that does the compile fail. The shape, recovered from `GlobalBundlePacker::Pack` (`0x10a86420`) and `BundlePacker::Feed` (`0x14021f20`):

```c
// GlobalBundlePacker::Pack()  @ 0x10a86420  (src global_bundle_packer.cc:157, 325-330)
StatusOr<vector<Bundle>> Pack() {
    ScopedLoggingTimer t("global bundle packer (...)");        // .rodata string, src 157
    GetOrCreateLastEmptyBundle();                              // seed bundles_ with one empty
    LloRegionVisitHelper visit(module);                        // topo walk of region members
    for (member in region, in IR order) {
        if (member.kind != kInstruction) continue;             // llo_region.h:161 RET_CHECK
        instr = member.instruction;
        uint16 op = instr->opcode;
        if (op - 233u >= 4u)                                   // skip opcodes 233..236 (PHI-edge members, handled by PackPhis)
            PackInstruction(instr);                            // 0x10a875a0; re-dispatches branches to PackBranch internally
    }
    Status st = ConsumeBundles();                              // 0x14026c40 — hand over vector<Bundle>
    // post-walk audit: for each bundle, has_branch_instruction == bundle.has_branch()  (src 79)
    return st;
}
```

`PackInstruction` (`0x10a875a0`) first re-tests the branch family `(op - 135u) < 2 || op == 239` and tail-calls `PackBranch` for those; otherwise it reads the monotonic pack-position counter at `packer + 0xDA0` (the `*((_QWORD*)this + 436)` field — the scheduling clock that advances one per op), forwards to `Feed(instr, counter)`, and increments the counter on success. `PackBranch` (`0x10a877a0`) does the same Feed/increment but then appends the branch-delay-slot bundles. The branch dispatch test in `Pack`'s post-walk audit reads literally as `(op - 135u) < 2 || op == 239` — opcodes **135** (`0x87`, conditional branch), **136** (`0x88`, jump), **239** (`0xEF`, predicated transfer).

> **QUIRK — the region walk, not a heuristic, fixes branch placement.** Branches are always the last LLO instruction a region emits, so `PackBranch` runs last and the branch's `scheduled_bundleno` is necessarily the highest in the block. There is no separate "put the terminator last" rule; topological order guarantees it. A reimplementation that reorders ops before this packer must preserve that invariant or the post-walk `has_branch_instruction == bundle.has_branch()` audit (`global_bundle_packer.cc:79`) FATALs.

### Function Map

| Function | Address | Role |
|---|---|---|
| `(anon)::PackBundles` | `0x10a30a20` | top-level driver: timer, build `BundlePackerOptions`, construct `GlobalBundlePacker` |
| `GlobalBundlePacker::Pack` | `0x10a86420` | region topo walk; per-op dispatch; `ConsumeBundles` |
| `GlobalBundlePacker::PackPhis` | `0x10a87160` | collect opcodes 233..236, feed each via `PackInstruction` |
| `GlobalBundlePacker::PackInstruction` | `0x10a875a0` | regular op → `Feed(instr, pack_counter)` |
| `GlobalBundlePacker::PackBranch` | `0x10a877a0` | branch op → `Feed` + delay-slot padding + branch barrier |
| `BundlePacker::Feed` | `0x14021f20` | schedule one op: earliest-bundle + place |
| `BundlePacker::ConsumeBundles` | `0x14026c40` | clear alias analysis, hand `vector<Bundle>` to caller |
| `ValidatePacking` | `0x14026ca0` | post-pack correctness re-check |

---

## Schedule One Op — `BundlePacker::Feed`

### Purpose

`Feed(LloInstruction*, latency)` (`0x14021f20`) is the heart of subsystem (A): it determines the earliest bundle the op may legally occupy, asks the resource trackers for a feasible bundle there, grows the bundle vector with NOP bundles if needed, and commits. Its decompile is large because it folds the opcode-class dispatch, the redundant-move peephole, the constant rejection, the dependency walk, the fence insertion, and the placement into one function.

### Algorithm

```c
// BundlePacker::Feed(LloInstruction* instr, int64 latency)  @ 0x14021f20  (src bundle_packer.cc)
Status Feed(LloInstruction *instr, int64 latency) {
    uint16 op = instr->opcode;

    // ---- (0) ops not yet in any bundle: special opcode classes ----
    if (!instr->in_bundle()) {                                // *((int*)instr+12) < 0
        if (op == 8) { /* drop into last empty bundle, NoteSchedulingBarrier */ ... return Ok; }
        if (RegisterTracker::IsRedundantMove(instr))          // 0x14033900  mov rN,rN peephole
            { /* co-locate with the operand's bundle; do not consume a slot */ return Ok; }
        // opcodes 219..234 with bit-test masks: barrier-into-last-empty-bundle class
        if (op == 44 /* constant */)
            return FailedPrecondition(                        // src bundle_packer.cc, near offset
                "Cannot feed constants into bundle packer. Copy them to registers first.");
    } else {
        // op already scheduled: must be the complement half of a wide-vector pair
        complement = GetInstructionComplement(instr);         // 0x1d4f62c0
        RET_CHECK(complement != nullptr);                     // src 872
        RET_CHECK(complement->in_bundle());                   // src 873
        RET_CHECK(instr->scheduled_bundleno()
                  == complement->scheduled_bundleno());       // src 875 "must be same bundle"
        return Ok;
    }

    // ---- (1) earliest legal bundle: a running MAX over every dependency floor ----
    int n = bundle_limits_min;                                // RangeSpec-driven base
    if (LloOpcodeCreatesSchedulingBarrierBeforeBundle(op)) BarrierHere();   // sync ops
    n = BundleFifoTracker::FindFeasibleBundle(instr);         // 0x14445ac0  FIFO-port floor
    for (operand in instr->non_immediate_operands()) {        // RAW from data operands
        producer = LloInstruction::FromValue(operand);
        if (producer->in_bundle()) {
            int rt = producer->scheduled_bundleno()
                   + LatencyTable::LatencyBetween(operand, instr);   // dependency latency
            n = max(n, rt);
        }
    }
    n = max(n, RegisterTracker::FindFirstFeasibleBundle(instr));          // 0x...
    n = max(n, BundleArchRegisterTracker::FindFirstFeasibleBundle(instr));
    // control-edge hazards (predicate / branch targets)
    if (OpcodeLowersToVload(op))
        n += DelayForRawHazardsFromControlSources(instr, n, ...);        // 0x14026440
    if (op == 135 && instr->target_block())
        n += DelayForRawHazardsToControlTargets(instr, n, ...);          // 0x140265e0
    n = max(n, BundleAliasAnalysis::FindMinSafeHoistBundleNo(instr, n, ...));

    // ---- (2) vector-store fence ordering ----
    BundleRequirement req = GetBundleRequirement(instr, immediates);     // op→requirement
    bool need_fence = RequiresVectorStoreFence(instr, n);                // 0x14020900
    if (need_fence) { /* synthesize CreateVectorStoreFence, fold its req into `req` */ }
    if (!need_fence)
        need_fence = !IsVmemReadPrecededByVmemStoreFence(instr, n);      // 0x14020820

    // ---- (3) grow the bundle vector to cover index n (NOP bundles) ----
    while (bundles_.size() <= n) {                            // emplace_back zero-inits 0xA0 bytes
        if (n - bundles_.size() + 1 >= 257)                   // src 1470
            LOG(WARNING) << "suspiciously large number of nops: " << ...;
        bundles_.emplace_back();                              // __emplace_back_slow_path 0x10a89620
    }

    // ---- (4) commit ----
    if (need_fence) PlaceInstruction(n, fence_instr, ...);    // fence goes first
    return PlaceInstruction(n, instr, value_set, req, is_branch);        // 0x14020a00
}
```

The dependency-floor variable is named `MinSafeHoistBundleNo` in the decompile, and it is raised by `if (n <= rt) n = rt;` at every contributor — i.e. a `max`. The operand RAW floor is byte-exact at the loop near `bundle_packer.cc` offset where each operand's `scheduled_bundleno + LatencyTable::LatencyBetween(operand, instr)` is taken; `LatencyBetween` is the same per-op-pair dependency latency the cost model uses ([Bundle-Aware Cost](../cost/bundle-aware-cost.md#the-dependency-latency-axis--latencybetween)).

> **GOTCHA — there is no spill-to-next-bundle search.** When the trackers cannot fit the op into any *existing* bundle at or after `n`, the algorithm appends empty bundles (each a 112-byte zeroed `Bundle`, with an out-of-line 160-byte block allocated lazily) and retries — it never re-tries earlier indices or moves already-placed ops. The only failures are the five fatal paths below. A reimplementation that adds an "if it doesn't fit, try the previous bundle" branch diverges from the binary.

> **NOTE — `op - 233u >= 4u` in `Pack` skips the PHI-edge opcode window; `PackInstruction` and `Feed` re-dispatch.** `GlobalBundlePacker::Pack`'s region loop skips opcodes **233..236** (`op - 233 >= 4` is the *keep* condition) because those member ops are handled by `PackPhis` (`global_bundle_packer.cc:104`), which collects them via the inverse test `op - 233 <= 3` and feeds each through `PackInstruction`. `PackInstruction` (`0x10a875a0`) then re-tests the branch family and tail-calls `PackBranch`. `Feed` itself special-cases `op == 8` (drop into last empty bundle + barrier), the redundant-move peephole, a `v5 - 219 <= 0x15` barrier window (opcodes **219..240**, further pruned by two inline bit-test masks), and `op == 44` (constant rejection) before the general dependency walk. The exact per-opcode membership of the bit-test-masked barrier window was not enumerated (PARTIAL).

### Failure Paths

Five distinct fatal/error sites in subsystem (A), all anchored to `bundle_packer.cc` / `global_bundle_packer.cc` line numbers in the decompile:

| Condition | Site | Status returned |
|---|---|---|
| Complement halves in different bundles | `bundle_packer.cc:875` | `RET_CHECK` → FailedPrecondition |
| Complement is null / not in bundle | `bundle_packer.cc:872`/`873` | `RET_CHECK` |
| Constant fed to packer | "Cannot feed constants…" | FailedPrecondition |
| Constant operand where only non-constants allowed | `bundle_packer.cc:928` | `RET_CHECK` |
| Requirement exceeds empty-bundle limit | `slot_tracker.cc:23` | `LogMessageFatal` "requirement doesn't fit in an empty bundle!" |
| `has_branch` / instruction mismatch (post-walk) | `global_bundle_packer.cc:79` | `RET_CHECK` |

---

## The Resource-Conflict Check — `SlotTracker::FindFeasibleBundleAfter`

### Purpose

`SlotTracker` (ctor `0x14033fc0`) is the per-`BundlePacker` mutable resource counter. It holds the gen's per-bundle **limit vector** (a `BundleRequirement`) and, per existing bundle, the requirement consumed so far. `FindFeasibleBundleAfter(req, start)` (`0x140340a0`) is the function that decides whether an op fits the current bundle or must start a new one: it returns the first bundle index ≥ `start` whose remaining capacity admits `req`.

### Algorithm

```c
// SlotTracker::FindFeasibleBundleAfter(req, start)  @ 0x140340a0  (src sched/slot_tracker.cc:20-49)
int FindFeasibleBundleAfter(const BundleRequirement &req, int start) {
    // (1) the op must fit an EMPTY bundle at all — checked field by field via
    //     four bitmap-difference masks against bundle_limits_:
    CHECK( ((limit[0] - req[0]) & 0x4925555552A84888) == 0 &&   // src 23 "requirement.IsWithin(...)"
           ((limit[1] - req[1]) & 0x555555554A508424) == 0 &&
           ((limit[2] - req[2]) & 0x2491515555088AAA) == 0 &&
           ((limit[3] - req[3]) & 0x524)               == 0 );  // else FATAL "doesn't fit empty bundle!"

    start = max(start, point_of_no_return_);                    // closed bundles excluded

    // (2) the per-requirement feasible-bundle cache (a bitmap of bundle indices)
    if (!feasible_bundles_by_requirement_.contains(req))
        PopulateFeasibleBundles(req);                           // 0x14034680 — rebuild the bitmap
    CHECK(feasible_bundles_by_requirement_.contains(req));      // src 38

    // (3) scan forward from `start` for the first set bit (feasible bundle)
    int ret = FindNextSetBitBeforeLimit(feasible_bitmap, start, num_bundles);
    if (found) {
        // (4) sanity: the chosen bundle's accumulated req + this req is within limits
        CHECK( BundleRequirement::CombinationIsWithin(                       // src 49
                   bundle_requirement(ret), req, bundle_limits_) );
        return ret;
    }
    return max(start, num_bundles + base);                      // none → index past the end
}
```

The four AND-masks are the bitfield layout of the `BundleRequirement` struct: each `BasicBitmap<uint64>` word carries several slot-type sub-counters, and the per-mask AND isolates the bits that would borrow if `req` exceeded `limit`. The four masks (`0x4925555552A84888`, `0x555555554A508424`, `0x2491515555088AAA`, `0x524`) are the slot-field "carry masks" — they are byte-confirmed but the per-field decomposition (which bits are scalar vs vector-ALU vs immediate vs XLU vs MXU) is downstream work (PARTIAL on the leaf field boundaries).

> **NOTE — "fits the current bundle vs starts a new one" is the set-bit scan in step (3).** `FindNextSetBitBeforeLimit` over `feasible_bitmap` from `start` returns the first bundle the op can join; if that bit is the current (last) bundle it is packed there, otherwise the search jumps to a later already-existing bundle, and if no set bit exists `Feed` grows the vector. The resource accounting that maintains the bitmap is `NoteAddedToBundle` (`0x14035080`, `bundle.consumed += req`) and `NoteRemovedFromBundle` (`0x14035780`, `-= req`); `BundleRequirement::operator+=` (`0x140237a0`) is plain 64-bit integer **addition** on each of the four packed words (`*a1 += *a2` for words 0–2 plus the 32-bit word 3), with the same four AND-masks (`0x4925555552A84888`, `0x555555554A508424`, `0x2491515555088AAA`, `0x524`) re-applied after each add as a carry-overflow check that FATALs `"IsValidWord(i)"` (`bundle_requirement.h:629`) if any per-field sub-counter overflows into its neighbor.

### `BundleRequirement` Fields (shape)

`BundleRequirement` is a packed struct of `BasicBitmap<uint64>` fields plus scalar counters. The exact bit boundaries are not fully decoded, but the field roster is recovered from the per-gen `SetLimits` mask writes and the `OverflowingFields` reporter (`0x1c623e00`):

| Field group | Tracks |
|---|---|
| scalar slots | SPU / sequencer slot occupancy (2 on JF/PF, up to 3 on V5+) |
| vector-ALU slots | VPU lane occupancy (2 on JF, up to 2–3 on V5+) |
| immediate slots | immediate-field occupancy (6 on JF; per-gen) |
| vector source ports | vs0..vs2 (3 on JF, 4 on V5+) |
| XLU resource | transcendental-unit demand (1 on JF/PF, 2 on V5+) |
| MXU resource | matrix-unit demand (1 on JF, up to 2–4 on V5+) |
| ttu / branch / vmem-load / vmem-store | TTU (JF), terminator flag, fence-position counters |

> **GOTCHA — the limit vector is per `(version, sequencer-type)`, not per chip.** `BundleRequirementTracker::GetBundleLimits(Target, TpuSequencerType)` (`0x1c6232e0`) indexes `trackers[*(int*)(target+920)]` (the `TpuVersion`) then offsets by the sequencer type: **`+32`** for `TensorCore` (type 0), **`+128`** for type 1, **`+224`** for type 2; types **3, 4, 5 FATAL** with `"Unsupported sequencer type"` in this build. A reimplementation that assumes one limit vector per chip will mis-size BarnaCore / SparseCore sub-bundles. The raw mapping of higher sequencer types to SparseCore/TAC/TEC limit offsets is **not** present in `0.0.40` (LOW confidence; only types 0–2 are wired).

---

## Per-Slot Legality — `TpuBundleRestrictions`

Per-generation slot rules are encapsulated in polymorphic `TpuBundleRestrictions` subclasses, one per silicon generation, each registered statically (`TpuBundleRestrictionsRegisterer`, `0x1c457880`) and looked up by codename at tracker construction. Four concrete subclasses are present in `0.0.40`: `JellyfishBundleRestrictions`, `PufferfishBundleRestrictions`, `ViperfishBundleRestrictions`, and `GhostliteBundleRestrictions` (over the abstract base `TpuBundleRestrictions`). Each implements the same virtual interface; the per-gen variation is entirely in the constants and per-opcode tables those virtuals carry.

### The Virtual Interface

| vtable slot | Method | Decides |
|---|---|---|
| +16 | `SetLimits(tc, sparse, misc)` | writes the per-sequencer-type slot-limit vectors via masked AND/OR |
| +24 | `AddXluRequirements(instr, req)` | per-opcode XLU (transcendental) slot demand |
| +32 | `AddMxuRequirements(instr, req)` | per-opcode MXU (matmul) slot demand |
| +40 | `HasVectorSlotRestrictions()` | gen forbids certain VPU slot pairings? |
| +48 | `HasEupRestrictions()` | EUP-modulo alignment required? |
| +56/+64 | `HasVectorOdd/EvenSlotRestrictions()` | odd/even VPU-slot constraint for shuffle/reduce |
| +72 | `LaneBroadcastSpecifierIsImplicitImmediate(int)` | lane-broadcast field encoded as implicit immediate (saves a slot)? |
| +80 | `MatchScalar(op_idx, ImmediateOperandType)` | which scalar/immediate slot can hold this immediate |

`JellyfishBundleRestrictions::SetLimits` (`0x1c457a40`) is the clearest anchor: it fills the three limit vectors (`tc`, `sparse`, `misc`) with vectorized `vandps`/`vorps`/`vpblendw` against per-gen constant blobs, including the documented `0xFE07FF8000007000` mask and the `0xAA9428404` / `0xC7FE607FFFFFFFFF` immediate-field constants. Each masked word sets one sequencer type's slot ceilings.

### Per-Gen Slot Inventory

Slot counts per generation, reconstructed from the `SetLimits` constants, the `BundleRequirement` struct-size growth (`0x80` on JF → `0xe0` on V5+), and the [bundle-model slot inventory](../isa/bundle-model-overview.md#slot-taxonomy). The byte-widths (41/51/64) are CONFIRMED on the bundle-model page; the per-slot counts are the *shape* of the growth.

Only the four codenames with a concrete `*BundleRestrictions` subclass appear in this build; `dragonfish` (v3) shares the `JellyfishBundleRestrictions` class (no separate subclass is registered). Version labels follow the SM/codename map: jellyfish = v2, dragonfish = v3, pufferfish = v4, viperfish = v5, ghostlite = v6 lite.

| Restriction class | Codename / ver | Bundle B | scalar | vec-ALU | imm | vs | XLU | MXU | ttu |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `JellyfishBundleRestrictions` | jellyfish (v2); also serves dragonfish (v3) | 41 | 2 | 2 | 6 | 3 | 1 | 1 | 1 |
| `PufferfishBundleRestrictions` | pufferfish (v4) | 51 | 2 | 2 | 6 | 3 | 1 | 1 | – |
| `ViperfishBundleRestrictions` | viperfish (v5) | 64 | 2 | 2 | 6–7 | 4 | 2 | 1–2 | – |
| `GhostliteBundleRestrictions` | ghostlite (v6 lite) | 64 | 2–3 | 2 | 6–7 | 4 | 2 | 2 | – |

> **NOTE — `MatchScalar` is the immediate-slot legalizer.** When an op carries an immediate operand, `(anon)::RequiredImmSlotCount` (`0x1c625ea0`) and `ImmediateSlotsRequired` (`0x1c626680`) count the immediate-slot contribution, and the per-gen `MatchScalar` (vtable +80) picks which physical immediate/scalar slot can satisfy each. Multiple immediates in one bundle compete; the `SlotTracker` tracks consumption. Constants too wide for any slot are spilled to a constant pool **upstream** of this packer — `Feed`'s `op == 44` rejection confirms the packer assumes per-instruction immediates already fit. See [Immediate Slot](../isa/slot-immediate.md).

---

## Commit — `PlaceInstructionInBundle`

`PlaceInstructionInBundle` (`0x1401fe80`) is the mutation step: once `PlaceInstruction` (`0x14020a00`) has applied the control-hazard delays and fence insertion, this function writes the op into the chosen `Bundle`.

```c
// BundlePacker::PlaceInstructionInBundle(bundle, bno, instr, value_set, req)  @ 0x1401fe80
//   (src bundle_packer.cc:659-689)
void PlaceInstructionInBundle(Bundle *b, int bno, LloInstruction *instr,
                              const LloValueSet &vs, const BundleRequirement &req) {
    bool multi = vptest(req) /* any requirement bit set → wide-vector pair */;
    Bundle::AddInstruction(b, instr, vs, multi);              // 0x1c455e60 — append InstructionRecord
    instr->scheduled_bundleno = bno;                         // *((int*)instr + 12) = bno

    if (LloOpcodeIsComplement(instr->opcode)) {              // 0x1d60c960 → opcode == 355 || == 36
        LloInstruction *primary = GetInstructionPrimary(instr);   // 0x1d4f6520
        Bundle::AddInstruction(b, primary, {}, /*multi=*/true);   // one LLO op fills two slots
        primary->scheduled_bundleno = bno;                   // *((int*)primary + 12) = bno
    }

    if (instr->opcode == 135 && instr->target_block()) {     // conditional branch with target
        b->ensure_branch_metadata();                         // alloc 0xA0-byte block at bundle[+104] (qw13)
        b->branch_meta->has_branch = 1;                      // byte +28 within that block
    }
    // NoteAddedToBundle(req, bno) bookkeeping happens on the SlotTracker
}
```

`Bundle::AddInstruction` (`0x1c455e60`) appends an `InstructionRecord {LloInstruction*, LloValueSet, bool multi}` to the bundle's inlined-vector (inline capacity 2, `EmplaceBackSlow` on overflow). The `multi` flag — computed by a `vptest` over the `BundleRequirement` (nonzero ⇒ set) — tells the encoder this record drives two physical slots (the wide-vector / complement pair).

> **QUIRK — one LLO op can fill two slots.** `LloOpcodeIsComplement` returns true only for opcodes **355** (`0x163`) and **36** (`0x24`); for those, `PlaceInstructionInBundle` adds *both* the complement and its `GetInstructionPrimary`, each into the same bundle with `multi=1`, and stamps both with the same `scheduled_bundleno`. That co-location is exactly what `Feed`'s complement `RET_CHECK` (`bundle_packer.cc:875`) enforces on the second-fed half. A reimplementation that treats every LLO op as one slot will mis-pack wide-vector pairs and trip the audit.

---

## Branch Handling — Terminator Bundle and Delay Slots

`GlobalBundlePacker::PackBranch` (`0x10a877a0`) handles opcodes `0x87` / `0x88` / `0xEF` (asserted by `LloOpcodeIsScalarBranch`, `global_bundle_packer.cc:112`).

```c
// GlobalBundlePacker::PackBranch(instr)  @ 0x10a877a0  (src global_bundle_packer.cc:112-133)
Status PackBranch(LloInstruction *instr) {
    int branch_delay = subtarget->branch_delay_slots;       // read at subtarget + 2324 (0x914)
    RET_CHECK(LloOpcodeIsScalarBranch(instr->opcode()));     // src 112
    Feed(instr, pack_counter);                               // normal placement
    int bno = instr->scheduled_bundleno();
    bundles_[bno].ensure_branch_metadata()->has_branch = 1;  // byte +28: terminator
    // append `branch_delay` empty bundles AFTER bno as delay slots
    for (int i = 0; i < branch_delay; ++i) {
        int slot = bno + 1 + i;
        while (bundles_.size() <= slot) bundles_.emplace_back();
        bundles_[slot].ensure_branch_metadata()->is_branch_delay_slot = branch_delay; // byte +29
    }
    ++pack_counter;
    EstablishBranchBarrier(bno, instr);                      // 0x14026880
    return Ok;
}
```

`branch_delay` is read from `TpuSubtarget + 2324` (= `0x914`, exactly matching the field offset). The delay-slot bundles are tagged with the byte at the branch-metadata block offset `+29` (the `is_branch_delay_slot` field). `EstablishBranchBarrier` (`0x14026880`) then:

1. Calls `BundleAliasAnalysis::NoteSchedulingBarrier(bno)` (`0x1402c780`) — taints memory aliasing past the branch.
2. Calls `SlotTracker::UpdatePointOfNoReturn(bno)` (`0x14035b40`) — so subsequent `Feed` calls cannot place ops at or before `bno` (the `start = max(start, point_of_no_return_)` clamp in `FindFeasibleBundleAfter`).
3. Records a predication override when the branch is predicated (the `latest_branch_barrier_` pair at `packer + 3032`/`+3040`), inverting the predicate so delay-slot ops can be predicated off.

> **NOTE — delay slots stay NOP here.** This packer does **not** fill the `branch_delay` bundles with hoisted ops; they remain empty (NOP) bundles. Any delay-slot scheduling is a separate pre-pass. The `point_of_no_return_` clamp specifically prevents `Feed` from sliding later ops into the delay window.

### Barriers

| Primitive | Address | Effect |
|---|---|---|
| `EstablishBarrier(bno)` | `0x14021d00` | `NoteSchedulingBarrier(bno)` + `UpdatePointOfNoReturn(bno)` for sync/unknown-effect ops |
| `EstablishBranchBarrier(bno, instr)` | `0x14026880` | as above, plus predication-override bookkeeping for branches |
| `BarrierHere()` | `0x14021de0` | append a fresh empty bundle after the last, then `EstablishBarrier` on it (in-line fence) |

`BarrierHere` is what `Feed` invokes for the vector-store fence and for ops where `LloOpcodeCreatesSchedulingBarrierBeforeBundle` is true.

---

## PHI Packing — SSA Resolution

`GlobalBundlePacker::PackPhis(region)` (`0x10a87160`) runs before the region body. It walks the region's members with the same `LloRegionVisitHelper` machinery as `Pack`, but with the *inverse* opcode filter: each member whose opcode satisfies `op - 233u <= 3u` (opcodes **233..236** — the PHI-edge member class that `Pack`'s body loop skips) is appended to a temporary `vector<LloInstruction*>`. After the walk, every collected op is packed through the normal `PackInstruction` path:

```c
// GlobalBundlePacker::PackPhis(region)  @ 0x10a87160  (src global_bundle_packer.cc:104)
Status PackPhis(LloRegion *region) {
    vector<LloInstruction*> phi_edge_ops;                  // operator new(0x18) — empty vector
    LloRegionVisitHelper visit(region);                    // same topo walk as Pack()
    for (member in region) {
        if (member.kind != kInstruction) continue;         // llo_region.h:161 RET_CHECK
        if ((uint16)(member.opcode - 233) <= 3u)           // opcodes 233..236 only
            phi_edge_ops.push_back(member.instruction);
    }
    for (instr in phi_edge_ops) {
        Status s = PackInstruction(instr);                 // 0x10a875a0
        if (!s.ok()) return AddSourceLocation(s, /*line=*/104);
    }
    return Ok;                                             // region body packed afterward by Pack()
}
```

The opcodes `233..236` are exactly the window `Pack`'s body loop excludes (`op - 233u >= 4u`), so the two passes partition the region: `PackPhis` handles the PHI-edge member ops, `Pack` handles everything else. PackPhis does **not** synthesize copies or write to predecessor bundles in this build — it simply re-routes those four opcode values through `PackInstruction` first. (CONFIRMED from the decompile; the *semantic* role of opcodes 233..236 as SSA-edge resolution is inferred — UNVERIFIED.)

---

## Subsystem (B) — The LLVM-Target `BundlePacker` Pass

A second, structurally identical packer lives in the LLVM TPU backend as a `MachineFunctionPass`, packing `MachineInstr*` after instruction selection. It produces the same bundle byte layout via the LLVM MC path ([MC-Emitter](../isa/mc-emitter.md)) rather than the proto-`Bundle` `Encoder<gen>` path.

```text
(anon)::BundlePacker::runOnMachineFunction  @ 0x13b206a0
  for MBB in MF:
    runOnMachineBasicBlock(scheduler, tracker, dag, MBB, insert_nops)   @ 0x13b23560
      ├─ TPUScheduleDAGMI: build the data-dependence DAG over MachineInstr
      ├─ BundlePackingScheduler (extends GenericScheduler): list-schedule
      │     pickNode / tryCandidate biased toward filling the current bundle
      └─ BundleTracker: assign each scheduled MI to a bundle
            canAddMI(MI)  @ 0x13b2b0e0   ── resource check (returns false → open new bundle)
            addMI(MI)     @ 0x13b2b2e0   ── commit
            addBundle(MI) @ 0x13b2b460   ── finalize via ResourceSolver::addMI per MI
  emitNop(MBB, iter, TII)  @ 0x13b2af20  ── explicit NOP MachineInstr when a bundle underfills
```

`BundleTracker::canAddMI` (`0x13b2b0e0`) is subsystem (B)'s resource-conflict check — the analog of `FindFeasibleBundleAfter`. Verified order:

1. **VReg source count**: `bundle.vs_used + TPUInstrInfo::countVRegSrcs(MI)` must be `≤ subtarget.maxVRegSrcs()` (read via vtable `+792`); over → return false.
2. **`ResourceSolver::canAddMI(MI)`** (`0x13beb500`): copy the `Solver` state, try `Solver::addMI`, check no over-budget counter incremented; discard the copy.
3. **`ResourceSolver::canAddVResDestPort(MI)`** (`0x13beb780`): vector-result destination-port conflicts.
4. **`ResourceSolver::canAddImm(MI, slot_pairs)`** (`0x13bec480`): immediate-slot availability per immediate operand.
5. **Must-be-last-in-bundle** ops (branches): `MachineInstr::hasPropertyInBundle` test → return false if the property forces bundle termination.

`Solver::getSlotAssignmentsForMI` (`0x13be8220`) reads the per-opcode allowed-slot mask from the LLVM `MCSchedClassDesc` table (`TPUStages`, see [MC-Emitter](../isa/mc-emitter.md)) and iterates the set bits to derive which proc-resources (= slot types) the op needs. The two subsystems are two implementations of the same VLIW slot-fill algorithm against two IRs.

> **GOTCHA — subsystem (B) emits explicit NOPs; subsystem (A) does not.** `(anon)::BundlePacker::emitNop` (`0x13b2af20`) constructs a real NOP `MachineInstr` (opcode from `TII.getNopOpcode()`, vtable `+0x358`) when a bundle underfills. Subsystem (A) instead leaves empty `Bundle` entries (zeroed slot mask), and the per-gen encoder fills them with `kNeverExecute` predicates or the static `kNoopBundleBytes` template ([Bundle Model](../isa/bundle-model-overview.md#empty-slot-and-nop-convention)). A reimplementation that mixes the two NOP conventions for one IR path will double-emit or under-emit pad bundles.

---

## Validation — `ValidatePacking`

`ValidatePacking(Span<const Bundle>, const LloModule&, bool strict)` (`0x14026ca0`) runs after `ConsumeBundles` and is fatal on failure. It re-derives each bundle's `BundleRequirement` from scratch (summing each instruction's requirement), fetches the gen's limit vector via `BundleRequirementTracker::GetBundleLimits`, and validates field by field that `bundle_req ≤ limit`. It additionally checks predicate-field ranges, that branch ops appear only in the last bundle of a basic block, and that `scheduled_bundleno` is monotonic. The expensive `BundleRequirementPreciseImpl::DeepValidateBundle` (`0x14054c00`, under `CodeGenerator::MakeBundleTester`) re-simulates adding each op one at a time; it is a test/debug path.

---

## Confidence Summary

| Claim | Evidence |
|---|---|
| Forward greedy list scheduling, no spill-to-next-bundle | `Pack` `0x10a86420` + `Feed` `0x14021f20` (emplace-only grow) |
| `Pack` walks regions topo-order, dispatches by opcode | `0x10a86420` (`op-233>=4` filter; `(op-135)<2 \|\| op==239` audit) |
| `Feed` earliest bundle = `max` over dependency floors | `0x14021f20` `MinSafeHoistBundleNo` raised by each contributor |
| Operand RAW floor = `producer_bno + LatencyBetween` | `0x14021f20` operand loop calling `LatencyTable::LatencyBetween` |
| `FindFeasibleBundleAfter` is the resource-conflict check | `0x140340a0` IsWithin masks + `FindNextSetBitBeforeLimit` + `CombinationIsWithin` |
| Four bitmap carry-masks for `IsWithin` | `0x140340a0` (`0x4925555552A84888`, …, `0x524`) |
| `PlaceInstructionInBundle`: AddInstruction + scheduled_bundleno + complement + branch tag | `0x1401fe80` |
| `LloOpcodeIsComplement` ⇔ opcode 355 or 36 | `0x1d60c960` `return a1==355 \|\| a1==36` |
| Branch delay slots read from `subtarget + 0x914` | `0x10a877a0` (`subtarget + 2324`); delay byte +29 |
| `EstablishBranchBarrier`: NoteSchedulingBarrier + UpdatePointOfNoReturn | `0x14026880` |
| `GetBundleLimits` seq-type offsets 32 / 128 / 224; 3–5 FATAL | `0x1c6232e0` switch |
| `SetLimits` writes 3 limit vectors via masked AND/OR (`0xFE07FF8000007000`) | `0x1c457a40` |
| Subsystem (B) `canAddMI` order (VRegSrcs, Solver, VResDestPort, Imm, last-in-bundle) | `0x13b2b0e0` |
| `PackPhis` collects opcodes 233..236, feeds via `PackInstruction` | `0x10a87160` (`op-233<=3` filter; AddSourceLocation 104) |
| Semantic role of opcodes 233..236 as SSA-edge resolution | inferred from pass partitioning, not byte-anchored |
| `BundleRequirement::operator+=` is integer add + carry-mask validity check | `0x140237a0` (`bundle_requirement.h:629` FATAL) |
| Four concrete `*BundleRestrictions` subclasses (JF/PF/VF/Ghostlite) | `nm` symbol roster; dragonfish shares JF class |
| Per-gen slot counts beyond Jellyfish | derived from `BundleRequirement` size growth + bundle widths |
| Seq types 3–5 → SparseCore/TAC/TEC offsets | not wired in `0.0.40` (FATAL) |

---

## Cross-References

- [Bundle Model Overview](../isa/bundle-model-overview.md) — the VLIW bundle word and slot taxonomy this packer fills; the empty-slot / `kNeverExecute` NOP convention.
- [MXU Slot](../isa/slot-mxu.md) — the matrix-unit slot family whose `AddMxuRequirements` demand the packer accounts for.
- [Immediate Slot](../isa/slot-immediate.md) — immediate-slot allocation and the `MatchScalar` legalizer this packer consults.
- [MC-Emitter](../isa/mc-emitter.md) — the LLVM-MC byte path that subsystem (B)'s `Solver`/`BundleTracker` feed.
- [Bundle-Aware Cost](../cost/bundle-aware-cost.md) — `MaxResourceCycles` (the bundle-cost `max`) and `LatencyBetween` (the dependency latency this packer reads to compute the earliest bundle).
- [LatencyHidingScheduler Core](latency-hiding-scheduler-core.md) — the macro-level HLO scheduler that runs above LLO; its per-node cost feeds from the same bundle model.
- [Bundle Modulo Scheduling](bundle-modulo-scheduling.md) — the separate inner-loop software-pipelining path (`TPUScheduleDAGModulo`) invoked instead of this list scheduler for hardware loops.
- **Binary:** `extracted/libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64/libtpu/libtpu.so` (build-id `89edbbe81c5b328a958fe628a9f2207d`)
- **Index entry:** Part VIII — Instruction Scheduling & Bundle Packing — [back to index](../index.md)
