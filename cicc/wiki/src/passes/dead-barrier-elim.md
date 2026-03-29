# Dead Barrier Elimination

Dead barrier elimination removes redundant `__syncthreads()` calls from CUDA kernels. CICC contains two complementary passes that target synchronization barriers: a lightweight `basic-dbe` pass and a heavyweight dead synchronization elimination engine. Together they eliminate barriers that provably do not order any memory hazard, reducing warp stall cycles without affecting correctness.

| | |
|---|---|
| **Pass name (lightweight)** | `basic-dbe` |
| **Class** | `llvm::BasicDeadBarrierEliminationPass` |
| **Scope** | Function pass (IR level) |
| **Registration** | New PM slot 376, line 2212 in `sub_2342890` |
| **Knob** | `basic-dbe` (registered at `ctor_261`, address `0x4F54D0` range) |

| | |
|---|---|
| **Pass name (full)** | Dead Synchronization Elimination |
| **Entry point** | `sub_2C84BA0` |
| **Size** | 94 KB (~3,400 lines decompiled) |
| **Helpers** | `sub_2C83D20` (barrier predicate), `sub_2C83AE0` (access classifier), `sub_2C84640` (per-BB analysis) |

## Why Barriers Can Be Dead

A `__syncthreads()` barrier orders shared-memory accesses across threads in a thread block. If the code between two barriers contains no shared-memory reads or writes that could conflict, the barrier serves no purpose. The dead barrier elimination pass performs a bidirectional dataflow analysis to detect such cases.

## Algorithm Overview

The full dead synchronization elimination pass (`sub_2C84BA0`) is the largest NVIDIA custom pass analyzed, at 94 KB. It operates in five phases:

### Phase 1: Barrier Identification

The helper `sub_2C83D20` identifies sync barrier instructions by checking:

- Opcode == 85 (internal call opcode)
- The callee is an intrinsic (byte at offset 0 == 0)
- Bit `0x20` at byte+33 is set (the `convergent` attribute flag)
- `sub_CEA1A0(field+36)` confirms the intrinsic is a barrier ID

### Phase 2: Memory Access Classification

For each non-barrier instruction, `sub_2C83AE0` classifies memory behavior:

| Opcode | Instruction | Classification |
|---|---|---|
| 61 (0x3D) | Store | Write (if element size > 0x1FF bits) |
| 62 (0x3E) | Load | Read (similar large-type check) |
| 65 (0x41) | Atomic | Read + Write |
| 66 (0x42) | AtomicCmpXchg | Write |
| 85 (0x55) | Call/Intrinsic | Read+Write (general), Read-only (barrier-like with opcode 25) |

### Phase 3: Bidirectional Fixed-Point Dataflow

The pass maintains eight red-black tree maps organized into forward and backward analysis phases, tracking four access categories per basic block:

| Category | Meaning |
|---|---|
| ReadAbove | Shared-memory read reachable from above this BB |
| WriteAbove | Shared-memory write reachable from above |
| ReadBelow | Read reachable from below |
| WriteBelow | Write reachable from below |

The algorithm iterates:

1. **Forward pass** (`sub_2C84640` with direction=1): scan each BB from start toward the first barrier, OR the read/write bits, propagate from successors.
2. **Convergence check**: compare new maps against previous values. If any category changed for any BB, set the changed flag and iterate.
3. **Backward pass** (`sub_2C84640` with direction=0): same analysis in reverse, propagating from predecessors.
4. **Convergence check**: if changed, iterate.
5. When both directions converge (no changes), proceed to elimination.

### Phase 4: Elimination Decision

For each barrier, the pass checks the "bridge" maps (at offsets `a1[63..86]`):

A barrier is **redundant** if:
- `ReadAbove == 0` AND `WriteAbove == 0` (no accesses need ordering from above), **OR**
- `ReadBelow == 0` AND `WriteBelow == 0` (no accesses need ordering from below)

In both cases, there is no read-write or write-write hazard that the barrier would prevent.

A special case handles intrinsic IDs 8260--8262: if `sub_BD3660` confirms safety, these barriers are also removable.

### Phase 5: Removal and Restart

When a barrier is removed, the pass emits a diagnostic:

```
Removed dead synch: [filename:line] in function <name>
Read above: N, Write above: N, Read below: N, Write below: N
```

After each removal, the pass restarts from Phase 3 (complete re-analysis). This handles cascading redundancies -- removing one barrier may make adjacent barriers dead -- at the cost of O(n_barriers * convergence_iterations) complexity.

## The basic-dbe Lightweight Pass

The `basic-dbe` pass (`llvm::BasicDeadBarrierEliminationPass`, slot 376) is a simpler, faster version that handles obvious cases without full dataflow analysis. It runs in the standard function pass pipeline alongside `branch-dist` and other NVIDIA passes. The full dead synchronization elimination engine (`sub_2C84BA0`) runs separately and handles the complex cases requiring fixed-point iteration.

## Conservative Design

The pass is designed to be conservative:

- It iterates to a fixed point before making any removal decision.
- It restarts the entire analysis after each removal, ensuring cascading effects are captured.
- The four-category tracking (read/write above/below) catches all memory hazard patterns -- not just read-after-write, but also write-after-read and write-after-write orderings.
- Only barriers where **no** hazard exists in **either** direction are removed.

## Data Structures

The pass object contains 12 red-black tree maps at specific offsets:

| Offset range | Purpose |
|---|---|
| `a1[15..38]` | Forward analysis: ReadAbove, WriteAbove, ReadBelow, WriteBelow |
| `a1[39..62]` | Backward analysis: same four categories |
| `a1[63..86]` | Bridge results: combined read/write sets crossing barrier boundaries |

Each map is keyed by basic block pointer and stores boolean (0/1) values for the access categories.
