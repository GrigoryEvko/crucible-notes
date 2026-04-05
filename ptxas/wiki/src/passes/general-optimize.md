# GeneralOptimize Bundles

The `GeneralOptimize*` passes are compound optimization bundles that run multiple sub-transformations in sequence on each basic block, repeating until no further changes occur (fixed-point iteration). They serve as the primary IR cleanup mechanism throughout the pipeline: after any major transformation introduces new dead code, redundant copies, or foldable constants, a GeneralOptimize pass re-normalizes the IR before the next major phase.

Six instances exist at strategic positions in the 159-phase pipeline. Despite sharing the "GeneralOptimize" name prefix, the six instances decompose into three distinct implementation families -- a lightweight block-iteration variant, a heavyweight bitvector-tracked orchestrator, and an indirect vtable dispatch stub. Each family shares a common architectural pattern (per-block iteration with convergence check) but invokes different sub-pass combinations and has different gate conditions.

| | |
|---|---|
| **Instances** | 6 (phases 13, 29, 37, 46, 58, 65) |
| **Pattern** | Per-block iteration with convergence check |
| **Sub-passes** | Copy propagation, constant folding, algebraic simplification, dead code elimination, predicate simplification |
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

### Variant A: Block-Iteration with Explicit Fixed-Point Loop (Phases 13, 29)

The Early and standard GeneralOptimize passes iterate over basic blocks with an explicit convergence loop. Phase 13 (`GeneralOptimizeEarly`) at `sub_7917F0` is the simplest and best-documented:

```c
// sub_7917F0 -- GeneralOptimizeEarly (multi-function path)
void GeneralOptimizeEarly(int64_t ctx) {
    if (!(*(uint8_t*)(ctx + 1382) & 4))   return;   // gate: optimization flag

    // Option 214 check -- uses vtable fast-path comparison:
    //   if vtable[72] == sub_6614A0, reads *(config + 15408) directly
    //   otherwise calls the virtual getOption(214)
    if (getOption(ctx, 214))               return;   // gate: skip if set

    // Option 487 check -- uses vtable[152] fast-path:
    //   if vtable[152] == sub_67EB60, calls sub_7468B0(config, 487)
    //   otherwise calls the virtual isOptionSet(487, 1)
    if (!getOption_v2(ctx, 487))           return;   // gate: general opt enable

    if (*(int64_t*)(*(int64_t*)ctx + 1056)) return;  // gate: already processed

    sub_785E20(ctx, 0);                    // reset per-block change tracking
    sub_781F80(ctx, 1);                    // initialize instruction flags
    sub_7E6090(ctx, 0, 0, 0, 0);          // prepare operand use/def chains
    sub_7E6AD0(ctx, 0, ...);              // build def-use/use-def links

    // Iterate over basic blocks (block_count at ctx+520)
    int bb_count = *(int32_t*)(ctx + 520);
    for (int i = 1; i <= bb_count; i++) {
        // block_order at ctx+512, block_table at ctx+296
        int bb_idx = *(int32_t*)(*(int64_t*)(ctx + 512) + 4*i);
        BasicBlock* bb = *(BasicBlock**)(*(int64_t*)(ctx + 296) + 8*bb_idx);

        // Fixed-point loop on this block
        int64_t state[...];   // stack-allocated state at rbp-0x88
        while (true) {
            bool changed = sub_753600(&state, bb);   // run sub-passes
            if (!changed)  break;

            // Iteration cap: knob 464
            if (!getOption_v2(ctx, 464))  break;

            sub_753B50(&state);            // apply instruction rewrites
        }
    }

    if (any_changed)
        sub_785E20(ctx, 0);                // re-normalize if anything changed
}
```

The inner function `sub_753600` runs on a single basic block and returns a boolean indicating whether any transformation fired. When it returns `true`, `sub_753B50` applies the accumulated changes (instruction replacement, operand rewriting, def-use chain updates), and the loop re-runs `sub_753600` on the same block to check if the new IR enables further simplifications.

The convergence check for option 464 acts as an emergency brake: if the knob returns false, the loop breaks even if changes were detected. This prevents pathological cases where mutual transformations oscillate indefinitely.

**Phase 29** (`sub_C5FC50`) follows the same pattern but delegates to `sub_908EB0`, which implements a more complex instruction walk with additional opcode dispatch (opcodes 97, 18, 124) and predicate-aware propagation.

