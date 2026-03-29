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
