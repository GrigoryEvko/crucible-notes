# Synchronization & Barriers

> *All addresses in this page apply to ptxas v13.0.88 (CUDA 13.0). Other versions will differ.*

The ptxas synchronization pipeline manages the insertion, optimization, and expansion of all GPU synchronization and barrier instructions. Eight phases span the full compilation pipeline, from early memory-ordering fence insertion through post-scheduling dependency barrier fixup. These phases collectively translate the PTX memory model into the hardware synchronization primitives required by each SM architecture: thread block barriers (`BAR`), memory barriers (`MEMBAR`), dependency barriers (`DEPBAR`), warp-level synchronization (`WARPSYNC`/`BSYNC`/`BSSY`), and asynchronous barriers (`MBARRIER`).

| | |
|---|---|
| **Phases** | 25, 26, 42, 71, 72, 99, 100, 114 |
| **Categories** | Lowering (25, 42, 72), Optimization (26, 71), Scheduling (99, 100, 114) |
| **Pipeline span** | Phase 25 (early optimization) through phase 114 (post-scheduling) |
| **Key opcodes** | `BAR` (opcode 61), `MEMBAR` (opcode 111), `DEPBAR`, `BSYNC`, `BSSY`, `WARPSYNC`, `MBARRIER.*`. Note: the code uses opcode 130 (`HSET2` in the ROT13 name table) as an internal marker for barrier/sync instructions in the Ori IR. |
| **Architecture gates** | Phases 100, 114 dispatch through architecture vtable; phase 42 dispatches through backend vtable at `ctx+1584` offset `0x168` |
| **Related EIATTR** | `EIATTR_SYNC_STACK`, `EIATTR_NUM_BARRIERS`, `EIATTR_NUM_MBARRIERS`, `EIATTR_MBARRIER_INSTR_OFFSETS`, `EIATTR_GEN_ERRBAR_AT_EXIT`, `EIATTR_SW_WAR_MEMBAR_SYS_INSTR_OFFSETS` |
| **CLI options** | `--assume-extern-functions-do-not-sync`, `--no-membermask-overlap`, `--print-potentially-overlapping-membermasks` |
| **Knobs** | `DisableErrbarAfterMembar`, knob 487 (iteration gate), knob 358 (sync mode), knob 472 (barrier liveness) |

## GPU Synchronization Model

NVIDIA GPUs provide four distinct synchronization mechanisms, each operating at a different scope and addressing different hazards.

### Thread Block Barriers (BAR)

Thread block barriers synchronize all threads within a cooperative thread array (CTA). The hardware provides 16 named barriers (indices 0--15), each tracking participation counts. PTX exposes these as:

- `bar.sync N` -- block until all threads in the CTA arrive at barrier N
- `bar.red.{and,or,popc} N` -- barrier with warp-level reduction
- `bar.arrive N` -- signal arrival without blocking
- `barrier.cta.{sync,arrive,red}` -- PTX 8.0+ cluster-aware variants

In SASS, these map to the `BAR` instruction family (opcode 61 in the ROT13 name table). The Ori IR uses opcode 130 (`HSET2` in the ROT13 name table) as an internal barrier/sync marker. The `EIATTR_NUM_BARRIERS` metadata records the maximum barrier index used, which the hardware uses to partition the convergence barrier file.

```text
PTX:     bar.sync 0;
SASS:    BAR.SYNC 0x0;
         // stalls warp until all CTASize threads arrive at barrier 0
```

### Memory Barriers (MEMBAR)

Memory barriers enforce ordering of memory operations across different visibility scopes:

- `membar.cta` -- visible to threads in the same CTA
- `membar.gpu` -- visible to threads on the same GPU device
- `membar.sys` -- visible to all agents (including host CPU and peer GPUs)

Additionally, `fence.proxy` instructions enforce ordering between different memory proxy domains (generic, texture, surface, constant).

The `EIATTR_SW_WAR_MEMBAR_SYS_INSTR_OFFSETS` records the byte offsets of `membar.sys` instructions for driver-level workaround injection.

### Dependency Barriers (DEPBAR / Scoreboards)

Dependency barriers are the micro-architectural mechanism for tracking instruction-level data hazards. Each SM provides 6 scoreboard entries (barriers 0--5) that track completion of long-latency operations. SASS instructions encode a 23-bit control word containing:

- **Stall count** (4 bits): cycles to wait before issuing the next instruction
- **Yield flag** (1 bit): hint to give up the scheduling quantum
- **Write barrier** (3 bits): scoreboard index to set on result writeback
- **Read barrier mask** (6 bits): scoreboard entries to wait for before reading
- **Wait barrier mask** (6 bits): scoreboard entries to clear/release

`DEPBAR` is the explicit dependency barrier instruction that waits for a specific set of scoreboard entries. Scoreboards are assigned by phase 115 (`AdvancedScoreboardsAndOpexes`) and phase 116 (`ProcessO0WaitsAndSBs`); the sync passes described here prepare the IR for scoreboard generation but do not assign scoreboards directly.

### Warp-Level Synchronization

Warp-level sync instructions operate within a single warp (32 threads):

- `WARPSYNC mask` -- synchronizes threads identified by the lane mask (sm70+)
- `BSSY B, target` -- pushes a synchronization barrier for convergence
- `BSYNC B` -- pops and waits at the convergence barrier

The `BSSY`/`BSYNC` mechanism replaces the pre-Volta implicit reconvergence stack. The compiler must insert these pairs explicitly at divergence/reconvergence points. `EIATTR_SYNC_STACK` records metadata about the convergence barrier stack depth.

### Asynchronous Barriers (MBARRIER)

Introduced in sm90 (Hopper), `MBARRIER` provides hardware-accelerated asynchronous barriers in shared memory. These support non-blocking arrival, expected transaction count tracking, and parity-based phase completion -- critical for async copy (`cp.async.bulk`) and TMA (Tensor Memory Accelerator) operations.

MBARRIER operations in PTX:

| PTX instruction | Purpose |
|---|---|
| `mbarrier.init` | Initialize barrier object in shared memory |
| `mbarrier.arrive` | Signal arrival (non-blocking) |
| `mbarrier.arrive_drop` | Arrive and decrement expected count |
| `mbarrier.arrive.expect_tx` | Arrive with expected transaction byte count |
| `mbarrier.test_wait` | Test if barrier phase is complete |
| `mbarrier.try_wait` | Wait with timeout |
| `mbarrier.try_wait.parity` | Phase-parity-based wait |
| `mbarrier.pending_count` | Query remaining arrivals |
| `mbarrier.inval` | Invalidate barrier |
| `mbarrier.complete_tx` | Mark transaction bytes as complete |

The `EIATTR_NUM_MBARRIERS` and `EIATTR_MBARRIER_INSTR_OFFSETS` metadata inform the runtime about barrier allocation and instruction locations for driver patching.

---

## Phase 25 -- StageAndFence

| | |
|---|---|
| **Phase name** | `StageAndFence` |
| **Category** | Lowering |
| **Execute wrapper** | `sub_C5FBC0` (34 bytes) |
| **Implementation** | `sub_1392E30` (166 bytes) |
| **Core logic** | `sub_1390B30` (8,956 bytes, 97 callees) |
| **Setup** | `sub_1389AF0` (3,049 bytes) |
| **Teardown** | `sub_138A6E0` (3,408 bytes) |
| **Gating** | Requires `opt_level > 1` AND `context+1368 bit 0` AND `context+1397 bits[6:7] != 0x40`; additionally guarded by `"LoopUnrolling"` disable check and knob 487 |
| **Total code** | ~16 KB across `0x1389AF0`--`0x1393340` |

### Purpose

StageAndFence inserts memory fence and staging instructions to enforce coherence ordering after loop unrolling. When loop unrolling replicates memory operations, the replicated loads and stores may violate the memory model if they cross a synchronization boundary that was inside the original loop body. This pass re-establishes correctness by inserting fence operations at the boundaries of unrolled iterations.

### Execution Flow

```c
sub_1392E30(compilation_unit):
    // Guard: must have loops and bit flags set
    if !(context+1368 bit 0) or (context+1397 & 0xC0) == 0x40:
        return

    // Check if "LoopUnrolling" pass is disabled
    IsPassDisabled(knob_state, "LoopUnrolling", &disabled)
    if disabled: return
    if opt_level <= 2: return

    // Check knob 487
    if !CheckKnob(knob_state, 487, 1): return

    // Core execution
    sub_1389AF0(state, compilation_unit)   // allocate working structures
    sub_1390B30(state)                     // main fence insertion pass
    sub_138A6E0(state)                     // cleanup
```

### Main Pass -- `sub_1390B30`

The main pass (8,956 bytes, 97 callees) iterates over loops in reverse postorder and decides, for each unrolled loop, whether to insert a fence instruction and at what strength. The function signature passes FP parameters (`double a2, double a3, __m128d a4`) that propagate latency heuristics from the caller into the profitability evaluator.

**Initialization.** The function reads knob 437 to determine the fence mode: when disabled, `fence_enabled` is false and every loop defaults to "skip". When enabled, the knob value (0 or 2) sets the initial strength level. Knob 429 provides an optional string filter of the form `"-N-"` or `"+N-"` that selectively enables or disables fence insertion for individual loop IDs. After knob reads, `sub_781F80` refreshes basic block metadata and `sub_1387660` prepares loop analysis state.

**Per-loop iteration.** The main loop walks the loop array from `ctx+512` in reverse order (highest loop index first). For each loop body at `ctx+296[loop_id]`:

1. **Induction variable analysis.** `sub_13858C0` locates the IV. If the IV instruction has opcode 95 with subop 5 and specific operand bits, the loop is tagged for special coherence handling; otherwise rejection code 13 is recorded via `sub_7F5D20` (which indexes the debug string table at `0x21D1EA0`).