### Variant B: Full-Program Sub-Pass Orchestration (Phases 37, 58)

The Mid and Late variants operate at a higher level: they construct a multi-field context structure, initialize bitvector tracking infrastructure, and call a heavyweight sub-pass orchestrator.

#### Phase 37 -- GeneralOptimizeMid (`sub_910840`)

1. Calls `sub_8F3EA0` -- a pre-condition check (returns false to skip the entire pass)
2. Checks option 487 (general optimization enable) via the same vtable fast-path pattern
3. Calls `sub_799250` with the string `"ConvertMemoryToRegisterOrUniform"` (at `0x21DD228`) -- a named phase gate that allows the pass to be selectively disabled via `--no-phase`
4. Constructs a **0x408-byte context object** on the stack with vtable pointer `off_21DBEF8` at offset 0. The layout is:
   ```
   GeneralOptimizeMid Context (0x408 bytes)
     +0x000  vtable_ptr     = off_21DBEF8
     +0x008  allocator      = *(ctx + 16)
     +0x010  (zero-init)    ...
     +0x018  (zero-init)    ...
     +0x020  (zero-init)    ...
     +0x030  int count      = 0
     +0x040  sub_context    -- initialized by sub_905B50 (bitvectors, register tracking)
     ...
   ```
5. Calls `sub_905B50` -- a 500+ line setup function that creates bitvector arrays for tracking register definitions, use-def chains, and per-block change flags. Allocates three pairs of {bitvector, metadata, capacity} structures for tracking definition reach, register liveness, and fold eligibility
6. Calls `sub_90FBA0` -- the main optimization loop that iterates over all blocks, running sub-passes per instruction

After `sub_90FBA0` returns, the function destroys three RAII-style bitvector containers at offsets `+0x200`, `+0x228`, and `+0x1E0` by invoking their vtable destructors via `*(vtable + 32)`.

#### Phase 58 -- GeneralOptimizeLate (`sub_8F7080`)

1. Checks function count > 2 via `sub_7DDB50` (stricter than other variants that check > 1)
2. Checks optimization level bits at `ctx+1396`: the condition `(flags & 0x30) != 0x20` ensures the pass is skipped at certain reduced optimization levels
3. Checks option 31 via the vtable fast-path; when option 31 reports as "extended" (value at `config+2232` is 1 with non-zero extra word at `config+2240`), an additional `sub_7DC0E0` check determines a secondary control flag `v7`
4. Constructs a **0x168-byte context** on the stack with 7 sub-pass tracking groups. Each group occupies 56 bytes (three `__int128` values + a boolean changed-flag + a counter):
   ```
   GeneralOptimizeLate Context (0x168 bytes)
     +0x000  ctx_ptr     = ctx (the compilation context)
     +0x008  flag_a      -- initialized from (ctx+1396 & 4)
     +0x009  flag_b      -- initialized from (ctx+1396 & 8)
     +0x00C  counter_0   = 0   |
     +0x010  changed_0   = 0   | Sub-pass group 0 (56 bytes)
     +0x018  ...              |
     +0x048  counter_1   = 0   | Sub-pass group 1
     ...
     +0x12C  counter_6   = 0   | Sub-pass group 6
     +0x130  changed_6   = 0   |
     +0x138  ...              |
   ```
5. Calls `sub_8F6FA0` -- the block iterator

The block iterator `sub_8F6FA0` initializes per-context flags from `ctx+1396`:
- Bit 2 (`& 4`): stored at `context+9`, controls whether opcode-7 instructions are processed
- Bit 3 (`& 8`): stored at `context+8`, controls whether opcode-6 (MOV variant) instructions are processed

It then calls `sub_7E6090` to rebuild use-def chains and walks the block list calling `sub_8F6530` per block.

### Variant C: Indirect Vtable Dispatch (Phases 46, 65)

The Mid2 and Late2 variants use indirect vtable dispatch to call their sub-pass bodies, making the exact implementation architecture-dependent:

**Phase 46** (`GeneralOptimizeMid2`) at `0xC60840`:
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

**Phase 65** (`GeneralOptimizeLate2`) at `sub_C60550`:
```c
// sub_C60550 -- GeneralOptimizeLate2 execute
int64_t GeneralOptimizeLate2(int64_t phase, int64_t ctx) {
    int64_t result = sub_7DDB50(ctx);       // get function count
    if ((int)result > 1) {
        int64_t comp_unit = *(int64_t*)(ctx + 1584);
        return (*(int64_t(**)(int64_t, int64_t))(*(int64_t*)comp_unit + 392))(comp_unit, ctx);
    }
    return result;
}
```

