# Strength Reduction

> *All addresses in this page apply to ptxas v13.0.88 (CUDA 13.0). Other versions will differ.*

Phase 21 (`OriStrengthReduce`) replaces expensive arithmetic operations with cheaper equivalents in the Ori IR. It runs early in the optimization pipeline -- after loop simplification (phase 18) and live range splitting (phase 19), but before loop unrolling (phase 22) and software pipelining (phase 24). This placement is deliberate: strength reduction benefits from canonicalized loop structure and benefits subsequent loop transformations by simplifying induction variable expressions.

Strength reduction in ptxas is not a single monolithic pass. It is distributed across three layers, each operating at a different abstraction level:

1. **Phase 21 (`OriStrengthReduce`)** -- Ori-level induction variable strength reduction on the use-def graph
2. **Peephole patterns** -- SASS-level algebraic simplifications in the `MainPeepholeOptimizer` (`sub_83EF00`)
3. **Division lowering templates** -- Newton-Raphson integer division sequences emitted during instruction selection

| | |
|---|---|
| **Phase index** | 21 |
| **Phase name** | `OriStrengthReduce` |
| **Category** | Optimization |
| **Pipeline position** | Stage 2 (Early Optimization), between PGO (phase 20) and loop unrolling (phase 22) |
| **Vtable address** | `off_22BD910` |
| **`execute()`** | `sub_C5FB30` (wrapper) -> `sub_752E40` (core logic, 359 lines decompiled, ~1.2 KB binary) |
| **`isNoOp()`** | `sub_C5F3D0` -- returns 0 (always runs) |
| **`getName()`** | `sub_C5F3C0` -- returns 21 |
| **Gate knob** | 487 (general optimization enablement) |
| **Key helpers** | `sub_745A80` (replacement register creator), `sub_91BF30` (virtual register allocator), `sub_A13890` (use-def chain iterator), `sub_9253C0` (instruction deleter) |
| **Peephole SHR+SHL->BFE** | `sub_81DB30` (matcher: `sub_81D7E0`) |
| **Peephole BFE+ADD** | `sub_81DDD0` (matcher: `sub_81DBC0`) |
| **Division templates** | `sub_1724A20` (32-bit, 28 KB), `sub_1728930` (64-bit unsigned, 16.5 KB), `sub_1727AC0` (64-bit signed, 13.7 KB) |

## Phase 21: Induction Variable Strength Reduction

### Architecture

The execute wrapper (`sub_C5FB30`) gates on multi-function compilation (function count > 1 via `sub_7DDB50`) and delegates to `sub_752E40` with parameters `(context, 0, 0, 0)`.

`sub_752E40` is the core. It performs a single-pass walk over the instruction list, focusing on a specific intermediate opcode -- opcode 137 (`SM73_FIRST`), masked with `& 0xFFFFCFFF` to strip modifier bits in the opcode field at instruction offset +72. The ROT13 name `SM73_FIRST` is a generation boundary marker name, but the Ori IR reuses this opcode slot at runtime for IMAD-like multiply-accumulate instructions in their pre-lowered form. The actual SASS `IMAD` is opcode 1.

### Algorithm

The pass executes in two phases within a single call:

**Phase 1 -- Trivial multiply elimination.** The first loop walks the instruction list (`*(context+272)` is the list head). For each instruction with masked opcode == 137 (`SM73_FIRST`; IMAD-like):

1. Check if the destination register (operand at +84) has no uses (`*(def+56) == NULL`) AND the source chain is empty (`*src_chain == NULL`). If both hold, delete the instruction via `sub_9253C0` -- it is dead.
2. Otherwise, for each source operand (iterating from operand count - 1 down to 0):
   - Check operand type: must be register (`(operand >> 28) & 7 == 1`)
   - Look up the register definition in the SSA value table (`*(context+88) + 8 * (operand & 0xFFFFFF)`)
   - Check the definition has no special flags (`*(def+48) & 0x400000022 == 0`)
   - Check the register type is not 9 (predicate register)
   - Check the source operand's use chain is empty (single-use) and the def has no other users
   - If all conditions hold, call `sub_91BF30` to allocate a replacement register with the same type, then patch the operand in place

**Phase 2 -- Use-def chain traversal.** The second loop walks the instruction list again. For each instruction with operands that have been marked (flag `0x100` at `instruction[6]`, set during initialization):

1. For each source operand with a use chain:
   - Compute the replacement register via `sub_745A80(context, def, a4)`, which:
     - Allocates a new virtual register via `sub_91BF30` with the same type as the original
     - Copies the data type field (`+16`) and relevant flags (`0x40`, `0x10`, `0x8` bits of the flags word at `+48`)
     - Returns the new register ID
   - If the operand was not yet marked (flag `0x100` bit not set), initialize it and mark as "needs strength reduction"
   - Traverse the use chain as a worklist: for each user of the replaced register, check if *its* uses also need updating, growing the worklist dynamically (doubling allocation via pool allocator)
