# nvgpu Dialect Overview

## Abstract

`nvgpu` is the bridge dialect between MLIR's generic `gpu` ops and NVPTX-specific `nvvm`. It names the NVIDIA kernel patterns that `gpu` cannot express — warp shuffle, MMA and WGMMA, `cp.async`, `mbarrier`, TMA — without yet committing to a concrete NVVM intrinsic. Tileiras links the upstream dialect unchanged. `cute_nvgpu` feeds it from above; `convert-nvgpu-to-nvvm` drains it from below.

About thirty ops live here. The conversion pass installs one `OpConversionPattern` per op and rewrites the module in a single sweep, each pattern emitting a small fixed body of `nvvm.*` ops — or, for four exception cases, expanded `memref` / `llvm` / `llvm.inline_asm`. The pass mnemonic is `convert-nvgpu-to-nvvm`; it runs after `convert-vector-to-llvm` and before `convert-func-to-llvm`, so by the time it fires every operand is already in LLVM-dialect or memref form.

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

`cute_nvgpu` ops still speak SM-tier vocabulary — TMA atoms, WGMMA atoms, Blackwell tensor-memory operations. `nvgpu` strips the source-level atom naming and re-presents the same behaviour over MLIR memrefs, vectors, descriptors, barrier groups, and async tokens. That makes the NVVM conversion mechanical: every `nvgpu` op below has a fixed `nvvm` (or `llvm.inline_asm`) lowering.

## Pattern-Instance Table

Any TileIR pipeline targeting Hopper or Blackwell hammers the same thirteen ops. Each row below pairs the `nvgpu` mnemonic with the `nvvm` op the rewriter emits, plus the per-pattern TypeID token sitting in the pattern instance's `+0x40` debug-name slot. The matching PTX text appears in the [Lowering-Target Table](#lowering-target-table) below.

| nvgpu op | nvvm op(s) | TypeID |
|---|---|---|
| `nvgpu.device_async_copy` | `nvvm.cp.async.shared.global` | `&unk_5B8DAE0` |
| `nvgpu.device_async_commit_group` | `nvvm.cp.async.commit_group` | `&unk_5B8DAF8` |
| `nvgpu.device_async_wait` | `nvvm.cp.async.wait_group` | `&unk_5B8DB10` |
| `nvgpu.mbarrier.create` | `nvvm.mbarrier.init` | `&unk_5B8DB28` |
| `nvgpu.mbarrier.init` | `nvvm.mbarrier.init.shared` | `&unk_5B8DB40` |
| `nvgpu.mbarrier.arrive` | `nvvm.mbarrier.arrive` | `&unk_5B8DB58` |
| `nvgpu.mbarrier.try_wait.parity` | `nvvm.mbarrier.try_wait.parity.shared` | `&unk_5B8DB70` |
| `nvgpu.tma.async.load` | `nvvm.cp.async.bulk.tensor.shared.global` | `&unk_5B8DB88` |
| `nvgpu.tma.async.store` | `nvvm.cp.async.bulk.tensor.global.shared` | `&unk_5B8DBA0` |
| `nvgpu.warp.execute_on_lane_0` | `nvvm.shfl.sync` + conditional region | `&unk_5B8DBB8` |
| `nvgpu.tensormap.create.descriptor` | `nvvm.tensormap.cp.async.shared` + `tensormap.replace.descriptor.*` | `&unk_5B8DBD0` |
| `nvgpu.mma.sync` | `nvvm.wmma.mma.sync.aligned` or `nvvm.wgmma.mma_async.sync.aligned` | `&unk_5B8DBE8` |
| `nvgpu.wgmma.descriptor` | `nvvm.wgmma.descriptor` (computes the 64-bit SMEM descriptor) | `&unk_5B8DC00` |

Upstream's `mlir::populateNVGPUToNVVMConversionPatterns()` installs the set, and Tileiras links it unchanged. Each pattern is a 0x68-byte `OpConversionPattern` (Shape B), allocated from the size-class allocator and registered with `benefit=1`. The rewriter callbacks branch on source memory space to pick the generic or `.shared` form of the `mbarrier` and `cp.async` intrinsics — address space 3 always selects `.shared`.

## Operand and Attribute Tables (Representative Ops)

Four ops cover the slot shapes that recur across the dialect: an async-copy op with a small enum bag, a descriptor-creation op carrying the full TMA layout, an mbarrier op that branches on memory space, and the WGMMA core that produces both the descriptor and the accumulator tile sequence.

