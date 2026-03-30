# InstrEmitter

InstrEmitter is the final translation layer between LLVM's SelectionDAG representation and the machine-level MachineInstr pipeline. After instruction selection has converted LLVM IR into a DAG of target-specific SDNodes, and after scheduling has linearized those nodes into a sequence, InstrEmitter walks the scheduled sequence and converts each SDNode into one or more MachineInstrs inserted into the current MachineBasicBlock. In CICC v13.0, the emitter lives at `sub_2EDDF20` (11,722 bytes) and is called by `ScheduleDAGSDNodes::EmitSchedule` (`sub_2EE0CF0`). NVIDIA's build contains three key modifications relative to upstream LLVM: a dedicated CopyToReg handler factored out for NVPTX's physical-register-heavy parameter ABI, a triple vtable dispatch pattern that gates custom pseudo-expansion for GPU-specific instructions, and an extended MachineInstr flag at bit 36 (`0x1000000000`) not present in stock LLVM.

| | |
|---|---|
| **EmitNode / EmitMachineNode** | `sub_2EDDF20` (11,722 bytes, 872-byte stack frame) |
| **EmitSchedule (top-level driver)** | `sub_2EE0CF0` (59KB) |
| **EmitCopyToReg handler** | `sub_2ED95B0` |
| **EmitSubregNode** | `sub_2EDB7A0` |
| **EmitCopyToRegClassOp** | `sub_2EDD7E0` |
| **ProcessOperands / EmitMachineNode core** | `sub_2ED3660` |
| **getRegForValue** | `sub_2E8B400` |
| **isDeadNode predicate** | `sub_2DADC00` |
| **MinRCSize threshold** | 4 (upstream default, unchanged) |
| **VReg hash load factor** | 3/4 (rehash when `count * 4 >= capacity * 3`) |
| **Hash function** | `key * 37`, masked by `capacity - 1` |
| **SDOperand stride** | 40 bytes (0x28) per entry |

## Emission Architecture

In upstream LLVM, `InstrEmitter::EmitNode` is a trivial dispatcher: if the SDNode carries a target-specific (machine) opcode, it calls `EmitMachineNode`; otherwise it calls `EmitSpecialNode` for ISD-level pseudo-operations. CICC merges both paths into a single monolithic function (`sub_2EDDF20`) that dispatches on the raw 16-bit opcode at SDNode offset `+0x44`. The entry point performs a bit-table test against a 64-bit immediate (`0x80001078000`) to classify opcodes <= `0x2B` as "special" ISD nodes requiring dedicated handling; everything above falls through to the generic machine emission path.

The driver, `ScheduleDAGSDNodes::EmitSchedule` (`sub_2EE0CF0`), iterates the scheduled SUnit sequence. For each SUnit, it first walks the glue chain backwards (via `SDNode::getGluedNode`) and emits each glued predecessor before emitting the SUnit's own node. This guarantees that glued instructions appear as a contiguous sequence in the MachineBasicBlock, which is critical for NVPTX where texture sampling sequences must remain bundled with their address computation.

## The Emission Algorithm

The combined EmitNode function proceeds through fourteen phases. The condensed flow:

```
EmitNode(InstrEmitter *self, SDNode *node):
    // Phase 1: Early exit for dead nodes
    if !self->forceEmit && node->useCount <= 1:
        return false  // single-use folded into consumer

    // Phase 2: Glue chain traversal
    root = node
    while root->predecessor has chain/glue bit set:
        root = strip_tag(root->predecessor)
        if root->hasChainResult:
            walk further to data-producing node

    // Phase 3: Opcode dispatch
    opc = node->opcode  // uint16 at +0x44
    switch opc:
        0x0E (CopyToReg):  call EmitCopyToReg(self, node)
        0x13 (TokenFactor): skip entirely
        0x14 (CopyFromReg): goto copyfromreg_path
        0x0F, 0x10, 0x1C, 0x2B: special ISD handling
        default: goto generic_emission

    // Phase 4: Generic machine emission
    desc = TII->get(opc)
    MI = BuildMI(MBB, node->debugLoc, desc)
    CreateVirtualRegisters(node, MI, desc)
    for each operand in node->operands:
        AddOperand(MI, operand)
    MI.setMemRefs(node->memoperands)
    MBB->insert(InsertPos, MI)

    // Phase 5: Custom inserter check (triple vtable dispatch)
    if TII->vtable[0xB8] != sub_2ED11C0:  // not default
        call custom inserter for NVPTX pseudos
    if TII->vtable[0x348] != sub_2ED11F0:
        call expandPostRAPseudo
    if TII->vtable[0x160] != sub_2ED11E0:
        call sub-register inserter

    // Phase 6: Implicit physreg defs
    collect UsedRegs from glue chain (CopyFromReg, RegisterSDNode)
    mark unused implicit defs as dead

    // Phase 7: Post-emission dead copy elimination
    for each emitted copy:
        if copy result has no remaining uses:
            eraseFromParent(copy MI)
```

