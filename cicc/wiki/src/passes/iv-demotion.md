# IV Demotion

IV demotion is NVIDIA's proprietary induction variable narrowing sub-pass, embedded within the IR-level [rematerialization](./rematerialization.md) pass (`nvvmrematerialize`). It reduces register pressure by converting wide induction variables -- typically 64-bit -- to narrower types, typically 32-bit. On NVIDIA GPUs this is a high-impact optimization: the NVPTX ISA provides native 32-bit integer arithmetic in a single instruction, while 64-bit operations require multi-instruction sequences (add.cc + addc for a single 64-bit add, for example). A 64-bit loop induction variable that provably fits in 32 bits wastes two registers where one would suffice, and every arithmetic operation on it costs roughly twice the instruction count.

The sub-pass is large -- 75KB of compiled code, larger than the main rematerialization driver itself -- reflecting the complexity of proving that narrowing is safe across all uses of an IV, rewriting PHI nodes, adjusting comparisons, and handling edge cases where some uses require the original width while others can consume the narrowed version.

## Key Facts

| Property | Value |
|---|---|
| Entry point | `sub_1CD74B0` (12 KB native; ~2,500 lines decomp) |
| Parent pass | `nvvmrematerialize` (IR-level rematerialization) |
| Invocation site | `sub_1CE7DD0` line ~2276 (post-remat phase) |
| Primary knob | `remat-iv` (default **4** = full demotion) |
| Debug knob | `dump-remat-iv` (default 0) |
| Gate condition | `dword_4FBFB40 != 0` (non-zero enables the sub-pass) |
| Helper: IV analysis | `sub_1CD5F30` |
| Helper: IV base lookup | `sub_1CD5400` |
| Helper: cleanup | `sub_1CD0600` |
| IR builder | `sub_15FB440` (opcode, type, operand, name, insertpt) |
| Width query | `sub_127FA20` (`DataLayout::getTypeStoreSize`) |

## Demotion Levels

The `remat-iv` knob controls five demotion aggressiveness levels:

| Level | Behavior | Gate in binary |
|---|---|---|
| 0 | Disabled -- IV demotion entirely skipped | `dword_4FBFB40 == 0` |
| 1--2 | Basic IV demotion. Only simple induction variables with constant step and all uses in the same loop body. | Default path |
| 3 | Extended IV demotion. Enables demotion of IVs whose uses extend to loop-exit comparisons and address computations outside the innermost loop. | line 1380: `if (dword > 3)` |
| 4 | Full demotion (default). Includes complex patterns: IVs used in GEP chains, IVs with multiple PHI consumers, and IVs that feed into both narrow and wide downstream computations. | line 1546: `if (dword <= 4)` |
| 5+ | Aggressive mode. Relaxes safety margins on range proofs, allowing demotion when the range check is tight (no headroom). |  |

Level 4 is the default because it captures the vast majority of profitable demotion opportunities in real CUDA kernels without the correctness risk of aggressive mode.

## Algorithm

### Phase 1: Loop Iteration and PHI Identification

The algorithm iterates over every loop in the function (obtained from LoopInfo, `sub_1440EE0`). For each loop, it examines the loop header block's PHI nodes. Each PHI node is a candidate induction variable. The pass checks the PHI's type width via `sub_127FA20` (`DataLayout::getTypeStoreSize`).

```c
for each loop L in function:
    header = L.getHeader()
    for each PHI in header:
        width = getTypeStoreSize(PHI.getType())    // sub_127FA20
        if width != 64:
            continue   // only demote 64-bit IVs to 32-bit
```

### Phase 2: Increment Pattern Analysis

For each 64-bit PHI, the pass identifies the increment pattern -- the value feeding back from the latch block. It verifies the pattern is a simple `add`/`sub` by a constant. The helper `sub_1CD5F30` (IV analysis helper) walks the def-use chain of the PHI's backedge operand to extract the step value and verify linearity.