`nvgpu.device_async_copy`

| Position | Name | Type | Notes |
|---|---|---|---|
| operand 0 | `dst` | `memref<...>` in addr-space 3 (shared) | minor dim must be unit-stride |
| operand 1 | `src` | `memref<...>` in addr-space 1 (global) | minor dim must be unit-stride |
| operand 2 | `dstIndices` | variadic `index` | rank == dst rank |
| operand 3 | `srcIndices` | variadic `index` | rank == src rank |
| attribute | `dstElements` | `i64` (IntegerAttr) | element count per lane; 4, 8, 16 |
| attribute | `srcElements` | optional `i64` | shorter source for predicated case |
| attribute | `bypassL1` | optional `UnitAttr` | selects `.cg` cache modifier |
| result | `token` | `!nvgpu.device.async.token` | passed to commit/wait |

`nvgpu.tma.async.load`

| Position | Name | Type | Notes |
|---|---|---|---|
| operand 0 | `dst` | `memref<...>` in addr-space 3 | TMA destination |
| operand 1 | `barrier` | `!nvgpu.mbarrier.group` | arrives expect-tx on completion |
| operand 2 | `tensorMapDescriptor` | `!nvgpu.tensormap.descriptor` | from `tma.create.descriptor` |
| operand 3 | `coordinates` | variadic `i32`, rank 1..5 | tile origin in tensor space |
| operand 4 | `multicastMask` | optional `i16` | cluster multicast bitmap |
| operand 5 | `l2CacheHint` | optional `i64` | maps to `.L2::cache_hint` |
| attribute | `predicate` | optional `i1` | gated TMA issue |

`nvgpu.mbarrier.arrive`

| Position | Name | Type | Notes |
|---|---|---|---|
| operand 0 | `barriers` | `!nvgpu.mbarrier.group` | wraps the shared-memory slot |
| operand 1 | `mbarId` | `index` | barrier index within the group |
| attribute | (none) | — | the address space drives the `.shared` selector |
| result 0 | `token` | `!nvgpu.mbarrier.token` | feeds `mbarrier.test.wait` |

`nvgpu.warpgroup.mma`

| Position | Name | Type | Notes |
|---|---|---|---|
| operand 0 | `descriptorA` | `!nvgpu.warpgroup.descriptor` | SMEM descriptor for A |
| operand 1 | `descriptorB` | `!nvgpu.warpgroup.descriptor` | SMEM descriptor for B |
| operand 2 | `matrixC` | `!nvgpu.warpgroup.accumulator` | input accumulator tile |
| attribute | `transposeA` | optional `UnitAttr` | wired into the WGMMA layout enum |
| attribute | `transposeB` | optional `UnitAttr` | wired into the WGMMA layout enum |
| attribute | `waitGroup` | optional `i32` | controls the wait-group depth |
| result 0 | `matrixD` | `!nvgpu.warpgroup.accumulator` | output accumulator tile |

## Lowering-Target Table

What each rewriter emits. The middle column gives the concrete NVVM op, or the expanded form when the pattern bypasses NVVM on purpose. The right column is a reader aid: it is what the NVPTX backend ultimately prints, not anything `nvgpu` itself emits.

