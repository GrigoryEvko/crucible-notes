# Loop Strength Reduction (NVIDIA Custom LSR)

> **NVIDIA-modified pass.** See [Differences from Upstream](#differences-from-upstream-llvm) for GPU-specific changes.

NVIDIA ships two entirely separate LSR implementations inside cicc v13.0. The first is upstream LLVM's `LoopStrengthReducePass` (approximately 200 helpers across `0x284F650`--`0x287C150`, compiled from `llvm/lib/Transforms/Scalar/LoopStrengthReduce.cpp`). The second is a custom 160KB formula solver (`sub_19A87A0`, 2688 decompiled lines) sitting at `0x199A`--`0x19BF`, wrapped by `NVLoopStrengthReduce` at `sub_19CE990`. Both are invoked through the `"loop-reduce"` pass name in the LLVM new pass manager pipeline, but NVIDIA's overlay replaces the formula generation and selection phases with GPU-aware logic while reusing LLVM's SCEV infrastructure, IV rewriting, and chain construction.

This page documents the NVIDIA overlay -- the most GPU-specific LLVM pass in cicc. If you are reimplementing cicc's optimizer, this is the pass you cannot skip.

## Why NVIDIA Rebuilt LSR

The root motivation is a single equation that does not exist on CPUs: **register count determines occupancy, and occupancy determines performance.** On a GPU, each additional register per thread can cross a discrete [occupancy cliff](../gpu-execution-model.md#occupancy-cliffs), dropping warp-level parallelism by an entire warp group -- see the [GPU Execution Model](../gpu-execution-model.md#register-pressure-and-occupancy) for the register budget and cliff table.

On a CPU, LSR's primary concern is minimizing the number of live induction variables to reduce register pressure, with a secondary goal of producing address expressions that fold into hardware addressing modes. The cost model compares formulae by counting registers, base additions, immediate encoding costs, and setup instructions. This works because a CPU's register file is fixed (16 GPRs on x86-64) and the cost of spilling to cache is relatively uniform.

On an NVIDIA GPU, four properties break this model:

1. **Discrete occupancy cliffs.** A formula that saves one instruction but adds one register might push the kernel past a cliff and lose 50% throughput. The cliff boundaries and their impact are documented in the [occupancy cliff table](../gpu-execution-model.md#occupancy-cliffs).

2. **No equivalent of L1 spill cost.** When a GPU "spills," values go to [local memory](../gpu-execution-model.md#memory-hierarchy) (DRAM, 200-800 cycles), which is orders of magnitude slower than CPU L1 cache.

3. **Address space semantics.** GPU memory is [partitioned into address spaces](../gpu-execution-model.md#address-space-semantics) with different widths and hardware addressing modes. Shared memory (`addrspace(3)`) uses 32-bit pointers with specialized `.shared::` load/store instructions. Generic pointers are 64-bit. Strength-reducing a 32-bit shared-memory pointer can produce 64-bit intermediate values that force truncation, defeating the optimization.

4. **Typed registers.** PTX uses [typed virtual registers](../reference/register-classes.md) (`%r` for 32-bit, `%rd` for 64-bit, `%f` for float). A 64-bit induction variable costs two 32-bit register slots. On older architectures (sm_3x through sm_5x), 64-bit integer operations are emulated and expensive; on sm_70+, native 64-bit addressing makes them acceptable.

LLVM's stock cost model knows none of this. It calls `TTI::isLSRCostLess` which compares an 8-field cost tuple (`{Insns, NumRegs, AddRecCost, NumIVMuls, NumBaseAdds, ImmCost, SetupCost, ScaleCost}`), but the NVPTX TTI implementation cannot express occupancy cliffs, address space constraints, or the sign-extension register savings that matter on GPU. NVIDIA's solution: replace the formula solver entirely, with 11 knobs for fine-grained control.

## Architecture Overview

The NVIDIA LSR overlay is structured as a 7-phase formula solver pipeline. The main entry point is `sub_19A87A0`, which takes a single argument: a pointer to an LSR state object (referred to as `a1` throughout). The state object is large -- relevant fields span from offset 0 through offset 32160.

### LSR State Object Layout

| Offset | Type | Field |
|--------|------|-------|
| `+8` | `ScalarEvolution*` | SCEV analysis handle |
| `+32` | `LoopInfo*` | Loop analysis handle |
| `+40` | `uint64_t` | Target address space identifier |
| `+192` | `int64_t**` | Stride factor table (array of stride values) |
| `+200` | `uint32_t` | Stride factor count |
| `+320` | `void*` | Reuse chain table base |
| `+328` | `uint32_t` | Reuse chain count |
| `+368` | `LoopRecord*` | Loop use-groups array base |
| `+376` | `uint32_t` | Loop use-groups count |
| `+32128` | `RPTracker` | Register pressure tracking structure |
| `+32136` | `void*` | Formula hash table base |
| `+32152` | `uint32_t` | Formula hash table bucket count |
| `+32160` | `void*` | Working formula set |

Each loop record is 1984 bytes. Each use record within a loop is 96 bytes. The loop's use array starts at loop record offset `+744`, with the use count at `+752`. The stride at these sizes -- 1984 bytes per loop, 96 bytes per use -- is a constant throughout all 7 phases. The solver iterates `loop_count * uses_per_loop` in every phase, making this an O(L * U * S) algorithm where S is the stride factor table size.

## The 7-Phase Formula Solver Pipeline

### Phase 1: Initial Use Setup (lines 471--537)

The solver iterates all loop use-groups and, within each, all individual uses. For each 96-byte use record, it:

1. Copies the use record to stack locals (the record contains base SCEV, stride SCEV, flags, formula kind, scaled register array, offset expression, and secondary immediate).
2. Calls `sub_19930D0` to expand the scaled register array into a working formula operand list.
3. Calls `sub_19A22F0` (per-register formula generation): iterates over the scaled register count and calls `sub_19A1B20` for each register operand to generate one initial formula candidate per operand. If `formula_kind == 1` (with-offset mode), it also calls `sub_19A1B20` with `operand_idx = -1` to generate a formula for the offset expression itself.
4. Calls `sub_19A23A0` (alternative formula generation): a second pass with different addressing-mode generation logic, likely producing formulae with combined `base+offset` or folded immediates.

The output of Phase 1 is a set of initial formula candidates, one per (use, scaled register) pair, covering the basic addressing modes.

### Phase 2: Expression Folding with Unfolded Offsets (lines 548--662)

This phase targets uses where `base == NULL` (pure IV uses, no base pointer). It performs two sub-passes:

**Sub-pass A (unfold offset into base):** For each pure-IV use, calls `sub_19A2680` per scaled register to generate candidate formulae that move the offset expression into the base register field. This is the inverse of LLVM's stock "fold offset into immediate" transform -- NVIDIA sometimes wants the offset in a register because GPU addressing modes have limited immediate widths.

**Sub-pass B (factor loop bounds into formula):** Builds an iterator set from the loop's start bound (`+712`) and end bound (`+720`), then calls `sub_19A2820` per (scaled register, iterator bound) pair. This generates formulae that factor common strides out of the loop bounds. For example, if the loop runs `i = 0..N` and a use computes `4*i + base`, this phase can factor out the stride 4 and produce a formula with a single-step IV.

### Phase 3: Stride Factor Expansion (lines 662--862)

Runs only for loops where the use type is 3 (immediate-only addressing). This is the phase that explores alternative stride factors from the stride factor table (`a1+192`).

For each use:

1. Extract the representative SCEV via `sub_1456040`.
2. Verify bit width is at most 64 via `sub_1456C90`.
3. Verify single loop bound (`start == end`, meaning unit stride).
4. Reject floating-point constant offsets (type 15).
5. For each stride factor `S` in the table:
   - Compute `scaled_stride = S * use_stride`.
   - **Overflow check:** verify `S * stride / S == stride` and that `INT64_MIN` is not involved (avoiding signed overflow UB).
   - Validate SCEV representability via `sub_1594790`.
   - Also validate `S * loop_start` and `S * loop_end`.
   - Construct a candidate formula with the factored stride.
   - Run formula legality check via `sub_1995490` (validates against TTI target legality, loop dominance, and SCEV overflow).
   - If legal: rewrite all scaled operands via `sub_13A5B60`, check value equivalence via `sub_1999100`, then commit via `sub_19A1660`.

The overflow guards in this phase are critical. The multiplication overflow check (`v94 * v455 / v94 == v455`) prevents generating formulae whose stride values cannot be represented in 64-bit arithmetic, which would silently produce wrong results.

### Phase 4: Chain-Based Formula Generation (lines 872--1082)

For each use, the solver attempts chain-based strength reduction: building formulae where one IV feeds the next use through a simple increment rather than a full recomputation.

Key logic:

- Extracts the representative SCEV for the use.
- If `formula_kind != 1` (not with-offset) or the formula has a single element, iterates stride factors and builds chained formulae.
- For immediate-type uses (`type == 3`), also considers promoting to with-offset mode (`type == 1`).
- Each candidate is validated through `sub_1995490`.
- Operands are rewritten via `sub_145CF80` (SCEV multiply by stride factor).
- The flag at loop record `+728` controls address-space-aware chain construction. When set, chains respect address space constraints -- critical for shared memory (see the `disable-lsr-for-sharedmem32-ptr` knob section).

### Phase 5: Reuse Chain Matching (lines 1093--1256)

For uses where `base == NULL`, the solver attempts to match existing IV chains for reuse rather than creating new ones.

1. Extract the representative SCEV and compute its "extent" (value range) via `sub_1456E10`.
2. Iterate the reuse chain table (`a1+320` through `a1+328`).
3. For each chain entry, check if the use's extent matches the chain target.
4. Validate legality via `sub_14A2CF0`.
5. If matched: rewrite the use's offset via `sub_147BE70` (SCEV rebase).
6. **Register pressure check:** validate via `sub_19955B0(rp_tracker, scev_value, loop_idx)` that the register pressure after adding this formula stays under the limit.
7. If RP passes: tag with address space via `sub_19932F0`, commit via `sub_19A1660`.

This is where the `lsr-check-rp` and `lsr-rp-limit` knobs have direct effect. The `sub_19955B0` function compares projected register pressure against the configured ceiling and returns false if the formula would exceed it.

### Phase 6: Formula-to-Use Hash Table Construction (lines 1260--1940)

The most complex phase. It builds two hash tables and uses them to identify which formulae serve multiple uses (shared IV expressions).

**Hash Table 1** (7-QWORD entries per slot): maps SCEV expression to a linked list of formula candidates.

| Field Offset | Size | Content |
|-------------|------|---------|
| `+0` | 8 | Key: SCEV pointer, or sentinel (`-8` = empty, `-16` = tombstone) |
| `+8` | 8 | Formula candidate linked list head |
| `+16` | 4 | Candidate count |
| `+24` | 8 | Linked list tail |
| `+32` | 8 | Previous pointer (for median walk) |
| `+40` | 8 | Next pointer (for median walk) |
| `+48` | 8 | Total cost accumulator |

**Hash Table 2** (2-QWORD entries): maps SCEV expression to a use-count bitmap tracking how many uses reference the expression.

Both tables use the same hash function: `(val >> 9) ^ (val >> 4)` masked to the bucket count. Probing is quadratic with tombstone support. Resize triggers at 75% load factor via `sub_19A82C0`.

The phase then:

1. Inserts every formula from the working set into Hash Table 1 with SCEV normalization via `sub_199D980`.
2. Cross-references into Hash Table 2 for use counting, merging bitmaps via `sub_1998630`.
3. Iterates the formula set again and, for each formula, traverses the linked list of referencing uses.
4. Computes combined cost using `sub_220EFE0` (reads cost from a binary tree node at `+32`).
5. Finds the **median-cost insertion point** (threshold at `total_cost / 2`) -- this is a key difference from upstream LLVM, which always picks the cheapest formula. NVIDIA's median heuristic avoids both extremes: the cheapest formula might use too many registers, while the most register-efficient formula might use too many instructions.
6. Builds (register_id, distance) candidate pairs for each (formula, use) combination. If the candidate set exceeds 31 entries, it migrates from an inline `SmallVector` to a balanced tree set (`sub_19A5C50`).

The use-count bitmap uses a compact inline representation: if `(value & 1)`, the high bits encode `max_reg_id` and the remaining bits form the bitmap directly; otherwise, the value is a pointer to a heap-allocated `BitVector` (size at `+16`, data at `+0`). The popcount check at line 1927 (`popcount != 1`) filters out expressions used by only one use -- they cannot benefit from strength reduction.

### Phase 7: Final Formula Selection and Commitment (lines 2042--2686)

After hash table cleanup, the solver iterates the candidate triples `(register_id, distance, scev_use)` and performs the final selection:

```text
for each candidate (reg_id, distance, scev_use):
    loop_record = a1->loops[loop_idx]
    repr_scev   = getStart(scev_use)                    // sub_1456040
    extent      = getExtent(repr_scev)                   // sub_1456E10
    offset_expr = getAddExpr(extent, -distance, 0)       // sub_15A0680
    offset_norm = foldNormalize(scev_ctx, offset_expr)   // sub_146F1B0
    bit_budget  = getBitWidth(offset_norm)                // sub_1456C90

    for each use in loop_record:
        copy 96-byte use record to stack
        if formula_kind == 1:     // with-offset mode
            fold offset into scaled_regs
            set formula_kind = 0  // demote to normal

        if candidate IV appears in use's scaled_regs:
            // Direct replacement path
            validate via sub_1995490 (formula legality)
            build replacement AddRecExpr: sub_147DD40(scev_ctx, [target_iv], 0, 0)
            replace matching operand in formula

            // Sign-extension / width-fit check:
            value_range = computeRange(replacement)
            if abs(distance) < value_range:
                tag with address space (sub_19932F0)
                commit formula (sub_19A1660)

        else if use references a different IV:
            // Cross-IV replacement path
            alt_offset = stride + num_uses * distance
            alt_formula = getAddExpr(extent, -alt_offset, 0)
            validate via sub_1995490

            // Sign-bit check: if sign(replacement) == sign(distance),
            // the formula may wrap -- reject
            if sign_bit_matches: continue

            // Width-fit check via APInt:
            if countLeadingZeros(result) confirms fit in register width:
                commit formula
```

The width-fit checks use full APInt arithmetic (`sub_16A4FD0` for copy, `sub_16A7490` for shift/add, `sub_16A8F40` for negate, `sub_16A7400` for absolute value, `sub_16A7B50` for bitwise AND, `sub_16A57B0` for leading zero count) to determine whether the replacement formula's value range fits in the bit budget. This is essential for correctness: a formula that overflows its register width produces wrong results silently.

## Register Pressure Integration

The integration between LSR and register pressure is the single most important difference from upstream LLVM. It works at three levels:

### Level 1: Hard Gate (lsr-check-rp + lsr-rp-limit)

Before committing any reuse chain formula (Phase 5) and internally within the legality check `sub_1995490`, the solver calls `sub_19955B0(rp_tracker, scev_value, loop_idx)`. This function reads the pre-computed per-loop register pressure estimate from offset `a1+32128` and compares the projected post-formula RP against `lsr-rp-limit`. If the projected RP exceeds the limit, the formula is rejected outright -- it does not even enter the candidate set.

This prevents the pathological case where LSR produces a formula that requires one less instruction per iteration but needs two more live registers, pushing the kernel past an occupancy cliff. On GPU, that one extra instruction is vastly cheaper than the occupancy loss.

### Level 2: Bit Budget Proxy (Phase 7)

The "bit budget" computed in Phase 7 (`v325 = sub_1456C90(offset_norm)`) acts as an indirect register pressure proxy. Wider values need more register slots: a 64-bit value occupies two 32-bit register slots on NVPTX. By enforcing that replacement formulae fit within the bit budget, the solver prevents needless register widening.

### Level 3: Sign-Extension Credit (count-sxt-opt-for-reg-pressure + lsr-sxtopt)

When `lsr-sxtopt` is enabled, LSR attempts to fold sign-extension operations into IV expressions, producing narrower IVs. When `count-sxt-opt-for-reg-pressure` is also enabled, the cost model credits the register pressure savings from eliminated sign-extensions. A formula that requires one more base register but eliminates a sign-extension might be net-neutral or even beneficial in RP terms.

### Level 4: Median-Cost Heuristic (Phase 6)

Rather than always selecting the cheapest formula (as upstream LLVM does), NVIDIA uses a median-cost heuristic. The total cost is summed across all uses of a formula, and the selection threshold is `total_cost / 2`. This balances instruction cost against register pressure: the cheapest formula often has the highest register pressure, while the formula nearest the median typically represents a balanced tradeoff.

## GPU-Specific Knobs

All 11 knobs are registered at `ctor_214_0` (`0x4E4B00`). They are LLVM `cl::opt` command-line options injected through NVIDIA's option registration infrastructure.

### Complete Knob Reference Table

| Knob | Type | Default | Category | Effect |
|------|------|---------|----------|--------|
| `disable-unknown-trip-lsr` | `bool` | `false` | Scope Control | Skips LSR entirely for loops where SCEV cannot determine the trip count. Unknown-trip loops on GPU may be warp-divergent; applying LSR without trip count knowledge can increase register pressure with no loop-count-informed gain. |
| `lsr-check-rp` | `bool` | `true` (likely) | Register Pressure | Master switch for register pressure checking. When disabled, LSR ignores occupancy constraints and behaves more like upstream LLVM. |
| `lsr-rp-limit` | `int` | ~32-64 (likely) | Register Pressure | Register pressure ceiling. If current RP for the loop meets or exceeds this value, LSR is skipped for that loop. The threshold is set to coincide with occupancy cliff boundaries. |
| `filter-bad-formula` | `bool` | `true` (likely) | Formula Quality | Enables NVIDIA's custom formula pruning pass. "Bad" formulae are those requiring too many registers or producing address modes unsupported by SASS (for example, formulae that require scaled-index modes that only exist on CPU). |
| `do-lsr-64-bit` | `bool` | arch-dependent | IV Width | Enables LSR for 64-bit induction variables. Default is `false` on sm_3x through sm_5x (where 64-bit integer ops are emulated), `true` on sm_70+ (native 64-bit datapath). |
| `count-sxt-opt-for-reg-pressure` | `bool` | `true` (likely) | Register Pressure | When calculating RP cost, credits the register savings from sign-extension eliminations that LSR enables. |
| `lsr-sxtopt` | `bool` | `true` (likely) | Sign Extension | Master switch for sign-extension folding within LSR. Folds sign-extension operations into IV expressions to produce narrower IVs, reducing register file consumption. |
| `lsr-loop-level` | `int` | `0` (all) | Scope Control | Restricts LSR to loops at a specific nesting depth. `0` = all levels. `1` = innermost loops only (where address arithmetic is hottest). |
| `lsr-skip-outer-loop` | `bool` | `false` | Scope Control | Skips the outer loop's IV when processing nested loops. Prevents strength-reducing the outer IV when the inner loop is the performance bottleneck. |
| `disable-lsr-for-sharedmem32-ptr` | `bool` | `false` | Address Space | Disables LSR for pointers into 32-bit shared memory (`addrspace(3)`). Protects efficient `.shared::` addressing modes and bank-conflict-free access patterns. |
| `disable-lsr-complexity-discount` | `bool` | `false` | Cost Model | Disables the complexity estimation discount. When the discount is active (this knob is `false`), the cost model gives a bonus to formulae that reduce addressing complexity even if they use more registers. Disabling forces strict register-count-based comparison. |

### Knob Grouping by Function

**Register pressure control (4 knobs):** `lsr-check-rp`, `lsr-rp-limit`, `count-sxt-opt-for-reg-pressure`, `lsr-sxtopt`. These collectively determine whether and how aggressively the solver factors occupancy into formula selection. With all four active, NVIDIA's LSR is deeply occupancy-aware. With all four disabled, it degrades toward upstream LLVM behavior.

**Scope control (3 knobs):** `disable-unknown-trip-lsr`, `lsr-loop-level`, `lsr-skip-outer-loop`. These restrict which loops LSR operates on. They are safety valves: if LSR is hurting a specific kernel, these allow narrowing its scope without disabling it entirely.

**Address space control (2 knobs):** `disable-lsr-for-sharedmem32-ptr`, `do-lsr-64-bit`. These control how LSR interacts with GPU memory semantics. The shared-memory knob protects 32-bit pointer optimality; the 64-bit knob controls IV width policy.

**Cost model control (2 knobs):** `filter-bad-formula`, `disable-lsr-complexity-discount`. These tune the formula evaluation heuristics. The bad-formula filter removes candidates early; the complexity discount adjusts the tradeoff between instruction count and register count.

## Address-Space Awareness

### Shared Memory 32-Bit Pointer Protection

Shared memory on NVIDIA GPUs uses `addrspace(3)` with 32-bit addressing. The hardware provides dedicated `.shared::` load/store instructions with efficient addressing modes, including bank-conflict-free access patterns tied to pointer alignment.

NVIDIA's LSR overlay tracks address spaces at two levels:

1. **Loop level:** the address space identifier at loop record `+40`.
2. **Use level:** the alignment constraint at use record `+48`.

In Phase 4 (chain-based formula generation), line 983 checks `use+48 == a1+40 || flag_at_+728`. If the use's address space matches the target or the address-space-crossing flag is set, the solver uses address-space-aware chain construction. The `sub_19932F0` helper tags committed formulae with the correct address space.

When `disable-lsr-for-sharedmem32-ptr` is enabled, the solver skips all formulae targeting `addrspace(3)` pointers. The rationale: strength-reducing a 32-bit shared memory pointer can create 64-bit intermediate values (the IV increment may be computed in 64-bit before truncation to 32-bit). This defeats the optimization and can prevent the backend from using efficient 32-bit `.shared::` addressing modes.

### 64-Bit IV Control

The `do-lsr-64-bit` knob controls whether LSR generates formulae using 64-bit induction variables. The architecture-dependent default reflects hardware reality:

- **sm_30 through sm_52:** 64-bit integer operations are emulated (two 32-bit ops + carry). A 64-bit IV costs roughly 2x the register pressure and 2x the instruction cost. LSR is disabled for 64-bit IVs.
- **sm_60 through sm_62:** Partial native 64-bit support for address computation.
- **sm_70 and above:** Full native 64-bit addressing and arithmetic. 64-bit IVs become acceptable.

Phase 3 (stride factor expansion) checks the bit width of the representative SCEV (`sub_1456C90` must return at most 64). Phase 7's bit budget check ensures replacement formulae fit within the available register width. Together, these prevent 64-bit IV generation on architectures where it is disabled.

## Sign-Extension Optimization

When `lsr-sxtopt` is enabled, the solver actively seeks to fold sign-extension operations into IV expressions. On NVPTX, this is important because:

1. PTX uses typed registers. A `sext i32 %x to i64` creates a new 64-bit value occupying a separate register pair.
2. If LSR can express the IV in a narrower type from the start, the sign-extension becomes dead code.
3. When `count-sxt-opt-for-reg-pressure` is also enabled, the cost model credits this saving.

The sign-extension check appears in Phase 7's width-fit verification. After constructing a replacement formula, the solver computes the value range using APInt arithmetic and checks whether `abs(distance) < value_range`. If the replacement fits, the sign-extension can be eliminated. An additional sign-bit check (line 2545) rejects replacements where the sign bit of the result matches the sign of the distance -- this would cause the formula to wrap, producing incorrect values.

## Complexity Discount Heuristic

When `disable-lsr-complexity-discount` is `false` (the default), the cost model applies a discount to formulae that reduce addressing complexity, even if they use more registers. "Addressing complexity" here means the number of operations required to compute the effective address for a memory operation.

Consider two formulae for a memory access inside a loop:

- Formula A: `base + 4*i` -- one multiplication, one addition. Requires a scaled index register.
- Formula B: `ptr += 4` each iteration -- one addition per iteration, no multiplication. Requires one increment register.

Formula B is "simpler" in addressing complexity but might use one more register (the incrementing pointer) alongside the existing base. The complexity discount gives Formula B a bonus in the cost model, reflecting the GPU reality that address computation instructions compete with arithmetic instructions for issue slots, while an extra register has low cost when the kernel is not at an occupancy cliff.

When the discount is disabled (the knob is set to `true`), the cost model falls back to strict register-count comparison, similar to upstream LLVM behavior.

## Comparison: NVIDIA LSR vs Upstream LLVM LSR

| Aspect | Upstream LLVM LSR | NVIDIA Custom LSR |
|--------|-------------------|-------------------|
| **Code size** | ~180KB compiled (500+ helpers, 4 mega-functions) | ~160KB compiled (30 functions, main solver 83KB) |
| **Binary location** | `0x284F650`--`0x287C150` | `0x199A`--`0x19BF` overlay |
| **Cost model** | 8-field tuple: `{Insns, NumRegs, AddRecCost, NumIVMuls, NumBaseAdds, ImmCost, SetupCost, ScaleCost}`. Compared via `TTI::isLSRCostLess`. | Register-pressure-aware with occupancy ceiling. Median-cost heuristic. Complexity discount. Sign-extension credit. |
| **Formula selection** | Always picks cheapest formula per cost tuple ordering | Median-cost heuristic: picks near cost midpoint to balance instructions vs registers |
| **Register pressure** | Counted but not capped. No occupancy awareness | Hard-gated: `lsr-check-rp` + `lsr-rp-limit` reject formulae that exceed RP ceiling |
| **Address spaces** | Single flat address space assumed | Full address-space tracking. Shared memory (addrspace 3) gets special 32-bit protection |
| **64-bit IVs** | Always considered if legal | Gated by `do-lsr-64-bit` with architecture-dependent defaults |
| **Sign-extension** | Not a first-class concern | Dedicated optimization path with RP credit (`lsr-sxtopt`, `count-sxt-opt-for-reg-pressure`) |
| **Loop scope** | All loops | Filterable by nesting depth (`lsr-loop-level`) and outer-loop exclusion (`lsr-skip-outer-loop`) |
| **Trip count requirement** | Attempts all loops | Can skip unknown-trip loops (`disable-unknown-trip-lsr`) |
| **Hash table** | `DenseSet<SmallVector<SCEV*>>` for uniquification | Custom 7-QWORD-per-entry hash table with quadratic probing, tombstones, 75% load factor resize, linked-list formula chains, and use-count bitmaps |
| **Formula phases** | Single-pass candidate generation followed by cost-based pruning | 7 sequential phases: initial setup, expression folding, stride expansion, chain generation, reuse matching, hash table construction, final selection |
| **SCEV infrastructure** | Native | Reused from LLVM (shared SCEV, IV rewriting, chain construction) |
| **Tuning knobs** | 7 `cl::opt` knobs (general-purpose: `lsr-insns-cost`, `lsr-filter-same-scaled-reg`, `lsr-complexity-limit`, etc.) | 11 GPU-specific knobs (register pressure, address space, loop scope, cost model) |

### What NVIDIA Reuses From Upstream

The NVIDIA overlay does not replace everything. It reuses:

- **SCEV infrastructure** (`0xDB`--`0xDF` range): `ScalarEvolution` analysis, `AddRecExpr` construction, range analysis, and trip count computation.
- **IV rewriting** (`sub_1997F10`): creates the replacement IV values with the naming convention `"IV.S."` and `"IV.S.next."`.
- **Chain construction** (`sub_199EAC0`): builds IV chains with the `"lsr.chain"` naming prefix.
- **Formula cost model base** (`sub_1995010`): the underlying cost computation, which NVIDIA then wraps with RP checking and sign-extension credit.
- **Terminator folding** (`sub_287C150`): the `"lsr_fold_term_cond"` transform that folds loop exit comparisons.

### What NVIDIA Replaces

- **Formula generation** (Phases 1--5): entirely custom, with address-space awareness, stride factor expansion, and reuse chain matching with RP validation.
- **Formula-to-use mapping** (Phase 6): custom hash tables replacing LLVM's `DenseSet`-based uniquification with a design optimized for linked-list traversal and median-cost computation.
- **Final selection** (Phase 7): custom selection with width-fit checks, sign-extension validation, and cross-IV replacement -- none of which exist in upstream LLVM.

## Key Helper Function Map

For reimplementation reference, the critical helpers and their roles:

| Address | Function | Role |
|---------|----------|------|
| `sub_19A87A0` | Main 7-phase solver | Entry point (83KB, 2688 lines) |
| `sub_19CE990` | `NVLoopStrengthReduce::run()` | Pass wrapper |
| `sub_1995490` | Formula legality validator | TTI + SCEV + loop constraint check |
| `sub_19955B0` | Register pressure check | Compares projected RP vs limit |
| `sub_19932F0` | Address space tagger | Sets addrspace on formula |
| `sub_19A1660` | Formula commit | Sorts, deduplicates, inserts into candidate set |
| `sub_19A22F0` | Per-register formula gen (Phase 1) | Loops `sub_19A1B20` per operand |
| `sub_19A2680` | Unfolded-offset formula gen (Phase 2a) | Offset-to-base transform |
| `sub_19A2820` | Loop-bound-factored formula gen (Phase 2b) | Stride factoring |
| `sub_19A82C0` | Hash table resize | Power-of-two bucket growth |
| `sub_199D980` | SCEV normalization | Canonical form for hashing |
| `sub_1998630` | Use-count bitmap merge | Inline bitmap + heap fallback |
| `sub_1456040` | SCEV `getStart()` | Extract base from AddRecExpr |
| `sub_1456C90` | SCEV `getBitWidth()` | Register width determination |
| `sub_1456E10` | SCEV extent computation | Value range of IV |
| `sub_145CF80` | SCEV `getMulExpr()` | Multiply SCEV by stride factor |
| `sub_147BE70` | SCEV rebase | Rewrite base in AddRecExpr |
| `sub_147DD40` | AddRecExpr constructor | Build replacement IV chain |
| `sub_15A0680` | SCEV `getAddExpr()` | Add constant offset |
| `sub_146F1B0` | SCEV fold/normalize | Simplify expression |

## Data Structure Reference

### Use Record (96 bytes)

```text
+0   [8]  base_scev       : SCEV* (NULL for pure-IV uses)
+8   [8]  stride_scev     : SCEV* (loop stride expression)
+16  [1]  flags           : bit 0 = is_address, bit 1 = has_offset
+24  [8]  formula_kind    : 0 = normal, 1 = with-offset, 3 = immediate-only
+32  [8]  scaled_regs_ptr : pointer to SmallVector<SCEV*>
+40  [4]  scaled_regs_cnt : number of scaled register operands
+48  [32] padding / alignment / additional fields
+80  [8]  offset_scev     : SCEV* (offset expression)
+88  [8]  secondary_imm   : secondary immediate value
```

### Loop Record (1984 bytes)

```text
+32   [4]  use_type        : 0 = generic, 1 = address-check, 3 = immediate
+40   [8]  addr_space      : address space identifier
+48   [4]  alignment       : alignment constraint (bytes)
+712  [8]  loop_start      : SCEV* (loop start bound)
+720  [8]  loop_end        : SCEV* (loop end bound)
+728  [1]  as_aware_flag   : address-space-aware LSR active
+729  [1]  dead_guard_flag : if set && use_count > 0, skip loop
+744  [8]  use_array_ptr   : pointer to array of use records
+752  [4]  use_count       : number of uses in this loop
```

## Reimplementation Notes

1. **Start with the knob infrastructure.** Register all 11 `cl::opt` knobs before anything else. The pass wrapper (`sub_19CE990`) reads these early and uses them to gate entire phases.

2. **The RP tracker must exist before the solver runs.** The register pressure estimate at `a1+32128` is computed by an earlier pass (likely during loop analysis). The NVIDIA LSR does not compute RP itself -- it only reads and compares.

3. **The hash function is deterministic.** `(val >> 9) ^ (val >> 4)` masked to bucket count. Quadratic probing with tombstone support. If you are reimplementing the hash tables, use the same scheme or your formula deduplication will differ.

4. **The median-cost heuristic is the secret sauce.** Upstream LLVM always picks the cheapest formula. NVIDIA picks near the median. This single difference is responsible for most of the occupancy improvements. If you must simplify, keep this heuristic.

5. **The overflow checks in Phase 3 are load-bearing.** The `S * stride / S == stride` check and the `INT64_MIN` guard prevent generating formulae with wrapped arithmetic. Removing these checks will produce silently wrong code on kernels with large strides.

6. **Address space tagging (`sub_19932F0`) must happen before commit.** Every formula committed via `sub_19A1660` must carry the correct address space tag. Forgetting this will produce PTX that uses generic loads/stores where shared-memory instructions are required, breaking both performance and correctness.

7. **The use-count bitmap has two representations.** Inline (when `value & 1`) and heap-allocated. The inline form is fast but limited to small register ID ranges. The heap form uses a `BitVector` with the size at `+16`. Both must be supported.

8. **Phase ordering is strict.** The 7 phases must run in order. Later phases depend on candidates generated by earlier ones, and the hash tables in Phase 6 assume all candidates have been generated by Phases 1--5.

## Differences from Upstream LLVM

| Aspect | Upstream LLVM | CICC v13.0 |
|--------|---------------|------------|
| **Formula solver** | Single LLVM `LoopStrengthReduce` with TTI-based cost model | Two implementations: stock LLVM LSR + custom 160 KB NVIDIA formula solver (`sub_19A87A0`, 2688 lines) that replaces formula generation/selection |
| **Cost model** | 8-field cost tuple (`{Insns, NumRegs, AddRecCost, ...}`), no occupancy concept | Occupancy-aware cost: register count evaluated against discrete warp occupancy cliffs where +1 register can halve throughput |
| **Address space awareness** | No address space semantics in formula selection | Address space tagging (`sub_19932F0`) ensures formulae preserve shared memory (addrspace 3) 32-bit pointer width; prevents strength-reducing 32-bit pointers into 64-bit generic form |
| **Knob count** | ~5 knobs for cost tuning | 11 knobs for fine-grained GPU-specific control (`lsr-no-ptr-address-space3`, stride limits, formula depth, etc.) |
| **Algorithm structure** | Monolithic formula generator + greedy selector | 7-phase formula solver pipeline: candidate generation, stride-based filtering, use-group analysis, formula selection, commit, rewrite |
| **State object** | Modest state for formula tracking | 32,160-byte state object with embedded register pressure tracker, formula hash table, and per-use-group formula arrays |
| **Typed register cost** | All registers weigh the same | 64-bit IVs cost two 32-bit register slots; emulated on sm_3x--5x; native on sm_70+ but still double the pressure |
