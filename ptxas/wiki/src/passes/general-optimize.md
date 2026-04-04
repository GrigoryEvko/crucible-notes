# GeneralOptimize Bundles

The `GeneralOptimize*` passes are compound optimization bundles that run multiple sub-transformations in sequence on each basic block, repeating until no further changes occur (fixed-point iteration). They serve as the primary IR cleanup mechanism throughout the pipeline: after any major transformation introduces new dead code, redundant copies, or foldable constants, a GeneralOptimize pass re-normalizes the IR before the next major phase.

Six instances exist at strategic positions in the 159-phase pipeline, each using a distinct `execute()` implementation that shares a common architectural pattern but invokes different sub-pass combinations and has different gate conditions.

| | |
|---|---|
| **Instances** | 6 (phases 13, 29, 37, 46, 58, 65) |
| **Pattern** | Per-block iteration with convergence check |
| **Sub-passes** | Copy propagation, constant folding, algebraic simplification, dead code elimination |
| **Convergence** | Boolean change flag per iteration; stops when no sub-pass reports a change |
| **Iteration cap** | Knob-controlled (option 464); breaks loop if knob returns false |
| **Single-function fast path** | Phases 13 and 65 have direct tail-call paths bypassing the multi-function dispatch |
| **Multi-function gate** | All variants check `sub_7DDB50(ctx) > 1` before entering the main loop |
| **Code range** | Execute functions at `0xC5F940`--`0xC60870`; sub-pass bodies at `0x7917F0`--`0x910840` |

## Instance Map

| Phase | Name | Vtable | `execute()` | Sub-pass Body | Gate Conditions |
|---|---|---|---|---|---|
| 13 | `GeneralOptimizeEarly` | `off_22BD7D0` | `0xC5F940` | `sub_7917F0` (multi-func) / `0x1C64BF0` (single-func) | `bit 2` of `ctx+1382` must be set |
| 29 | `GeneralOptimize` | `off_22BDA50` | `0xC5FC50` | `sub_908EB0` | Option 487 enabled; option 231 not set; option 461 pass |
| 37 | `GeneralOptimizeMid` | `off_22BDB90` | `0xC5FD70` | `sub_910840` | `sub_8F3EA0` pre-check; option 487; "ConvertMemoryToRegisterOrUniform" name-gate |
| 46 | `GeneralOptimizeMid2` | `off_22BDCF8` | `0xC60840` | indirect via `[*(ctx+1584)]->vtable[0x1C0]` | Vtable dispatch; skips if target == `sub_7D6DD0` (no-op sentinel) |
| 58 | `GeneralOptimizeLate` | `off_22BDED8` | `0xC5FF20` | `sub_8F7080` | Function count > 2; bits 4-5 of `ctx+1396` != `0x20`; option 31 checked |
| 65 | `GeneralOptimizeLate2` | `off_22BDFF0` | `0xC60550` | indirect via `[*(ctx+1584)]->vtable[392]` | Function count > 1; indirect dispatch through compilation unit vtable |

## Architecture: Three Structural Variants

Despite sharing the "GeneralOptimize" name, the six instances decompose into three distinct implementation families based on their execute path:

### Variant A: Block-Iteration with Explicit Fixed-Point Loop (Phases 13, 29)

The Early and standard GeneralOptimize passes iterate over basic blocks with an explicit convergence loop. Phase 13 (`GeneralOptimizeEarly`) is the simplest and best-documented:

```c
// sub_7917F0 -- GeneralOptimizeEarly (multi-function path), simplified
void GeneralOptimizeEarly(CompilationCtx* ctx) {
    if (!(ctx->flags_1382 & 4))  return;   // gate: must have optimization flag
    if (getOption(ctx, 214))     return;   // gate: skip if option 214 set
    if (!getOption(ctx, 487))    return;   // gate: must have option 487 (general opt enable)
    if (ctx->vtable_entry_1056)  return;   // gate: skip if already processed

    sub_785E20(ctx, 0);                    // reset per-block change tracking
    sub_781F80(ctx, 1);                    // initialize instruction flags
    sub_7E6090(ctx, 0, 0, 0, 0);          // prepare operand use/def chains
    sub_7E6AD0(ctx, 0, ...);              // build def-use/use-def links

    // Iterate over basic blocks
    for (int bb_idx = 1; bb_idx <= ctx->block_count; bb_idx++) {
        BasicBlock* bb = ctx->block_table[ctx->block_order[bb_idx]];

        // Fixed-point loop on this block
        while (true) {
            bool changed = sub_753600(&state, bb);  // run sub-passes
            if (!changed) break;

            if (!getOption(ctx, 464)) break;        // iteration cap (knob-controlled)

            sub_753B50(&state);                      // apply transformations from this iteration
        }
    }

    if (changed_any)
        sub_785E20(ctx, 0);                          // re-normalize if anything changed
}
```