2. Track how many source operands were rewritten (`v72` counter)
3. After processing all operands of an instruction: if the instruction is still opcode 137 (`SM73_FIRST`; IMAD-like) and certain conditions hold (destination matches source pattern, specific operand bit patterns), either delete it or convert it to opcode 130 / `0x82` (`HSET2` in ROT13; used as an internal MOV-like marker -- actual SASS `MOV` is opcode 19)

The worklist traversal is the key algorithmic insight: when a multiply's result feeds into another multiply, the chain of strength reductions propagates transitively through the def-use graph.

### Data Flow

```
sub_C5FB30 (execute wrapper)
  |
  +-- sub_7DDB50: check function count > 1
  |
  +-- sub_752E40 (core logic)
        |
        +-- sub_7468B0 / vtable+152: check knob 487 (optimization enabled)
        |
        +-- Phase 1: Walk instruction list (*(ctx+272))
        |     +-- For opcode 137 (`SM73_FIRST`; IMAD-like) instructions:
        |     |     +-- sub_9253C0: delete dead instructions
        |     |     +-- sub_91BF30: allocate replacement registers
        |     |
        |     +-- Clear flag 0x100 on all basic blocks (*(ctx+104) chain)
        |     +-- Set flag 0x40 at ctx+1385
        |
        +-- sub_A13890: initialize use-def traversal context
        |     +-- Creates context object with vtable off_21DBEF8
        |     +-- Sets up iterator with vtable off_21B4FD0
        |
        +-- Phase 2: Walk instruction list again
        |     +-- For each source operand with use chain:
        |     |     +-- sub_745A80: create replacement register
        |     |     +-- Worklist propagation through use chain
        |     |
        |     +-- Convert trivial IMAD to MOV (opcode 130 / `0x82`, `HSET2`; MOV-like)
        |     +-- sub_9253C0: delete fully reduced instructions
        |
        +-- sub_7B52B0: optional post-reduction scheduling pass
        |     (called if any replacements were made, flag v76)
        |
        +-- sub_8E3A20: destroy use-def context
```

### Instruction Representation

The pass operates on the Ori IR instruction format. Relevant fields:

| Offset | Field | Usage in this pass |
|---|---|---|
| +8 | next pointer | Instruction list traversal |
| +64 | source operand chain | Array of `{use_chain_ptr, ...}` per operand |
| +72 | opcode (DWORD) | Bits 0-11 = base opcode, bits 12-13 = modifier (masked with `0xCF`) |
| +80 | operand count | Number of source operands |
| +84 | operand[0] | First source operand descriptor (bits 28-30 = type tag, bits 0-23 = register ID) |
| +92 | operand[1] | Second source operand |
| +100 | operand[2] | Third source operand (for IMAD: accumulator) |

Operand type tags (bits 28-30):
- `1` = register operand (index into SSA value table at `*(context+88)`)
- `2`, `3` = immediate operand
- `7` = special/predicate

Register definition structure (from SSA value table):

| Offset | Field | Usage |
|---|---|---|
| +8 | register ID | Unique identifier |
| +16 | data type | Copied to replacement register |
| +20 | use count | Checked for single-use optimization |
| +28 | replacement ID | Set by `sub_745A80` to point to strength-reduced version |
| +48 | flags (QWORD) | Bit 0x100 = "marked for strength reduction", bit 0x40 = volatile, bit 0x10/0x8 = scheduling hints |
| +56 | defining instruction | Pointer to the instruction that defines this register |
| +64 | register class | Type code (2/3 = integer, 4 = predicate, 7 = special, 9 = predicate) |

## Peephole Strength Reduction Patterns

The `MainPeepholeOptimizer` (`sub_83EF00`, 29 KB, case 2 of the opcode switch) applies algebraic strength reduction patterns at the SASS instruction level. These run later in the pipeline than phase 21 and operate on concrete SASS opcodes rather than the pre-lowered intermediate form.

### Pattern: SHR + SHL -> BFE (Bit-Field Extract)

**Matcher:** `sub_81D7E0` (166 lines decompiled)
**Emitter:** `sub_81DB30`
**Target opcodes:** 290, 151, or 2 (various ALU forms) with operand size 11 or 12

