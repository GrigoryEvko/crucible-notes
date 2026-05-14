# NVVM Dialect Overview

## Abstract

Every `nvvm.X` op exists to print one PTX instruction. `nvvm` is the bottom MLIR dialect in TileIR's lowering stack — a typed intrinsic layer, not a programming model. Earlier dialects decide tiling, scheduling, pipeline stages, layouts, and target atoms; `nvvm` preserves those decisions in a form LLVM and the NVPTX backend understand.

Three lowering paths cover the whole dialect. Most ops become a `call @llvm.nvvm.X` intrinsic that the NVPTX backend prints as the PTX instruction shown in the per-family tables below. A smaller set lowers to `llvm.inline_asm` with a fixed PTX template — sparse MMA, a handful of TMA replace variants, a few cluster ops. The third path expands into ordinary `llvm` dialect ops (`alloca`, GEP, store, call). No `nvvm.*` op survives NVVM-to-LLVM conversion.

## Position in the Cascade

```text
nvgpu
    |
    | convert GPU operations to NVVM operations and LLVM helper IR
    v
nvvm
    |
    | convert NVVM operations to LLVM intrinsics or inline assembly
    v
llvm
    |
    | optimize, verify, select instructions, print PTX
    v
PTX
```

`nvgpu` is the last MLIR layer that still looks like a GPU dialect. `nvvm` looks more like LLVM IR: pointer types, vector types, memory-order attributes, target attributes, and intrinsic operand shapes have to be explicit by the time IR arrives. Most verifier failures here are best read as "the previous lowering didn't finish specifying the target operation."

## Operation Roster

The dialect registers exactly 213 ops through the constructor `sub_2EFC390` (8 589 B). Two independent counts agree: the constructor body holds 43 inline calls to the single-op registrar `sub_4461CA0` and 170 batch trampolines, summing to 213; the dense TypeID slab at `.data.rel.ro 0x5B8D618..0x5B8DCB8` is `0x6A8 / 8 = 213` entries long.

Three functions form the init chain. `sub_2EFC390` (8 589 B) is the main `NVVMDialect::initialize` body. `sub_2EFE520` (1 195 B) is a secondary register thunk for late-registered ops. `sub_2EFE9D0` runs after `NVVMDialect` and registers the LLVM-side type conversions used during NVVM-to-LLVM materialization.

The 213 ops fall into the families below. Bucket counts sum to 213; the example column carries representative spellings.

| Family | Count | Example ops | SM gate |
|---|---:|---|---|
| Synchronisation | 18 | `nvvm.barrier0`, `nvvm.barrier.cta.sync`, `nvvm.bar.warp.sync` | sm_70+ |
| mbarrier | 21 | `nvvm.mbarrier.init`, `nvvm.mbarrier.try_wait.parity.shared`, `nvvm.mbarrier.arrive.expect_tx` | sm_90+ |
| cp.async | 12 | `nvvm.cp.async.shared.global`, `nvvm.cp.async.commit_group`, `nvvm.cp.async.wait_group` | sm_80+ |
| cp.async.bulk (TMA) | 38 | `nvvm.cp.async.bulk.tensor.shared.global`, `nvvm.cp.async.bulk.commit.group` | sm_90+ |
| WMMA | 64 | `nvvm.wmma.load.a.sync`, `nvvm.wmma.store.d.sync`, `nvvm.wmma.mma.sync.aligned.m16n16k16` | sm_70+ |
| WGMMA | 9 | `nvvm.wgmma.mma_async.sync.aligned`, `nvvm.wgmma.fence.sync.aligned`, `nvvm.wgmma.wait.group.sync.aligned` | sm_90a |
| tcgen05 | 14 | `nvvm.tcgen05.alloc.shared`, `nvvm.tcgen05.ld`, `nvvm.tcgen05.mma.sync` | sm_100+ |
| Cluster | 8 | `nvvm.cluster.arrive`, `nvvm.cluster.wait`, `nvvm.cluster.arrive.relaxed` | sm_90+ |
| Special registers | 12 | `nvvm.read.ptx.sreg.tid.x`, `nvvm.read.ptx.sreg.ctaid.x`, `nvvm.read.ptx.sreg.warpid` | sm_70+ |
| shfl / vote | 8 | `nvvm.shfl.sync`, `nvvm.vote.ballot.sync` | sm_70+ |
| barrier-id helpers | 4 | `nvvm.bar.arrive`, `nvvm.bar.sync` | sm_70+ |
| Other (mapa, fence, ldmatrix/stmatrix, redux) | 5 | `nvvm.mapa`, `nvvm.fence.mbarrier.init`, `nvvm.ldmatrix.sync.aligned`, `nvvm.stmatrix.sync.aligned` | varies |

