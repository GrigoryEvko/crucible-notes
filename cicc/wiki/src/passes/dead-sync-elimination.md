# Dead Synchronization Elimination

The dead synchronization elimination engine at `sub_2C84BA0` is the largest NVIDIA-custom pass in cicc at 96KB (~3,400 decompiled lines). It removes `__syncthreads()` barriers that provably do not order any memory hazard, reducing warp stall cycles in CUDA kernels without affecting correctness. The algorithm performs a bidirectional fixed-point dataflow analysis across the entire function's CFG, tracking four memory access categories per basic block through eight red-black tree maps. After convergence, it evaluates every barrier against the computed access sets and deletes those that protect no actual hazard. Each deletion triggers a full restart of the analysis, handling cascading redundancies at the cost of quadratic worst-case complexity.

This pass is distinct from the lightweight [`basic-dbe`](./dead-barrier-elim.md) pass (slot 376, `llvm::BasicDeadBarrierEliminationPass`) and from the [`branch-dist`](./branch-distribution.md) pass. All three target dead barriers, but only this engine performs full inter-block dataflow with complete restart -- the other two handle simpler local or single-pass cases.

## Key Facts

| Property | Value |
|---|---|
| Entry point | `sub_2C84BA0` |
| Binary size | 96KB (~3,400 decompiled lines) |
| Pass type | Module-level NVIDIA custom (not registered in New PM) |
| Callers | `sub_2C88020`, `sub_2C883F0`, self-recursive |
| Barrier predicate | `sub_2C83D20` |
| Access classifier | `sub_2C83AE0` |
| Per-BB analysis | `sub_2C84640` (bidirectional, parameterized by direction) |
| State object | 12 red-black tree maps at known offsets in `a1` |
| Diagnostic | `" Removed dead synch: "` with per-category read/write counts |
| Upstream equivalent | None -- entirely NVIDIA-proprietary |

## Five-Phase Algorithm

### Phase 1: Barrier Identification (`sub_2C83D20`)

The helper `sub_2C83D20` classifies whether a given instruction is a synchronization barrier. The check is a conjunction of five conditions:

```
function isSyncBarrier(inst) -> bool:
    if inst.opcode != 85:                       // internal call opcode
        return false
    callee = inst.field[-32]                     // callee pointer at offset -32
    if callee == null:
        return false
    if callee.byte[0] != 0:                     // byte 0 == 0 means intrinsic (not user-defined)
        return false
    if callee.field[24] != inst.field[80]:       // scope match
        return false
    if !(callee.byte[33] & 0x20):               // convergent attribute flag
        return false
    return CEA1A0(callee.field[36])              // confirm barrier intrinsic ID
```

The `convergent` attribute flag (bit `0x20` at byte+33) is the key discriminator. LLVM marks barrier intrinsics as `convergent` to prevent optimizations from moving them across control flow boundaries. The final `sub_CEA1A0` call validates that the intrinsic ID falls within the known barrier ID range, distinguishing barriers from other convergent intrinsics (e.g., warp vote operations).

### Phase 2: Memory Access Classification (`sub_2C83AE0`)

For every non-barrier instruction, `sub_2C83AE0` determines whether it reads from or writes to memory that could create a hazard across a barrier. It outputs two boolean flags via pointer parameters `a2` (read) and `a3` (write).

| Opcode | Value | Instruction | Classification |
|---|---|---|---|
| 61 | `0x3D` | Store | Write, if element size > `0x1FF` bits |
| 62 | `0x3E` | Load | Read, with same large-type gate |
| 65 | `0x41` | Atomic | Read + Write |
| 66 | `0x42` | AtomicCmpXchg | Write |
| 85 | `0x55` | Call/Intrinsic | Context-dependent (see below) |

For call instructions (opcode 85), the classifier applies recursive analysis:

1. Check if the callee has intrinsic flag `0x20` set.
2. For barrier-like intrinsics with opcode 25 and `field+96 == 0`: classify as **Read only**.
3. For general calls: invoke `sub_B49E00` (`isSharedMemoryAccess`) to determine whether the callee accesses shared/global memory. If yes: **Read + Write**.

The element size gate (`> 0x1FF` bits, i.e., > 511 bits) filters out trivially small memory operations that target scalar types in registers rather than actual memory-backed storage. Loads and stores of types narrower than 512 bits are assumed to operate on register-promoted values and do not participate in cross-thread hazards.

### Phase 3: Bidirectional Fixed-Point Dataflow

**Complexity.** Let B = number of basic blocks, S = number of barrier instructions, and I = total instructions across all blocks. Phase 1 (barrier identification) is O(S). Phase 2 (access classification) is O(I). The dataflow fixed-point iterates until no boolean in the 4 * B * 2 lattice positions flips from 0 to 1; since the lattice has height 1, convergence is bounded by O(B) iterations, each costing O(B + I) for the forward and backward scans, giving O(B * (B + I)) per convergence cycle. Phase 4 (elimination decision) is O(S). Phase 5 restarts the entire analysis from Phase 3 on each removal, yielding a worst-case total of O(S * B * (B + I)). In practice, CUDA kernels have B < 100, S < 20, and convergence in 2--3 iterations, so the pass behaves as near-linear in typical use. The red-black tree maps contribute O(log B) per insert/lookup, but this is dominated by the iteration cost.

This is the core of the pass and accounts for the majority of its 96KB size. The algorithm maintains **eight red-black tree maps** organized into forward and backward analysis sets, plus **four bridge maps** for the final elimination decision.

#### Map Layout

| Offset range | Direction | Contents |
|---|---|---|
| `a1[15..20]` | Forward | ReadAbove per basic block |
| `a1[21..26]` | Forward | WriteAbove per basic block |
| `a1[27..32]` | Forward | ReadBelow per basic block |
| `a1[33..38]` | Forward | WriteBelow per basic block |
| `a1[39..44]` | Backward | ReadAbove per basic block |
| `a1[45..50]` | Backward | WriteAbove per basic block |
| `a1[51..56]` | Backward | ReadBelow per basic block |
| `a1[57..62]` | Backward | WriteBelow per basic block |
| `a1[63..68]` | Bridge | ReadAbove crossing barrier |
| `a1[69..74]` | Bridge | WriteAbove crossing barrier |
| `a1[75..80]` | Bridge | ReadBelow crossing barrier |
| `a1[81..86]` | Bridge | WriteBelow crossing barrier |

Each map is a `std::map`-style red-black tree (48-byte nodes: left/right/parent pointers, key = basic block pointer, value = 1-byte boolean at offset 40). The helper `sub_2C84590` performs map insertion; `sub_2C84AF0` is a variant for a different node type used in the bridge maps.

#### Iteration Algorithm

The analysis loop is implemented as a goto-based iteration between labels `LABEL_2` and `LABEL_178` in the decompiled output:

```
function analyzeBarriers(F, state):
    LABEL_2:  // restart point after barrier removal

    // --- Forward pass ---
    for each BB in F:
        sub_2C84640(state, BB, direction=1)  // scan BB forward
            // For each instruction from BB start toward first barrier:
            //   classify as read/write via sub_2C83AE0
            //   OR the flags into forward maps [15..38]
            // Propagate successor BBs' flags backward if they
            // contain already-analyzed barriers

    // --- Forward convergence check ---
    changed_fwd = false
    for each BB in F:
        if forward_maps[BB] != previous_forward_maps[BB]:
            changed_fwd = true
            break

    // --- Backward pass ---
    for each BB in F:
        sub_2C84640(state, BB, direction=0)  // scan BB backward
            // For each instruction from BB end toward last barrier:
            //   classify as read/write
            //   OR into backward maps [39..62]
            // Propagate predecessor BBs' flags forward

    // --- Backward convergence check ---
    changed_bwd = false
    for each BB in F:
        if backward_maps[BB] != previous_backward_maps[BB]:
            changed_bwd = true
            break

    // If either direction changed, iterate
    if changed_fwd or changed_bwd:
        goto LABEL_2_inner  // re-run dataflow (not full restart)

    // Both converged -- proceed to Phase 4
    goto elimination_phase
```