This indirection means the actual optimization behavior for phases 46 and 65 is determined by the compilation unit's vtable, which varies by target architecture and optimization level. The no-op sentinel `sub_7D6DD0` (for phase 46) indicates that some architectures skip this pass entirely.

## Sub-Pass Decomposition

The sub-passes that run inside a GeneralOptimize iteration are not named individually in the binary -- they are inline code within the per-block processing functions. Based on the decompiled logic, the following sub-transformations are identifiable:

### Copy Propagation

**String evidence:** `"OriCopyProp"` at `0x21E6CE1` appears in the phase name table at index 22, confirming that copy propagation is a recognized sub-pass within the system.

The copy propagation logic in `sub_908EB0` (phase 29) walks the instruction linked list of each basic block. For each instruction:

1. Reads the opcode field at `instr+72` (masked with `& 0xCF00`) against known move/copy opcodes
2. **Opcode 97** (register-to-register move): follows the def-use chain via the lookup at `*(ctx+296) + 8 * *(int32_t*)(instr+84) & 0xFFFFFF`. When a single-use move chain is detected, the destination is replaced with the source throughout all uses. The change flag `v10` is set to track propagation, and subsequent instructions check liveness via `sub_7DF3A0`
3. **Opcode 18** (predicated copy): calls `sub_8F2E50` to check if the copy is eligible for propagation. When eligible and `v21` is true (an additional gate from the vtable dispatch at `*(comp_unit+1312)`), the operand at position `instr + 8*(operand_count + ~((opcode>>11)&2)) + 84` is marked with `0x400` (propagated under predicate)
4. **Opcode 124** (conditional select / phi-like move): also dispatches through `sub_8F2E50`. When the operand type field (`& 0xF`) is 1 (integer constant), calls `sub_8F29C0` for predicate analysis. When the type is not 1 but `sub_8F29C0` succeeds and certain flag bits (`& 0x1B`) are clear, falls through to direct propagation. Otherwise invokes the two-pass predicate simplifier `sub_908A60` with direction flags 1 (forward) and 0 (backward)

The propagation sets flags on instruction operand fields:
- Bit 8 (`0x100`): marks operand as propagated
- Bit 9 (`0x200`): marks operand for deferred cleanup. The combined mask `0xFFFFFDF0 | 0x201` clears the old type bits and sets both "propagated" and "immediate-eligible"
- Bit 10 (`0x400`): marks as "propagated under predicate"

### Constant Folding

Constant folding is integrated into the per-block pass through checks on operand type fields. The fold-eligibility check in `sub_8F2E50` at `0x8F2E50` is the central gatekeeper:

```c
// sub_8F2E50 -- Copy/fold eligibility check (simplified)
bool is_eligible(int64_t ctx, uint32_t* instr) {
    uint32_t opcode = instr[18];
    opcode = (opcode & ~0x3000) | (opcode & 0xCFFF);  // mask opcode

    if (opcode == 18) {   // predicated move
        int dest_operand = instr[2 * (instr[20] + ~((instr[18]>>11)&2)) + 21];
        int type_nibble = (dest_operand >> 2) & 0xF;
        if (type_nibble == 10) return false;           // type 10: not foldable
        if (!(type_nibble | (dest_operand & 0x400)))   // no type bits set
            // SM version check via comp_unit vtable[1320]:
            //   if sentinel sub_7D7240, check source operand types (bits 28-30)
            //   otherwise call the virtual method
            return check_source_operands(ctx, instr);
        return false;
    }

    if (opcode == 124) {  // conditional select
        int dest = instr[2 * (instr[20] + ~((instr[18]>>11)&2)) + 21];
        if (dest & 0x100) return false;    // already propagated
        if (dest & 0x70)  return false;    // has modifier bits

        int type = dest & 0xF;
        int sm_version = *(int32_t*)(*(int64_t*)(ctx + 1584) + 372);
        // Type 1 (integer) or Type 2 (float): foldable if SM <= 20479 (pre-Turing)
        // or no additional constraint bits (& 0x1C00 == 0)
        if ((type == 2 || type == 1) && (sm_version <= 20479 || (dest & 0x1C00) == 0))
            return true;
    }
    return false;
}
```