The inner function `sub_753600` runs on a single basic block and returns a boolean indicating whether any transformation fired. When it returns true, `sub_753B50` applies the accumulated changes (instruction replacement, operand rewriting), and the loop re-runs `sub_753600` on the same block to check if the new IR enables further simplifications.

The convergence check for option 464 acts as an emergency brake: if the knob returns false, the loop breaks even if changes were detected. This prevents pathological cases where mutual transformations oscillate indefinitely.

### Variant B: Full-Program Sub-Pass Orchestration (Phases 37, 58)

The Mid and Late variants operate at a higher level: they construct a multi-field context structure, initialize bitvector tracking infrastructure, and call a heavyweight sub-pass orchestrator.

Phase 37 (`GeneralOptimizeMid`) at `sub_910840`:
1. Calls `sub_8F3EA0` -- a pre-condition check (returns false to skip the entire pass)
2. Checks option 487 (general optimization enable)
3. Calls `sub_799250` with the string `"ConvertMemoryToRegisterOrUniform"` -- a named phase gate that allows the pass to be selectively disabled via `--no-phase`
4. Constructs a 0x408-byte context object with vtable pointer `off_21DBEF8`
5. Calls `sub_905B50` -- a 500+ line setup function that creates bitvector arrays for tracking register definitions, use-def chains, and per-block change flags
6. Calls `sub_90FBA0` -- the main optimization loop that iterates over all blocks, running sub-passes per instruction

Phase 58 (`GeneralOptimizeLate`) at `sub_8F7080`:
1. Checks function count > 2 (unlike other variants that check > 1)
2. Checks optimization level bits at `ctx+1396`
3. Checks option 31 (architecture-dependent)
4. Constructs a 0x168-byte context with 6 sub-pass tracking fields (8 zero-initialized `__int128` groups)
5. Calls `sub_8F6FA0` -- iterates over blocks calling `sub_8F6530` per block

The per-block function `sub_8F6530` (550 lines) initializes 7 distinct change counters at offsets `+20`, `+76`, `+132`, `+188`, `+244`, `+300`, and per-block state at multiple 56-byte intervals. Each counter corresponds to one sub-pass, and the function runs all sub-passes in sequence on the block's instruction list.

### Variant C: Indirect Vtable Dispatch (Phases 46, 65)

The Mid2 and Late2 variants use indirect vtable dispatch to call their sub-pass bodies, making the exact implementation architecture-dependent:

Phase 46 (`GeneralOptimizeMid2`) at `0xC60840`:
```asm
mov  rdi, [rsi+0x630]      ; load compilation_unit->field_1584
mov  rax, [rdi]             ; load vtable
mov  rax, [rax+0x1C0]      ; load vtable slot 56 (offset 0x1C0 = 448)
cmp  rax, 0x7D6DD0          ; compare against no-op sentinel
jne  call_it                ; if not sentinel, call it
ret                          ; otherwise, return (phase is no-op)
call_it:
jmp  rax                    ; tail-call the vtable method
```

Phase 65 (`GeneralOptimizeLate2`) at `0xC60550`:
```c
result = sub_7DDB50(ctx);           // get function count
if (result > 1)
    return (*(ctx->comp_unit->vtable + 392))(ctx->comp_unit, ctx);
```

This indirection means the actual optimization behavior for phases 46 and 65 is determined by the compilation unit's vtable, which varies by target architecture and optimization level. The no-op sentinel `sub_7D6DD0` (for phase 46) indicates that some architectures skip this pass entirely.

## Sub-Pass Decomposition

The sub-passes that run inside a GeneralOptimize iteration are not named individually in the binary -- they are inline code within the per-block processing functions. Based on the decompiled logic, the following sub-transformations are identifiable:

### Copy Propagation

**String evidence:** `"OriCopyProp"` at `0x21E6CE1` appears in the phase name table at index 22, confirming that copy propagation is a recognized sub-pass within the system.

