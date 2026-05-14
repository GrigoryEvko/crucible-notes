# NVVM Dialect Overview

## Abstract

Every `nvvm.X` op exists to print one PTX instruction (or one inline-asm template). `nvvm` is the bottom MLIR dialect in TileIR's lowering stack — a typed intrinsic layer, not a programming model. Earlier dialects decide tiling, scheduling, pipeline stages, layouts, and target atoms; `nvvm` preserves those decisions in a form LLVM and the NVPTX backend understand.

Three lowering paths cover the whole dialect. Most ops become a `call @llvm.nvvm.X` intrinsic that the NVPTX backend prints as the matching PTX instruction. A smaller set lowers to `llvm.inline_asm` with a fixed PTX template — sparse MMA, a handful of TMA replace variants, a few cluster ops. The third path expands into ordinary `llvm` dialect ops (`alloca`, GEP, store, call). No `nvvm.*` op survives NVVM-to-LLVM conversion.

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

[`nvgpu`](../nvgpu/overview.md) is the last MLIR layer that still looks like a GPU dialect. `nvvm` looks more like LLVM IR: pointer types, vector types, memory-order attributes, target attributes, and intrinsic operand shapes have to be explicit by the time IR arrives. Most verifier failures here are best read as "the previous lowering didn't finish specifying the target operation." See [Lowering: nvgpu / gpu to NVVM](../../lowering/nvgpu-and-gpu-to-nvvm.md) for the per-op rewrite contract.

## Per-Family Pages

The dialect ships about 213 ops. They split cleanly into eight large families plus a long tail of small ones. The bulk of each family is documented on its own page; this overview lists the families, their roster sizes, the SM floor, and one example op so the cross-link table doubles as an index.

| Family | Count | SM floor | Example op | Page |
|---|---:|---|---|---|
| WMMA — warp-synchronous register MMA | 64 | sm_70 | `nvvm.wmma.mma.sync.aligned.m16n16k16.row.col.f16.f16.f16.f16` | [WMMA Ops](wmma-ops.md) |
| WGMMA — warp-group async MMA (Hopper) | 9 | sm_90a | `nvvm.wgmma.mma_async.sync.aligned` | [WGMMA Ops](wgmma-ops.md) |
| TMA — bulk tensor copy, prefetch, reduce | 38 | sm_90 | `nvvm.cp.async.bulk.tensor.shared.global` | [TMA Ops](tma-ops.md) |
| tcgen05 — Blackwell tensor memory + MMA | 14 | sm_100a | `nvvm.tcgen05.mma.block_scale` | [tcgen05 Ops](tcgen05-ops.md) |
| mbarrier — shared-memory barrier state machine | 21 | sm_80 | `nvvm.mbarrier.arrive.expect_tx.shared` | [mbarrier Ops](mbarrier-ops.md) |
| Cluster — thread-block cluster sync | 8 | sm_90 | `nvvm.cluster.wait`, `nvvm.mapa` | [Cluster Ops](cluster-ops.md) |
| Synchronisation — `barrier0`, `bar.sync`, `bar.warp.sync` | 18 | sm_70 | `nvvm.barrier.cta.sync.aligned` | (this page) |
| `cp.async` (Ampere SM80 async-copy queue) | 12 | sm_80 | `nvvm.cp.async.shared.global` | (this page) |
| Special registers — `tid`, `ctaid`, `ntid`, etc. | 12 | sm_70 | `nvvm.read.ptx.sreg.tid.x` | (this page) |
| shfl / vote / elect.sync | 8 | sm_70 | `nvvm.shfl.sync` | (this page) |
| `bar.{arrive,sync}` barrier-id helpers | 4 | sm_70 | `nvvm.bar.sync` | (this page) |
| Other (`mapa`, fences, ldmatrix/stmatrix, redux, prefetch) | 5 | varies | `nvvm.ldmatrix.sync.aligned` | (this page) |

The family page is the normative spec: it pins each op to its operand list, LLVM intrinsic, PTX template, constraint string for inline-asm variants, and SM floor. The roster table below covers the smaller families that don't justify their own page.

## Roster — Small Families

### Synchronisation