| nvgpu op | NVVM op (or expansion) | Final PTX (after NVVM lowering) |
|---|---|---|
| `nvgpu.device_async_copy` | `nvvm.cp.async.shared.global` | `cp.async.{ca,cg}.shared.global [%dst], [%src], N;` |
| `nvgpu.device_async_commit_group` | `nvvm.cp.async.commit.group` | `cp.async.commit_group;` |
| `nvgpu.device_async_wait` | `nvvm.cp.async.wait.group` | `cp.async.wait_group N;` |
| `nvgpu.mbarrier.create` | `memref.global "private"` + `memref.get_global` | (no PTX; allocates SMEM slot) |
| `nvgpu.mbarrier.init` | `nvvm.mbarrier.init.shared` | `mbarrier.init.shared.b64 [%mbar], %count;` |
| `nvgpu.mbarrier.arrive` | `nvvm.mbarrier.arrive[.shared]` | `mbarrier.arrive.shared.b64 %tok, [%mbar];` |
| `nvgpu.mbarrier.try_wait.parity` | `nvvm.mbarrier.try_wait.parity.shared` | `mbarrier.try_wait.parity.shared.b64 %p, [%mbar], %ph, %ns;` |
| `nvgpu.tma.async.load` | `nvvm.cp.async.bulk.tensor.shared.global` | `cp.async.bulk.tensor.{1..5}d.shared::cluster.global.tile.mbarrier::complete_tx::bytes [%dst], [%tmap, {%c0,..}], [%mbar];` |
| `nvgpu.tma.async.store` | `nvvm.cp.async.bulk.tensor.global.shared` | `cp.async.bulk.tensor.{1..5}d.global.shared::cta.tile [%tmap, {%c0,..}], [%src];` |
| `nvgpu.tma.create.descriptor` | `llvm.alloca` + GEP/store sequence + `llvm.call @cuTensorMapEncodeTiled` | (no PTX; host-side encode of a 128-byte `CUtensorMap`) |
| `nvgpu.warp.execute_on_lane_0` | `nvvm.shfl.sync` + conditional region | `shfl.sync.idx.b32 %r, %v, 0, 0x1f, 0xffffffff;` |
| `nvgpu.warpgroup.mma` | `nvvm.wgmma.fence.aligned` → N× `nvvm.wgmma.mma_async.sync.aligned` → `nvvm.wgmma.commit.group.sync.aligned` → `nvvm.wgmma.wait.group.sync.aligned` | `wgmma.fence.sync.aligned;` then `wgmma.mma_async.sync.aligned.m64nXkY.f32.{f16,bf16,e4m3,e5m2}.{f16,bf16,e4m3,e5m2} {...}, %da, %db, p, 1, 1, %la, %lb;` then `wgmma.commit_group.sync.aligned;` then `wgmma.wait_group.sync.aligned N;` |
| `nvgpu.warpgroup.descriptor` | `nvvm.wgmma.descriptor` | (no PTX; the 64-bit SMEM descriptor is built by ordinary `shl`/`or` integer ops; the PTX side sees the materialised `b64` value) |
| `nvgpu.mma.sync` | `nvvm.wmma.mma.sync.aligned` (sm_70..sm_89) or `nvvm.wgmma.mma_async.sync.aligned` (sm_90+) | `mma.sync.aligned.m16n8kK.{row,col}.{row,col}.{...} {...}, %a, %b, %c;` |
| `nvgpu.mma.sp.sync` | `llvm.inline_asm` with `mma.sp.sync.aligned.m...` template | `mma.sp.sync.aligned.m16n8k{16,32}.row.col.{f16,bf16,...} {...}, %a, %b, %c, %meta, 0x0;` |
| `nvgpu.tensormap.create.descriptor` | `nvvm.tensormap.cp.async.shared` + `tensormap.replace.descriptor.*` | `tensormap.cp.async.shared::cta.bulk_group [%dst], [%src];` then a sequence of `tensormap.replace.tile.{global_address,box_dim,elem_stride,...}.[%tmap], ...;` |

`mma.sp.sync.aligned` reaches PTX through `llvm.inline_asm` because the snapshot's upstream NVVM does not yet expose a sparse-MMA op. The template, constraint string, and result type live in the pattern body and drop verbatim into the LLVM module.

## Per-Arch Availability

`convert-nvgpu-to-nvvm` runs unconditionally on every target — the gates live inside the patterns and in NVVM verification, not in pass scheduling. The first column gives the lowest SM that accepts each pattern, the second the form it emits at that floor, the third the lowest PTX ISA version that defines the resulting instruction.