The copy propagation logic in `sub_908EB0` (phase 29) walks the instruction linked list of each basic block. For each instruction:

1. Checks the opcode field at `instr+72` (masked with `& 0xCF00`) against known move/copy opcodes
2. For opcode 97 (register-to-register move): follows the def-use chain to find the source, then replaces the destination with the source throughout all uses
3. For opcode 18 (predicated copy): calls `sub_8F2E50` to check if the copy is eligible for propagation (checks operand types, predicate conditions, and architecture constraints via `ctx->comp_unit+372` version check against 20479)
4. For opcode 124 (conditional select / phi-like move): similar propagation with additional checks on operand flags (`& 0x100`, `& 0x70`)

The propagation sets flags on instruction operand fields:
- Bit 8 (`0x100`): marks operand as propagated
- Bit 9 (`0x200`): marks operand for deferred cleanup
- Bit 10 (`0x400`): marks as "propagated under predicate"

### Constant Folding

Constant folding is integrated into the per-block pass through checks on operand type fields. When the operand at position `instr+84 + 8 * dest_idx` has type bits indicating a constant source (type field `& 0xF` equals 1 or 2), the instruction is eligible for folding.

The fold-eligibility check in `sub_8F2E50` distinguishes:
- Type 1: integer constants -- foldable if SM version <= 20479 or no additional constraint bits are set (`& 0x1C00` == 0)
- Type 2: floating-point constants -- same SM version check

The knob `"LimitFoldFP"` (string at `0x1CE3D23`) suggests a separate control for limiting floating-point constant folding to prevent precision issues.

### Algebraic Simplification

The algebraic simplifier runs as part of the same per-block scan. Key evidence from `sub_753600`:

1. Calls `sub_7E7380(instr_a, instr_b)` -- an instruction equivalence check that compares opcode, operand count, operand types, and operand values. This is used to detect patterns like `x op x` (self-operations)
2. Checks `instr[25] & 7` for operand format codes (values 3 and 4 distinguish immediate vs register operands)
3. Compares operand definition chains through `instr+128` (def-chain pointer) to detect when two operands refer to the same value

Patterns recognized include:
- Operand identity: when source and destination of a move are the same register
- Redundant operations: when the same operation is applied twice in succession with matching operands
- Constant identity operations: detected through the operand type + value comparison at `instr[v23+21] & 0xFFFFFF`

The function `sub_747F40` and `sub_747F80` extract instruction modifiers (negation, absolute value) for detecting patterns like double-negation or `abs(abs(x))`.

### Dead Code Elimination

DCE within GeneralOptimize is lightweight compared to the standalone `OriPerformLiveDead` passes. It operates locally within basic blocks using the `sub_7DF3A0` function, which checks an instruction's use count. The check `(*result & 0xC) != 0` at the return of `sub_7DF3A0` indicates that bits 2-3 of the instruction status word encode "has live uses" -- if neither bit is set, the instruction is dead.

Dead instructions are not immediately deleted. Instead, they are marked for removal by the convergence loop cleanup phase (`sub_753B50`), which rewires the instruction list to skip dead nodes and updates the block's def-use chains.

### Predicate Simplification

A distinct sub-pass handles predicate register operations. The code in `sub_908EB0` at the `v4 == 18` and `v4 == 124` branches processes predicated moves and conditional selects:

- Opcode 18: predicated move -- if the predicate is known-true (from prior constant folding), simplifies to unconditional move
- Opcode 124: conditional select -- if both source operands are identical, simplifies to an unconditional copy; if the predicate is constant, selects the appropriate source

The helper `sub_8F29C0` performs predicate-specific analysis, and `sub_908A60` handles the two-pass predicate simplification (called with argument `1` for the first direction, then `0` for the reverse direction).

## Fixed-Point Convergence

### Per-Block Iteration Model

All GeneralOptimize variants use a per-block convergence model: they iterate over basic blocks in linear order (following the block ordering table at `ctx+512`), and for each block, run the sub-passes repeatedly until convergence. This differs from the global worklist model used by other optimizers (GVN-CSE at phase 49 uses a global worklist).

```
for each block B in reverse postorder:
    repeat:
        changed = run_sub_passes(B)
    until !changed OR !getOption(464)
```

### Change Detection Mechanism

