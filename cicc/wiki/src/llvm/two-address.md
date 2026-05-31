# TwoAddressInstruction

> **NVIDIA-modified pass.** See [Differences from Upstream](#differences-from-upstream-llvm) for GPU-specific changes.
>
> **LLVM version note:** Structurally identical to LLVM 20.0.0 `TwoAddressInstructionPass.cpp`. NVIDIA extensions are limited to deeper `EXTRACT_SUBREG` handling for multi-register results (texture/tensor/warp ops), extended `LiveVariables` maintenance, `OptimizationRemarkEmitter` integration, and the standard `optnone`/fast-compile gate.

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

The pass iterates over every `MachineBasicBlock` and every `MachineInstr` within it, maintaining per-block data structures that are cleared at block boundaries. The C-level skeleton below consolidates the four core paths -- `run()` worklist, `tieOperands()` heuristic, commutativity check, and the post-conversion verifier call -- as reconstructed from `sub_1F53550`, `sub_1F4EF20`, and `sub_1F50270`.

```c
/* sub_1F53550: TwoAddressInstructionPass::runOnMachineFunction */
bool TAI_run(MachineFunction *MF) {
    Pass *P = MF->pass;                                  /* +232..+296 */
    P->OptLevel = isOptNoneOrFastCompile(MF) ? 0 : MF->OptLevel;   /* sub_1636880 */
    bool changed = false;

    for (MachineBasicBlock *MBB = MF->front; MBB; MBB = MBB->next) {
        DenseMap_clear(&P->DistanceMap);                 /* offsets +312..+336 */
        DenseMap_clear(&P->SrcEqClassMap);               /* +552..+576 */
        DenseMap_clear(&P->DstEqClassMap);               /* +584..+608 */
        SmallPtrSet_clear(&P->Processed);                /* +344..+376 */
        unsigned dist = 0;

        for (MachineInstr *MI = MBB->front; MI; MI = MI->next) {
            if (MI->isBundleInternal()) continue;
            unsigned opc = MI->getOpcode();
            if (opc == TargetOpcode_COPY || opc == TargetOpcode_SUBREG_TO_REG) continue;
            if (SmallPtrSet_contains(&P->Processed, MI)) continue;   /* sub_1F4DD40 */

            if (opc == TargetOpcode_EXTRACT_SUBREG) {    /* NVIDIA multi-result path */
                decomposeExtractSubreg(P, MBB, MI);      /* sub_1F53550 lines 821-994 */
                continue;
            }
            if (opc == TargetOpcode_REG_SEQUENCE) {
                eliminateRegSequence(P, MI);
                continue;
            }

            DenseMap_insert(&P->DistanceMap, MI, ++dist);     /* sub_1F4EC70 */
            processCopy(P, MI);                                /* builds eq-class maps */

            /* === STEP: collectTiedOperands === (lines 1183-1413) */
            TiedOperandMap tom;  TiedOperandMap_init(&tom);
            if (!collectTiedOperands(MI, &tom)) continue;

            /* === Fast path: try to eliminate the tie without inserting a COPY === */
            if (TiedOperandMap_singleRegSinglePair(&tom)) {
                unsigned srcIdx, dstIdx;
                TiedOperandMap_first(&tom, &srcIdx, &dstIdx);
                if (tryInstructionTransform(P, MI, srcIdx, dstIdx, dist))
                    { TiedOperandMap_clear(&tom); continue; }      /* sub_1F4EF20 */
            }

            /* === Slow path: insert COPY for every remaining tied pair === */
            for (auto &e : tom) processTiedPairs(P, MI, &e.pairs, dist);   /* sub_1F50270 */

            if (opc == TargetOpcode_INSERT_SUBREG) {     /* lines 2386-2396 */
                MI->getOp(0).setSubReg(MI->getOp(3).getSubRegIdx());
                MI->getOp(0).setTied(MI->getOp(1).isTied());
                MI->RemoveOperand(3); MI->RemoveOperand(1);
                MI->setDesc(TII_get(TargetOpcode_COPY));
            }
            TiedOperandMap_clear(&tom);
            changed = true;
        }
    }

    /* === Post-conversion verifier (NVIDIA always-on) === */
    MachineFunction_verify(MF, "After two-address instruction pass");   /* sub_1E926D0 */
    return changed;
}

/* sub_1F4EF20: tryInstructionTransform -- commutativity-first heuristic */
bool tryInstructionTransform(Pass *P, MachineInstr *MI,
                             unsigned srcIdx, unsigned dstIdx, unsigned dist) {
    if (P->OptLevel == 0) return false;                  /* fast-compile bypass */

    /* 1. Commutation: swap (src,other) so that other matches dst */
    if (MI->isCommutable()) {
        unsigned other = TII->findCommutedOpIndices(MI, srcIdx);
        if (isProfitableToCommute(MI, srcIdx, dstIdx, dist, /*MaxEdges=*/3)) {
            if (TII->commuteInstruction(MI, srcIdx, other)) {
                if (MI->getOp(srcIdx).getReg() == MI->getOp(dstIdx).getReg())
                    return true;                          /* tie satisfied */
            }
        }
    }
    /* 2. 3-address conversion (NVPTX: dead -- PTX is already 3-address) */
    if (MachineInstr *MI2 = TII->convertToThreeAddress(MI, P->LIS)) return true;
    /* 3. Rescheduling (twoaddr-reschedule=true) */
    if (rescheduleMIBelowKill(P, MI, srcIdx, dstIdx, dist)) return true;   /* sub_1F4CC10 */
    if (rescheduleKillAboveMI(P, MI, srcIdx, dstIdx, dist)) return true;   /* sub_1F4D060 */
    /* 4. Load unfolding (NVPTX: no folded loads) */
    return false;                                        /* fall through -> processTiedPairs */
}
```

```c
for each MBB in MF:
    clear DistanceMap, SrcRegMap, DstRegMap, SrcEqClassMap, DstEqClassMap, Processed
    dist = 0

    for each MI in MBB:
        skip bundle internals
        skip COPY (opcode 12) and SUBREG_TO_REG (opcode 13)
        skip if MI is in the "reprocess" set

        if MI is EXTRACT_SUBREG (opcode 14):
            // NVPTX extended path -- multi-result decomposition
            // See detailed algorithm below
            decomposeExtractSubreg(MI)
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

> **QUIRK -- Dead 3-address conversion still runs.** `tryInstructionTransform` step 2 calls `TII->convertToThreeAddress()` on every tied instruction, but the NVPTX `TargetInstrInfo` does not override this hook so it always returns `nullptr`. PTX is already a 3-address ISA; nevertheless the call survives because the pass is shared LLVM source. Each tied instruction therefore pays the cost of a virtual dispatch and a null check on a path that cannot succeed.

> **QUIRK -- Verifier always runs.** Upstream LLVM gates `MachineFunction::verify("After two-address instruction pass")` on the `-verify-machineinstrs` flag. The CICC binary issues the verifier call unconditionally (`sub_1E926D0` is reached on every `runOnMachineFunction` exit). This catches stale `LiveVariables` state from the deep EXTRACT_SUBREG decomposition early but adds a full machine-function walk to every compilation, including `optnone` builds.

> **QUIRK -- Recursive self-call on unfolded chains.** `sub_1F4EF20` appears in its own xref list (22 cross-references including the self-edge). When load unfolding (step 4) creates a new MachineInstr that itself carries tied operands, the function recurses into the same body to resolve the new tie. On NVPTX this branch is unreachable because no folded loads exist, but the recursion frame and tail call are still emitted, contributing to the 28 KB function size.

### tryInstructionTransform (sub\_1F4EF20)

This is the optimization core. When `OptLevel != None`, it attempts to satisfy a tied constraint without inserting a COPY, in priority order:

1. **Commutation.** If swapping operands makes src match dst, commute the instruction via `TII->commuteInstruction()`. On NVPTX, most arithmetic instructions are commutative, so this is the most frequent success path. Upstream uses `isProfitableToCommute()` which walks up to `MaxDataFlowEdge` (default 3) dataflow edges to evaluate benefit.

2. **3-address conversion.** Call `TII->convertToThreeAddress()` to produce a true three-operand form. On NVPTX this is essentially dead code -- PTX instructions are already three-address -- but the infrastructure exists because the pass is shared LLVM code.

3. **Rescheduling.** When `twoaddr-reschedule` is enabled (default `true`), attempt to move the kill of the source register closer to the current instruction (`rescheduleMIBelowKill`) or move the current instruction below the kill (`rescheduleKillAboveMI`). This can eliminate the need for a copy by making the source register die at the tied use.

4. **Load unfolding.** For instructions with folded loads where the source is not killed, unfold the load into a separate `MOV` + arithmetic pair. Not applicable on NVPTX (no load folding).

5. **COPY insertion.** If all optimization attempts fail, fall through to `processTiedPairs` which inserts an explicit `COPY`.

The function calls itself recursively (22 cross-references including a recursive self-call at `sub_1F4EF20`) for transitive constraint resolution -- when unfolding creates a new instruction that itself has tied operands, the resolution recurses.

## EXTRACT\_SUBREG Multi-Result Decomposition Algorithm

This is the most substantial NVIDIA extension to the upstream pass. The code lives at lines 821--994 of `sub_1F53550` (decompilation line numbers from the 2,470-line function body). Standard LLVM handles single-result `EXTRACT_SUBREG`; the NVPTX version handles multi-result instructions where the `InstrEmitter` has produced a single `EXTRACT_SUBREG` pseudo with multiple operand pairs representing all extracted components.

### Why Multi-Result EXTRACT\_SUBREG Exists

When `InstrEmitter::EmitNode` (`sub_2EDDF20`, 872-byte stack frame, self-recursive for multi-result SDNode chains) lowers a multi-result NVPTX intrinsic, it produces a single `MachineInstr` with opcode 14 (`EXTRACT_SUBREG`) carrying N operand pairs -- one per result component. Each pair contains a def register (the extracted component destination) and a use register (the source super-register) plus a subreg index encoding which component to extract. The TwoAddress pass must decompose this single multi-operand pseudo into N separate `COPY` instructions.

The three major producer categories:

| Producer | Handler | ID range | Typical result width |
|---|---|---|---|
| Texture/surface loads | `sub_33A4350` | 50 IDs (0x5D--0x8D) | v4f32, v2f64, v4i32 |
| WMMA / MMA operations | `sub_33A64B0` | 95 IDs (0xA4--0xA8, 0x194--0x1EC) | 2--8 register fragments |
| Multi-element surface ops | case 0xA2 | single | loop over elements |
| MMA sm90+ (wgmma) | `sub_33AC8F0` | 0x183--0x191 | 8--16 register fragments |
| TMA operations | `sub_33AD3D0` | 0x179--0x17C | varies |
| Async copy | `sub_33ADA20` | 0x17F--0x182 | 2 results (data + token) |

The DAG-level builders that produce multi-result nodes are `sub_3411BE0` (multi-result DAG node), `sub_33FC220` (multi-result variadic node), and `sub_33F7800` (multi-result alternate form). The type list is built by `sub_1D25C30` (`SelectionDAG::getVTList` for multi-result).

### Operand Memory Layout

Each `MachineOperand` occupies 40 bytes in memory (stride 40 per operand in the operand array):

| Offset within operand | Size | Field |
|---|---|---|
| +0 | `byte` | Flags byte 0: bit 0 = isDef |
| +2 | `word` | Flags word: bits 4--11 = subreg class index, bits 8--19 = subreg index |
| +3 | `byte` | Flags byte 3: bit 4 = isTied, bit 6 = earlyTied |
| +4 | `byte` | Flags byte 4: bit 0 = isTied flag (secondary) |
| +8 | `int64` | Register number (virtual reg > 0, physical reg < 0) |

The subreg index is extracted by the formula:

```c
subregIdx = (*(uint32_t*)(operand + 0) >> 8) & 0xFFF
```

This 12-bit field encodes which sub-register of the source to extract: sub0, sub1, sub2, sub3, etc. For a v4f32 texture result, the values are typically 1 through 4.

### Decomposition Pseudocode

```c
// sub_1F53550 lines 821-994: EXTRACT_SUBREG handler (opcode == 14)
decomposeExtractSubreg(MI):
    numOps = MI.getNumOperands()              // v405
    pairIdx = 0                               // v286, stride-2 counter

    while pairIdx < numOps:
        defOp  = MI.getOperand(pairIdx)       // base + pairIdx * 40
        useOp  = MI.getOperand(pairIdx + 1)   // base + (pairIdx+1) * 40

        dstReg = defOp.getReg()               // *(int64*)(defOp + 8)
        srcReg = useOp.getReg()               // *(int64*)(useOp + 8)

        // Extract subreg index from def operand flags (bits 8-19)
        subregIdx = (defOp.flags >> 8) & 0xFFF

        // Check if this operand is already tied (bit 0 of byte +4)
        alreadyTied = (defOp.flagsByte4 & 1) != 0

        // === CREATE COPY INSTRUCTION ===
        // sub_1E0B640(MBB, insertPoint, MI.getDebugLoc(), 0)
        // This is BuildMI -- allocates a new MachineInstr with opcode COPY
        newCOPY = BuildMI(MBB, MI, MI.getDebugLoc(), TII.get(TargetOpcode::COPY))

        // Insert into block's instruction list
        if MI.isBundledWithSucc():
            sub_1DD6E10(MBB, MI, newCOPY)     // insertBefore (bundled variant)
        else:
            sub_1DD5BA0(MBB, MI, newCOPY)     // standard list insert

        // Add def operand: destination register with subreg class encoding
        // sub_1E1A9C0(newCOPY, dstReg, flags_with_subregclass)
        newCOPY.addOperand(MachineOperand::CreateReg(dstReg, /*isDef=*/true))

        // Add use operand: source register
        // sub_1E1A9C0(newCOPY, srcReg, flags_use)
        newCOPY.addOperand(MachineOperand::CreateReg(srcReg, /*isDef=*/false))

        // === EARLY TIED OPTIMIZATION ===
        // When this is NOT the first pair (pairIdx > 0) and the instruction
        // has tied constraints, check if a later pair shares the same dest
        // register. If so, mark the first operand of this COPY with isTied,
        // allowing the register coalescer to merge them without an extra COPY.
        if pairIdx > 0:
            earlyTiedCheck = (defOp.flagsByte3 >> 6) & 1   // bit 6
            isTiedCheck    = (defOp.flagsByte3 >> 4) & 1   // bit 4
            if earlyTiedCheck AND isTiedCheck:
                newCOPY.getOperand(0).setTied()  // set bit 0 of byte +4

        // === OPTIMIZATION REMARK ===
        if ORE != null:                       // pass object offset +272
            sub_1DCCCA0(ORE, dstReg, MI, newCOPY)   // emit copy remark
            remarkData = sub_1DCC790(ORE, dstReg)    // lookup remark data
            sub_1F4C640(remarkData)                   // filter/emit remark
            sub_1DCBB50(ORE)                          // push to output
            if newCOPY.isInsideBundle():
                walk to bundle head via successor chain
            if sub_1E1AFE0(bundleHead):               // hasProperty check
                sub_1DCC370(ORE, remarkNode)          // append to list

        // === LIVEVARIABLES UPDATE ===
        if LV != null:                        // pass object offset +280
            sub_1DBF6C0(LV, MBB, MI, newCOPY, ...)
            // This calls the full update chain:
            //   sub_1DBA290: createNewVarInfo for newCOPY's def register
            //   sub_1DBB110: initVarInfo (initialize kill/def lists)
            //   sub_1DB3C70: findKill (locate kill point in block)
            //   sub_1DB4410: addKill (update kill tracking for srcReg)
            //   sub_1DB8610: addNewBlock (update block-level liveness)

        pairIdx += 2                          // v286 += 2 (stride-2)

    // === CLEANUP ===
    // Remove all operands from original MI, then erase it
    sub_1E16240(MI)                           // RemoveOperand (bulk)
    MI.eraseFromParent()
```

### earlyTied Optimization Detail

The earlyTied optimization is a critical performance path. Consider a `v4f32` texture load producing 4 results. Without earlyTied, the decomposition creates 4 independent COPY instructions. The register coalescer must then discover independently that some of these COPYs can be coalesced.

The earlyTied flag (bit 6 of operand flags byte +3) is set during instruction emission when the emitter knows that consecutive extract results target adjacent sub-registers of a contiguous super-register. When detected, the pass marks the COPY's def operand with the isTied bit, creating a chain of tied constraints:

```llvm
// Without earlyTied (4 independent COPYs, coalescer must work harder):
%dst0 = COPY %src.sub0
%dst1 = COPY %src.sub1
%dst2 = COPY %src.sub2
%dst3 = COPY %src.sub3

// With earlyTied (COPYs carry tie hints, coalescer has direct information):
%dst0 = COPY %src.sub0                          // first pair: no tie
%dst1 = COPY %src.sub1   [tied to %dst0.succ]   // isTied bit set
%dst2 = COPY %src.sub2   [tied to %dst1.succ]   // isTied bit set
%dst3 = COPY %src.sub3   [tied to %dst2.succ]   // isTied bit set
```

The condition is: `(flagsByte3 >> 6) & 1` (earlyTied set) AND `(flagsByte3 >> 4) & 1` (isTied set) AND `pairIdx > 0` (not the first pair). This triple-guard prevents false positives on single-result extracts and on the first component which has no predecessor to tie to.

### LiveVariables Update Chain

Every COPY produced by the decomposition triggers a six-function update sequence. This is deeper than upstream LLVM's TwoAddress LiveVariables handling and suggests NVIDIA's downstream register allocator (the greedy RA at `sub_1E5B110`) is particularly sensitive to stale liveness:

| Step | Function | Purpose |
|---|---|---|
| 1 | `sub_1DBF6C0` | Entry: transfer liveness from old MI to new COPY |
| 2 | `sub_1DBA290` | `createNewVarInfo`: allocate VarInfo for the COPY's def register |
| 3 | `sub_1DBB110` | `initVarInfo`: initialize the VarInfo's kill list, def list, and alive-block bitvector |
| 4 | `sub_1DB3C70` | `findKill`: scan the current block to locate where srcReg is killed |
| 5 | `sub_1DB4410` | `addKill` / `removeKill`: move the kill point from the original MI to the new COPY (srcReg now dies at the COPY, not at the original EXTRACT_SUBREG) |
| 6 | `sub_1DB8610` | `addNewBlock`: update block-level liveness bitvectors if srcReg is live-in to this block from a predecessor |

For a v4f32 decomposition, this executes 24 function calls (6 per component times 4 components). For a wmma.mma producing 8 fragments, it is 48 calls. The cost is quadratic in the worst case because `findKill` scans from the block start, but in practice the kill is always close to the insertion point.

## Multi-Result Producers on NVPTX

The EXTRACT_SUBREG decomposition path fires for all NVPTX operations that produce more than one register result. These originate in the intrinsic lowering pass (`sub_33A64B0` and friends in the `0x33A` cluster) and flow through SelectionDAG ISel and InstrEmitter before reaching TwoAddress.

### Texture and Surface Loads

The texture bulk handler `sub_33A4350` covers 50 intrinsic IDs (0x5D through 0x8D). A `tex.1d.v4.f32` intrinsic produces an SDNode with value type list `{f32, f32, f32, f32, chain}` via `sub_1D25C30` (`getVTList`). InstrEmitter converts this into a single MachineInstr with 8 operands (4 def/use pairs), which TwoAddress decomposes into 4 COPYs.

Surface read/write handlers at `sub_33A3180` (IDs 0x8E--0x90) and the scatter/gather handler at case 0xA2 follow the same pattern with variable result widths.

### WMMA and MMA Operations

The mega-handler `sub_33A64B0` services 95 intrinsic IDs covering all wmma/mma variants across sm70+. A `wmma.mma.sync` on sm70 with fp16 accumulation produces 8 f16x2 fragments; on sm80 with tf32 it produces 4 f32 fragments. The sm90+ wgmma handler at `sub_33AC8F0` (IDs 0x183--0x191) can produce up to 16 register fragments for large matrix shapes.

Each fragment becomes one operand pair in the EXTRACT_SUBREG pseudo. The TwoAddress pass decomposes a 16-fragment wgmma result into 16 individual COPYs, each with full LiveVariables update. This is the most expensive decomposition path in the entire pass.

### TMA and Async Copy

TMA bulk operations (`sub_33AD3D0`, IDs 0x179--0x17C) and async copy operations (`sub_33ADA20`, IDs 0x17F--0x182) produce 2-result nodes (data + completion token). These are simpler decompositions with only 2 COPY instructions.

## Inline Assembly Tied Operands

CUDA inline assembly with `"+r"` read-write constraints is the third category that exercises the TwoAddress pass on NVPTX. The tied operand pipeline spans three compilation stages:

### Stage 1: EDG L-value Address Emission (sub\_1286D80 path)

The EDG frontend's inline asm codegen (analyzed in `p2-B07-inline-asm-codegen.txt`) detects tied operands when the operand descriptor byte at offset +24 equals 3. It constructs the constraint string by:

1. Computing the l-value descriptor via `EmitAddressOf` (`sub_1286D80`) so the tied operand can be re-loaded and stored back
2. Appending `*` for indirect operands
3. Appending the tied operand index as a decimal number to the constraint string

If the type size is a power-of-2 and 64 bits or less, it may insert a bitcast to matching integer type. GCC-style matching-digit constraints in input position are explicitly rejected with `"tied input/output operands not supported!"`.

### Stage 2: DAG-Level Tied Resolution (sub\_2079C70)

`SelectionDAGBuilder::visitInlineAsm` (`sub_2079C70`, 83KB) uses:
- `sub_20B4290`: `hasTiedOperand()` -- checks if tied index is not -1
- `sub_20B42B0`: `getTiedOperand()` -- returns the tied index
- `sub_2045250`: `resolveTiedOperand()` -- creates the DAG-level constraint

The error string `"inline asm not supported yet: don't know how to handle tied indirect register inputs"` guards against the unsupported case of tied operands on memory-indirect inline asm operands.

### Stage 3: TwoAddress COPY Insertion

After ISel, the tied operand from inline asm appears as a regular tied constraint in the MachineInstr operand list. The TwoAddress pass processes it through the standard `collectTiedOperands` / `processTiedPairs` path. For `"+r"` constraints this typically produces a single COPY before the INLINEASM instruction.

## processTiedPairs Detail (sub\_1F50270)

This 63KB / 2,209-line function is the heavyweight tied-operand resolver. It is called from the main loop whenever `collectTiedOperands` finds constraints that the fast path (`tryInstructionTransform`) could not resolve.

```c
processTiedPairs(MI, tiedPairs, distance):
    for each (srcIdx, dstIdx) in tiedPairs:
        srcReg = MI.getOperand(srcIdx).getReg()
        dstReg = MI.getOperand(dstIdx).getReg()

        if srcReg == dstReg:
            continue    // constraint already satisfied

        // === ATTEMPT COMMUTATION (OptLevel != None) ===
        if canCommute(MI):
            // isProfitableToCommute walks up to MaxDataFlowEdge (default 3)
            // dataflow edges from srcReg and dstReg, comparing distances
            // in DistanceMap to determine if commuting reduces copies
            if isProfitableToCommute(MI, srcIdx, dstIdx, distance):
                TII->commuteInstruction(MI)
                if MI.getOperand(srcIdx).getReg() == MI.getOperand(dstIdx).getReg():
                    continue    // resolved by commutation

        // === ATTEMPT RESCHEDULING (twoaddr-reschedule = true) ===
        if twoAddrReschedule:
            // Try to move MI below the kill of srcReg
            if rescheduleMIBelowKill(MI, srcIdx, dstIdx, distance):
                continue    // resolved by rescheduling
            // Try to move the kill of srcReg above MI
            if rescheduleKillAboveMI(MI, srcIdx, dstIdx, distance):
                continue    // resolved by rescheduling

        // === ATTEMPT 3-ADDRESS CONVERSION ===
        // On NVPTX, convertToThreeAddress always returns null (dead code)
        if TII->convertToThreeAddress(MI, LIS):
            continue    // resolved by conversion (never happens on NVPTX)

        // === INSERT COPY (last resort) ===
        newCOPY = BuildMI(MBB, MI, DL, TII.get(COPY), dstReg).addReg(srcReg)

        // Extract subreg index from original operand
        subregIdx = (MI.getOperand(srcIdx).flags >> 8) & 0xFFF
        if subregIdx != 0:
            newCOPY.getOperand(1).setSubReg(subregIdx)

        // Insert into DistanceMap with incremented counter
        // Walk predecessor chain to find scheduling unit
        DistanceMap[newCOPY] = ++distance
        DistanceMap[MI] = ++distance

        // Rewrite srcReg to dstReg in original MI
        MI.getOperand(srcIdx).setReg(dstReg)     // sub_1E310D0

        // Update SrcEqClassMap: map srcReg -> dstReg
        SrcEqClassMap.insert(srcReg, dstReg)      // sub_1F4E3A0

        // === LIVEVARIABLES UPDATE ===
        if LV:
            varInfo = LV.getVarInfo(dstReg)        // sub_1DC1550
            if varInfo not found:
                varInfo = LV.createNewVarInfo(dstReg)  // sub_1DBA290
                LV.initVarInfo(varInfo)                 // sub_1DBB110
            // Transfer kill info: srcReg kill moves from MI to newCOPY
            killInfo = varInfo.findKill(MBB)       // sub_1DB3C70
            varInfo.addKill(newCOPY, flags)        // sub_1DB4410
            // Update block-level liveness
            varInfo.addNewBlock(MBB, position)     // sub_1DB8610

        // === OPTIMIZATION REMARK ===
        if ORE and commutationWasAttempted:        // v384 flag
            sub_1DCC790(ORE, srcReg)               // lookup remark data
            sub_1F4C640(remarkData)                 // filter remark
            sub_1DCBB50(ORE)                        // push
            if newCOPY.isInsideBundle():
                walk to bundle head
            if sub_1E1AFE0(bundleHead):
                sub_1DCC370(ORE, remarkNode)       // append to list

        // === REGISTER CLASS TIGHTENING ===
        // sub_1E69410(SubtargetInfo, dstReg, regClass, 0)
        // constrainRegClass on the destination register to the intersection
        // of the current class and the class required by the tied operand
```

### INSERT\_SUBREG Rewrite (lines 2386--2396)

After all tied pairs are processed for an `INSERT_SUBREG` instruction (opcode 8), the pass converts it into a plain `COPY`:

```c
if MI.getOpcode() == INSERT_SUBREG:
    // Propagate subreg encoding from operand[3] into operand[0]
    subregBits = MI.getOperand(3).getSubRegIdx()
    MI.getOperand(0).setSubReg(subregBits)
    // Copy tie flag from operand[1] into operand[0]
    MI.getOperand(0).setTied(MI.getOperand(1).isTied())
    // Remove operands 3 and 1 (in reverse order to preserve indices)
    MI.RemoveOperand(3)              // sub_1E16C90(MI, 3)
    MI.RemoveOperand(1)              // sub_1E16C90(MI, 1)
    // Rewrite opcode descriptor to COPY
    MI.setDesc(TII.get(COPY))        // descriptor at TII + 960
```

## Copy-Equivalence Classes

The pass builds two maps (`SrcEqClassMap` at offset +552, `DstEqClassMap` at +584) that track transitive copy chains. When it encounters `COPY`, `REG_SEQUENCE`, or `INSERT_SUBREG` instructions, it records the source-to-destination register mapping. The helper `collectRegCopies` (`sub_1F4E620`, 357 lines) walks use-def chains to build transitivity: if `A -> B -> C` via COPYs, then `A` maps directly to `C`. These maps are consumed by the downstream `RegisterCoalescer` to improve copy elimination.

The `collectRegCopies` algorithm:

```c
collectRegCopies(startReg):
    chain = SmallVector()
    reg = startReg

    while true:
        if not MRI.hasOneUse(reg):        // sub_1E69E00
            break
        defMI = MRI.getVRegDef(reg)
        if defMI.getOpcode() not in {COPY, REG_SEQUENCE, INSERT_SUBREG}:
            break
        nextReg = defMI.getOperand(1).getReg()
        chain.push(reg)
        reg = nextReg

    // Process chain in reverse: build transitivity
    for i in reverse(chain):
        SrcEqClassMap.insert(chain[i], chain[i+1])    // sub_1F4E3A0
```

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

**DistanceMap** (`DenseMap<MachineInstr*, unsigned>` at pass object offsets +312..+336): maps each MI to its sequential position within the current block. Hash: `(ptr >> 4) ^ (ptr >> 9)`. Used by `tryInstructionTransform` and `processTiedPairs` for rescheduling decisions and commutation profitability evaluation.

**Pass Object Layout** (selected fields):

| Offset | Type | Field |
|---|---|---|
| +232 | `MachineFunction*` | Current function |
| +240 | `MachineRegisterInfo*` | MRI |
| +248 | `TargetInstrInfo*` | TII |
| +256 | `TargetRegisterInfo*` | TRI |
| +264 | `ptr` | InstrItineraryData* or TargetSubtargetInfo* |
| +272 | `OptimizationRemarkEmitter*` | ORE (NVIDIA addition) |
| +280 | `LiveVariables*` | LV |
| +288 | `LiveIntervals*` | LIS (via SlotIndexes at +160) |
| +296 | `int` | Effective optimization level |
| +304 | `MachineBasicBlock*` | Current MBB |
| +312..+336 | `DenseMap` | DistanceMap |
| +344..+376 | `SmallPtrSet` | Processed set |
| +448..+476 | `SmallPtrSet` | Second set (reprocessing) |
| +552..+576 | `DenseMap` | SrcEqClassMap |
| +584..+608 | `DenseMap` | DstEqClassMap |

## Tied Operand Scanning (Lines 1183--1413)

The `collectTiedOperands` logic iterates all operands of an instruction checking for tied constraints. The inner loop (at STEP 7 in the raw analysis) contains a special-case direct resolution path:

```c
for opIdx in 0..numOps-1:
    // Skip defs, already-tied, and operands with no subreg class
    if operand.isDef():           continue     // byte +0 != 0
    if operand.isTied():          continue     // bit 4 of byte +3
    if operand.subregClass == 0:  continue     // bits 4-11 of word +2

    tiedIdx = MI.findTiedOperandIdx(opIdx)     // sub_1E16AB0
    srcReg = operand[opIdx].getReg()
    dstReg = operand[tiedIdx].getReg()

    if srcReg == dstReg:
        continue    // already satisfied

    // SPECIAL CASE: direct resolution without COPY
    if operand.isTied(secondary) AND def.subregClass == 0:
        if dstReg < 0:    // physical register
            regClass = sub_1F3AD60(MRI, instrDesc, opIdx, TII, MF)
            if regClass:
                MRI.constrainRegClass(dstReg, regClass)   // sub_1E69410
        operand.setReg(dstReg)                     // sub_1E310D0
        operand.clearSubregBits()                  // *operand &= 0xFFF000FF
        // Constraint resolved: use now points to same reg as def
        continue

    // NORMAL: add to TiedOperandMap
    TiedOperandMap[srcReg].push({opIdx, tiedIdx})  // packed as qword
```

The special-case path at the `isTied(secondary)` check (bit 0 of byte +4) handles the case where the operand carries a secondary tie flag from instruction emission and the def side has no subreg class constraint. In this case the pass can directly rewrite the use register to match the def without inserting a COPY, and clears the subreg bits with the mask `0xFFF000FF`.

## NVIDIA Modifications

The pass is structurally stock LLVM -- the libNVVM build at `sub_F4EA80` is byte-for-byte identical in structure, confirming shared source. The NVIDIA delta consists of four additions:

1. **Extended EXTRACT_SUBREG handling** (lines 821--994 of the decompilation). Standard LLVM handles single EXTRACT_SUBREG; the NVPTX version handles multi-result instructions with multiple extract chains via stride-2 operand iteration. This is required for texture/surface loads returning `v4f32`, wmma/mma producing multi-register fragments, and similar multi-result NVPTX intrinsics. The earlyTied optimization (checking bits 4 and 6 of operand flags byte +3) is unique to this extension and provides direct coalescing hints for contiguous sub-register sequences.

2. **Deeper LiveVariables maintenance** (lines 1791--2064). When a COPY is inserted, the pass creates new `VarInfo` entries (`sub_1DBA290`), initializes them (`sub_1DBB110`), updates kill info (`sub_1DB3C70` / `sub_1DB4410`), and maintains block-level liveness (`sub_1DB8610`). This six-function chain executes per COPY, not per instruction. For a 16-fragment wgmma result, this produces 96 function calls for liveness maintenance alone.

3. **OptimizationRemarkEmitter integration** (lines 2207--2258). The pass reports cases where tied-operand constraints forced extra COPY insertions, providing performance diagnostic information. This is absent in upstream LLVM's TwoAddress pass. The ORE pointer is stored at pass object offset +272 and acquired via analysis lookup of `unk_4FC4534`. The five-function chain (`sub_1DCCCA0` through `sub_1DCC370`) handles remark creation, filtering, and bundle-aware emission.

4. **optnone/fast-compile gate** (`sub_1636880`). When the function has `optnone` or when NVIDIA's fast-compile mode is active, the effective optimization level is forced to 0. This disables commutation, 3-address conversion, and rescheduling attempts in `tryInstructionTransform` (which returns `false` immediately when `OptLevel == None`), making the pass a pure COPY-insertion pass with no optimization.

## Knobs

| Knob | Default | Effect |
|---|---|---|
| `twoaddr-reschedule` | `true` | Enable/disable instruction rescheduling to coalesce copies. When `true`, the pass attempts to move instructions up or down within the block to avoid needing a COPY. |
| `dataflow-edge-limit` | `3` | Maximum number of dataflow edges to traverse when evaluating the profitability of commuting operands in `isProfitableToCommute()`. Higher values allow deeper analysis at compile-time cost. |

Both knobs are registered in constructor `ctor_337` (found in the sweep at `0x4F0000`--`0x51FFFF`). They are standard upstream LLVM options with no NVIDIA-specific modifications to their defaults.

The `optnone`/fast-compile gate is not a knob per se but has the effect of disabling all optimization paths in the pass, equivalent to setting both knobs to their most conservative values.

## Function Map

| Function | Address | Size | Role |
|---|---|---|---|
| Pass registration (name + ID) | `sub_1F4D900` | small | Sets `"Two-Address instruction pass"` and `"twoaddressinstruction"` |
| Constructor | `sub_1F4D9F0` | small |  |
| Helper: rescheduleMIBelowKill support | `sub_1F4CC10` | -- | Called by `sub_1F4EF20` |
| Helper: rescheduleKillAboveMI support | `sub_1F4D060` | -- | Called by `sub_1F4EF20` |
| `SmallPtrSet::contains(MI*)` | `sub_1F4DD40` | 67 lines | Processed set membership check |
| `SmallDenseMap::clear()` | `sub_1F4DE20` | 180 lines | TiedOperandMap cleanup, frees heap-allocated pair lists |
| `DenseMap<int,int>::insert` | `sub_1F4E3A0` | 166 lines | EqClassMap insertion, hash = `37 * key` |
| `collectRegCopies` | `sub_1F4E620` | 357 lines | Walks COPY chains to build transitive equivalence classes |
| `DenseMap<ptr,int>::insert` | `sub_1F4EC70` | 164 lines | DistanceMap insertion, hash = `(ptr>>4) ^ (ptr>>9)` |
| `tryInstructionTransform` | `sub_1F4EF20` | 28KB / 1,127 lines | Core tied-operand rewriter: commutation, 3-addr, COPY. Recursive (22 xrefs). |
| `processTiedPairs` | `sub_1F50270` | 63KB / 2,209 lines | Full pipeline: commute, convert, COPY insertion, LV/LI update |
| `SmallDenseMap::grow` | `sub_1F53020` | 312 lines | TiedOperandMap rehash, 56-byte entry stride |
| `runOnMachineFunction` | `sub_1F53550` | 79KB / 2,470 lines | Pass entry point |
| Helper: find matching superclass | `sub_1F3AD60` | -- | Finds register class for tied physical reg constraints |
| Helper: implicit tied operands | `sub_1F4C460` | -- | Checks if MI has implicit tied operand pairs |
| Helper: filter/emit remark | `sub_1F4C640` | -- | ORE filtering for copy-insertion diagnostics |
| `LiveVariables::createNewVarInfo` | `sub_1DBA290` | -- | Allocates VarInfo for new register |
| `LiveVariables::initVarInfo` | `sub_1DBB110` | -- | Initializes kill/def lists and alive bitvector |
| `VarInfo::findKill` | `sub_1DB3C70` | -- | Scans block for register kill point |
| `VarInfo::addKill` / `removeKill` | `sub_1DB4410` | -- | Updates kill tracking |
| `VarInfo::addNewBlock` | `sub_1DB8610` | -- | Updates block-level liveness bitvectors |
| `LiveVariables::HandlePhysRegDef` | `sub_1DBF6C0` | -- | Transfer liveness from old MI to new COPY |
| `ORE::emit` (copy remark) | `sub_1DCCCA0` | -- | Emits optimization remark for COPY insertion |
| `ORE::lookup` | `sub_1DCC790` | -- | Looks up remark data for register |
| `ORE::push` | `sub_1DCBB50` | -- | Pushes remark to output |
| `ORE::appendToList` | `sub_1DCC370` | -- | Appends remark (bundle-aware) |
| `MachineFunction::verify` | `sub_1E926D0` | -- | Called with `"After two-address instruction pass"` |
| `isOptNone` / fast-compile check | `sub_1636880` | -- | Forces `OptLevel = 0` when active |

## Binary Size Note

The 79KB `runOnMachineFunction` plus 63KB `processTiedPairs` plus 28KB `tryInstructionTransform` total approximately 170KB of machine code. Upstream LLVM source for the entire pass is approximately 2,000 lines of C++. The binary bloat is almost entirely explained by aggressive inlining: every `DenseMap::insert`, `DenseMap::find`, `DenseMap::clear`, `SmallPtrSet::insert`, and `SmallPtrSet::find` operation is fully expanded inline with all template specialization, sentinel initialization, grow/rehash, and power-of-2 computation logic. This accounts for roughly 40% of the binary. The remaining expansion comes from the COPY-creation path (operand setup, flag manipulation, list splicing) being duplicated for each opcode-specific branch rather than factored into a shared helper.

## Differences from Upstream LLVM

| Aspect | Upstream LLVM | CICC v13.0 |
|--------|---------------|------------|
| **Primary purpose** | Convert 3-address to 2-address form for physical register constraints (x86 tied operands) | Largely a formality on NVPTX (PTX is 3-address); primary role is eliminating `REG_SEQUENCE`/`INSERT_SUBREG` and building copy-equivalence maps |
| **EXTRACT_SUBREG handling** | Standard sub-register extraction for CPU multi-result instructions | Extended decomposition for multi-register NVPTX results: texture loads, tensor core operations (WMMA/MMA), and warp-level collectives |
| **LiveVariables maintenance** | Standard liveness tracking | Deeper `LiveVariables` maintenance with explicit `VarInfo` allocation/init (`sub_1DBA290`/`sub_1DBB110`) for new registers created during decomposition |
| **ORE integration** | Basic or absent remark emission for copies | Full `OptimizationRemarkEmitter` integration for COPY insertion diagnostics (`sub_1DCCCA0`/`sub_1DCC790`/`sub_1DCBB50`) |
| **Binary size** | ~2,000 lines of C++ source | 170 KB of machine code (79 KB `runOnMachineFunction` + 63 KB `processTiedPairs` + 28 KB `tryInstructionTransform`); bloat from aggressive DenseMap inlining |
| **optnone/fast-compile gate** | Standard `OptLevel` check | NVIDIA `optnone` / fast-compile check (`sub_1636880`) forces `OptLevel = 0` for fast-compile kernels |

## Cross-References

- [Register Coalescing](./register-coalescing.md) -- runs immediately after TwoAddress; consumes the SrcEqClassMap/DstEqClassMap built here
- [Register Allocation](./register-allocation.md) -- the downstream consumer that requires tied operands to be resolved
- [SelectionDAG](./selectiondag.md) -- produces the EXTRACT_SUBREG/INSERT_SUBREG/REG_SEQUENCE pseudo-instructions that this pass eliminates
- [Instruction Emitter](./instr-emitter.md) -- `sub_2EDDF20` creates multi-result EXTRACT_SUBREG chains from SDNode output
- [MMA Code Generation](./mma-codegen.md) -- WMMA/MMA intrinsics producing multi-register results that require decomposition
- [ISel Patterns](./isel-patterns.md) -- instruction selection creates the tied operand constraints
- [Instruction Scheduling](./scheduling.md) -- runs before TwoAddress in the pre-RA scheduling slot
- [Pipeline & Ordering](./pipeline.md) -- full pass ordering context
- [CLI Flags](../config/cli-flags.md) -- `optnone` and fast-compile mode
- [LLVM Knobs](../config/knobs.md) -- `twoaddr-reschedule`, `dataflow-edge-limit`
- [Hash Infrastructure](../infra/hash-infrastructure.md) -- DenseMap and SmallDenseMap internals used throughout
- [Diagnostics](../infra/diagnostics.md) -- OptimizationRemarkEmitter system