2. **Cross-iteration coherence check.** The architecture backend vtable (slot at `ctx+1784`) is queried; if the loop body already has sufficient coherence guarantees (`sub_7E5120` returns true), rejection code 11 is recorded and the loop is skipped. For high-iteration loops (count > 999) with a predecessor count > 0, the ratio `iter_count / pred_count > 3` further gates insertion.

3. **Budget computation.** The remaining instruction budget determines whether the loop has enough ILP to tolerate the coherence hazard without an explicit fence:
   ```c
   budget_scale = QueryKnobDouble(knob 900, 0.5)  // 0x3FE0000000000000
   budget = total_insns + head_insns - overhead - floor(budget_scale * iter_count)
   ```
   If `10 * fence_cost < budget`, the fence is skipped (rejection code 7).

4. **Fence strength selection.** A multi-way decision tree assigns strength 0/1/2/4 based on: whether `fence_enabled` is true and the loop spans multiple blocks; the trip count (`trip_count > 0`) and budget threshold (`budget <= 49`); knob 429 override when present; for cross-block loops, the secondary cost formula `cost = min(5 * loop_depth + 22, 100)` compared against budget; knob 903 forcing `1 << state+228` (power-of-two mode); and strength 4 as a fallback for budget-passing loops without cross-block operands.

5. **Trip count extraction.** `sub_1385950` (IV analysis), `sub_1385E90` (trip count bound), and `sub_1385CC0` (constant IV detection) compute the iteration count. `sub_1383200` returns the comparison mode (1=LE, 2=LT, 3=exact). The iteration count must be positive and representable as `int32`. Rejection codes 14--23 cover failure modes (non-unit stride, wrap-around, non-integral bounds).

6. **Profitability evaluation.** When knob 892 is enabled and both source and target operands are fence-eligible (`sub_7D6780` confirms), `sub_1383620` runs the full unroll profitability evaluator with the fence strength, target block ID, and FP latency parameters. Success increments the backend fence counter at `ctx+1584+348`.

7. **Fence insertion.** `sub_1387C30` performs the insertion, taking the loop descriptor, unroll factor, fence strength, and FP latency parameters. For loops with a non-zero remainder (`trip_count % unroll_factor`), `sub_931920` clones the loop body and `sub_932E40` duplicates instructions across copies. `sub_13880F0` updates CFG successor edges post-insertion.

**Finalization.** After all loops, the unfenced loop count is written to `ctx+1584+352`. If any fences were inserted, `sub_A0F020` (DAG scheduler) rebuilds dependencies incorporating the new fence instructions, then `sub_7B52B0` updates the backend synchronization state.

---

## Phase 26 -- OriRemoveRedundantBarriers

| | |
|---|---|
| **Phase name** | `OriRemoveRedundantBarriers` |
| **Category** | Optimization |
| **Execute wrapper** | `sub_C60BD0` (334 bytes) |
| **Implementation** | `sub_790A40` (2,288 bytes, 33 callees) |
| **Helper: post-RA sched** | `sub_790020` (1,200 bytes) |
| **Helper: pre-RA opt** | `sub_7904D0` (1,381 bytes) |
| **Helper: barrier opt** | `sub_7923A0` (2,344 bytes, 30 callees) |
| **Helper: barrier pass** | `sub_792CD0` (1,360 bytes, 25 callees) |
| **Gating** | Multi-function dispatch: only runs when `sub_7DDB50(ctx) > 1` (i.e., the compilation unit contains more than one function) |
| **Total code** | ~10 KB across `0x790020`--`0x793220` |

### Purpose

OriRemoveRedundantBarriers performs dataflow-driven elimination of provably redundant barrier instructions. When the compiler can prove that all threads in a warp (or CTA) must have already passed through a dominating synchronization point, subsequent barriers to the same scope are redundant and can be removed. This reduces the synchronization overhead without changing program semantics.

### Execution Flow

The execute wrapper `sub_C60BD0` is a multi-function dispatch pattern: when a compilation unit contains multiple functions, it creates two reference-counted list objects, stores the current phase chain pointer, and calls `sub_790A40` for cross-function barrier analysis. For single-function units, it returns directly.

```c
sub_C60BD0(phase, compilation_unit):
    func_count = sub_7DDB50(compilation_unit)
    if func_count <= 1: return

    // Create two ref-counted analysis lists
    list1 = pool_alloc(24)
    list1->refcount = 1
    list2 = pool_alloc(24)
    list2->refcount = 1

    // Store current phase chain
    saved_chain = compilation_unit->field_88

    // Run multi-function barrier analysis
    sub_790A40(&compilation_unit)

    // Release ref-counted lists
    release(list1)
    release(list2)
```

### Main Analysis -- `sub_790A40`

The main analysis function (2,288 bytes) operates through several stages:

1. **Mode selection**: Queries knob 358 (sync mode) through the knob container at `ctx+1664`. Three modes exist:
   - Mode 0: no barrier removal (return immediately via `sub_756F10`)
   - Mode 1: conservative removal (calls `sub_790020`)
   - Mode 2: aggressive removal (calls `sub_790020` with flag)
   - Mode >= 3: full multi-function analysis

2. **Graph construction** (`sub_7E6090`): Builds an instruction-level dependency graph with 32-bit flags. Called with `(ctx, 0, 0, 0, 0)`.

3. **Liveness refresh** (`sub_781F80`): Refreshes the basic block liveness information with mode parameter 1 (compute barrier liveness).

4. **Dependency tracking** (`sub_A10160`): Sets up dependency tracking data structures.

5. **Block iteration** (`sub_769300`, `sub_752AB0`): Builds block-level analysis structures for the function.

6. **Redundancy analysis**: For each barrier instruction (opcode 130), checks whether the barrier's destination register is live in any successor block. If the barrier result is dead (no thread could observe it before the next dominating barrier), the barrier is eliminated.

7. **Block-level merging** (`sub_75EAE0`, `sub_75E2F0`): Merges barriers at block boundaries where adjacent blocks have compatible barrier scopes.

### Dominance-based redundancy proof -- `sub_1245740`

The per-operand proof (`sub_1245740`, 380 bytes) decides whether operand `a4` in instruction `a3` is dominated by a prior barrier in `a2`. Returns `true` = redundant (safe to eliminate). Arguments: `(ctx, dom_insn, insn, operand_idx)`.

```c
can_eliminate_barrier_operand(ctx, dom_insn, insn, op_idx):
    word = insn->operands[op_idx]                       // insn + 84 + 8*idx
    if (word >> 28) & 7 != 1: return true               // non-register operand, trivially safe
    reg = ctx->reg_table[word & 0xFFFFFF]               // *(ctx+88)[index]

    // Register class gate: only classes 2/3 (barrier regs) need scope analysis
    if (reg->class - 2) > 1: goto block_compare         // class != 2,3: skip to block-ID test
    flags = reg->field_48
    if flags & 0x4000000:  goto block_compare           // volatile -- skip scope analysis
    if flags & 0x10000000: return false                  // pinned -- never eliminate
    if !(ctx->byte_1370 & 0x20): return false            // global analysis disabled

    // Loop nesting guard: both blocks must be in the same reducible loop nest
    bb_insn = ctx->bb_table[insn->block_id]
    bb_dom  = ctx->bb_table[dom_insn->block_id]
    if (bb_insn->byte_283 | bb_dom->byte_283) & 0x20: return false  // irreducible region
    if bb_insn->loop_depth < 0: return false
    if bb_insn->loop_depth != bb_dom->loop_depth: return false

  block_compare:
    dom_blk  = dom_insn->block_id
    insn_blk = insn->block_id
    if dom_blk == insn_blk:                             // same-block fast path
        tied = reg->field_56                            // tied definition register
        if tied:
            if dom_blk != tied->block_id and reg->use_count == 1: return true
            if !uniform_warp_check(ctx, insn, dom_insn, tied): return true
            if ctx->field_1792 and (ctx->byte_1384 & 0x40): return false
            dom_blk = dom_insn->block_id; insn_blk = insn->block_id
        is_entry = ctx->byte_908 and (ctx->field_904 == dom_blk)
        return dominance_verify(ctx, reg, dom_blk, insn_blk, is_entry)
    // Cross-block: if single-def or no tied-def or tied-def not in insn's block
    if (reg->byte_50 & 1) or !reg->field_56 or insn_blk != reg->field_56->block_id:
        return dominance_verify(ctx, reg, dom_blk, insn_blk, false)
    return true                                         // tied def co-located => dominated
```

### Dominance verification -- `sub_1244EC0`

The structural verifier (`sub_1244EC0`, 490 bytes) takes `(ctx, reg, dom_block, insn_block, is_entry)`:

- **Pre-colored registers** (class 41/42): always safe (return `true` immediately).
- **Uniform registers** (class 39): safe only if both blocks' enclosing functions (via `bb->field_164` indexed through `ctx->field_368`) are identical, or neither function has a convergence point (`bb->field_288 == 0` for both).
- **Tied-definition check**: verifies the def's opcode via `sub_7DEB90` (130 = barrier; 272/273 = memory barrier variants, requiring both source operands to also pass). Checks `ctx->byte_1368` bits: `& 0x10` for loop-awareness, `& 0x04` for cross-function tolerance. When cross-function is set and the def block has convergence depth (`field_148 != 0`), verifies bidirectional dominance via `sub_76ABE0` (bitset test against dominator bitmap at `bb->field_176`).
- **Same-function check** (`ctx->byte_1368 & 0x02`): safe if both blocks share function index (`bb->field_164`) and arch mode (`ctx->field_896`) is 4 or 5; otherwise requires function index 0 with use-count 1.

### Block-level merging -- `sub_75EAE0` / `sub_75E2F0`

`sub_75EAE0` (240 bytes) drains each basic block's successor list (`bb+128`). For each successor instruction it scans operands for register-type barrier references (`(word >> 28) & 7 == 1`, high bit set, not yet merged) whose register index matches the target barrier register (`a3+8`), then calls `sub_75E2F0`.