| Op | LLVM intrinsic | PTX printed |
|---|---|---|
| `nvvm.barrier0` | `llvm.nvvm.barrier0` | `bar.sync 0;` |
| `nvvm.bar.warp.sync` | `llvm.nvvm.bar.warp.sync` | `bar.warp.sync %m;` |
| `nvvm.barrier.cta.sync.aligned` | `llvm.nvvm.barrier.cta.sync.aligned` | `barrier.cta.sync.aligned %b, %n;` |
| `nvvm.barrier.cta.sync` (non-aligned) | `llvm.nvvm.barrier.cta.sync` | `barrier.cta.sync %b, %n;` |
| `nvvm.barrier.cta.arrive` | `llvm.nvvm.barrier.cta.arrive` | `barrier.cta.arrive %b, %n;` |
| `nvvm.barrier.cta.arrive.aligned` | `llvm.nvvm.barrier.cta.arrive.aligned` | `barrier.cta.arrive.aligned %b, %n;` |
| `nvvm.bar.arrive` / `nvvm.bar.sync` | `llvm.nvvm.bar.arrive` / `llvm.nvvm.bar.sync` | `bar.arrive %b, %n;` / `bar.sync %b, %n;` |
| `nvvm.elect.sync` | `llvm.nvvm.elect.sync` | `elect.sync %p|%r, %mask;` |

### Special-register reads

| Op | LLVM intrinsic | PTX printed |
|---|---|---|
| `nvvm.read.ptx.sreg.tid.x` (`.y`, `.z`) | `llvm.nvvm.read.ptx.sreg.tid.{x,y,z}` | `mov.u32 %r, %tid.{x,y,z};` |
| `nvvm.read.ptx.sreg.ntid.x` (`.y`, `.z`) | `llvm.nvvm.read.ptx.sreg.ntid.{x,y,z}` | `mov.u32 %r, %ntid.{x,y,z};` |
| `nvvm.read.ptx.sreg.ctaid.x` (`.y`, `.z`) | `llvm.nvvm.read.ptx.sreg.ctaid.{x,y,z}` | `mov.u32 %r, %ctaid.{x,y,z};` |
| `nvvm.read.ptx.sreg.nctaid.x` (`.y`, `.z`) | `llvm.nvvm.read.ptx.sreg.nctaid.{x,y,z}` | `mov.u32 %r, %nctaid.{x,y,z};` |
| `nvvm.read.ptx.sreg.warpid` | `llvm.nvvm.read.ptx.sreg.warpid` | `mov.u32 %r, %warpid;` |
| `nvvm.read.ptx.sreg.laneid` | `llvm.nvvm.read.ptx.sreg.laneid` | `mov.u32 %r, %laneid;` |
| `nvvm.read.ptx.sreg.smid` | `llvm.nvvm.read.ptx.sreg.smid` | `mov.u32 %r, %smid;` |

### `cp.async` (Ampere)

| Op | LLVM intrinsic | PTX printed |
|---|---|---|
| `nvvm.cp.async.shared.global` | `llvm.nvvm.cp.async.{ca,cg}.shared.global.{4,8,16}` | `cp.async.{ca,cg}.shared.global [%dst], [%src], N;` |
| `nvvm.cp.async.commit.group` | `llvm.nvvm.cp.async.commit.group` | `cp.async.commit_group;` |
| `nvvm.cp.async.wait.group` | `llvm.nvvm.cp.async.wait.group` | `cp.async.wait_group N;` |
| `nvvm.cp.async.wait.all` | `llvm.nvvm.cp.async.wait.all` | `cp.async.wait_all;` |
| `nvvm.cp.async.mbarrier.arrive[.shared]` | `llvm.nvvm.cp.async.mbarrier.arrive[.shared]` | `cp.async.mbarrier.arrive[.shared].b64 [%mbar];` |
| `nvvm.cp.async.mbarrier.arrive.noinc[.shared]` | `llvm.nvvm.cp.async.mbarrier.arrive.noinc[.shared]` | `cp.async.mbarrier.arrive.noinc[.shared].b64 [%mbar];` |

### shfl / vote

