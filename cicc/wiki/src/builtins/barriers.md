# Barrier and Synchronization Builtins

Barrier builtins handle thread synchronization, memory fencing, and cluster-level coordination. They span IDs 1--5 (core barriers), 8--20 (cluster and barrier extensions), and several scattered IDs for memory barriers and fences. The lowering layer emits either LLVM intrinsic calls or inline PTX assembly, depending on whether the operation has a direct LLVM IR equivalent.

## Core Barriers (IDs 1--5)

The most fundamental synchronization primitives in CUDA map to the lowest builtin IDs.

| ID | Builtin | PTX Equivalent | Description |
|---|---|---|---|
| 1 | `__syncthreads` | `bar.sync 0` | Block-wide barrier |
| 2 | `__nvvm_bar0` | `bar.sync 0` | Alias for `__syncthreads` |
| 3 | `__nvvm_membar_cta` | `membar.cta` | CTA-scope memory fence |
| 4 | `__nvvm_membar_gl` | `membar.gl` | Device-scope memory fence |
| 5 | `__nvvm_membar_sys` | `membar.sys` | System-scope memory fence |

The core `__syncthreads` (ID 1) lowers to the LLVM intrinsic `llvm.nvvm.barrier0` (intrinsic ID 8259). Memory barriers at IDs 3--5 are lowered via inline IR generation: the handler builds a barrier store node through `sub_128B420` / `sub_92C9E0` and inserts it into the current basic block.

## Barrier Extensions (IDs 15--20)

These builtins extend the basic barrier with predicate reduction and explicit warp/block synchronization.

| ID | Builtin | Intrinsic | Description |
|---|---|---|---|
| 15 | `__nvvm_bar0_popc` | `llvm.nvvm.barrier0.popc` | Barrier + population count of predicate |
| 16 | `__nvvm_bar0_and` | `llvm.nvvm.barrier0.and` | Barrier + AND reduction of predicate |
| 17 | `__nvvm_bar0_or` | `llvm.nvvm.barrier0.or` | Barrier + OR reduction of predicate |
| 18 | `__nvvm_bar_sync_all` | `llvm.nvvm.barrier.sync` (8925) | Named barrier sync (all threads) |
| 19 | `__nvvm_barrier_sync` | `llvm.nvvm.barrier.sync.cnt` (9296) | Named barrier sync with count |
| 20 | `__nvvm_bar_warp_sync` | `llvm.nvvm.bar.warp.sync` (8258) | Warp-level barrier |

The reduction barriers (IDs 15--17) are dispatched through `sub_12AB550` / `sub_94C360`. The handler looks up intrinsic 3767 (EDG) or the corresponding entry from `dword_3F14778[]` (NVVM) and emits a function call via `sub_1285290` / `sub_921880`. ID 16 sets flag=1 (AND) and ID 17 sets flag=16|0 (OR); the population count variant uses the default flag.

Barriers with explicit count (IDs 205--206, `__nvvm_bar_sync_all_cnt` and `__nvvm_barrier_sync_cnt`) follow the same pattern with additional count arguments.

## Cluster Operations (IDs 8--14, SM 90+)

Thread block cluster operations were introduced with SM 90 (Hopper). These builtins query cluster geometry and perform inter-block synchronization within a cluster.

### Cluster Geometry Queries (IDs 8--10, 405--408)

| ID | Builtin | Handler | Description |
|---|---|---|---|
| 8 | `__nv_clusterDimIsSpecified_impl` | `sub_12AB0E0(ctx, 0)` | Whether cluster dimensions are explicit |
| 9 | `__nv_clusterRelativeBlockRank_impl` | `sub_12AB0E0(ctx, 1)` | Block rank within cluster |
| 10 | `__nv_clusterSizeInBlocks_impl` | `sub_12AB0E0(ctx, 2)` | Number of blocks in cluster |
| 405 | `__nv_clusterDim_impl` | -- | Cluster dimension |
| 406 | `__nv_clusterRelativeBlockIdx_impl` | -- | Block index within cluster |
| 407 | `__nv_clusterGridDimInClusters_impl` | -- | Grid dimension in cluster units |
| 408 | `__nv_clusterIdx_impl` | -- | Cluster index |

### Cluster Barriers (IDs 11--14)

| ID | Builtin | Intrinsic ID | Description |
|---|---|---|---|
| 11 | `__nv_cluster_barrier_arrive_impl` | 3767 | Signal arrival at cluster barrier |
| 12 | `__nv_cluster_barrier_wait_impl` | 3767 | Wait at cluster barrier |
| 13 | `__nv_cluster_barrier_arrive_relaxed_impl` | 3767 | Relaxed arrival (no ordering guarantee) |
| 14 | `__nv_threadfence_cluster_impl` | 4159 / 9052 | Cluster-scope memory fence |