`sub_75E2F0` (1,700 bytes) hashes the `(block_id, operand_index, instruction_ptr)` triple via FNV-1a into a merge table (`sub_75DFC0`), allocates a 176-byte record, and populates `[min, max]` program-point intervals: offsets +12/+16 and +20/+24 for barrier-definition operands (opcode `& 0xFFFFCFFF == 137`), +28/+32 and +36/+40 for barrier-use operands. Barrier-def operands are collected into a linked list at record+48 via `sub_685940`; non-def barrier operands increment a counter at record+44. Use-def chain successors are enqueued into a circular BFS work queue (`a1+16`, power-of-two capacity at `a1+40`) for cross-block propagation.

---

## Phase 42 -- ExpandMbarrier

| | |
|---|---|
| **Phase name** | `ExpandMbarrier` |
| **Category** | Lowering |
| **Execute wrapper** | `0xC5F110` (6 bytes) |
| **Implementation** | Architecture-dispatch via `*(*(ctx+0x630))->vtable[0x168/8]` |
| **isNoOp** | Always false (`0xC5F130` returns 0) |
| **No opt-level check** | Runs at all optimization levels |

### Purpose

ExpandMbarrier expands `MBARRIER` pseudo-instructions into native barrier instruction sequences. This is critically important for sm90+ (Hopper and later) architectures that use asynchronous barriers for TMA operations, `cp.async.bulk`, and warpgroup-level synchronization.

### Dispatch Mechanism

Unlike most phases that tail-call a fixed function after an optimization level check, ExpandMbarrier performs a direct vtable dispatch:

```asm
mov    rdi, [rsi+0x630]     ; rdi = ctx->arch_backend (offset 1584)
mov    rax, [rdi]            ; rax = arch_backend->vtable
jmp    [rax+0x168]           ; call vtable[45] -- ExpandMbarrier impl
```

The architecture backend at `ctx+1584` provides the actual expansion logic. This design allows each SM generation to define its own mbarrier expansion rules:

- **Pre-sm90**: MBARRIER pseudo-ops do not exist; the phase is effectively a no-op.
- **sm90 (Hopper)**: Expands MBARRIER pseudo-ops into hardware mbarrier instruction sequences using the mbarrier object in shared memory. Handles `mbarrier.init`, `mbarrier.arrive`, `mbarrier.arrive.expect_tx`, `mbarrier.try_wait.parity`, and `mbarrier.inval`.
- **sm100+ (Blackwell)**: Extended mbarrier semantics for `tcgen05.fence`, cluster-level barriers, and async pipeline operations.

### MBARRIER Expansion Patterns

A typical async copy pattern in the Ori IR and its expansion:

```text
Before expansion (pseudo-ops):
    MBARRIER_INIT  %mbar, count
    MBARRIER_ARRIVE_EXPECT_TX  %mbar, bytes
    CP.ASYNC.BULK.TENSOR  dst, src, %mbar
    MBARRIER_TRY_WAIT_PARITY  %mbar, parity, pred

After expansion (native):
    MBARRIER.INIT  [smem_addr], count
    MBARRIER.ARRIVE.EXPECT_TX  [smem_addr], bytes
    CP.ASYNC.BULK.TENSOR  [dst], [src], [smem_addr]
    MBARRIER.TRY_WAIT.PARITY  pred, [smem_addr], parity
```

The expansion resolves shared memory addresses for the mbarrier objects, handles the naming of `__nv_reservedSMEM_tmem_allocation_pipeline_mbarrier` and `__nv_reservedSMEM_tmem_allocation_pipeline_mbarrier_parity` reserved shared memory regions, and inserts any required `fence.proxy` operations for proxy domain coherence.

---

## Phase 71 -- OptimizeSyncInstructions

| | |
|---|---|
| **Phase name** | `OptimizeSyncInstructions` |
| **Category** | Optimization |
| **Execute wrapper** | `sub_C60080` (34 bytes) |
| **Implementation** | `sub_90A340` (1,670 bytes, 21 callees) |
| **Sync predicate** | `sub_18F6930` (185 bytes) -- determines if sync optimization should run |
| **Gating** | Requires `opt_level > 2`; additionally checks knob 487, architecture flags at `context+1368`, and `sub_18F6930` predicate |
| **Pipeline position** | After `OriPropagateVaryingSecond` (70), before `LateExpandSyncInstructions` (72) |

### Purpose

OptimizeSyncInstructions performs redundancy elimination and simplification of synchronization instructions within the partial-SSA window. It identifies and removes sync instructions that are provably unnecessary based on the data flow and the GPU memory model, and simplifies complex sync patterns into cheaper equivalents.

### Gating Logic

The pass has elaborate gating controlled by `sub_18F6930`, which evaluates:

```c
sub_18F6930(ctx, mode):
    // Check architecture-specific sync flags
    flags = *(ctx+1398)
    if (flags & 0x18) != 0:
        return (flags & 0x18) == 8   // specific arch config

    // Check whether SM requires explicit sync
    if !(*(ctx+1412) bit 7) or *(ctx+1584)->field_372 <= 28673:
        return true

    // Functions with <= 4 registers always need sync
    if *(ctx+1704) <= 4:
        return true

    // Mode-specific knob checks at offsets 51120/51192
    ...
```

The value 28673 corresponds to sm70/sm72/sm73/sm75 architecture IDs. The predicate returns true (optimize) for architectures that have explicit synchronization requirements (Volta and later), and false for older architectures where synchronization is implicit.

### Main Algorithm -- `sub_90A340`

```c
sub_90A340(ctx):
    if opt_level <= 2: return
    if !CheckKnob(ctx+1664, 487, 1): return

    // Determine sync optimization mode
    has_uniform_regs = (ctx+1412 bit 7) && !(ctx+1368 bit 4)
    arch_data = *(*(ctx+1664)+72)
    sync_mode = *(arch_data + 15480)
    if sync_mode == 1: mode = *(arch_data + 15488)

    // Main path: combined sync + barrier optimization
    if (ctx+1368 flags 0x20000001 all set) && (ctx+1377 bit 6) && !mode:
        need_expand = sub_18F6930(ctx, 0)
        sub_781F80(ctx, 1)               // refresh liveness

        if !need_expand && !has_uniform_regs:
            sub_7E6090(ctx, 0, 0, 0, 32) // build dep graph, 32-bit mode
            goto optimize
    else:
        need_expand = sub_18F6930(ctx, 0)
        if !has_uniform_regs && !need_expand: return
        sub_781F80(ctx, 1)

    // Barrier liveness computation
    sub_775010(ctx)
    sub_7E6090(ctx, 0, 0, 0, 32)

    // Walk instruction list, find opcode 130 (HSET2 in ROT13; internal barrier/sync marker)
    for instr = ctx->first_instr; instr; instr = instr->next:
        if instr->opcode != 130: continue

        // Extract operand, check register type
        operand = instr->field_84
        if operand_type(operand) != 1: continue

        reg = register_table[operand & 0xFFFFFF]
        if !check_liveness(reg): continue

        // For uniform-register-aware path:
        if has_uniform_regs:
            if (instr->field_91 & 1): continue  // skip if flagged
            if reg->file != 6: continue          // must be barrier reg
            if reg->use_count <= 1: continue
            // Check all uses via use-def chain...
            try_merge_barriers(ctx, instr)

        // Standard redundancy elimination
        try_eliminate_redundant_sync(ctx, instr)

    cleanup_lists()
```

### Barrier merge -- inner loop of `sub_90A340`

The merge subroutine (inlined at offset +348 in `sub_90A340`) fires only on the `has_uniform_regs` path. For each opcode-130 instruction whose barrier register has `reg->file == 6`, `use_count > 1`, the linked-list at `reg+112` is not null, and `reg+48 bit 5` is clear, it walks every pair `(use_A, use_B)` from the use-chain:

1. **Identity / same-block filter** -- skip if `use_A == use_B`, or if `use_A->block_id == use_B->block_id`.
2. **Opcode gate** -- `use_A` must also be opcode 130 (barrier).
3. **Dominance check** -- `use_B`'s block must dominate `use_A`'s block, verified via the dominator bitmap at `block+176`:  `(1 << block_A->field_144) & block_B->bitmap[block_A->field_144 >> 5]`.
4. **Operand-exact match** -- both instructions must agree on opcode, modifier word, and every `(operand_hi, operand_lo)` pair from index `num_operands-1` down to 0.
5. **Interference check** -- no third use in the same register's use-chain has a program-point between `use_A` and `use_B` (checked via `block->field_144` ordering).
6. **Final dominance proof** -- `sub_1245740(ctx, use_B, use_A, 1)` confirms the domination is safe across convergence boundaries.

On success the dominated instruction `use_B` is deleted via `sub_9253C0` (unlink from IR list, update successor chain). If `use_B`'s last source operand is a barrier-register reference (`(operand >> 28) & 7 == 1`), that register's use-count is decremented. The barrier register's own use-count (`reg+24`) is decremented; when it reaches 1, `reg+56` is updated to point to the sole remaining use.

### Redundant sync elimination -- `sub_8F31F0`

`sub_8F31F0(ctx, instr, mode)` (560 bytes, 8 callees) targets opcode-130 instructions whose barrier register has `use_count > 1` and bit 5 of `reg+48` clear. It operates only when `sub_91E030(instr) != 1` (instruction has more than one predecessor/successor in the linked list) and `ctx+1370 bit 2` is set.

When `mode == 1`, a preliminary check `sub_7DEB90(instr+92, ctx)` verifies the source operand. Then the use-chain at `reg+112` is walked:

1. **Operand comparison loop** -- for every use `U` in the chain, `U->num_operands` must equal the original instruction's, opcodes must match, modifier words must match, and every operand pair is compared backwards from index `num_operands-1` to 0. The first mismatch aborts.
2. **Dominator walk** -- after the use-chain is exhausted, `sub_BDDCB0` iterates bits in the dominator bitmap (`block+176`), scanning from `block->field_144 + 1` downward. For each set bit, `sub_76ABE0` confirms the dominator block dominates the current use's block. On success, the walk advances to the next use in the chain, repeating the dominator check.
3. **Deletion** -- uses that share the same block as the dominator's first successor (`**(block+0) + 24 == block_id`) are retained; all others are deleted via `sub_9253C0`. After filtering, `reg->use_count` is updated to the survivor count. If zero survivors remain, the pass calls `sub_7E5350` to insert a replacement instruction, then `sub_932720` to remove the original, and clears bit 25 of `reg+48`.

### Sync-pair CSE -- `sub_902F30` / `sub_9034A0`

The second optimization phase (guarded by `v57`, the combined `need_expand | has_uniform_regs` flag) performs hash-based common subexpression elimination on `DEPBAR`/`BAR.SYNC` instruction pairs.

`sub_902F30` (530 bytes) walks each basic block. For opcodes 137 (DEPBAR variants) with modifier in `{7, 13, 14}`, it computes a signature via `sub_8FB680`: the signature hashes each use's `(block_id, operand_index)` with a multiplicative hash `h = (1025 * (val + h)) ^ ((1025 * (val + h)) >> 6)`, and validates that all uses are single-def opcode-130 instructions with no flags in `operand_word & 0x603FFFF`. The 8-byte signature is inserted into an FNV-1a-indexed hash table (init seed `0x811C9DC5`, multiplier `16777619`).

When two DEPBAR instructions produce the same signature, `sub_8FB8D0` confirms structural equivalence by walking both use-chains in parallel: block IDs, source operand indices, and the polarity bit (`operand[24] & 0x4000000`) must all match. On confirmed match, `sub_8FBA60` replaces the pair: it allocates two fresh barrier registers (register file 6), emits a new opcode-130 for each original use in the chain, rewrites both the original and the duplicate DEPBAR to reference the new registers, and sets polarity bits (`0x4000000` / `0x2000000`) to distinguish the arrival direction.

`sub_9034A0` (200 bytes) drives the block-walk: starting from the entry block's first successor, it follows the RPO chain via `block->successor[1] -> block_id`, calling `sub_902F30` per block. When `block->field_148 == block->field_144` (single-entry single-exit region), the per-block hash table is flushed before processing.

---

## Phase 72 -- LateExpandSyncInstructions

| | |
|---|---|
| **Phase name** | `LateExpandSyncInstructions` |
| **Category** | Lowering |
| **Execute wrapper** | `sub_C600B0` (34 bytes) |
| **Implementation** | `sub_1381DA0` (1,517 bytes, 3 callees) |
| **Core driver** | `sub_1381CD0` (206 bytes) |
| **Gating** | Requires `opt_level > 1`; checks `context+1376 bit 5`, `"Predication"` disable flag, and knob 487 with iteration counter |
| **Error diagnostic** | `"ExpandSyncInstLate option is not supported on this architecture."` (via `sub_7EF030`) |
| **Pipeline position** | After `OptimizeSyncInstructions` (71), before `ConvertAllMovPhiToMov` (73) |
| **Gate pass** | Phase 135 (`AdvancedPhaseLateExpandSyncInstructions`) provides an additional architecture hook |

### Purpose

LateExpandSyncInstructions performs the final expansion of synchronization pseudo-instructions into their target-specific SASS instruction sequences. This runs late in the pipeline (phase 72, within the partial-SSA window) so that earlier optimization passes can work with high-level sync pseudo-ops rather than architecture-specific instruction sequences.

### Execution Flow

The entry function shares structural similarity with the [Predication](predication.md) pass entry (`sub_1381DA0`) because both operate within the same address range (`0x1381000`--`0x1382000`) and share infrastructure for walking the instruction list within the partial-SSA window.

```c
sub_1381DA0(ctx):
    if context+1376 bit 5: return      // disabled by phase flag

    // Read expansion mode from knob container
    knob_state = *(ctx+1664)
    mode = *(*(knob_state+72) + 16416)

    if mode == 0:
        limit = (ctx+1419 bit 4) != 0
    elif mode == 1:
        limit = *(*(knob_state+72) + 16424)

    IsPassDisabled(knob_state, "Predication", &disabled)
    if disabled or limit: return

    // Knob 487 iteration gating with counter
    if !CheckKnob487WithCounter(knob_state): return

    // Set up working state
    context+1385 |= 1    // mark expansion active

    // Call core driver
    sub_1381CD0(state)

    context+1385 &= ~1   // clear expansion flag
    cleanup_pools()
```

### Expansion Rules

The pass transforms sync pseudo-instructions according to the target SM:

| Pseudo-instruction | sm70+ expansion | sm90+ expansion |
|---|---|---|
| `SYNC.WARP mask` | `WARPSYNC mask` | `WARPSYNC mask` |
| `SYNC.BLOCK` | `BAR.SYNC 0` | `BAR.SYNC 0` |
| `SYNC.CONVERGE target` | `BSSY B, target` ... `BSYNC B` | `BSSY B, target` ... `BSYNC B` |
| `MBARRIER.WAIT pseudo` | *(not expanded here)* | `MBARRIER.TRY_WAIT.PARITY` loop |
| `ERRBAR` | `BAR.SYNC 15` (error barrier) | Conditional on `DisableErrbarAfterMembar` |

The `ERRBAR` (error barrier) is a compiler-inserted synchronization point placed after `membar.sys` instructions to ensure memory ordering is observable before proceeding. The `DisableErrbarAfterMembar` knob (accessible via the CLI option string at `0x1D04BC0`) controls whether these error barriers are emitted. When set to 1, the compiler omits the error barrier, trading safety for performance.

---

## Phase 99 -- OriDoSyncronization

| | |
|---|---|
| **Phase name** | `OriDoSyncronization` |
| **Category** | Scheduling |
| **Execute wrapper** | `sub_C5FAD0` (34 bytes) |
| **Implementation** | `sub_A0F020` (2,375 bytes, 32 callees) -- DAG scheduler entry |
| **Dependency builder** | `sub_A0D800` (dependency DAG construction) |
| **Per-block processor** | `sub_A06A60` (3,045 bytes, 53 callees) |
| **Uninit reg check** | `sub_A0B5E0` |
| **Gating** | Requires `opt_level > 1` |
| **Pipeline position** | After `BackPropagateVEC2D` (98), before `ApplyPostSyncronizationWars` (100) |
| **Callers of `sub_A0F020`** | 11 sites: `sub_913A30`, `sub_9AEF60` (x2), `sub_C5FA40`/`sub_C5FA70`/`sub_C5FAA0`/`sub_C5FAD0` (4 arch wrappers), `sub_1390B30` (x2), `sub_1395850` (x2) |

### Purpose

OriDoSyncronization is the post-optimization synchronization insertion pass. It runs after all IR-level optimizations are complete and before register allocation, using the scheduling infrastructure to analyze data dependencies and insert the synchronization instructions (`BAR`, `DEPBAR`, `MEMBAR`) required by the GPU memory model for correctness.

Note the intentional misspelling "Syncronization" (missing 'h') -- this is present in the binary's string table and preserved here for fidelity.

### Architecture

OriDoSyncronization reuses the DAG scheduler's infrastructure (`sub_A0F020`) rather than implementing its own analysis. The same function serves as the scheduling entry point in multiple contexts:

- Phase 99 (`OriDoSyncronization`): inserts sync instructions based on dependency analysis
- Phase 25 (`StageAndFence`): inserts fences via `sub_1390B30`
- Multiple architecture-specific scheduling wrappers: `sub_C5FA40`, `sub_C5FA70`, `sub_C5FAA0`

### Execution Flow

```c
sub_A0F020(ctx):
    while true:
        if *(ctx+1648) == 0: break

        // Initialize dependency context
        dep_ctx = pool_alloc(16)
        dep_ctx->refcount = 2
        dep_ctx->parent = ctx->pool

        // Build dependency DAG
        sub_A0D800(ctx, dep_ctx)

        // Process blocks in reverse order
        for each basic_block in reverse(block_list):
            if block->opcode == 8: continue  // skip NOP/exit blocks
            sub_A06A60(ctx, callback, block, flags...)

        // Check for uninitialized register usage
        sub_A0B5E0(ctx, dep_ctx)

        // Diagnostic output if enabled
        sub_7F44D0(ctx)

        // Break or retry based on scheduling result
        ...
```

### Per-Block Synchronization -- `sub_A06A60`

The per-block processor (3,045 bytes, 53 callees) is the core of sync insertion. It takes six parameters beyond the context: a callback (`a2`) invoked per-instruction to emit sync primitives, the basic block (`a3`), and three mode flags (`a4` = emit-mode, `a5` = cross-block-sync, `a6` = predecessor-tracking). The function maintains a liveness bitvector (`live`, aliased to `ctx+832`) that tracks which virtual registers are live at each program point, plus two temporaries (`bv_kill`, `bv_gen`) allocated when uniform registers are present (`ctx+1378 bit 3`).

**Initialization.** Copies the block-entry live set into `live` from `bb+40` via `assign`. If uniform registers are active, allocates `bv_kill` and `bv_gen` sized to `ctx+220 + 1` bits each.

**Opcode dispatch.** The instruction walk scans from `bb+0` (first instruction) to `bb+8` (end sentinel), reading the masked opcode at `instr+72` with bits 12--13 cleared (`& 0xCFFF`). The dispatch tree selects the sync insertion strategy:

| Masked opcode | Mnemonic | Liveness action | Sync primitive selection |
|---|---|---|---|
| 29 (`PMTRIG`) | Control-flow join | Multi-target + last operand is barrier-type: `live = assign(succ_live)`; else `live \|= succ_live` | Propagates to `bb_live` via `copyFrom` |
| 32 (`VABSDIFF4`) | GMMA wait / fence | Looks up fence target via `ctx+368`; if `a5=0` and block unchanged: `live = assign(target+16)`; if changed: `live \|= target+16` | If `a6`: `live = live & ~(target+112)` then `live \|= (target+64) & (target+16)` |
| 42, 53, 55 | `MUFU`/`BREV`/`BMOV_R` | `live \|= bb_extension+40` via `OR=` | If `sub_7DF3A0(instr)` bit 1 set: also propagates to `bb_live` |
| 52 (`AL2P_INDEXED`) | BB boundary pseudo | Saves `bb_ptr`; examines linked successor's opcode (159/32/188) to set `needs_barrier` flag | Deferred barrier: tests successor via vtable+1080 function pointer; on IMMA (188): checks `target+59` for shared-memory flag |
| 93 (`OUT_FINAL`) | Call / tess output | `live = assign(callee_save_set)` from `ctx+296[target]+24`; at O1 walks chained opcode-269 defs, marking each via `setBit(live, phys_id)` | On sm_gen 4--5: `setBit(live, stack_ptr)` via `ctx+88[39]+12` |
| 94 (`LDS`) | Exception entry | `live = clearAll()`; for each handler target in `ctx+616[idx]`: `live \|= succ_live` | Propagates to `bb_live` via `copyFrom` |
| 95 (`STS`) | Barrier / terminator | `live &= succ_live` (AND-merge from `ctx+296[target]+24`) | Propagates merged result to `bb_live` via `copyFrom` |
| 97 (`STG`) | Branch / jump | Tests `ctx+1368 bit 4`: first visit calls `orIfChanged(succ_live, live)` setting `block_changed` flag | Propagates to `bb_live` via `copyFrom` |
| 188 (`IMMA`) | Matrix multiply | `sub_A06950` helper; at O1 walks chained opcode-269 defs with `setBit` | `sub_A06950`: `assign` for IMMA, `OR=` for non-IMMA |
| 190 (`LDGDEPBAR`) | Load-global depbar | `sub_A06950` helper then propagates to `bb_live` | Same as 188 |
| 271 (arrive) | GMMA arrive | `bv_kill = setAll()`, `bv_gen = clearAll()`; per-successor via `sub_923B30` | Three modes: `a5`: `bv_gen \|= succ+16`; `a6`: `bv_kill &= succ+112`, `bv_gen \|= (succ+64) & (succ+16)`; neither: `orIfChanged(succ+40, live)`, `bv_gen \|= succ+16` |

After each instruction, the callback `a2(ctx, instr, 0, emit_mode, dep_ctx)` is invoked. The callback examines the dependency DAG and emits the appropriate sync primitive (`BAR`, `DEPBAR`, or `MEMBAR`) based on which dependency edges cross the current program point.

**GMMA arrive merge (opcode 271).** The three-bitvector merge after the successor loop: if `block_changed` is false, `live = live & ~bv_kill` (subtract kills); in all cases, `live \|= bv_gen` (add gens). When `a6` is set and the block is unchanged, the stronger operation `live = live & ~bv_kill` followed by `live \|= (succ+64) & (succ+16)` is applied per-successor instead.

**Block-exit fixup.** After the instruction loop completes, if the function has multiple basic blocks (`sub_7DDB50 > 1`), a stack-pointer presence check (`ctx+2132 != -1`) or an architecture vtable+1072 query determines whether the block's exit live set at `bb+16` needs a final `OR=` or `orIfChanged` merge from `live`.

**Bitvector operations (13 functions).** Complete inventory of bitvector primitives:

| # | Address | Name | Sites | Role in sync insertion |
|---|---------|------|-------|------------------------|
| 1 | `sub_BDBA60` | `allocate` | 2 | Allocates `bv_kill`, `bv_gen` for UR tracking |
| 2 | `sub_BDBB80` | `setBit` | 4 | Marks individual regs live (call defs, IMMA defs, stack ptr) |
| 3 | `sub_BDC050` | `free` | 2 | Releases `bv_kill`, `bv_gen` at function exit |
| 4 | `sub_BDC080` | `clearAll` | 2 | Zeroes `bv_gen` (opcode 271) or `live` (opcode 94) |
| 5 | `sub_BDC0A0` | `setAll` | 1 | Fills `bv_kill` with 0xFF (opcode 271: start as "kill everything") |
| 6 | `sub_BDC1B0` | `copyFrom` | 5 | Propagates `live` to `bb_live` at block terminators |
| 7 | `sub_BDC300` | `assign` | 7 | Replaces `live` with successor's set (call returns, joins) |
| 8 | `sub_BDC5F0` | `AND=` | 1 | Intersects kill set with successor info (opcode 271 + `a6`) |
| 9 | `sub_BDCDE0` | `OR=` | 13 | Primary merge: unions successor/def sets into `live` |
| 10 | `sub_BDCF40` | `orIfChanged` | 5 | Fixed-point detection: returns 1 if `live` grew |
| 11 | `sub_BDD140` | `orWithAND` | 2 | Fused `dst \|= a & b` for masked gen-set merge |
| 12 | `sub_BDD8C0` | `assignANDNOT` | 2 | Fused `dst = a & ~b` for kill-set subtraction |
| 13 | `sub_A06950` | sync helper | 2 | Wraps `assign`/`OR=` selection for IMMA/LDGDEPBAR opcodes |

---

## Phase 100 -- ApplyPostSyncronizationWars

| | |
|---|---|
| **Phase name** | `ApplyPostSyncronizationWars` |
| **Category** | Scheduling |
| **Execute wrapper** | `sub_C607A0` (51 bytes) |
| **Implementation** | Architecture-dispatch via `*(*(ctx+0x630))->vtable[0x110/8]` |
| **Shared WAR infrastructure** | `sub_6FBC20` (7.4KB), `sub_6FA5B0` (2.5KB), `sub_6FA930`, `sub_6FA7B0`, `sub_6FAA90` |
| **Nullsub guard** | Skips if vtable entry equals `nullsub_170` (`0x7D6C80`) |
| **Gating** | Requires `opt_level > 1` |
| **Pipeline position** | After `OriDoSyncronization` (99), before `AdvancedPhaseAllocReg` (101) |

### Purpose

ApplyPostSyncronizationWars fixes write-after-read (WAR) hazards that are introduced or exposed by the synchronization insertion in phase 99. When OriDoSyncronization inserts `BAR`, `DEPBAR`, or `MEMBAR` instructions, those new instructions read registers that subsequent instructions may overwrite -- the GPU pipeline can execute the write before the sync instruction observes the old value. This pass scans for such hazards and resolves them by inserting explicit dependency barriers, scoreboard waits, and stall-cycle annotations.

The same WAR resolution algorithm (`sub_6FBC20`) is reused in three pipeline positions: here (phase 100, post-sync), in the Mercury pipeline (phases 119/121, pre- and post-opex), and at phase 105 (`ApplyPostRegAllocWars`, post-register-allocation). The reason for the repetition is that each preceding pass introduces new instructions that can create fresh WAR hazards absent from the prior stream.

### Dispatch Mechanism

```asm
; sub_C607A0 (51 bytes)
mov    rbx, rsi                ; save ctx
call   sub_7DDB50              ; get opt_level
cmp    eax, 1
jle    return                  ; skip if opt_level <= 1
mov    rdi, [rbx+0x630]       ; rdi = ctx->arch_backend
mov    rax, [rdi]              ; rax = arch_backend->vtable
mov    rax, [rax+0x110]       ; vtable[34] = ApplyPostSyncronizationWars impl
cmp    rax, 0x7D6C80          ; compare with nullsub_170
jne    call_impl               ; if not nullsub, call it
return:
    ret
call_impl:
    jmp    rax                 ; tail-call architecture implementation
```

The `nullsub_170` check (at `0x7D6C80`) is the no-op sentinel: if the architecture backend does not override vtable slot 34, the phase is silently skipped. This allows architectures whose sync instructions do not create WAR hazards to avoid the pass entirely.

### WAR Detection Algorithm -- `sub_6FA5B0` (2.5KB)

The architecture implementation tail-calls into the shared WAR generation pass `sub_6FBC20`, which walks every instruction in the function and invokes the three-stage hazard detector `sub_6FA5B0` per instruction.

**Stage 1 -- bitmask `0x100000400001` (opcodes 34--78).** The range check `(opcode - 34) > 0x2C` admits opcodes 34--78. Within that 45-bit window, three set bits select opcodes for architecture-specific vetting via vtable +968/+1008: opcode 34 (IDE), 56 (BMOV), and 78 (RTT). All other opcodes in the range (42 instructions including I2I, MUFU, DEPBAR, BRA, CALL, RET, EXIT, etc.) fall through to stage 2.

**Stage 2 -- bitmask `0x800200000100001` (opcodes 71--130) plus opcode 235.** An explicit equality test marks opcode 235 (UBLKRED) as never-hazardous. Within the 60-bit window, four bits flag non-hazardous opcodes: 71 (CALL), 91 (AST), 116 (PIXLD), 130 (HSET2). All remaining opcodes pass to stage 3.

**Stage 3 -- per-opcode classification.** The remaining opcodes are classified into four severity tiers:

| Category | Opcodes | Action |
|----------|---------|--------|
| Always hazardous | 49 (FRND), 92 (OUT), 248 (VIADDMNMX) | `++counter` (severity 1) |
| Conditionally hazardous | 75 (BPT) | hazardous unless `sub_10AE600(ctx, operand, 179)` succeeds |
| Severity 3 (medium) | 35 (I2I) | gated by vtable +528 arch check |
| Severity 4 (high) | 35 (I2I), 246 (VHMNMX) | vtable +504 arch check; VHMNMX is unconditional |