```c
backedge_val = PHI.getIncomingValueForBlock(latch)
if backedge_val is not (PHI + constant) and
   backedge_val is not (PHI - constant):
    skip this PHI    // non-linear IV, cannot demote
step = extract_constant(backedge_val)
```

### Phase 3: Value Range Fitting

The critical safety check. The pass must prove that the IV's value never exceeds the 32-bit signed range throughout the loop's execution. The check uses an unsigned comparison trick:

```c
(val + 0x80000000) <= 0xFFFFFFFF
```

This is equivalent to checking `-2^31 <= val <= 2^31 - 1` (the signed i32 range). Adding `0x80000000` shifts the signed range to `[0, 0xFFFFFFFF]`, which can be checked with a single unsigned comparison. The pass evaluates this condition on:

1. The initial value (from the preheader incoming edge of the PHI).
2. The final value (derived from the loop trip count and step).
3. Conservatively, any intermediate values if the step is not +1/-1.

The initial value and trip count information come from the loop analysis infrastructure. The pass does not directly invoke SCEV (`ScalarEvolution`) -- it operates on NVIDIA's own IR-level live variable analysis and loop info passes (`sub_1440EE0` for loop structure, `sub_1BFC830` for live variable analysis). However, upstream LLVM's IndVarSimplify (`sub_1945A50`) may have already widened or simplified IVs using SCEV before this pass runs. The IV demotion pass operates on whatever IV structure remains after the main optimization pipeline.

If the range check fails, the IV is skipped. There is no speculative demotion with runtime guards.

### Phase 4: Use Analysis and Classification

Before rewriting, the pass classifies every use of the original 64-bit IV:

- **Narrow-safe uses**: Arithmetic (add, sub, mul, shift), array indexing within the loop body. These can consume the 32-bit value directly.
- **Comparison uses**: Loop exit conditions (`icmp`). These need a narrow comparison instruction (`newICmp`).
- **Address uses**: GEP instructions that use the IV as an index. At level 4+, these are handled by cloning the base address computation (`iv_base_clone_`).
- **Escape uses**: Uses outside the loop (LCSSA PHIs, return values). These require sign/zero extension back to 64-bit.

The level knob gates which use categories are eligible:

| Use category | Minimum level |
|---|---|
| Same-block arithmetic | 1 |
| Loop exit comparisons | 2 |
| Cross-block GEP indexing | 3 |
| Multi-PHI consumers | 4 |
| Tight-range speculation | 5 |

### Phase 5: Instruction Generation

Once an IV is approved for demotion, the pass generates four types of synthetic instructions:

#### `demoteIV` -- Truncation

```c
v475 = "demoteIV";
v366 = sub_15FB440(11, destg, v401, &v475, v115);
// opcode 11 = trunc
```

Creates a `trunc i64 %iv to i32` instruction, inserted at the point where the original IV was defined. This is the primary demotion: the new 32-bit value replaces the old 64-bit value for all narrow-safe uses.

**IR before:**
```llvm
%iv = phi i64 [ %init, %preheader ], [ %iv.next, %latch ]
%iv.next = add i64 %iv, 1
```

**IR after (`demoteIV` inserted):**
```llvm
%iv = phi i64 [ %init, %preheader ], [ %iv.next, %latch ]
%demoteIV = trunc i64 %iv to i32
```

#### `newBaseIV` -- Narrow PHI Replacement

```c
v475 = "newBaseIV";
desth = sub_15FB440(11, v289, v427, &v475, destd);
```

When the entire loop can use a 32-bit IV, the pass creates a completely new PHI node with `i32` type in the loop header. The old 64-bit PHI is not simply truncated -- a new narrow induction cycle is constructed:

- A narrow initial value: `%newInit = trunc i64 %init to i32`
- A narrow PHI: `%newBaseIV = phi i32 [ %newInit, %preheader ], [ %newInc, %latch ]`
- A narrow increment: `%newInc = add i32 %newBaseIV, <step32>`

The old 64-bit IV becomes dead if all uses are successfully rewritten.