Registration is unconditional — every target sees all 213 ops. Verifier and lowering logic do the gating, so an op may exist in the dialect even when the chosen target rejects it. The SM gate column above states the lowering-time requirement, not a registration-time predicate.

## Per-Family PTX Templates

Each row below pins one op family to the exact PTX the NVPTX backend prints for that family's canonical intrinsic. `{...}` tokens are enum-driven — cache modifier, layout, eltype — and trace back to the Properties slots in [Properties Blob and Attribute Parsers](properties-blob-and-attr-parsers.md). Per-op variants substitute different mnemonics; the slot layout and skeleton stay the same.

| Op | NVVM-to-LLVM target | PTX printed by the backend |
|---|---|---|
| `nvvm.barrier0` | `call @llvm.nvvm.barrier0()` | `bar.sync 0;` |
| `nvvm.bar.warp.sync` | `call @llvm.nvvm.bar.warp.sync(i32)` | `bar.warp.sync %m;` |
| `nvvm.barrier.cta.sync.aligned` | `call @llvm.nvvm.barrier.cta.sync.aligned(i32, i32)` | `barrier.cta.sync.aligned %b, %n;` |
| `nvvm.shfl.sync` | `call @llvm.nvvm.shfl.sync.{idx,up,down,bfly}.{i32,f32}` | `shfl.sync.{idx,up,down,bfly}.b32 %r, %v, %lane, %m, %mask;` |
| `nvvm.vote.ballot.sync` | `call @llvm.nvvm.vote.ballot.sync` | `vote.sync.ballot.b32 %r, %p, %mask;` |
| `nvvm.read.ptx.sreg.tid.x` | `call @llvm.nvvm.read.ptx.sreg.tid.x()` | `mov.u32 %r, %tid.x;` |
| `nvvm.read.ptx.sreg.ctaid.x` | `call @llvm.nvvm.read.ptx.sreg.ctaid.x()` | `mov.u32 %r, %ctaid.x;` |
| `nvvm.cp.async.shared.global` | `call @llvm.nvvm.cp.async.{ca,cg}.shared.global.{4,8,16}` | `cp.async.{ca,cg}.shared.global [%dst], [%src], N;` |
| `nvvm.cp.async.commit.group` | `call @llvm.nvvm.cp.async.commit.group()` | `cp.async.commit_group;` |
| `nvvm.cp.async.wait.group` | `call @llvm.nvvm.cp.async.wait.group(i32)` | `cp.async.wait_group N;` |
| `nvvm.mbarrier.init.shared` | `call @llvm.nvvm.mbarrier.init.shared(ptr addrspace(3), i32)` | `mbarrier.init.shared.b64 [%mbar], %count;` |
| `nvvm.mbarrier.arrive` | `call @llvm.nvvm.mbarrier.arrive[.shared].b64` | `mbarrier.arrive[.shared].b64 %tok, [%mbar];` |
| `nvvm.mbarrier.arrive.expect_tx.shared` | `call @llvm.nvvm.mbarrier.arrive.expect_tx.shared.b64` | `mbarrier.arrive.expect_tx.shared.b64 %tok, [%mbar], %tx;` |
| `nvvm.mbarrier.try_wait.parity.shared` | `call @llvm.nvvm.mbarrier.try_wait.parity.shared.b64` | `mbarrier.try_wait.parity.shared.b64 %p, [%mbar], %ph, %ns;` |
| `nvvm.cp.async.bulk.tensor.shared.global` | `call @llvm.nvvm.cp.async.bulk.tensor.{1..5}d.shared.cluster.global.tile` | `cp.async.bulk.tensor.{N}d.shared::cluster.global.tile.mbarrier::complete_tx::bytes [%dst], [%tmap, {%c0,..}], [%mbar];` |
| `nvvm.cp.async.bulk.tensor.global.shared` | `call @llvm.nvvm.cp.async.bulk.tensor.{1..5}d.global.shared.cta.tile` | `cp.async.bulk.tensor.{N}d.global.shared::cta.tile [%tmap, {%c0,..}], [%src];` |
| `nvvm.cp.async.bulk.commit.group` | `call @llvm.nvvm.cp.async.bulk.commit.group()` | `cp.async.bulk.commit_group;` |
| `nvvm.cp.async.bulk.wait.group` | `call @llvm.nvvm.cp.async.bulk.wait.group(i32)` | `cp.async.bulk.wait_group N;` |
| `nvvm.wmma.load.a.sync` | `call @llvm.nvvm.wmma.m16n16k16.load.a.{row,col}.stride.{f16,bf16,...}` | `wmma.load.a.sync.aligned.m16n16k16.{row,col}.shared::cta.{f16,bf16,...} {...}, [%addr], %stride;` |
| `nvvm.wmma.store.d.sync` | `call @llvm.nvvm.wmma.m16n16k16.store.d.{row,col}.stride.{f16,f32,...}` | `wmma.store.d.sync.aligned.m16n16k16.{row,col}.shared::cta.{f16,f32,...} [%addr], {...}, %stride;` |
| `nvvm.wmma.mma.sync.aligned.m16n16k16` | `call @llvm.nvvm.wmma.m16n16k16.mma.{row,col}.{row,col}.{f16,f32}.{f16,f32}` | `wmma.mma.sync.aligned.m16n16k16.{row,col}.{row,col}.{f16,f32}.{f16,f32} {...}, %a, %b, %c;` |
| `nvvm.wgmma.fence.aligned` | `call @llvm.nvvm.wgmma.fence.sync.aligned()` | `wgmma.fence.sync.aligned;` |
| `nvvm.wgmma.mma_async.sync.aligned` | `call @llvm.nvvm.wgmma.mma_async.sync.aligned.m64nXkY.f32.{f16,bf16,e4m3,e5m2}.{f16,bf16,e4m3,e5m2}` | `wgmma.mma_async.sync.aligned.m64nXkY.f32.{f16,bf16,e4m3,e5m2}.{f16,bf16,e4m3,e5m2} {...}, %da, %db, p, 1, 1, %la, %lb;` |
| `nvvm.wgmma.commit.group.sync.aligned` | `call @llvm.nvvm.wgmma.commit.group.sync.aligned()` | `wgmma.commit_group.sync.aligned;` |
| `nvvm.wgmma.wait.group.sync.aligned` | `call @llvm.nvvm.wgmma.wait.group.sync.aligned(i32)` | `wgmma.wait_group.sync.aligned N;` |
| `nvvm.ldmatrix.sync.aligned` | `call @llvm.nvvm.ldmatrix.sync.aligned.m8n8.{x1,x2,x4}.{trans.,}{b16,b8x16,...}` | `ldmatrix.sync.aligned.m8n8.x{1,2,4}{.trans,}.shared::cta.{b16,b8x16,...} {...}, [%addr];` |
| `nvvm.stmatrix.sync.aligned` | `call @llvm.nvvm.stmatrix.sync.aligned.m8n8.{x1,x2,x4}.{trans.,}{b16,b8x16}` | `stmatrix.sync.aligned.m8n8.x{1,2,4}{.trans,}.shared::cta.{b16,b8x16} [%addr], {...};` |
| `nvvm.tcgen05.alloc.shared` | `call @llvm.nvvm.tcgen05.alloc.cta_group.{1,2}.shared` | `tcgen05.alloc.cta_group::{1,2}.shared::cta.b32 [%tmem], %n;` |
| `nvvm.tcgen05.ld` | `call @llvm.nvvm.tcgen05.ld.{shape}.{num}` | `tcgen05.ld.sync.aligned.{shape}.{num}.b32 {...}, [%tmem];` |
| `nvvm.tcgen05.mma.sync` | `call @llvm.nvvm.tcgen05.mma.{kind}.cta_group.{1,2}.{collector}` | `tcgen05.mma.sync.aligned.{kind}.cta_group::{1,2}.{layout}.{collector} [%d], %a, %b, %scale;` |
| `nvvm.cluster.arrive` | `call @llvm.nvvm.barrier.cluster.arrive()` | `barrier.cluster.arrive;` |
| `nvvm.cluster.arrive.relaxed` | `call @llvm.nvvm.barrier.cluster.arrive.relaxed()` | `barrier.cluster.arrive.relaxed;` |
| `nvvm.cluster.wait` | `call @llvm.nvvm.barrier.cluster.wait()` | `barrier.cluster.wait;` |
| `nvvm.mapa` | `call @llvm.nvvm.mapa.shared.cluster.i64` | `mapa.shared::cluster.u64 %r, %addr, %cta_rank;` |
| `nvvm.fence.mbarrier.init` | `call @llvm.nvvm.fence.mbarrier.init.release.cluster()` | `fence.mbarrier_init.release.cluster;` |
| `nvvm.redux.sync` | `call @llvm.nvvm.redux.sync.{op}.{type}` | `redux.sync.{op}.{type} %r, %v, %mask;` |
| `nvvm.elect.sync` | `call @llvm.nvvm.elect.sync(i32)` | `elect.sync %p|%r, %mask;` |
| `nvvm.bar.arrive` / `nvvm.bar.sync` | `call @llvm.nvvm.bar{.arrive,.sync}(i32, i32)` | `bar.arrive %b, %n;` / `bar.sync %b, %n;` |
| `nvvm.cp.async.bulk.tensor.prefetch` | `call @llvm.nvvm.cp.async.bulk.tensor.prefetch.{N}d.tile` | `cp.async.bulk.prefetch.tensor.{N}d.global.tile [%tmap, {%c0,..}];` |
| `nvvm.cp.async.bulk.tensor.reduce` | `call @llvm.nvvm.cp.async.bulk.tensor.reduce.{redop}.{N}d.global.shared.cta.tile` | `cp.async.bulk.tensor.{N}d.global.shared::cta.tile.{redop}.bulk_group [%tmap, {%c0,..}], [%src];` |
| `nvvm.prefetch.tensormap` | `call @llvm.nvvm.prefetch.tensormap` | `prefetch.tensormap [%tmap];` |