The `sub_2C84640` helper is the per-BB analysis workhorse. It takes a direction parameter:

- **direction=1 (forward)**: scans from block entry toward the first barrier, accumulating ReadAbove/WriteAbove. Propagates read/write information from successor blocks.
- **direction=0 (backward)**: scans from block exit toward the last barrier, accumulating ReadBelow/WriteBelow. Propagates information from predecessor blocks.

The convergence check compares the entire map contents (all four categories for every BB) against their values from the previous iteration. If any single boolean flipped from 0 to 1, the changed flag is set. Since the analysis is monotone (booleans can only transition from 0 to 1, never back), convergence is guaranteed in at most O(|BB|) iterations, though in practice it converges in 2--3 iterations for typical CUDA kernels.

### Phase 4: Elimination Decision

After the dataflow converges, the pass examines every barrier instruction and checks the **bridge maps** (`a1[63..86]`) which represent the combined read/write sets crossing barrier boundaries.

A barrier is **redundant** (dead) if any of the following holds:

| Condition | Interpretation |
|---|---|
| `ReadAbove == 0 AND WriteAbove == 0` | No shared-memory accesses reach this barrier from above; the barrier orders nothing |
| `ReadBelow == 0 AND WriteBelow == 0` | No accesses reach from below |
| `ReadAbove == 0 AND WriteBelow == 0` | No RAW or WAW hazard across the barrier |
| `WriteAbove == 0 AND ReadBelow == 0` | No WAR or WAW hazard across the barrier |

The first two conditions capture the case where one side of the barrier has no memory traffic at all. The latter two capture the case where both sides access memory, but the access patterns cannot conflict.

```c
/* Phase 4 elimination decision driven by the four bridge maps
 * (state->maps[63..86]). One pass over every barrier; restart from
 * Phase 3 on the first removal because cascading is possible. */
bool decideEliminations(DSEState *state, Function *F) {
    for (Instruction *I = firstInst(F); I; I = nextInst(I)) {
        if (!isSyncBarrier(I)) continue;                  /* sub_2C83D20 */

        bool RA = rbtreeLookup(state->maps[63], I);       /* ReadAbove   */
        bool WA = rbtreeLookup(state->maps[69], I);       /* WriteAbove  */
        bool RB = rbtreeLookup(state->maps[75], I);       /* ReadBelow   */
        bool WB = rbtreeLookup(state->maps[81], I);       /* WriteBelow  */

        bool dead = (!RA && !WA)           /* no traffic above       */
                 || (!RB && !WB)           /* no traffic below       */
                 || (!RA && !WB)           /* no RAW/WAW across      */
                 || (!WA && !RB);          /* no WAR/WAW across      */

        /* Special case for syncthreads_{count,and,or}: IDs 8260..8262.
         * The barrier's return value carries data; if it has at most
         * one use, the data flow is trivially handled and the fence
         * itself may still be redundant. */
        unsigned id = getIntrinsicId(I);
        if (id - 8260u <= 2u && hasOneUse(I))             /* sub_BD3660 */
            dead = true;

        if (dead) {
            emitDiagnostic(I, RA, WA, RB, WB);            /* "Removed dead synch:" */
            eraseFromParent(I);                           /* sub_B43D60 */
            return true;                                  /* triggers Phase 3 restart */
        }
    }
    return false;
}
```

#### Special Case: Intrinsic IDs 8260--8262

