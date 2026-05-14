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

The TMA atom family rooted at `cute_nvgpu.tma_partition` routes every executable and non-exec TMA atom through one partition op — the canonical place where descriptor shape, transfer mode, multicast cardinality, and reduce kind are validated together. The partition verifier enforces eleven invariants on every TMA partition op and, on success, returns a packed result record per partitioned tile.

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

The partition verifier returns one packed `TmaPartitionResult` per partitioned tile into a `SmallVector` owned by the verifier and forwarded to the executable-atom builder. The 24-byte record carries the interned TMA tensor type, the tile element count, a flags word, and the non-exec atom body that downstream lowering consumes.

```c
typedef struct TmaPartitionResult {
    /*+0x00*/ uint64_t   tma_tensor_type;       // interned MLIR Type * (TmaLoad/Store/ReduceAtomType)
    /*+0x08*/ uint32_t   tile_element_count;    // size(canonical_smem) * num_multicast
    /*+0x0C*/ uint16_t   flags;                 // see "Flags Word" below
    /*+0x0E*/ uint8_t    swizzle_mode;          // 0=none, 2=128B, 3=32B/64B blend
    /*+0x0F*/ uint8_t    rank;                  // descriptor rank (1..5)
    /*+0x10*/ uint64_t   non_exec_atom_body;    // interned non-exec atom Attribute *
} TmaPartitionResult;
```

Only the tensor type and atom body get consumed during executable-atom binding. The flags word, swizzle mode, and rank are echoed back so the executable-atom builder and downstream prefetch logic do not have to re-derive them from the descriptor type.

### Flags Word

The 16-bit `flags` word records every property the partition verifier learned about the tile while it was walking the layout — the multicast mode, the im2col shape, the sparsity tier, the two-CTA cooperative bit, and a handful of operand-source bits used to short-circuit later checks. Downstream passes read this word bit-by-bit rather than re-running the partition algorithm.

| Bit | Field | Meaning |
|---:|---|---|
| 0 | `multicast` | tile lowers to a multicast TMA load (W or W128) |
| 1 | `im2col` | tile is im2col-flavoured (rank reduced before transfer) |
| 2 | `im2col_w` | im2col with warp-cooperative offset table |
| 3 | `im2col_w128` | im2col with wide warp-cooperative offset table (128-thread) |
| 4 | `two_cta` | 2-CTA cooperative load; CTA V-map has been folded into the SMEM layout |
| 5 | `sparse` | metadata operand present; sparsity-aware stride walk |
| 6 | `static_smem` | SMEM layout passed the static-shape predicate |
| 7 | `static_vmap` | CTA V-map passed the static-shape predicate |
| 8 | `gmem_int_stride` | GMEM layout passed the integer-stride walk |
| 9 | `smem_int_stride` | SMEM layout passed the integer-stride walk |
| 10 | `shape_equiv` | top-level shape equivalence between SMEM and V-map held |
| 11 | `g_basis_ok` | G-basis computation returned a valid layout |
| 12 | `s2t_descriptor` | result wraps a `get_copy_s2t_smem_desc` view (Blackwell SMEM-to-tmem descriptor) |
| 13 | `prefetch_eligible` | descriptor handle survives prefetch (no per-axis dynamism that would invalidate it) |
| 14 | `reserved` | — |
| 15 | `reserved` | — |

Bits 6 through 13 mirror the predicate checks the eleven-step verifier ran in steps 4 through 7 and 11. Folding the outcomes back into the flags word lets the executable-atom builder skip the equivalent predicates entirely — the partition verifier is the only place where these layout invariants get checked.

## Eleven-Step Partition Verifier

The partition verifier walks eleven invariants in fixed order. Each gate emits a verbatim diagnostic on failure; the strings are part of the user-visible contract and a reimplementation must preserve them byte-for-byte.

| # | Step | Verbatim diagnostic |
|---|---|---|
| 1 | Type gate on the SMEM and GMEM operands | `"invalid operand types, got "` |
| 2 | SMEM layout-kind gate (`LayoutType` or `ComposedLayoutType`) | `"invalid smem layout type, expected LayoutType or ComposedLayoutType, got "` |
| 3 | GMEM layout-kind gate | `"unsupported layout for the GMEM tensor, got "` |
| 4 | Integer-stride walk on both layouts | `"expected the GMEM and SMEM layouts to have integer stride elements, but got "` |
| 5 | SMEM layout must be a swizzle layout | `"expected the SMEM layout to be a swizzle layout, but got "` |
| 6 | SMEM layout and CTA V-map must be static | `"expected the SMEM layout and the CTA V-map to be static, but got "` |
| 7 | Top-level shape equivalence between SMEM and V-map | `"expected top-level shape equivalence between the SMEM layout and the CTA V-map, but got "` |
| 8 | TMA G-basis computation | `"failed to compute the TMA G-basis, got "` |
| 9 | Final TMA layout validity check | `"Computed TMA layout is invalid, got "` |
| 10 | TMA tensor-type construction | `"Failed to construct the TMA tensor type"` |
| 11 | Multicast-count consistency (load variant only) | `"missing or invalid num_multicast for a multicast TMA load"` |