### Inline-PTX template paths

A handful of ops never reach `call @llvm.nvvm.X`. They lower to `llvm.inline_asm` with a fixed PTX template that the NVPTX backend rubber-stamps into the output. The template literal is built once per pattern body; the constraint string is the matching `"=r,r,r,..."` form. Reimplementers must reproduce both verbatim — the backend rejects the asm node otherwise.

| Op | Inline PTX template (printed) |
|---|---|
| `nvvm.mma.sp.sync` | `mma.sp.sync.aligned.m16n8k{16,32}.row.col.{f16,bf16,...} {...}, %a, %b, %c, %meta, 0x0;` |
| `nvvm.cp.async.bulk.tensor.shared.cluster.to.global` (im2col cache-hint variant) | `cp.async.bulk.tensor.{N}d.global.shared::cta.im2col.bulk_group.L2::cache_hint [%tmap, {%c0,..}], [%src], %hint;` |
| `nvvm.tcgen05.cp` | `tcgen05.cp.{shape}.{multicast}.{src_fmt} [%dst], [%src];` |

## Per-Arch Availability

Registration is uniform across targets; the gate lives in the verifier and the backend. This table is the practical "what runs where" view. `ptx_min` is the lowest PTX ISA version the final printed instruction requires.

| Op family | SM floor | SM ceiling (observed) | `ptx_min` | Notes |
|---|---|---|---|---|
| Synchronisation (`barrier0`, `bar.warp.sync`, `barrier.cta.sync.aligned`) | sm_70 | unbounded | 6.0 / 7.0 | `aligned` forms require 7.0 |
| Special registers (`read.ptx.sreg.*`) | sm_70 | unbounded | 6.0 | always legal |
| shfl / vote | sm_70 | unbounded | 6.0 | only the `.sync` forms are emitted |
| cp.async (`cp.async.{ca,cg}.shared.global`, `commit_group`, `wait_group`) | sm_80 | unbounded | 7.0 | Ampere async-copy queue |
| mbarrier (`mbarrier.{init,arrive,arrive.expect_tx,try_wait.parity}[.shared]`) | sm_80 | unbounded | 7.0 (base), 7.8 (`.expect_tx`, `.parity`) | shared-memory variant on Ampere; cluster-aware extensions on Hopper |
| WMMA load / store / mma | sm_70 | sm_89 emits Ampere/Ada forms; sm_90+ redirects through WGMMA | 6.0 | the only MMA path on Turing/Ampere |
| WGMMA (`wgmma.fence`, `wgmma.mma_async`, `wgmma.commit_group`, `wgmma.wait_group`) | sm_90a | sm_90a (no Blackwell WGMMA — Blackwell uses tcgen05) | 8.0 | architecture-qualified; plain `sm_90` is rejected |
| TMA (`cp.async.bulk.tensor.*`, `cp.async.bulk.{commit,wait}.group`, `tensormap.replace.*`) | sm_90 | unbounded | 8.0 (tile) / 8.3 (`tensormap.replace.*`) | descriptor lives in global memory; SMEM mailbox via `tensormap.cp.async.shared` |
| Cluster (`cluster.arrive[.relaxed]`, `cluster.wait`, `mapa`, `fence.mbarrier.init`) | sm_90 | unbounded | 8.0 | requires `barrier.cluster.*` PTX |
| ldmatrix / stmatrix | sm_75 (`ldmatrix`) / sm_90 (`stmatrix`) | unbounded | 6.5 / 8.0 | width-4 `.trans` form requires 7.8 |
| tcgen05 (`alloc.shared`, `ld`, `st`, `mma.sync`, `cp`, `commit`, `wait`, `fence`) | sm_100a | sm_100a (also sm_100f for the `f`-suffixed variants) | 8.6 | Blackwell tensor-memory family |
| Block-scaled MMA (`mma.block_scale`) | sm_100a | sm_100a | 8.6 | the only sm_100 form in the legacy `nvvm.mma` namespace |
| redux / barrier-id helpers | sm_80 (`redux.sync`) / sm_70 (`bar.{arrive,sync}`) | unbounded | 7.0 / 6.0 | `redux.sync` requires Ampere |