### Opcode Dispatch Details

The bit-table dispatch uses a 64-bit immediate as a compressed lookup: `bt 0x80001078000, opcode`. The bits that are set correspond to ISD opcodes that need special (non-generic) handling:

| Opcode | ISD Value | Handler |
|---|---|---|
| `0x0E` | `ISD::CopyToReg` | `sub_2ED95B0` -- dedicated handler |
| `0x0F` | `ISD::EH_LABEL` / special | Label emission path |
| `0x10` | `ISD::INLINEASM` | Inline assembly emission |
| `0x13` | `ISD::TokenFactor` | Skipped (ordering-only, no MI) |
| `0x14` | `ISD::CopyFromReg` | Physical-to-virtual register copy |
| `0x1C` | `ISD::LIFETIME_START/END` | Frame index annotation |
| `0x2B` | `ISD::PSEUDO_PROBE` | Profiling probe emission |

For opcodes above `0x2B`, the emitter falls through to the generic path that calls `TII->get(opc)` to obtain the MCInstrDesc and builds a MachineInstr from its operand descriptors.

## CopyToReg Emission

CopyToReg (`sub_2ED95B0`) handles the common case of copying a value from a virtual register into a physical register. Upstream LLVM handles this inline within `EmitSpecialNode`; NVIDIA factors it into a separate function, likely for code size reasons given how frequently CopyToReg appears in NVPTX code. NVPTX's parameter-passing convention maps kernel parameters to fixed physical registers `%r1`--`%r255`, which generates large CopyToReg cascades at function entry and before calls.

The handler:
1. Reads the destination register from `SDNode->operand(1)` (a RegisterSDNode).
2. If the destination is virtual and the source is an `IMPLICIT_DEF`, emits `IMPLICIT_DEF dest` directly instead of a COPY.
3. Otherwise resolves the source value to a virtual register via `getVR` (which consults the VRBaseMap).
4. If source and destination are the same register, does nothing (copy coalesced away).
5. Emits `COPY dest, src`.

## CopyFromReg Emission

CopyFromReg (opcode `0x14`) is the reverse: it copies a physical register into the virtual register domain. The CICC implementation at `sub_2EDDF20` offset `0x2EDF423` follows a multi-step process:

1. Extract the source register from `SDNode->operand(1)`. If virtual, insert the SDValue-to-VReg mapping directly into VRBaseMap and return.
2. If physical, determine the correct register class:
   - Query all users of this CopyFromReg. If the sole user is a CopyToReg to a virtual register in the same class, reuse that destination register.
   - Otherwise compute `UseRC` as the intersection of all user register class constraints via `TRI->getCommonSubClass`.
   - Fall back to `TRI->getMinimalPhysRegClass(SrcReg, VT)`.
3. If copying the physical register is impossible or expensive (`RC->expensiveOrImpossibleToCopy()`), use the physical register directly.
4. Otherwise emit `COPY VRBase, SrcReg` where VRBase is a new virtual register in DstRC.

The register class membership test at `0x2EDF4C2` uses LLVM's compressed bit-vector representation:

```c
bool RegisterClass::contains(unsigned Reg) {
    unsigned class_idx = Reg >> 3;
    if (class_idx >= desc->num_classes)
        return false;
    return (desc->class_table[class_idx] >> (Reg & 7)) & 1;
}
```

