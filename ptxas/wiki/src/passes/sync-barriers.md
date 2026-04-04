# Synchronization & Barriers

The ptxas synchronization pipeline manages the insertion, optimization, and expansion of all GPU synchronization and barrier instructions. Eight phases span the full compilation pipeline, from early memory-ordering fence insertion through post-scheduling dependency barrier fixup. These phases collectively translate the PTX memory model into the hardware synchronization primitives required by each SM architecture: thread block barriers (`BAR`), memory barriers (`MEMBAR`), dependency barriers (`DEPBAR`), warp-level synchronization (`WARPSYNC`/`BSYNC`/`BSSY`), and asynchronous barriers (`MBARRIER`).

| | |
|---|---|
| **Phases** | 25, 26, 42, 71, 72, 99, 100, 114 |
| **Categories** | Lowering (25, 42, 72), Optimization (26, 71), Scheduling (99, 100, 114) |
| **Pipeline span** | Phase 25 (early optimization) through phase 114 (post-scheduling) |
| **Key opcodes** | `BAR` (opcode 95), `MEMBAR`, `DEPBAR`, `BSYNC`, `BSSY`, `WARPSYNC`, `MBARRIER.*` |
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

In SASS, these map to the `BAR` instruction family with opcode 95. The `EIATTR_NUM_BARRIERS` metadata records the maximum barrier index used, which the hardware uses to partition the convergence barrier file.

```
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

```
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

The main pass (8,956 bytes) is the largest function in this phase group. It:

1. Iterates over the basic block list via the instruction chain (`context+272`)
2. Identifies memory operations that cross unrolled loop iteration boundaries
3. Computes fence requirements based on the memory model and target architecture
4. Calls `sub_A0F020` (the scheduling entry point) to build dependency information and determine where fences are needed
5. Inserts `fence.proxy` or `MEMBAR` pseudo-instructions at identified locations
6. Updates the instruction list metadata via `sub_781F80` (basic block refresh)

The function takes floating-point parameters (`double a2, double a3, __m128d a4`), suggesting it incorporates latency and throughput heuristics when deciding fence placement -- preferring to merge adjacent fences or delay fences to overlap with independent computation.

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

```
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

6. **Redundancy analysis**: For each barrier instruction (opcode 130, i.e., `BAR`/`MEMBAR`), checks whether the barrier's destination register is live in any successor block. If the barrier result is dead (no thread could observe it before the next dominating barrier), the barrier is eliminated.

7. **Block-level merging** (`sub_75EAE0`, `sub_75E2F0`): Merges barriers at block boundaries where adjacent blocks have compatible barrier scopes.

The algorithm checks barriers by walking the instruction chain and testing opcode 130 (the internal Ori opcode for barrier/sync instructions). For each barrier, it extracts the destination operand (`field+84`), resolves the register through the register table at `context+88`, and tests whether the register's use-count (`reg+24`) indicates the barrier result is consumed.

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

```
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

```
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

```
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

    // Walk instruction list, find opcode 130 (BAR/sync)
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

The pass iterates the flat instruction list (not per-block), checking every instruction with opcode 130 (the internal Ori IR opcode for barrier/synchronization instructions). For each barrier, it examines the operand to determine:

1. Whether the barrier result register is consumed by any subsequent instruction
2. Whether the barrier can be merged with an adjacent barrier of the same scope
3. Whether the barrier guards a memory region that is provably thread-local

The `sub_1245740` call performs the actual redundancy proof by checking dominance relationships between barrier pairs.

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

```
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

```
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

The per-block processor (3,045 bytes, 53 callees) is the core of sync insertion. For each basic block:

1. **Allocates temporary liveness bitsets** via `sub_BDBA60` (bitvector alloc)
2. **Copies block-entry live set** from `ctx+832` via `sub_BDC300`
3. **Walks instructions forward**, examining each opcode (masked by `0xCFFF`):
   - Opcode 93 (call): copies callee-save register set, handles arguments
   - Opcode 95 (barrier): AND-merges successor block live sets
   - Opcode 97 (branch): tests if live set changed since block entry
4. **Inserts sync instructions** where data dependencies cross synchronization boundaries
5. **Updates uniform register liveness** at `ctx+856` when `ctx+1378 bit 3` is set

The function uses extensive bitvector operations (13 different bitvector functions from the `sub_BDB*`/`sub_BDC*` infrastructure) to track register liveness through synchronization points.

---

## Phase 100 -- ApplyPostSyncronizationWars

| | |
|---|---|
| **Phase name** | `ApplyPostSyncronizationWars` |
| **Category** | Scheduling |
| **Execute wrapper** | `sub_C607A0` (51 bytes) |
| **Implementation** | Architecture-dispatch via `*(*(ctx+0x630))->vtable[0x110/8]` |
| **Nullsub guard** | Skips if vtable entry equals `nullsub_170` (`0x7D6C80`) |
| **Gating** | Requires `opt_level > 1` |
| **Pipeline position** | After `OriDoSyncronization` (99), before `AdvancedPhaseAllocReg` (101) |

### Purpose

ApplyPostSyncronizationWars fixes write-after-read (WAR) hazards that are introduced or exposed by the synchronization insertion in phase 99. When OriDoSyncronization inserts new barrier or memory fence instructions, these insertions can create new register hazards (the barrier instruction may read a register that a subsequent instruction writes). This pass scans for and resolves those hazards.

### Dispatch Mechanism

