# TMA Atoms

## Abstract

The `cute_nvgpu` TMA atom family surfaces Hopper and Blackwell tensor-memory transfers as descriptor-driven IR. A TMA descriptor records the global tensor, tile box, strides, rank, swizzle, fill behaviour, and cache policy. Executable TMA atoms bind that descriptor to coordinates, an mbarrier, optional multicast state, and cache hints, then lower to asynchronous tensor copy or reduce instructions. This page documents the atom family, the descriptor contract, the verifier rules, and the lowering shape.

## Atom Family

| Operation | Role |
|---|---|
| `atom.tma_load` | Execute asynchronous global-to-shared tensor load. |
| `atom.tma_store` | Execute asynchronous shared-to-global tensor store. |
| `atom.tma_reduce` | Execute asynchronous tensor reduction into global memory. |
| `atom.non_exec_tiled_tma_load` | Describe a tiled TMA load before mbarrier/cache binding. |
| `atom.non_exec_tiled_tma_store` | Describe a tiled TMA store before execution binding. |
| `atom.non_exec_tiled_tma_reduce` | Describe a tiled TMA reduce before execution binding. |
| `prefetch_tma_desc` | Prefetch descriptor state before a transfer. |
| `tma_descriptor_tiled` | Descriptor type for ordinary tiled tensor movement. |
| `tma_descriptor_im2col` | Descriptor type for im2col tensor movement. |
| `atom.make_exec_tma` | Bind a non-exec atom with mbarrier, multicast, and cache mode. |

The non-exec atoms pay off because layout and partitioning can be verified before any pass commits to a runtime barrier or cache policy.

## Partition Op and Mode Enums

The TMA atom family rooted at `cute_nvgpu.tma_partition` routes every executable and non-exec TMA atom through one partition op — the canonical place where descriptor shape, transfer mode, multicast cardinality, and reduce kind are validated together. The partition verifier `sub_17A08D0` (4 598 B) enforces eleven invariants on every TMA partition op and, on success, returns a packed result record per partitioned tile.

Three mode enums select the transfer variant. Load-mode covers single-CTA, two-CTA cooperative, and warp-multicast loads at two granularities; store-mode covers tiled stores and im2col-flavour stores; reduce-kind covers the asynchronous reduces the hardware supports.

```c
typedef enum TmaLoadMode {
    TMA_LOAD_NO_MULTICAST   = 0,   // single-CTA load
    TMA_LOAD_TWO_CTA        = 1,   // 2-CTA cluster cooperative load
    TMA_LOAD_W_MULTICAST    = 2,   // warp multicast (16-thread)
    TMA_LOAD_W128_MULTICAST = 3,   // wide warp multicast (128-thread)
} TmaLoadMode;

typedef enum TmaStoreMode {
    TMA_STORE_TILED       = 0,     // tiled SMEM -> GMEM
    TMA_STORE_IM2COL      = 1,     // im2col-flavor tiled store
    TMA_STORE_IM2COL_W    = 2,     // im2col + warp multicast
    TMA_STORE_IM2COL_W128 = 3,
} TmaStoreMode;

typedef enum TmaReduceKind {
    TMA_REDUCE_ADD = 0, TMA_REDUCE_MIN = 1, TMA_REDUCE_MAX = 2,
    TMA_REDUCE_INC = 3, TMA_REDUCE_DEC = 4,
    TMA_REDUCE_AND = 5, TMA_REDUCE_OR  = 6, TMA_REDUCE_XOR = 7,
} TmaReduceKind;
```

The enums are part of the verifier's input contract. Consistency between load mode, store mode, and reduce kind is checked together with rank and swizzle in the eleven-step walk below.

## Partition Result ABI

`sub_17A08D0` returns one 24-byte `TmaPartitionResult` per partitioned tile, packed into a `SmallVector` owned by the verifier and forwarded to the executable-atom builder.

```c
typedef struct TmaPartitionResult {
    /*+0x00*/ uint64_t   descriptor_handle;     // i32-handle into the per-CTA descriptor table
    /*+0x08*/ uint32_t   smem_offset;           // SMEM byte offset for the tile
    /*+0x0C*/ uint32_t   element_count;         // number of elements in the tile
    /*+0x10*/ uint8_t    swizzle_mode;          // 0=none, 1=128B, 2=64B, 3=32B
    /*+0x11*/ uint8_t    rank;                  // descriptor rank (1..5)
    /*+0x12*/ uint16_t   flags;                 // bit 0 = multicast, bit 1 = im2col, ...
} TmaPartitionResult;
```

Total record size is 24 bytes. Only the descriptor handle, SMEM offset, and element count get consumed during lowering. Rank, swizzle, and flags are echoed back so downstream passes do not have to re-derive them from the descriptor type.

## Eleven-Step Partition Verifier

`sub_17A08D0` walks eleven invariants in fixed order. Each invariant emits a verbatim diagnostic on failure; the strings are part of the user-visible contract and a reimplementation must preserve them byte-for-byte.