| Op | LLVM intrinsic | PTX printed |
|---|---|---|
| `nvvm.shfl.sync` | `llvm.nvvm.shfl.sync.{idx,up,down,bfly}.{i32,f32}` | `shfl.sync.{idx,up,down,bfly}.b32 %r, %v, %lane, %m, %mask;` |
| `nvvm.vote.ballot.sync` | `llvm.nvvm.vote.ballot.sync` | `vote.sync.ballot.b32 %r, %p, %mask;` |
| `nvvm.vote.all.sync` / `.any.sync` / `.uni.sync` | `llvm.nvvm.vote.{all,any,uni}.sync` | `vote.sync.{all,any,uni}.pred %p, %src, %mask;` |
| `nvvm.match.sync` | `llvm.nvvm.match.{any,all}.sync.{i32,i64}` | `match.{any,all}.sync.b{32,64} %r, %v, %mask;` |
| `nvvm.redux.sync` | `llvm.nvvm.redux.sync.{op}.{type}` | `redux.sync.{op}.{type} %r, %v, %mask;` |

### ldmatrix / stmatrix and miscellaneous

| Op | LLVM intrinsic | PTX printed |
|---|---|---|
| `nvvm.ldmatrix.sync.aligned` | `llvm.nvvm.ldmatrix.sync.aligned.m8n8.x{1,2,4}{.trans,}.{b16,b8x16,...}` | `ldmatrix.sync.aligned.m8n8.x{1,2,4}{.trans,}.shared::cta.{b16,b8x16,...} {...}, [%addr];` |
| `nvvm.stmatrix.sync.aligned` | `llvm.nvvm.stmatrix.sync.aligned.m8n8.x{1,2,4}{.trans,}.{b16,b8x16}` | `stmatrix.sync.aligned.m8n8.x{1,2,4}{.trans,}.shared::cta.{b16,b8x16} [%addr], {...};` |
| `nvvm.prefetch.tensormap` | `llvm.nvvm.prefetch.tensormap` | `prefetch.tensormap [%tmap];` |
| `nvvm.fence.proxy.acquire.sync.cluster` | `llvm.nvvm.fence.proxy.acquire.sync.cluster` | `fence.proxy.async.shared::cluster;` |
| `nvvm.fence.mbarrier.init.release.cluster` | `llvm.nvvm.fence.mbarrier.init.release.cluster` | `fence.mbarrier_init.release.cluster;` |
| `nvvm.cvt.packfloat.f32` | `llvm.nvvm.cvt.{rn,rz,rm,rp}.{f16x2,bf16x2,e4m3x2,e5m2x2}.f32` | `cvt.{rnd}.{f16,bf16,e4m3,e5m2}x2.f32 %r, %fhi, %flo;` |
| `nvvm.mma.sync` (Ampere/Ada dense) | `llvm.nvvm.mma.m{8,16}n{8,16}k{...}.row.col.{...}` | `mma.sync.aligned.m16n8kK.{row,col}.{row,col}.{...} {...}, %a, %b, %c;` |
| `nvvm.mma.block_scale` | `llvm.nvvm.mma.block_scale.m16n8k.{kind}` | `mma.sync.aligned.m16n8k.{kind}.scale::vec::{16,32} {...}, %a, %b, %c, %sa, %sb;` |

## Inline-PTX Templates and Constraint Strings

A handful of ops bypass `call @llvm.nvvm.X` and lower to `llvm.inline_asm` with a fixed PTX template plus a verbatim constraint string. The backend rejects the asm node unless template and constraint match the operand list exactly; reimplementers must reproduce both byte-for-byte.

The constraint codes used in this dialect:

| Code | Meaning |
|---|---|
| `r` | 32-bit integer register (`i32` / `f32` / `i16` / `i8`) |
| `l` | 64-bit integer register (`i64`, including pointer-typed operands) |
| `f` | 32-bit floating-point register (`f32`) |
| `h` | 16-bit integer register (`i16` / `f16` / `bf16`) |
| `n` | compile-time integer immediate |
| `=r` / `=l` / `=f` / `=h` | output-only register of the matching width |

### Sparse MMA

```text
template:    "mma.sp.sync.aligned.m{M}n{N}k{K}.row.col.{aType}.{bType}.{cType}.{dType}
                 { %0, %1, %2, %3 },          // D (output)
                 { %4, %5, %6, %7 },          // A (sparse halved)
                 { %8, %9, %10, %11, %12, %13, %14, %15 },  // B
                 { %16, %17, %18, %19 },      // C
                 %20, 0x{selector};"          // sparse metadata, selector immediate
constraint:  "=r,=r,=r,=r,r,r,r,r,r,r,r,r,r,r,r,r,r,r,r,r,r"
```

