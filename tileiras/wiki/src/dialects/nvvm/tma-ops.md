# NVVM TMA Ops

## Abstract

`nvvm.cp.async.bulk.*` covers the Hopper Tensor Memory Accelerator (TMA) surface: tile loads from global to shared, tile stores from shared to global, prefetches, reductions, group commit / wait, and the descriptor-mutation helpers that backfill an in-SMEM `CUtensorMap`. The 38 ops in this family enumerate every legal combination of mode (`tile` vs `im2col` vs `im2col_w` vs `im2col_w_128`), direction, cache-hint presence, multicast presence, and tensor rank.

TMA descriptors live in global memory as 128-byte `CUtensorMap` structs encoded by the CUDA driver. The device-side ops in this family consume the descriptor as an opaque global pointer, with cache-hint and multicast attributes wiring into optional intrinsic operand slots.

## Op Roster

| Sub-family | Count | Mnemonic stem |
|---|---:|---|
| Tile load (global → shared via cluster) | 5 | `nvvm.cp.async.bulk.tensor.{1..5}d.shared.cluster.global.tile` |
| Tile load (im2col) | 4 | `nvvm.cp.async.bulk.tensor.{3..5}d.shared.cluster.global.im2col` |
| Tile load (im2col_w / im2col_w_128) | 4 | `nvvm.cp.async.bulk.tensor.{3..5}d.shared.cluster.global.im2col_w[.128]` |
| Tile store (shared → global) | 5 | `nvvm.cp.async.bulk.tensor.{1..5}d.global.shared.cta.tile` |
| Reduce | 5 | `nvvm.cp.async.bulk.tensor.{1..5}d.global.shared.cta.tile.reduce` |
| Prefetch | 5 | `nvvm.cp.async.bulk.tensor.{1..5}d.tile.prefetch` |
| Group control | 2 | `nvvm.cp.async.bulk.commit.group`, `nvvm.cp.async.bulk.wait.group` |
| Descriptor copy / replace | 8 | `nvvm.tensormap.cp.async.shared`, `nvvm.tensormap.replace.tile.{global_address,box_dim,element_stride,...}` |

Variants are spelled out explicitly rather than parameterised over `rank` and `mode` attributes — the dialect prefers one op per intrinsic and one intrinsic per PTX instruction.

## Operand Tables

### `nvvm.cp.async.bulk.tensor.{N}d.shared.cluster.global.tile`

| Position | Name | Type | Notes |
|---|---|---|---|
| operand 0 | `dstAddr` | `ptr addrspace(3)` | SMEM destination tile origin |
| operand 1 | `tensorMap` | `ptr` (global, opaque) | 128-byte `CUtensorMap` pointer |
| operands 2..N+1 | `coords` | variadic `i32`, rank N | tile origin in tensor space |
| operand N+2 | `barrier` | `ptr addrspace(3)` | mbarrier slot for expect-tx completion |
| operand N+3 | `multicastMask` | optional `i16` | cluster multicast bitmap (operand slot 6+rank) |
| operand N+4 | `cacheHint` | optional `i64` | L2 cache hint (operand slot 7+rank) |
| attribute | `cacheHintEnable` | `UnitAttr` | gates the `.L2::cache_hint` modifier |
| attribute | `multicastEnable` | `UnitAttr` | gates the `.multicast` modifier |
| attribute | `mode` | enum `tma_load_mode` | `tile` / `im2col` / `im2col_w` / `im2col_w_128` |