Recognition:
1. The instruction must have two register source operands (type tag 1), no modifier bits, no special flags
2. Source operand 0's definition must be opcode 213 (SHL) or 214 (SHR)
3. Source operand 1's definition must be the complementary shift (SHR if 0 was SHL, or vice versa)
4. Both shift definitions must have immediate shift amounts (type tag 2 or 3)
5. The shift amounts must sum to 32 (i.e., `SHL(x, n)` paired with `SHR(x, 32-n)`)
6. Both definitions must dominate the current instruction (`sub_1245740` dominance check)
7. Loop depth heuristic: if the shift definitions are in a shallower loop than the current instruction (checked via block RPO depth at `*(block+156)`), the transformation may be suppressed to avoid increasing register pressure

Transformation:
```
Before:  t1 = SHL(x, n)          ; opcode 213
         t2 = SHR(x, 32-n)       ; opcode 214
         r  = ALU(t1, t2)        ; opcode 290/151/2

After:   r  = BFE(x, ...)        ; opcode 210 (bit-field extract)
```

The emitter calls `sub_9314F0` to create a BFE instruction (opcode 210) with the appropriate operands, then `sub_9253C0` to delete the original ALU instruction.

### Pattern: BFE Folding into ADD

**Matcher:** `sub_81DBC0` (83 lines decompiled)
**Emitter:** `sub_81DDD0`
**Target opcode:** 2 (IADD) with operand size 11 or 12

Recognition:
1. One source operand is defined by opcode 210 (BFE)
2. The BFE has no modifier bits, no special flags on the last operand
3. The BFE's immediate operand (shift amount) is 1-31
4. The BFE has a single use (use count <= 1)
5. Dominance check passes

Transformation:
```
Before:  t = BFE(x, amount)      ; opcode 210
         r = IADD(t, y)          ; opcode 2

After:   r = LOP3/SHF(x, y, ...) ; opcode 102, combining shift+add
```

The emitter creates opcode 102 (a combined shift-and-add operation) with encoded shift amount (`8 * amount | 0x60000002`).

## Integer Division Lowering

Integer division and modulo by non-constant values are lowered to multi-instruction sequences during instruction selection. This is not part of phase 21 but is the most visible strength reduction in ptxas output. The sequences use the classic Barrett reduction / Newton-Raphson reciprocal algorithm.

### 32-bit Division -- `sub_1724A20`

**Size:** 28,138 bytes decompiled (the largest function in the 0x1723000-0x17F8000 ISA description range)
**Called from:** `sub_1727130` (template driver)
**Instruction count:** ~35 SASS instructions emitted

Algorithm (unsigned 32-bit `a / b`):
1. Convert divisor to float: `I2F(b)` (opcode 0xD5)
2. Compute approximate reciprocal via `MUFU.RCP` (opcode 0x3C)
3. Convert back to integer: `F2I(1/b)` (opcode 0xD6)
4. Refine via multiply-add: `IMAD(q, b, ...)` (opcode 0x6E)
5. Correction step with conditional branch: `ISETP` + `BRA` (opcodes 0xC9, 0x5F)
6. Final adjustment via `IADD` (opcode 0x02)

Key constants allocated via `sub_91D160`:
- 23 (float exponent bias for mantissa extraction)
- 255 (exponent mask)
- 127 (IEEE 754 single-precision bias)
- 254 (double-bias for overflow guard)
- 1, -1 (correction increments)

The temporary register pool uses indices 90-126 from a parameter array (`a7[]`), providing 37 dedicated scratch registers for the sequence.

### 64-bit Division

Two variants handle 64-bit operands:
- **`sub_1728930`** (16,545 bytes): unsigned 64-bit division. Emits longer sequences with double-width multiply and carry propagation.
- **`sub_1727AC0`** (13,776 bytes): signed 64-bit division. Parallel structure with sign-handling logic.

Both are called from `sub_1729B50`.

### Division by Constant

Division by compile-time constant is handled separately during constant folding (in the `GeneralOptimize` bundle passes). The classic magic-number multiplication technique (Granlund-Montgomery) converts `x / C` into `MULHI(x, magic) >> shift`, avoiding the Newton-Raphson sequence entirely. This produces 2-3 instructions instead of ~35.

## SASS Cost Model

The profitability of strength reduction on NVIDIA GPUs differs from CPUs in several important ways:

**Integer multiply is cheap.** Modern NVIDIA GPUs (sm_70+) have dedicated integer multiply-add (IMAD) functional units. IMAD has the same throughput as IADD on most architectures -- both are single-cycle operations on the integer ALU. This means the classical "replace multiply with shift+add" transformation is often **not profitable** on GPU. ptxas does not aggressively replace multiplies with shift chains the way CPU compilers do.

**Integer division is expensive.** There is no hardware integer divider. Division must be lowered to the ~35-instruction Newton-Raphson sequence described above. This is why division-by-constant is a high-priority optimization -- replacing 35 instructions with 2-3 is a massive win.

