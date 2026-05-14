# NVVM WMMA Ops

## Abstract

The `nvvm.wmma.*` family is the warp-synchronous matrix-multiply-accumulate path used on every NVIDIA target from sm_70 through sm_89. Each op exists to print one `wmma.{load,store,mma}.sync.aligned` PTX instruction at one fixed `(M, N, K)` tile shape. The dialect carries 64 ops because the cross-product of operation, fragment role, tile shape, layout, and element type is enumerated as separate ops rather than parameterised over attributes.

Hopper (sm_90+) does not extend this family. Warp-group MMA on Hopper lives in [`nvvm.wgmma.*`](wgmma-ops.md); Blackwell MMA lives in [`nvvm.tcgen05.*`](tcgen05-ops.md).

## Op Layout

The 64 ops break down as:

| Sub-family | Count | Mnemonic stem |
|---|---:|---|
| Load A | 12 | `nvvm.wmma.load.a.sync.aligned.m{16,32,8}n{16,8,32}k{16,8}.{row,col}.{f16,bf16,tf32,s8,u8,s4,u4,b1}` |
| Load B | 12 | `nvvm.wmma.load.b.sync.aligned.m..n..k..{row,col}.{...}` |
| Load C | 12 | `nvvm.wmma.load.c.sync.aligned.m..n..k..{row,col}.{f16,f32,s32}` |
| Store D | 12 | `nvvm.wmma.store.d.sync.aligned.m..n..k..{row,col}.{f16,f32,s32}` |
| MMA  | 16 | `nvvm.wmma.mma.sync.aligned.m..n..k..{row,col}.{row,col}.{atype}.{btype}.{ctype}.{dtype}` |

Tile shapes enumerated: `m16n16k16`, `m8n32k16`, `m32n8k16` for `f16`/`bf16`; `m16n16k8` and `m8n32k8` and `m32n8k8` for `tf32`; `m16n16k16` and `m8n32k16` and `m32n8k16` for `s8`/`u8`; `m8n8k128` for `b1`; `m8n8k32` for `s4`/`u4`.

## Operand Tables

### `nvvm.wmma.load.{a,b,c}.sync.aligned.mXnYkZ.{row,col}.{T}`

| Position | Name | Type | Notes |
|---|---|---|---|
| operand 0 | `addr` | `ptr addrspace(3)` | shared-memory tile origin |
| operand 1 | `stride` | `i32` | row or column stride in elements |
| result 0 | `frag` | `!llvm.struct<(T, T, ..., T)>` | per-thread register fragment; cardinality fixed by shape and element type |

Each shape pins the fragment length: an `m16n16k16.f16` A fragment is `struct<(vector<2xf16>, vector<2xf16>, ..., vector<2xf16>)>` of length 8; an `m16n16k16.tf32` A fragment is `struct<(i32, i32, i32, i32)>`; an `m16n16k16.s8` A fragment is `struct<(i32, i32)>`. The verifier rejects any other arity for the chosen shape/type pair.

### `nvvm.wmma.store.d.sync.aligned.mXnYkZ.{row,col}.{T}`

| Position | Name | Type | Notes |
|---|---|---|---|
| operand 0 | `addr` | `ptr addrspace(3)` | shared-memory destination |
| operand 1..N | `frag` | per shape/type | D fragment elements, expanded into one operand per register |
| operand N+1 | `stride` | `i32` | row or column stride in elements |

`store.d` flattens the fragment into separate operands rather than re-packing into a struct, which mirrors LLVM's intrinsic signature.

### `nvvm.wmma.mma.sync.aligned.mXnYkZ.{layoutA}.{layoutB}.{aT}.{bT}.{cT}.{dT}`

| Position | Name | Type | Notes |
|---|---|---|---|
| operand 0..p | A fragment | per shape and `aT` | flattened |
| operand p+1..q | B fragment | per shape and `bT` | flattened |
| operand q+1..r | C fragment | per shape and `cT` | accumulator input |
| result 0 | D fragment | `!llvm.struct<(dT, ..., dT)>` | accumulator output |

For `m16n16k16.row.col.f16.f16.f16.f16` the operand bag is A=8 `f16x2`, B=8 `f16x2`, C=4 `f16x2`, and the result is `struct<(vector<2xf16>) x 4>`. The verifier cross-checks the operand counts against the shape and types and rejects any mismatch.

## LLVM Intrinsic Mapping

Every `nvvm.wmma.*` op lowers to one `call @llvm.nvvm.wmma.mXnYkZ.{op}.{layout}.{...}` intrinsic. The intrinsic name is constructed at TableGen registration time by concatenating the shape, op, layout, and type tokens. The lowering pattern reads the op's attributes only to verify; it does not pick the intrinsic at run time — the per-op vtable hardwires the intrinsic ID.