Changes are detected through a return-value protocol:
- `sub_753600` (Variant A): returns a boolean indicating whether any copy was propagated or any simplification fired
- `sub_8F6530` (Variant B): sets per-sub-pass counters at 56-byte intervals in the context structure; the caller checks if any counter is non-zero
- Variant C: the indirectly-called function returns a boolean

### Iteration Limits

The fixed-point loop is guarded by option 464, which acts as an iteration limit. In `sub_7917F0`, the check is:

```c
if (!getOption(ctx->config, 464)) break;
```

This is called after each successful iteration (when `changed == true`). If the option returns false, the loop terminates even though more changes could be made. The exact semantics of option 464 depend on the knob's implementation -- it could be a simple counter that decrements, a boolean that gets cleared after N iterations, or a cost-based threshold. The default behavior (when option 464 always returns true) allows unbounded iteration until convergence.

In practice, most basic blocks converge in 1-3 iterations. A block that generates new optimization opportunities typically does so because copy propagation exposes a constant, which enables constant folding, which creates a dead instruction. The second iteration catches any cascading effects, and the third confirms convergence. Blocks requiring more than 3 iterations are rare and typically involve chains of dependent copies or nested predicate simplifications.

## Differences Between Early/Mid/Late Variants

The six instances are not identical copies with different names. They differ in three dimensions:

### 1. Gate Conditions (Who Runs)

| Phase | Gate Logic |
|---|---|
| 13 (Early) | Requires `ctx->flags_1382 & 4`; skips if option 214 is set; requires option 487 |
| 29 | Requires option 487; skips if option 231 (dump mode) is set; requires `ctx+33192` check |
| 37 (Mid) | Requires `sub_8F3EA0` pre-check; option 487; can be disabled via `--no-phase ConvertMemoryToRegisterOrUniform` |
| 46 (Mid2) | Indirect dispatch; skips if vtable slot points to no-op sentinel `sub_7D6DD0` |
| 58 (Late) | Requires function count > 2; checks optimization level bits `(ctx+1396 & 0x30) != 0x20`; checks option 31 |
| 65 (Late2) | Requires function count > 1; indirect dispatch through compilation unit vtable |

### 2. Sub-Pass Selection (What Runs)

| Phase | Sub-Passes Included |
|---|---|
| 13 (Early) | Copy prop, algebraic simplify, predicate simplify, limited DCE. No use-def chain rebuild. Lightweight -- designed for quick cleanup after initial lowering. |
| 29 | Copy prop with predicate handling, constant folding, algebraic simplify, DCE with instruction flag marking (`0x400` bit). Full operand analysis via `sub_7DF3A0`. |
| 37 (Mid) | Full sub-pass suite plus `ConvertMemoryToRegisterOrUniform` (memory-to-register promotion). Bitvector-based change tracking (`sub_905B50`). Most comprehensive instance. |
| 46 (Mid2) | Architecture-dependent (vtable dispatch). May include additional target-specific simplifications. |
| 58 (Late) | Copy prop, constant folding, algebraic simplify, DCE. Also includes option-31-gated patterns (architecture-specific fold eligibility). 7-counter change tracking structure. |
| 65 (Late2) | Architecture-dependent (vtable dispatch). Final cleanup before register allocation. |

### 3. Infrastructure Weight (How It Runs)

| Phase | Context Size | Tracking | Complexity |
|---|---|---|---|
| 13 (Early) | Minimal (stack locals) | Boolean changed flag | Low (~78 lines in execute path) |
| 29 | Stack frame (~0x60 bytes) | Boolean + instruction flags | Medium (~218 lines in sub_908EB0) |
| 37 (Mid) | 0x408-byte heap context | Bitvector arrays via `sub_905B50` | High (~500+ lines in setup + loop) |
| 46 (Mid2) | Vtable-dependent | Vtable-dependent | Variable |
| 58 (Late) | 0x168-byte stack context | 7 per-sub-pass counters (56-byte stride) | Medium-high (~550 lines in sub_8F6530) |
| 65 (Late2) | Vtable-dependent | Vtable-dependent | Variable |

## Pipeline Positioning

The six instances are positioned to clean up after specific groups of transformations:

```
Phase 0-12:  Initial setup, FP16 promotion, unsupported op conversion
  --> Phase 13: GeneralOptimizeEarly  (clean up after lowering artifacts)

Phase 14-28: Branch opt, loop passes, strength reduction, pipelining
  --> Phase 29: GeneralOptimize       (clean up after loop transformations)

Phase 30-36: Switch opt, linear replacement, LICM
  --> Phase 37: GeneralOptimizeMid    (heavy cleanup + mem-to-reg promotion)

Phase 38-45: Nested branch opt, CTA expansion, mbarrier, mid expansion
  --> Phase 46: GeneralOptimizeMid2   (clean up after mid-level expansion)

Phase 47-57: GVN-CSE, reassociation, remat, late expansion, speculative hoist
  --> Phase 58: GeneralOptimizeLate   (clean up after late expansion)

Phase 59-64: Loop fusion, predication, late commoning
  --> Phase 65: GeneralOptimizeLate2  (final cleanup before register work)
```

After phase 65, the pipeline transitions to register-attribute setting (phase 90), synchronization (phase 99), and register allocation (phase 101). No GeneralOptimize instance runs after register allocation -- the post-RA pipeline uses different peephole mechanisms.

## Function Map

| Address | Name | Role |
|---|---|---|
| `0xC5F940` | Phase 13 execute | Tail-calls `0x1C64BF0` (single-func) or `sub_7917F0` (multi-func) |
| `0xC5FC50` | Phase 29 execute | Checks count > 1, calls `sub_908EB0` |
| `0xC5FD70` | Phase 37 execute | Checks count > 1, calls `sub_910840` |
| `0xC60840` | Phase 46 execute | Indirect vtable dispatch through `comp_unit->vtable[0x1C0]` |
| `0xC5FF20` | Phase 58 execute | Checks count > 1, calls `sub_8F7080` |
| `0xC60550` | Phase 65 execute | Checks count > 1, indirect dispatch through `comp_unit->vtable[392]` |
| `0x7917F0` | `GeneralOptimizeEarly` body | Multi-function path: iterates blocks, fixed-point loop with `sub_753600` |
| `0x908EB0` | `GeneralOptimize` body | Per-block copy prop + predicate simplification with flag marking |
| `0x910840` | `GeneralOptimizeMid` body | Full suite with mem-to-reg; delegates to `sub_905B50` + `sub_90FBA0` |
| `0x8F7080` | `GeneralOptimizeLate` body | Bitvector-tracked multi-counter pass; calls `sub_8F6FA0` |
| `0x753600` | Per-block sub-pass runner (Early) | Copy prop + algebraic simplify; returns boolean changed |
| `0x753B50` | Per-block apply changes (Early) | Instruction rewriting, def-use chain update |
| `0x8F6530` | Per-block sub-pass runner (Late) | 7-counter change tracking; 550-line function |
| `0x8F6FA0` | Block iterator (Late) | Walks block list calling `sub_8F6530` per block |
| `0x905B50` | Setup/init (Mid) | ~500 lines; creates bitvector infrastructure |
| `0x90FBA0` | Main loop (Mid) | Instruction-level iteration with constant folding and simplification |
| `0x8F2E50` | Copy eligibility check | Checks opcode (18, 124), operand types, SM version constraints |
| `0x8F29C0` | Predicate analysis helper | Determines if predicate allows propagation |
| `0x908A60` | Two-pass predicate simplify | Called with direction flag (1 = forward, 0 = backward) |
| `0x785E20` | Change tracking reset | Resets per-block change flags |
| `0x781F80` | Instruction flag init | Initializes per-instruction optimization flags (1800 lines) |
| `0x7E6090` | Use-def chain builder | Builds operand use-def chains for copy propagation |
| `0x7E6AD0` | Def-use link builder | Builds def-use/use-def bidirectional links |
| `0x7DF3A0` | Liveness check | Returns instruction status; bits 2-3 indicate live uses |
| `0x7E7380` | Instruction equivalence | Compares two instructions for structural equivalence |

## Cross-References

- [Pass Inventory](index.md) -- full 159-phase table with GeneralOptimize instances highlighted
- [Phase Manager](phase-manager.md) -- dispatch loop, vtable protocol, factory switch
- [Optimization Pipeline](../pipeline/optimizer.md) -- overall pipeline stages
- [Liveness Analysis](liveness.md) -- standalone `OriPerformLiveDead` passes (heavier DCE)
- [Knobs System](../config/knobs.md) -- option 464 (iteration cap), option 487 (general opt enable)