## NVPTX Custom Pseudo-Expansion

The triple vtable dispatch pattern is the emitter's most distinctive NVIDIA modification. After inserting a MachineInstr for a target-specific opcode, the emitter checks three separate vtable slots to determine whether the instruction requires custom expansion:

**Vtable slot 0xB8: `EmitInstrWithCustomInserter`**
Default stub: `sub_2ED11C0` (returns false). When the NVPTX target overrides this for a given opcode, the custom inserter replaces the pseudo MachineInstr with an expanded sequence. Approximately 15--20 NVPTX pseudo-instructions use this path:
- Texture load operations (`tex.1d`, `tex.2d`, `tex.3d`) -- these expand into address register setup, sampler state configuration, and the actual texture fetch instruction.
- Surface operations (`sust`, `suld`) -- surface load/store instructions that need coordinate clamping and format conversion.
- Warp-level intrinsics (`shfl`, `vote`, `match`) -- instructions that require lane mask setup and predicate register manipulation.
- Atomic operations -- certain atomics expand into compare-and-swap loops on older architectures.

**Vtable slot 0x348: `expandPostRAPseudo`**
Default stub: `sub_2ED11F0`. This handles pseudo-instructions that can only be expanded after register allocation has assigned physical registers. In NVPTX this is less common since the PTX virtual register model defers most allocation to ptxas.

**Vtable slot 0x160: sub-register insertion**
Default stub: `sub_2ED11E0`. Handles `INSERT_SUBREG` and related patterns that need target-specific lowering.

All three stubs are adjacent in memory (within 48 bytes of each other), confirming they are trivial return-false implementations in the NVPTXInstrInfo class.

## Register Class Assignment During Emission

When creating virtual registers for SDNode results, `CreateVirtualRegisters` (`sub_2E8B400` path) performs:

1. For each result value of the SDNode, obtain the register class from `TII->getRegClass(II, i)`.
2. Refine based on the value type: if the type is legal, compute `TLI->getRegClassFor(VT, isDivergent)` and intersect with the instruction constraint via `TRI->getCommonSubClass`.
3. The divergence flag (`SDNode::isDivergent`) is critical in NVPTX: divergent values must go into general-purpose registers (not uniform/constant registers), which affects class selection.
4. If a result's sole consumer is a CopyToReg to a virtual register in a compatible class, reuse the CopyToReg destination directly to avoid a redundant copy.
5. Create the virtual register via `MRI->createVirtualRegister(RC)` and add it as a def operand on the MachineInstr.

The `MinRCSize` threshold (4, unchanged from upstream) prevents over-constraining: if the intersection of all register class constraints would yield a class with fewer than 4 registers, the emitter inserts a COPY to a less-constrained virtual register instead.

## Implicit Def/Use Handling

After inserting a MachineInstr, the emitter processes implicit physical register definitions. This is essential for GPU instructions that clobber status registers or have side effects beyond their explicit operands.

The flow collects `UsedRegs` by scanning:
1. **Implicit defs beyond explicit results**: if `NumResults > NumDefs`, the extra results correspond to implicit physical register definitions from `MCInstrDesc::implicit_defs()`. For each such def that has at least one use, a CopyFromReg is emitted to capture the value.
2. **Glue chain uses**: the emitter walks the glue chain upward from the current node, collecting physical registers referenced by CopyFromReg nodes and RegisterSDNode operands.
3. **Dead marking**: `MachineInstr::setPhysRegsDeadExcept(UsedRegs)` marks any implicit def that is NOT in UsedRegs as dead, allowing the register allocator and later passes to ignore it.

The NVIDIA-specific extended flag at bit 36 (`0x1000000000`), checked via `sub_2E88A90`, appears to mark instructions with NVPTX-specific implicit uses -- possibly predicate registers or convergence control tokens that standard LLVM MachineInstr flags cannot represent.

## Internal Data Structures

### InstrEmitter Object Layout

The InstrEmitter instance carries three hash tables for tracking the SDNode-to-MachineInstr mapping:

| Offset | Name | Entry Size | Purpose |
|---|---|---|---|
| `+0x410` | VReg Map (Table A) | 16 bytes | SDNode result to virtual register |
| `+0x460` | MI Map (Table B) | 40 bytes | Glue chain to MachineInstr mapping |
| `+0x4D0` | Result Map (Table C) | 32 bytes | SDNode to result number |
| `+0x4E0` | forceEmit flag | 1 byte | When set, emit even dead nodes |

All three use LLVM's DenseMap implementation with open addressing and linear probing. The hash function is `key * 37` (LLVM's `DenseMapInfo<unsigned>::getHashValue`). Empty sentinel: `0xFFFFFFFF`. Tombstone: `0xFFFFFFFE`. Table C uses an extended sentinel `0xFFFFFFFFFFFFF000`. Rehash triggers at 3/4 load factor: `entry_count * 4 >= capacity * 3`. Growth is handled by `sub_2E29BA0` which doubles capacity and rehashes.

### SDOperand Output Record

Each emitted result is recorded in a 40-byte (0x28) structure:

```
struct EmitResultRecord {  // 40 bytes
    SDNode *producer;         // +0x00: SDNode that produced this result
    int32_t src_vreg;         // +0x08: source virtual register (-1 if physical)
    int32_t dst_vreg;         // +0x0C: destination virtual register (-1 if unassigned)
    TargetRegisterClass *RC;  // +0x10: register class pointer (or NULL)
    unsigned sub_reg_idx;     // +0x18: sub-register index (or 0)
    uint32_t flags;           // +0x20: tied, early_clobber, implicit bits
};
```

### SDNode Field Offsets

Confirmed SDNode field layout from the binary (matches LLVM 20.0.0 base with minor NVIDIA extensions):

| Offset | Type | Field |
|---|---|---|
| `+0x00` | tagged ptr | Chain/glue link (low 3 bits = type tag) |
| `+0x08` | uint32 | Use count / reference count |
| `+0x20` | ptr | Operand array pointer |
| `+0x28` | uint32 | Operand count (low 24 bits) | flags (high 8 bits) |
| `+0x2C` | uint8 | Property flags (bit 2 = isTied, bit 3 = hasEarlyClobber) |
| `+0x30` | tagged ptr | First predecessor link |
| `+0x38` | tagged ptr | Glue result chain |
| `+0x44` | uint16 | Opcode |
| `+0x78` | uint32 | Reference count (dead node detection) |

Tagged pointers are stripped throughout with `AND 0xFFFFFFFFFFFFFFF8` (clear low 3 bits). Physical registers are encoded with bit 31 set (negative int32); extraction uses `AND 0x7FFFFFFF` followed by a shift-left by 4 to index the register descriptor table.

## Dead Copy Elimination

After the main emission loop completes, a dedicated cleanup pass (Phase 12 in the binary, offset `0x2EE0816`) scans all emitted COPY instructions:

1. For each copy whose source is a CopyFromReg result (opcode `0x14`): check via `sub_2E8B100` (`isUnusedReg`) whether any downstream instruction uses the copy's destination.
2. If unused: call `sub_2E88E20` (`eraseFromParent`) to delete the MI immediately.
3. If used: call `sub_2EBF120` (`clearKillFlags`) on both source and destination registers to prevent incorrect kill annotations from propagating to the register allocator.
4. Any MI queued for deferred deletion is added to the dead list at `InstrEmitter+0x4A0` via `sub_2ED56A0`.

This pass is notably aggressive in CICC relative to upstream. The raw report notes that NVPTX's SelectionDAG generates many redundant copies when lowering kernel parameter loads, because each parameter maps to a fixed physical register (`%r1`--`%r255` corresponding to PTX parameter registers). The dead copy pass reclaims these immediately rather than deferring to a later dead MI elimination pass.

## NVIDIA Modifications vs Stock LLVM