**Shift operations.** SHL and SHR are single-cycle on the integer ALU, same throughput as IADD and IMAD. However, they use a different functional unit slot on some architectures, which can matter for scheduling.

**BFE (bit-field extract)** is a dedicated single-cycle instruction. Recognizing SHR+SHL pairs and folding them to BFE saves an instruction and a register, which is the primary motivation for the peephole patterns.

**Register pressure dominates.** On GPUs, the primary cost metric is not instruction count but register pressure, because register count directly determines occupancy (the number of concurrent warps). The strength reduction pass checks loop depth before transformations and suppresses replacements that would increase register pressure in inner loops (the RPO depth comparison in `sub_81D7E0`).

This explains why phase 21's core logic is relatively compact (~1.2 KB binary) compared to CPU compilers' strength reduction passes: the GPU cost model makes fewer algebraic replacements profitable, so the pass focuses narrowly on use-def chain simplification and trivial multiply elimination rather than elaborate pattern tables.

## Pipeline Context

Phase 21 runs after:
- **Phase 18 (`OriLoopSimplification`)** -- loops are canonicalized with single entry, single back-edge, and preheaders
- **Phase 19 (`OriSplitLiveRanges`)** -- live ranges are split at loop boundaries
- **Phase 20 (`PerformPGO`)** -- profile data is applied (block weights inform the cost model)

Phase 21 runs before:
- **Phase 22 (`OriLoopUnrolling`)** -- simplified induction variables enable better unroll decisions
- **Phase 24 (`OriPipelining`)** -- strength-reduced loops are more amenable to software pipelining
- **Phase 29 (`GeneralOptimize`)** -- compound pass cleans up any dead code left by strength reduction

The `GeneralOptimize` bundles (phases 13, 29, 37, 46, 58, 65) also perform algebraic simplification that overlaps with strength reduction -- specifically constant folding of multiply-by-power-of-2 to shifts. Phase 21 handles the more complex cases that require use-def chain analysis, while `GeneralOptimize` handles local, single-instruction rewrites.

## Function Map

| Address | Size | Function | Role |
|---|---|---|---|
| `sub_C5FB30` | 9 bytes | `OriStrengthReduce::execute` | Vtable entry, gates on function count |
| `sub_C5F3C0` | 16 bytes | `OriStrengthReduce::getName` | Returns phase index 21 |
| `sub_C5F3D0` | 16 bytes | `OriStrengthReduce::isNoOp` | Returns 0 (never skipped) |
| `sub_752E40` | ~1.2 KB | Core strength reduction | Use-def chain walk, replacement |
| `sub_745A80` | 168 bytes | Replacement register creator | Allocates new register with copied type/flags |
| `sub_91BF30` | ~400 bytes | Virtual register allocator | Creates 160-byte register descriptor |
| `sub_9253C0` | 325 bytes | Instruction deleter | Unlinks and removes instruction (634 callers) |
| `sub_A13890` | ~2 KB | Use-def context initializer | Sets up chain traversal structures |
| `sub_81D7E0` | ~660 bytes | SHR+SHL->BFE matcher | Peephole pattern recognizer |
| `sub_81DB30` | ~112 bytes | SHR+SHL->BFE emitter | Emits BFE (opcode 210) |
| `sub_81DBC0` | ~330 bytes | BFE+ADD matcher | Peephole pattern recognizer |
| `sub_81DDD0` | ~100 bytes | BFE+ADD emitter | Emits combined shift-add (opcode 102) |
| `sub_1724A20` | 28,138 bytes | 32-bit div/mod template | Newton-Raphson integer division |
| `sub_1728930` | 16,545 bytes | 64-bit unsigned div template | Double-width Newton-Raphson |
| `sub_1727AC0` | 13,776 bytes | 64-bit signed div template | Signed variant |
| `sub_1727130` | ~2 KB | Division template driver | Allocates temps, dispatches to templates |

## Cross-References

- [Pass Inventory & Ordering](index.md) -- complete 159-phase table showing phase 21's position
- [GeneralOptimize Bundles](general-optimize.md) -- algebraic simplification sub-passes that complement strength reduction
- [Loop Passes](loop-passes.md) -- loop canonicalization (phase 18) that enables induction variable analysis
- [Ori IR Overview](../ir/overview.md) -- instruction format, opcode encoding (ROT13), register model
- [Peephole Optimization](../codegen/peephole.md) -- `MainPeepholeOptimizer` containing SHR+SHL->BFE patterns
- [Newton-Raphson Templates](../codegen/templates.md) -- detailed analysis of division lowering sequences
- [Scheduling](../scheduling/overview.md) -- `sub_7B52B0` scheduling pass called after strength reduction
- [Knobs System](../config/knobs.md) -- knob 487 controlling optimization enablement