| nvgpu op | SM floor | Emits at floor | `ptx_min` |
|---|---|---|---|
| `nvgpu.device_async_copy` | sm_80 | `cp.async.{ca,cg}.shared.global` | 7.0 |
| `nvgpu.device_async_commit_group` | sm_80 | `cp.async.commit_group` | 7.0 |
| `nvgpu.device_async_wait` | sm_80 | `cp.async.wait_group` | 7.0 |
| `nvgpu.mbarrier.{create,init,arrive,try_wait.parity}` | sm_80 | shared-memory mbarrier | 7.0 (full set on 7.0; cluster-aware forms 7.8) |
| `nvgpu.tma.async.{load,store}` | sm_90 | `cp.async.bulk.tensor.{Nd,shared,global}` | 8.0 |
| `nvgpu.tma.create.descriptor` | sm_90 | runtime call to `cuTensorMapEncodeTiled` | (host) |
| `nvgpu.tensormap.create.descriptor` | sm_90 | `tensormap.cp.async.shared` + `tensormap.replace.*` | 8.3 |
| `nvgpu.warpgroup.mma` | sm_90a | `wgmma.mma_async.sync.aligned.m64nXkY.*` | 8.0 |
| `nvgpu.warpgroup.descriptor` | sm_90a | (no PTX; SMEM descriptor synthesis) | n/a |
| `nvgpu.mma.sync` (Ampere/Ada path) | sm_80 | `mma.sync.aligned.m16n8k{16,32}.*` | 7.0 |
| `nvgpu.mma.sync` (Hopper path) | sm_90 | redirects through `nvvm.wgmma.mma_async.*` | 8.0 |
| `nvgpu.mma.sp.sync` | sm_80 | inline `mma.sp.sync.aligned.m16n8k{16,32}.*` | 7.1 |
| `nvgpu.warp.execute_on_lane_0` | sm_70 | `shfl.sync.idx.b32` + region predicate | 6.0 |
| `nvgpu.device_async_copy` with `bypassL1` | sm_80 | `cp.async.cg.shared.global` (forces `.cg`) | 7.0 |

`sm_90a` is the architecture-qualified variant `wgmma` and TMA require; plain `sm_90` rejects them at NVVM verification. The dialect has no `sm_100` op — the Blackwell tcgen05 surface lives entirely in `nvvm`.

## TableGen Registration Shape

Tileiras consumes the dialect as a prebuilt library, but each op started life as a TableGen record. The build stamps the same five-piece layout into the registrar; a reimplementer needs all five to round-trip an op through `mlir-opt`:

1. **`OperationName` singleton** — one interned `OperationName` per op, stored in the dialect's name table. The singleton
   pointer is what the TypeID slab cells in this dialect actually hold.
2. **Operand/result spec** — the operand and result `TypeConstraint`s above, materialised as a `getODSOperands()`
   function and an `OpAdaptor` body. Variadic operand groups (TMA coordinates, MMA fragments) flow through an
   `OperandSegmentSizes` inline buffer at Properties `+0..+15`.
3. **Attribute spec** — the inherited-attribute getters wrap the same Pattern-A/B/C/D/E reads documented for NVVM in
   [Properties Blob and Attribute Parsers](../nvvm/properties-blob-and-attr-parsers.md).