```asm
; sub_C607A0
mov    rbx, rsi                ; save ctx
call   sub_7DDB50              ; get opt_level
cmp    eax, 1
jle    return                  ; skip if opt_level <= 1

mov    rdi, [rbx+0x630]       ; rdi = ctx->arch_backend
mov    rax, [rdi]              ; rax = arch_backend->vtable
mov    rax, [rax+0x110]       ; vtable[34] = ApplyPostSyncWars impl
cmp    rax, 0x7D6C80          ; compare with nullsub_170
jne    call_impl               ; if not nullsub, call it
return:
    ret
call_impl:
    jmp    rax                 ; tail-call architecture implementation
```

The `nullsub_170` check (at `0x7D6C80`) is the no-op sentinel: if the architecture backend does not override this vtable entry, the phase is silently skipped. This allows architectures that do not have post-sync WAR hazards to avoid unnecessary work.

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

### Purpose

FixUpTexDepBarAndSync performs a post-scheduling fixup of texture dependency barriers and synchronization instructions. After the main scheduling passes (phases 97--110) have reordered instructions and the Mercury encoder (phases 117--122) has finalized SASS encoding, texture fetch instructions may have dependency barriers that are incorrect due to instruction movement. This phase corrects those barriers.

### Dispatch Mechanism

The dispatch is doubly-indirect, going through two vtable levels:

```asm
; sub_C60600
mov    rbx, rsi
call   sub_7DDB50              ; get opt_level
cmp    eax, 1
jle    return

mov    rax, [rbx+0x630]       ; arch_backend
mov    rdi, [rax+0x10]        ; secondary object at arch_backend+16
mov    rax, [rdi]              ; secondary vtable
mov    rax, [rax+0x70]        ; vtable[14] = FixUpTexDepBar impl
cmp    rax, 0x680170           ; compare with nullsub_43
jne    call_impl
return:
    ret
call_impl:
    jmp    rax                 ; tail-call implementation
```

The double indirection (`arch_backend -> arch_backend+16 -> vtable+0x70`) indicates that the texture dependency barrier fixup lives in a secondary object owned by the architecture backend -- likely the scheduling/scoreboard subsystem object.

### Texture Dependency Barriers

Texture fetches are long-latency operations (hundreds of cycles). The hardware uses dependency barriers (scoreboards) to track their completion. When the scheduler moves a texture fetch away from its original position, the dependency barrier assignment from `AdvancedScoreboardsAndOpexes` (phase 115) may become suboptimal or incorrect. This fixup pass:

1. Scans for texture fetch instructions (`opcode 0x17` / class 0x37/0x38 in the scheduling tables)
2. Checks that the assigned write-barrier index correctly covers the instruction's result register
3. Verifies that consumer instructions have the corresponding read-barrier bit set in their wait mask
4. Adjusts stall counts and yield flags if the texture result is consumed sooner than the original schedule assumed

---

## Function Map

| Address | Size | Identity | Phase |
|---|---|---|---|
| `sub_C5FBC0` | 34 | StageAndFence execute wrapper | 25 |
| `sub_1392E30` | 166 | StageAndFence entry | 25 |
| `sub_1389AF0` | 3,049 | StageAndFence setup | 25 |
| `sub_1390B30` | 8,956 | StageAndFence core (fence insertion) | 25 |
| `sub_138A6E0` | 3,408 | StageAndFence teardown | 25 |
| `sub_C60BD0` | 334 | OriRemoveRedundantBarriers execute wrapper | 26 |
| `sub_790A40` | 2,288 | OriRemoveRedundantBarriers main | 26 |
| `sub_790020` | 1,200 | Post-RA scheduling helper | 26 |
| `sub_7904D0` | 1,381 | Pre-RA optimization helper | 26 |
| `sub_7923A0` | 2,344 | Barrier placement optimization | 26 |
| `sub_792CD0` | 1,360 | Top-level barrier pass | 26 |
| `0xC5F110` | 6 | ExpandMbarrier execute (vtable dispatch) | 42 |
| `sub_C60080` | 34 | OptimizeSyncInstructions execute wrapper | 71 |
| `sub_90A340` | 1,670 | OptimizeSyncInstructions main | 71 |
| `sub_18F6930` | 185 | Sync optimization predicate | 71 |
| `sub_C600B0` | 34 | LateExpandSyncInstructions execute wrapper | 72 |
| `sub_1381DA0` | 1,517 | LateExpandSyncInstructions entry | 72 |
| `sub_1381CD0` | 206 | LateExpandSyncInstructions core driver | 72 |
| `sub_C5FAD0` | 34 | OriDoSyncronization execute wrapper | 99 |
| `sub_A0F020` | 2,375 | DAG scheduler entry (sync insertion) | 99 |
| `sub_A0D800` | -- | Dependency DAG builder | 99 |
| `sub_A06A60` | 3,045 | Per-block sync processor | 99 |
| `sub_A0B5E0` | -- | Uninitialized register check | 99 |
| `sub_C607A0` | 51 | ApplyPostSyncronizationWars execute wrapper | 100 |
| `sub_C60600` | 51 | FixUpTexDepBarAndSync execute wrapper | 114 |
| `sub_A9C550` | 2,178 | Barrier instruction lowering | -- |
| `sub_80F400` | 1,779 | Sync instruction SASS lowering | -- |
| `sub_AA3BB0` | 2,726 | MBARRIER encoding | -- |
| `sub_AA33C0` | -- | MBARRIER mnemonic builder | -- |
| `sub_775010` | 18 | Barrier liveness computation entry | -- |

## Pipeline Position and Data Flow

The eight sync phases are distributed across the pipeline to operate at the appropriate abstraction level:

```
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
- [GMMA/WGMMA Pipeline](gmma-pipeline.md) -- `wgmma.fence` and `tcgen05.fence` interactions
- [SM Architecture Map](../targets/index.md) -- per-SM sync capabilities
- [Knobs System](../config/knobs.md) -- knob 358, 472, 487, DisableErrbarAfterMembar