| Op | LLVM intrinsic |
|---|---|
| `nvvm.wmma.load.a.sync.aligned.m16n16k16.row.f16` | `llvm.nvvm.wmma.m16n16k16.load.a.row.stride.f16` |
| `nvvm.wmma.load.b.sync.aligned.m16n16k16.col.f16` | `llvm.nvvm.wmma.m16n16k16.load.b.col.stride.f16` |
| `nvvm.wmma.load.c.sync.aligned.m16n16k16.row.f32` | `llvm.nvvm.wmma.m16n16k16.load.c.row.stride.f32` |
| `nvvm.wmma.store.d.sync.aligned.m16n16k16.row.f32` | `llvm.nvvm.wmma.m16n16k16.store.d.row.stride.f32` |
| `nvvm.wmma.mma.sync.aligned.m16n16k16.row.col.f16.f16.f16.f16` | `llvm.nvvm.wmma.m16n16k16.mma.row.col.f16.f16` |
| `nvvm.wmma.mma.sync.aligned.m16n16k16.row.col.f16.f16.f32.f32` | `llvm.nvvm.wmma.m16n16k16.mma.row.col.f32.f32` |
| `nvvm.wmma.mma.sync.aligned.m8n8k128.row.col.b1.b1.s32.s32` | `llvm.nvvm.wmma.m8n8k128.mma.row.col.b1` |
| `nvvm.wmma.mma.sync.aligned.m8n8k32.row.col.s4.s4.s32.s32` | `llvm.nvvm.wmma.m8n8k32.mma.row.col.s4` |

Every `.col.row` / `.col.col` / `.row.row` variant has a matching intrinsic name where the layout tokens after `.mma.` mirror the op's layout pair.

## PTX Templates

Once the LLVM intrinsic is selected, the NVPTX backend emits one PTX instruction. The templates below cover the canonical shape/type combinations; other combinations substitute the shape and type tokens without changing the skeleton.

| Op | PTX printed |
|---|---|
| `wmma.load.a.sync` (f16, row) | `wmma.load.a.sync.aligned.m16n16k16.row.shared::cta.f16 {%r0, %r1, %r2, %r3, %r4, %r5, %r6, %r7}, [%addr], %stride;` |
| `wmma.load.b.sync` (f16, col) | `wmma.load.b.sync.aligned.m16n16k16.col.shared::cta.f16 {%r0, %r1, %r2, %r3, %r4, %r5, %r6, %r7}, [%addr], %stride;` |
| `wmma.load.c.sync` (f32, row) | `wmma.load.c.sync.aligned.m16n16k16.row.shared::cta.f32 {%r0, %r1, %r2, %r3, %r4, %r5, %r6, %r7}, [%addr], %stride;` |
| `wmma.store.d.sync` (f32, row) | `wmma.store.d.sync.aligned.m16n16k16.row.shared::cta.f32 [%addr], {%r0, %r1, %r2, %r3, %r4, %r5, %r6, %r7}, %stride;` |
| `wmma.mma.sync` (row.col.f16.f16.f16.f16) | `wmma.mma.sync.aligned.m16n16k16.row.col.f16.f16 {%d0..%d3}, {%a0..%a7}, {%b0..%b7}, {%c0..%c3};` |
| `wmma.mma.sync` (row.col.f16.f16.f32.f32) | `wmma.mma.sync.aligned.m16n16k16.row.col.f32.f32 {%d0..%d7}, {%a0..%a7}, {%b0..%b7}, {%c0..%c7};` |
| `wmma.mma.sync` (row.col.s8.s8.s32.s32) | `wmma.mma.sync.aligned.m16n16k16.row.col.s8 {%d0..%d7}, {%a0..%a1}, {%b0..%b1}, {%c0..%c7};` |
| `wmma.mma.sync` (row.col.b1.b1.s32.s32, popc) | `wmma.mma.sync.aligned.m8n8k128.row.col.popc.b1 {%d0..%d1}, {%a0}, {%b0}, {%c0..%c1};` |

The `.popc` and `.and.popc` modifiers on the `b1` form are encoded into the op mnemonic (`nvvm.wmma.mma.sync.aligned.m8n8k128.row.col.popc.b1` vs `.and.popc.b1`). The verifier rejects any combination not listed in the PTX ISA.

## Per-Arch Availability

| Sub-family | SM floor | `ptx_min` | Notes |
|---|---|---|---|
| `f16`/`f32` accumulators | sm_70 | 6.0 | universal across Volta and later |
| `bf16` | sm_80 | 7.0 | Ampere extension |
| `tf32` | sm_80 | 7.0 | only with `m16n16k8` / `m8n32k8` / `m32n8k8` shapes |
| `s8` / `u8` | sm_72 | 6.3 | mobile + datacenter Turing onwards |
| `s4` / `u4` | sm_75 | 6.3 | `m8n8k32` shape only |
| `b1` (popc / and.popc) | sm_75 | 6.3 | `m8n8k128` shape only |

Hopper (sm_90+) backends accept `nvvm.wmma.*` for backward compatibility but Tileiras prefers [`nvvm.wgmma.mma_async.*`](wgmma-ops.md#nvvmwgmmamma_asyncsyncaligned) once the target hits sm_90a. Blackwell (sm_100+) keeps WMMA legal for short-K tiles only — long-K paths go through [`nvvm.tcgen05.mma.sync`](tcgen05-ops.md#nvvmtcgen05mma-dense). See [Per-SM Emission Templates — SM70 / SM75](../../codegen/per-sm-emission-templates.md#sm70--sm75) for the Volta/Turing PTX templates.

## Verification Invariants

- Tile shape and element-type tuple must match a row of the PTX ISA's WMMA shape table.
- A and B fragment cardinalities are derived from the shape; the verifier rejects mismatched operand counts.
- C and D layouts (`row` / `col`) must agree.
- `.popc` / `.and.popc` are legal only on the `b1` form.
- `f64` WMMA does not exist in this dialect; the FP64 MMA path uses `nvvm.mma.sync.aligned.m8n8k4.f64`.