4. **Verifier hook** — a free function attached to the op-info record, called by the upstream verification driver. The
   shape checks listed under [Verification Invariants](#verification-invariants) live here.
5. **Custom assembly + printer** — a parser/printer pair that `mlir-opt` consults when round-tripping textual IR. The
   nvgpu dialect's parsers are split between `sub_2DB9700..sub_2DBA470` (twelve-property bank), `sub_2DB8E10`
   (`packed_kind`), and `sub_2DB9590` (`rnd`); the printer side reads the same slots back through `MlirOpAsmPrinter`.

Items (2) and (5) fall out of upstream MLIR's TableGen output. Items (1), (3), and (4) are dialect-specific glue a reimplementer must wire by hand if they aren't running tablegen at all.

## Properties Parsers

`nvgpu` ops carry attributes through upstream's `OpProperty` infrastructure, not the legacy attribute dictionary. Two consequences follow: storage is dense, so `OpAdaptor` reads compile to fixed offsets; and the parser side must be generated apart from the inherited-attribute path. The dialect installs three parser entry points:

- `sub_2DB9700..sub_2DBA470` is the twelve-property parser bank. It covers `dst_format`, `src_format`, `precision`,
  `cta_layout`, `warp_layout`, and the related shape and tile-layout enums shared by the WGMMA and TMA ops.
- `sub_2DB8E10` parses `packed_kind`, the enum that selects between NVIDIA's packed-FP4 and packed-FP8 packings on
  `nvgpu.cvt_fpext` and `nvgpu.cvt_fptrunc`.
- `sub_2DB9590` parses `rnd`, the rounding-mode enum shared by every packed-arithmetic op (`cvt_fp*`, `fma.packed.f32x2`,
  `mul.packed.f32x2`, `rcp`).

These parsers sit beside the NVVM ones documented in [Properties Blob and Attribute Parsers](../nvvm/properties-blob-and-attr-parsers.md). Both dialects share the same `MlirRawOstream` / `MlirOpAsmPrinter` accessor pattern, which makes attribute round-tripping indistinguishable across the boundary.

## Pattern-Set Construction

`populateNVGPUToNVVMConversionPatterns` is a flat populator. A single 11-KiB body runs the same four-step sequence for each of the ~30 patterns: allocate a 0x68-byte instance from the size-class allocator, call the canonical `OpRewritePattern<OpT>` base ctor (which records the op name, looks up the TypeID, and sets `benefit=1`), overwrite the vtable pointer with the per-pattern vtable in `.rodata`, then append the instance to the `RewritePatternSet`. Each per-pattern vtable carries eight slots, with `matchAndRewrite` at slot 6 (offset `+0x30`).

Append overflows fall back to the standard `std::vector<unique_ptr<RewritePattern>>` realloc-insert path. The populator references eleven byte-identical 432-byte instantiations of `_M_realloc_insert`; the linker did not ICF them, so they appear as separate functions, but the pairing is incidental — each one is reachable by exactly one pattern because that's where the linker happened to lay it out, not because of any semantic association.

## Lowering Contract

The conversion never reinfers layout intent. By the time IR reaches `nvgpu`, descriptor shape, memory space, vector shape, MMA tile shape, sparse metadata, and barrier identity already live in operands and attributes. Pattern bodies stay small as a result.

The mbarrier family branches on memory space and emits one `nvvm.mbarrier.*[.shared]` intrinsic per op. TMA load and store each emit a single `nvvm.cp.async.bulk.tensor.*` intrinsic, threading the variadic coordinates, multicast mask, and L2 cache hint through unchanged. The largest pattern is `nvgpu.warpgroup.mma`: it emits the four-stage Hopper WGMMA sequence — `nvvm.wgmma.fence.aligned`, one `nvvm.wgmma.mma_async` per accumulator tile, `nvvm.wgmma.commit.group.sync.aligned`, then `nvvm.wgmma.wait.group.sync.aligned` — and validates GMMA layout up front with the canonical "Not a canonical GMMA_MN Layout" wording lifted from CUTLASS's `gmma.hpp`.

A handful of patterns emit no `nvvm.*` op at all. `nvgpu.mbarrier.create` emits a `memref.global` with `"private"` visibility plus a `memref.get_global`, allocating the `__mbarrier` slot in shared memory. `nvgpu.tma.create.descriptor` emits an `llvm.alloca` for a 128-byte `CUtensorMap`, fills it via `llvm.getelementptr`+`llvm.store` sequences, then calls the CUDA driver's `cuTensorMapEncodeTiled`. `nvgpu.mma.sp.sync` emits an `llvm.inline_asm` with the verbatim `"mma.sp.sync.aligned.m..."` PTX template; at the snapshot revision tileiras tracks, upstream NVVM has no sparse-MMA op yet, and inline-asm is the upstream design.

## Verification Invariants

The interesting `nvgpu` verifier checks are semantic, not lexical. TMA ops demand valid descriptor types, compatible source or destination memrefs, supported tensor-map ranks, and a legal shared-memory layout. WGMMA demands rank-2 matrix fragments, compatible `M`/`N`/`K`, a supported tile shape, matching accumulator and result types, and legal transpose flags. MMA and sparse MMA add element-type checks, sparse-selector bounds, and a guard that `tf32` only pairs with valid floating-point operands. Device async copy requires matching element types, unit-stride minor dimensions, supported transfer sizes, and correct alignment when L1 bypass is requested.

The boundary matters because NVVM conversion assumes the op is already legal for the selected target. Invalid shapes slipping through here resurface later as much less useful intrinsic-selection or backend diagnostics.

## Reimplementation Checklist

A practical reimplementation needs the operation families above, typed descriptor and barrier values, shape-aware verifiers, and a deterministic conversion table to NVVM. Keep the layer transient. Independent scheduling, high-level layout algebra, and CUDA Tile semantics all belong above `nvgpu`. The dialect's job is to normalise hardware operations, verify their low-level shape contracts, and hand them to NVVM with as little policy as possible.

The minimum useful surface: tensor-map descriptor creation and async TMA load/store; shared-memory barrier groups and barrier tokens; WGMMA and MMA ops with explicit shape attributes; sparse MMA metadata and selector validation; `ldmatrix` and device async copy; packed conversion and arithmetic helpers; a complete `nvgpu`-to-`nvvm` conversion table; and target-aware verification before conversion.
