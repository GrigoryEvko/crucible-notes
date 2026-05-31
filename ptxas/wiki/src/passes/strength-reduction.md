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

### Scope limitation -- opcode 137 only

Despite the generic name, Phase 21 touches exactly one Ori IR opcode: **137** (`SM73_FIRST` in the ROT13 naming scheme). Every instruction in the function is visited, but the opcode check `(*(instr+72) & 0xFFFFCFFF) == 137` gates all transformations -- non-137 instructions are skipped entirely. The `& 0xFFFFCFFF` mask zeroes bits 12-13 (modifier field), so variants 137, 137|0x1000, 137|0x2000, and 137|0x3000 all match.

Opcode 137 is the pre-lowered IMAD (integer multiply-accumulate). The `SM73_FIRST` ROT13 label is a generation boundary name; ptxas reuses this slot in the Ori IR for IMAD-like operations before instruction selection lowers them to SASS `IMAD` (opcode 1). The pass therefore performs strength reduction exclusively on integer multiply chains.

### Algorithm

The pass executes in two loops within a single call to `sub_752E40`:

**Loop 1 -- Dead multiply elimination and single-use register copy.** Walks the instruction list (`*(context+272)` head). For each opcode-137 instruction:

1. If the destination's use chain is empty (`*(def+56) == NULL`) and its source chain head is null, the instruction is dead -- delete via `sub_9253C0`.
2. If the destination's use chain is empty but its source chain head is non-null, convert in place to opcode 130 (`0x82`; internal MOV-like marker) by preserving bits 12-13 and overwriting the base opcode.
3. Otherwise, iterate source operands from index `(operand_count - 1)` down to 0. For each register operand (type tag 1) whose definition passes all guards -- no special flags (`def+48 & 0x400000022 == 0`), not predicate type (type != 9), single-use, no other def users -- allocate a fresh virtual register via `sub_91BF30` and patch the operand in place (preserve upper 8 bits of the operand DWORD, replace lower 24 bits with new register ID).

**Loop 2 -- Worklist-driven transitive propagation.** Walks the instruction list a second time. For each source operand that has a non-null use chain pointer:

1. If the definition already carries flag `0x100` (byte 1 of `def+48`), call `sub_745A80` to create a replacement register and record the mapping at `def+28`.
2. If not yet flagged, set `0x100`, then conditionally set additional flag bits based on register type: types 2-3 clear bits 3-4 (`& ~0x18`) while others set bits 3-4 plus 8 (`|= 0x118`).
3. Push the `(instruction, operand_index)` pair onto the worklist and enter the propagation loop (see below).
4. After all operands: if the instruction is still opcode 137, either delete it (when operand[1] is a register matching operand[0]'s register ID, operand[2] has no upper modifier bits, and operand[3]'s bits 25-31 are zero) or convert to opcode 130 (when the rewrite count != 1 and the destination has no remaining uses).

### Worklist data structure

The propagation loop uses a pool-allocated explicit stack, not recursion:

| Property | Value |
|---|---|
| Entry size | 16 bytes: `{instruction_ptr: u64, operand_index: i32, pad: i32}` |
| Initial capacity | 20 entries (320 bytes allocated via `(*pool_vtable+24)(pool, 320)`) |
| Growth policy | Double on overflow: `new_cap = 2 * old_cap`; allocate new buffer, `memcpy` old, free old via `(*pool_vtable+32)` |
| Stack pointer | `v33` (signed int, -1 = empty) |
| Push | `++v33`; if `v33 >= capacity`, grow; write `{use_instr, use_operand_index}` at `base[16 * v33]` |
| Pop | Read entry at `base[16 * v33]`; `--v33`; `v41 -= 16` |

The propagation loop body (lines 244-304 of the decompiled source):