The two `UnitAttr`s gate the corresponding optional operand. When `cacheHintEnable` is absent the `cacheHint` operand position is left empty in the LLVM intrinsic call; when present the operand must be supplied. The same pattern applies to `multicastEnable` and `multicastMask`. See [Lowering: nvgpu / gpu to NVVM](../../lowering/nvgpu-and-gpu-to-nvvm.md#tma-async-load-operand-mapping) for the operand-slot mapping the nvgpu-to-nvvm rewriter performs.

### `nvvm.cp.async.bulk.tensor.{N}d.global.shared.cta.tile`

| Position | Name | Type | Notes |
|---|---|---|---|
| operand 0 | `tensorMap` | `ptr` (global, opaque) | 128-byte `CUtensorMap` |
| operands 1..N | `coords` | variadic `i32`, rank N | tile origin |
| operand N+1 | `srcAddr` | `ptr addrspace(3)` | SMEM source tile |
| operand N+2 | `cacheHint` | optional `i64` | L2 cache hint |
| attribute | `cacheHintEnable` | `UnitAttr` | gates the `.L2::cache_hint` modifier |

No barrier — the producer issues the store and continues; the consumer side observes completion via `cp.async.bulk.wait.group`.

### `nvvm.cp.async.bulk.tensor.{N}d.global.shared.cta.tile.reduce`

| Position | Name | Type | Notes |
|---|---|---|---|
| operand 0 | `tensorMap` | `ptr` (global) | `CUtensorMap` |
| operands 1..N | `coords` | variadic `i32`, rank N | tile origin |
| operand N+1 | `srcAddr` | `ptr addrspace(3)` | SMEM source |
| operand N+2 | `cacheHint` | optional `i64` | L2 cache hint |
| attribute | `redop` | enum `tma_redux_kind` | `add` / `min` / `max` / `inc` / `dec` / `and` / `or` / `xor` |

### `nvvm.cp.async.bulk.tensor.{N}d.tile.prefetch`

| Position | Name | Type | Notes |
|---|---|---|---|
| operand 0 | `tensorMap` | `ptr` (global) | `CUtensorMap` |
| operands 1..N | `coords` | variadic `i32`, rank N | tile origin |
| attribute | `mode` | enum `load_mode` | `tile` / `im2col` (matches the load form) |
| attribute | `cacheHintEnable` | `UnitAttr` (optional) | gates a cache-hint operand |

### `nvvm.cp.async.bulk.commit.group` / `nvvm.cp.async.bulk.wait.group`

| Position | Name | Type | Notes |
|---|---|---|---|
| (`wait.group`) operand 0 | `groupDepth` | `i32` | number of older groups to keep in flight |
| (`commit.group`) | — | — | no operands |

### `nvvm.tensormap.cp.async.shared`

| Position | Name | Type | Notes |
|---|---|---|---|
| operand 0 | `dst` | `ptr addrspace(3)` | SMEM destination descriptor mailbox |
| operand 1 | `src` | `ptr` (global) | source descriptor |

### `nvvm.tensormap.replace.tile.global_address` (and `.box_dim`, `.element_stride`, `.box_corner`, `.elem_type`, `.swizzle`, `.fill`)

| Position | Name | Type | Notes |
|---|---|---|---|
| operand 0 | `descriptor` | `ptr addrspace(3)` | SMEM-resident descriptor being mutated |
| operand 1 | `value` | `i64` / `i32` / enum | replacement value for the named field |
| attribute | `ord` | `i32` | rank index for `box_dim` / `element_stride` |

## LLVM Intrinsic Mapping

| Op | LLVM intrinsic |
|---|---|
| `nvvm.cp.async.bulk.tensor.2d.shared.cluster.global.tile` | `llvm.nvvm.cp.async.bulk.tensor.2d.shared.cluster.global.tile` |
| `nvvm.cp.async.bulk.tensor.3d.shared.cluster.global.im2col` | `llvm.nvvm.cp.async.bulk.tensor.3d.shared.cluster.global.im2col` |
| `nvvm.cp.async.bulk.tensor.2d.global.shared.cta.tile` | `llvm.nvvm.cp.async.bulk.tensor.2d.global.shared.cta.tile` |
| `nvvm.cp.async.bulk.tensor.2d.global.shared.cta.tile.reduce` | `llvm.nvvm.cp.async.bulk.tensor.2d.global.shared.cta.tile.reduce.{redop}` |
| `nvvm.cp.async.bulk.tensor.2d.tile.prefetch` | `llvm.nvvm.cp.async.bulk.tensor.prefetch.2d.tile` |
| `nvvm.cp.async.bulk.commit.group` | `llvm.nvvm.cp.async.bulk.commit.group` |
| `nvvm.cp.async.bulk.wait.group` | `llvm.nvvm.cp.async.bulk.wait.group` |
| `nvvm.tensormap.cp.async.shared` | `llvm.nvvm.cp.async.bulk.tensor.shared.cluster.tensormap.cta` |
| `nvvm.tensormap.replace.tile.global_address` | `llvm.nvvm.tensormap.replace.tile.global.address` |
| `nvvm.tensormap.replace.tile.box_dim` | `llvm.nvvm.tensormap.replace.tile.box_dim` |
| `nvvm.tensormap.replace.tile.element_stride` | `llvm.nvvm.tensormap.replace.tile.element.stride` |
| `nvvm.tensormap.replace.tile.elem_type` | `llvm.nvvm.tensormap.replace.tile.elemtype` |
| `nvvm.tensormap.replace.tile.swizzle` | `llvm.nvvm.tensormap.replace.tile.swizzle` |

The reduction intrinsic concatenates the `redop` name into the intrinsic ID; eight distinct intrinsics exist per rank.

## PTX Templates

```text
cp.async.bulk.tensor.{N}d.shared::cluster.global.tile.mbarrier::complete_tx::bytes
    [%dst], [%tmap, {%c0, %c1, ..., %c{N-1}}], [%mbar];

cp.async.bulk.tensor.{N}d.shared::cluster.global.tile.mbarrier::complete_tx::bytes.multicast::cluster
    [%dst], [%tmap, {%c0, ..., %c{N-1}}], [%mbar], %multicastMask;

cp.async.bulk.tensor.{N}d.shared::cluster.global.tile.mbarrier::complete_tx::bytes.L2::cache_hint
    [%dst], [%tmap, {%c0, ..., %c{N-1}}], [%mbar], %hint;

cp.async.bulk.tensor.{N}d.shared::cluster.global.im2col.mbarrier::complete_tx::bytes
    [%dst], [%tmap, {%c0, ..., %c{N-1}}], [%mbar];

cp.async.bulk.tensor.{N}d.global.shared::cta.tile
    [%tmap, {%c0, ..., %c{N-1}}], [%src];

cp.async.bulk.tensor.{N}d.global.shared::cta.tile.{redop}.bulk_group
    [%tmap, {%c0, ..., %c{N-1}}], [%src];

cp.async.bulk.prefetch.tensor.{N}d.global.tile
    [%tmap, {%c0, ..., %c{N-1}}];

cp.async.bulk.commit_group;
cp.async.bulk.wait_group N;

tensormap.cp.async.shared::cta.bulk_group [%dst], [%src];
tensormap.replace.tile.global_address [%tmap], %addr;
tensormap.replace.tile.box_dim.k       [%tmap], %dim;
tensormap.replace.tile.element_stride.k [%tmap], %stride;
tensormap.replace.tile.elemtype        [%tmap], %elt_id;
tensormap.replace.tile.swizzle         [%tmap], %mode;
```

The `multicast` and `L2::cache_hint` suffix variants are picked per template by the presence flags. The reductions enumerate `redop ∈ {add, min, max, inc, dec, and, or, xor}` as separate ops, one per PTX modifier.

## Inline-PTX Variants

A few TMA paths reach PTX through `llvm.inline_asm` because no LLVM intrinsic exists at the snapshot revision Tileiras tracks. The most common is the im2col cache-hint store variant:

```text
asm template: "cp.async.bulk.tensor.{N}d.global.shared::cta.im2col.bulk_group.L2::cache_hint
                  [%tmap, {%c0, ..., %c{N-1}}], [%src], %hint;"
constraints : "l,l,r,r,r,...,l"
```

`l` is the 64-bit descriptor pointer and the cache-hint operand; `r` is each 32-bit coordinate; the source SMEM pointer is also `l` (an opaque pointer). Tileiras retains the upstream constraint string verbatim; reimplementers must not rearrange operand order, because the NVPTX backend matches positional registers against the template's `%` slots.

## Per-Arch Availability

| Op family | SM floor | `ptx_min` |
|---|---|---|
| Tile load / store / reduce | sm_90 | 8.0 |
| Im2col forms | sm_90 | 8.0 |
| Multicast / cluster forms | sm_90 (`sm_90a` for cluster mode) | 8.0 |
| Prefetch | sm_90 | 8.0 |
| Group commit / wait | sm_90 | 8.0 |
| `tensormap.cp.async.shared` | sm_90 | 8.3 |
| `tensormap.replace.*` | sm_90 | 8.3 |

Blackwell extends the cache-hint and OOB-fill modes but keeps the same op surface and the same intrinsic shape; verification accepts `sm_100+` for every op in the family.

## Verifier Invariants

- `tensorMap` is a global-memory pointer; the descriptor itself is opaque.
- `dstAddr` (loads) is in addr-space 3; `srcAddr` (stores) is in addr-space 3.
- Coordinate operand count equals the rank in the op mnemonic.
- `multicastEnable` and `cacheHintEnable` agree with the operand list: presence of the attribute requires the operand to be supplied.
- For reductions, `redop` is one of the eight legal values.
- For im2col forms, the rank is 3, 4, or 5; lower ranks are rejected.
