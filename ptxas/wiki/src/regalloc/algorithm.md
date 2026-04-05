# Fat-Point Allocation Algorithm

The ptxas register allocator uses a fat-point greedy algorithm. For each virtual register, it scans a per-physical-register pressure array, picks the slot with the lowest interference count, and commits the assignment. There is no graph coloring, no simplify-select-spill loop, and no worklist -- just two 512-DWORD pressure histograms and a linear scan. This page documents the algorithm in full detail: pressure array construction, constraint evaluation, register selection, assignment propagation, the retry loop, and the supporting knobs.

| | |
|---|---|
| **Core allocator** | `sub_957160` (1658 lines) -- fat-point coloring engine |
| **Pressure builder** | `sub_957020` -- populates primary and secondary arrays |
| **Interference builder** | `sub_926A30` (4005 lines) -- constraint solver |
| **Assignment** | `sub_94FDD0` (155 lines) -- write physical reg, propagate aliases |
| **Pre-allocation** | `sub_94A020` (331 lines) -- pre-assign high-priority operands |
| **Retry driver** | `sub_971A90` (355 lines) -- NOSPILL then SPILL retry loop |
| **Best result recorder** | `sub_93D070` (155 lines) -- compare and keep best attempt |
| **Entry point** | `sub_9721C0` (1086 lines) -- per-function allocation driver |

## Pressure Array Construction

The core allocator (`sub_957160`) allocates two stack-local arrays at the start of each allocation round. Each array is 2056 bytes: 512 DWORDs (2048 bytes) of pressure data plus a 2-DWORD sentinel.

| Array | Variable | Role |
|-------|----------|------|
| Primary | `v12` | Per-physical-register interference count. Lower is better. |
| Secondary | `v225` | Per-physical-register secondary cost. Breaks ties when primary values are equal. |

Both arrays are zeroed using SSE2 vectorized `_mm_store_si128` loops aligned to 16-byte boundaries. The zeroing loop processes 128 bits per iteration, covering 512 DWORDs in approximately 128 iterations.

For each virtual register in the allocation worklist (linked list at `alloc+744`), the allocator calls `sub_957020` to populate the pressure arrays. This function walks the virtual register's constraint list (`vreg+144`) and, for each constraint, increments the appropriate pressure array entries at the physical register slots that conflict with the current virtual register. The result is a histogram: `primary[slot]` holds the total interference weight for physical register `slot`, accumulated over all constraints of all previously-assigned virtual registers that conflict with the current one.

The secondary array accumulates a separate cost metric used for tie-breaking. It captures weaker interference signals -- preferences and soft constraints that do not represent hard conflicts but indicate suboptimal placement.

### Budget Computation

Before the pressure scan begins, the allocator computes the maximum physical register count for the current class:

```
v231 = hardware_limit + 7                   // alloc+756, with headroom
if allocation_mode == 6 (CSSA paired):
    v231 *= 4                               // quad range for paired allocation
elif allocation_mode == 3 (CSSA):
    v231 *= 2                               // doubled range
alloc.budget = v231                         // stored at alloc+60
```

The hardware limit comes from the target descriptor and reflects the physical register file size for the current class (e.g. 255 for GPRs, 7 for predicates). The `+7` headroom allows the allocator to explore slightly beyond the architectural limit before triggering a hard failure -- this is clamped during assignment by the register budget check in `sub_94FDD0`.

The register budget at `alloc+1524` interacts with `--maxrregcount` and `--register-usage-level` (values 0--10). The CLI-specified maximum register count is stored in the compilation context and propagated to the allocator as the hard ceiling. The `register-usage-level` option modulates the target: level 0 means no restriction, level 10 means minimize register usage as aggressively as possible. The per-class register budget stored at `alloc+32*class+884` reflects this interaction.

## Constraint Types

The fat-point interference builder (`sub_926A30`, 4005 lines) processes constraints attached to each virtual register. Constraints are extracted from instruction operand descriptors encoded as 32-bit values: bits 28--30 encode the operand type, bits 0--23 encode the register index, bit 24 is the pair extension bit, and bit 31 is a sign/direction flag.

The builder recognizes 15 constraint types. Each constraint type adds interference weight to specific physical register slots in the pressure arrays:

| Type | Name | Pressure effect |
|------|------|-----------------|
| 0 | Point interference | Adds weight to specific physical register slots that are live at the same program point as this VR. The most common constraint -- represents a simple "these two VRs cannot share a physical register because both are live at instruction I." |
| 1 | Exclude-one | Adds weight to exactly one physical register slot, excluding it from consideration. Used when a specific physical register is reserved (e.g. for ABI constraints or hardware requirements). |
| 2 | Exclude-all-but | Adds weight to all slots *except* one. Forces the VR into a single permitted physical register. Used for fixed-register operands (e.g. `R0` for return values). |
| 3 | Below-point | Adds interference weight for registers live below (after) the current program point. Captures downward-exposed liveness -- the VR must avoid physical registers that are used by later instructions. |
| 4 | (reserved) | Not observed in common paths. |
| 5 | Paired-low | Constrains the VR to an even-numbered physical register. Used for the low half of a 64-bit register pair. The pressure builder increments only even-indexed slots. |
| 6 | Paired-high | Constrains the VR to an odd-numbered physical register (the slot immediately after its paired-low partner). Increments only odd-indexed slots. |
| 7 | Aligned-pair | Constrains a pair of VRs to consecutive even/odd physical registers simultaneously. Combines the effects of types 5 and 6. |
| 8 | Phi-related | Marks interference from CSSA phi instructions (opcode 195). Phi constraints are softer -- they add lower weight because the phi can potentially be eliminated by the coalescing pass. |
| 9 | (reserved) | Not observed in common paths. |
| 10 | (reserved) | Not observed in common paths. |
| 11 | Paired-even-parity | Constrains the VR to a physical register whose index has even parity with respect to a bank partition. Used for bank-conflict avoidance on architectures where register bank is determined by `reg_index % N`. |
| 12 | Paired-odd-parity | Constrains to odd parity within the bank partition. |
| 13 | Paired-parity-group | Constrains a group of VRs to compatible parity assignments across a bank. |
| 14 | Paired-parity-extended | Extended variant of parity constraints for wider register groups (quads). |
| 15 | Range | Adds interference over an interval of program points rather than a single point. Represents a VR whose live range spans multiple instructions and conflicts with another VR whose live range overlaps. The weight is proportional to the overlap length. |

The builder uses FNV-1a hashing (seed `0x811C9DC5`, prime `16777619`) for hash-table lookups into the pre-allocation candidate table. It contains SSE2-vectorized inner loops (`_mm_add_epi64`) for bulk interference weight accumulation when processing large constraint lists. The builder dispatches through 7+ vtable entries for OCG knob queries that modulate constraint weights.

### Constraint List Structure

Each virtual register carries a constraint list at `vreg+144`. The list is a linked chain of constraint nodes, each containing:

- Constraint type (one of the 15 types above)
- Target VR or physical register index
- Weight (integer, typically 1 for hard constraints, lower for soft)
- Program point or interval (for types 0, 3, 15)
- Pair/alignment specification (for types 5--7, 11--14)

The interference builder iterates this list for every VR being assigned, accumulating weights into the pressure arrays. The total cost of assignment to slot `S` is the sum of all constraint weights that map to `S`.

## Register Selection

After the pressure arrays are populated for a given VR, the allocator scans physical register candidates and selects the one with minimum cost:

```
function select_register(primary[], secondary[], maxRegs, threshold, pair_mode):
    best_slot = -1
    best_primary_cost = MAX_INT
    best_secondary_cost = MAX_INT

    stride = 1
    if pair_mode != 0:
        stride = 2 << shift              // 2 for pairs, 4 for quads

    for slot in range(0, maxRegs, stride):
        if primary[slot] > threshold:     // knob 684, default 50
            continue                      // skip congested slots

        p = primary[slot]
        s = secondary[slot]

        if p < best_primary_cost:
            best_slot = slot
            best_primary_cost = p
            best_secondary_cost = s
        elif p == best_primary_cost and s < best_secondary_cost:
            best_slot = slot
            best_secondary_cost = s

    return best_slot                      // -1 if nothing found
```

Key design decisions in the selection loop:

**Threshold filtering.** The interference threshold (OCG knob 684, default 50) acts as a congestion cutoff. Any physical register slot with total interference weight above this value is immediately skipped. This prevents the allocator from assigning a VR to a slot that would cause excessive register pressure, even if that slot happens to be the global minimum. The threshold trades a small increase in the number of spills for a significant improvement in allocation quality -- high-interference slots tend to require cascading reassignments.

**Alignment stride.** For paired registers (pair mode 1 or 3 in `vreg+48` bits 20--21), the scan steps by 2 instead of 1, ensuring the VR lands on an even-numbered slot. For quad-width registers, the stride is 4. The shift amount comes from the register class descriptor and varies by allocation mode.

**Two-level tie-breaking.** When two candidates have equal primary cost, the secondary array breaks the tie. This provides a smooth gradient for the allocator to follow when the primary interference picture is flat. The secondary array typically captures weaker signals like register preference hints, pre-allocation suggestions, and copy-related affinities.

**No backtracking.** The selection is final once made. There is no local search, no Kempe-chain swapping, and no reassignment of previously-colored VRs. If the selection leads to a spill later, the retry loop (see below) handles it by rerunning the entire allocation with updated spill guidance.

## Assignment: sub\_94FDD0

Once a physical register slot is selected, `sub_94FDD0` (155 lines) commits the assignment. This function handles four cases:

### Case 1: Normal Assignment

The physical register number is written to `vreg+68`. The register consumption counter (`sub_939CE0`, 23 lines) computes how many physical slots this VR occupies:

```
consumption = slot + (1 << (pair_mode == 3)) - 1
```

For single registers, this is just `slot`. For double-width pairs (pair_mode 3), it is `slot + 1`, consuming two consecutive physical registers. The peak usage trackers at `alloc+1528` and `alloc+1564` are updated if `consumption` exceeds the current maximum.

### Case 2: Predicate Half-Width

For predicate registers (class 2, type 3), the allocator performs a half-width division. The physical slot is divided by 2, and the odd/even bit is stored at `vreg+48` bit 23 (the `0x800000` flag):

```
physical_reg = slot / 2
if slot is odd:
    vreg.flags |= 0x800000    // hi-half of pair
else:
    vreg.flags &= ~0x800000   // lo-half of pair
```

This maps two virtual predicate registers to one physical predicate register, since NVIDIA's predicate register file supports sub-register addressing (each physical predicate holds two 1-bit values).

### Case 3: Over-Budget / Spill Trigger

If `slot >= regclass_info.max_regs` and the VR is not already marked as spilled (flag `0x4000` at `vreg+48`), the allocator sets the needs-spill flag:

```
vreg.flags |= 0x40000         // needs-spill flag (bit 18)
```

When the needs-spill flag is later detected, the allocator calls:
1. `sub_939BD0` -- spill allocator setup (selects bucket size, alignment, max based on knob 623 and cost threshold at `alloc+776`)
2. `sub_94F150` -- spill code generation (561 lines, emits spill/reload instructions)

The spill cost is accumulated:
```
alloc.total_spill_cost += vreg.spill_cost     // double at alloc+1568
alloc.secondary_cost   += vreg.secondary_cost  // float at alloc+1576
```

### Case 4: Alias Chain Propagation

After writing the physical register, the function follows the alias chain at `vreg+36` (coalesced parent pointer). Every VR in the chain receives the same physical assignment:

```
alias = vreg.alias_parent                    // vreg+36
while alias != NULL:
    alias.physical_register = slot           // alias+68
    alias = alias.alias_parent               // alias+36
```

This propagation ensures that coalesced registers (merged by the coalescing pass at `sub_9B1200`) share a single physical register without requiring the allocator to re-derive the relationship.

### Pre-Allocated Candidate Check

Before committing a normal assignment, `sub_94FDD0` calls `sub_950100` (205 lines) to check if the VR has a pre-allocated candidate in the hash table at `alloc+248`. If a candidate exists (FNV-1a keyed lookup), the pre-assigned physical register is used instead of the one selected by the pressure scan. For paired registers, the pre-assigned slot is doubled (`type 1 -> slot * 2`) to account for pair stride.

## Pre-Allocation Pass: sub\_94A020

Before the core allocator runs, the pre-allocation pass (331 lines) optionally assigns physical registers to high-priority operands. This pass is gated by three knobs:

| Knob | Role |
|------|------|
| 628 | Enable pre-allocation pass |
| 629 | Enable coalescing-aware pre-allocation |
| 618 | Enable uniform register pre-allocation |

When enabled and the allocation mode is 3, 5, or 6, the pass:

1. Clears the pre-allocation candidate hash tables at `alloc+240..336` (six tables covering candidates, results, and overflow).
2. Iterates basic blocks calling `sub_9499E0` (per-block scanner, 304 lines) to identify pre-assignment opportunities.
3. For each eligible instruction, calls `sub_93ECB0` (194 lines) to pre-assign operands.

`sub_93ECB0` iterates instruction operands in reverse order (last to first). It filters: operands must be type 1 (register), index not 41--44 (architectural predicates) or 39 (special). A switch on the masked opcode determines how many operands qualify: opcode 22 dispatches to `sub_7E40E0`, opcode 50 uses a lookup table, opcodes 77/83/110--112/279/289/297/352 each have dedicated handlers. The function calls `sub_93E9D0` with a priority level determined by OCG knob 646:

| Priority | Meaning |
|----------|---------|
| 1 | Pre-assign read operands only |
| 2 | Pre-assign write operands only |
| 3 | Pre-assign both read and write operands |

`sub_93E9D0` (125 lines) creates a spill candidate node via `sub_93E290` (allocates 192-byte structures from the arena freelist at `alloc+232`), marks the live range via `sub_93DBD0` (356 lines), and recursively processes dependent operands via `sub_93EC50`.

## Retry Loop: sub\_971A90

The per-class allocation driver (355 lines) wraps the core allocator in a two-phase retry loop.

### Phase 1: NOSPILL

The first attempt runs the core allocator without spill permission. The debug log emits:

```
"-CLASS NOSPILL REGALLOC: attemp N, used M, target T"
```

(Note: "attemp" is a typo present in the binary.)

The call sequence for each NOSPILL attempt:

```
sub_93FBE0(alloc, ctx, iteration)       // reset state for attempt
if iteration == 0:
    sub_956130(alloc, class)            // build interference masks (first attempt only)
result = sub_957160(alloc, ctx, iteration)  // core fat-point allocator
sub_93D070(&best, class, iteration,         // record best result
           result, pressure, alloc, cost)
```

The NOSPILL loop runs up to `v102` attempts. Retry mode selection (from `sub_971A90` lines 199--240):

| Condition | v102 (max attempts) | Behavior |
|-----------|---------------------|----------|
| Knob 638 enabled + special mode | 0 | No allocation at all |
| Knob 638 enabled, knob 639 set | knob 639 value | Custom iteration count |
| Knob 638 enabled, knob 639 unset | 1 | Single attempt |
| Knob 638 disabled, pressure low | 2 | Standard 2-attempt retry |
| Knob 638 disabled, pressure high | 0 | Skip to spill |

Exit conditions within the NOSPILL loop:
- `target >= adjusted_result`: allocation fits within budget (success)
- `target >= result`: no improvement possible between iterations (give up)
- The best-result recorder (`sub_93D070`) compares the current attempt against the best seen so far using a multi-criterion ranking: register count first, then cost (double at `best+56`), then spill count, then class width. It uses `128 / register_count` as an inverse density metric.

### Phase 2: SPILL

If all NOSPILL attempts fail, the driver invokes spill guidance:

```
guidance = sub_96D940(ctx, guidance_array, attempt_no)   // 2983 lines
```

The spill guidance function builds priority queues of spill candidates for each of the 7 register classes. Each guidance entry is an 11112-byte working structure containing 128-element bitmask arrays. The function contains 7 near-identical code blocks (one per class), likely unrolled from a C++ template.

After spill guidance, a final allocation attempt runs via `sub_9714E0` (finalize/spill). If this also fails, `sub_936FD0` (fallback allocation) makes a last-ditch effort. If that fails too, register assignments are cleared to `-1` and the allocator reports:

```
"Register allocation failed with register count of '%d'.
 Compile the program with a higher register target"
```

### SMEM Spill Activation

For allocation modes 3 or 6 when the compilation target is device type 5, shared-memory spilling is activated before the retry loop:

```
if (class == 3 || class == 6) and device_type == 5:
    if num_variables > 0:
        sub_939BD0(alloc)                  // spill allocator setup
        sub_94F150(alloc, ctx, 1)          // spill codegen to SMEM
    alloc.spill_triggered = 1              // flag at alloc+865
```

This path generates spill/reload instructions targeting shared memory instead of local memory, which is faster but limited in size and shared across the CTA.

## Per-Class Iteration

The top-level entry point (`sub_9721C0`, 1086 lines) drives allocation for all register classes sequentially:

```
for class_id in 1..6:
    if class_list[class_id] is empty:
        continue
    alloc.current_class = class_id          // alloc+376
    while sub_971A90(alloc, ctx, class_id) != 0:
        sub_8E3A80(alloc+2)                 // arena cleanup between attempts
```

Classes 1--6 are initialized via the target descriptor vtable at offset `+896`. The vtable call `vtable[896](alloc_state, class_id)` populates per-class register file descriptors at `alloc[114..156]` (four 8-byte entries per class). The class IDs correspond to `reg_type` values (1 = R, 2 = R alt, 3 = UR, 4 = UR ext, 5 = P/UP, 6 = Tensor/Acc). Barrier registers (`reg_type = 9`) are above the `<= 6` cutoff and handled separately.

| Class ID | Name | Type | File size | Description |
|----------|------|------|-----------|-------------|
| 1 | R | GPR | up to 255 | General-purpose 32-bit registers |
| 2 | P | pred | up to 7 | Predicate registers |
| 3 | B | bar | up to 16 | Barrier registers |
| 4 | UR | ugpr | up to 63 | Uniform general-purpose registers |
| 5 | UP | upred | up to 7 | Uniform predicate registers |
| 6 | UB | ubar | up to 16 | Uniform barrier registers |

Class 0 (unified/cross-class) is skipped in the main loop. It is used for cross-class constraint propagation during the interference building phase. Classes 3 (UR) and 6 (Tensor/Acc) have early-out conditions: if `alloc+348 == 2` (class 3) or `alloc+332 == 2` (class 6), allocation is skipped because no VRs of that class exist.

Before the per-class loop, virtual registers are distributed into class-specific linked lists (lines 520--549 of `sub_9721C0`):

```
for each vreg in function_vreg_list:       // from ctx+104
    if vreg.id in {41, 42, 43, 44}:        // skip architectural predicates
        continue
    class = vreg.register_class             // vreg+12
    if class >= 1 and class <= 6 and vreg.type != 0:
        insert(class_lists[class], vreg)
```

The VR list is sorted by priority (`sub_9375C0`) before distribution. Priority ordering ensures that VRs with more constraints and higher spill costs are allocated first, giving them first pick of the register file.

## Fast Register Allocation: Knob 638

Knob 638 (`register pressure analysis enable` / fast allocation mode) controls a single-pass no-retry allocation path. When enabled with the special mode flag set, the allocator sets `v102 = 0`, meaning the NOSPILL retry loop body never executes. Allocation proceeds directly to spill handling without iterating.

When knob 638 is enabled without the special mode flag:
- The iteration count is set to 1 (or the value of knob 639 if set)
- This creates a limited-retry mode where the allocator makes at most `knob_639` attempts
- Each attempt still uses the full fat-point algorithm but with no fallback to the multi-attempt guidance-driven loop

This mode is intended for fast compilation (`--fast-compile`) where compilation time matters more than register allocation quality. The allocator accepts the first viable assignment rather than searching for an optimal one.

## Interference Builder: sub\_926A30

The interference builder (4005 lines) is the largest single function in the allocator. It constructs the constraint lists that feed the pressure arrays. For each basic block and each instruction within it, the builder:

1. Iterates instruction operands. Each operand is a 32-bit descriptor:
   - Bits 27--25: operand type (1 = register, 6 = special, 7 = immediate)
   - Bits 23--0: register/variable ID
   - Bit 31: sign/direction flag
   - Bit 24: pair extension bit
2. For register operands (type 1), extracts the VR ID and looks up the VR object.
3. Determines the constraint type based on the operand's role (def, use, or both), the instruction's properties, and the VR's pair mode.
4. Creates a constraint node and appends it to the VR's constraint list.
5. For paired registers (type 3 in the operand descriptor), generates two constraints: one for the low half and one for the high half (distinguished by bit 23).
6. Uses SSE2 vectorized loops for bulk weight accumulation when processing large basic blocks with many live registers.