The detector maintains a per-instruction WAR counter at `*(DWORD*)(state+8)` and severity at `*(DWORD*)(state+12)`. For severity >= 3, `sub_6FA430` inserts `(severity - counter)` additional stall slots before the hazardous instruction. The WAR flag is recorded in the instruction's operand descriptor at bit 9 (`*(DWORD*)(operand+280) |= 0x200`), and a register liveness bitvector is updated at `state[13] + 8*(latency>>6)` with a single-bit set for the conflicting register.

### Fixup Actions

When the detector returns severity >= 3, three fixup handlers fire in sequence:

1. **`sub_6FA930`** -- inserts a DEPBAR (opcode 54, scoreboard barrier) before the hazardous instruction when `*(BYTE*)(instr+48) & 0x10` is set. Barrier type is extracted from bits 7:5 of the flag byte. Encoding: `*(DWORD*)(new_instr+56) = 4`; control bits `*(DWORD*)(new_instr+48) &= 0xFFF83FFF | 0x50000`.
2. **`sub_6FA7B0`** -- inserts a WAITDP (opcode 246) if one does not already exist at the insertion point. Operands are configured with codes 102/467 (barrier ID) and 301/1520 (wait mask). Uses FNV-1a hash lookup to detect existing WAITDPs.
3. **`sub_6FAA90`** (7.9KB) -- computes required stall cycles via architecture vtable methods at +888/+896/+904 and writes them into the instruction's control word. Architecture config field `v8[14] == 9` triggers GPU-family-specific stall tables.

After the per-instruction loop completes, `sub_6FB850` (post-WAR adjustment, 4.5KB) and `sub_6FB350` (WAR finalization, 6KB) run a cleanup pass that removes redundant barriers and reconciles stall counts across basic block boundaries.

---

## Phase 114 -- FixUpTexDepBarAndSync

| | |
|---|---|
| **Phase name** | `FixUpTexDepBarAndSync` |
| **Category** | Scheduling |
| **Execute wrapper** | `sub_C60600` (51 bytes) |
| **Implementation** | Architecture-dispatch via `*(*(*(ctx+0x630)+0x10))->vtable[0x70/8]` |
| **Nullsub guard** | Skips if vtable entry equals `nullsub_43` (`0x680170`) |
| **Gating** | Requires `opt_level > 1` |
| **Pipeline position** | After `PostFixForMercTargets` (113), before `AdvancedScoreboardsAndOpexes` (115) |
| **Prerequisite** | Phase 91 (`OriCalcDependantTex`) computes texture dependency metadata |

### Purpose

FixUpTexDepBarAndSync performs a pre-scoreboard fixup of dependency barriers for texture fetch instructions. It runs *before* the main scoreboard pass (phase 115), not after it. The phase corrects barrier assignments that the instruction scheduler (phases 97--110) left in a state inconsistent with texture pipeline requirements. Texture fetches (Ori opcodes 60, 62, 78, 79 -- corresponding to PTX `tex`, `tld`/`txq`, `tmml` and related surface operations) have latencies of 200--400+ cycles and require dependency barriers rather than stall counts, since the 4-bit stall field can only encode 0--15 cycles.

Phase 91 (`OriCalcDependantTex`) runs during late optimization to compute per-instruction texture dependency metadata and mark which instructions carry texture-dependent register values. The earlier `TexNodep` pre-scheduling pass (`sub_A10100`, 556 bytes) also uses the DAG construction infrastructure (`sub_A0F970`) with texture-specific callbacks (`sub_A07E70`) to build texture-only dependency edges (parameters `a4=0, a5=1, a6=0`).

### Dispatch Mechanism

The dispatch traverses two vtable levels to reach a scheduling subsystem object owned by the architecture backend:

```c
sub_C60600(ctx, func):
    if get_opt_level(func) <= 1:        // sub_7DDB50
        return
    arch_backend  = *(func + 0x630)
    sched_subsys  = *(arch_backend + 0x10)    // secondary object
    impl          = *(*(sched_subsys) + 0x70) // vtable slot 14
    if impl == 0x680170:                      // nullsub_43 sentinel
        return
    impl(sched_subsys, func)                  // tail-call
```

The secondary object at `arch_backend+16` is the scheduling/scoreboard subsystem. It owns the per-scheduling-class scoreboard configuration tables -- 88-byte records containing up to 6 `(scoreboard_id, threshold, mask)` triplets that define which hardware scoreboards apply to each instruction class and the stall-count threshold above which a barrier is required.

### Architecture Activation

The default vtable maps slot 14 to `nullsub_43` (`0x680170`) -- a no-op for architectures that handle texture dependencies entirely within the general scoreboard pass (phase 115). Architecture backends that need texture-specific barrier fixup override this entry. The per-SM scoreboard configuration tables from the binary show the scope of scoreboard IDs involved:

| SM | Distinct scoreboard IDs | Max triplets per class |
|---|---|---|
| sm_80 | 16 (0, 2, 5, 6, 9, 11, 13, 15, 16, 18--21, 27, 31, 34) | 1 |
| sm_86--90a | 16 (0, 2, 5, 6, 12, 13, 15, 16, 18--21, 27, 30, 31, 33) | 1 |
| sm_100 | 17 (0--2, 5, 6, 12, 13, 15--17, 19, 21, 23, 27, 28, 31, 34) | **6** |
| sm_103 | 20 (0--2, 5, 6, 12--19, 21, 23, 27, 28, 30, 31, 33) | 1 |

The jump from 1 triplet per scheduling class (pre-Blackwell) to 6 triplets (sm_100) indicates Blackwell introduced multi-scoreboard dependency tracking. When a single texture fetch can map to multiple scoreboards simultaneously, the fixup must coordinate barrier assignments across all of them -- the primary motivation for this pass on sm_100.

### Fixup Algorithm

The implementation is architecture-specific (behind the vtable dispatch). Based on the scoreboard infrastructure and Phase 115's fast-path handling of texture opcodes (60, 62, 78, 79 via `sub_A22B40` in `sub_85C890`), the fixup performs:

1. **Scan**: Walk all basic blocks, identifying texture fetch instructions by Ori opcode or scheduling class
2. **Validate write-barrier**: For each texture fetch, verify the assigned write-barrier index (3-bit field in the control word, values 0--5, 7=none) covers the instruction's result registers. The hardware provides 6 barriers per warp; texture fetches typically consume one
3. **Validate wait-mask**: For each consumer of a texture result, verify the corresponding bit is set in the consumer's 6-bit wait-barrier mask at the point where the result is first read
4. **Patch stall/yield**: If the texture result is consumed closer than the scoreboard threshold (56 cycles in all extracted configs), adjust the stall count field and set the yield flag to hint warp descheduling during the texture pipeline stall
5. **Insert/adjust DEPBAR**: Where barrier-only tracking is insufficient (e.g., all 6 barriers in use, or a texture dependency crosses a barrier recycle point), insert explicit `DEPBAR` instructions

The `EIATTR_TEXMODE_INDEPENDENT` flag (code 77 / `0x4D`) in the output cubin signals that the kernel uses independent texture mode (from `.texmode_independent` or `--texmode-independent`, stored at compilation context byte +219). This affects texture descriptor resolution at runtime and may gate which fixup rules apply within the architecture implementation.

---

## Memory Order Intrinsic Lowering

Before the eight sync phases operate on the Ori IR, the OCG intrinsic lowering pipeline translates PTX memory-ordering intrinsics into Ori IR instruction sequences. Three sibling functions in the OCG body dispatcher (`sub_6D8B20`) handle the three families of memory-ordering intrinsics. All three share an identical subop-array parsing protocol and the same scope/memory-order/deprecation validation logic.

### Dispatcher and Function Family

The OCG body dispatcher at `sub_6D8B20` (432 lines) reads the intrinsic ID from `*(state+10688)` -- set by `sub_6C9BC0` which maps OCG builtin names to slot indices -- and dispatches via a 43-case switch (cases 0--0x2A). Each case corresponds to one of the 44 OCG operation slots (slot 43, `sttm`, uses a different path). The default returns the sentinel `0x10000019`.

Complete case table with OCG slot names, handler functions, sizes, and dispatch categories:

| Case | OCG name | Function | Bytes | Category |
|---|---|---|---|---|
| 0 | `add` | `sub_6BDB60` | 693 | Arithmetic (IADD3/FADD) |
| 1 | `cp_async_commit` | `sub_6BE400` | 787 | Async pipeline commit (LDGDEPBAR) |
| 2 | `cp_async_wait` | `sub_6BE720` | 1,344 | Async pipeline wait (DEPBAR) |
| 3 | `cache` | `sub_6BDE20` | 1,496 | Cache control (CCTL/PREFETCH) |
| 4 | `ld_mc` | `sub_6C9230` | 1,419 | Multicast load (LDG.MC) |
| 5 | `ldc` | `sub_6BEC60` | 1,153 | Constant load (LDC) |
| 6 | `s2r` | `sub_6BF0F0` | 841 | Special register read (S2R) |
| 7 | `acqblk` | *inline* | -- | Emits Ori opcode 298 directly |
| 8 | `preexit` | *inline* | -- | Emits Ori opcode 313 directly |
| 9 | `red_async` | `sub_6C0D90` | 3,922 | **Atomic/reduction** (scope+memorder) |
| 0xA | `cp_async_bulk` | `sub_6C1CF0` | 3,559 | **Mbarrier** (arrive/wait/test/counted) |
| 0xB | `cp_red_async_bulk` | `sub_6C2AE0` | 2,435 | Bulk async reduction (UBLKCP.RED) |
| 0xC | `cp_async_tensor` | `sub_6C3470` | 4,670 | TMA copy (UTMAKCP, 1--5D) |
| 0xD | `cp_async_prefetch_tensor` | `sub_6C46B0` | 1,768 | TMA prefetch (UTMAPF) |
| 0xE | `fence_view_async` | `sub_6C0C10` | 371 | Async fence (FENCE.VIEW.ASYNC) |
| 0xF | `viadd` | `sub_6BF440` | 1,219 | Vector integer add (VIADD) |
| 0x10 | `viaddmax` | `sub_6BFE10` | 591 | Fused add+max (VIADDMNMX) |
| 0x11 | `viaddmin` | `sub_6C0060` | 591 | Fused add+min (VIADDMNMX) |
| 0x12 | `vimax` | `sub_6C02B0` | 591 | Vector integer max (VIMNMX) |
| 0x13 | `vimin` | `sub_6C0500` | 591 | Vector integer min (VIMNMX) |
| 0x14 | `vimax3` | `sub_6C0750` | 607 | 3-way vector max (VIMNMX3) |
| 0x15 | `vimin3` | `sub_6C09B0` | 607 | 3-way vector min (VIMNMX3) |
| 0x16 | `write_async` | `sub_6C4DA0` | 3,222 | **Fence/load-store** (scope+domain) |
| 0x17 | `cctl_c` | `sub_6C5A40` | 1,646 | Cache control (CCTL shallow/deep) |
| 0x18 | `getnextworkid` | `sub_6C60B0` | 1,549 | Work distribution (selfcast/broadcast) |
| 0x19 | `fadd2` | *inline+sub_6D2AC0* | -- | Packed f16 add; Ori opcode 270 |
| 0x1A | `ffma2` | *inline+sub_6D2AC0* | -- | Packed f16 FMA; Ori opcode 279 |
| 0x1B | `fmul2` | *inline+sub_6D2AC0* | -- | Packed f16 mul; Ori opcode 282 |
| 0x1C | `mnmx` | `sub_6C8FB0` | 626 | Integer min/max (IMNMX/FMNMX) |
| 0x1D | `fmax3` | `sub_6C8BF0` | 479 | 3-way float max (FMNMX3) |
| 0x1E | `fmin3` | `sub_6C8DD0` | 479 | 3-way float min (FMNMX3) |
| 0x1F | `tcbar` | `sub_6C8100` | 1,931 | TC barrier (TCBAR) |
| 0x20 | `mmareadshma` | `sub_6C66C0` | 1,227 | MMA shared-mem read (LDSM variant) |
| 0x21 | `tccp` | `sub_6D4350` | 6,349 | TC copy (TCCP) |
| 0x22 | `tcmma` | `sub_6C6B90` | 874 | TC MMA setup (TCMMA) |
| 0x23 | `tcshift` | `sub_6C6F00` | 331 | TC shift (TCSHIFT) |
| 0x24 | `virtcount` | `sub_6C7050` | 2,442 | Virtual warp counter |
| 0x25 | `tcatomsws` | `sub_6C79E0` | 249 | TC atomic SWS (TCATOM.SWS) |
| 0x26 | `tcldsws` | `sub_6C7AE0` | 613 | TC load SWS (TCLD.SWS) |
| 0x27 | `tcstsws` | `sub_6C7D50` | 936 | TC store SWS (TCST.SWS) |
| 0x28 | `memclear` | *inline* | -- | Emits Ori opcode 345 directly |
| 0x29 | `acqshminit` | `sub_6D7AF0` | 4,133 | Shared-mem init barrier |
| 0x2A | `ldtm` | `sub_6D69B0` | 2,597 | Tensor memory load (LDTM) |

**Three memory-ordering families** (bolded above) are the sync-relevant handlers:

| Case | Function | Family | PTX instructions |
|---|---|---|---|
| 9 | `sub_6C0D90` | Atomic/reduction | `atom.add`, `atom.cas`, `atom.exch`, `red.add` |
| 0xA | `sub_6C1CF0` | Mbarrier | `mbarrier.arrive`, `mbarrier.test_wait`, `mbarrier.try_wait` |
| 0x16 | `sub_6C4DA0` | Fence / load-store | `fence.sc`, `ld.acquire`, `st.release` with scope/domain |

**Structural patterns** visible in the dispatch:

- **Inline opcode emission** (cases 7, 8, 0x28): No handler function -- `sub_934630` emits a single Ori opcode (298, 313, or 345) with `flags=1` and no operands. These are parameterless control instructions.
- **Packed-float triple** (cases 0x19--0x1B): Identical inline subop-parsing loop reads the subop array for rounding mode (subops 1--4 map to mode 0--3) and flush-to-zero flag (subop 0 sets `ftz=1`), then delegates to `sub_6D2AC0` with the operation's Ori opcode (270/279/282) and the parsed mode/ftz pair. `sub_6D2AC0` (1,633 bytes) is the shared packed-float emitter.
- **VIMNMX sextet** (cases 0x10--0x15): Six handlers of nearly identical size (591--607 bytes) that share parameter validation logic for vector integer min/max/add variants.
- **SWS trio** (cases 0x25--0x27): Software-scoreboard operations for Blackwell tensor core pipelines, three small handlers (249--936 bytes).

### Subop Array Protocol

Each intrinsic descriptor carries a subop array at `state+10704` (an `int[]`) with the count at `state+10712`. The subop values encode orthogonal PTX qualifiers (scope, memory order, type, domain) into a flat integer sequence that the lowering functions parse in positional order.

Reconstructed subop value map (shared by all three functions):

| Subop | Meaning | IR effect |
|---|---|---|
| 0 | Scope qualifier (`.sys`/`.gpu`/`.cta`) | Sets scope_level = 4 |
| 1 | Counted mode (mbarrier arrival count) | Adds extra type-14 parameter |
| 2 | Shared domain (`_shared`) | scope = 5 |
| 3 | Memory order acquire | Sets order = 5 |
| 4 | Memory order release | Sets order = 6 |
| 5 | MMIO flag (`.mmio`) | Sets flag bit 8 |
| 6 | Vector width 2x | scope_width = 2 |
| 7 | Vector width 4x | scope_width = 4 |
| 8 | Type u32 | IR type 12 |
| 9 | Type s32 | IR type 11 |
| 0xA | Type u64 | IR type 10 |
| 0xB--0x12 | Reduction ops (add/min/max/inc/dec/and/or/xor) | Op index 0--7 |

### Scope and Memory Order Validation

All three functions enforce the PTX 8.0 scoped memory model rules through a three-way decision tree. The logic (taken from `sub_6C0D90` and `sub_6C4DA0` where the strings appear verbatim; `sub_6C1CF0` enforces equivalent constraints via positional subop checks) is:

```c
if scope_qualifier_present:
    if memory_order NOT present:
        ERROR 7308: "Required scope with memory order semantics"
elif memory_order_present:
    WARNING 7308 (via sub_7F7C10): "Deprecated scope without memory order semantics"
    // Deprecation warning — may be promoted to error in future PTX versions.
    // If location info available (ctx+104), emits follow-up via sub_8955D0.

if mmio_flag AND NOT global_domain:
    ERROR 7308: "Domain param \"_global\" required for mmio semantics"
```

The warning path uses `sub_7F7C10` (the deprecation-warning emitter at `context+1176`), which returns a boolean indicating whether the warning was promoted to an error. This implements NVIDIA's staged deprecation of unscoped memory operations: PTX code using old-style `membar.cta` without explicit `.acquire`/`.release` qualifiers triggers the deprecation path, while new-style `fence.sc.cta.acquire` requires the full scope + order combination.

### Mbarrier Intrinsic Lowering -- `sub_6C1CF0`

The mbarrier handler (16KB, case 0xA) lowers `mbarrier.*` PTX intrinsics into Ori IR instruction sequences. It handles:

1. **Scope/domain parsing**: First subop must be 2 (shared) or 3 (global). If the first subop is > 1, it is treated as the domain selector directly; otherwise the function enters the two-position scope path where the second subop supplies the domain.

2. **Counted mode** (subop 1): Enables arrival-count tracking. When active, the parameter list includes an extra type-14 (integer) parameter for the expected arrival count. Bytemask mode (subop 6) is incompatible with counted mode -- error 7300: `"byte mask not allowed with counted"`.

3. **Bytemask mode** (subop 6): Requires global destination (`subop[1] == 3`) and shared source (`subop[2] == 2`). Sets flag bit 17 (`0x20000`). Error messages: `"global dst should be specified with bytemask"` and `"shared src should be specified with bytemask"`.

4. **Sequenced mode** (subop 5): Explicitly unsupported. Error 7300: `"sequenced : Not yet supported"`.

5. **MMIO flag** (subop 4 when value == 4 in the optional-subop loop): Sets bit 3 in the flag word. Only valid with global domain (scope 2); enforced by the same `"_global required for mmio"` rule.

#### Parameter Processing

Parameters are stored at `state+10728` as 12-byte records `{value[4], flags[4], type[4]}`. The function iterates over `v100` parameters (2 or 3 depending on counted mode):

- Each parameter type must be 10 (predicate register) or 12 (scope domain). Other types trigger error 7302 using the type name table at `off_229E8C0`.
- For scope-domain parameters, the top 3 bits of the value word (`(value >> 28) & 7`) select the resolution mode:
  - Mode 5: Named barrier resolution via `sub_91BF30`, then `sub_934630(opcode 130)` to create a barrier pseudo-op in the Ori IR.
  - Mode 1 (no bit 24): Direct register reference (fast path, no resolution needed).
  - Other modes: Full register resolution via `sub_91D150` + `sub_7DEFA0`.

#### Output Instruction Sequence

The function generates three Ori IR instructions:

| Step | Builder | Opcode | Purpose |
|---|---|---|---|
| 1 | `sub_934630` | 214 | Mbarrier scope-domain setup; template mask `0x90FFFFFF` |
| 2 | `sub_934630` | 273 | Memory ordering constraint / fence |
| 3 | `sub_92C240` | 299 | Mbarrier operation with full flags (arrive/wait/test) |

The flag word passed to opcode 299 encodes: `flags | 0x60000000`, where `flags` accumulates mmio (bit 3), bytemask (bit 17), and other qualifiers from the subop parsing.

### Error Codes