**SM version 20479**: This threshold (0x4FFF) divides architectures where constant folding is unconditionally safe from those requiring additional constraint checks. SM versions above 20479 correspond to architectures with extended immediate encoding constraints.

The knob `"limit-fold-fp"` (string at `0x1CE3D23`, helptext `"Enable/disable constant folding of float operations"` at `0x1CE63B0`) provides user control over floating-point constant folding to prevent precision issues in numerical code.

### Algebraic Simplification

The algebraic simplifier runs as part of the same per-block scan in `sub_753600`. The function is approximately 250 lines of decompiled code and operates on instruction pairs. Key mechanisms:

1. **Instruction equivalence check** via `sub_7E7380(instr_a, instr_b)`: compares opcode at `instr+72`, operand count, operand types, and operand values. Returns a boolean. Used to detect patterns like `x op x` (self-operations) and redundant re-computation

2. **Operand format classification** at `instr[25] & 7`: values 3 and 4 distinguish immediate vs register operands. When both instructions have identical format code, the simplifier can reason about algebraic identities

3. **Def-chain following**: compares operand definition chains through `instr+128` (def-chain pointer) and register indices at `instr+24` to detect when two operands in different instructions refer to the same SSA value

4. **Modifier extraction** via `sub_747F40` (negation flag) and `sub_747F80` (absolute-value flag): these helper functions extract per-operand modifiers from the operand encoding. The simplifier detects and eliminates double-negation (`neg(neg(x))`) and redundant absolute values (`abs(abs(x))`)

5. **Constant identity matching**: when `instr[v23+21] & 0xFFFFFF` matches between two operands after accounting for operand index offsets, the instructions compute the same value. The 24-bit mask extracts the register index, and equality means the same virtual register

6. **Deep equivalence for non-trivial patterns**: for opcodes where the source operands are register-type (bits 28-30 == 1), the simplifier follows the definition chain to the defining instruction. If the defining instruction has opcode 119 (a specific ALU operation), it compares the result operands at `instr[2*instr[20]+19]` and checks additional constraint bits (bit 0 and bit 3 of the result encoding). It then calls `sub_748570` to verify no aliasing hazards, and `sub_1245740(ctx, instr_a, instr_b, 2)` for structural equivalence of the entire sub-DAG

### Dead Code Elimination

DCE within GeneralOptimize is lightweight compared to the standalone `OriPerformLiveDead` passes (phases 16, 33, 61, 84). It operates locally within basic blocks using the `sub_7DF3A0` function:

```c
// sub_7DF3A0 -- instruction liveness check
//   Returns pointer to status word
//   Bits 2-3 (mask 0xC): has live uses
//   Bit 0 (mask 0x1): marked dead
int8_t* check_liveness(int64_t instr, int64_t* ctx) {
    // ... examines use-def chains ...
    return status_ptr;   // caller checks (*result & 0xC) != 0
}
```

In `sub_908EB0`, the DCE check appears as the fallback for unrecognized opcodes:

```c
if (!v10) {   // v10 = "previous instruction was a recognized copy"
    int8_t* status = sub_7DF3A0(instr, ctx);
    v10 = (*status & 0xC) != 0;   // live uses exist?
}
```

When `(*status & 0xC) == 0`, the instruction has no live consumers and is effectively dead. In Variant A, dead instructions are not immediately deleted -- they are marked for removal by the convergence loop cleanup phase (`sub_753B50`), which rewires the instruction list to skip dead nodes and updates the block's def-use chains via `sub_931920`, `sub_932E80`, `sub_749090`, and `sub_9253C0`.

In Variant B (phase 58), `sub_8F6530` uses the same `sub_7DF3A0` liveness check but integrates the result into its 7-counter change tracking structure, incrementing the appropriate sub-pass counter when a dead instruction is found.

### Predicate Simplification

A distinct sub-pass handles predicate register operations. The code in `sub_908EB0` at the opcode-18 and opcode-124 branches processes predicated moves and conditional selects:

- **Opcode 18** (predicated move): if the predicate is known-true (from prior constant folding), simplifies to unconditional move. If the `v21` flag is set (indicating the vtable dispatch at `comp_unit+1312` returned non-zero, i.e. the target supports this transformation), marks the destination operand with `0x400`
- **Opcode 124** (conditional select): if both source operands are identical (detected via def-chain comparison), simplifies to an unconditional copy; if the predicate is constant, selects the appropriate source. The two-pass approach via `sub_908A60` handles phi-like patterns where direction matters:
  - Pass 1: `sub_908A60(ctx, reg_entry, instr, 1, &out_a, &out_b)` -- forward direction
  - Pass 2 (if pass 1 found no simplification but detected a partial match): `sub_908A60(ctx, reg_entry, instr, 0, &out_a, &out_b)` -- backward direction

The helper `sub_8F29C0` at `0x8F29C0` performs predicate-specific analysis, determining whether the predicate condition allows safe propagation given the current instruction context.

## The Per-Block Sub-Pass Runner: `sub_8F6530` (Variant B Detail)

The 550-line function `sub_8F6530` is the core of Variant B (phase 58). It processes a single basic block using a **6-slot circular buffer** of instruction pairs, tracked at 56-byte intervals:

```
sub_8F6530 Context (passed as a1)
  +0x000  ctx_ptr                 -- compilation context
  +0x008  flag_ctrl_flow_4        -- from ctx+1396 bit 2 (opcode-7 enable)
  +0x009  flag_ctrl_flow_8        -- from ctx+1396 bit 3 (opcode-6 enable)
  +0x00C  slot_index              -- current slot (modulo 6)
  +0x010  slot_0_changed          -- boolean: did this slot's pair fire?
  +0x014  slot_0_count            -- how many pairs stored in this slot

  Slot layout (each 56 bytes = 7 int64_t):
    +0x00  count/used flag
    +0x04  changed flag
    +0x08  instr_ptr_a            -- first instruction of the pair
    +0x10  instr_ptr_b            -- second instruction of the pair
    +0x18  (reserved)
    ...

  6 slots at offsets: +0x10, +0x48, +0x80, +0xB8, +0xF0, +0x128
```

The slot index increments with `(*(a1+3) + 1) % 6` after each pair is processed. When a new instruction pair is encountered that doesn't match any existing slot, the oldest slot is evicted (slot index advances). Each slot can hold up to 2 instruction pointers.

The function walks the instruction list looking for specific opcode patterns:

1. **Opcodes 139 and 110** (MOV variants with different addressing modes): these are the primary targets. The function checks operand field at `instr+76` for value 6 (register operand) or 7 (immediate operand), with the `flag_ctrl_flow_4` and `flag_ctrl_flow_8` gates controlling which variants are processed
2. For register operands (type field bits 28-30 == 1), it verifies:
   - Use count == 1 (`*(reginfo+24) == 1`)
   - No aliasing flags (`*(reginfo+50) & 1 == 0`)
   - Register class not in range 2-8 (`*(reginfo+20) - 2 > 6`)
3. For instructions with opcode 139 and no modifier bits (`*(instr+88) & 0x603FFFF == 0`), the function attempts to find the instruction in the circular buffer and either promote it (if found) or insert it as a new entry
4. **Option 605** (`getOption(ctx, 605)`) at `0x8F6530+0x1A0`: when enabled, restricts the matching to only instructions already present in the buffer, preventing new insertions. This is an architecture-gated optimization

## Fixed-Point Convergence

### Per-Block Iteration Model

All GeneralOptimize variants use a per-block convergence model: they iterate over basic blocks in linear order (following the block ordering table at `ctx+512`), and for each block, run the sub-passes repeatedly until convergence. This differs from the global worklist model used by other optimizers (GVN-CSE at phase 49 uses a global worklist).

```
for each block B in reverse postorder:
    repeat:
        changed = run_sub_passes(B)
    until !changed OR !getOption(464)
```

The block ordering table is an array of `int32_t` indices at `*(ctx+512)`, with the count at `*(ctx+520)`. Block iteration starts at index 1 (not 0) and proceeds through `bb_count` inclusive. Each index is used to look up the actual basic block pointer via `*(*(ctx+296) + 8 * block_order[i])`.

### Change Detection Mechanism

Changes are detected through different protocols depending on the variant:

- **Variant A** (`sub_753600`): returns a boolean. The return value is the logical OR of all sub-pass fire events. The state machine in `sub_7917F0` stores the result in `v15` (mapped to register `bp`) and accumulates across iterations via `v4 = v15`
- **Variant B, phase 58** (`sub_8F6530`): maintains 7 independent counters at 56-byte intervals in the context structure. Counters are at `*(a1 + 5)`, `*(a1 + 19)`, `*(a1 + 33)`, `*(a1 + 47)`, `*(a1 + 61)`, `*(a1 + 75)`. The corresponding boolean changed-flags are at `*(a1 + 16)`, `*(a1 + 72)`, `*(a1 + 128)`, `*(a1 + 184)`, `*(a1 + 240)`, `*(a1 + 296)`. All are zero-initialized at entry. The caller checks if any counter is non-zero to determine convergence
- **Variant B, phase 37** (`sub_90FBA0`): uses a different approach -- tracks a floating-point "cost" accumulator at `context+25/26/27` (three `double` values representing total cost, weighted cost, and instruction count). Convergence is determined when the cost delta falls below a threshold (initialized to 0.25, adjustable via knob 474 at `0x90FBA0+0x50`). Knob 135 at `0x90FBA0+0x20` controls an initial threshold override when enabled (checked via `*(config+9720)`)

### Iteration Limits

The fixed-point loop is guarded by option 464 in Variant A. In `sub_7917F0`:

```c
while (true) {
    bool changed = sub_753600(&state, bb);
    if (!changed) break;

    // Option 464 check -- same vtable fast-path pattern:
    //   vtable[152] == sub_67EB60  =>  sub_7468B0(config, 464)
    //   otherwise                  =>  vtable[152](config, 464, 1)
    if (!getOption_v2(ctx, 464)) break;

    sub_753B50(&state);   // apply rewrites before re-scanning
}
```

The option 464 check is called after each successful iteration (when `changed == true`). If the option returns false, the loop terminates even though more changes could be made. The exact semantics of option 464 depend on the knob's implementation -- it could be a simple counter that decrements, a boolean that gets cleared after N iterations, or a cost-based threshold. The default behavior (when option 464 always returns true) allows unbounded iteration until convergence.

Variant B (phases 37 and 58) does **not** use option 464 for iteration control. Phase 37 uses the cost-based threshold described above. Phase 58 makes a single pass over the block list via `sub_8F6FA0`, which does not loop -- each block is visited exactly once, with the 6-slot circular buffer providing limited lookback within the walk.

In practice, most basic blocks converge in 1--3 iterations. A block that generates new optimization opportunities typically does so because copy propagation exposes a constant, which enables constant folding, which creates a dead instruction. The second iteration catches any cascading effects, and the third confirms convergence. Blocks requiring more than 3 iterations are rare and typically involve chains of dependent copies or nested predicate simplifications.

## The Apply-Changes Function: `sub_753B50`

After `sub_753600` reports changes, `sub_753B50` applies the accumulated transformations. This is a compact 70-line function that performs instruction-list surgery:

1. **Creates a replacement instruction** via `sub_931920(ctx, state->instr_pair, *(*(state->instr_pair+8)+8), -1)` -- the `-1` argument (`0xFFFFFFFF`) signals "allocate new"
2. **Updates the block's instruction head** at `*(ctx+232)` with the new instruction's head pointer
3. **Clears the block's instruction count** at `*(ctx+264) = 0`
4. **Calls `sub_932E80`** to relink the instruction into the block's doubly-linked list
5. **Propagates flags**: if the original instruction had flag bit 3 of `*(instr+280)` set (indicating a control-flow-sensitive instruction), the replacement inherits it via `new_instr[70] |= 8`
6. **Walks the state's instruction chain** (from `state[1]` through `state[2]`), creating replacements for each and calling `sub_749090` to update register-to-instruction mappings
7. **Final cleanup**: calls `sub_9253C0` to remove the dead instructions from their blocks, and `sub_749290` to update the register numbering, and `sub_91E310` to splice the old instruction range out of the linked list

## Differences Between Early/Mid/Late Variants

### 1. Gate Conditions (Who Runs)

| Phase | Gate Logic |
|---|---|
| 13 (Early) | Requires `ctx->flags_1382 & 4`; skips if option 214 is set; requires option 487; skips if `*(*(ctx)+1056)` is non-null |
| 29 | Requires option 487; skips if option 231 (dump mode) is set; requires `*(config+33192)` check or option 461 pass; skips if function count == 1 |
| 37 (Mid) | Requires `sub_8F3EA0` pre-check; option 487; can be disabled via `--no-phase ConvertMemoryToRegisterOrUniform`; skips if function count == 1 |
| 46 (Mid2) | Indirect dispatch; skips if vtable slot `[0x1C0]` points to no-op sentinel `sub_7D6DD0` |
| 58 (Late) | Requires function count > 2 (not just > 1); checks optimization level bits `(ctx+1396 & 0x30) != 0x20`; checks option 31 with extended-value semantics |
| 65 (Late2) | Requires function count > 1; indirect dispatch through compilation unit vtable slot at offset 392 |