The cluster fence at ID 14 emits intrinsic `llvm.nvvm.cp.async.commit.group` (EDG intrinsic 4159, NVVM intrinsic 9052) with a flag constant of 4, encoding the thread-fence semantic.

### Cluster Shared Memory (IDs 202--203, 365)

| ID | Builtin | Description |
|---|---|---|
| 202 | `__nv_isClusterShared_impl` | Query if address is in cluster shared memory |
| 203 | `__nv_cluster_query_shared_rank_impl` | Get rank of block that owns shared address |
| 365 | `__nv_cluster_map_shared_rank_impl` | Map address to another block's shared memory |

ID 203 has an SM-dependent lowering path: on SM <= 63, the handler returns an inline constant (passthrough); on SM 64+, it emits intrinsic 3769 (EDG) / 8825 (NVVM). The same pattern applies to ID 365, which gates on intrinsic 3770 / 9005.

## Memory Fence Lowering

Memory fences are emitted as inline PTX assembly because they have no direct LLVM IR equivalent. Two handlers exist:

### `sub_94F9E0` -- membar (CTA/Device/System)

Generates `membar.{scope};` where scope is determined by the scope parameter:

| Scope Value | PTX Output |
|---|---|
| 0, 1 | `membar.cta;` |
| 2, 3 | `membar.gl;` |
| 4 | `membar.sys;` |

The constraint string is `~{memory}` to ensure the compiler treats the fence as a full memory clobber. The emitted node receives two memory attributes: `inaccessiblemem` (attribute 41) and a readonly fence marker (attribute 6).

### `sub_94FDF0` -- fence (with explicit ordering)

Generates `fence.{ordering}.{scope};` for SM 70+ targets:

| Ordering Value | PTX Qualifier |
|---|---|
| 3 | `sc` (sequentially consistent) |
| 4 | `acq_rel` |
| 5 | `sc` (same as 3) |

Both fence handlers use `sub_B41A60` to create the inline assembly call and `sub_921880` to emit it into the instruction stream.

## Async Memory Copy Barriers (IDs 367--369)

The `cp.async` instructions for asynchronous shared-to-global memory copies include implicit barrier semantics:

| ID | Builtin | Size | Description |
|---|---|---|---|
| 367 | `__nv_memcpy_async_shared_global_4_impl` | 4 bytes | Async copy with barrier |
| 368 | `__nv_memcpy_async_shared_global_8_impl` | 8 bytes | Async copy with barrier |
| 369 | `__nv_memcpy_async_shared_global_16_impl` | 16 bytes | Async copy with barrier |

These are lowered through `sub_12AB730` / `sub_94C5F0`, which builds the `cp.async` PTX instruction with the specified transfer size.

## Architecture Gates

| SM Threshold | Barrier Feature |
|---|---|
| All SM | `__syncthreads`, `membar.{cta,gl,sys}`, barrier reductions |
| SM 70+ | Explicit fence ordering (`fence.{ordering}.{scope}`) |
| SM 70+ | `cp.async` asynchronous memory copy with barrier |
| SM 90+ (Hopper) | Cluster barriers, cluster fence, cluster shared memory queries |

## Lowering Strategy Summary

Barrier builtins use three distinct lowering strategies:

1. **LLVM intrinsic call** -- `__syncthreads`, barrier reductions, cluster barriers. These map to well-known LLVM/NVVM intrinsic IDs (8259, 8925, 9296, etc.) and emit via `sub_1285290`.

2. **Inline IR generation** -- Memory barriers (`__nvvm_membar_*`). The handler directly constructs barrier store IR nodes without going through an intrinsic lookup.

3. **Inline PTX assembly** -- Memory fences (`membar.*`, `fence.*`). These have no LLVM IR equivalent and are emitted as inline asm strings with `~{memory}` clobber constraints.

## Convergence Contract and Deadlock Conditions

