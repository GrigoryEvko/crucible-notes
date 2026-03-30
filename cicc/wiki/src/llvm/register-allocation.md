# Register Allocation

> **Prerequisites:** Familiarity with [NVPTX register classes](../reference/register-classes.md), the [GPU execution model](../gpu-execution-model.md) (especially occupancy and register pressure), and [Live Range Calculation](live-range-calc.md). Understanding of [Register Coalescing](register-coalescing.md) is helpful.

> **NVIDIA-modified pass.** See [Differences from Upstream](#differences-from-upstream-llvm) for GPU-specific changes.
>
> **Upstream source:** `llvm/lib/CodeGen/RegAllocGreedy.cpp`, `llvm/lib/CodeGen/SplitKit.cpp`, `llvm/lib/CodeGen/RegisterCoalescer.cpp`, `llvm/lib/CodeGen/LiveRangeEdit.cpp` (LLVM 20.0.0). NVPTX register class definitions: `llvm/lib/Target/NVPTX/NVPTXRegisterInfo.td`.
>
> **LLVM version note:** CICC v13.0 ships two complete copies of `RAGreedy` (legacy PM at `0x1EC0400`, new PM at `0x2F4C2E0`). The new PM variant matches the LLVM 20 `RAGreedyPass` interface. The `PriorityAdvisor`/`EvictionAdvisor` infrastructure matches LLVM 15+ patterns. All NVPTX-specific behavior (pressure-driven allocation, `-maxreg` ceiling, occupancy-aware rematerialization) is layered on top of stock `RAGreedy` via TTI hooks and custom knobs.

NVPTX register allocation in CICC v13.0 operates under a fundamentally different model from CPU targets. PTX has no fixed physical register file -- registers are virtual (`%r0`, `%r1`, `%f0`, ...) and the hardware scheduler maps them to physical resources at launch time. The "physical register" concept in LLVM's greedy allocator maps to register pressure constraints rather than actual hardware registers, making the allocator pressure-driven rather than assignment-driven. The primary constraint is the `-maxreg` limit (default 70), which bounds total live registers across all classes to control occupancy on the SM.

| | |
|---|---|
| **Greedy RA driver** | `sub_2F5A640` (466 lines) |
| **selectOrSplit core** | `sub_2F49070` (82KB, 2,314 lines) |
| **Live range splitting** | `sub_2F2D9F0` (93KB, 2,339 lines) |
| **Register coalescing** | `sub_2F71140` (80KB, 2,190 lines) |
| **Register info init (new)** | `sub_30590F0` |
| **Register info init (old)** | `sub_2163AB0` |
| **Allocation failure handler** | `sub_2F418E0` |

## Dual Greedy RA Instances

CICC contains two complete copies of the Greedy Register Allocator infrastructure, corresponding to the legacy and new LLVM pass managers:

- **Instance A** (legacy, `0x1EC0400` region): registered through the old pass manager pipeline.
- **Instance B** (new, `0x2F4C2E0` region): registered through `sub_2F504C0` as the factory function.

Both are registered under the pass name "Greedy Register Allocator" via `RAGreedyPass` (`sub_2342890`). The `selectOrSplit` entry point at `sub_2F4BAF0` is a thin wrapper that redirects to `sub_2F49070(this + 200, ...)`. A separate entry at `sub_2F4BB00` handles the spill-or-split path with `SplitEditor` integration.

## NVPTX Register Classes

CICC defines nine register classes plus one internal-only class. The complete register class table -- vtable addresses, PTX type suffixes, prefixes, encoded IDs, copy opcodes, and coalescing constraints -- is in [Register Classes](../reference/register-classes.md).

The classes are completely disjoint -- there is no cross-class interference. Each type lives in its own namespace: integer 32-bit values occupy `%r` registers, 32-bit floats occupy `%f` registers, and so on. Copy instructions are class-specific, with both same-class and cross-class opcodes dispatched by `sub_2162350` (see the [copy opcode table](../reference/register-classes.md#copy-opcodes----sub_2162350)).

## Greedy selectOrSplit -- Detailed Algorithm

**Complexity.** Let V = number of virtual registers, R = number of register units, and I = total MachineInstr count. The main allocation loop processes V virtual registers in priority order. For each VReg, `selectOrSplit` performs: (1) operand scanning in O(operands) with 40-byte stride, (2) interference scanning (`scanInterference`) in O(R) via the RegAllocMatrix, (3) assignment or eviction attempts in O(R) per candidate. The `tryLastChanceRecoloring` path is bounded by `lcr-max-depth` (default 5) and `lcr-max-interf` (default 8), giving O(8^5) = O(32768) per VReg in the absolute worst case -- though this path is rarely taken. Live range splitting (`splitAroundRegion`, 93KB) iterates segments in O(S) where S = number of live range segments, with interference analysis per segment in O(R). Overall: O(V * R) for the common case, O(V * R + V * 8^D) when last-chance recoloring is exercised at depth D. The interference cache's open-addressing hash map with `37 * reg` provides O(1) amortized lookups. Spill cost computation (`setupSpillCosts`) is O(V * I_avg) where I_avg is average instructions per VReg's live range. On NVPTX, the completely disjoint register classes mean cross-class interference is zero, reducing the effective R to the per-class register count.

The core allocation algorithm (`sub_2F49070`, 82KB, 2,314 decompiled lines) follows LLVM's standard `RAGreedy::selectOrSplit` structure with NVPTX-specific adaptations for pressure-driven allocation. The following pseudocode is reconstructed from the decompiled binary and covers the key phases visible in the new-pass-manager instance.

### Initialization (lines 381--484)

```text
fn selectOrSplit(this: &mut RAGreedyState, VirtReg: &LiveInterval) -> PhysReg {
    let TRI     = this.TargetRegisterInfo;
    let NumRegs = TRI[+44];                             // total reg unit count

    // --- RegUnitStates: per-register-unit state array ---
    //     Stored at this+1112, 4 bytes per unit.
    //     Values: 0 = free, 1 = interfering, 2 = reserved
    this.RegUnitStates = alloc_zeroed(NumRegs * 4);     // at this+1112

    // --- Live-through bitvector ---
    //     Stored at this+736, one bit per register unit,
    //     packed into 64-bit words.  Set bits mark units
    //     live across the entire interval.
    let bv_words = (NumRegs + 63) / 64;
    this.LiveThrough = alloc_zeroed(bv_words * 8);      // at this+736

    // --- Interference cache ---
    //     Open-addressing hash map at this+648/656/664.
    //     Key   = register number (unsigned 32-bit)
    //     Hash  = 37 * reg  (mod table_capacity)
    //     Empty = 0xFFFFFFFF (-1),  Tombstone = 0xFFFFFFFE (-2)
    //     Growth: when 4*(count+1) >= 3*capacity, double & rehash.
    this.IntfCache.buckets  = alloc_sentinel(initial_cap); // this+648
    this.IntfCache.count    = 0;                           // this+656
    this.IntfCache.capacity = initial_cap;                 // this+664
    ...
```

The `RegUnitStates` array is the central per-unit bookkeeping structure for the entire allocation of a single virtual register. Each 4-byte slot tracks whether that register unit is free, already interfering with the current live range, or reserved by the target. The array is zeroed at the start of every `selectOrSplit` invocation and released at cleanup (lines 2192--2313).

The interference cache at `this+648` is distinct from LLVM's standard `InterferenceCache` (allocated at `0x2C0` bytes via `sub_2FB0E40` during driver setup). This per-invocation cache is a lightweight open-addressing map used to deduplicate interference queries within a single `selectOrSplit` call. The hash function `37 * reg` is a small Knuth-style multiplicative hash chosen for speed over distribution quality -- adequate because register numbers are small consecutive integers.

### Operand Scanning (lines 690--1468)

The function walks every `MachineOperand` attached to the live range's segment list. Operands are stored in a flat array with a **40-byte stride** per entry. The type byte at offset `+0` of each operand classifies it:

| Type Byte | Meaning | Action |
|---|---|---|
| `0` | Virtual register | Check copyable/tied flags; record in VReg worklist |
| `12` | Register mask (call clobber) | Store pointer in regmask list at `this+1176/1184` |
| other | Physical register | Mark in reserved bitvector; update `RegUnitStates` |

For each operand:

```text
    for op in VirtReg.operands(stride=40):
        match op.type_byte:
            0 =>                                        // virtual register
                if op.reg & 0x80000000:                 // negative = virtual
                    check_copyable(op);
                    check_tied(op);
                    update needsRecoloringFlag;          // v321
                else:
                    mark_reserved(op.reg, RegUnitStates);
                    update hasPhysicalAssignment;         // v323
            12 =>                                       // regmask
                append(this.regmask_list, op);
            _ =>
                mark_reserved(op.reg, RegUnitStates);
```

The 40-byte operand stride is wider than upstream LLVM's `MachineOperand` (typically 32 bytes) because CICC embeds an additional 8-byte field for NVPTX-specific metadata (likely the register class tag and a flags word). The scanning loop at line 690 uses `v321` (`needsRecoloringFlag`) and `v323` (`hasPhysicalAssignment`) as accumulator flags that gate later phases: if no virtual registers need work, the function returns early.

### Interference Processing via sub_2F43DC0 (lines 714--955)

After operand scanning, the allocator calls `sub_2F43DC0` (scanInterference) to populate the interference cache:

```text
    scanInterference(this, VirtReg, &IntfCache);
    // IntfCache now contains register units that conflict.
    // Iterate the conflict list at this+1128/1136:

    for conflict in IntfCache.entries():
        if conflict.is_constrained:
            // Tied operand or early-clobber -- must try eviction
            result = tryEviction(conflict);              // sub_2F48CE0
        else:
            // Normal overlap -- try simple direct assignment first
            result = tryAssign(conflict);                // sub_2F47B00

        if result.success:
            record_assignment(result.phys_reg);
            break;
        // else: continue to next candidate
```

`sub_2F43DC0` is the interference scanner. It walks the `RegAllocMatrix` (set up by `sub_3501A90` during driver init) to find live range overlaps. For each physical register unit that overlaps the current virtual register's live range, it inserts an entry into the interference cache using the `37 * reg` hash. The scanner distinguishes between two conflict types:

- **Constrained conflicts** (tied operands, early-clobber, regmask kills) -- these route to `sub_2F48CE0` (tryEviction), which attempts to evict the conflicting virtual register from its current assignment if the eviction cost is lower than the current candidate's spill weight.
- **Normal conflicts** -- these route to `sub_2F47B00` (tryAssign), which attempts a simple recoloring without eviction.

Additional helper functions participate in this phase:

| Function | Role |
|---|---|
| `sub_2F47200` | processConstrainedCopies -- handles operands where a COPY forced a specific register |
| `sub_2F46530` | tryLastChanceRecoloring -- last-resort recoloring bounded by `lcr-max-depth` (default 5) and `lcr-max-interf` (default 8) |
| `sub_2F46EE0` | rehashInterferenceTable -- grows/rehashes when load factor exceeds 75% |
| `sub_2F424E0` | updateInterferenceCache -- inserts a newly discovered conflict |
| `sub_2F42840` | markRegReserved -- marks a physical register as reserved in `RegUnitStates` |

The tryLastChanceRecoloring path (`sub_2F46530`) is the most expensive fallback. It recursively attempts to reassign conflicting registers, up to `lcr-max-depth` levels deep and considering at most `lcr-max-interf` conflicting live ranges at each level. The `exhaustive-register-search` flag bypasses both cutoffs, trading compile time for allocation quality.

### Copy Coalescing Hints -- Kinds 20 and 21 (lines 1060--1163)

During operand scanning, the allocator identifies COPY-like instructions by checking the **operand kind** field. Two kind values trigger coalescing hint recording:

```text
    for op in VirtReg.operands(stride=40):
        match op.kind:
            20 =>                                       // direct COPY hint
                record_hint(op.source_reg, op.dest_reg);
            21 =>                                       // parent-chain COPY hint
                if op.flags[+44] & (1 << 4):           // "has parent" flag
                    // Walk up the parent live range chain
                    let parent = op.parent_LR;
                    while parent != null:
                        recordCoalescingHint(parent);   // sub_2F41240
                        parent = parent.parent;
                    // Coalescing opportunities tracked at this+832/840
```

Kind 20 represents a simple register-to-register COPY where the source and destination should ideally receive the same physical register. Kind 21 is more complex: it indicates a COPY from a split sub-range that has a parent live range. The `has parent` flag at byte `+44` bit 4 triggers a chain walk via `sub_2F41240` (recordCoalescingHint), which records each parent in a coalescing hint list at `this+832/840`. The hint list is later consumed by `sub_2F434D0` (collectHintInfo) during the allocation priority computation, biasing the allocator toward assigning the same physical register to the entire chain.

This is standard LLVM coalescing hint infrastructure, but on NVPTX it interacts with the complete class separation: hints only apply within a single register class, since cross-class coalescing is impossible.

### Virtual Register Assignment (lines 1005--1368)

After interference processing and copy hint collection, the function enters the main assignment loop:

```text
    for vreg in unassigned_vregs:
        // Check the live-through bitvector at this+736
        if is_live_through(vreg, this.LiveThrough):
            // This vreg is live across the entire region -- expensive
            result = tryLastChanceRecoloring(vreg);     // sub_2F46530
        else:
            result = tryAssignFromHints(vreg);

        if result.success:
            recordAssignment(result);                    // sub_2F42240
            refresh_operand_list();                      // re-scan
        else:
            // Allocation failed for this vreg -- proceed to splitting
            add_to_split_worklist(vreg);
```

The live-through bitvector at `this+736` is the key data structure for this phase. A set bit indicates that the register unit is live from the beginning to the end of the current region, making it the hardest case for the allocator because there is no gap in which to insert a split point. These live-through ranges go directly to last-chance recoloring.

### Cleanup (lines 2192--2313)

The function releases the `RegUnitStates` array, clears the interference cache, frees the live-through bitvector, and returns 1 on success (physical register assigned) or 0 on failure (must spill).

## Live Range Splitting -- Detailed Algorithm

The splitting engine (`sub_2F2D9F0`, 93KB, 2,339 lines) implements `RAGreedy::splitAroundRegion` with `SplitAnalysis` and `SplitEditor` integration. This is the largest single function in the register allocation cluster.

### Segment Enumeration (40-byte stride, gap/sub-range flags)

The splitting engine iterates the live range's segment linked list using the same **40-byte stride** as the operand scanner. Two flag bits in the segment header control splitting decisions:

| Flag | Location | Meaning |
|---|---|---|
| Gap flag | bit 2 of `byte[0]` | Segment has a gap before it (potential split point) |
| Sub-range flag | bit 3 of `byte[44]` | Segment is a sub-range of a larger interval |

```text
fn splitAroundRegion(this: &mut SplitEditor, MF: &MachineFunction) {
    let SubTarget = MF.vtable[+128];
    let TRI       = SubTarget.vtable[+200];

    // Per-region loop -- worklist at this+320
    for region in this.worklist:

        // (a) Hash table init -- 16-byte entries per tracked register
        clear_and_resize(this.region_hash, initial_cap=16);

        // (b) Segment enumeration
        let seg = region.first_segment;
        while seg != null:
            let is_gap      = (seg[0] >> 2) & 1;       // bit 2 of byte[0]
            let is_subrange = (seg[44] >> 3) & 1;       // bit 3 of byte[44]

            if is_gap:
                // Potential split point -- record in visit set
                record_gap(seg, this.visit_set);         // sub_C8CC70

            if is_subrange:
                // Chain through sub-ranges
                process_subranges(seg);

            seg = seg.next;                              // stride = 40 bytes
```

The gap flag is the primary signal for split point selection. When the allocator detects a gap between two live segments, it can insert a split there without introducing a new spill -- the value is simply not live during the gap, so the split editor can create two separate live ranges that each get a different physical register. The sub-range flag indicates that the segment belongs to a sub-register lane (e.g., the low half of an `Int64Regs` value), which requires special handling to avoid breaking the lane structure.

### Copy Hint Detection and Local Splitting

For COPY instructions (kind values 68 and 0), the splitter extracts register pairs and builds a conflict set:

```text
        // (c) Copy hint detection
        for inst in region.instructions:
            if inst.kind == 68 || inst.kind == 0:       // COPY variants
                let (src, dst) = extract_reg_pair(inst); // operands at +32, stride 40, reg at +8
                conflict_set.insert(src);
                conflict_set.insert(dst);

                // Try local split first
                if tryLocalSplit(conflict_set):          // sub_2F2A2A0
                    // Success -- materialize the new segments
                    materializeSplitSegment();            // sub_2FDF330
                    continue;
```

`sub_2F2A2A0` (tryLocalSplit) attempts a low-cost split within a single basic block. On success, `sub_2FDF330` inserts the new split segments into the live interval data structure. The result entries from a local split use a 24-byte stride, where byte `+16` is a quality flag and dwords at `+8`/`+12` are the start/end positions of the split segment.

### Interference Analysis for Non-COPY Segments

For non-COPY segments, the splitting engine performs interference analysis using regmasks:

```text
        // (d) Interference analysis (lines 785-914)
        for seg in region.non_copy_segments:
            for op in seg.operands:
                if op.is_def && op.flags[+3] & (1 << 4):
                    check_def_interference(op);          // sub_2F28E80

            // Regmask check -- type 12 operands
            if op.type_byte == 12:
                for entry in region_hash:
                    if bittest(op.mask_data[+24], entry.reg):
                        // Register killed by mask -- tombstone it
                        tombstone(entry);                // set to -2
```

The `_bittest` operation on regmask data at offset `+24` identifies which registers are killed by call clobber masks. Killed entries are tombstoned in the tracking hash table (sentinel value `-2`), removing them from further consideration.

### Coalescing and Reassignment Dispatch

The splitting engine dispatches through vtable offsets for coalescing:

```text
        // (e) Coalescing / reassignment (lines 917-999)
        if vtable[1064](this, region):                   // tryReassign
            markRegUsed(result_reg);                     // sub_2E88E20
            goto DONE;

        if vtable[1072](this, region):                   // canRecolorVirtReg
            markRegUsed(result_reg);                     // sub_2E88E20
            goto DONE;

        // Also try alternative local split via vtable[480]
        vtable[480](this, region, &SmallVectorArgs);
```

The vtable-indirect calls at offsets `[1064]` and `[1072]` correspond to `tryReassign` and `canRecolorVirtReg` in upstream LLVM. The offset `[480]` call is a fallback local split strategy. On success, `sub_2E88E20` (markRegUsed) updates the allocation state.

## Register Pressure and the -maxreg Constraint

The real allocation constraint on NVPTX is not register scarcity but register pressure -- higher per-thread register usage reduces [occupancy](../gpu-execution-model.md#register-pressure-and-occupancy), directly impacting throughput through fewer warps available for latency hiding. The `-maxreg` CLI flag (parsed at `sub_900130`, stored at compilation context offset `+1192`) caps the total live register count. Duplicate `-maxreg` definitions produce the error: `"libnvvm : error: -maxreg defined more than once"` (`sub_9624D0`).

### Concrete Occupancy Examples

The occupancy formula and cliff table are documented in the [GPU Execution Model](../gpu-execution-model.md#occupancy-cliffs). Here the relevant values are shown for the `-maxreg` settings that the allocator targets:

| `-maxreg` | Regs/Warp | Warps (SM 8.0) | Occupancy | Warps (SM 9.0) | Occupancy |
|---|---|---|---|---|---|
| 32 | 1,024 | 64 | 100% | 48 | 100% |
| 64 | 2,048 | 32 | 50% | 32 | 67% |
| 96 | 3,072 | 21 | 33% | 21 | 44% |
| 128 | 4,096 | 16 | 25% | 16 | 33% |
| 192 | 6,144 | 10 | 16% | 10 | 21% |
| 255 | 8,160 | 8 | 13% | 8 | 17% |

The `-maxreg` flag sets the ceiling, and the remat infrastructure aggressively reduces pressure below the nearest cliff to avoid losing an entire warp slot.

The `remat-for-occ` knob (default 120) encodes an occupancy target. When set, the IR-level rematerialization pass (`sub_1CE7DD0`) calls `sub_1C01730` to compute an occupancy-based register target. The heuristic applies a scale factor: if the computed occupancy level exceeds 4, it multiplies the target by `3/2` (effectively allowing more registers when occupancy is already high). If the result still exceeds the ceiling, it applies `target = 2*target/3` as a tighter bound.

### ptxas Register Allocation Knobs

In addition to cicc's LLVM-side allocator, **ptxas** has its own register allocation stage with 72+ dedicated knobs. These are independent of the LLVM greedy allocator and operate on the ptxas-internal IR after PTX parsing:

| ptxas Knob | Description |
|---|---|
| `RegAllocRematEnable` | Enable ptxas-level rematerialization |
| `RegAllocEnableOptimizedRemat` | Use optimized remat algorithm |
| `RegAllocSpillForceXBlockHoistRefill` | Force cross-block spill hoist/refill |
| `RegAllocSpillValidateDebug` | Validate spill code in debug builds |
| `RegAllocDebugConflictDetails` | Print conflict details during allocation |
| `RegAllocPrintDetails` | Print allocation decisions |
| `RegAllocPerfDiffBackoff` | Back off allocation when perf difference is small |
| `RegAllocPerfDiffBackoffBegin/End` | Range for perf backoff |
| `CTAReconfigMaxRegAlloc` | Max registers for CTA reconfiguration |
| `MaxRegsForMaxWarp` | Register ceiling for maximum warp occupancy |
| `RegTgtSelHigherWarpCntHeur` | Heuristic favoring higher warp count |
| `RegTgtSelLowerWarpCntHeur` | Heuristic favoring lower warp count |
| `CommonCrossBlockRegLimit` | Cross-block register usage limit |
| `DisableHMMARegAllocWar` | Disable HMMA register allocation workaround |

These ptxas knobs are accessed via `nvcc -Xptxas "--knob KnobName=Value"`. The `MaxRegsForMaxWarp` and `RegTgtSel*` knobs directly implement the occupancy-aware allocation strategy at the ptxas level, complementing cicc's `-maxreg` ceiling.

### NVIDIA Rematerialization Knobs (cicc)

NVIDIA provides an extensive set of custom rematerialization knobs to reduce pressure below the target threshold:

| Knob | Default | Description |
|---|---|---|
| `nv-remat-default-max-reg` | 70 | Default maximum register target |
| `nv-remat-max-times` | 10 | Max rematerialization iterations |
| `nv-remat-block-single-cost` | 10 | Single live pull-in cost limit |
| `nv-remat-block-max-cost` | 100 | Max clone cost for reducing one live |
| `nv-remat-block-loop-cost-factor` | 20 | Loop body cost scaling factor |
| `nv-remat-block-liveout-min-percentage` | 70 | Minimum live-out percentage for block remat |
| `nv-remat-block-map-size-limit` | 6 | Map size limit for block-level remat |
| `nv-remat-block-load-cost` | 10 | Load cost in Remat Machine Block |
| `nv-remat-threshold-for-spec-reg` | 20 | Threshold for special register remat |
| `load-remat` | (flag) | Enable load rematerialization |
| `no-mi-remat` | (flag) | Disable MI remat for specific functions |

The greedy allocator itself has additional tuning knobs:

| Knob | Default | Description |
|---|---|---|
| `split-spill-mode` | 1 | 0=default, 1=size, 2=speed |
| `lcr-max-depth` | 5 | Last chance recoloring max depth |
| `lcr-max-interf` | 8 | Last chance recoloring max interferences |
| `exhaustive-register-search` | (flag) | Bypass LCR depth/interference cutoffs |
| `enable-deferred-spilling` | (flag) | Defer spill code to end of allocation |
| `grow-region-complexity-budget` | 10000 | `growRegion()` edge budget |
| `split-threshold-for-reg-with-hint` | 75 | Split threshold percentage |

Additional rematerialization knobs registered separately include `do-remat` (default 3), `remat-maxreg-ceiling` (default 0), `remat-single-cost-limit` (default 6000), `remat-loop-trip` (default 20), and `remat-for-occ` (default 120, targeting higher occupancy).

## Spill Cost Computation

Spill costs are computed during driver initialization by `sub_2RAD5E0` (step 5 of the driver sequence), which calculates `VirtRegAuxInfo` spill weights for every virtual register before the main allocation loop begins. The spill weight determines priority in the allocation queue and eviction decisions.

On NVPTX, "spilling" is a misnomer because PTX has no stack spill in the traditional CPU sense -- a spilled value either gets rematerialized (re-computed from inputs) or written to local memory (per-thread DRAM-backed memory, orders of magnitude slower than registers). The cost model therefore heavily penalizes local memory spills and strongly favors rematerialization.

The `PriorityAdvisor` (looked up via global `dword_5023AC8`) determines the order in which virtual registers enter the allocation queue. The `EvictionAdvisor` (looked up via `dword_5023BA8`) determines when to evict a lower-priority register to make room for a higher-priority one. Both advisors are initialized via vtable `[24]` calls during driver setup and can be customized via the `regalloc-evict` and `regalloc-priority` analysis passes registered in the pipeline parser.

## Allocation Failure Handler (sub_2F418E0) -- Three Error Paths

When physical register assignment fails (`sub_2F418E0`), three error paths exist:

### Path 1: Empty Allocation Order

```text
"no registers from class available to allocate"
```

The register class has zero allocatable registers. This can happen for the [internal-only class](../reference/register-classes.md#the-internal-only-class----off_4a026e0) (`off_4A026E0`) if the target configuration excludes all environment registers. Diagnostic emitted via `sub_B6EB20` (DiagnosticHandler).

### Path 2: All Registers Occupied

```text
"ran out of registers during register allocation"
```

The allocation order exists but all registers are occupied/interfering. This fires when the eviction/split pipeline exhausts all options -- the sequence is: tryAssign -> tryEviction -> tryLastChanceRecoloring -> trySplit -> fail. Uses `sub_B2BE50` for source location, `sub_B157E0` for `DebugLoc`, and `sub_B158E0` for diagnostic formatting.

### Path 3: Inline Assembly Overflow

```text
"inline assembly requires more registers than available"
```

Special handling for inline asm operands (kind values 1--2 at offset `+68`). Inline assembly can specify explicit register constraints that consume all available registers in a class, leaving nothing for surrounding code.

### FailedRegAlloc Flag

All three paths set the `FailedRegAlloc` flag (bit 10 in `MachineFunction` properties, `sub_2E78A80`). This flag allows downstream passes to handle the failure gracefully rather than crashing. Passes that check this flag can skip optimization or emit degraded but correct code.

## The RAGreedy Driver

The top-level driver (`sub_2F5A640`) orchestrates the full allocation pass:

1. Store `MachineFunction` at `a1[96]`, retrieve `SubTarget` (vtable `+128`).
2. Optional debug dump: `"Before greedy register allocator"`.
3. `sub_35B4B20` -- calculate register class info.
4. `sub_2F55040` -- check if any virtual registers need allocation.
5. `sub_2FAD5E0` -- setup spill costs.
6. `sub_2F54D60` -- compute live intervals.
7. Query vtable `+328` for `getRegPressureSetLimit` (stored at `a1[3633]`).
8. Look up `EvictionAdvisor` (`dword_5023BA8`) and `PriorityAdvisor` (`dword_5023AC8`) via `std::map` lookups.
9. Initialize advisors via vtable `[24]`.
10. Allocate `InterferenceCache` (0x2C0 bytes, `sub_2FB0E40`).
11. Allocate `SplitAnalysis` (0x738 bytes, `sub_2FB1ED0`).
12. `sub_3501A90` -- setup `RegAllocMatrix`.
13. Initialize `PhysRegEntries` array (32 entries, 144-byte stride).
14. `sub_2F55730` -- reset priority queue.
15. `sub_35B5380` -- seed queue from virtual registers.
16. `sub_2F58C00` -- main allocation loop.
17. Optional debug dump: `"Before post optimization"`.
18. Post-allocation optimization via vtable `[24]`.
19. `sub_2F5A580`, `sub_2F50510` -- finalize.

## Differences from Upstream LLVM

The following table summarizes where CICC's register allocator diverges from upstream LLVM 20.0.0 `RAGreedy`:

| Aspect | Upstream LLVM 20 | CICC v13.0 |
|---|---|---|
| **Primary constraint** | Fixed physical register set (CPU ISA-defined) | Pressure ceiling via `-maxreg`; no fixed physical registers |
| **Register classes** | Often overlapping (e.g., GR32 is a subset of GR64 on x86) | 9 completely disjoint classes; no cross-class interference |
| **Spill destination** | Stack frame (cheap, L1/L2 latency) | Local memory (DRAM-backed, 100x+ latency) or rematerialization |
| **Rematerialization** | LLVM built-in `MachineInstr::isRematerializable()` | Massive custom infrastructure: 11+ `nv-remat-*` knobs, separate IR-level remat pass (`sub_1CE7DD0`), iterative pressure reduction loop |
| **Occupancy awareness** | None -- CPU has no occupancy concept | `remat-for-occ` (default 120) drives occupancy-targeted register reduction; `MaxRegsForMaxWarp` ptxas knob |
| **Interference cache hash** | Standard LLVM `DenseMap` with `(ptr >> 4) ^ (ptr >> 9)` | Custom open-addressing map with `37 * reg` hash, `-1`/`-2` sentinels |
| **Operand stride** | 32 bytes (`MachineOperand` size) | 40 bytes (8-byte NVPTX extension for class tag + flags) |
| **Dual pass manager** | Single implementation used by both old and new PM | Two complete copies: Instance A at `0x1EC0400`, Instance B at `0x2F4C2E0` |
| **Register encoding** | LLVM `MCRegister` (16-bit class + index) | 32-bit: 4-bit class tag in `[31:28]`, 28-bit index in `[27:0]` |
| **Spill weight formula** | `length / (spill_cost * block_freq)` | Same formula, but cost model penalizes local memory heavily; rematerialization candidates get near-zero weight |
| **Last-chance recoloring** | Same knobs, but rarely critical | Frequently exercised due to tight `-maxreg` ceilings; `exhaustive-register-search` flag more relevant |
| **Post-RA remat** | Minimal | ptxas performs a second register allocation with its own 72+ knobs (`RegAllocRematEnable`, etc.) |
| **Splitting strategy** | Region-based splitting (`splitAroundRegion`) | Same algorithm, but gap flag (bit 2) and sub-range flag (bit 3) in 40-byte segment entries use NVPTX-specific encoding |
| **Callee-saved registers** | CSR-first-time-cost matters for ABI compliance | NVPTX has no callee-saved convention; `regalloc-csr-first-time-cost` is effectively dead code |
| **Debug strings** | `"Before greedy register allocator"` | Same string, but emitted conditionally on `unk_503FCFD` (a debug flag at a fixed BSS address) |

## What Upstream LLVM Gets Wrong for GPU

Upstream LLVM's register allocation framework was designed for CPU targets where the register file is a fixed, small, physically-interfering resource. Every core assumption breaks on NVPTX:

- **Upstream assumes spills are cheap (L1/L2 latency).** On x86/AArch64, a spill is a store to the stack frame backed by L1 cache (3-5 cycles). On GPU, a "spill" writes to local memory backed by device DRAM at 200-800 cycle latency. This 40-160x penalty makes rematerialization nearly always preferable to spilling, which is why NVIDIA ships 11+ custom `nv-remat-*` knobs and an iterative remat loop that has no upstream equivalent.
- **Upstream assumes a fixed physical register set with cross-class interference.** CPU ISAs have a static register file (e.g., 16 GPRs on x86-64) where GR32 is a sub-register of GR64 and allocating one constrains the other. NVPTX has no fixed register count and its nine register classes are completely disjoint -- allocating `%r5` (Int32Regs) never conflicts with `%f5` (Float32Regs). The entire interference-graph framework is solving the wrong problem.
- **Upstream has no concept of occupancy.** CPU register allocation never reduces parallelism -- a function uses N registers and that is the end of the story. On GPU, every additional register per thread can cross an [occupancy cliff](../gpu-execution-model.md#occupancy-cliffs), losing an entire warp group and halving throughput. The allocator must minimize pressure to a target, not just avoid running out of registers.
- **Upstream assumes one allocation pass produces the final assignment.** On CPU, LLVM's greedy RA emits final machine code. On NVPTX, cicc's allocator emits PTX with virtual registers bounded by `-maxreg`, and then ptxas performs an entirely separate second allocation pass with its own 72+ knobs to map virtual PTX registers to hardware resources. The LLVM allocator is half the pipeline, not the whole thing.
- **Upstream's callee-saved register convention is irrelevant.** CPU ABIs define callee-saved sets (e.g., `rbx`, `rbp` on SysV x86-64) that the allocator must respect. NVPTX has no callee-saved convention at all -- there is no hardware call stack for registers. The `regalloc-csr-first-time-cost` knob is dead code on this target.

## Common Pitfalls

These are mistakes a reimplementor is likely to make when building a register allocator for an NVPTX-like GPU target.

**1. Treating register allocation as an assignment problem instead of a pressure problem.** On CPU targets, the allocator must map N virtual registers to K physical registers, and the problem is coloring a fixed interference graph. On NVPTX, there is no fixed physical register file -- PTX registers are virtual and unlimited. The real constraint is the `-maxreg` ceiling, which controls occupancy. A reimplementation that tries to assign physical registers will produce correct but meaningless output; the correct approach is to minimize peak live register count below the `-maxreg` threshold, and let ptxas handle the final hardware mapping.

**2. Ignoring occupancy cliffs when setting the register target.** Going from 64 to 65 registers per thread crosses an occupancy cliff that halves the number of active warps on SM 8.0 (from 32 warps at 50% to 21 warps at 33%). A reimplementation that treats the register ceiling as a hard binary constraint (under = good, over = bad) will miss the fact that reducing from 65 to 64 is worth enormous effort (doubles throughput), while reducing from 63 to 62 is nearly worthless. The `remat-for-occ` knob (default 120) exists specifically to drive rematerialization toward the nearest cliff boundary, not just toward the ceiling.

**3. Using CPU-calibrated spill costs.** On x86, a spill is a store to L1-cached stack memory at 3-5 cycle latency. On GPU, a "spill" writes to per-thread local memory backed by device DRAM at 200-800 cycle latency -- a 40-160x penalty. A reimplementation that uses upstream LLVM's default spill cost formula without recalibrating for GPU memory latency will spill aggressively when it should rematerialize. NVIDIA's 11+ `nv-remat-*` knobs and the iterative rematerialization loop exist because rematerialization is almost always cheaper than spilling on GPU.

**4. Assuming cross-class register interference exists.** NVPTX's nine register classes are completely disjoint: `Int32Regs` (`%r`) never conflicts with `Float32Regs` (`%f`), `Int64Regs` (`%rd`) never conflicts with `Float64Regs` (`%fd`), and so on. A reimplementation that builds a global interference graph spanning all classes will waste significant compile time computing interference relationships that are always empty. The correct approach is per-class allocation with independent pressure tracking.

**5. Forgetting that cicc's allocation is only half the pipeline.** The LLVM greedy allocator in cicc emits PTX with virtual registers bounded by `-maxreg`. Then ptxas performs an entirely separate second allocation pass with its own 72+ knobs to map virtual PTX registers to hardware resources. A reimplementation that tries to produce final hardware register assignments at the LLVM level is solving the wrong problem -- the output should be well-pressure-managed virtual registers, not hardware assignments.

## Diagnostic Strings

Diagnostic strings recovered from the register allocation binary region (`p2c.5-01-register-alloc.txt`) and the rematerialization passes (`p2b.2-01-remat-ir.txt`, `p2b.2-02-remat-machine.txt`).

### Allocation Failure Diagnostics

| String | Source | Category | Trigger |
|--------|--------|----------|---------|
| `"no registers from class available to allocate"` | `sub_2F418E0` path 1 | Error | Register class has zero allocatable registers; emitted via `sub_B6EB20` (DiagnosticHandler) |
| `"ran out of registers during register allocation"` | `sub_2F418E0` path 2 | Error | All registers occupied/interfering after tryAssign -> tryEviction -> tryLastChanceRecoloring -> trySplit exhausted |
| `"inline assembly requires more registers than available"` | `sub_2F418E0` path 3 | Error | Inline asm explicit register constraints consume all available registers in a class |
| `"libnvvm : error: -maxreg defined more than once"` | `sub_9624D0` | Error | Duplicate `-maxreg` CLI flag definitions |

### Debug/Trace Diagnostics

| String | Source | Category | Trigger |
|--------|--------|----------|---------|
| `"Before greedy register allocator"` | `sub_2F5A640` step 2 | Debug | Conditional on `unk_503FCFD` debug flag |
| `"Before post optimization"` | `sub_2F5A640` step 17 | Debug | Post-allocation debug dump |
| `"Before register coalescing"` | `sub_2F60C50` | Debug | Register coalescer debug dump |
| `"After register coalescing"` | `sub_2F60C50` | Debug | Register coalescer debug dump |

### Rematerialization Diagnostics (nv-remat-block)

| String | Source | Category | Trigger |
|--------|--------|----------|---------|
| `"Skip machine-instruction rematerialization on <name>"` | `sub_1CE7DD0` region | Debug | Function name matches `no-mi-remat` skip list |
| `"Max-Live-Function(<num_blocks>) = <max_live>"` | remat-block step 10 | Debug | Reports maximum live register count across all blocks |
| `"live-out = <count>"` | remat-block step 7 | Debug | Per-block live-out register count |
| `"Pullable: <count>"` | remat-block step 5 | Debug | Number of pullable (rematerializable) instructions |
| `"Total Pullable before considering cost: <count>"` | remat-block step 8 | Debug | Total pullable candidates before cost filtering |
| `"Really Final Pull-in: <count> (<total_cost>)"` | remat-block step 11 | Debug | Final rematerialization candidate count and total cost |
| `"After pre-check, <N> good candidates, <M> given second-chance"` | remat two-phase selection | Debug | Two-phase candidate selection with second-chance |
| `"ADD <N> candidates from second-chance"` | remat two-phase selection | Debug | Candidates recovered from second-chance pass |
| `"\treplaced"` | remat code emission | Debug | Rematerialized instruction replacement confirmation |

### Pass Registration Strings

| String | Source |
|--------|--------|
| `"Greedy Register Allocator"` | Pass name for both Instance A (`0x1EC0400`) and Instance B (`0x2F4C2E0`) |
| `"Register Coalescer"` | `sub_2F60C50` pass registration |
| `"nv-remat-block"` | `ctor_361_0` at `0x5108E0` -- machine-level remat pass registration |
| `"Legacy IR Remat"` | `sub_1CE7DD0` region -- IR-level remat pass display name |
| `"nvvmrematerialize"` | IR-level remat pass pipeline ID |

## Function Map

| Function | Address | Size | Role |
|---|---|---|---|
| `RAGreedy::runOnMachineFunction` | `sub_2F5A640` | -- | Top-level driver (466 lines) |
| `RAGreedy::selectOrSplit` | `sub_2F49070` | -- | Core allocator (82KB, 2,314 lines) |
| selectOrSplit thunk | `sub_2F4BAF0` | -- | Redirects to `sub_2F49070(this+200)` |
| selectOrSplit + SplitEditor | `sub_2F4BB00` | -- | Spill-or-split path |
| `SplitEditor::splitAroundRegion` | `sub_2F2D9F0` | -- | Live range splitting (93KB) |
| tryLocalSplit | `sub_2F2A2A0` | -- | Local split within single BB |
| materializeSplitSegment | `sub_2FDF330` | -- | Insert split segments |
| scanInterference | `sub_2F43DC0` | -- | Populate interference cache |
| tryAssign | `sub_2F47B00` | -- | Simple assignment path |
| tryEviction | `sub_2F48CE0` | -- | Evict conflicting VReg |
| tryLastChanceRecoloring | `sub_2F46530` | -- | Recursive recoloring fallback |
| processConstrainedCopies | `sub_2F47200` | -- | Handle tied-operand COPYs |
| rehashInterferenceTable | `sub_2F46EE0` | -- | Interference cache rehash |
| rehashCoalescingTable | `sub_2F46A90` | -- | Coalescing hint table rehash |
| markRegReserved | `sub_2F42840` | -- | Mark unit as reserved |
| recordAssignment | `sub_2F42240` | -- | Record successful assignment |
| updateInterferenceCache | `sub_2F424E0` | -- | Insert conflict entry |
| recordCoalescingHint | `sub_2F41240` | -- | Record parent-chain hint |
| collectHintInfo | `sub_2F434D0` | -- | Gather all hints for priority |
| assignRegFromClass | `sub_2F418E0` | -- | Allocation failure handler |
| hasVRegsToAllocate | `sub_2F55040` | -- | Pre-flight check |
| computeLiveIntervals | `sub_2F54D60` | -- | Build live interval data |
| resetPriorityQueue | `sub_2F55730` | -- | Clear and re-init queue |
| mainAllocationLoop | `sub_2F58C00` | -- | Per-VReg dispatch loop |
| finalize | `sub_2F50510` | -- | Post-allocation cleanup |
| setupSpillCosts | `sub_2FAD5E0` | -- | Compute VirtRegAuxInfo weights |
| `InterferenceCache::init` | `sub_2FB0E40` | -- | Allocate 0x2C0-byte cache |
| `SplitAnalysis::init` | `sub_2FB1ED0` | -- | Allocate 0x738-byte analysis |
| setupRegAllocMatrix | `sub_3501A90` | -- | Build the global interference matrix |
| calculateRegClassInfo | `sub_35B4B20` | -- | Pre-compute class sizes/orders |
| seedQueueFromVRegs | `sub_35B5380` | -- | Initial queue population |
| `RegisterCoalescer::runOnMachineFunction` | `sub_2F71140` | -- | Register coalescing (80KB) |
| printMachineProperties | `sub_2E78A80` | -- | Includes `FailedRegAlloc` flag |
| encodeVirtualReg | `sub_21583D0` | -- | `CLASS_BITS \ |
| emitCopyInstruction | `sub_2162350` | -- | Class-specific copy opcodes |

## Reimplementation Checklist

1. **Pressure-driven allocation model.** Replace the standard assignment-to-physical-registers model with a pressure-tracking model: PTX registers are virtual, so the allocator must track and bound total live register count per class against the `-maxreg` ceiling (default 70) rather than assigning to a finite physical register set.
2. **Nine disjoint register classes.** Define the nine NVPTX register classes (Int1Regs, Int16Regs, Int32Regs, Int64Regs, Float32Regs, Float64Regs, Int16HalfRegs, Int32HalfRegs, Int128Regs) with complete cross-class disjointness -- no interference between classes, class-specific copy opcodes, and per-class pressure tracking.
3. **Greedy selectOrSplit with NVPTX adaptations.** Implement the core allocation loop: per-unit RegUnitStates array (free/interfering/reserved), interference cache with `37 * reg` hash, 40-byte-stride operand scanning, copy coalescing hints (kinds 20/21), and live-through bitvector for detecting worst-case live ranges.
4. **Live range splitting with SplitKit.** Implement `splitAroundRegion` (93KB equivalent): identify split points at block boundaries and within blocks, create sub-ranges with new virtual registers, insert copies at split points, and update the interference cache.
5. **Eviction and last-chance recoloring.** Implement `tryEviction` (compare spill weights to decide whether evicting a conflicting VReg is cheaper) and `tryLastChanceRecoloring` (recursive reassignment bounded by `lcr-max-depth=5` and `lcr-max-interf=8`).
6. **Occupancy-aware spill cost computation.** Weight spill costs by occupancy impact: spills to local memory (device DRAM, 200--800 cycle latency) must account for the GPU-specific penalty, and the register ceiling must respect occupancy cliff boundaries.
7. **Dual pass manager instances.** Register the allocator for both legacy and new pass managers, ensuring both instances share the same NVPTX-specific hooks (custom rematerialization interaction, pressure-driven priority queues, `maxreg` enforcement).

## Architectural Uniqueness

NVPTX's register allocation differs from all other LLVM targets in several fundamental ways:

- **Unlimited virtual registers**: PTX has no fixed register count. The allocator manages pressure, not assignment to a finite set of physical registers.
- **Complete class separation**: The nine register classes are fully disjoint. An `Int32Regs` allocation never conflicts with a `Float32Regs` allocation.
- **Pressure as the primary constraint**: The `-maxreg` ceiling and NVIDIA's custom rematerialization infrastructure (`nv-remat-*` knobs) exist specifically to control occupancy, which has no equivalent in CPU register allocation.
- **Two-stage allocation**: cicc performs LLVM greedy RA to emit PTX with virtual registers bounded by `-maxreg`, then ptxas performs a second allocation pass with its own 72+ knobs to map virtual PTX registers to hardware resources.
- **Dual implementation**: Two complete RA copies exist (old at `0x1E*`--`0x1F*`, new at `0x2F*`--`0x35*`), one per pass manager generation.

## ptxas Interaction

Register allocation in cicc is the first of two allocation stages. cicc's greedy RA assigns virtual PTX registers (`%r0`, `%f3`, etc.) bounded by the `-maxreg` ceiling to control occupancy, but these are not hardware registers -- they are symbolic names in the PTX text. `ptxas` then performs its own complete register allocation pass, mapping cicc's virtual registers onto the SM's physical register file (e.g., 255 32-bit registers per thread on SM 80+). ptxas has 72+ RA-related knobs (`RegAllocScheme`, `DynamicRegAlloc`, `RegUsageOpt`, etc.) and may split, coalesce, or spill registers differently than cicc anticipated. The `-maxreg` value cicc enforces serves as a hint to ptxas about the desired occupancy target, but ptxas makes the final hardware binding decision.
