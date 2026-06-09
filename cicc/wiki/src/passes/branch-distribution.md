# Branch Distribution (Dead Synchronization Elimination)

Despite its name, the `branch-dist` pass does not distribute or restructure branches. It is a GPU-specific **dead synchronization elimination** pass that removes `__syncthreads()` barriers and fence intrinsics when no actual memory hazard exists across the barrier boundary. In CUDA kernels, programmers often insert barriers conservatively to guarantee correctness, but many of these barriers protect code regions that have no conflicting read/write patterns on shared or global memory. Removing them eliminates warp serialization points and reduces the latency cost of unnecessary thread coordination.

The pass works by classifying every instruction in the function as a shared/global memory read, a write, or neither. It then propagates this information through the control flow graph using a standard dataflow fixed-point iteration. For each synchronization instruction, it examines the memory access patterns above and below the barrier; if no read-after-write, write-after-read, or write-after-write hazard exists, the barrier is dead and is deleted. Because removing one barrier may expose others as redundant, the entire analysis restarts after each deletion until no more dead barriers remain.

## Pipeline Position

| Field | Value |
|---|---|
| Pass name | `branch-dist` |
| Pass type | FunctionPass (NVIDIA-custom, not in upstream LLVM) |
| Registration | New PM #377, line 2217 in `sub_2342890` |
| Runtime positions | Tier 1/2/3 #78, #82 (NVVMBranchDist via `sub_1CB73C0`, gated by `!opts[2080] && !opts[2120]`); see [Pipeline](../llvm/pipeline.md) |
| Core function | `sub_1C47810` (9.5 KB native; 2,357 lines decomp) |
| Pass wrapper | `sub_1C49D10` (970 bytes native; 179 lines decomp) |
| Knob constructor | `ctor_525_0` at `0x563730` (493 lines) |
| Global enable flag | `byte_4FBB6C0` (initialized to 0 in `ctor_261`) |

The pass runs during the NVIDIA IR optimization pipeline. The global enable flag at `byte_4FBB6C0` is set by the pipeline setup when appropriate for the current optimization level.

## IR Before/After Example

The pass removes `__syncthreads()` barriers that protect no actual shared/global memory hazard.

**Before** (conservative barrier placement):
```llvm
define void @kernel(ptr addrspace(3) %smem) {
entry:
  %x = add i32 %tid, 1               ; pure register computation
  %y = mul i32 %x, 42                ; pure register computation
  call void @llvm.nvvm.barrier0()     ; __syncthreads() -- no shared/global R/W above
  %z = add i32 %y, %x                ; pure register computation
  ret void
}
```

**After** (dead barrier removed):
```llvm
define void @kernel(ptr addrspace(3) %smem) {
entry:
  %x = add i32 %tid, 1
  %y = mul i32 %x, 42
  ; barrier removed: no shared/global reads or writes above or below
  %z = add i32 %y, %x
  ret void
}
```

When the dataflow analysis determines that neither side of the barrier accesses shared or global memory, the barrier is dead and removed. The pass restarts after each removal since deleting one barrier may expose another as redundant.

## Algorithm

### Phase 1: Instruction Classification (`sub_1C46330`)

The classifier (`sub_1C45690`, 117 lines) examines each instruction's opcode byte at offset `+16` and determines whether it reads or writes shared/global memory:

| Opcode | Hex | Meaning | Action |
|---|---|---|---|
| `0x36` | `'6'` | Load | Check address space; mark as read if shared/global |
| `0x37` | `'7'` | Store | Check address space; mark as write |
| `0x3A` | `':'` | Memory op | Check address space |
| `0x3B` | `';'` | Memory op | Check address space |
| `0x4E` | `'N'` (78) | Call | Complex analysis: filter sync intrinsics, check callee attributes |

The classifier is invoked twice per basic block:

- **Forward scan** (a3=1): iterates from the last instruction backward to the first sync instruction. Everything after the sync is classified as "above" the barrier.
- **Backward scan** (a3=0): iterates from the first instruction forward to the first sync instruction. Everything before the sync is classified as "below" the barrier.