**IR after (full base IV replacement):**
```llvm
%newInit = trunc i64 %init to i32
%newBaseIV = phi i32 [ %newInit, %preheader ], [ %newInc, %latch ]
%newInc = add i32 %newBaseIV, 1
```

#### `iv_base_clone_` -- Comparison Clone

```c
v475 = "iv_base_clone_";
v214 = sub_15F4880(v210);         // clone instruction
sub_164B780(v214, &v475);         // set name
sub_15F2120(v395, v198);          // insert into block
```

When some uses of the IV require the original 64-bit width -- typically the loop exit comparison or an address computation that cannot be narrowed -- the pass clones the IV's base value. The clone instruction preserves the original semantics while allowing the primary loop computation to proceed with the narrow type. The clone is placed at the specific use site rather than at the loop header, avoiding the register pressure cost of keeping the wide value live across the entire loop body.

#### `substIV` -- Use Replacement

After generating the narrow IV infrastructure, the pass walks all uses of the original wide IV and replaces them with the demoted version. This is the final rewriting step:

- Arithmetic uses: replaced with uses of `%newBaseIV` or `%demoteIV`.
- Comparison uses: replaced with narrow comparisons (`newICmp`) on the demoted value.
- PHI uses at LCSSA boundaries: a `sext`/`zext` is inserted to restore 64-bit width for consumers outside the loop.

The pass also creates `newICmp` instructions -- narrower comparison instructions that compare `i32` values instead of `i64` values, rewriting the loop exit condition to match the demoted IV.

After all use replacement, `sub_1CD0600` performs dead code cleanup: if the original 64-bit IV has no remaining uses, the wide PHI and its increment chain are deleted.

## GPU Motivation: 32-bit vs. 64-bit Performance

The performance gap between 32-bit and 64-bit integer operations on NVIDIA GPUs is substantial and architectural, not merely a throughput difference:

**Instruction count.** 64-bit integer addition on PTX compiles to two machine instructions (`add.cc.u32` + `addc.u32`) because the hardware ALU is 32-bit wide. A 64-bit multiply is even worse: it decomposes into multiple 32-bit multiplies and adds. Every loop iteration with a 64-bit IV pays this tax on the increment alone.

