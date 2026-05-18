# nvgpu Dialect Overview

## Abstract

`nvgpu` is the bridge dialect between MLIR's generic `gpu` dialect and NVPTX-specific [`nvvm`](../nvvm/overview.md). It names the NVIDIA kernel patterns that `gpu` cannot express — warp shuffle, MMA and WGMMA, `cp.async`, `mbarrier`, TMA — without committing yet to a concrete NVVM intrinsic. Tileiras links the upstream dialect unchanged. `cute_nvgpu` feeds it from above; [`convert-nvgpu-to-nvvm`](../../lowering/nvgpu-and-gpu-to-nvvm.md) drains it from below.

About thirty ops live here. The conversion pass installs one `OpConversionPattern` per op and rewrites the module in a single sweep, each pattern emitting a small fixed body of `nvvm.*` ops — or, for a few exception cases, expanded `memref` / `llvm` / `llvm.inline_asm`. The pass mnemonic is `convert-nvgpu-to-nvvm`; in the O3 pipeline it runs immediately after the broad `convert-to-llvm` step and before `convert-vector-to-llvm`, `arith-expand`, and `convert-memref-to-llvm` (see [Pass List by Optimization Level — O3](../../pipeline/full-pass-list-by-opt-level.md#o3-full-pipeline)), so by the time it fires every operand is already in LLVM-dialect or memref form.

## Position in the Cascade

```text
cute_nvgpu
    |
    | lower architecture atoms into stock GPU operations
    v
nvgpu
    |
    | convert-nvgpu-to-nvvm: ~30 patterns, one sweep
    v
nvvm
    |
    | translate to LLVM IR and the NVPTX backend
    v
PTX
```

[`cute_nvgpu`](../cute_nvgpu/overview.md) ops still speak SM-tier vocabulary — TMA atoms, WGMMA atoms, Blackwell tensor-memory operations. `nvgpu` strips the source-level atom naming and re-presents the same behaviour over MLIR memrefs, vectors, descriptors, barrier groups, and async tokens. That makes the NVVM conversion mechanical: every `nvgpu` op below has a fixed `nvvm` (or `llvm.inline_asm`) lowering.

## Op Roster

`populateNVGPUToNVVMConversionPatterns` installs one `OpConversionPattern` per op. Tileiras links the upstream populator unchanged. The rewriter callbacks branch on source memory space to pick the generic or `.shared` form of the `mbarrier` and `cp.async` intrinsics — address space 3 always selects `.shared`.

| nvgpu op | NVVM op(s) emitted |
|---|---|
| `nvgpu.device_async_copy` | `nvvm.cp.async.shared.global` |
| `nvgpu.device_async_create_group` | `nvvm.cp.async.commit.group` |
| `nvgpu.device_async_wait` | `nvvm.cp.async.wait.group` |
| `nvgpu.mbarrier.create` | (no `nvvm.*`; `memref.global` + `memref.get_global`) |
| `nvgpu.mbarrier.init` | `nvvm.mbarrier.init` |
| `nvgpu.mbarrier.init.shared` | `nvvm.mbarrier.init.shared` |
| `nvgpu.mbarrier.arrive` | `nvvm.mbarrier.arrive[.shared]` |
| `nvgpu.mbarrier.arrive.nocomplete` | `nvvm.mbarrier.arrive.nocomplete[.shared]` |
| `nvgpu.mbarrier.arrive.expect_tx` | `nvvm.mbarrier.arrive.expect_tx[.shared]` |
| `nvgpu.mbarrier.test.wait` | `nvvm.mbarrier.test.wait[.shared]` |
| `nvgpu.mbarrier.try_wait.parity` | `nvvm.mbarrier.try_wait.parity[.shared]` |
| `nvgpu.mbarrier.inval` | `nvvm.mbarrier.inval[.shared]` |
| `nvgpu.tma.async.load` | `nvvm.cp.async.bulk.tensor.shared.global` |
| `nvgpu.tma.async.store` | `nvvm.cp.async.bulk.tensor.global.shared` |
| `nvgpu.tma.prefetch.descriptor` | `nvvm.prefetch.tensormap` |
| `nvgpu.tma.fence.descriptor` | `nvvm.fence.proxy.acquire` |
| `nvgpu.tma.async.reduce` | `nvvm.cp.async.bulk.tensor.reduce` |
| `nvgpu.tma.create.descriptor` | `llvm.alloca` + GEP/store sequence + `llvm.call @cuTensorMapEncodeTiled` |
| `nvgpu.tensormap.create.descriptor` | `nvvm.tensormap.cp.async.shared` + `tensormap.replace.*` |
| `nvgpu.tensormap.update.global_address` | `nvvm.tensormap.replace.global_address` |
| `nvgpu.tensormap.update.box_dim` | `nvvm.tensormap.replace.box_dim` |
| `nvgpu.tensormap.update.element_stride` | `nvvm.tensormap.replace.element_stride` |
| `nvgpu.warp.execute_on_lane_0` | `nvvm.shfl.sync` + conditional region |
| `nvgpu.warpgroup.descriptor` (generate) | integer `shl`/`or` chain producing a 64-bit value (see [WGMMA descriptor bit layout](../cute_nvgpu/mma-atoms-sm70-120.md#smem-descriptor-construction)) |
| `nvgpu.warpgroup.mma` | `nvvm.wgmma.fence.aligned` + N× `nvvm.wgmma.mma_async.sync.aligned` + `nvvm.wgmma.commit.group.sync.aligned` + `nvvm.wgmma.wait.group.sync.aligned` |
| `nvgpu.warpgroup.mma.store` | per-thread `llvm.store` decomposition of the accumulator |
| `nvgpu.warpgroup.mma.init.accumulator` | `llvm.mlir.undef` (or zero) accumulator aggregate |
| `nvgpu.mma.sync` | `nvvm.wmma.mma.sync.aligned` (sm_70..sm_89) or `nvvm.wgmma.mma_async.sync.aligned` (sm_90+) |
| `nvgpu.mma.sp.sync` | `llvm.inline_asm` with `mma.sp.sync.aligned.m...` template |
| `nvgpu.ldmatrix` | `nvvm.ldmatrix.sync.aligned` + register repack |
| `nvgpu.stmatrix` | `nvvm.stmatrix.sync.aligned` (when available) or `llvm.inline_asm` |
| `nvgpu.rcp` | `nvvm.rcp.approx.ftz.f` family or libdevice call |
| `nvgpu.cvt_fpext` / `nvgpu.cvt_fptrunc` | `nvvm.cvt.packfloat.f32` family |
| `nvgpu.fma.packed.f32x2` / `nvgpu.mul.packed.f32x2` | packed `nvvm.fma.rn.f32x2` / `nvvm.mul.f32x2` |

Patterns are registered at `benefit = 1`. The 64-bit values consumed by `nvgpu.warpgroup.mma`'s `descriptorA` / `descriptorB` operands are the same SMEM descriptor words that flow through `cute_nvgpu` MMA atoms — the [canonical bitfield decode is on the cute_nvgpu MMA atoms page](../cute_nvgpu/mma-atoms-sm70-120.md#smem-descriptor-construction).

## Operand and Attribute Tables

The tables below pin every op family in the dialect to its operand list, attribute list, result list, and the NVVM rewrite the conversion pattern emits. SM gating lives in the per-arch availability table further down.

### `nvgpu.device_async_copy`

| Position | Name | Type | Notes |
|---|---|---|---|
| operand 0 | `dst` | `memref<...>` in addr-space 3 (shared) | minor dim must be unit-stride |
| operand 1 | `src` | `memref<...>` in addr-space 1 (global) | minor dim must be unit-stride |
| operand 2 | `dstIndices` | variadic `index` | rank == dst rank |
| operand 3 | `srcIndices` | variadic `index` | rank == src rank |
| operand 4 | `srcElements` | optional `index` | runtime element count for predicated case |
| attribute | `dstElements` | `i64` (IntegerAttr) | element count per lane; 4, 8, 16 |
| attribute | `bypassL1` | optional `UnitAttr` | selects `.cg` cache modifier |
| result 0 | `token` | `!nvgpu.device.async.token` | passed to commit / wait |

Rewriter emits `nvvm.cp.async.shared.global` with `cp_size = dstElements * eltbytes` and `cp_modifier = bypassL1 ? cg : ca`.

### `nvgpu.device_async_create_group`

| Position | Name | Type | Notes |
|---|---|---|---|
| operand 0..N | `inputTokens` | variadic `!nvgpu.device.async.token` | tokens to commit as a group |
| result 0 | `groupToken` | `!nvgpu.device.async.token` | feeds `device_async_wait` |

Rewriter emits a single `nvvm.cp.async.commit.group`. Input tokens are erased; the SSA edge survives only as a happens-before constraint.

### `nvgpu.device_async_wait`

| Position | Name | Type | Notes |
|---|---|---|---|
| operand 0 | `asyncDependencies` | `!nvgpu.device.async.token` | group token to wait on |
| attribute | `numGroups` | optional `i32` | passed verbatim as the `wait_group` immediate |

Rewriter emits `nvvm.cp.async.wait.group N` where `N = numGroups` (default 0).

### `nvgpu.mbarrier.create`

| Position | Name | Type | Notes |
|---|---|---|---|
| attribute | `numBarriers` | `i64` | requested mbarrier count in the group |
| result 0 | `barriers` | `!nvgpu.mbarrier.group` | wraps the shared-memory slot |

Rewriter emits no `nvvm.*` op. It generates a `memref.global "private" @__mbarrier ... : memref<NxNumBarrier x i64, 3>` and a `memref.get_global` returning a base pointer.

### `nvgpu.mbarrier.init.shared` (alias of `mbarrier.init` over addr-space 3)

| Position | Name | Type | Notes |
|---|---|---|---|
| operand 0 | `barriers` | `!nvgpu.mbarrier.group` | the group from `mbarrier.create` |
| operand 1 | `mbarId` | `index` | barrier index within the group |
| operand 2 | `count` | `index` | participant count |
| attribute | (none) | — | the address space drives the `.shared` selector |

Rewriter emits `nvvm.mbarrier.init.shared` against the GEP-resolved slot address.

### `nvgpu.mbarrier.arrive`

| Position | Name | Type | Notes |
|---|---|---|---|
| operand 0 | `barriers` | `!nvgpu.mbarrier.group` | wraps the shared-memory slot |
| operand 1 | `mbarId` | `index` | barrier index within the group |
| result 0 | `token` | `!nvgpu.mbarrier.token` | feeds `mbarrier.test.wait` |

Rewriter emits `nvvm.mbarrier.arrive` or `nvvm.mbarrier.arrive.shared` based on the slot's address space.

### `nvgpu.mbarrier.arrive.expect_tx`

| Position | Name | Type | Notes |
|---|---|---|---|
| operand 0 | `barriers` | `!nvgpu.mbarrier.group` | mbarrier slot |
| operand 1 | `mbarId` | `index` | barrier index within the group |
| operand 2 | `txCount` | `index` | expect-tx byte count |

Rewriter emits `nvvm.mbarrier.arrive.expect_tx[.shared]`. No SSA result; the side effect is on the shared-memory mbarrier slot.

### `nvgpu.mbarrier.try_wait.parity`

| Position | Name | Type | Notes |
|---|---|---|---|
| operand 0 | `barriers` | `!nvgpu.mbarrier.group` | mbarrier slot |
| operand 1 | `mbarId` | `index` | barrier index within the group |
| operand 2 | `phase` | `index` | phase parity (`0` or `1`) |
| operand 3 | `ticks` | `index` | retry budget |

Rewriter emits `nvvm.mbarrier.try_wait.parity.shared` returning an `i1` polled in a loop.

### `nvgpu.mbarrier.inval`

| Position | Name | Type | Notes |
|---|---|---|---|
| operand 0 | `barriers` | `!nvgpu.mbarrier.group` | mbarrier slot |
| operand 1 | `mbarId` | `index` | barrier index within the group |

Rewriter emits `nvvm.mbarrier.inval[.shared]`.

### `nvgpu.tma.async.load`

| Position | Name | Type | Notes |
|---|---|---|---|
| operand 0 | `dst` | `memref<...>` in addr-space 3 | TMA destination |
| operand 1 | `barrier` | `!nvgpu.mbarrier.group` | arrives expect-tx on completion |
| operand 2 | `tensorMapDescriptor` | `!nvgpu.tensormap.descriptor` | from `tma.create.descriptor` |
| operand 3..7 | `coordinates` | variadic `i32`, rank 1..5 | tile origin in tensor space |
| operand 8 | `multicastMask` | optional `i16` | cluster multicast bitmap |
| operand 9 | `l2CacheHint` | optional `i64` | maps to `.L2::cache_hint` |
| attribute | `predicate` | optional `i1` | gated TMA issue |

Rewriter emits a single `nvvm.cp.async.bulk.tensor.shared.global`. See [Lowering: TMA Async Load — Operand Mapping](../../lowering/nvgpu-and-gpu-to-nvvm.md#operand-mapping-rank-n) for the operand-slot mapping.

### `nvgpu.tma.async.store`

| Position | Name | Type | Notes |
|---|---|---|---|
| operand 0 | `src` | `memref<...>` in addr-space 3 | TMA source tile in SMEM |
| operand 1 | `tensorMapDescriptor` | `!nvgpu.tensormap.descriptor` | global tensor map |
| operand 2..6 | `coordinates` | variadic `i32`, rank 1..5 | tile origin in tensor space |
| operand 7 | `l2CacheHint` | optional `i64` | maps to `.L2::cache_hint` |
| attribute | `predicate` | optional `i1` | gated TMA issue |

Rewriter emits `nvvm.cp.async.bulk.tensor.global.shared`. No barrier — the producer side does not wait.

### `nvgpu.tma.prefetch.descriptor`

| Position | Name | Type | Notes |
|---|---|---|---|
| operand 0 | `tensorMapDescriptor` | `!nvgpu.tensormap.descriptor` | descriptor to prefetch |

Rewriter emits `nvvm.prefetch.tensormap [%tmap]`.

### `nvgpu.tma.fence.descriptor`

| Position | Name | Type | Notes |
|---|---|---|---|
| operand 0 | `tensorMapDescriptor` | `!nvgpu.tensormap.descriptor` | descriptor being made visible |

Rewriter emits `nvvm.fence.proxy.acquire.sync.cluster` — the proxy-acquire fence that the WGMMA descriptor consumer needs.

### `nvgpu.tma.async.reduce`

| Position | Name | Type | Notes |
|---|---|---|---|
| operand 0 | `src` | `memref<...>` in addr-space 3 | SMEM source tile |
| operand 1 | `tensorMapDescriptor` | `!nvgpu.tensormap.descriptor` | global tensor map |
| operand 2..6 | `coordinates` | variadic `i32`, rank 1..5 | tile origin in tensor space |
| operand 7 | `l2CacheHint` | optional `i64` | L2 hint |
| attribute | `redop` | enum `tma_redux_kind` | `add` / `min` / `max` / `inc` / `dec` / `and` / `or` / `xor` |

Rewriter emits `nvvm.cp.async.bulk.tensor.reduce` with `red_op` decoded from the attribute.

### `nvgpu.tma.create.descriptor`

| Position | Name | Type | Notes |
|---|---|---|---|
| operand 0 | `tensor` | `memref<...>` | global tensor whose layout the descriptor encodes |
| operand 1..N | `boxDimensions` | variadic `index` | TMA tile shape per rank |
| attribute | `swizzle` | enum `tma_swizzle` | `none` / `32B` / `64B` / `128B` |
| attribute | `l2Promotion` | enum `tma_l2_promotion` | `none` / `64B` / `128B` / `256B` |
| attribute | `oobFill` | enum `tma_oob_fill` | `none` / `nan` |
| attribute | `interleave` | enum `tma_interleave` | `none` / `16B` / `32B` |
| result 0 | `descriptor` | `!nvgpu.tensormap.descriptor` | global-memory pointer to the 128-byte `CUtensorMap` |

Rewriter emits no `nvvm.*` op. It allocates a 128-byte `CUtensorMap` on the host stack via `llvm.alloca`, fills it through `llvm.getelementptr` + `llvm.store`, and calls `cuTensorMapEncodeTiled`.

### `nvgpu.tensormap.create.descriptor` (device-side replace path)

| Position | Name | Type | Notes |
|---|---|---|---|
| operand 0 | `dst` | `!nvgpu.tensormap.descriptor` in shared | destination mailbox |
| operand 1 | `src` | `!nvgpu.tensormap.descriptor` in global | source descriptor |

Rewriter emits `nvvm.tensormap.cp.async.shared` followed by a sequence of `nvvm.tensormap.replace.*` ops that overwrite the per-field slots of the new descriptor.

### `nvgpu.tensormap.update.global_address` / `box_dim` / `element_stride`

| Position | Name | Type | Notes |
|---|---|---|---|
| operand 0 | `descriptor` | `!nvgpu.tensormap.descriptor` in shared | descriptor being edited |
| operand 1 | `value` | `i64` or `i32` | new field value |
| attribute | `ord` | `i32` | rank index for `box_dim` / `element_stride` |

Each rewrites to the matching `nvvm.tensormap.replace.*` op against the SMEM-resident descriptor.

### `nvgpu.warpgroup.descriptor` (also spelled `warpgroup.generate.descriptor`)

| Position | Name | Type | Notes |
|---|---|---|---|
| operand 0 | `tensor` | `memref<...>` in addr-space 3 | SMEM tile origin |
| attribute | `layout` | enum (`row` / `col`) | matrix layout |
| attribute | `swizzle` | enum (`none` / `32B` / `64B` / `128B`) | SMEM swizzle pattern |
| result 0 | `descriptor` | `!nvgpu.warpgroup.descriptor` | 64-bit SMEM descriptor |

Rewriter packs the descriptor bits inline. The result is a `i64` LLVM value built by an `shl`/`or` chain; the bit layout (`start_addr[14] | lbo[16] | sbo[16] | base_offset[3] | reserved[3] | swizzle_mode[2] | pad[10]`) is documented on the [cute_nvgpu MMA atoms page](../cute_nvgpu/mma-atoms-sm70-120.md#smem-descriptor-construction).

### `nvgpu.warpgroup.mma`

| Position | Name | Type | Notes |
|---|---|---|---|
| operand 0 | `descriptorA` | `!nvgpu.warpgroup.descriptor` | [SMEM descriptor](../cute_nvgpu/mma-atoms-sm70-120.md#smem-descriptor-construction) for A |
| operand 1 | `descriptorB` | `!nvgpu.warpgroup.descriptor` | [SMEM descriptor](../cute_nvgpu/mma-atoms-sm70-120.md#smem-descriptor-construction) for B |
| operand 2 | `matrixC` | `!nvgpu.warpgroup.accumulator` | input accumulator tile |
| attribute | `transposeA` | optional `UnitAttr` | wired into the WGMMA layout enum |
| attribute | `transposeB` | optional `UnitAttr` | wired into the WGMMA layout enum |
| attribute | `waitGroup` | optional `i32` | controls the wait-group depth |
| result 0 | `matrixD` | `!nvgpu.warpgroup.accumulator` | output accumulator tile |

Rewriter expands to the canonical four-op WGMMA sequence: `nvvm.wgmma.fence.aligned`, one `nvvm.wgmma.mma_async.sync.aligned` per accumulator tile, `nvvm.wgmma.commit.group.sync.aligned`, then `nvvm.wgmma.wait.group.sync.aligned waitGroup`. See [WGMMA Emission Protocol — The Four-Op Sequence](../../topics/wgmma-emission-protocol.md#the-four-op-sequence) for the timing rules and accumulator lifetime. It validates GMMA layout up front with the canonical "Not a canonical GMMA_MN Layout" wording lifted from CUTLASS's `gmma.hpp`.

### `nvgpu.warpgroup.mma.store`

| Position | Name | Type | Notes |
|---|---|---|---|
| operand 0 | `matrixD` | `!nvgpu.warpgroup.accumulator` | accumulator to drain |
| operand 1 | `dst` | `memref<...>` in addr-space 3 | SMEM destination tile |

Rewriter decomposes the accumulator into per-thread `llvm.store` operations against the destination memref. No `nvvm.*` op is emitted.

### `nvgpu.warpgroup.mma.init.accumulator`

| Position | Name | Type | Notes |
|---|---|---|---|
| result 0 | `accumulator` | `!nvgpu.warpgroup.accumulator` | zero-valued accumulator |

Rewriter emits `llvm.mlir.zero` (or `llvm.mlir.undef` followed by per-field zero stores) producing the accumulator aggregate.

### `nvgpu.mma.sync`

| Position | Name | Type | Notes |
|---|---|---|---|
| operand 0 | `matrixA` | `vector<...>` register fragment | A operand fragment |
| operand 1 | `matrixB` | `vector<...>` register fragment | B operand fragment |
| operand 2 | `matrixC` | `vector<...>` register fragment | accumulator fragment |
| attribute | `mmaShape` | `ArrayAttr<i64>` of length 3 | `[m, n, k]` |
| attribute | `tf32Enabled` | optional `UnitAttr` | enables `tf32` element-type lowering |
| result 0 | `matrixD` | `vector<...>` register fragment | D = A * B + C |

Rewriter emits `nvvm.wmma.mma.sync.aligned` (Ampere/Ada) or routes through `nvvm.wgmma.mma_async.sync.aligned` (Hopper) based on the active SM.

### `nvgpu.mma.sp.sync`

| Position | Name | Type | Notes |
|---|---|---|---|
| operand 0 | `matrixA` | `vector<...>` register fragment | structurally sparse A operand |
| operand 1 | `matrixB` | `vector<...>` register fragment | dense B operand |
| operand 2 | `matrixC` | `vector<...>` register fragment | accumulator fragment |
| operand 3 | `sparseMetadata` | `vector<2xi16>` | sparse selector word |
| operand 4 | `sparsitySelector` | `i32` | 0 or 1 — selects which packed pair |
| attribute | `mmaShape` | `ArrayAttr<i64>` of length 3 | `[m, n, k]` |
| result 0 | `matrixD` | `vector<...>` register fragment | sparse MMA result |

Rewriter emits `llvm.inline_asm` with the `mma.sp.sync.aligned.m...` template; upstream NVVM exposes no sparse-MMA op in the snapshot tileiras tracks.

### `nvgpu.ldmatrix`

| Position | Name | Type | Notes |
|---|---|---|---|
| operand 0 | `src` | `memref<...>` in addr-space 3 | SMEM tile origin |
| operand 1..N | `indices` | variadic `index` | rank-matched indices |
| attribute | `numTiles` | `i32` | `1`, `2`, or `4` |
| attribute | `transpose` | `UnitAttr` (optional) | selects `.trans` form |
| result 0 | `res` | `vector<NxNxi32>` | repacked register fragment |

Rewriter emits `nvvm.ldmatrix.sync.aligned` returning an `llvm.struct<(i32, i32, ...)>`, then a `pack-struct-into-vector` repack to match the result type.

### `nvgpu.stmatrix`

| Position | Name | Type | Notes |
|---|---|---|---|
| operand 0 | `dst` | `memref<...>` in addr-space 3 | SMEM destination |
| operand 1..N | `indices` | variadic `index` | rank-matched indices |
| operand 2 | `src` | `vector<NxNxi32>` | per-thread fragment |
| attribute | `transpose` | `UnitAttr` (optional) | selects `.trans` form |

Rewriter emits `nvvm.stmatrix.sync.aligned` on sm_90+ targets, or `llvm.inline_asm` with the matching `stmatrix...` template otherwise.

### `nvgpu.warp.execute_on_lane_0`

| Position | Name | Type | Notes |
|---|---|---|---|
| region | body | one block | runs on lane 0 only |
| result 0..N | `results` | any LLVM-typed values | shuffled to every lane after the region |

Rewriter emits a region predicate against `lane == 0`, runs the body, and broadcasts each result with `nvvm.shfl.sync` (`idx`, `0`, `0xffffffff`).

### Packed conversion and arithmetic helpers

| Op | Operands | Result | NVVM emission |
|---|---|---|---|
| `nvgpu.rcp` | `f32` | `f32` | `nvvm.rcp.approx.ftz.f` or libdevice call |
| `nvgpu.cvt_fpext` | packed `i32` of FP4/FP8 | `vector<2xf16>` / `vector<2xf32>` | `nvvm.cvt.packfloat.f32` family |
| `nvgpu.cvt_fptrunc` | `vector<2xf16>` / `vector<2xf32>` | packed `i32` | `nvvm.cvt.packfloat.f32` family |
| `nvgpu.fma.packed.f32x2` | three `vector<2xf32>` | `vector<2xf32>` | `nvvm.fma.rn.f32x2` |
| `nvgpu.mul.packed.f32x2` | two `vector<2xf32>` | `vector<2xf32>` | `nvvm.mul.f32x2` |

Each packed op carries a `rnd` enum (`rn`, `rz`, `rm`, `rp`) and, where applicable, a `packed_kind` enum that selects between MXFP / NVFP packing modes.

## Lowering-Target Table

What each rewriter emits. The middle column gives the concrete NVVM op (or the expanded form when the pattern bypasses NVVM on purpose); the right column is what the NVPTX backend ultimately prints, not anything `nvgpu` itself emits.

| nvgpu op | NVVM op (or expansion) | Final PTX (after NVVM lowering) |
|---|---|---|
| `nvgpu.device_async_copy` | `nvvm.cp.async.shared.global` | `cp.async.{ca,cg}.shared.global [%dst], [%src], N;` |
| `nvgpu.device_async_create_group` | `nvvm.cp.async.commit.group` | `cp.async.commit_group;` |
| `nvgpu.device_async_wait` | `nvvm.cp.async.wait.group` | `cp.async.wait_group N;` |
| `nvgpu.mbarrier.create` | `memref.global "private"` + `memref.get_global` | (no PTX; allocates SMEM slot) |
| `nvgpu.mbarrier.init.shared` | `nvvm.mbarrier.init.shared` | `mbarrier.init.shared.b64 [%mbar], %count;` |
| `nvgpu.mbarrier.arrive` | `nvvm.mbarrier.arrive[.shared]` | `mbarrier.arrive.shared.b64 %tok, [%mbar];` |
| `nvgpu.mbarrier.arrive.expect_tx` | `nvvm.mbarrier.arrive.expect_tx[.shared]` | `mbarrier.arrive.expect_tx.shared.b64 %tok, [%mbar], %tx;` |
| `nvgpu.mbarrier.try_wait.parity` | `nvvm.mbarrier.try_wait.parity.shared` | `mbarrier.try_wait.parity.shared.b64 %p, [%mbar], %ph, %ns;` |
| `nvgpu.mbarrier.inval` | `nvvm.mbarrier.inval[.shared]` | `mbarrier.inval.shared.b64 [%mbar];` |
| `nvgpu.tma.async.load` | `nvvm.cp.async.bulk.tensor.shared.global` | `cp.async.bulk.tensor.{1..5}d.shared::cluster.global.tile.mbarrier::complete_tx::bytes [%dst], [%tmap, {%c0,..}], [%mbar];` |
| `nvgpu.tma.async.store` | `nvvm.cp.async.bulk.tensor.global.shared` | `cp.async.bulk.tensor.{1..5}d.global.shared::cta.tile [%tmap, {%c0,..}], [%src];` |
| `nvgpu.tma.async.reduce` | `nvvm.cp.async.bulk.tensor.reduce` | `cp.async.bulk.tensor.{1..5}d.global.shared::cta.tile.{redop}.bulk_group [%tmap, {%c0,..}], [%src];` |
| `nvgpu.tma.prefetch.descriptor` | `nvvm.prefetch.tensormap` | `prefetch.tensormap [%tmap];` |
| `nvgpu.tma.fence.descriptor` | `nvvm.fence.proxy.acquire.sync.cluster` | `fence.proxy.async.shared::cluster;` |
| `nvgpu.tma.create.descriptor` | `llvm.alloca` + GEP/store sequence + `llvm.call @cuTensorMapEncodeTiled` | (no PTX; host-side encode of a 128-byte `CUtensorMap`) |
| `nvgpu.tensormap.create.descriptor` | `nvvm.tensormap.cp.async.shared` + `tensormap.replace.*` | `tensormap.cp.async.shared::cta.bulk_group [%dst], [%src];` then `tensormap.replace.tile.{global_address,box_dim,elem_stride,...}.[%tmap], ...;` |
| `nvgpu.warp.execute_on_lane_0` | `nvvm.shfl.sync` + conditional region | `shfl.sync.idx.b32 %r, %v, 0, 0x1f, 0xffffffff;` |
| `nvgpu.warpgroup.descriptor` | integer `shl`/`or` chain — no NVVM op | (no PTX; the 64-bit [SMEM descriptor](../cute_nvgpu/mma-atoms-sm70-120.md#smem-descriptor-construction) is built by ordinary integer ops; the PTX side sees the materialised `b64` value) |
| `nvgpu.warpgroup.mma` | `nvvm.wgmma.fence.aligned` → N× `nvvm.wgmma.mma_async.sync.aligned` → `nvvm.wgmma.commit.group.sync.aligned` → `nvvm.wgmma.wait.group.sync.aligned` | `wgmma.fence.sync.aligned;` then `wgmma.mma_async.sync.aligned.m64nXkY.f32.{f16,bf16,e4m3,e5m2}.{f16,bf16,e4m3,e5m2} {...}, %da, %db, p, 1, 1, %la, %lb;` then `wgmma.commit_group.sync.aligned;` then `wgmma.wait_group.sync.aligned N;` |
| `nvgpu.warpgroup.mma.store` | per-thread `llvm.store` decomposition | `st.shared.b32 [%dst+off], %r;` per fragment lane |
| `nvgpu.mma.sync` | `nvvm.wmma.mma.sync.aligned` (sm_70..sm_89) or `nvvm.wgmma.mma_async.sync.aligned` (sm_90+) | `mma.sync.aligned.m16n8kK.{row,col}.{row,col}.{...} {...}, %a, %b, %c;` |
| `nvgpu.mma.sp.sync` | `llvm.inline_asm` with `mma.sp.sync.aligned.m...` template | `mma.sp.sync.aligned.m16n8k{16,32}.row.col.{f16,bf16,...} {...}, %a, %b, %c, %meta, 0x0;` |
| `nvgpu.ldmatrix` | `nvvm.ldmatrix.sync.aligned` + repack | `ldmatrix.sync.aligned.m8n8.x{1,2,4}{.trans,}.shared::cta.b16 {...}, [%addr];` |
| `nvgpu.stmatrix` | `nvvm.stmatrix.sync.aligned` or inline asm | `stmatrix.sync.aligned.m8n8.x{1,2,4}{.trans,}.shared::cta.b16 [%addr], {...};` |
| `nvgpu.cvt_fpext` / `nvgpu.cvt_fptrunc` | `nvvm.cvt.packfloat.f32.*` | `cvt.{rn,rz,...}.{f16,bf16,e4m3,e5m2}.f32 %r, %f;` (per lane) |
| `nvgpu.fma.packed.f32x2` | `nvvm.fma.rn.f32x2` | `fma.rn.f32x2 %r, %a, %b, %c;` |

The sparse-MMA path reaches PTX through `llvm.inline_asm` because the snapshot's upstream NVVM does not yet expose a sparse-MMA op. The template, constraint string, and result type live in the pattern body and drop verbatim into the LLVM module — see [Inline-PTX templates and constraint strings](../nvvm/overview.md#inline-ptx-templates-and-constraint-strings) on the NVVM overview for the constraint-string form.

## Per-Arch Availability

`convert-nvgpu-to-nvvm` runs unconditionally on every target — the gates live inside the patterns and in NVVM verification, not in pass scheduling. The first column gives the lowest SM that accepts each pattern, the second the form it emits at that floor, the third the lowest PTX ISA version that defines the resulting instruction.

| nvgpu op | SM floor | Emits at floor | `ptx_min` |
|---|---|---|---|
| `nvgpu.device_async_copy` | sm_80 | `cp.async.{ca,cg}.shared.global` | 7.0 |
| `nvgpu.device_async_create_group` | sm_80 | `cp.async.commit_group` | 7.0 |
| `nvgpu.device_async_wait` | sm_80 | `cp.async.wait_group` | 7.0 |
| `nvgpu.mbarrier.{create,init,arrive,try_wait.parity,inval}` | sm_80 | shared-memory mbarrier | 7.0 (base set on 7.0; cluster-aware forms 7.8) |
| `nvgpu.mbarrier.arrive.expect_tx` | sm_90 | `mbarrier.arrive.expect_tx.shared.b64` | 7.8 |
| `nvgpu.tma.async.{load,store,reduce}` | sm_90 | `cp.async.bulk.tensor.{Nd,shared,global}` | 8.0 |
| `nvgpu.tma.prefetch.descriptor` | sm_90 | `prefetch.tensormap` | 8.0 |
| `nvgpu.tma.fence.descriptor` | sm_90 | `fence.proxy.async.shared::cluster` | 8.0 |
| `nvgpu.tma.create.descriptor` | sm_90 | runtime call to `cuTensorMapEncodeTiled` | (host) |
| `nvgpu.tensormap.create.descriptor` | sm_90 | `tensormap.cp.async.shared` + `tensormap.replace.*` | 8.3 |
| `nvgpu.tensormap.update.*` | sm_90 | `tensormap.replace.*` | 8.3 |
| `nvgpu.warpgroup.mma` | sm_90a | `wgmma.mma_async.sync.aligned.m64nXkY.*` | 8.0 |
| `nvgpu.warpgroup.mma.store` | sm_90a | per-thread `st.shared.*` | 8.0 |
| `nvgpu.warpgroup.mma.init.accumulator` | sm_90a | `llvm.mlir.zero` (no PTX) | 8.0 |
| `nvgpu.warpgroup.descriptor` | sm_90a | (no PTX; [SMEM descriptor](../cute_nvgpu/mma-atoms-sm70-120.md#smem-descriptor-construction) synthesis) | n/a |
| `nvgpu.mma.sync` (Ampere/Ada path) | sm_80 | `mma.sync.aligned.m16n8k{16,32}.*` | 7.0 |
| `nvgpu.mma.sync` (Hopper path) | sm_90 | redirects through `nvvm.wgmma.mma_async.*` | 8.0 |
| `nvgpu.mma.sp.sync` | sm_80 | inline `mma.sp.sync.aligned.m16n8k{16,32}.*` | 7.1 |
| `nvgpu.ldmatrix` | sm_75 | `ldmatrix.sync.aligned.m8n8.x{1,2,4}` | 6.5 |
| `nvgpu.stmatrix` | sm_90 | `stmatrix.sync.aligned.m8n8.x{1,2,4}` | 8.0 |
| `nvgpu.warp.execute_on_lane_0` | sm_70 | `shfl.sync.idx.b32` + region predicate | 6.0 |
| `nvgpu.cvt_fpext` / `nvgpu.cvt_fptrunc` (FP4 / FP8) | sm_89 (FP8) / sm_100a (FP4) | `cvt.{rn,rz,...}.{e4m3,e5m2}.f32` | 7.8 / 8.6 |
| `nvgpu.fma.packed.f32x2` / `nvgpu.mul.packed.f32x2` | sm_100a | `fma.rn.f32x2` / `mul.rn.f32x2` | 8.6 |
| `nvgpu.rcp` | sm_70 | `rcp.approx.ftz.f32` | 6.0 |

`sm_90a` is the architecture-qualified variant `wgmma` and TMA require; plain `sm_90` rejects them at NVVM verification. The dialect has no `sm_100` op of its own — the Blackwell tcgen05 surface lives entirely in [`nvvm`](../nvvm/tcgen05-ops.md), accessed through [`cute_nvgpu`](../cute_nvgpu/overview.md) atoms that lower past `nvgpu`. See [Per-SM Emission Templates](../../codegen/per-sm-emission-templates.md#capability-matrix) for the per-tier capability matrix.

## Pattern-Set Construction

`populateNVGPUToNVVMConversionPatterns` is a flat populator: one `OpConversionPattern` per `nvgpu.*` op, each registered with `benefit = 1`. The patterns are stateless — they read operands and attributes through their `OpAdaptor`, emit a fixed sequence of `nvvm.*` (or `llvm.*` / `memref.*`) ops, and replace the root.

Tileiras consumes this populator unchanged from upstream MLIR. Reimplementations should match the same one-pattern-per-op shape; the rewriter's branch on source memory space is the only piece of policy the patterns carry. See [Lowering: nvgpu / gpu to NVVM — Pattern Shapes](../../lowering/nvgpu-and-gpu-to-nvvm.md#pattern-shapes) for the rewrite primitives the patterns share.

## Lowering Contract

The conversion never reinfers layout intent. By the time IR reaches `nvgpu`, descriptor shape, memory space, vector shape, MMA tile shape, sparse metadata, and barrier identity already live in operands and attributes. Pattern bodies stay small as a result.

The mbarrier family branches on memory space and emits one `nvvm.mbarrier.*[.shared]` intrinsic per op. See [mbarrier State Machine](../../topics/mbarrier-state-machine.md#state-machine) for the slot transitions and [NVVM mbarrier Ops](../nvvm/mbarrier-ops.md) for the per-op intrinsic mapping. TMA load and store each emit a single `nvvm.cp.async.bulk.tensor.*` intrinsic, threading the variadic coordinates, multicast mask, and L2 cache hint through unchanged. The largest pattern is `nvgpu.warpgroup.mma`: it emits the [four-stage Hopper WGMMA sequence](../../topics/wgmma-emission-protocol.md#the-four-op-sequence) — fence, async MMA, commit, wait — and validates GMMA layout up front with the canonical "Not a canonical GMMA_MN Layout" wording lifted from CUTLASS's `gmma.hpp`.

A handful of patterns emit no `nvvm.*` op at all. `nvgpu.mbarrier.create` emits a `memref.global` with `"private"` visibility plus a `memref.get_global`, allocating the `__mbarrier` slot in shared memory. `nvgpu.tma.create.descriptor` emits an `llvm.alloca` for a 128-byte `CUtensorMap`, fills it via `llvm.getelementptr`+`llvm.store` sequences, then calls the CUDA driver's `cuTensorMapEncodeTiled`. `nvgpu.warpgroup.descriptor` is a pure `shl`/`or` chain over the [WGMMA descriptor bitfield](../cute_nvgpu/mma-atoms-sm70-120.md#smem-descriptor-construction). `nvgpu.mma.sp.sync` emits an `llvm.inline_asm` with the verbatim `"mma.sp.sync.aligned.m..."` PTX template; at the snapshot revision tileiras tracks, upstream NVVM has no sparse-MMA op yet, and inline-asm is the upstream design.

## Verification Invariants

The interesting `nvgpu` verifier checks are semantic, not lexical. TMA ops demand valid descriptor types, compatible source or destination memrefs, supported tensor-map ranks, and a legal shared-memory layout. WGMMA demands rank-2 matrix fragments, compatible `M`/`N`/`K`, a supported tile shape, matching accumulator and result types, and legal transpose flags. MMA and sparse MMA add element-type checks, sparse-selector bounds, and a guard that `tf32` only pairs with valid floating-point operands. Device async copy requires matching element types, unit-stride minor dimensions, supported transfer sizes, and correct alignment when L1 bypass is requested.

The boundary matters because NVVM conversion assumes the op is already legal for the selected target. Invalid shapes slipping through here resurface later as much less useful intrinsic-selection or backend diagnostics.

## Reimplementation Checklist

A practical reimplementation needs the operation families above, typed descriptor and barrier values, shape-aware verifiers, and a deterministic conversion table to NVVM. Keep the layer transient. Independent scheduling, high-level layout algebra, and CUDA Tile semantics all belong above `nvgpu`. The dialect's job is to normalise hardware operations, verify their low-level shape contracts, and hand them to NVVM with as little policy as possible.

The minimum useful surface: tensor-map descriptor creation and async TMA load/store/reduce; shared-memory barrier groups and barrier tokens with `expect_tx` and `try_wait.parity`; WGMMA accumulator init / mma / store; the [WGMMA descriptor packer](../cute_nvgpu/mma-atoms-sm70-120.md#smem-descriptor-construction); MMA ops with explicit shape attributes; sparse MMA metadata and selector validation; `ldmatrix` and `stmatrix`; SM80 `cp.async` device-async copy; packed conversion and arithmetic helpers; a complete `nvgpu`-to-`nvvm` conversion table; and target-aware verification before conversion.