### 2. Sub-Pass Selection (What Runs)

| Phase | Sub-Passes Included |
|---|---|
| 13 (Early) | Copy prop via `sub_753600` (register-move chains, algebraic identity, modifier canonicalization), instruction rewrite via `sub_753B50`. No instruction-level constant folding. Lightweight -- designed for quick cleanup after initial lowering. |
| 29 | Copy prop with full opcode dispatch (97, 18, 124), predicate-aware propagation via `sub_8F2E50`/`sub_8F29C0`, two-pass predicate simplification via `sub_908A60`, liveness-gated DCE via `sub_7DF3A0`. Flag marking with `0x100`/`0x200`/`0x400` bits. |
| 37 (Mid) | Full sub-pass suite plus `ConvertMemoryToRegisterOrUniform` (memory-to-register promotion). Bitvector-based change tracking. Cost-driven convergence with configurable threshold (default 0.25, knob 474). Most comprehensive instance. |
| 46 (Mid2) | Architecture-dependent (vtable dispatch). May include additional target-specific simplifications. |
| 58 (Late) | 6-slot circular buffer pattern matching over MOV/copy instructions (opcodes 139, 110). Register use-count and aliasing checks. Option-605-gated restriction mode. Per-block single-pass (no iteration). |
| 65 (Late2) | Architecture-dependent (vtable dispatch). Final cleanup before register allocation. |

### 3. Infrastructure Weight (How It Runs)

| Phase | Context Size | Tracking | Complexity |
|---|---|---|---|
| 13 (Early) | Minimal (0x88 bytes on stack) | Boolean changed flag | Low (78 lines in `sub_7917F0`) |
| 29 | Stack frame (~0x60 bytes) | Boolean + instruction flag bits | Medium (218 lines in `sub_908EB0`) |
| 37 (Mid) | 0x408-byte stack context + heap bitvectors | Cost-based convergence (3 doubles) + bitvector arrays | High (500+ lines in setup + 400+ in loop) |
| 46 (Mid2) | Vtable-dependent | Vtable-dependent | Variable |
| 58 (Late) | 0x168-byte stack context | 7 counters at 56-byte stride + 6-slot circular buffer | Medium-high (550 lines in `sub_8F6530`) |
| 65 (Late2) | Vtable-dependent | Vtable-dependent | Variable |

## Initialization Infrastructure

Two large helper functions set up the state required before the sub-passes can run:

### `sub_785E20` -- Change Tracking Reset

Called at the start of phase 13 and after the convergence loop completes (if any changes were made). Resets per-block change flags and instruction state. Takes `(ctx, 0)` -- the second argument selects the reset mode.

### `sub_781F80` -- Instruction Flag Initialization

A large function (~1800 lines) that walks every instruction in every basic block, setting per-instruction optimization flags. Called with argument 1 to enable full initialization. These flags control which instructions are eligible for the sub-passes: instructions marked with certain flag patterns are skipped by copy prop, others are skipped by the algebraic simplifier.

### `sub_7E6090` -- Use-Def Chain Builder

Builds operand use-def chains for copy propagation. Called with `(ctx, 0, 0, 0, 0)` at the start of phases 13 and 58. The zero arguments indicate "build from scratch" rather than incremental update.

### `sub_7E6AD0` -- Def-Use Link Builder

Builds bidirectional def-use/use-def links. Called only by phase 13 (Variant A). Variant B phases use their own bitvector-based tracking instead.

### `sub_905B50` -- Bitvector Infrastructure (Phase 37 Only)

A 500+ line setup function specific to `GeneralOptimizeMid`. Allocates and initializes three major bitvector structures for tracking:
1. Register definition reach (which definitions reach each block entry)
2. Per-register liveness within basic blocks
3. Fold eligibility tracking (which operands have known-constant sources)

These bitvectors are destroyed by RAII-style cleanup after `sub_90FBA0` returns, using vtable destructors at offsets `+32` in the bitvector vtables.

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

## Knobs and Options