This produces four boolean flags per block, stored in red-black tree maps: `reads_above`, `writes_above`, `reads_below`, `writes_below`.

### Phase 2: CFG Propagation (`sub_1C46620`)

A classic dataflow fixed-point iteration propagates memory access information through successor edges. For each basic block, the read/write flags from its successors' "below" maps are OR-combined into the current block's "above" maps. The iteration repeats until no flags change (convergence). This ensures that a barrier's necessity accounts for memory accesses reachable through any control flow path, not just the local block.

The `branch-dist-norm` knob modifies the dataflow meet operator: the default (0) uses OR-propagation (conservative), while a non-zero value likely switches to AND-normalization (more aggressive, requiring all paths to access memory before considering a sync necessary).

### Phase 3: Dead Sync Identification and Removal

After propagation, the main function (`sub_1C47810`) iterates over all blocks and instructions. For each synchronization intrinsic, it looks up the four per-instruction flags:

```c
ra = inst_read_above[I]    wa = inst_write_above[I]
rb = inst_read_below[I]    wb = inst_write_below[I]
```

A sync is dead (removable) when any of these conditions holds:

| Condition | Meaning |
|---|---|
| `!ra && !wa` | Nothing above the barrier accesses shared/global memory |
| `!rb && !wb` | Nothing below the barrier accesses shared/global memory |
| `!ra && !wb` | No read-after-write or write-after-write hazard |
| `!wa && !rb` | No write-after-read or write-after-write hazard |

When a sync is removed, the pass calls `sub_15F20C0` to delete it from the IR, then **restarts the entire algorithm** (goto LABEL_2). This restart is necessary because removing one barrier may cause another to become dead.

### Special Cases

Barrier variants that carry data — `__syncthreads_count`, `__syncthreads_and`, `__syncthreads_or` (intrinsic IDs 3734--3736) — are explicitly excluded from removal. Their return values encode lane participation information, so they cannot be elided even when no memory hazard exists.

## Address Space Filtering

The pass only considers memory accesses to shared and global address spaces as relevant for synchronization. The address space check in `sub_1C45690`:

- Address space IDs <= `0x1FF` (511) or in the `0x300` range: considered **local/private** — do not require synchronization.
- Address space IDs > 511 and not in the `0x3xx` range: considered **shared/global** — these are the accesses that justify keeping a barrier.

This distinction is critical: local memory is per-thread and never visible to other threads in the warp, so barriers protecting only local accesses are always dead.

## Intrinsic Classification

Two predicates classify synchronization-related intrinsics:

**`sub_1C301F0` (is-sync-intrinsic):** Returns true for intrinsic IDs representing barrier operations:

| ID | Likely Mapping |
|---|---|
| 34 | `llvm.nvvm.barrier0` (basic `__syncthreads`) |
| 3718--3720 | `barrier.sync` / `bar.warp.sync` variants |
| 3731--3736 | `__syncthreads_count/and/or`, `bar.arrive` |

**`sub_1C30240` (is-fence-intrinsic):** Returns true for IDs 4046 and 4242, which are memory fence/membar intrinsics. These are excluded from the sync test — they impose memory ordering but are not full barriers that can be elided by this pass.

## Configuration Knobs

All registered in `ctor_525_0` at `0x563730`. All are `cl::opt<>` with `hidden` visibility.

| Knob | Type | Default | Description |
|---|---|---|---|
| `dump-branch-dist` | bool | false | Emit diagnostic output on each removed sync |
| `ignore-call-safety` | bool | **true** | Treat function calls as non-memory-accessing (aggressive) |
| `ignore-variance-cond` | int | 0 | Ignore warp divergence on branch conditions |
| `ignore-address-space-check` | int | 0 | Treat all memory accesses as requiring sync (conservative) |
| `ignore-phi-overhead` | int | 0 | Ignore PHI node overhead from sync removal in cost model |
| `disable-complex-branch-dist` | int | 0 | Disable inter-block CFG propagation (Phase 2) |
| `no-branch-dist` | string | (empty) | Comma-separated list of function names to skip |
| `branch-dist-func-limit` | int | -1 | Max functions to process (-1 = unlimited) |
| `branch-dist-block-limit` | int | -1 | Max blocks per function (-1 = unlimited) |
| `branch-dist-norm` | int | 0 | Dataflow meet operator mode (0 = OR, non-zero = AND) |

