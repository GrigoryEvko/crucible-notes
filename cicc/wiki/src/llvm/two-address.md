# TwoAddressInstruction

The TwoAddressInstruction pass converts three-address `MachineInstr`s into two-address form by inserting `COPY` pseudo-instructions so that tied operand constraints are satisfied before register allocation. In upstream LLVM, many CPU targets have instructions where one source operand must be the same physical register as the destination (x86 `addl %esi, %edi` means `%edi = %edi + %esi`); the pass rewrites `A = B op C` into `A = COPY B; A op= C`. On NVPTX this pass is largely a formality -- PTX instructions are three-address and the virtual register file has no physical-register constraints -- but it still performs essential bookkeeping: eliminating `REG_SEQUENCE` and `INSERT_SUBREG` pseudo-instructions, building copy-equivalence maps for downstream coalescing, and handling the tied operands that arise from multi-result NVPTX intrinsics (texture loads, tensor core operations, warp-level collectives). CICC's binary is structurally identical to stock LLVM, with extended `EXTRACT_SUBREG` handling for multi-register results, deeper `LiveVariables` maintenance, `OptimizationRemarkEmitter` integration, and the standard NVIDIA `optnone`/fast-compile gate.

| | |
|---|---|
| **Pass name** | `"Two-Address instruction pass"` |
| **Pass ID** | `"twoaddressinstruction"` |
| **Pipeline slot** | `"two-address-instruction"` (MachineFunction pass #521) |
| **`runOnMachineFunction`** | `sub_1F53550` (79KB, 2,470 lines) |
| **`tryInstructionTransform`** | `sub_1F4EF20` (28KB, 1,127 lines) |
| **`processTiedPairs`** | `sub_1F50270` (63KB, 2,209 lines) |
| **Cluster address range** | `0x1F4D000` -- `0x1F56000` |
| **libNVVM twin** | `sub_F4EA80` (2,455 lines, structurally identical) |
| **Verification string** | `"After two-address instruction pass"` |
| **Ordering** | After PHI elimination, before RegisterCoalescer |

## Why This Pass Exists on NVPTX

PTX is a three-address virtual ISA -- every arithmetic instruction takes separate dst, src0, src1 operands, and the hardware register allocator inside `ptxas` handles physical assignment. On a CPU target like x86, the TwoAddress pass is critical because most ALU instructions destroy one source register. On NVPTX, the pass fires primarily for three categories:

1. **Pseudo-instruction lowering.** `REG_SEQUENCE`, `INSERT_SUBREG`, and `EXTRACT_SUBREG` are LLVM-internal pseudo-opcodes that must be eliminated before register allocation regardless of target. The TwoAddress pass rewrites `INSERT_SUBREG` into `COPY` and expands `REG_SEQUENCE` into per-subreg copies.

2. **Multi-result intrinsics.** NVPTX texture/surface loads return `v4f32` or `v2f64` as multi-register results. Warp-level operations (`wmma`, `mma`) produce multi-register outputs. These get lowered into chains of `EXTRACT_SUBREG` pseudo-instructions that the pass must decompose into individual `COPY`s, one per extracted component.

3. **Inline assembly tied operands.** CUDA inline `asm` blocks with `"+r"` (read-write) constraints produce tied operands where the output register must match the input. The pass inserts a `COPY` from the input virtual register to the output register to satisfy the constraint.

For most ordinary NVPTX arithmetic instructions, `collectTiedOperands` finds nothing and the pass skips the instruction after updating the distance map and processing any copy-equivalence information. The pass is not a no-op, but the heavy transformation paths (commutation, 3-address conversion, load unfolding) almost never fire for GPU code.

## Algorithm

The pass iterates over every `MachineBasicBlock` and every `MachineInstr` within it, maintaining per-block data structures that are cleared at block boundaries.

```
for each MBB in MF:
    clear DistanceMap, SrcRegMap, DstRegMap, SrcEqClassMap, DstEqClassMap, Processed
    dist = 0

    for each MI in MBB:
        skip bundle internals
        skip COPY (opcode 12) and SUBREG_TO_REG (opcode 13)
        skip if MI is in the "reprocess" set

        if MI is EXTRACT_SUBREG (opcode 14):
            // NVPTX extended path -- multi-result decomposition
            for each pair of operands (stride-2 iteration):
                create COPY: dst_subreg = COPY src_reg
                insert before MI, update LiveVariables
                emit OptimizationRemark if ORE present
            erase original EXTRACT_SUBREG
            continue

        if MI is REG_SEQUENCE (opcode 15):
            // Standard LLVM: expand into per-subreg COPYs
            eliminateRegSequence(MI)
            continue

        DistanceMap[MI] = ++dist

        // Build copy-equivalence classes for downstream coalescing
        processCopy(MI)  // tracks COPY, REG_SEQUENCE, INSERT_SUBREG chains

        // Collect (srcIdx, dstIdx) pairs for all tied operands
        if not collectTiedOperands(MI, TiedOperandMap):
            continue

        // Single-pair fast path: attempt commutation / 3-addr conversion
        if TiedOperandMap has exactly 1 register with 1 pair:
            if tryInstructionTransform(MI, srcIdx, dstIdx, dist):
                continue  // constraint eliminated without COPY

        // General path: insert COPYs for all remaining tied pairs
        for each (reg, pairs) in TiedOperandMap:
            processTiedPairs(MI, pairs, dist)

        // Rewrite INSERT_SUBREG to COPY after tied constraints satisfied
        if MI is INSERT_SUBREG:
            remove operands 3 and 1
            rewrite descriptor to COPY
```

### tryInstructionTransform (sub_1F4EF20)

This is the optimization core. When `OptLevel != None`, it attempts to satisfy a tied constraint without inserting a COPY, in priority order:

1. **Commutation.** If swapping operands makes src match dst, commute the instruction via `TII->commuteInstruction()`. On NVPTX, most arithmetic instructions are commutative, so this is the most frequent success path. Upstream uses `isProfitableToCommute()` which walks up to `MaxDataFlowEdge` (default 3) dataflow edges to evaluate benefit.

2. **3-address conversion.** Call `TII->convertToThreeAddress()` to produce a true three-operand form. On NVPTX this is essentially dead code -- PTX instructions are already three-address -- but the infrastructure exists because the pass is shared LLVM code.

3. **Rescheduling.** When `twoaddr-reschedule` is enabled (default `true`), attempt to move the kill of the source register closer to the current instruction (`rescheduleMIBelowKill`) or move the current instruction below the kill (`rescheduleKillAboveMI`). This can eliminate the need for a copy by making the source register die at the tied use.

4. **Load unfolding.** For instructions with folded loads where the source is not killed, unfold the load into a separate `MOV` + arithmetic pair. Not applicable on NVPTX (no load folding).

5. **COPY insertion.** If all optimization attempts fail, fall through to `processTiedPairs` which inserts an explicit `COPY`.

The function calls itself recursively for transitive constraint resolution (e.g., when unfolding creates a new instruction that itself has tied operands).

### EXTRACT_SUBREG Multi-Result Decomposition

This is the most visibly NVPTX-extended portion. Standard LLVM handles `EXTRACT_SUBREG` in TwoAddress, but the NVPTX version extends it for multi-result instructions. When a texture load returns `v4f32`, ISEL produces a single pseudo-instruction with 8 operands (4 def/use pairs), each extracting one component. The pass iterates by stride-2 across the operand list, creating one `COPY` per component:

```
// Original: %dst0, %dst1, %dst2, %dst3 = EXTRACT_SUBREG %src, sub0, sub1, sub2, sub3
// After:
//   %dst0 = COPY %src.sub0
//   %dst1 = COPY %src.sub1
//   %dst2 = COPY %src.sub2
//   %dst3 = COPY %src.sub3
```

The `earlyTied` optimization (checking bit 6 of operand flags at byte +3) detects when a later pair shares the same destination register and can merge the tie constraint forward, avoiding an extra COPY.

### Copy-Equivalence Classes

The pass builds two maps (`SrcEqClassMap` at offset +552, `DstEqClassMap` at +584) that track transitive copy chains. When it encounters `COPY`, `REG_SEQUENCE`, or `INSERT_SUBREG` instructions, it records the source-to-destination register mapping. The helper `collectRegCopies` (`sub_1F4E620`) walks use-def chains to build transitivity: if `A -> B -> C` via COPYs, then `A` maps directly to `C`. These maps are consumed by the downstream `RegisterCoalescer` to improve copy elimination.

## Data Structures

**TiedOperandMap** (stack-allocated `SmallDenseMap<unsigned, SmallVector<pair<unsigned,unsigned>, 4>>` with 4 inline entries):

| Offset in entry | Type | Field |
|---|---|---|
| +0 | `int32` | Key (virtual register number; `-1` = empty, `-2` = tombstone) |
| +8 | `ptr` | Pair list pointer (points to +24 for inline storage) |
| +16 | `int32` | Pair list size |
| +20 | `int32` | Pair list capacity |
| +24 | `int64[4]` | Inline pair storage (each `qword` packs `srcIdx | (dstIdx << 32)`) |

Entry stride: 56 bytes. Hash function: `37 * key`, linear probing, load factor 3/4. Total inline size: 224 bytes on stack.

**DistanceMap** (`DenseMap<MachineInstr*, unsigned>` at pass object offsets +312..+336): maps each MI to its sequential position within the current block. Hash: `(ptr >> 4) ^ (ptr >> 9)`. Used by `tryInstructionTransform` for rescheduling decisions.

**Pass Object Layout** (selected fields):

| Offset | Type | Field |
|---|---|---|
| +232 | `MachineFunction*` | Current function |
| +240 | `MachineRegisterInfo*` | MRI |
| +248 | `TargetInstrInfo*` | TII |
| +256 | `TargetRegisterInfo*` | TRI |
| +272 | `OptimizationRemarkEmitter*` | ORE (NVIDIA addition) |
| +280 | `LiveVariables*` | LV |
| +288 | `LiveIntervals*` | LIS (via SlotIndexes at +160) |
| +296 | `int` | Effective optimization level |
| +304 | `MachineBasicBlock*` | Current MBB |
| +312..+336 | `DenseMap` | DistanceMap |
| +344..+376 | `SmallPtrSet` | Processed set |
| +552..+576 | `DenseMap` | SrcEqClassMap |
| +584..+608 | `DenseMap` | DstEqClassMap |

## NVIDIA Modifications

The pass is structurally stock LLVM -- the libNVVM build at `sub_F4EA80` is byte-for-byte identical in structure, confirming shared source. The NVIDIA delta consists of four additions:

1. **Extended EXTRACT_SUBREG handling** (lines 821--994 of the decompilation). Standard LLVM handles single EXTRACT_SUBREG; the NVPTX version handles multi-result instructions with multiple extract chains via stride-2 operand iteration. This is required for texture/surface loads returning `v4f32`, wmma/mma producing multi-register fragments, and similar multi-result NVPTX intrinsics.

2. **Deeper LiveVariables maintenance** (lines 1791--2064). When a COPY is inserted, the pass creates new `VarInfo` entries (`sub_1DBA290`), initializes them (`sub_1DBB110`), updates kill info (`sub_1DB3C70` / `sub_1DB4410`), and maintains block-level liveness (`sub_1DB8610`). This depth suggests NVIDIA's downstream register allocator is particularly sensitive to stale liveness information.

3. **OptimizationRemarkEmitter integration** (lines 2207--2258). The pass reports cases where tied-operand constraints forced extra COPY insertions, providing performance diagnostic information. This is absent in upstream LLVM's TwoAddress pass. The ORE pointer is stored at pass object offset +272 and acquired via analysis lookup of `unk_4FC4534`.

4. **optnone/fast-compile gate** (`sub_1636880`). When the function has `optnone` or when NVIDIA's fast-compile mode is active, the effective optimization level is forced to 0. This disables commutation, 3-address conversion, and rescheduling attempts in `tryInstructionTransform` (which returns `false` immediately when `OptLevel == None`), making the pass a pure COPY-insertion pass with no optimization.

## Knobs

| Knob | Default | Effect |
|---|---|---|
| `twoaddr-reschedule` | `true` | Enable/disable instruction rescheduling to coalesce copies. When `true`, the pass attempts to move instructions up or down within the block to avoid needing a COPY. |
| `dataflow-edge-limit` | `3` | Maximum number of dataflow edges to traverse when evaluating the profitability of commuting operands in `isProfitableToCommute()`. Higher values allow deeper analysis at compile-time cost. |

Both knobs are registered in constructor `ctor_337` (found in the sweep at `0x4F0000`--`0x51FFFF`). They are standard upstream LLVM options with no NVIDIA-specific modifications to their defaults.

The `optnone`/fast-compile gate is not a knob per se but has the effect of disabling all optimization paths in the pass, equivalent to setting both knobs to their most conservative values.

## Function Map

| Address | Identity | Size | Notes |
|---|---|---|---|
| `sub_1F4D900` | Pass registration (name + ID) | small | Sets `"Two-Address instruction pass"` and `"twoaddressinstruction"` |
| `sub_1F4D9F0` | Constructor | small | |
| `sub_1F4DD40` | `SmallPtrSet::contains(MI*)` | 67 lines | Processed set membership check |
| `sub_1F4DE20` | `SmallDenseMap::clear()` | 180 lines | TiedOperandMap cleanup, frees heap-allocated pair lists |
| `sub_1F4E3A0` | `DenseMap<int,int>::insert` | 166 lines | EqClassMap insertion, hash = `37 * key` |
| `sub_1F4E620` | `collectRegCopies` | 357 lines | Walks COPY chains to build transitive equivalence classes |
| `sub_1F4EC70` | `DenseMap<ptr,int>::insert` | 164 lines | DistanceMap insertion, hash = `(ptr>>4) ^ (ptr>>9)` |
| `sub_1F4EF20` | `tryInstructionTransform` | 28KB / 1,127 lines | Core tied-operand rewriter: commutation, 3-addr, COPY. Recursive. |
| `sub_1F50270` | `processTiedPairs` | 63KB / 2,209 lines | Full pipeline: commute, convert, COPY insertion, LV/LI update |
| `sub_1F53020` | `SmallDenseMap::grow` | 312 lines | TiedOperandMap rehash, 56-byte entry stride |
| `sub_1F53550` | `runOnMachineFunction` | 79KB / 2,470 lines | Pass entry point |
| `sub_1F3AD60` | Helper: find matching superclass | -- | Finds register class for tied physical reg constraints |
| `sub_1F4C460` | Helper: implicit tied operands | -- | Checks if MI has implicit tied operand pairs |
| `sub_1F4C640` | Helper: filter/emit remark | -- | ORE filtering for copy-insertion diagnostics |
| `sub_1E926D0` | `MachineFunction::verify` | -- | Called with `"After two-address instruction pass"` |
| `sub_1636880` | `isOptNone` / fast-compile check | -- | Forces `OptLevel = 0` when active |

## Binary Size Note

The 79KB `runOnMachineFunction` plus 63KB `processTiedPairs` plus 28KB `tryInstructionTransform` total approximately 170KB of machine code. Upstream LLVM source for the entire pass is approximately 2,000 lines of C++. The binary bloat is almost entirely explained by aggressive inlining: every `DenseMap::insert`, `DenseMap::find`, `DenseMap::clear`, `SmallPtrSet::insert`, and `SmallPtrSet::find` operation is fully expanded inline with all template specialization, sentinel initialization, grow/rehash, and power-of-2 computation logic. This accounts for roughly 40% of the binary. The remaining expansion comes from the COPY-creation path (operand setup, flag manipulation, list splicing) being duplicated for each opcode-specific branch rather than factored into a shared helper.

## Cross-References

- [Register Coalescing](./register-coalescing.md) -- runs immediately after TwoAddress; consumes the SrcEqClassMap/DstEqClassMap built here
- [Register Allocation](./register-allocation.md) -- the downstream consumer that requires tied operands to be resolved
- [SelectionDAG](./selectiondag.md) -- produces the EXTRACT_SUBREG/INSERT_SUBREG/REG_SEQUENCE pseudo-instructions that this pass eliminates
- [Instruction Scheduling](./scheduling.md) -- runs before TwoAddress in the pre-RA scheduling slot
- [Pipeline & Ordering](./pipeline.md) -- full pass ordering context
- [CLI Flags](../config/cli-flags.md) -- `optnone` and fast-compile mode
- [LLVM Knobs](../config/knobs.md) -- `twoaddr-reschedule`, `dataflow-edge-limit`