| Code | Message template | Severity |
|---|---|---|
| 7300 | `"Unexpected intrinsic name (%s)"` | Semantic restriction (hard error) |
| 7301 | `"Unexpected intrinsic param number (%d)"` | Parameter count mismatch |
| 7302 | `"Unexpected intrinsic type (%s) in param (%d)"` | Wrong parameter type |
| 7303 | `"Unexpected intrinsic type (%s) instead of (%s) in param (%d)"` | Type mismatch with expected |
| 7306 | `"Unexpected intrinsic subop in position (%d)"` | Positional subop error |
| 7307 | `"Unexpected intrinsic subop (%s) in position (%d)"` | Named subop error |
| 7308 | `"Instrinsic - \"%s\""` | Scope/order/domain validation |

Two diagnostic functions handle these errors: `sub_895530` emits directly when source location is available (`ctx+48`); `sub_7EEFA0` builds a deferred diagnostic record.

---

## Function Map

| Address | Size | Identity | Phase | Confidence |
|---|---|---|---|---|
| `sub_C5FBC0` | 34 | StageAndFence execute wrapper | 25 | CERTAIN |
| `sub_1392E30` | 166 | StageAndFence entry | 25 | HIGH |
| `sub_1389AF0` | 3,049 | StageAndFence setup | 25 | HIGH |
| `sub_1390B30` | 8,956 | StageAndFence core (fence insertion) | 25 | HIGH |
| `sub_138A6E0` | 3,408 | StageAndFence teardown | 25 | HIGH |
| `sub_C60BD0` | 334 | OriRemoveRedundantBarriers execute wrapper | 26 | CERTAIN |
| `sub_790A40` | 2,288 | OriRemoveRedundantBarriers main | 26 | HIGH |
| `sub_790020` | 1,200 | Post-RA scheduling helper | 26 | MEDIUM |
| `sub_7904D0` | 1,381 | Pre-RA optimization helper | 26 | MEDIUM |
| `sub_7923A0` | 2,344 | Barrier placement optimization | 26 | MEDIUM |
| `sub_792CD0` | 1,360 | Top-level barrier pass | 26 | MEDIUM |
| `0xC5F110` | 6 | ExpandMbarrier execute (vtable dispatch) | 42 | CERTAIN |
| `sub_C60080` | 34 | OptimizeSyncInstructions execute wrapper | 71 | CERTAIN |
| `sub_90A340` | 1,670 | OptimizeSyncInstructions main | 71 | HIGH |
| `sub_18F6930` | 185 | Sync optimization predicate | 71 | HIGH |
| `sub_C600B0` | 34 | LateExpandSyncInstructions execute wrapper | 72 | CERTAIN |
| `sub_1381DA0` | 1,517 | LateExpandSyncInstructions entry | 72 | HIGH |
| `sub_1381CD0` | 206 | LateExpandSyncInstructions core driver | 72 | HIGH |
| `sub_C5FAD0` | 34 | OriDoSyncronization execute wrapper | 99 | CERTAIN |
| `sub_A0F020` | 2,375 | DAG scheduler entry (sync insertion) | 99 | HIGH |
| `sub_A0D800` | -- | Dependency DAG builder | 99 | MEDIUM |
| `sub_A06A60` | 3,045 | Per-block sync processor | 99 | HIGH |
| `sub_A0B5E0` | -- | Uninitialized register check | 99 | MEDIUM |
| `sub_C607A0` | 51 | ApplyPostSyncronizationWars execute wrapper | 100 | CERTAIN |
| `sub_C60600` | 51 | FixUpTexDepBarAndSync execute wrapper | 114 | CERTAIN |
| `sub_A9C550` | 2,178 | Barrier instruction lowering | -- | HIGH |
| `sub_80F400` | 1,779 | Sync instruction SASS lowering | -- | HIGH |
| `sub_AA3BB0` | 2,726 | MBARRIER encoding | -- | HIGH |
| `sub_AA33C0` | -- | MBARRIER mnemonic builder | -- | MEDIUM |
| `sub_775010` | 18 | Barrier liveness computation entry | -- | MEDIUM |
| `sub_6D8B20` | 432 lines | OCG intrinsic body dispatcher (43-case switch) | -- | HIGH |
| `sub_6C0D90` | 812 lines | Atomic/reduction intrinsic lowering (scope+order) | -- | HIGH |
| `sub_6C1CF0` | 633 lines | Mbarrier intrinsic lowering (arrive/wait/test) | -- | HIGH |
| `sub_6C4DA0` | 647 lines | Fence/load-store intrinsic lowering (scope+domain) | -- | HIGH |

## Pipeline Position and Data Flow

The eight sync phases are distributed across the pipeline to operate at the appropriate abstraction level:

```text
Phase 25  StageAndFence               ─── Early: after loop unrolling (24)
Phase 26  OriRemoveRedundantBarriers   ─── Early: before GeneralOptimize (29)
    ... (mid-level optimization) ...
Phase 42  ExpandMbarrier               ─── Mid: after CTA expansion (40)
    ... (late optimization) ...
Phase 71  OptimizeSyncInstructions     ─── Late: after varying propagation (70)
Phase 72  LateExpandSyncInstructions   ─── Late: before SSA destruction (73)
    ... (legalization, scheduling setup) ...
Phase 99  OriDoSyncronization          ─── Post-opt: sync insertion pass
Phase 100 ApplyPostSyncronizationWars  ─── Post-opt: WAR fixup
    ... (register allocation, scheduling) ...
Phase 114 FixUpTexDepBarAndSync        ─── Post-sched: texture dep fixup
```

Data dependencies between phases:

- **Phase 25 -> 26**: StageAndFence inserts fences; OriRemoveRedundantBarriers may then eliminate redundant ones.
- **Phase 42 -> 71**: ExpandMbarrier materializes mbarrier ops; OptimizeSyncInstructions may simplify the resulting sequences.
- **Phase 71 -> 72**: OptimizeSyncInstructions reduces sync count; LateExpandSyncInstructions expands remaining pseudo-ops to SASS.
- **Phase 99 -> 100**: OriDoSyncronization inserts sync instructions; ApplyPostSyncronizationWars fixes hazards introduced by the insertion.
- **Phase 114 -> 115**: FixUpTexDepBarAndSync prepares texture barriers for AdvancedScoreboardsAndOpexes.

## Architecture-Specific Behavior

The sync passes have significant architecture-dependent behavior controlled through the architecture backend vtable at `ctx+1584`:

| SM generation | Key behavior |
|---|---|
| sm70--sm75 (Volta/Turing) | Explicit `BSSY`/`BSYNC` convergence; `WARPSYNC` required; `--no-membermask-overlap` warning active; `EIATTR_SW_WAR_MEMBAR_SYS_INSTR_OFFSETS` emitted for `membar.sys` WAR |
| sm80--sm89 (Ampere/Ada) | `cp.async` commit/wait groups; `ERRBAR` after `membar.sys`; barrier number range checked [0..15] |
| sm90--sm90a (Hopper) | Full `MBARRIER` support; TMA async pipeline barriers; `EIATTR_NUM_MBARRIERS` and `EIATTR_MBARRIER_INSTR_OFFSETS` emitted; `wgmma.fence` / `tcgen05.fence` sync fences for tensor operations |
| sm100+ (Blackwell) | Extended cluster barriers (`barrier.cluster.arrive`/`wait`); `fence.proxy` with proxy domain annotations; `sync_restrict::shared::{cta,cluster}` scope qualifiers; async bulk multicast |

The `sub_18F6930` predicate (185 bytes) encodes the architecture-specific decision logic. The magic value 28673 at `*(ctx+1584)+372` corresponds to an architecture version threshold that enables explicit synchronization optimization for Volta-class and later architectures.

## Related CLI Options

| Option | Effect |
|---|---|
| `--assume-extern-functions-do-not-sync` | Tells the compiler that external function calls do not execute synchronization instructions, enabling more aggressive barrier elimination |
| `--no-membermask-overlap` | Asserts that no sync instruction is executed with different but overlapping thread masks (sm70--sm75 only). Enables additional optimizations. |
| `--print-potentially-overlapping-membermasks` | Diagnostic: prints locations of sync instructions where the compiler must assume overlapping masks |

## Related Knobs

| Knob | Effect |
|---|---|
| `DisableErrbarAfterMembar` | When set to 1, suppresses error barrier (`BAR.SYNC 15`) insertion after `membar.sys` instructions |
| Knob 358 | Sync optimization mode selector (0=disabled, 1=conservative, 2=aggressive, 3+=full analysis) |
| Knob 472 | Barrier liveness tracking enable |
| Knob 487 | Iteration gate (shared with multiple passes); controls maximum number of iterations |

## Cross-References

- [Pass Inventory](index.md) -- complete 159-phase table with sync phases at positions 25, 26, 42, 71, 72, 99, 100, 114
- [Scheduler Architecture](../scheduling/overview.md) -- the scheduling infrastructure reused by OriDoSyncronization
- [Scoreboards & Dependency Barriers](../scheduling/scoreboards.md) -- phases 114, 115, 116; scoreboard generation
- [Phase Manager](phase-manager.md) -- vtable dispatch mechanism, factory switch
- [Predication](predication.md) -- shares entry infrastructure with LateExpandSyncInstructions
- [Intrinsics Index](../intrinsics/index.md) -- OCG body dispatcher (`sub_6D8B20`) and per-family lowering functions
- [OCG Intrinsic Lowering](../codegen/overview.md) -- dispatch table for `sub_6C0D90`/`sub_6C1CF0`/`sub_6C4DA0`
- [GMMA/WGMMA Pipeline](gmma-pipeline.md) -- `wgmma.fence` and `tcgen05.fence` interactions
- [SM Architecture Map](../targets/index.md) -- per-SM sync capabilities
- [Knobs System](../config/knobs.md) -- knob 358, 472, 487, DisableErrbarAfterMembar