1. Pop `(instr, op_idx)` from the stack.
2. Look up the use-chain list at `*(instr+64) + 8*op_idx`. If null, continue popping.
3. Read the current operand at `instr + 84 + 8*op_idx`. Look up `def+28` (the replacement ID set by `sub_745A80`). If non-zero, patch the operand's lower 24 bits with the replacement ID.
4. Increment the "positive operand" counter `v71` when the operand's sign bit (bit 31) is clear.
5. Null out the use-chain pointer (`*use_chain_slot = 0`) to mark this edge as consumed.
6. Walk the use-chain linked list: for each user `{next_ptr, user_instr, user_op_idx}`, if `user_instr`'s use-chain at `user_op_idx` is non-null, push `(user_instr, user_op_idx)` onto the stack.
7. Goto step 1 until the stack is empty (`v33 == -1`).

This design avoids recursion depth limits (multiply chains in unrolled loops can be hundreds deep) and reuses the pool allocator's bump-pointer fast path for the initial 320-byte allocation.

### Data Flow

```text
sub_C5FB30 (execute wrapper)
  |
  +-- sub_7DDB50: check function count > 1
  |
  +-- sub_752E40 (core logic)
        |
        +-- sub_7468B0 / vtable+152: check knob 487 (optimization enabled)
        |
        +-- Loop 1: Walk instruction list (*(ctx+272))
        |     +-- For opcode 137 (IMAD-like) instructions only:
        |     |     +-- sub_9253C0: delete dead instructions
        |     |     +-- sub_91BF30: allocate replacement registers
        |     |     +-- Convert dead-dest IMAD to opcode 130 (MOV-like)
        |     |
        |     +-- Clear flag 0x100 on all basic blocks (*(ctx+104) chain)
        |     +-- Set flag 0x40 at ctx+1385
        |
        +-- sub_A13890: initialize use-def traversal context
        |     +-- Creates context object with vtable off_21DBEF8
        |     +-- Sets up iterator with vtable off_21B4FD0
        |
        +-- Loop 2: Walk instruction list again
        |     +-- For each source operand with use chain:
        |     |     +-- sub_745A80: create replacement register
        |     |     +-- Worklist stack (16B entries, pool-allocated, doubles on overflow)
        |     |     +-- Transitive propagation through use chain
        |     |
        |     +-- Convert trivial IMAD to opcode 130 (MOV-like) or delete
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
```text
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
```text
Before:  t = BFE(x, amount)      ; opcode 210
         r = IADD(t, y)          ; opcode 2

After:   r = LOP3/SHF(x, y, ...) ; opcode 102, combining shift+add
```

The emitter creates opcode 102 (a combined shift-and-add operation) with encoded shift amount (`8 * amount | 0x60000002`).

## Integer Division Lowering

Integer division and modulo by non-constant values are lowered to multi-instruction sequences during instruction selection. This is not part of phase 21 but is the most visible strength reduction in ptxas output. There is no hardware integer divider on any NVIDIA GPU architecture, so every integer division must be software-emulated.

### 32-bit Division -- `sub_1724A20`

**Size:** 28,138 bytes decompiled (957 lines), the largest function in the `0x1723000`--`0x17F8000` ISA description range.
**Called from:** `sub_1727130` (coordinator B, which allocates 59 virtual registers from `dword_23976E0`).
**Instruction count:** 55 SASS instructions emitted (verified by counting all 51 `sub_9314F0` + 4 `sub_935130` emission calls in the decompilation).

The algorithm converts the divisor to FP32, uses `MUFU.RCP` for a ~23-bit reciprocal seed, converts back to integer, then refines via iterated `IMAD` multiply-add chains. Four conditional branches (`BRA`, opcode 0x5F) guard correction paths for overflow and edge cases. The sequence operates in two passes -- the first obtains a coarse quotient estimate, the second uses wide multiplies (`IMAD.WIDE`, opcode 0x6F) to compute a precise remainder and correct the quotient.

Complete instruction mix (verified from decompilation, counting each `sub_9314F0`/`sub_935130` call):