The default for `ignore-call-safety` is notably **true** (aggressive): device function calls are assumed not to access shared/global memory unless proven otherwise. This is reasonable for typical CUDA kernels where helper functions operate on registers and local memory.

## Diagnostic Strings

Diagnostic strings recovered from `p2b.3-01-branchdist.txt`. All runtime diagnostics are gated by the `dump-branch-dist` knob (default false).

| String | Source | Category | Trigger |
|--------|--------|----------|---------|
| `"[filename:line] Removed dead synch: Read above: X, Write above: Y, Read below: Z, Write below: W in function NAME"` | `sub_1C47810` phase 3 | Debug | `dump-branch-dist` enabled and a barrier is removed; prints the four read/write flags and the function name |
| `"Dump information from Branch Distribution"` | `ctor_525_0` at `0x563730` | Knob | `dump-branch-dist` knob description |
| `"Ignore calls safety in branch Distribution"` | `ctor_525_0` | Knob | `ignore-call-safety` knob description |
| `"Ignore variance condition in branch Distribution"` | `ctor_525_0` | Knob | `ignore-variance-cond` knob description |
| `"Ignore address-space checks in branch Distribution"` | `ctor_525_0` | Knob | `ignore-address-space-check` knob description |
| `"Ignore the overhead due to phis"` | `ctor_525_0` | Knob | `ignore-phi-overhead` knob description |
| `"Disable more complex branch Distribution"` | `ctor_525_0` | Knob | `disable-complex-branch-dist` knob description |
| `"Do not do Branch Distribution on some functions"` | `ctor_525_0` | Knob | `no-branch-dist` knob description (value format: `"function1,function2,..."`) |
| `"Control number of functions to apply"` | `ctor_525_0` | Knob | `branch-dist-func-limit` knob description |
| `"Control number of blocks to apply"` | `ctor_525_0` | Knob | `branch-dist-block-limit` knob description |
| `"Control normalization for branch dist"` | `ctor_525_0` | Knob | `branch-dist-norm` knob description |

## Data Structures

The pass allocates a large state object (~696 bytes, 87 QWORDs) containing 13 red-black tree maps organized in three tiers:

| Maps | Keys | Values | Purpose |
|---|---|---|---|
| `a1[3..14]` (2 maps) | Block pointer | bool | Has-sync-above/below per block |
| `a1[15..38]` (4 maps) | Block pointer | bool | Propagated read/write above/below (Phase 2 output) |
| `a1[39..62]` (4 maps) | Block pointer | bool | Initial read/write above/below (Phase 1 output) |
| `a1[63..86]` (4 maps) | Instruction pointer | bool | Per-instruction read/write above/below (Phase 3) |

All maps are `std::map`-like red-black trees with 48-byte nodes (left/right/parent pointers + key + 1-byte boolean value at offset 40). Tree operations are implemented in `sub_1C46280` (insert-or-find for block maps), `sub_1C47760` (insert-or-find for instruction maps), `sub_1C45B10` (erase), and `sub_1C45C70`/`sub_1C45940` (recursive destructors).

## Function Map