Order matters. The cheap type and structural gates — steps 1 through 6 — run before the more expensive G-basis and layout-product computations in steps 8 and 9. Step 11 is specific to the load variant; the store and reduce variants skip it because TMA store and reduce never take a multicast operand.

A twelfth string `"got num_multicast of "` is emitted as a companion to step 11 when the multicast mode is non-multicast (mode 0 or mode 2) but the supplied `num_multicast` value is not 1. The two error paths share the same `FAIL` label and treat the pair as one diagnostic: a missing or zero multicast count for a multicast mode, or a non-unit count for a non-multicast mode.

Treat only the descriptor base pointer, per-axis dimension sizes, and non-leading strides as device-mutable. Rank, element type, swizzle, multicast count, and mode are descriptor-construction facts and cannot change once the partition op has verified.

## Worked Example: Rank-6 Rejection

A TMA descriptor builder consuming a rank-6 input lands on step 1 of the partition verifier. The SMEM and GMEM types print as a rank-6 layout, which is outside the accepted `LayoutType` / `ComposedLayoutType` union the partition core requires, and the diagnostic chain emits the verbatim ladder shown below before the verifier returns failure.

```mlir
// Input op — rank 6 is one above the TMA hardware cap.
%bad = cute_nvgpu.atom.non_exec_tiled_tma_load
       %desc_r6, %tile_r6, %cta_map_r6 : !cute_nvgpu.tma_descriptor_tiled
```

```text
error: invalid operand types, got !cute.layout<(a,b,c,d,e,f),...>, !cute.layout<(a,b,c,d,e,f),...>, and !cute.layout<...>
```

The rank-6 tile prints into the first `<smem_ty>` slot, the rank-6 GMEM type into the second `<gmem_ty>` slot, and the CTA V-map into the trailing `<v_map_ty>` slot. The verifier prints all three because the failing condition is a *combination* — the type gate runs on the trio as a unit, so the diagnostic must show every operand that participated.

A stride-4-byte (not 16-byte-aligned) input fails one step later. Steps 1 through 3 pass because the layout kinds are accepted; step 4 walks the GMEM stride tuple, finds a non-integer or below-16-byte entry, and emits:

```text
error: expected the GMEM and SMEM layouts to have integer stride elements, but got !cute.layout<...>, and !cute.layout<...>
```

The two printed layouts are the GMEM and SMEM layouts in the same order step 1 printed them. The trailing `" and "` between the two arguments is the same shared format helper the type-gate diagnostic uses.

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

Device-side descriptor mutation is limited to three fields: the global base pointer, per-axis dimension extents, and non-leading strides (the leading stride is implicit element-size and never written). The atom dialect exposes those three changes as dedicated update kinds rather than as a general byte write, so verification can reject any other mutation at IR construction time:

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

The three update kinds map directly to the `tensormap.replace.tile.{global_address, global_dim, global_stride}` PTX mutator family. The rebind sequence on the device side — acquire fence, address write, `rank` dim writes, `rank-1` stride writes, release fence — and the proxy-fence ordering that pairs each rebind with its `cp.async.bulk.tensor.*` consumer is documented in [TMA Tensormap and cp.async.bulk — TMA Descriptor Mutators](../../codegen/tma-tensormap-and-cp-async-bulk.md#tma-descriptor-mutators). The descriptor builder above is the partition-time view; the atom-lowering page covers how the runtime side issues those three mutators in the contractually mandated order.

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

The structural difference: in CUTLASS the descriptor is an opaque `CUtensorMap` blob bound at runtime. Tileiras carries rank, element width, swizzle mode, box shape, and stride layout as typed IR attributes the partition verifier re-checks before each TMA op lowers. Device-side mutation is restricted to base pointer, per-axis dimension, and non-leading stride (see Descriptor Mutation above) — the same surface the hardware allows, exposed through dedicated ops rather than raw byte writes.

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

## Cross-References

[Mode Pattern Verifiers — Swizzle Legality](mode-pattern-verifiers.md#swizzle-legality) documents the swizzle-legality, [UMMA Canonical Layout Verifier](mode-pattern-verifiers.md#umma-canonical-layout-verifier), and [tcgen05.mma Kind-Word Verifier](mode-pattern-verifiers.md#tcgen05mma-kind-word-verifier) verifiers that the TMA partition core composes with. [SM Tier Roster and Copy Atom Registry — Atom TypeID Registry](sm-tier-roster-and-copy-atom-registry.md#atom-typeid-registry) covers the SM90/SM100/SM120 atom interfaces TMA atoms implement. [cute Atom Builders and Desugar — Kernel-entry ABI](../cute/atom-builders-and-desugar.md#kernel-entry-abi) covers the kernel-entry ABI that hoists TMA descriptors as `.param` constant-space arguments.