The first four `=r` slots are the output D fragment; the trailing `r` slots are the input fragments and the metadata word. The selector immediate is baked into the template literal at lowering time rather than passed as an operand; the same op emits 0x0 or 0x1 depending on the `sparsitySelector` attribute.

For shape `m16n8k16.row.col.f16.f16.f16.f16` the operand widths are A=4, B=8, C=4, D=4, metadata=1 (total 17 operands). For `m16n8k32.row.col.s8.s8.s32.s32` they are A=2, B=4, C=4, D=4, metadata=1 (total 15 operands). The verifier rejects any combination not listed in the PTX ISA.

### im2col TMA store with L2 cache hint

```text
template:    "cp.async.bulk.tensor.{N}d.global.shared::cta.im2col.bulk_group.L2::cache_hint
                 [%0, { %1, %2, ..., %{N} }],
                 [%{N+1}],
                 %{N+2};"
constraint:  "l,r,r,r,r,r,l,l"      // N=5 example
```

Operand 0 is the `i64` descriptor pointer; the next `N` operands (one per rank) are 32-bit coordinates; the SMEM source pointer is `l`; the cache hint is `l`. Rank-3 and rank-4 forms drop coordinate operands and shrink the constraint string accordingly.

### tcgen05.cp

```text
template:    "tcgen05.cp.{shape}.{multicast}.{src_fmt} [%0], [%1];"
constraint:  "r,r"
```

The two `r` operands are the destination and source TMEM column indices. The shape, multicast, and src_fmt tokens are baked into the template literal at pattern-build time.

### stmatrix fallback (pre-sm_90)

When `nvvm.stmatrix.sync.aligned` is targeted at a pre-sm_90 SM that exposes `ldmatrix` but not `stmatrix` directly, the op lowers through `llvm.inline_asm`:

```text
template:    "stmatrix.sync.aligned.m8n8.x{num}{.trans,}.shared::cta.b16
                 [%0], { %1, %2, ..., %{num} };"
constraint:  "l,r,r,...,r"          // one l for addr, num× r for fragment regs
```

`l` is the `ptr addrspace(3)` destination; the trailing `r` slots are the fragment registers.

### WGMMA scale-D selector (when the immediate form is rejected)

Most `wgmma.mma_async.sync.aligned` variants reach PTX through the LLVM intrinsic, which carries `scale_d` as a compile-time argument. The few ops that drop to inline-asm use:

```text
template:    "wgmma.mma_async.sync.aligned.m64n{N}k{K}.{accT}.{aT}.{bT}
                 { %0, %1, ..., %{accW-1} },
                 %da, %db, %p,
                 1, 1, %la, %lb;"
constraint:  "=f,=f,...,=f,l,l,n,n,n"
```

Each output accumulator register is `=f` (for `f32` accumulator types) or `=h` (`f16`). The two descriptor inputs are `l`. The `%p` predicate and the two trailing `n` slots are compile-time immediates. The `=r` slot used in some upstream snapshots for the scale-D return value does not appear on this constraint string because the immediate form is the only one tileiras emits.

## Per-Arch Availability

Registration is uniform across targets; the gate lives in the verifier and the backend. The table is the practical "what runs where" view. `ptx_min` is the lowest PTX ISA version the final printed instruction requires.

| Family | SM floor | SM ceiling (observed) | `ptx_min` | Notes |
|---|---|---|---|---|
| Synchronisation | sm_70 | unbounded | 6.0 / 7.0 | `aligned` forms require 7.0 |
| Special registers | sm_70 | unbounded | 6.0 | always legal |
| shfl / vote | sm_70 | unbounded | 6.0 | only the `.sync` forms are emitted |
| cp.async (Ampere) | sm_80 | unbounded | 7.0 | Ampere async-copy queue |
| mbarrier | sm_80 (base), sm_90 (`.expect_tx`) | unbounded | 7.0 / 7.8 | shared-memory variant on Ampere; cluster-aware extensions on Hopper |
| WMMA | sm_70 | sm_89 (Hopper redirects through WGMMA) | 6.0 | the only MMA path on Turing/Ampere |
| WGMMA | sm_90a | sm_90a (no Blackwell WGMMA) | 8.0 | architecture-qualified; plain `sm_90` is rejected |
| TMA | sm_90 | unbounded | 8.0 / 8.3 | descriptor lives in global memory |
| Cluster | sm_90 | unbounded | 8.0 | requires `barrier.cluster.*` PTX |
| ldmatrix / stmatrix | sm_75 (`ldmatrix`), sm_90 (`stmatrix`) | unbounded | 6.5 / 8.0 | width-4 `.trans` form requires 7.8 |
| tcgen05 | sm_100a | sm_100a (+ sm_100f for `f`-suffixed copy variants) | 8.6 | Blackwell tensor-memory family |
| Block-scaled MMA (`mma.block_scale`) | sm_100a | sm_100a | 8.6 | the only sm_100 form in the legacy `nvvm.mma` namespace |
| redux / barrier-id helpers | sm_80 (`redux.sync`) / sm_70 (`bar.{arrive,sync}`) | unbounded | 7.0 / 6.0 | `redux.sync` requires Ampere |