## TypeID Slab

The dense TypeID array sits at `.data.rel.ro 0x5B8D618..0x5B8DCB8` — `213 * 8 B = 1 704 B`. Each entry is a `void*` to one op's interned `OperationName`. The slab is laid out in registration order: entry 0 is the first op the constructor registers, entry 212 is the last. Walking the slab top to bottom recovers registration order directly, without re-deriving it from the call graph.

## Constant-Fold Switch

The LLVM-side constant-folder `sub_39ADED0` carries a 412-case switch that decides which intrinsic IDs `canConstantFoldCallTo` is willing to evaluate at compile time. The cases span every NVPTX intrinsic ID plus the upstream LLVM `Intrinsic::*` enum range. IDs `8851..9011` are the interesting block — the nvvm-namespaced intrinsics with known fold rules, mostly conversions like `cvt.rn.f16.f32` and `cvt.rn.bf16.f32`. The broader intrinsic-ID dispatch that feeds this switch is covered in [libdevice/intrinsic-id-switch-and-name-table.md](../../libdevice/intrinsic-id-switch-and-name-table.md).

## Operand and Attribute Tables (Representative Ops)

Four ops cover the shape categories that recur across the dialect: a simple reader, a synchronisation arrival, a TMA tile-load, and the WGMMA core. Each table maps positional and named pieces to the MLIR types the verifier enforces.

