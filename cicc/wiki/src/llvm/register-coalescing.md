# Register Coalescing

> **NVIDIA-modified pass.** See [Differences from Upstream](#differences-from-upstream-llvm) for GPU-specific changes.

Register coalescing in CICC v13.0 eliminates redundant copy instructions by merging the live ranges of their source and destination virtual registers. NVPTX's unlimited virtual register model (PTX has no fixed physical register file) changes the purpose of coalescing compared to CPU targets: rather than reducing physical register pressure to avoid spills, the goal is strictly copy elimination — fewer `mov` instructions in the emitted PTX, which in turn gives `ptxas` a cleaner input with fewer live-range constraints to resolve during its own physical allocation. CICC runs two coalescing passes in sequence: the standard LLVM `RegisterCoalescer` at `sub_2F71140` (which handles generic `COPY` pseudo-instructions) and a separate NVPTX-specific coalescer rooted at `sub_34AF4A0` (which handles NVPTX copy instruction families in the opcode 440--503 range that the generic pass does not recognize). This page documents both, with emphasis on the NVPTX-specific pass where the bulk of the proprietary logic resides.

| | |
|---|---|
| **Standard LLVM RegisterCoalescer** | `sub_2F71140` (80KB, 2,190 lines) |
| **NVPTX coalescing driver** | `sub_34AF4A0` (67KB, 2,373 lines) |
| **Per-instruction coalesce attempt** | `sub_34AE060` (28KB) |
| **Interference check** | `sub_34AA450` (11.5KB) |
| **Block-level coalescing** | `sub_34BAAF0` (31.7KB) |
| **Live-out / weight computation** | `sub_34B7280` (22KB) |
| **Interval tree (red-black BST)** | `sub_34A0610` (14.7KB) |
| **Range rebuild after merge** | `sub_34A46B0` (13KB) |
| **Opcode -> copy-type mapping** | `sub_3494EA0` (12.7KB) |
| **Operand type classification table** | `byte_444C4A0` (16-byte entries) |
| **Address range** | `0x3494EA0` — `0x34BF740` |
| **Pass parameters** | `(pass_obj*, func_info*, MF*, copy_limit, coalesce_limit)` |
| **Pass ordering** | After TwoAddressInstruction, before greedy RA |

## Why Coalescing Matters on a Virtual-Register Target

On CPU targets, coalescing reduces register pressure by allowing two virtual registers to share one physical register, potentially preventing a spill. On NVPTX the motivation is different. PTX is a virtual ISA with typed, unlimited registers (`%r0`, `%r1`, ... for 32-bit integers; `%f0`, `%f1`, ... for 32-bit floats). The "physical" allocation is deferred entirely to `ptxas`, which maps virtual registers to the hardware register file at kernel launch time based on occupancy targets. CICC's coalescing therefore serves three purposes:

1. **Copy elimination.** Every `mov` instruction that survives into emitted PTX is dead weight — it costs an issue slot and extends the live range of both source and destination. Coalescing removes these by unifying src and dst into a single virtual register.

2. **Reduced register name count.** Even though PTX registers are virtual, `ptxas` must solve a graph-coloring problem on them. Fewer distinct register names (after coalescing merges equivalents) give `ptxas` a smaller interference graph and faster compilation.

3. **Cleaner SSA destruction.** PHI elimination during the transition from SSA form to machine code inserts copies at every PHI edge. Many of these are immediately coalesceable because the PHI operand's live range does not extend past the copy point. The coalescer cleans up the mechanical output of PHI lowering.

Copies that the coalescer processes arise from three sources: PHI elimination copies, ABI/calling-convention `.param` register copies for kernel call boundaries, and sub-register operations (`EXTRACT_SUBREG`, `INSERT_SUBREG`).

## Standard LLVM RegisterCoalescer (sub_2F71140)

CICC includes the stock LLVM `RegisterCoalescer` at `sub_2F71140`, registered as pass `"register-coalescer"` with debug output markers `"Before register coalescing"` / `"After register coalescing"`. This pass handles the generic `COPY` pseudo-instruction (LLVM's `TargetOpcode::COPY`) using the standard worklist-driven algorithm from upstream.

The key LLVM knobs that apply to this instance:

| Knob | Default | Effect |
|---|---|---|
| `join-liveintervals` | `true` | Master enable for copy coalescing |
| `join-splitedges` | subtarget | Coalesce copies on split critical edges |
| `join-globalcopies` | subtarget | Coalesce copies that span basic blocks |
| `terminal-rule` | `true` | Apply the terminal rule (copies at block ends) |
| `verify-coalescing` | `false` | Verify MachineInstrs before and after coalescing |
| `late-remat-update-threshold` | `100` | Batch live-interval updates when a def has many copy uses |
| `large-interval-size-threshold` | `100` | Intervals with more valnos than this are "large" |
| `large-interval-freq-threshold` | `256` | Stop coalescing a large interval after this many joins |

The standard pass operates on `COPY` pseudo-instructions only. It does not understand NVPTX-specific move instruction families (opcodes 440--503), which is why the NVPTX-specific pass exists.

## NVPTX-Specific Coalescer (sub_34AF4A0)

The proprietary coalescer at `sub_34AF4A0` runs after the standard `RegisterCoalescer` and targets NVPTX copy instruction families that the generic pass skips. It operates on the `MachineFunction` representation and accepts two limit parameters beyond the standard pass/MF arguments: `copy_limit` (maximum number of copy instructions to consider) and `coalesce_limit` (maximum number of successful merges before bailing out). These are compile-time budget controls that prevent quadratic behavior on large functions.

### Opcode Classification

The function `sub_3494EA0` contains a giant switch statement mapping NVPTX instruction opcodes (range 1--0x12) to copy families in the 440--503 opcode range. Each family represents a distinct copy semantic:

- **Opcodes 440--443**: Type-preserving moves within a single register class (i32-to-i32, f32-to-f32, etc.). These map from internal opcodes 12, 13, 15 in the operand-type classification table.
- **Opcodes 444--503**: Cross-class moves, paired/wide register moves (128-bit pairs for tensor core paths), and ABI-related `.param` copies.

The return value is an `__m128i` pair encoding both the copy semantics and the register class constraints, which subsequent stages use to decide whether coalescing is legal.

Operand-type classification happens via `sub_34961A0`, which reads operands and classifies them through a lookup table at `byte_444C4A0`. Each entry in this table is 16 bytes:

```c
struct OperandTypeEntry {
    uint8_t type_code;        // +0: 12=i32, 13=i64, 15=f32, etc.
    uint8_t size_class;       // +1: size in register-width units
    uint8_t register_bank;    // +2: bank identifier
    uint8_t constraint_flags; // +3: bit 0x10 = participates in coalescing
    uint8_t reserved[12];     // +4: padding/future use
};
```

The constraint flag at offset +3 (mask `0x10`) gates whether the operand participates in coalescing at all. Operands with this bit cleared are excluded from the worklist.

### Register Class Constraints

Coalescing is constrained to same-class merges. The NVPTX register classes are completely disjoint — an `Int32Regs` (`%r`) register cannot coalesce with a `Float32Regs` (`%f`) register even though both are 32 bits wide. This is a consequence of PTX's typed register model: `.reg .b32 %r0` and `.reg .f32 %f0` are distinct storage locations from `ptxas`'s perspective. The complete register class table and coalescing constraint flags are in [Register Classes](../reference/register-classes.md#coalescing-constraints). All eight primary classes are same-class-only; `Int128Regs` is excluded from the coalescing worklist entirely (constraint flag cleared).

Cross-class copies (e.g., bitcasting an `i32` to `f32`) use distinct cross-class copy opcodes (see the [copy opcode table](../reference/register-classes.md#copy-opcodes----sub_2162350)) and are never eliminated by the coalescer — they must survive as explicit instructions in PTX.

### Sub-Register Handling

NVPTX has a flat register file with no sub-register structure in the CPU sense. There are no `%eax`/`%ax`/`%al` hierarchies. The exception is wide register pairs: 128-bit values used by tensor core operations are represented as pairs of 64-bit registers. `sub_3497B40` handles paired-register decomposition, and when coalescing the low half of a pair, the high half inherits corresponding constraints. The coalesce candidate record (248 bytes) stores sub-operand arrays at offset +16 (4 entries of 32 bytes each, inline SBO) specifically for tracking these pair relationships.

## Coalescing Algorithm

The NVPTX coalescer follows the standard LLVM pattern of worklist-driven interval joining but uses proprietary data structures throughout.

### Phase 1: Initialization (lines 494--617)

Load `TargetInstrInfo`, `TargetRegisterInfo`, and `TargetSubtargetInfo` from the `MachineFunction` vtables. Initialize approximately 15 open-addressing hash maps, 2 min-heaps, 3 interval trees (red-black BSTs), and 2 linked lists. The stack frame is approximately 4.5KB. Walk all basic blocks, filter virtual-register operands via `sub_2DADC00` (the `isVirtualRegister` check), and collect copy instructions into the worklist hash.

### Phase 2: Block-Level Scanning (lines 618--857)

For each basic block, walk instructions and identify NVPTX copy instructions (opcode field at instruction offset +68 equals 14 or 15). For each copy:

1. Validate source type via `sub_B10CD0` (extract register class).
2. Check physical register constraints (vestigial on NVPTX but present in the code).
3. Build a coalesce pair via `sub_34A70E0`, creating a 248-byte candidate record.

Track live-through registers per block using bitvectors.

### Phase 3: Interference Graph Construction (lines 858--998)

Build the interval tree via `sub_2DACB60` and `sub_C8CD80`. Cross-compare forward and backward interval lists via `sub_2E564A0`. Flatten into indexed format via `sub_2E507D0`. The result is a set of live intervals indexed by register number, stored in a red-black BST where each node is 448 bytes (`0x1C0`).

### Phase 4: Worklist-Driven Coalescing (lines 1040--2092)

This is the core loop. Candidates are extracted from a min-heap ordered by register number (lowest first — a standard LLVM heuristic that processes defs before uses in reverse postorder).

```c
function CoalesceWorklistDriven(heap, intervals, hash_map):
    while heap is not empty:
        candidate = heap.extract_min()
        src_interval = lookup(hash_map, candidate.src_key)
        dst_interval = lookup(hash_map, candidate.dst_key)

        // Same-class check
        if register_class(src_interval) != register_class(dst_interval):
            continue

        // Interference check
        if CheckInterference(src_interval, dst_interval) != 0:
            push candidate to secondary_heap
            continue

        // Pre-coalesce validation
        if not ValidateCopy(candidate):
            push candidate to secondary_heap
            continue

        // Execute the merge
        merged = MergeIntervals(src_interval, dst_interval)
        RewriteOperands(candidate.copy_instr, merged)
        UpdateHashMap(hash_map, merged)

        // Verify and rebuild
        VerifyMergedInterval(merged)
        RebuildRanges(merged)

    // Double-buffer swap: retry with secondary heap
    swap(heap, secondary_heap)
    if secondary_heap was non-empty:
        repeat from top
```

The double-buffer swap (lines 2073--2093) alternates between two heaps (`v373` and `v376`). After exhausting one worklist, the pass swaps and retries — implementing the LLVM-style "iterate until convergence" pattern where an earlier merge may resolve interference that blocked a later merge.

### Phase 5: Code Patching (lines 2095--2144)

For each coalesced pair, rewrite instruction operands:

1. `sub_349D6E0` — look up the merged interval's representative register.
2. `sub_349FA50` — find the instruction position.
3. `sub_2E31040` — patch the operand's register field.
4. Fix linked-list pointers using the `ptr & 0xFFFFFFFFFFFFFFF8` mask (the low 3 bits encode tags on `MachineOperand` pointers: 0 = normal, 3 = tied operand, 4 = implicit operand).

### Phase 6: Cleanup (lines 2145--2371)

Destroy interval trees (`sub_349E8A0`), perform final range rebuild (`sub_34A46B0`), finalize coalescing metadata (`sub_34A2530`), commit merged intervals (`sub_34AA090`), and deallocate all hash maps, heaps, and trees (16+ `free` calls).

## Interference Check (sub_34AA450)

The interference check is the critical decision point. Given two intervals (identified by their register keys), it determines whether merging them would create a conflict — that is, whether both registers are simultaneously live at any program point.

```c
function CheckInterference(interval_A, interval_B) -> {0 = safe, 1 = interfering}:
    for each instruction I in interval_A.instruction_vector:
        if I is in the "already-coalesced" set:
            continue
        reg_class = extract_register_class(I)
        dst_interval = lookup(reg_to_interval_hash, I.dst_reg)
        if dst_interval overlaps with interval_B:
            return 1  // interfering
    return 0  // safe to coalesce
```

The "already-coalesced" set is an open-addressing hash map (pointer keys, hash `(key >> 9) ^ (key >> 4)`, sentinels -4096/-8192). The sentinel check at `a3+8` (a flag byte) determines whether the set uses inline or heap storage (small-buffer optimization for sets under approximately 8 entries).

Since NVPTX has no physical register file, "interference" here means purely that two virtual register live ranges overlap at a program point. On CPU targets this would also involve physical register conflict checks, but on NVPTX that dimension is absent.

## Priority and Weight System

The coalescing priority determines the order in which candidates are processed when the min-heap's register-number ordering produces ties.

**Weight computation** (`sub_34B7280`):

```c
weight = instruction_count + spill_weight[offset+240] + use_count[offset+252]
```

The flag at `offset+254 & 1` guards weight computation: if set, the interval was pre-weighted by an earlier pass and the coalescer uses the existing weight rather than recomputing.

Higher weight means higher coalescing priority. The overall ordering is:

1. **Primary key**: register number (min-heap, lowest first).
2. **Secondary key**: weight (higher breaks ties in favor of more-used registers).

**Block frequency integration**: The pass reads a boolean from `TargetPassConfig` (via `sub_35DDE70` at `*(_QWORD*)(pass[4]+256)+856`) that controls whether block frequency data influences priority. When enabled, copies in hot blocks receive higher priority, biasing the coalescer toward eliminating copies on the critical execution path.

## Data Structures

### Hash Maps

All hash maps use the standard DenseMap open-addressing infrastructure described in [Hash Table and Collection Infrastructure](../infra/hash-infrastructure.md). Two sentinel variants appear in this pass:

| Variant | Key Type | Sentinel pair |
|---|---|---|
| Integer-key | `int32_t` | `-1` / `-2` (hash: `key * 37`) |
| Pointer-key | `int64_t` | `-4096` / `-8192` (hash: `(key >> 9) ^ (key >> 4)`) |

Growth policy: `next_power_of_2(2 * old_capacity - 1)`, minimum 64 entries.

Allocator: `sub_C7D670(size, alignment=8)` / `sub_C7D6A0(ptr, size, alignment=8)` — CICC's aligned malloc/free wrappers.

### Interval Tree (Red-Black BST)

Managed by `sub_34A0610`. Each node is 448 bytes (`0x1C0`):

| Offset | Size | Field |
|---|---|---|
| +0 | 24 | Tree links (left, right, parent pointers) |
| +32 | 8 | Interval key (register/slot encoding) |
| +64 | 8 | Instruction vector pointer |
| +72 | 4 | Instruction count |
| +192 | 16 | Debug name (SBO: inline if len <= 15) |
| +200 | 4 | Sub-operand count |
| +224 | 4 | Instruction opcode |
| +240 | 2 | Priority/weight (`uint16`) |

Comparator: `sub_34A0190` (compares interval start positions). Rebalancing: `sub_34A0330`. The tree maintains count (`a2[5]`) and cached min/max (`a2[3]`/`a2[4]`).

### Coalesce Candidate Record (248 bytes)

Built by `sub_349AB40` for each potential coalescing opportunity:

| Offset | Size | Field |
|---|---|---|
| +0 | 8 | Source interval key |
| +8 | 8 | Destination interval key |
| +16 | 128 | Sub-operand array (SBO, 4 entries x 32 bytes) |
| +64 | 112 | Type-constraint array (SBO, 2 entries x 56 bytes) |
| +192 | 32 | Debug name (SBO string) |
| +224 | 4 | Opcode classification (1--6: copy, subreg, extract, ...) |
| +232 | 4 | Copy source register |
| +240 | 2 | Priority (default: 1) |

### MachineOperand Pointer Encoding

Throughout the coalescing code, `MachineOperand` pointers use low-bit tagging (8-byte alignment guarantees 3 unused low bits):

| Tag (ptr & 7) | Meaning |
|---|---|
| 0 | Normal operand |
| 3 | Tied operand (requires special coalescing — both operands must map to same register) |
| 4 | Implicit operand (flag bit at operand offset +44, bit 3) |

The code consistently masks with `& 0xFFFFFFFFFFFFFFF8` before dereferencing and checks `(ptr & 7) == 3` or `(ptr & 4) != 0` for branching decisions.

## CSSA Coalescing (PHI-Specific)

Separate from the two coalescing passes above, CICC includes a CSSA (Conventional SSA) coalescing stage controlled by the `cssa-coalesce` knob (constructor at `ctor_705`, address `0x5BD430`). This pass operates at the SSA level rather than the machine level, coalescing PHI operands before PHI elimination to reduce the number of copies that PHI lowering generates. Associated knobs:

| Knob | Effect |
|---|---|
| `cssa-coalesce` | Enable/disable PHI operand coalescing |
| `cssa-verbosity` | Verbosity level for CSSA debug output |
| `dump-before-cssa` | Dump IR before CSSA coalescing |
| `usedessa` | Select deSSA method (alternative to CSSA) |

## Knobs and Thresholds Summary

| Knob | Source | Default | Effect |
|---|---|---|---|
| `join-liveintervals` | LLVM | `true` | Master enable for standard RegisterCoalescer |
| `join-splitedges` | LLVM | subtarget | Coalesce on split critical edges |
| `join-globalcopies` | LLVM | subtarget | Coalesce cross-block copies |
| `terminal-rule` | LLVM | `true` | Terminal rule for block-end copies |
| `verify-coalescing` | LLVM | `false` | Pre/post verification |
| `late-remat-update-threshold` | LLVM | `100` | Batch remat update threshold |
| `large-interval-size-threshold` | LLVM | `100` | Large interval valno threshold |
| `large-interval-freq-threshold` | LLVM | `256` | Large interval coalesce limit |
| `twoaddr-reschedule` | LLVM | — | Coalesce copies by rescheduling in TwoAddress |
| `copy_limit` | NVPTX | runtime | Max copies to consider in NVPTX pass |
| `coalesce_limit` | NVPTX | runtime | Max merges before bailout in NVPTX pass |
| `cssa-coalesce` | NVPTX | — | PHI operand coalescing |
| `cssa-verbosity` | NVPTX | — | CSSA debug verbosity |
| block frequency flag | NVPTX | config | Weight copies by block hotness |

The `copy_limit` and `coalesce_limit` parameters are passed into `sub_34AF4A0` at call time (not static `cl::opt` knobs). Their values come from the pass pipeline configuration and serve as compile-time budget caps to avoid quadratic worst-case behavior on functions with thousands of copies.

## Impact on ptxas

The quality of CICC's coalescing directly affects `ptxas`'s register allocation phase:

- **Fewer virtual registers** means a smaller interference graph for `ptxas` to color, reducing its compilation time.
- **Eliminated copies** reduce instruction count, giving `ptxas`'s scheduler more freedom and fewer false dependencies.
- **Preserved type invariants** (no cross-class coalescing) ensure `ptxas` never encounters type-inconsistent register usage, which would require additional conversion instructions.
- **Wide register pair tracking** ensures tensor core instruction patterns remain intact — `ptxas` expects specific register pair relationships for `mma` and `wmma` instructions.

A pathological case is over-aggressive coalescing that creates very long live ranges spanning many basic blocks. On NVPTX this does not cause spills (there is no physical register file to spill from), but it can increase `ptxas`'s reported register usage, reducing occupancy. The `coalesce_limit` parameter and the large-interval frequency threshold exist partly to avoid this scenario.

## Function Map

| Function | Address | Size | Role |
|---|---|---|---|
| Main NVPTX coalescing driver | `sub_34AF4A0` | 67KB | — |
| Per-instruction coalesce attempt | `sub_34AE060` | 28KB | — |
| Pre-coalesce validation (opcode 14/15 check) | `sub_34AB5C0` | 16KB | — |
| Post-coalesce update (rewrite def-use chains) | `sub_34AC810` | 19KB | — |
| Constrained-copy validation variant | `sub_34AD8B0` | 8.5KB | — |
| Interference check | `sub_34AA450` | 11.5KB | — |
| Range rebuild (bitvector `v90[12336]`) | `sub_34A46B0` | 13KB | — |
| Interval equivalence verify | `sub_34A2770` | 7.3KB | — |
| Interval tree insert/rebalance (RB-tree) | `sub_34A0610` | 14.7KB | — |
| Register-to-interval hash lookup | `sub_34A3910` | 2.7KB | — |
| Build worklist from BB operand scan | `sub_34A3D10` | 5KB | — |
| Build worklist from instruction iteration | `sub_34A41A0` | 4.8KB | — |
| Block-level coalescing driver | `sub_34BAAF0` | 31.7KB | — |
| Live-out analysis + weight computation | `sub_34B7280` | 22KB | — |
| Per-register interference build | `sub_34B6620` | 17.7KB | — |
| Operand-type classification | `sub_34961A0` | 26.6KB | — |
| Register-pair decomposition | `sub_3497B40` | 16.5KB | — |
| Opcode -> copy-type mapping (switch) | `sub_3494EA0` | 12.7KB | — |
| Build coalesce candidate list | `sub_349AB40` | 24.5KB | — |
| Merged-interval representative lookup | `sub_349D6E0` | — | — |
| Instruction position lookup/creation | `sub_349FA50` | 7.1KB | — |
| Interval tree destructor (variant A) | `sub_349E330` | 4KB | — |
| Interval tree destructor (variant B) | `sub_349E500` | 4KB | — |
| Interval tree destructor (variant C) | `sub_349E6D0` | 4KB | — |
| Interval tree destructor (variant D) | `sub_349E8A0` | 4KB | — |
| Interval info populate from instruction | `sub_349F140` | 4.7KB | — |
| Interval structure reset | `sub_349F740` | 4KB | — |
| Generic map cleanup (callback `sub_349D600`) | `sub_34A2010` | — | — |
| Finalize coalescing metadata | `sub_34A2530` | — | — |
| Commit merged intervals | `sub_34AA090` | — | — |
| Secondary coalesce commit | `sub_34A9A60` | — | — |
| Register info initializer | `sub_35065A0` | — | — |
| Standard LLVM RegisterCoalescer | `sub_2F71140` | 80KB | — |
| RegisterCoalescer::getPassName | `sub_2F60C50` | — | — |

## Differences from Upstream LLVM

| Aspect | Upstream LLVM | CICC v13.0 |
|--------|---------------|------------|
| **Number of passes** | Single `RegisterCoalescer` pass handling `COPY` pseudo-instructions | Two passes in sequence: stock LLVM `RegisterCoalescer` (`sub_2F71140`) + NVPTX-specific coalescer (`sub_34AF4A0`) |
| **Opcode coverage** | Handles only `TargetOpcode::COPY` (generic copy pseudo) | NVPTX pass handles NVPTX copy instruction families in opcode range 440--503 that the generic pass does not recognize |
| **Coalescing goal** | Reduce physical register pressure to prevent spills | Strictly copy elimination (PTX has unlimited virtual registers); goal is fewer `mov` instructions in emitted PTX and smaller interference graphs for `ptxas` |
| **Interference check** | Standard `LiveIntervals` query | Custom interference check (`sub_34AA450`, 11.5 KB) with interval tree (red-black BST at `sub_34A0610`) for NVPTX register classes |
| **Block-level coalescing** | Part of the unified worklist | Separate block-level coalescing pass (`sub_34BAAF0`, 31.7 KB) processes copies within each block before cross-block coalescing |
| **Operand classification** | Generic operand handling | Custom operand type classification table (`byte_444C4A0`, 16-byte entries) maps NVPTX opcode families to copy semantics |
| **Pass parameters** | Standard `runOnMachineFunction` with no limits | Parameterized with explicit `(copy_limit, coalesce_limit)` bounds for compile-time control on large kernels |

## Cross-References

- **[Register Allocation](./register-allocation.md)** — the greedy allocator that runs after coalescing; shares the register class table and interference hash pattern.
- **[Instruction Scheduling](./scheduling.md)** — scheduling runs after RA and benefits from reduced copy count; MRPA pressure tracking is affected by coalescing decisions.
- **[LLVM Knobs](../config/knobs.md)** — full knob inventory including all coalescing-related flags.
- **[Code Generation](../pipeline/codegen.md)** — pipeline ordering showing where coalescing fits relative to other machine passes.