For call instructions (opcode 85) where the callee's intrinsic ID satisfies `(ID - 8260) <= 2` (i.e., IDs 8260, 8261, or 8262), the pass applies an additional test via `sub_BD3660` (`hasOneUse`). If the barrier-like intrinsic has only a single use, it is considered removable even if the standard dataflow check would keep it. These IDs likely correspond to specialized barrier variants (`__syncthreads_count`, `__syncthreads_and`, `__syncthreads_or`) where the return value is used as data. When the return value has only one use, the compiler can reason that the data-carrying aspect is trivially handled and the barrier itself may still be dead from a memory ordering perspective.

### Phase 5: Removal and Complete Restart

When a barrier is identified as dead, the pass:

1. Emits a diagnostic string (if the controlling dump flag is enabled):
   ```
    Removed dead synch: [filename:line] in function <name>
   Read above: N, Write above: N, Read below: N, Write below: N
   ```
   where N is 0 or 1 for each category.

2. Calls `sub_B43D60` (`Instruction::eraseFromParent`) to delete the barrier instruction from the IR.

3. **Restarts from Phase 3** (goto LABEL_2) -- a complete re-analysis of the entire function.

The restart is not optional. Removing a barrier changes the memory access pattern visible between adjacent barriers: what was previously two separate "above/below" regions separated by a barrier now becomes a single merged region. This merging may cause an adjacent barrier to lose its hazard justification, making it dead as well. The cascading effect can propagate through a chain of barriers.

## Dataflow Analysis Detail

### The Eight Red-Black Tree Maps

The forward and backward map sets track identical categories but are populated from different CFG traversal directions. This bidirectional approach ensures that the analysis captures memory accesses reachable from any control flow path, not just straight-line code.

**Forward maps (`a1[15..38]`):**
- Populated by scanning each BB from entry toward the first barrier.
- ReadAbove[BB] = 1 if any instruction before the first barrier in BB reads shared/global memory, OR if any successor BB contributes a read.
- WriteAbove[BB] = same for writes.
- ReadBelow/WriteBelow[BB] = propagated from successor blocks' analysis.

**Backward maps (`a1[39..62]`):**
- Populated by scanning each BB from exit toward the last barrier.
- ReadBelow[BB] = 1 if any instruction after the last barrier in BB reads memory, OR if any predecessor BB contributes a read.
- WriteBelow[BB] = same for writes.
- ReadAbove/WriteAbove[BB] = propagated from predecessor blocks.

**Bridge maps (`a1[63..86]`):**
- Keyed by barrier instruction pointer (not BB pointer).
- Represent the combined access sets that cross the specific barrier boundary.
- Populated during the final pass over barrier instructions after dataflow convergence.

### Monotone Dataflow Framework

The analysis is a classic monotone dataflow problem on a Boolean lattice:

- **Domain**: {0, 1} per (basic-block, category) pair.
- **Transfer function**: OR of local classification with propagated values.
- **Meet operator**: OR (any path contributing an access sets the flag).
- **Direction**: Bidirectional (forward pass propagates from successors, backward pass propagates from predecessors).
- **Convergence**: Guaranteed because the lattice has height 1 (a value can only change from 0 to 1, never back). The fixed point is reached when no additional propagation changes any value.

In the worst case, each iteration may set one new bit, and there are 4 * |BB| bits per direction, so convergence takes at most 4 * |BB| iterations per direction. In practice, CUDA kernels have shallow CFGs and the iteration converges in 2--3 rounds.

## Cascading Restart Logic

The most expensive aspect of the algorithm is the complete restart after each barrier removal. Consider a function with N barriers:

```
B0 -- barrier_1 -- B1 -- barrier_2 -- B2 -- barrier_3 -- B3
```

If `barrier_2` is removed first, blocks B1 and B2 merge into a single region. If B1 contained only writes and B2 contained only reads, `barrier_1` was previously justified by the WAR hazard between B0's writes and B1's reads. But after merging, B1+B2 now contains both reads and writes, and `barrier_3` might become dead if B3 has no memory accesses. This cascading effect requires full re-analysis.