`nvvm.read.ptx.sreg.tid.x`

| Position | Name | Type | Notes |
|---|---|---|---|
| result 0 | `tid_x` | `i32` | matches the i32 () signature of the underlying LLVM intrinsic |

`nvvm.mbarrier.arrive.expect_tx.shared`

| Position | Name | Type | Notes |
|---|---|---|---|
| operand 0 | `addr` | `ptr addrspace(3)` | shared-memory mbarrier slot |
| operand 1 | `txCount` | `i32` | expect-tx byte count |
| result 0 | `token` | `i64` | consumed by `mbarrier.test.wait` |

`nvvm.cp.async.bulk.tensor.shared.global`

| Position | Name | Type | Notes |
|---|---|---|---|
| operand 0 | `dstAddr` | `ptr addrspace(3)` | SMEM destination tile origin |
| operand 1 | `tensorMap` | `ptr` (global, opaque) | 128-byte `CUtensorMap` |
| operands 2..6 | `coords` | variadic `i32`, rank 1..5 | tile origin in tensor space |
| operand 7 | `barrier` | `ptr addrspace(3)` | mbarrier slot for expect-tx |
| operand 8 | `multicastMask` | optional `i16` | cluster multicast bitmap |
| operand 9 | `cacheHint` | optional `i64` | L2 cache hint |
| attribute | `im2col_offsets` | optional `ArrayAttr<i16>` | enables `.im2col` form |
| attribute | `cacheHintEnable` | `UnitAttr` (Pattern C) | gates the `.L2::cache_hint` modifier |
| attribute | `multicastEnable` | `UnitAttr` (Pattern C) | gates the `.multicast` modifier |
| attribute | `mode` | enum `tma_load_mode` (Pattern A) | `tile` / `im2col` / `im2col_w` / `im2col_w_128` |