**Register pressure.** A single `i64` value occupies a pair of 32-bit registers. In a loop with 3 IVs, demoting all three frees 3 registers -- enough to cross an [occupancy cliff](../gpu-execution-model.md#occupancy-cliffs) and gain an entire warp group on many kernels.

**Address arithmetic.** CUDA uses 64-bit pointers (`nvptx64` target), so loop index computations are promoted to `i64` by default during LLVM IR generation. But most CUDA kernels operate on arrays smaller than 4 GB, making the upper 32 bits of the index perpetually zero. The IV demotion pass recovers this wasted precision.

**Pipeline utilization.** GPU SM pipelines have limited integer execution units. Halving the instruction count for IV arithmetic directly translates to higher utilization of other functional units (FP, memory) in the same warp cycle.

## Configuration

### Knobs (registered at `ctor_277_0`, address `0x4F7BE0`)

| Knob | Global | Default | Description |
|---|---|---|---|
| `remat-iv` | `dword_4FBFB40` | **4** | IV demotion level. 0=off, 1-2=basic, 3=extended, 4=full, 5+=aggressive. |
| `dump-remat-iv` | `dword_4FC0160` | 0 | Debug dump verbosity for IV demotion. Non-zero enables diagnostic output. |

The `remat-iv` knob is read by the main rematerialization driver (`sub_1CE7DD0`) at the post-remat phase gate. When non-zero, `sub_1CD74B0` is invoked. The level value is then read inside `sub_1CD74B0` to control which demotion patterns are attempted.

### Interaction with ptxas

The ptxas assembler has its own rematerialization controls (`--knob RegAllocRematEnable`, `RematEnable`, etc.) but does not have an IV demotion equivalent. IV demotion is purely an IR-level transformation -- by the time ptxas sees the code, the IVs are already narrow. The ptxas knob `--advanced-remat` (0/1/2) controls machine-level rematerialization but does not perform type narrowing.

## Diagnostic Strings

All strings emitted by `sub_1CD74B0`:

```text
"phiNode"           -- PHI node identification during loop header scan
"demoteIV"          -- Truncation instruction creation
"newInit"           -- Narrow initial value for new base IV
"newInc"            -- Narrow increment for new base IV
"argBaseIV"         -- Base IV argument lookup
"newBaseIV"         -- New narrow PHI node creation
"newICmp"           -- Narrow comparison instruction creation
"iv_base_clone_"    -- Clone of IV base for original-width uses
"substIV"           -- Use replacement pass
```

These strings are set as instruction name prefixes via `sub_164B780` (for cloned instructions) or passed directly to the IR builder `sub_15FB440`. They appear in IR dumps when `dump-remat-iv` is non-zero or when the module is printed after the rematerialization pass.

## Differences from Upstream LLVM

Upstream LLVM's `IndVarSimplify` pass (`indvars`) performs IV widening and narrowing through SCEV-based analysis. NVIDIA's IV demotion sub-pass is a completely separate implementation with several key differences:

| Aspect | Upstream `IndVarSimplify` | NVIDIA IV Demotion |
|---|---|---|
| Analysis framework | SCEV (`ScalarEvolution`) | NVIDIA live variable analysis + LoopInfo |
| Direction | Primarily widens narrow IVs to canonical form | Narrows wide IVs to reduce register pressure |
| Motivation | Canonical form for other optimizations | Register pressure reduction for GPU occupancy |
| Placement | Early in optimization pipeline | Late, inside rematerialization (post-optimization) |
| Range proof | SCEV range analysis | Direct `(val + 0x80000000) <= 0xFFFFFFFF` check |
| IV creation | SCEV expander | Direct IR builder calls (`sub_15FB440`) |
| Configuration | `indvars-widen-indvars` (bool) | `remat-iv` (6-level integer knob) |

The two passes are complementary. `IndVarSimplify` runs early and may widen IVs for canonical form. Later, IV demotion runs inside rematerialization and narrows them back when the wide form causes excessive register pressure. This is not redundant work -- the early widening enables other optimizations (loop vectorization, strength reduction), and the late narrowing recovers the register cost after those optimizations have completed.

## Function Map

| Function | Address | Size | Role |
|----------|---------|------|------|
| IV demotion entry | `sub_1CD74B0` | 75KB | Main algorithm: PHI scan, range check, rewrite |
| IV analysis helper | `sub_1CD5F30` | -- | Walks def-use chain to extract step/linearity |
| IV base lookup | `sub_1CD5400` | -- | Finds base value of induction variable |
| Dead IV cleanup | `sub_1CD0600` | -- | Removes unreferenced wide IVs after demotion |
| IR builder | `sub_15FB440` | -- | Creates instruction (opcode, type, operand, name, insertpt) |
| Clone instruction | `sub_15F4880` | -- | Clones an IR instruction (for `iv_base_clone_`) |
| Set name prefix | `sub_164B780` | -- | Sets name string on cloned instruction |
| Insert into block | `sub_15F2120` | -- | Inserts instruction at specified position |
| Replace uses | `sub_1648780` | -- | Rewrites all uses of a value to a new value |
| Delete dead instr | `sub_15F20C0` | -- | Erases instruction from parent block |
| Type store size | `sub_127FA20` | -- | `DataLayout::getTypeStoreSize` -- returns bit width |

## Cross-References

- [Rematerialization](./rematerialization.md) -- parent pass; IV demotion is invoked in the post-remat phase
- [ScalarEvolution](../llvm/scev.md) -- upstream SCEV framework; not used directly by IV demotion but related
- [IndVarSimplify](../llvm/loop-passes-standard.md) -- upstream IV canonicalization pass
- [LLVM Optimizer](../pipeline/optimizer.md) -- pipeline context showing where rematerialization runs
- [Knobs](../config/knobs.md) -- central knob inventory