**Worst-case complexity**: O(N_barriers * N_BBs * convergence_iterations), where convergence_iterations is bounded by 4 * |BB| but is typically 2--3. For a kernel with B barriers removed in sequence, the total work is O(B * F * C) where F is the per-iteration cost of the dataflow and C is the convergence bound.

In practice, CUDA kernels rarely have more than 10--20 barriers, and cascading removals are uncommon (typically 0--3 restarts), so the theoretical quadratic cost is not a bottleneck.

## Relationship to `basic-dbe` and `branch-dist`

CICC contains three passes that eliminate dead synchronization barriers. They differ in scope, cost, and the cases they handle:

| Property | `basic-dbe` | `branch-dist` | Dead Sync Elimination |
|---|---|---|---|
| Pass name | `basic-dbe` | `branch-dist` | (unnamed, called from module pass) |
| Entry point | `llvm::BasicDeadBarrierEliminationPass` | `sub_1C47810` | `sub_2C84BA0` |
| Registration | New PM slot 376 | New PM slot (function pass) | Module-level caller |
| Scope | Single BB / local | Function-level with CFG propagation | Function-level with full restart |
| Dataflow | None (pattern match) | Fixed-point, 13 rb-tree maps | Fixed-point, 12 rb-tree maps |
| Restart on removal | No | Yes (goto LABEL_2) | Yes (goto LABEL_2) |
| Binary size | Small (ctor_261) | 63KB core + helpers | 96KB core + helpers |
| Knobs | `basic-dbe` | 10 knobs (ctor_525) | None known (controlled by caller) |

`basic-dbe` handles trivially dead barriers detectable without dataflow analysis -- cases where the barrier is immediately adjacent to another barrier, or where the enclosing block contains no memory operations at all. It runs in the standard function pass pipeline and is cheap.

`branch-dist` performs full CFG propagation with 13 red-black tree maps and restart-on-removal, but it uses NVVM IR opcodes (0x36/0x37/0x3A/0x3B/0x4E) rather than the generic LLVM IR opcodes (61/62/65/66/85) used by the full engine. It also has its own address space filtering logic and 10 configurable knobs.

The full dead synchronization elimination engine (`sub_2C84BA0`) is the most aggressive of the three. It uses the LLVM IR opcode set, applies the element-size gate for loads/stores, and handles the special intrinsic IDs 8260--8262. It runs separately from the New PM function pass pipeline, invoked from module-level callers `sub_2C88020` and `sub_2C883F0`.

## Configuration

No dedicated knobs have been identified for the full engine at `sub_2C84BA0`. Its behavior is controlled entirely by its callers (`sub_2C88020`, `sub_2C883F0`), which determine when and whether the engine runs. This is in contrast to `branch-dist`, which has 10 knobs, and `basic-dbe`, which has at least an enable flag.

The diagnostic output is gated by an internal condition in the caller, not by a standalone dump knob.

## Diagnostic Strings

```
" Removed dead synch: "
"Read above: "
", Write above: "
", Read below: "
", Write below: "
" in function "
"dbg"
```

The complete diagnostic message, assembled from these fragments:

```
 Removed dead synch: [filename:line] in function <name>
Read above: 0, Write above: 0, Read below: 1, Write below: 1
```

The numeric values are the boolean (0/1) access flags for each category. When the pass removes a barrier, the diagnostic shows exactly why it was safe: which of the four access categories was absent.

## Function Map