| SASS Opcode | Internal ID | Count | Role |
|---|---|---|---|
| MOV | 0x82 | 10 | Register copies and constant loads |
| MOV (typed) | 0x0A | 6 | Typed moves with immediate masks |
| IADD | 0x02 | 8 | Integer add (corrections, exponent unbias) |
| ISETP | 0xC9 | 8 | Integer set-predicate (overflow/underflow guards) |
| IMAD | 0x6E | 5 | Integer multiply-add (quotient estimation) |
| BRA | 0x5F | 4 | Conditional branch (correction paths) |
| F2I | 0xD6 | 3 | Float-to-integer conversion |
| IMAD.WIDE | 0x6F | 3 | Wide multiply-add (64-bit intermediate) |
| FSETP | 0x97 | 3 | Float set-predicate (NaN/overflow) |
| I2F | 0xD5 | 2 | Integer-to-float conversion |
| LEA | 0x24 | 2 | Shift-left / load effective address |
| LOP | 0x93 | 1 | Logic op (carry merge) |
| **Total** | | **55** | |

Key constants allocated via `sub_91D160`: 23 (mantissa shift), 255 (exponent mask), 127 (IEEE 754 bias), 254 (double-bias overflow guard), 0, 1, -1 (correction), 32 (word-width shift), 0x80000000 (sign bit), 0x7FFFFF (mantissa mask), 0x800000 (implicit-one bit). The temporary register pool spans indices 90--150 of the parameter array (`a7[]`), providing 61 dedicated scratch registers. Branch targets reference label arrays at `a10[21]`--`a10[28]`.

### 64-bit Division

Two variants handle 64-bit operands, both dispatched from `sub_1729B50`:
- **`sub_1728930`** (16,545 bytes, 634 lines): unsigned 64-bit. Emits 41 SASS instructions including `MUFU.RCP` seed, `PRMT` byte-permute for half-extraction, and `IMAD.WIDE` for double-width products.
- **`sub_1727AC0`** (13,776 bytes, 538 lines): signed 64-bit. Emits 36 instructions: sign extraction via XOR, inlined unsigned core, conditional negate at output.

See [Templates -- Integer Division](../codegen/templates.md#integer-division-lowering) for the full per-phase pseudocode of all three variants.

### Division by Constant (Granlund-Montgomery)

Division by compile-time constant is handled during the `GeneralOptimize` bundle passes (not by the Newton-Raphson templates). The Granlund-Montgomery algorithm computes a "magic number" `M` and shift amount `s` at compile time such that for all unsigned `x`: `x / C == MULHI(x, M) >> s`. The magic number satisfies `M = ceil(2^(32+s) / C)`, chosen so the high 32 bits of `x * M` yield the exact quotient after the right-shift. For signed division, an additional add-and-shift fixup handles the off-by-one from truncation toward zero. The output is 2--3 SASS instructions (`IMAD.HI` + `SHR`, sometimes with an intermediate `IADD`) instead of 55.

### FP Division Strength Reduction

Floating-point division undergoes a separate strength reduction during instruction selection (not in phase 21). With `-use_fast_math` or the `.approx` PTX modifier, `div.approx.f32 a, b` is lowered to `MUFU.RCP(b)` followed by `FMUL(a, rcp)` -- 2 instructions with ~23-bit precision. Without fast-math, `div.rn.f32` calls an IEEE-compliant libdevice routine (~15 instructions), and `div.rn.f64` expands to the full DDIV Newton-Raphson template (~100+ instructions). See [Math Intrinsics](../intrinsics/math.md#fast-math-vs-ieee-compliant-summary) for the complete precision table.

## SASS Cost Model

The profitability of strength reduction on NVIDIA GPUs differs from CPUs in several important ways:

**Integer multiply is cheap.** Modern NVIDIA GPUs (sm_70+) have dedicated integer multiply-add (IMAD) functional units. IMAD has the same throughput as IADD on most architectures -- both are single-cycle operations on the integer ALU. This means the classical "replace multiply with shift+add" transformation is often **not profitable** on GPU. ptxas does not aggressively replace multiplies with shift chains the way CPU compilers do.

**Integer division is expensive.** There is no hardware integer divider. Division must be lowered to the 55-instruction Newton-Raphson sequence described above. This is why division-by-constant is a high-priority optimization -- replacing 55 instructions with 2-3 is a massive win.

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
