# InstrEmitter

> **NVIDIA-modified pass.** See [Differences from Upstream](#differences-from-upstream-llvm) for GPU-specific changes.

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

## NVIDIA Extended Flag: Bit 36 (`0x1000000000`)

Standard LLVM MachineInstr flags occupy bits 0--31 of the flags word (is_def, is_implicit, is_dead, is_kill, is_undef, is_early_clobber, etc.). CICC extends this to a 64-bit flags field and reserves bit 36 (`0x1000000000`) for an NVIDIA-specific purpose. The flag is queried via `sub_2E88A90` (`hasProperty`) with argument `rsi = 0x1000000000, edx = operand_index`.

### Where Bit 36 Is Checked

There are exactly two call sites within `sub_2EDDF20`:

**Site 1 -- Generic emission path (`0x2EDE50A`--`0x2EDE523`)**

```
0x2EDE4EF: mov  eax, [r13+2Ch]          ; load SDNode property flags
0x2EDE4F3: test eax, 0x20000            ; bit 17 = hasDebugValue?
0x2EDE4F8: jnz  skip_flag_check         ; if set, skip the bit-36 test
0x2EDE4FA: test al, 4                   ; bit 2 = isTied
0x2EDE4FC: jnz  loc_2EDF064             ; tied operand -> different path
0x2EDE502: test al, 8                   ; bit 3 = hasGlue
0x2EDE504: jz   loc_2EDF064             ; no glue -> different path
0x2EDE50A: mov  edx, 1                  ; operand index = 1
0x2EDE50F: mov  rdi, r13                ; SDNode*
0x2EDE512: mov  rsi, 0x1000000000       ; bit 36 flag mask
0x2EDE51C: call sub_2E88A90             ; hasProperty(node, flag, idx)
0x2EDE521: test al, al
0x2EDE523: jnz  loc_2EDE086             ; if set -> skip emission entirely
```

**Site 2 -- CopyFromReg-adjacent path (`0x2EDEE5D`--`0x2EDEE86`)**

```
0x2EDEE5D: test al, 4                   ; bit 2 = isTied
0x2EDEE5F: jnz  loc_2EDEFA2             ; tied -> sub-register path
0x2EDEE65: test al, 8                   ; bit 3 = hasGlue
0x2EDEE67: jz   loc_2EDEFA2             ; no glue -> sub-register path
0x2EDEE6D: mov  edx, 1                  ; operand index = 1
0x2EDEE72: mov  rdi, r13                ; SDNode*
0x2EDEE75: mov  rsi, 0x1000000000       ; bit 36 flag mask
0x2EDEE7F: call sub_2E88A90             ; hasProperty(node, flag, idx)
0x2EDEE84: test al, al
0x2EDEE86: jnz  loc_2EDE100             ; if set -> skip (no MI emitted)
```

### Guard Conditions and Semantics

Both sites share the same guard pattern: the flag is only checked when the SDNode's property byte at `+0x2C` satisfies `bit_3_set AND NOT bit_2_set` -- i.e., the node has a glue result chain but is not a tied operand. This narrows the check to nodes that participate in glue chains: typically multi-instruction sequences like texture fetches, surface operations, and warp-level intrinsics where a chain of SDNodes must emit as a contiguous bundle.

When `hasProperty(node, 0x1000000000, 1)` returns true, the emitter skips the node entirely. The operand index of 1 means the flag is checked on the first data operand (operand 0 is typically the chain input). The effect is that nodes carrying bit 36 on operand 1 are treated as "already materialized" -- their value has been produced by a preceding glued instruction and does not require a separate MachineInstr.

The most likely interpretation of bit 36 is **"implicit glue consumer already emitted"**: when a glued predecessor has already produced the value as a side effect (e.g., a texture fetch that writes both the result and a predicate), the glue consumer SDNode carries bit 36 to tell the emitter that no additional COPY or MI is needed. This is consistent with the check position immediately after `getRegForValue` succeeds -- the VReg mapping exists, the glue chain has been walked, and the emitter is about to create a potentially redundant MI.

### `sub_2E88A90` Calling Convention

The function serves as a universal property query across the emitter and other codegen passes. Observed flag values and their meanings:

| Flag Value | Bit | Meaning | Call Sites |
|---|---|---|---|
| `0x80` | 7 | isCall | Instruction scheduler (`sub_2EE40E0`) |
| `0x200` | 9 | isReservedReg | Branch folding (`sub_2F33DD0`) |
| `0x80000` | 19 | isImplicit | InstrEmitter generic path, StructurizeCFG |
| `0x100000` | 20 | isSimple / isMachineReg | InstrEmitter CopyFromReg, dead copy pass |
| `0x400000` | 22 | isSubRegister | InstrEmitter sub-register resolution |
| `0x40000000` | 30 | isAllocatable | InstrEmitter CopyFromReg class check |
| `0x1000000000` | 36 | NVIDIA: implicit glue consumer | InstrEmitter only (2 sites) |

The function signature is `bool hasProperty(SDNode *node, uint64_t flag_mask, unsigned operand_idx)`. It reads the MCInstrDesc via `[node+10h] -> [desc+18h]`, extracts a bit field by shifting right by the appropriate amount, and ANDs with 1 to produce a boolean result.

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

After the main emission loop completes, a dedicated cleanup pass (Phase 12 in the binary, offset `0x2EE0816`--`0x2EE09AC`) scans all emitted result records and eliminates redundant COPY instructions. This is notably aggressive compared to upstream LLVM, which defers dead copy removal to a separate `DeadMachineInstrElimination` pass later in the pipeline. CICC performs it inline because NVPTX's SelectionDAG generates massive numbers of redundant copies when lowering kernel parameter loads -- each parameter maps to a fixed physical register (`%r1`--`%r255` corresponding to PTX parameter registers), and the DAG legalizer inserts CopyFromReg nodes for every parameter access.

### Dead Copy Elimination Algorithm

The algorithm walks the emitted result record array (0x28-byte stride, accumulated during Phases 4--11) and classifies each record for deletion or preservation.

```
DeadCopyElimination(InstrEmitter *self, ResultRecord *records, int count):
    // records is at [rbp-0x250], count at [rbp-0x248]
    // stride = 0x28 (40 bytes per record)

    end = records + count * 0x28
    cursor = records

    while cursor < end:
        MI = cursor->producer             // [rbx+0x00]: the MachineInstr*
        TII = self->TargetInstrInfo       // [r14+0x08]

        // Step 1: Classify by opcode
        if MI->opcode == 0x14:            // CopyFromReg
            // CopyFromReg-specific path: virtual dispatch to target
            vtable = TII->vtable
            result = vtable[0xF0](         // ~30th virtual method
                MI,                        // the CopyFromReg MI
                &cursor[0x08],             // source vreg slot
                /* additional args */
            )
            // This checks whether the target considers the copy
            // sinkable or rematerlizable -- NVPTX overrides this
            // for parameter register copies that are trivially dead

        else:
            // Generic MI path: check via vtable[0x350]
            result = TII->vtable[0x350](MI, cursor, ...)

        // Step 2: Check source register kill flags
        src_reg = cursor->src_vreg        // [rbx+0x08]
        if src_reg < 0:                   // physical register (sign bit set)
            clearKillFlags(self->MRI, src_reg)  // sub_2EBF120

        // Step 3: Check dest register kill flags
        dst_reg = cursor->dst_vreg        // [rbx+0x0C]
        if dst_reg < 0:                   // physical register
            clearKillFlags(self->MRI, dst_reg)  // sub_2EBF120

        // Step 4: Determine if MI is dead
        //   Check opcode: if (MI->opcode - 1) <= 1 (opcode 1 or 2)
        //   then check MI->operand[0] byte [+0x40] bit 4 (0x10)
        //   which indicates "result consumed by inline fold"
        opc = MI->opcode
        if (opc == 1 || opc == 2):        // COPY or REG_SEQUENCE
            if MI->operands[0].flags & 0x10:   // inline folded
                goto mark_dead

        // Step 5: Property gate
        flags_2c = MI->flags_2c           // [rdi+2Ch]
        if !(flags_2c & 0x04):            // bit 2 not set
            // Check TSFlags bit 20 via descriptor
            desc = MI->MCInstrDesc        // [rdi+10h]
            tsflags = desc->TSFlags       // [desc+18h]
            is_simple = (tsflags >> 20) & 1
            if !is_simple:
                goto emit_and_advance     // not a candidate

        // (falls through only when bit 2 set OR TSFlags bit 20 set)

        // Step 6: Check hasProperty(0x100000, 1) -- isMachineReg
        has_prop = hasProperty(MI, 0x100000, 1)   // sub_2E88A90
        if !has_prop:
            // MI is deletable: call eraseFromParent
            eraseFromParent(MI)            // sub_2E88E20
            advance cursor by 0x28
            continue

    mark_dead:
        // Step 7: Liveness check via isUnusedReg
        unused = isUnusedReg(MI)           // sub_2E8B100
        if unused:
            // Still has a def -- erase immediately
            eraseFromParent(MI)            // sub_2E88E20
        else:
            // Defer: add to dead list for bulk deletion
            addToDeadList(self->deadList, MI)  // sub_2ED56A0
            // deadList is at InstrEmitter+0x4A0

        advance cursor by 0x28
```

### Glue Chain Walk in Dead Copy Context

After the per-record loop, the emitter performs a secondary traversal for CopyFromReg records that survived deletion. For each surviving copy whose SDNode has a glue result (`[r13+38h] != 0`):

1. Walk the glue chain backward via `[r13+0] & 0xFFFFFFFFFFFFFFF8` (strip tag bits).
2. For each predecessor in the chain, check `[rax+2Ch] & 4` -- if the predecessor has been scheduled (bit 2 set), continue walking.
3. If the predecessor has an unresolved glue reference (`[r13+38h]` non-null) and the predecessor's MI has zero uses after copy elimination, mark it for deferred deletion too.

This secondary walk catches cascading dead copies: when a CopyFromReg is deleted, its glued predecessor may also become dead.

### Deferred Deletion via Dead List

MIs added to `InstrEmitter+0x4A0` via `sub_2ED56A0` are not deleted immediately. Instead, they are accumulated and deleted in bulk during Phase 14 (final cleanup at `0x2EE0C0B`). The dead list is a SmallVector<MachineInstr*> with 8 inline entries (64 bytes inline buffer), growing via `sub_C8D5F0` if needed. Bulk deletion avoids iterator invalidation during the emission loop and is more cache-friendly for large basic blocks.

### Why NVPTX Needs Aggressive Dead Copy Elimination

NVPTX kernel signatures routinely have 20--60 parameters, each lowered through a CopyFromReg from a fixed physical register. The SelectionDAG legalizer creates CopyFromReg SDNodes for each parameter load, but many parameters are only used in a subset of the kernel's basic blocks. Without immediate dead copy elimination, a kernel with 50 parameters would carry 50 COPY MachineInstrs at function entry, most of which are dead in any given block. The standard LLVM `DeadMachineInstrElimination` pass would eventually clean these up, but doing so immediately during emission:

1. Reduces the MachineBasicBlock size that subsequent passes (register allocation, scheduling) must process.
2. Avoids creating unnecessary VReg-to-PhysReg interference entries in the register allocator.
3. Prevents false register pressure signals from dead copies during the MRPA (Machine Register Pressure Analysis) pass that NVIDIA uses for scheduling decisions.

## NVIDIA-Specific Emission Patterns

### Parameter Cascade Emission

NVPTX kernel entry functions map each parameter to a physical register via a cascade of CopyFromReg SDNodes. During emission, this produces a dense block of COPY MachineInstrs at the top of the entry MachineBasicBlock. The emitter handles this pattern specially:

1. When `EmitSchedule` processes the first SUnit, it detects a sequence of CopyFromReg nodes whose source registers are consecutive physical parameter registers (`%r1`, `%r2`, ...).
2. Each CopyFromReg is processed through the Phase 5 path (at `0x2EDF423`). The register class resolution at `0x2EDF4C2` uses the compressed bit-vector test to verify the destination belongs to the `Int32Regs` or `Int64Regs` class.
3. Dead copy elimination (Phase 12) immediately removes copies whose destinations have no users, reducing the entry block size before subsequent passes see it.

### Texture/Surface Glue Bundle Emission

Texture and surface operations are emitted as glue bundles: a chain of SDNodes connected by glue edges that must produce a contiguous sequence of MachineInstrs. The emitter walks the glue chain backward from the final node and emits predecessors first. The bit 36 flag is critical here: when a texture fetch produces both a data result and a predicate condition, the predicate-producing node carries bit 36 on its data operand, telling the emitter that the preceding glued instruction already materialized the value and no separate COPY is needed.

The triple vtable dispatch at the end of emission (Phase 5 in the algorithm) handles the expansion of texture pseudo-instructions: `EmitInstrWithCustomInserter` (vtable 0xB8) replaces the texture pseudo-MI with the actual address setup, sampler configuration, and fetch instruction sequence.

### Multi-Result SDNode Self-Recursion

When an SDNode produces multiple results (e.g., a `div`+`rem` pair or a load-with-predicate), the emitter calls itself recursively at `sub_2EDDF20` to emit MIs for each additional result. The self-recursive call shares the same InstrEmitter instance and hash tables. This is a CICC-specific pattern; upstream LLVM handles multi-result nodes in a loop within `EmitMachineNode` rather than via recursion. The recursive approach simplifies the handling of multi-result nodes that themselves have glue chains (e.g., a texture fetch that returns 4 components).

### Opcode-1/Opcode-2 Inline Fold Detection

During the dead copy scan (Phase 12, offset `0x2EE08A0`--`0x2EE08BA`), the emitter checks if the MI's opcode is 1 or 2 (COPY or REG_SEQUENCE). For these opcodes, it reads the first operand's byte at `[operand_array + 0x40]` and tests bit 4 (`0x10`). This bit indicates the result was consumed via an inline fold -- the consumer instruction selected a pattern that folds the copy directly into its own operand. When this bit is set, the COPY MI is marked dead regardless of its use count, because the consuming instruction no longer references it.

```
0x2EE08A0: movzx eax, word ptr [rdi+44h]   ; MI->opcode
0x2EE08A4: sub   eax, 1                     ; opcode - 1
0x2EE08A7: cmp   eax, 1                     ; is it 1 (COPY) or 2 (REG_SEQUENCE)?
0x2EE08AA: ja    not_copy                   ; no -> skip
0x2EE08AC: mov   rax, [rdi+20h]             ; MI->operands array
0x2EE08B0: test  byte ptr [rax+40h], 0x10   ; bit 4 = inline fold consumed
0x2EE08B4: jnz   mark_dead                  ; if folded -> dead
```

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
| Inline fold detection | Not present at this stage | Opcode-1/2 fold bit check during dead copy scan |
| Glue chain secondary walk | Not present | Cascading dead copy detection through glue predecessors |

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
| `sub_2ED1840` | operand comparison | Operand equality helper |
| `sub_2ED19B0` | MI builder | Additional MachineInstr construction |
| `sub_2ED41E0` | register mapping | Register mapping utility |
| `sub_2ED4900` | register info query | Register info accessor |
| `sub_2ED5D10` | MI property query | MachineInstr property reader |
| `sub_2EDA920` | emission utility | Additional emission helper |
| `sub_2EAB0C0` | `setDesc` | Sets MI operand descriptors during emission |
| `sub_2E31210` | `addOperand` | Appends operand to MachineInstr |
| `sub_2E31DD0` | MI manipulation | Additional MI manipulation utility |
| `sub_2E4EE60` | TRI utility | TargetRegisterInfo helper |
| `sub_2E4F5F0` | NVPTXRegisterInfo | Register class query vtable method |

## Differences from Upstream LLVM

| Aspect | Upstream LLVM | CICC v13.0 |
|--------|---------------|------------|
| **EmitNode structure** | Separate `EmitNode` and `EmitSpecialNode` dispatchers | Merged into single monolithic function (`sub_2EDDF20`, 11,722 bytes) with bit-table opcode classification |
| **CopyToReg handling** | Inline within EmitSpecialNode | Factored out to dedicated handler (`sub_2ED95B0`) for NVPTX's physical-register-heavy `.param` ABI |
| **MachineInstr flags** | Standard flag bits (up to bit ~20) | Extended flag at bit 36 (`0x1000000000`) not present in stock LLVM; marks NVIDIA-specific instruction properties |
| **Pseudo-expansion** | Single vtable dispatch for target pseudo-instructions | Triple vtable dispatch pattern gating custom expansion for GPU-specific pseudo-instructions |
| **Dead node predicate** | Standard `isDeadNode` check | Custom `sub_2DADC00` predicate with NVPTX-specific liveness criteria |
| **VReg hash table** | Standard `DenseMap` for value-to-VReg mapping | Custom hash with `key * 37` and 3/4 load factor rehash policy |

## Cross-References

- [SelectionDAG & Instruction Selection](./selectiondag.md) -- the DAG construction and pattern-matching phase that produces the SDNodes consumed by InstrEmitter
- [Instruction Scheduling](./scheduling.md) -- `ScheduleDAGSDNodes::EmitSchedule` calls InstrEmitter after linearizing the scheduled sequence
- [Register Allocation](./register-allocation.md) -- the VRegs created by InstrEmitter flow into the register allocator
- [Register Coalescing](./register-coalescing.md) -- coalesces the COPY instructions emitted here
- [AsmPrinter & PTX Body Emission](../infra/asmprinter.md) -- the final consumer of the MachineInstrs produced by InstrEmitter
