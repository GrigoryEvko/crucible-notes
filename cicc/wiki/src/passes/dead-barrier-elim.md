# Dead Barrier Elimination

CICC contains three independent passes that eliminate redundant `__syncthreads()` barriers from CUDA kernels. This page documents the lightweight `basic-dbe` pass -- a single-pass, intra-block pattern matcher that removes trivially dead barriers without dataflow analysis. The two heavyweight engines are covered on their own pages: [Dead Synchronization Elimination](./dead-sync-elimination.md) (`sub_2C84BA0`, 96KB, full bidirectional fixed-point dataflow) and [Branch Distribution](./branch-distribution.md) (`sub_1C47810`, 63KB, NVVM-IR-level fixed-point with restart). All three target the same goal -- eliminating barriers that provably do not order any memory hazard -- but at different cost/precision tradeoffs.

## Key Facts: `basic-dbe`

| Property | Value |
|---|---|
| Pass name | `basic-dbe` |
| Class | `llvm::BasicDeadBarrierEliminationPass` |
| Scope | Function pass (LLVM IR level) |
| Registration | New PM #376, line 2212 in `sub_2342890` (first NVIDIA function pass registered) |
| Runtime positions | Inserted via pipeline extension callbacks; not in the Tier 0/1/2/3 tables (see [Pipeline](../llvm/pipeline.md)) |
| Parameters | None (non-parameterized pass) |
| Knob constructor | `ctor_261` (below 5KB, in `0x4F0000`--`0x51FFFF` range) |
| Enable global | `byte_4FBB6C0` (initialized to 0 in `ctor_261`, set to 1 by pipeline setup) |
| Binary size | Small (< 5KB compiled) |
| Upstream equivalent | None -- entirely NVIDIA-proprietary |

## Why a Lightweight Pass Exists

The full dead synchronization elimination engine at `sub_2C84BA0` is 96KB of code implementing bidirectional fixed-point dataflow with complete restart after each removal. That is expensive. For the common cases -- consecutive barriers with no intervening memory operations, barriers at function entry/exit with no shared memory traffic in the block, or barriers immediately followed by another barrier -- the heavyweight engine is overkill.

`basic-dbe` exists as a cheap pre-filter: it handles the trivially dead cases in a single linear scan per function, eliminating the low-hanging fruit before the full engine (if scheduled) performs its expensive inter-block analysis. By removing obvious dead barriers early, `basic-dbe` also reduces the iteration count of the heavyweight pass, since fewer barriers remain for it to analyze.

## Algorithm

`basic-dbe` operates as a single-pass function pass with no dataflow propagation, no fixed-point iteration, and no restart-on-removal. It scans each basic block once and applies local pattern matching to identify barriers that are trivially dead.

### Barrier Identification

The pass reuses the same barrier predicate logic as the full engine. An instruction is a synchronization barrier if all of the following hold:

1. Opcode == 85 (internal call opcode for intrinsics)
2. The callee pointer at offset -32 is non-null
3. The callee's byte at offset 0 == 0 (intrinsic, not user-defined function)
4. The `convergent` attribute flag (bit `0x20` at byte+33) is set
5. `sub_CEA1A0(callee.field[36])` confirms the intrinsic ID falls within the known barrier ID range

This is the same check implemented by `sub_2C83D20` in the full engine.

### Elimination Patterns

`basic-dbe` identifies four categories of trivially dead barriers, all detectable without inter-block analysis:

#### Pattern 1: Consecutive Barriers

Two or more `__syncthreads()` calls with no intervening instructions (or only non-memory instructions between them). The second and subsequent barriers are redundant because the first already forces all threads to synchronize.

```llvm
; Before basic-dbe:
  call void @llvm.nvvm.barrier0()     ; barrier A
  call void @llvm.nvvm.barrier0()     ; barrier B -- DEAD (consecutive)

; After basic-dbe:
  call void @llvm.nvvm.barrier0()     ; barrier A retained
```

#### Pattern 2: Barrier in Empty Block

A basic block whose only non-terminator instructions are barriers and non-memory operations (debug info, metadata). If no instruction in the block reads or writes shared/global memory, every barrier in the block is dead -- there is nothing to order.

```llvm
; Before basic-dbe:
bb_empty:
  call void @llvm.nvvm.barrier0()     ; DEAD -- no memory ops in block
  br label %bb_next

; After basic-dbe:
bb_empty:
  br label %bb_next
```

#### Pattern 3: Barrier at Function Entry

A barrier at the start of a kernel (or device function) with no memory operations between function entry and the barrier. Since no thread has performed any shared memory access yet, the barrier orders nothing.

#### Pattern 4: Barrier Before Return

A barrier immediately before a return with no memory operations between the barrier and the function exit. The barrier would order accesses that have already been performed, but since no subsequent access follows, no hazard exists in the forward direction.

### Pseudocode

```cpp
function BasicDeadBarrierEliminationPass::run(F):
    if not byte_4FBB6C0:          // global enable flag
        return PreservedAnalyses::all()

    changed = false

    for each BB in F:
        barriers = []
        has_memory_op = false

        for each inst in BB:
            if isSyncBarrier(inst):
                if not has_memory_op:
                    // Pattern 2/3: barrier with no preceding memory op
                    // Also handles Pattern 1: consecutive barriers
                    //   (first barrier is not a memory op, so second is dead)
                    mark inst for deletion
                    changed = true
                else:
                    barriers.append(inst)
                    has_memory_op = false   // reset for next segment

            else if classifyMemoryAccess(inst) has read or write:
                has_memory_op = true

        // Pattern 4: check trailing barrier before terminator
        if not barriers.empty() and not has_memory_op:
            mark barriers.back() for deletion
            changed = true

    // Delete all marked instructions
    for each marked inst:
        inst.eraseFromParent()

    if changed:
        return PreservedAnalyses::none()  // IR modified
    else:
        return PreservedAnalyses::all()
```