The builder queries multiple OCG knobs via vtable dispatches at offsets +72, +120, +152, +224, +256, +272, and +320. These knobs modulate constraint weights and enable/disable specific constraint categories (e.g. bank-conflict-aware constraints are gated by knob 641).

Special register IDs 41--44 (PT, P0--P3) and 39 are always skipped. The skip predicate (`sub_9446D0`, 29 lines) additionally checks for CSSA phi instructions (opcode 195 with type 9 = barrier) and performs hash-table lookups in the exclusion set at `alloc+360`.

## Best Result Recorder: sub\_93D070

The best-result recorder (155 lines) compares the current allocation result against the best seen across all retry attempts. It maintains state at offsets `best[10..20]`:

```
best[10] = register_count                   // best count so far
best[13] = 128 / register_count             // inverse density metric
best[16] = max_pressure                     // peak live registers
best[17] = spill_score
*(double*)(best + 56) = cost                // floating-point cost metric
best[18] = arch_peak_1                      // from architecture state +408
best[20] = arch_peak_2                      // from architecture state +400
```

Comparison uses lexicographic ordering:
1. Lower register count wins
2. On tie: lower cost (double) wins
3. On tie: lower spill count wins
4. On tie: lower class width wins

When the current attempt improves over the best, the recorder allocates a per-register assignment array and copies the full VR-to-physical-register mapping for later restoration.

## Per-Instruction Assignment: sub\_9680F0

The per-instruction assignment core loop (3722 lines, the largest function in part 2 of the allocator) handles the actual instruction-by-instruction walk during allocation:

1. Iterates instructions via linked list (`v87 = *(_QWORD *)v87`)
2. For each instruction, calls `sub_961A60` to attempt register assignment
3. Tracks register pressure via `v86` counter and 256-bit bitvectors at `alloc+1342..1350`
4. Manages three bitvector masks per instruction: assigned, must-not-spill, and used
5. Detects rematerialization opportunities (flag `v570`) and calls `sub_93AC90`
6. Detects bank conflicts via `sub_9364B0` and resolves them
7. Handles special opcodes: 187 (LOAD), 97 (STORE), 52 (BRANCH), 236 (CALL)
8. Tracks first-spill-candidate (`alloc+1354`) and fallback-spill-candidate (`alloc+1355`)
9. On allocation failure for an instruction, calls `sub_96CE90` which recursively invokes `sub_9680F0` with different flags for the spill fallback path

## Function Map

| Address | Lines | Role |
|---------|-------|------|
| `sub_926A30` | 4005 | Fat-point interference builder / constraint solver |
| `sub_93D070` | 155 | Best result recorder (multi-criterion comparison) |
| `sub_93E290` | 397 | Spill candidate node creator (192-byte arena alloc) |
| `sub_93E9D0` | 125 | Pre-assign individual operand |
| `sub_93ECB0` | 194 | Pre-assign registers (per-instruction dispatcher) |
| `sub_93FBE0` | 940 | Per-iteration allocation state reset |
| `sub_939CE0` | 23 | Register consumption counter (pair-aware) |
| `sub_9446D0` | 29 | Register skip predicate (special regs, exclusion set) |
| `sub_94A020` | 331 | Pre-allocation pass (knobs 628/629/618) |
| `sub_94FDD0` | 155 | Register assignment + alias propagation |
| `sub_950100` | 205 | Pre-allocated candidate applier (FNV-1a lookup) |
| `sub_956130` | 873 | Register class interference mask builder (SSE2) |
| `sub_957020` | -- | Pressure bitmap setup (per-VR constraint walk) |
| `sub_957160` | 1658 | Core fat-point allocator (coloring engine) |
| `sub_9680F0` | 3722 | Per-instruction assignment core loop |
| `sub_96D940` | 2983 | Spill guidance (7-class priority queues) |
| `sub_971A90` | 355 | NOSPILL / SPILL retry driver |
| `sub_9714E0` | -- | Post-allocation finalization |
| `sub_9721C0` | 1086 | Register allocation entry point |
| `sub_936FD0` | -- | Final fallback allocation |
| `sub_9375C0` | -- | VR priority sort |