## Lowering Contract

NVVM-to-LLVM conversion is deliberately mechanical. Each `nvvm.X` op has a single registered lowering: a direct `call @llvm.nvvm.X` intrinsic when LLVM exposes a matching intrinsic, or an `llvm.inline_asm` with a hard-coded PTX template otherwise. A third path expands into ordinary `llvm` dialect ops for the few cases that aren't a single instruction (e.g. `nvvm.shfl.sync` synthesised broadcast loops).

The choice is fixed per op at registration time. The conversion driver walks each `nvvm.*` op, looks up the op's `OperationName` in the dispatch map, and invokes the matching rewrite:

```c
LogicalResult lower_nvvm_op(Operation *op) {
    const NvvmLowering *entry = lookup_by_operation_name(op->getName());
    require(entry != NULL);

    switch (entry->kind) {
        case NVVM_DIRECT_INTRINSIC:
            return replace_with_llvm_intrinsic_call(op, entry->intrinsic_id);
        case NVVM_INLINE_ASM:
            return replace_with_inline_asm(op, entry->ptx_template, entry->constraints);
        case NVVM_LLVM_EXPANSION:
            return entry->custom_expand(op);
    }
}
```

The dispatch map is built once at dialect-load time from the TableGen records: each record declares `dialect="nvvm"`, an op mnemonic, an LLVM intrinsic ID (or an inline-asm template + constraint string), and the kind. Lowering reads each field straight out of the entry. No layout, scheduling, or pipeline policy is reinferred here — earlier dialects must already have committed to the target operation.

After the sweep, no `nvvm.*` op survives. The verifier check that follows the sweep treats any remaining `nvvm.*` op as a missing pattern, not as a default-illegal op.

## Verifier Invariants

The verifier rejects anything that cannot be legally translated to the selected target:

- intrinsic operand counts and result counts match the selected intrinsic;
- pointer address spaces are explicit and legal for the operation;
- memory scopes and memory-ordering attributes are compatible;
- MMA and WGMMA shapes are supported by the target;
- sparse and block-scaled MMA forms carry the required metadata operands;
- TMA and async-copy operands have valid descriptor, barrier, and memory-space types;
- special-register reads are valid for the target and execution model;
- inline-PTX operations have complete constraint strings and result types;
- operations requiring a newer SM generation are not emitted for an older target.

This is the last MLIR-level diagnostic point before LLVM IR and the machine backend. A good error here names the semantic mismatch, not just the intrinsic.

## Target Attributes

`nvvm` carries the target attributes that make the LLVM handoff meaningful: architecture (`nvvm.target = "sm_90a"`), PTX version, feature flags (`+ptx80`, `+tmem`, ...), kernel markers, launch bounds, cluster dimensions, and assorted function- and module-level properties. Earlier passes set these through `gpu.module` and conversion interfaces; by the time the LLVM module materialises, the NVPTX backend has to recover a concrete subtarget from them.

The attributes are plain string / integer / array attributes attached to the `gpu.module` or `func.func` parents — the NVPTX backend reads them from the LLVM module's metadata after MLIR-to-LLVM translation. Missing or contradictory attributes here are silent disasters: the backend still receives syntactically valid LLVM IR, but generates code for the wrong target contract. The verifier rejects the obvious cases (no `sm`, no `ptx`), and the [NVPTX subtarget feature matrix](../../codegen/nvptx-subtarget-and-feature-matrix.md) lists which features each SM accepts.