`nvvm.wgmma.mma_async.sync.aligned`

| Position | Name | Type | Notes |
|---|---|---|---|
| operand 0 | `descA` | `i64` | SMEM descriptor for A |
| operand 1 | `descB` | `i64` | SMEM descriptor for B |
| operand 2 | `accumIn` | `!nvvm.struct<...>` of accumulator regs | accumulator tile in registers |
| attribute | `typeA` | enum `wgmma_type` (Pattern A, slot +64) | `f16` / `bf16` / `e4m3` / `e5m2` / `s8` / `u8` / ... |
| attribute | `b1Op` | enum (Pattern A, slot +72) | `xor_popc` / `and_popc` / `none` |
| attribute | `typeB` | enum `wgmma_type` (Pattern A, slot +80) | mirror of `typeA` |
| attribute | `shape` | enum `shape` (Pattern A, slot +88) | `m64nXkY` selector |
| attribute | `typeC` | enum `wgmma_type` (Pattern A, slot +96) | usually `f32` |
| attribute | `scaleIn` | enum `wgmma_scale_in` (Pattern A, slot +112) | `+1` / `-1` for A and B |
| attribute | `scaleOut` | enum `wgmma_scale_out` (Pattern A, slot +120) | accumulator scale (0 or 1) |
| attribute | `layoutA` | enum `mma_layout` (Pattern A, slot +128) | `row` / `col` |
| attribute | `layoutB` | enum `mma_layout` (Pattern A, slot +136) | `row` / `col` |
| result 0 | `accumOut` | same struct type as `accumIn` | accumulator tile after the MMA |

The slot offsets above mirror the table in [Properties Blob and Attribute Parsers](properties-blob-and-attr-parsers.md). Only the `+64..+136` window matters for the representative ops shown here; `+128` and `+136` are touched exclusively by `wgmma.mma_async` (and by `mma.block_scale` for `scale_vec`).

## TableGen Registration Shape

Each `nvvm.X` op starts life as a TableGen record. The build stamps five pieces into the registrar; a reimplementer needs all five to round-trip an op through `mlir-opt`:

1. **`OperationName` singleton** — registered once per op. The 213-entry TypeID slab at `.data.rel.ro 0x5B8D618..0x5B8DCB8`
   is the dense index of these singletons in registration order.