| # | Invariant | Diagnostic |
|---|---|---|
| 1 | Descriptor rank gate | `"TMA descriptor rank must be 1..5"` |
| 2 | Element type whitelist | `"TMA descriptor element type must be one of {f16, bf16, f32, s8, e4m3}"` |
| 3 | Shape compatibility | `"TMA descriptor box-shape mismatch with tile-shape"` |
| 4 | Swizzle-size check | `"TMA descriptor swizzle mode incompatible with tile element size"` |
| 5 | GMEM stride alignment | `"TMA descriptor stride must be 16-byte aligned"` |
| 6 | Multicast cardinality | `"TMA descriptor multicast count must be in {1, 2, 16, 128}"` |
| 7 | Im2col preconditions | `"TMA descriptor im2col mode requires box-shape >= filter-shape"` |
| 8 | SMEM-side alignment | `"TMA descriptor smem offset must be 16-byte aligned"` |
| 9 | Reduce-mode AS check | `"TMA descriptor reduce kind requires destination to be global memory"` |
| 10 | Mode exclusivity | `"TMA descriptor cannot mix multicast and im2col"` |
| 11 | Element-count cap | `"TMA descriptor element count exceeds 65536"` |

Order matters. The cheap structural gates — rank, element type, shapes — run before the more expensive cross-field checks like mode exclusivity and element-count cap. Treat only the descriptor base pointer, per-axis dimension sizes, and non-leading strides as device-mutable. Rank, element type, swizzle, multicast count, and mode are descriptor-construction facts and cannot change once the partition op has verified.

## Descriptor Builder

Descriptor construction consumes a global tensor, a layout, dynamic shapes, dynamic strides, padding values, TMA mode, store mode, element width, multicast metadata, and operand segment sizes.

```c
TmaDescriptor build_tma_descriptor(Tensor tensor,
                                   Layout layout,
                                   ArrayRef<Value> shapes,
                                   ArrayRef<Value> strides,
                                   TmaMode mode,
                                   TmaStoreMode store_mode) {
    require(tensor.memory_space == GLOBAL_MEMORY);
    require(rank(tensor) >= 1 && rank(tensor) <= 5);
    require(!is_composed_layout(layout));
    require(layout_is_static_enough_for_tma(layout));

    TmaDescriptor desc;
    desc.base = tensor.base;
    desc.element_bits = bit_width(tensor.element_type);
    desc.rank = rank(tensor);
    desc.box = compute_box_sizes(layout, shapes);
    desc.strides = compute_tma_strides(layout, strides);
    desc.mode = mode;
    desc.store_mode = store_mode;
    desc.cache_policy = default_cache_policy();
    return desc;
}
```

The first box dimension times the element bit width must divide evenly by the TMA transfer granularity. Padding values are restricted — non-zero padding requires a mode that explicitly supports it.

## Non-Exec Atom Verification

The shared non-exec verifier checks the tuple of shared-memory layout, global layout, partitioner tile, and CTA value map. Success yields a TMA tensor type and a non-executing atom body ready to bind to runtime state later.

```c
LogicalResult verify_non_exec_tma(NonExecTmaAtom atom) {
    require(is_smem_layout(atom.smem_layout));
    require(is_global_layout(atom.global_layout));
    require(is_tile_like(atom.partitioner));
    require(is_cta_value_map(atom.cta_v_map));
    require(smem_layout_uses_supported_swizzle(atom.smem_layout));
    require(layouts_are_statically_resolvable(atom.smem_layout, atom.cta_v_map));
    require(tma_partition_is_valid(atom));
    return success();
}
```

Load, store, and reduce variants add mode-specific checks. TMA reduce accepts only the reductions the target instruction family supports.

## Executable Atom Binding

`atom.make_exec_tma` turns a non-exec atom into an executable atom by attaching
runtime state:

```c
ExecTmaAtom make_exec_tma(NonExecTmaAtom atom,
                          MBarrier barrier,
                          CacheMode cache,
                          Optional<MulticastMask> multicast) {
    require(atom.verified);
    require(barrier.memory_space == SHARED_MEMORY);

    ExecTmaAtom exec;
    exec.atom = atom;
    exec.barrier = barrier;
    exec.cache_mode = cache;
    exec.multicast = multicast;
    return exec;
}
```

Executable TMA lowering increments the barrier transaction count by the number of bytes the transfer will complete.

## Lowering Shape

```c
void lower_tma_load(ExecTmaAtom atom, MemRef dst, Coord coord) {
    require(atom.atom.kind == TMA_LOAD);
    require(dst.memory_space == SHARED_MEMORY);
    require(coord.rank == atom.atom.descriptor.rank);

    prefetch_descriptor_if_requested(atom.atom.descriptor);
    emit_cp_async_bulk_tensor_load(atom.atom.descriptor,
                                   dst,
                                   coord,
                                   atom.barrier,
                                   atom.cache_mode,
                                   atom.multicast);
}

void lower_tma_store(ExecTmaAtom atom, MemRef src, Coord coord) {
    require(atom.atom.kind == TMA_STORE);
    require(src.memory_space == SHARED_MEMORY);
    emit_cp_async_bulk_tensor_store(atom.atom.descriptor, src, coord, atom.cache_mode);
}
```