The key design choice: `basic-dbe` treats each basic block as an isolated unit. It does **not** look at predecessor or successor blocks. This means it will miss cases where a barrier is dead because all reaching paths lack memory accesses -- those cases require the full inter-block dataflow of `sub_2C84BA0` or `sub_1C47810`.

### Memory Access Classification

Within the basic block scan, `basic-dbe` must determine which instructions constitute memory operations that could create cross-thread hazards. The classification mirrors the logic in `sub_2C83AE0` (the full engine's classifier):

| Opcode | Value | Instruction | Classification |
|---|---|---|---|
| 61 | `0x3D` | Store | Memory write |
| 62 | `0x3E` | Load | Memory read |
| 65 | `0x41` | Atomic | Memory read + write |
| 66 | `0x42` | AtomicCmpXchg | Memory write |
| 85 | `0x55` | Call/Intrinsic | Read+Write if callee accesses shared/global memory |

Non-memory instructions (arithmetic, comparisons, PHI nodes, debug info, branches) do not set the `has_memory_op` flag.

## The `byte_4FBB6C0` Enable Flag

The global byte at `byte_4FBB6C0` serves as a shared enable flag initialized to 0 in `ctor_261`. The pipeline setup code sets it to 1 when the optimization level and target configuration warrant running barrier elimination. This same flag gates `branch-dist` (`sub_1C49D10` checks it before invoking `sub_1C47810`), confirming that `ctor_261` initializes shared state for the barrier elimination subsystem as a whole, not just `basic-dbe`.

## Relationship to Other Dead-Sync Passes

CICC's three barrier elimination passes form a layered strategy:

| Property | `basic-dbe` | `branch-dist` | Dead Sync Elimination |
|---|---|---|---|
| **Entry point** | `llvm::BasicDeadBarrierEliminationPass` | `sub_1C47810` | `sub_2C84BA0` |
| **PM slot** | 376 (New PM function pass) | 377 (New PM function pass) | None (module-level caller) |
| **Scope** | Intra-block only | Inter-block (CFG propagation) | Inter-block (full restart) |
| **Dataflow** | None (pattern match) | Fixed-point, 13 RB-tree maps | Fixed-point, 12 RB-tree maps |
| **Restart on removal** | No | Yes (goto LABEL_2) | Yes (goto LABEL_2) |
| **IR level** | LLVM IR (opcodes 61/62/65/66/85) | NVVM IR (opcodes 0x36/0x37/0x3A/0x3B/0x4E) | LLVM IR (opcodes 61/62/65/66/85) |
| **Binary size** | < 5KB | 63KB core + helpers | 96KB core + helpers |
| **Knobs** | `byte_4FBB6C0` enable flag | 10 knobs (ctor_525) | None known (controlled by caller) |
| **Complexity** | O(n_instructions) | O(B * F * C) | O(B * F * C) |
| **Typical runtime** | Microseconds | Milliseconds | Milliseconds |

The intended execution order:

1. **`basic-dbe`** runs first in the function pass pipeline, eliminating trivially dead barriers in O(n) time.
2. **`branch-dist`** runs next (slot 377, immediately after basic-dbe at slot 376), performing full inter-block analysis on the reduced barrier set using NVVM IR opcodes.
3. **Dead Sync Elimination** (`sub_2C84BA0`) runs later from module-level callers (`sub_2C88020`, `sub_2C883F0`), performing the most aggressive analysis using LLVM IR opcodes with the element-size gate and special intrinsic ID handling.

## Configuration

| Knob | Type | Default | Effect |
|---|---|---|---|
| `byte_4FBB6C0` | bool (global) | 0 (disabled) | Master enable for `basic-dbe` and `branch-dist` |

No dedicated per-pass knobs (threshold, dump flags, or limits) have been identified for `basic-dbe` itself. The pass is controlled entirely by its enable flag. This is consistent with its role as a lightweight pre-filter -- there is nothing to tune.

## Function Map

| Function | Address | Size | Role |
|---|---|---|---|
| -- | `sub_2342890` line 2212 | -- | New PM registration: maps `"basic-dbe"` to `llvm::BasicDeadBarrierEliminationPass` |
| -- | `ctor_261` (0x4F range) | -- | Global constructor: initializes `byte_4FBB6C0` to 0, registers `basic-dbe` knob string |
| -- | `byte_4FBB6C0` | -- | Global enable flag (shared with `branch-dist`) |
| -- | `sub_2C83D20` | -- | `isSyncBarrier` predicate (shared with full engine) |
| -- | `sub_2C83AE0` | -- | `classifyMemoryAccess` (shared with full engine) |
| -- | `sub_CEA1A0` | -- | Barrier intrinsic ID confirmation |
| -- | `sub_B49E00` | -- | `isSharedMemoryAccess` -- CUDA address space check |
| -- | `sub_B43D60` | -- | `Instruction::eraseFromParent` -- barrier deletion |

## Cross-References

- [Dead Synchronization Elimination](./dead-sync-elimination.md) -- the full 96KB bidirectional dataflow engine
- [Branch Distribution](./branch-distribution.md) -- the NVVM-IR-level dead-sync pass (63KB, 13 RB-tree maps)
- [NVIDIA Custom Passes: Inventory](./index.md) -- registry entry
- [LLVM Optimizer: Pipeline](../pipeline/optimizer.md) -- pipeline context showing `basic-dbe` at slot 376
- [GPU Execution Model](../gpu-execution-model.md) -- why `__syncthreads()` exists and when it matters