2. **Operand / result spec** — operand `TypeConstraint`s and result `TypeConstraint`s, exposed as `getODSOperands()` and
   an `OpAdaptor` reader. Variadic groups (TMA coordinates, MMA fragments) thread through the
   `OperandSegmentSizes` inline buffer at Properties `+0..+15`.
3. **Attribute spec (Properties)** — slot positions and Pattern-A/B/C/D/E decoders from
   [Properties Blob and Attribute Parsers](properties-blob-and-attr-parsers.md). The 67 enum namespaces registered by
   `NVVMDialect::initialize` are exactly the enums Pattern A reads from those slots; the sixty-eighth registrar handles
   `NVVMTargetAttr` (`chip`, `features`, `link-files`, `flags`).
4. **Verifier hook** — a free function attached to the op-info record. The op-set verifier enforces target gating,
   operand-count alignment with the chosen intrinsic, pointer address spaces, memory ordering, MMA / WGMMA shape, and
   completeness of inline-PTX constraint strings.
5. **Lowering hook** — the NVVM-to-LLVM rewrite pattern. The three branches in
   [Lowering Contract](#lowering-contract) (intrinsic call / inline-asm / expanded LLVM ops) are selected here.

Items (2) and (5) fall out of upstream MLIR's TableGen output. Items (1), (3), and (4) are dialect-specific glue.

## Lowering Contract

NVVM-to-LLVM conversion is deliberately mechanical. Each op becomes a call to a known LLVM intrinsic, inline assembly with a fixed operand contract, or an expansion into ordinary LLVM dialect operations — never anything else.

```c
void lower_nvvm_to_llvm(Module module, Target target) {
    for (Operation *op : module.walk()) {
        require(verify_nvvm_op_for_target(op, target));

        if (has_direct_nvvm_intrinsic(op)) {
            replace_with_llvm_intrinsic_call(op, intrinsic_id(op));
        } else if (requires_inline_ptx(op)) {
            replace_with_inline_asm(op, ptx_template(op), operand_constraints(op));
        } else {
            expand_with_llvm_operations(op);
        }
    }

    require(no_operations_with_dialect(module, "nvvm"));
}
```

This pass should never rediscover layout, scheduling, or pipeline semantics — those decisions belong to `nv_tileas`, `cute`, `cute_nvgpu`, and `nvgpu`. At the NVVM boundary the only questions are whether the op is well typed for the target and how to spell it in LLVM IR.

## Verifier Invariants

The verifier rejects anything that cannot be legally translated to the selected target:

- intrinsic operand counts and result counts match the selected intrinsic,
- pointer address spaces are explicit and legal for the operation,
- memory scopes and memory-ordering attributes are compatible,
- MMA and WGMMA shapes are supported by the target,
- sparse and block-scaled MMA forms carry the required metadata operands,
- TMA and async-copy operands have valid descriptor, barrier, and memory-space types,
- special-register reads are valid for the target and execution model,
- inline PTX operations have complete constraint strings and result types,
- operations requiring a newer SM generation are not emitted for an older target.

This is the last MLIR-level diagnostic point before LLVM IR and the machine backend. A good error here names the semantic mismatch, not just the intrinsic.

## Target Attributes

`nvvm` carries the target attributes that make the LLVM handoff meaningful: architecture, PTX version, feature flags, kernel markers, launch bounds, cluster dimensions, and assorted function- and module-level properties. Earlier passes set these through `gpu.module` and conversion interfaces; by the time the LLVM module materializes, the backend has to recover a concrete NVPTX target machine from them.

```c
NvptxTarget build_target_from_nvvm_attrs(Module module) {
    NvptxTarget target;
    target.sm = read_required_attr(module, "sm");
    target.ptx_version = read_required_attr(module, "ptx");
    target.features = read_feature_set(module);
    target.kernels = collect_functions_with_nvvm_kernel_attr(module);
    return target;
}
```

Missing or contradictory attributes here are silent disasters: the backend still receives syntactically valid LLVM IR, but generates code for the wrong target contract.