| Area | Upstream LLVM | CICC v13.0 |
|---|---|---|
| EmitNode dispatch | Two separate functions: `EmitMachineNode` + `EmitSpecialNode` | Single merged function `sub_2EDDF20` with bit-table dispatch |
| CopyToReg | Inline in `EmitSpecialNode` | Factored into dedicated `sub_2ED95B0` |
| Custom inserter check | Single vtable call to `EmitInstrWithCustomInserter` | Triple vtable dispatch (0xB8, 0x348, 0x160) |
| Extended MI flags | Standard LLVM flag set (32 bits) | Bit 36 (`0x1000000000`) for NVPTX-specific semantics |
| Dead copy elimination | Post-emission pass in ScheduleDAGSDNodes | Inlined aggressive cleanup within EmitNode |
| Stack frame | ~300--400 bytes typical | 872 bytes (multiple inline SmallVectors and hash tables) |
| Self-recursion | Not self-recursive | Self-recursive for multi-result SDNode chains |

## Complexity

- Main emission loop: O(N) in the number of scheduled SDNodes.
- Hash table lookups: O(1) amortized with rehashing at 3/4 load.
- Dead copy elimination: O(C * U) where C = copies emitted, U = average uses per register.
- Glue chain traversal: O(G) per node where G = glue chain length (typically 1--5).
- Memory: O(N) for the three hash tables + O(R) for result records.

## Function Map

| Address | Identity | Notes |
|---|---|---|
| `sub_2EDDF20` | `InstrEmitter::EmitNode` | Main entry, 11,722 bytes |
| `sub_2EE0CF0` | `ScheduleDAGSDNodes::EmitSchedule` | Top-level driver, 59KB |
| `sub_2ED95B0` | `EmitCopyToReg` | Dedicated CopyToReg handler |
| `sub_2E8B400` | `getRegForValue` | SDValue to VReg mapping |
| `sub_2E8B100` | `isUnusedReg` | Dead register predicate |
| `sub_2DADC00` | `isDeadNode` | Dead SDNode predicate |
| `sub_2E88E20` | `eraseFromParent` | MachineInstr deletion |
| `sub_2E88A90` | `hasProperty` | Register/operand flag query |
| `sub_2EBEE10` | `getVRegDef` | Virtual register definition lookup |
| `sub_2EBEF70` | `isPhysReg` | Physical vs virtual register check |
| `sub_2EBECB0` | `replaceRegWith` | Virtual register substitution |
| `sub_2EBF120` | `clearKillFlags` | Remove kill annotations |
| `sub_2ED7930` | Sub-register resolution | SUBREG_TO_REG handling |
| `sub_2EDB7A0` | `EmitSubregNode` | Sub-register copy emission |
| `sub_2EDD7E0` | `EmitCopyToRegClassOp` | Class-constrained copy |
| `sub_2ED3660` | `ProcessOperands` | EmitMachineNode core |
| `sub_2E6D360` | `isAllocatableInClass` | Register class membership |
| `sub_2E5E6D0` | `DenseMap::find` | SDNode-to-MI lookup |
| `sub_2ED56A0` | `addToDeadList` | Queue MI for deletion |
| `sub_2E29BA0` | `DenseMap::grow` | Hash table resize |
| `sub_2ED11C0` | NVPTXInstrInfo default | `EmitInstrWithCustomInserter` stub |
| `sub_2ED11E0` | NVPTXInstrInfo default | `getInsertSubreg` stub |
| `sub_2ED11F0` | NVPTXInstrInfo default | `expandPostRAPseudo` stub |
| `sub_2E4F5F0` | NVPTXRegisterInfo | Register class query vtable method |

## Cross-References

- [SelectionDAG & Instruction Selection](./selectiondag.md) -- the DAG construction and pattern-matching phase that produces the SDNodes consumed by InstrEmitter
- [Instruction Scheduling](./scheduling.md) -- `ScheduleDAGSDNodes::EmitSchedule` calls InstrEmitter after linearizing the scheduled sequence
- [Register Allocation](./register-allocation.md) -- the VRegs created by InstrEmitter flow into the register allocator
- [Register Coalescing](./register-coalescing.md) -- coalesces the COPY instructions emitted here
- [AsmPrinter & PTX Body Emission](../infra/asmprinter.md) -- the final consumer of the MachineInstrs produced by InstrEmitter