TMA load completes through an mbarrier — a consumer must wait on the barrier before using the destination tile. TMA store and reduce follow the target's async-bulk ordering rules and must not be reordered across conflicting memory effects.

## Descriptor Mutation

Device-side descriptor mutation is limited. Expose dedicated operations for allowed changes rather than a general byte write:

```c
void update_tma_descriptor(TmaDescriptor *desc, TmaUpdate update) {
    switch (update.kind) {
    case UPDATE_BASE_POINTER:
        desc->base = update.base;
        break;
    case UPDATE_DIM:
        desc->shape[update.axis] = update.value;
        break;
    case UPDATE_STRIDE:
        require(update.axis > 0);
        desc->strides[update.axis] = update.value;
        break;
    default:
        fail("TMA descriptor field is not device-mutable");
    }
}
```

## If You Know CUTLASS (open source) — cross-walk

Coming from CUTLASS Hopper/Blackwell TMA usage:

| CUTLASS C++ | tileiras IR (`cute_nvgpu`) |
|---|---|
| `cuTensorMapEncodeTiled(&tmap, ...)` (host-side, runtime API) | `nv_tileas.make_tiled_tma_desc` op materialising a `!tma_descriptor_tiled` typed value |
| `cuTensorMapEncodeIm2col(&tmap, ...)` | `nv_tileas.make_tiled_tma_desc` with im2col mode → `!tma_descriptor_im2col` |
| `cute::SM90_TMA_LOAD::copy(...)` | `cute_nvgpu.atom.tma_load` op (after `make_exec_tma` binding) |
| `cute::SM90_TMA_STORE::copy(...)` | `cute_nvgpu.atom.tma_store` op |
| `cute::SM90_TMA_REDUCE_ADD::copy(...)` | `cute_nvgpu.atom.tma_reduce` with `kind = TMA_REDUCE_ADD` |
| Multicast TMA (`SM90_TMA_LOAD_MULTICAST`) | `tma_load_mode` attribute on the partition op |
| `cute::prefetch_tma_descriptor(tmap)` | `cute_nvgpu.prefetch_tma_desc` op |
| `mbarrier::arrive_and_expect_tx(mbar, bytes)` paired with TMA | barrier operand + `expect_tx` attribute on the executable TMA op |

The structural difference: in CUTLASS the descriptor is an opaque `CUtensorMap` blob bound at runtime. Tileiras carries rank, element width, swizzle mode, box shape, and stride layout as typed IR attributes the partition verifier (`sub_17A08D0`) re-checks before each TMA op lowers. Device-side mutation is restricted to base pointer, per-axis dimension, and non-leading stride (see Descriptor Mutation above) — the same surface the hardware allows, exposed through dedicated ops rather than raw byte writes.

## Worked Example

```mlir
%desc = nv_tileas.make_tiled_tma_desc %tensor, %layout
    shapes(%m, %n, %k) strides(%sn, %sk) paddings()
    {mode = #cute_nvgpu.tma_load_mode<tiled>,
     elementBitWidth = 16} : !cute_nvgpu.tma_descriptor_tiled

%atom = cute_nvgpu.atom.non_exec_tiled_tma_load %desc, %tile, %cta_map
    {num_multicast = 1}

%exec = cute_nvgpu.atom.make_exec_tma %atom, %mbar
    {cache_mode = #cute_nvgpu.cache_mode<cg>}

cute_nvgpu.atom.tma_load %exec, %smem_tile, %coord
    {allow_tma = true, inBounds = true}
```

After lowering the executable load becomes a `cp.async.bulk.tensor`-style op with descriptor, coordinate, destination, barrier, and optional cache or multicast modifiers.

## Invariants

- TMA rank is between one and five.
- Descriptor pointers are aligned to the hardware descriptor requirement.
- Composed layouts are rejected where the descriptor builder needs a plain
  static layout.
- Shared-memory layouts use supported swizzle modes.
- Global and shared layouts agree with the partitioner and CTA value map.
- Descriptor base, dimensions, and strides are the only mutable device fields.
- TMA load completion is ordered through an mbarrier.
- Im2col and multicast modes are architecture-gated.

## Reimplementation Checklist

1. Model descriptors as typed records with explicit fields.
2. Verify descriptor rank, element width, box sizes, strides, and swizzle.
3. Represent non-exec TMA atoms separately from executable atoms.
4. Bind mbarrier, cache, and multicast state only after partitioning verifies.
5. Lower descriptor mutation through field-specific operations.
6. Keep TMA rank and im2col/multicast feature gates target-aware.