| Function | Address | Size | Role |
|---|---|---|---|
| -- | `sub_2C84BA0` | 13 KB native (3,400 lines decomp) | Main engine: 5-phase algorithm |
| -- | `sub_2C83D20` | small | `isSyncBarrier` predicate |
| -- | `sub_2C83AE0` | small | `classifyMemoryAccess` (read/write classification) |
| -- | `sub_2C84640` | medium | Per-BB analysis (bidirectional, direction parameter) |
| -- | `sub_2C84590` | small | Red-black tree insert (forward/backward maps) |
| -- | `sub_2C84AF0` | small | Red-black tree insert (bridge maps, different node type) |
| -- | `sub_2C84080` | small | Map lookup / convergence check helper |
| -- | `sub_2C83F20` | small | Map initialization / clear helper |
| -- | `sub_2C83D50` | small | Map destructor / cleanup |
| -- | `sub_BD3660` | small | `hasOneUse` -- used for intrinsic IDs 8260--8262 special case |
| -- | `sub_CEA1A0` | small | Barrier intrinsic ID confirmation |
| -- | `sub_B49E00` | small | `isSharedMemoryAccess` -- CUDA address space check |
| -- | `sub_B43D60` | small | `Instruction::eraseFromParent` -- barrier deletion |
| -- | `sub_B46E30` | small | `getNumSuccessors` -- CFG successor count |
| -- | `sub_B46EC0` | small | `getSuccessor(i)` -- i-th successor retrieval |
| -- | `sub_CB6200` | small | `raw_ostream::write` -- diagnostic string output |
| -- | `sub_B91420` | small | Debug location extraction (filename/line) |
| -- | `sub_B91F50` | small | Debug info accessor |
| -- | `sub_BD5D20` | small | Type/value accessor |
| -- | `sub_22409D0` | small | IR utility (instruction manipulation) |
| -- | `sub_CB59D0` | small | `raw_ostream` integer write |
| -- | `sub_CB59F0` | small | `raw_ostream` integer write (variant) |
| -- | `sub_2C88020` | -- | Caller: module-level pass invoking the engine |
| -- | `sub_2C883F0` | -- | Caller: module-level pass invoking the engine (variant) |

## Common Pitfalls

These are mistakes a reimplementor is likely to make when building an equivalent dead synchronization elimination engine.

**1. Removing a barrier that protects a cross-thread shared memory hazard invisible to single-thread analysis.** The most dangerous mistake is treating the analysis as a single-thread dataflow problem. The pass classifies memory accesses as read/write *per thread*, but the barrier's purpose is to order accesses *across threads*. If thread A writes to `smem[tid]` above the barrier and thread B reads `smem[tid-1]` below it, a single-thread view sees no RAW hazard (different addresses). The correct analysis must conservatively assume that any shared memory write above and any shared memory read below constitutes a hazard -- the pass uses boolean flags (not address tracking) precisely because aliasing across threads is unknowable at compile time. A reimplementation that attempts to be "smarter" by tracking addresses will remove barriers that are needed.

**2. Not restarting the full analysis after each barrier removal.** When a barrier is deleted, the two regions it separated merge into one. This merged region may expose an adjacent barrier as dead (it no longer has memory accesses on one side). A reimplementation that removes all identified dead barriers in a single pass and then stops will miss these cascading redundancies. The restart is mandatory: the pass deliberately uses a goto back to Phase 3 after each removal, re-analyzing the entire function from scratch.

**3. Incorrectly classifying call instructions as non-memory-accessing.** The access classifier (`sub_2C83AE0`) must recursively analyze callees to determine if they access shared/global memory. A reimplementation that conservatively marks all calls as read+write will be correct but will retain too many barriers (poor optimization). Conversely, one that ignores calls entirely will remove barriers protecting memory accesses hidden inside called functions. The correct behavior checks the `isSharedMemoryAccess` predicate on the callee and falls back to read+write if the callee is opaque.

**4. Treating `__syncthreads_count/and/or` (IDs 8260--8262) the same as plain `__syncthreads`.** These barrier variants return a value (lane participation count/and/or). Even when the barrier is dead from a memory-ordering perspective, the return value may be used as data by the program. The pass applies a special `hasOneUse` check for these IDs. A reimplementation that blindly removes them when the dataflow says "no hazard" will break programs that depend on the return value for algorithmic purposes.