| Option | Name/Description | Used By |
|---|---|---|
| 214 | Skip `GeneralOptimizeEarly` when set | Phase 13 only |
| 231 | Dump mode -- skip `GeneralOptimize` when set | Phase 29 only |
| 31 | Architecture-dependent fold eligibility gate | Phase 58; extended-value semantics |
| 135 | Threshold override for cost-based convergence | Phase 37; checked via `*(config+9720)` |
| 461 | Secondary gate for phase 29 | Phase 29; passed through `sub_661470` |
| 464 | **Iteration cap** -- breaks fixed-point loop when returns false | Phase 13, and by extension any Variant A user |
| 474 | Cost convergence threshold (float, default 0.25) | Phase 37 (`sub_90FBA0`) |
| 487 | **General optimization enable** -- master switch for all GeneralOptimize passes | Phases 13, 29, 37 |
| 605 | Restrict circular buffer matching to existing entries only | Phase 58 (`sub_8F6530`) |
| `limit-fold-fp` | `"Enable/disable constant folding of float operations"` | Controls FP constant folding globally |

The `"ConvertMemoryToRegisterOrUniform"` named-phase gate at `0x21DD228` allows phase 37 to be disabled via the `--no-phase` command-line option.

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
| `0x8F7080` | `GeneralOptimizeLate` body | Bitvector-tracked 7-counter pass; calls `sub_8F6FA0` |
| `0x753600` | Per-block sub-pass runner (Early) | Copy prop + algebraic simplify; returns boolean changed |
| `0x753B50` | Per-block apply changes (Early) | Instruction rewriting: `sub_931920`, `sub_932E80`, `sub_749090`, `sub_9253C0` |
| `0x8F6530` | Per-block sub-pass runner (Late) | 6-slot circular buffer; 7-counter change tracking; 550-line function |
| `0x8F6FA0` | Block iterator (Late) | Walks block list calling `sub_8F6530` per block; single pass, no iteration |
| `0x905B50` | Setup/init (Mid) | ~500 lines; creates bitvector infrastructure; 3 tracked structures |
| `0x90FBA0` | Main loop (Mid) | Cost-based instruction-level iteration with constant folding and simplification |
| `0x8F2E50` | Copy/fold eligibility check | Checks opcode (18, 124), operand types, SM version <= 20479, constraint bits |
| `0x8F29C0` | Predicate analysis helper | Determines if predicate condition allows propagation |
| `0x908A60` | Two-pass predicate simplify | Called with direction flag (1 = forward, 0 = backward) |
| `0x785E20` | Change tracking reset | Resets per-block change flags |
| `0x781F80` | Instruction flag init | Initializes per-instruction optimization flags (~1800 lines) |
| `0x7E6090` | Use-def chain builder | Builds operand use-def chains; called with `(ctx, 0, 0, 0, 0)` |
| `0x7E6AD0` | Def-use link builder | Builds def-use/use-def bidirectional links |
| `0x7DF3A0` | Liveness check | Returns status pointer; bits 2-3 (`& 0xC`) indicate live uses |
| `0x7E7380` | Instruction equivalence | Compares two instructions for structural equivalence |
| `0x747F40` | Negation flag extractor | Extracts negation modifier from operand encoding |
| `0x747F80` | Absolute-value flag extractor | Extracts abs modifier from operand encoding |
| `0x748570` | Alias hazard check | Returns true if operand has aliasing hazard |
| `0x1245740` | Sub-DAG equivalence | Compares two instruction sub-DAGs for structural equivalence (arg 2 = depth) |
| `0x91D150` | Register constraint check | Checks register constraints for propagation eligibility |
| `0x91E860` | Use-count estimator | Returns estimated use count for cost-based decisions (used by phase 37) |

## Cross-References

- [Pass Inventory](index.md) -- full 159-phase table with GeneralOptimize instances highlighted
- [Phase Manager](phase-manager.md) -- dispatch loop, vtable protocol, factory switch at `sub_C60D30`
- [Optimization Pipeline](../pipeline/optimizer.md) -- overall pipeline stages
- [Copy Propagation & CSE](copy-prop-cse.md) -- standalone copy propagation passes (phases 49, 50, 64, 83)
- [Liveness Analysis](liveness.md) -- standalone `OriPerformLiveDead` passes (heavier DCE)
- [Knobs System](../config/knobs.md) -- option 464 (iteration cap), option 487 (general opt enable), `limit-fold-fp`