`__syncthreads` (`bar.sync 0`) is a block-wide rendezvous: every thread in the CTA must execute the same `bar.sync` instance, or hardware deadlocks. Cicc enforces this only structurally — the `convergent` attribute (bit `0x20` at the intrinsic's byte+33, checked by `sub_2C83D20` in the dead-barrier-elimination predicate) prevents transforms like loop-unswitching, jump-threading, and divergent-branch sinking from duplicating a barrier into a divergent region. It does **not** prove that all threads will arrive: a `__syncthreads()` inside an `if (threadIdx.x < N)` branch where `N < blockDim.x` is undetected by cicc and hangs the kernel at runtime.

```text
barrier-safety classifier (binary-recovered, sub_2C83D20):
  is_sync(I) :=
        opcode(I) == 85                       # intrinsic call
     ∧  callee_attr(I).convergent             # bit 0x20 at byte+33
     ∧  intrinsic_id(I) ∈ barrier_id_range    # sub_CEA1A0 range check
     ∧  I.scope ∈ {CTA, cluster}              # not warp.sync

  reachable_from_all(I) :=
        ∀ entry_thread t: t will reach I along every path through the CFG
        # NOT computed by cicc — this is the responsibility of the kernel author
```

The named-barrier intrinsics (`barrier.sync.cnt`, IDs 19 / 205) accept an explicit thread count and a barrier index `[0, 15]`. With a count smaller than `blockDim.x` they serve as a sub-CTA rendezvous — useful for cooperative groups — but the count must match exactly across the named participants or the surplus threads sit at the barrier indefinitely.

> ⚡ **QUIRK — `__syncthreads` is not a memory fence on SM 70+**
> Pre-Volta, `bar.sync` doubled as an implicit `membar.cta` because all threads in the warp executed lockstep. With Independent Thread Scheduling on Volta+, `bar.sync` only guarantees control-flow convergence; loads issued after the barrier can still observe stale stores from before it unless an explicit `membar.cta` (ID 3) is also emitted. Cicc preserves user-written `membar.cta` calls but does **not** synthesize one beside a `__syncthreads` — the responsibility falls to the libdevice macros and to ptxas's post-lowering. A hand-rolled inline-PTX `bar.sync 0` without a paired `membar` is a real, silent reorder hazard on SM 70+.

> ⚡ **QUIRK — Cluster barrier ID range overlap**
> IDs 11–13 (`cluster_barrier_arrive`, `_wait`, `_arrive_relaxed`) all dispatch through intrinsic ID 3767 — the same ID used by the barrier-reduction builtins 15–17. The disambiguation is entirely positional: the `flag` operand passed to `sub_94C360` encodes both the reduction op (popc=0 / and=1 / or=16) and the cluster-barrier variant (arrive / wait / arrive_relaxed) in disjoint subranges. A wrong-flag construction inside the handler would route a cluster-arrive call to the popc-reduction codepath without any IR-verifier failure, because the LLVM intrinsic signature `(i32, i32) -> i32` is identical for both.

## Async Barrier (`mbarrier`) Objects (SM 80+)

Beyond the named-barrier wire (`bar.sync N`), Ampere introduced `mbarrier` — a 64-bit object in `.shared` that tracks an arrival count plus a parity bit. The lifecycle is exposed as a family of builtins that cicc lowers to `mbarrier.{init,arrive,arrive_drop,test_wait,try_wait,inval}` PTX instructions. The intrinsic family lives at IDs 367–369 (the `cp.async` copy builtins implicitly carry an mbarrier completion) and at the cluster-barrier IDs 11–13 (which are mbarrier-backed on SM 90+).

| PTX op | Semantics | Phase parity? |
|---|---|---|
| `mbarrier.init` | Set arrival count, clear parity | resets to 0 |
| `mbarrier.arrive` | Decrement count, return phase token | flips on count→0 |
| `mbarrier.test_wait` | Non-blocking parity comparison | reads current parity |
| `mbarrier.try_wait` | Bounded-wait parity comparison | reads current parity |
| `mbarrier.arrive_drop` | Permanently lower expected count | resets to 0 |

The parity-bit design means waiters do not race with the next phase's arrivals — a producer can call `arrive` for phase N+1 before all consumers have finished `test_wait` on phase N. Cicc does not validate that an mbarrier is initialized before use; the PTX instruction itself faults on uninitialized state at runtime.

## Cross-References

- [Warp-Level Builtins](./warp.md) — `_sync` membermask discipline is the warp-level analogue of CTA-level barrier convergence
- [Dead Synchronization Elimination](../passes/dead-sync-elimination.md) — uses the same barrier-classifier predicate (`sub_2C83D20`) to identify removable syncs
- [Dead Barrier Elimination (basic-dbe)](../passes/dead-barrier-elim.md) — single-pass dead-barrier remover that depends on the `convergent` attribute remaining intact
- [Branch Distribution](../passes/branch-distribution.md) — NVVM-IR-level pass that fixes barrier placement after loop-distribution rewrites