**5. Applying the element-size gate too aggressively.** The pass filters out loads/stores of types narrower than 512 bits (`> 0x1FF`), assuming they are register-promoted scalars. A reimplementation that raises this threshold (e.g., to 1024 bits) will miss legitimate memory operations that should keep a barrier alive. Conversely, lowering it to 0 will make the analysis overly conservative, retaining dead barriers for trivial register operations.

## Test This

The following kernel contains consecutive `__syncthreads()` barriers with no shared memory accesses between them. The dead synchronization elimination pass should remove the redundant barriers.

```cuda
__global__ void dead_sync_test(float* out, int n) {
    __shared__ float smem[256];

    smem[threadIdx.x] = (float)threadIdx.x;
    __syncthreads();    // barrier 1: needed (write above, read below)

    float val = smem[threadIdx.x ^ 1];
    __syncthreads();    // barrier 2: dead -- no smem access between barrier 1 and 2's "below"

    __syncthreads();    // barrier 3: consecutive with barrier 2 -- trivially dead

    out[threadIdx.x] = val;
}
```

**What to look for in PTX:**
- Count the number of `bar.sync 0;` instructions. The kernel has three `__syncthreads()` calls in source, but only **one** should survive: barrier 1 (which orders the write to `smem` against the read from `smem[tid^1]`). Barriers 2 and 3 have no shared memory hazard to protect.
- The diagnostic `"Removed dead synch:"` (visible with internal dump flags) shows the per-category access flags that justified removal: `Read above: 0, Write above: 0` means no memory accesses reach the barrier from above.
- To verify the pass preserves necessary barriers, move the `float val = smem[...]` read to between barriers 2 and 3. Now barrier 2 orders the write against this read and must survive -- expect two `bar.sync` instructions.
- The cascading restart behavior is observable with 5 consecutive `__syncthreads()` with no memory between them. The pass removes one, restarts the analysis, removes the next, and repeats until only one remains.

## Reimplementation Checklist

1. **Barrier identification predicate.** Implement the five-condition conjunction: opcode == 85 (internal call), non-null callee, byte[0] == 0 (intrinsic flag), scope match (callee.field[24] == inst.field[80]), convergent attribute (bit 0x20 at byte+33), and barrier intrinsic ID confirmation.
2. **Memory access classifier.** Classify every non-barrier instruction as read/write/both/neither based on opcode (store=0x3D, load=0x3E, atomic=0x41, cmpxchg=0x42, call=0x55), with the element-size gate (>511 bits) for loads/stores and recursive analysis for call instructions including shared-memory-access checks.
3. **Bidirectional fixed-point dataflow.** Maintain eight red-black tree maps (forward ReadAbove/WriteAbove/ReadBelow/WriteBelow per BB, backward same) populated by scanning each BB in both directions, propagating from successors (forward) and predecessors (backward), iterating until no boolean flips from 0 to 1.
4. **Bridge map construction.** After dataflow convergence, populate four bridge maps keyed by barrier instruction pointer, representing the combined read/write access sets crossing each specific barrier boundary.
5. **Elimination decision logic.** A barrier is dead if: (ReadAbove==0 AND WriteAbove==0), OR (ReadBelow==0 AND WriteBelow==0), OR (ReadAbove==0 AND WriteBelow==0), OR (WriteAbove==0 AND ReadBelow==0). Handle the special case for intrinsic IDs 8260--8262 (`__syncthreads_count/and/or`) where single-use return values allow additional removal.
6. **Complete restart after removal.** After each barrier deletion, restart the entire dataflow analysis from scratch to handle cascading redundancies where removing one barrier makes adjacent barriers dead.

## Cross-References

- [Dead Barrier Elimination](./dead-barrier-elim.md) -- overview page covering both `basic-dbe` and this engine
- [Branch Distribution](./branch-distribution.md) -- the other full dead-sync pass using NVVM IR opcodes
- [NVIDIA Custom Passes: Inventory](./index.md) -- registry entry for Dead Synchronization Elimination
- [LLVM Optimizer: Pipeline](../pipeline/optimizer.md) -- pipeline context and Phase I/II interaction
