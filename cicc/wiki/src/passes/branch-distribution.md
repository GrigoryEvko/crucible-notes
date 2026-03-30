# Branch Distribution (Dead Synchronization Elimination)

Despite its name, the `branch-dist` pass does not distribute or restructure branches. It is a GPU-specific **dead synchronization elimination** pass that removes `__syncthreads()` barriers and fence intrinsics when no actual memory hazard exists across the barrier boundary. In CUDA kernels, programmers often insert barriers conservatively to guarantee correctness, but many of these barriers protect code regions that have no conflicting read/write patterns on shared or global memory. Removing them eliminates warp serialization points and reduces the latency cost of unnecessary thread coordination.

The pass works by classifying every instruction in the function as a shared/global memory read, a write, or neither. It then propagates this information through the control flow graph using a standard dataflow fixed-point iteration. For each synchronization instruction, it examines the memory access patterns above and below the barrier; if no read-after-write, write-after-read, or write-after-write hazard exists, the barrier is dead and is deleted. Because removing one barrier may expose others as redundant, the entire analysis restarts after each deletion until no more dead barriers remain.

## Pipeline Position

| Field | Value |
|---|---|
| Pass name | `branch-dist` |
| Pass type | FunctionPass (NVIDIA-custom, not in upstream LLVM) |
| Core function | `sub_1C47810` (2357 lines) |
| Pass wrapper | `sub_1C49D10` (179 lines) |
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

```
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

Barrier variants that carry data -- `__syncthreads_count`, `__syncthreads_and`, `__syncthreads_or` (intrinsic IDs 3734--3736) -- are explicitly excluded from removal. Their return values encode lane participation information, so they cannot be elided even when no memory hazard exists.

## Address Space Filtering

The pass only considers memory accesses to shared and global address spaces as relevant for synchronization. The address space check in `sub_1C45690`:

- Address space IDs <= `0x1FF` (511) or in the `0x300` range: considered **local/private** -- do not require synchronization.
- Address space IDs > 511 and not in the `0x3xx` range: considered **shared/global** -- these are the accesses that justify keeping a barrier.

This distinction is critical: local memory is per-thread and never visible to other threads in the warp, so barriers protecting only local accesses are always dead.

## Intrinsic Classification

Two predicates classify synchronization-related intrinsics:

**`sub_1C301F0` (is-sync-intrinsic):** Returns true for intrinsic IDs representing barrier operations:

| ID | Likely Mapping |
|---|---|
| 34 | `llvm.nvvm.barrier0` (basic `__syncthreads`) |
| 3718--3720 | `barrier.sync` / `bar.warp.sync` variants |
| 3731--3736 | `__syncthreads_count/and/or`, `bar.arrive` |

**`sub_1C30240` (is-fence-intrinsic):** Returns true for IDs 4046 and 4242, which are memory fence/membar intrinsics. These are excluded from the sync test -- they impose memory ordering but are not full barriers that can be elided by this pass.

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

When `dump-branch-dist` is enabled, the pass emits:

```
[filename:line] Removed dead synch: Read above: X, Write above: Y,
  Read below: Z, Write below: W in function NAME
```

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

| Address | Size | Role |
|---|---|---|
| `0x1C47810` | 2357L | Core algorithm: classify + propagate + remove |
| `0x1C49D10` | 179L | Pass wrapper: init state, call core, cleanup |
| `0x1C46330` | 197L | Phase 1: forward/backward instruction scan |
| `0x1C46620` | 1157L | Phase 2: CFG successor propagation (fixed-point) |
| `0x1C45690` | 117L | Instruction classifier: determines R/W flags |
| `0x1C458C0` | 28L | Helper: classify all instructions in a block |
| `0x1C46280` | 38L | Map insert-or-find (block-level maps) |
| `0x1C47760` | 37L | Map insert-or-find (instruction-level maps) |
| `0x1C475C0` | 43L | Map lower_bound lookup |
| `0x1C47660` | 50L | Map find with hint |
| `0x1C45B10` | 113L | Map erase operation |
| `0x1C45C70` | 133L | Tree destructor (recursive free) |
| `0x1C45940` | 133L | Tree destructor (recursive free, alt type) |
| `0x1C301F0` | 15L | Is-sync-intrinsic predicate |
| `0x1C30240` | 13L | Is-fence-intrinsic predicate |
| `0x563730` | 493L | CLI knob registration (`ctor_525_0`) |

## GPU-Specific Motivation

On NVIDIA GPUs, `__syncthreads()` forces all threads in a thread block to reach the barrier before any can proceed. This is one of the most expensive control flow operations in CUDA -- it serializes warp execution and creates a pipeline stall. In practice, CUDA programmers insert barriers conservatively (every shared memory access pattern gets a barrier "just in case"), leading to significant over-synchronization. This pass recovers the performance lost to unnecessary barriers by proving, through static dataflow analysis, that specific barriers protect no actual memory hazard.

The `ignore-variance-cond` knob connects to warp divergence analysis: when a branch condition is provably uniform (all lanes take the same path), synchronization across that branch is trivially unnecessary regardless of memory access patterns. This is a common case in well-structured CUDA code where control flow depends on `blockIdx` or compile-time constants.