| Function | Address | Size | Role |
|---|---|---|---|
| — | `0x1C47810` | 2357L | Core algorithm: classify + propagate + remove |
| — | `0x1C49D10` | 179L | Pass wrapper: init state, call core, cleanup |
| — | `0x1C46330` | 197L | Phase 1: forward/backward instruction scan |
| — | `0x1C46620` | 1157L | Phase 2: CFG successor propagation (fixed-point) |
| — | `0x1C45690` | 117L | Instruction classifier: determines R/W flags |
| — | `0x1C458C0` | 28L | Helper: classify all instructions in a block |
| — | `0x1C46280` | 38L | Map insert-or-find (block-level maps) |
| — | `0x1C47760` | 37L | Map insert-or-find (instruction-level maps) |
| — | `0x1C475C0` | 43L | Map lower_bound lookup |
| — | `0x1C47660` | 50L | Map find with hint |
| — | `0x1C45B10` | 113L | Map erase operation |
| — | `0x1C45C70` | 133L | Tree destructor (recursive free) |
| — | `0x1C45940` | 133L | Tree destructor (recursive free, alt type) |
| — | `0x1C301F0` | 15L | Is-sync-intrinsic predicate |
| — | `0x1C30240` | 13L | Is-fence-intrinsic predicate |
| — | `0x563730` | 493L | CLI knob registration (`ctor_525_0`) |

## Common Pitfalls

These are mistakes a reimplementor is likely to make when building an equivalent dead barrier elimination pass using CFG dataflow.

**1. Using address-level tracking instead of boolean per-category flags.** The pass tracks four boolean flags per block (reads_above, writes_above, reads_below, writes_below) for shared/global memory, not specific addresses. A reimplementation that attempts to track precise addresses ("smem[0] is only written above, smem[1] is only read below") will appear to find more dead barriers but is fundamentally unsound for GPU execution. Different threads access different addresses through the same pointer expression (`smem[tid]` vs `smem[tid-1]`), making address-based alias analysis across threads impossible at compile time. The boolean-per-category approach is the correct conservative abstraction.

**2. Not excluding `__syncthreads_count/and/or` (IDs 3734--3736) from removal.** These barrier variants return a value that encodes lane participation information (`__syncthreads_count` returns the number of threads that passed a non-zero predicate). Even when no memory hazard exists across the barrier, the return value carries data that the program depends on. A reimplementation that removes these barriers based solely on memory analysis will break programs that use the return value for algorithmic purposes (e.g., warp-level voting patterns, early-exit counting).

**3. Treating the `ignore-call-safety` default as conservative.** The default for `ignore-call-safety` is `true` (aggressive): function calls are assumed not to access shared/global memory. This is correct for typical CUDA helper functions that operate on registers and local memory, but a reimplementation that uses `false` as the default will retain nearly all barriers in code that calls device functions, defeating the optimization. Conversely, a reimplementation that uses `true` but does not also check the callee's `isSharedMemoryAccess` attribute when available will miss cases where a called function does access shared memory through a pointer argument.

**4. Not restarting the analysis after removing a barrier.** The pass restarts from Phase 1 (goto LABEL_2) after each barrier deletion because removing one barrier merges the regions it separated, potentially exposing adjacent barriers as dead. A reimplementation that collects all dead barriers in one pass and removes them simultaneously will miss cascading redundancies. Worse, it may remove barriers in the wrong order: if barrier B2 is dead only because barrier B1 separates it from a hazard, removing both simultaneously removes B1's protection while the hazard still exists.

**5. Conflating address space filtering with memory visibility.** The pass considers only shared and global memory accesses (address spaces > 511 and not in the `0x3xx` range) as relevant for barrier justification. Local/private memory (per-thread, invisible to other threads) is correctly excluded. A reimplementation that includes local memory accesses in the analysis will never remove any barrier in code that uses local arrays, since every function with local variables would show "read+write above and below." The address space filter is essential for the optimization to have any effect.

## GPU-Specific Motivation

On NVIDIA GPUs, `__syncthreads()` forces all threads in a thread block to reach the barrier before any can proceed. This is one of the most expensive control flow operations in CUDA — it serializes warp execution and creates a pipeline stall. In practice, CUDA programmers insert barriers conservatively (every shared memory access pattern gets a barrier "just in case"), leading to significant over-synchronization. This pass recovers the performance lost to unnecessary barriers by proving, through static dataflow analysis, that specific barriers protect no actual memory hazard.

The `ignore-variance-cond` knob connects to warp divergence analysis: when a branch condition is provably uniform (all lanes take the same path), synchronization across that branch is trivially unnecessary regardless of memory access patterns. This is a common case in well-structured CUDA code where control flow depends on `blockIdx` or compile-time constants.
