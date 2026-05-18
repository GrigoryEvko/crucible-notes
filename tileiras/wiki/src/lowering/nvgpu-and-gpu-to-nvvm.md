# Lowering: nvgpu / gpu to NVVM

## Abstract

This lowering family is the final MLIR-side step. It strips the standard `gpu` and `nvgpu` dialects from a Tileiras kernel module: portable GPU concepts (thread indices, barriers, dynamic shared memory, subgroup operations, `printf`) and NVIDIA-specific operations (async-copy, tensor-memory, mbarrier, WGMMA, sparse MMA, packed arithmetic) all become NVVM and LLVM operations the NVPTX backend can consume.

The contract is semantic, not archaeological: once these conversions run, no executable `gpu.*` or `nvgpu.*` operation should remain. The resulting module contains `llvm.*`, `nvvm.*`, and a small set of explicitly legal container or bridge operations that later serialization already understands.

## Boundary Contract

Two related but distinct jobs share this pass.

`gpu -> nvvm` lowers the standard MLIR GPU dialect: thread and block index queries, cluster index queries, barriers, GPU function boundaries, GPU returns, dynamic shared memory, shuffle/reduce operations, `printf`, and math operations that need libdevice calls.

`nvgpu -> nvvm` lowers NVIDIA architectural operations: mbarrier operations, TMA tensor copy operations, descriptor construction and prefetching, WGMMA descriptor and accumulator operations, synchronous MMA, ldmatrix, SM80-style `cp.async`, sparse MMA, reciprocal approximation, packed float conversion, and packed `f32x2` arithmetic.

The conversion target is strict:

| Input concept | Output form |
|---|---|
| `gpu.thread_id`, `gpu.block_id`, dimension queries | `nvvm.read.ptx.sreg.*` and integer arithmetic |
| `gpu.barrier` | `nvvm.barrier0` |
| `cf.assert` in GPU code | guarded call to CUDA-compatible `__assertfail` |
| `gpu.printf` | `vprintf` call with lowered format and argument buffer |
| `math.*` operations that require device helpers | scalarized libdevice `__nv_*` calls |
| `nvgpu.mbarrier.*` | `nvvm.mbarrier.*`, usually with shared-memory variants |
| `nvgpu.tma.*` | `nvvm.cp.async.bulk.tensor.*`, tensor-map helpers, and proxy fences |
| `nvgpu.warpgroup.*` | WGMMA NVVM operations plus LLVM value packing |
| `nvgpu.mma.sync`, `nvgpu.ldmatrix` | matching NVVM matrix intrinsics plus LLVM repacking |
| `nvgpu.device_async_*` | SM80 `nvvm.cp.async.*` group operations |
| `nvgpu.mma.sp.sync` | `llvm.inline_asm` carrying the PTX sparse-MMA instruction |
| SM100 packed arithmetic and conversion ops | dedicated `nvvm.*` packed operations |

Violation behavior is uniform across the two halves of the pass: any executable `gpu.*` or `nvgpu.*` op remaining after the partial conversion is a hard failure — `applyPartialConversion` reports the unconverted op and the pass fails. An `nvgpu.mbarrier.*` op whose operand does not resolve to a shared-memory pointer emits `"mbarrier requires shared-memory operand"` and rejects, because an implicit address-space cast would change the semantic memory space tcgen05 lowering relies on. A vector-typed `math.*` operation that reaches libdevice dispatch without prior scalarisation is rejected by the conversion target rather than dispatched lane-by-lane silently. A `cf.assert` whose message globals cannot be materialised falls through to the upstream LLVM diagnostic. The `gpu.module` container itself is the only legal `gpu.*` surface on output; any other surviving `gpu.*` op signals a missing pattern in this bank.

## GPU Dialect Lowering

The standard GPU pass builds a conversion target that legalises LLVM and NVVM, keeps `gpu.module` and `gpu.yield` legal so kernel bodies can be rewritten in place, marks the rest of the GPU dialect illegal, and adds libdevice-backed math operations and `cf.assert` to the illegal set. A surviving `gpu.*` op after this pass means either no pattern was registered or the pattern rejected the operation; the strict target makes the failure mode visible.

### Index Queries

Thread, block, cluster, and grid index queries each rewrite to one NVVM special-register read plus an `i32`-to-`index` cast. The shape is uniform across the family — only the special-register name varies.

```mlir
%i = gpu.thread_id x : index
   ↓
%r = nvvm.read.ptx.sreg.tid.x : i32
%i = arith.index_cast %r : i32 to index
```

The full mapping covers nine source operations:

| Source | Special register |
|---|---|
| `gpu.thread_id {x,y,z}` | `nvvm.read.ptx.sreg.tid.{x,y,z}` |
| `gpu.block_id {x,y,z}` | `nvvm.read.ptx.sreg.ctaid.{x,y,z}` |
| `gpu.block_dim {x,y,z}` | `nvvm.read.ptx.sreg.ntid.{x,y,z}` |
| `gpu.grid_dim {x,y,z}` | `nvvm.read.ptx.sreg.nctaid.{x,y,z}` |
| `gpu.cluster_id {x,y,z}` | `nvvm.read.ptx.sreg.clusterid.{x,y,z}` |
| `gpu.cluster_dim {x,y,z}` | `nvvm.read.ptx.sreg.nclusterid.{x,y,z}` |
| `gpu.cluster_block_id {x,y,z}` | `nvvm.read.ptx.sreg.cluster.ctaid.{x,y,z}` |
| `gpu.subgroup_size` | `nvvm.read.ptx.sreg.warpsize` |
| `gpu.lane_id` | `nvvm.read.ptx.sreg.laneid` |

### Barrier

The CTA-wide barrier rewrite is one-to-one and must not introduce control flow — schedulers downstream rely on a barrier appearing exactly where the source op did.

```mlir
gpu.barrier
   ↓
nvvm.bar.sync.aligned %c0 : i32
```

The `aligned` variant is mandatory: tileiras kernels always launch with warp-aligned thread counts, and the non-aligned barrier would force a fallback path the scheduler has not budgeted for.

### Assert

`cf.assert` preserves CUDA's runtime contract. Message, source file, and function name become module-level global strings; the original predicate controls a conditional branch where the failing edge calls `__assertfail` and the passing edge falls through.

```mlir
cf.assert %cond, "message" : i1
   ↓
llvm.cond_br %cond, ^cont, ^fail
^fail:
  %msg  = llvm.mlir.addressof @.assert_msg  : !llvm.ptr
  %file = llvm.mlir.addressof @.assert_file : !llvm.ptr
  %func = llvm.mlir.addressof @.assert_func : !llvm.ptr
  llvm.call @__assertfail(%msg, %file, %line, %func, %c0_i64) : ...
  llvm.br ^cont
^cont:
  ...
```

`__assertfail` is the CUDA runtime symbol the linker resolves; the signature `(char*, char*, i32 line, char*, i64 charSize)` is fixed by the runtime ABI and any reimplementation must call it with exactly those argument types in that order.

### Libdevice Math

Vector lanes are scalarised before libdevice dispatch because libdevice functions are scalar and downstream cleanup folds scalar LLVM operations far more reliably than dialect-vector calls. The rewriter walks vector results, emits a per-lane libdevice call selected by element type, and reconstructs the vector via `insertelement`.

```mlir
%r = math.sqrt %v : vector<4xf32>
   ↓
%v0 = vector.extract %v[0] : f32 from vector<4xf32>
%v1 = vector.extract %v[1] : f32 from vector<4xf32>
%v2 = vector.extract %v[2] : f32 from vector<4xf32>
%v3 = vector.extract %v[3] : f32 from vector<4xf32>
%r0 = llvm.call @__nv_sqrtf(%v0) : (f32) -> f32
%r1 = llvm.call @__nv_sqrtf(%v1) : (f32) -> f32
%r2 = llvm.call @__nv_sqrtf(%v2) : (f32) -> f32
%r3 = llvm.call @__nv_sqrtf(%v3) : (f32) -> f32
%r  = vector.from_elements %r0, %r1, %r2, %r3 : vector<4xf32>
```

The callee name comes from a `(MathOpKind, ElementType)` table: `math.sqrt` of `f32` selects `__nv_sqrtf`, of `f64` selects `__nv_sqrt`, of `f16` selects `__nv_sqrtf` with operand promotion. Reflection-resolved variants for fast-math and unsafe-math intrinsics (`__nv_fast_sinf`, `__nv_unsafe_divf`) attach via the `fastmath` attribute on the source op.

## NVGPU Dialect Lowering

The NVGPU conversion is a table-driven pattern set. Each pattern has one root operation and a typed `matchAndRewrite` body. Most emit a single NVVM operation. A handful are structural: tensor-map descriptor construction writes an LLVM stack object, WGMMA store decomposes an accumulator into per-thread stores, and sparse MMA emits inline assembly because the dialect snapshot doesn't model that instruction as a first-class NVVM op.

| Source family | Lowering behavior |
|---|---|
| `nvgpu.mbarrier.create` | creates or references a private shared-memory barrier object |
| `nvgpu.mbarrier.init` | initializes the barrier with the requested participant count |
| `nvgpu.mbarrier.arrive*` | emits arrival, no-complete, and expect-transaction NVVM intrinsics |
| `nvgpu.mbarrier.test.wait` | tests and waits on a phase or token |
| `nvgpu.mbarrier.try_wait.parity` | emits the parity-sensitive wait primitive |
| `nvgpu.tma.async.load` | emits tensor bulk copy from global tensor memory into shared memory |
| `nvgpu.tma.async.store` | emits tensor bulk copy from shared memory back to global tensor memory |
| `nvgpu.tma.create.descriptor` | builds the tensor-map descriptor that the CUDA driver can encode |
| `nvgpu.tma.prefetch.descriptor` | emits tensor-map prefetch |
| `nvgpu.tma.fence.descriptor` | emits proxy acquire fence for descriptor visibility |
| `nvgpu.warpgroup.generate.descriptor` | packs the GMMA shared-memory descriptor bitfields |
| `nvgpu.warpgroup.mma` | emits WGMMA fence, async MMA, commit, and wait operations |
| `nvgpu.warpgroup.mma.store` | maps accumulator fragments to per-thread stores |
| `nvgpu.warpgroup.mma.init.accumulator` | builds the zero or poison accumulator aggregate |
| `nvgpu.mma.sync` | emits synchronous MMA NVVM intrinsic |
| `nvgpu.ldmatrix` | emits ldmatrix and repacks the returned fragments |
| `nvgpu.device_async_copy` | emits SM80 `cp.async.shared.global` |
| `nvgpu.device_async_create_group` | emits `cp.async.commit.group` |
| `nvgpu.device_async_wait` | emits `cp.async.wait.group` |
| `nvgpu.mma.sp.sync` | emits sparse MMA inline assembly |
| `nvgpu.rcp` | emits reciprocal approximation |
| `nvgpu.cvt_fptrunc`, `nvgpu.cvt_fpext` | emits packed float conversion |
| `nvgpu.fma.packed.f32x2`, `nvgpu.mul.packed.f32x2` | emits packed `f32x2` arithmetic |

Each entry above is a distinct `OpConversionPattern` subclass registered against its root op. The conversion engine selects among them by op kind; there is no shared dispatcher inside a single rewriter.

## Pattern Shapes

Every NVGPU pattern in this stage shares one outer shape: match on a root NVGPU op, convert its operands through the shared LLVM type converter, emit one or more NVVM ops plus any packing arithmetic, and replace the root. The four shapes below cover the families that need more than a single emission step; the remaining one-to-one patterns reduce to `generic_remap` from [the 43-instantiation arith bank](pattern-set-and-typeconverter.md#the-43-instantiation-arith-bank).

### Mbarrier

The mbarrier family rewrites the five `nvgpu.mbarrier.*` operations into matching `nvvm.mbarrier.*` intrinsics. Shared-memory variants take a `!llvm.ptr<3>` barrier address; non-shared variants take a generic pointer the rewriter must address-space-cast to shared or reject.

```mlir
%bar = nvgpu.mbarrier.create : !nvgpu.mbarrier
   ↓
%bar = llvm.mlir.addressof @mbar_storage : !llvm.ptr<3>

nvgpu.mbarrier.init %bar, %count : !nvgpu.mbarrier, i32
   ↓
nvvm.mbarrier.init.shared %bar, %count : !llvm.ptr<3>, i32

nvgpu.mbarrier.arrive %bar : !nvgpu.mbarrier -> !nvgpu.token
   ↓
%tok = nvvm.mbarrier.arrive.shared %bar : !llvm.ptr<3> -> i64

nvgpu.mbarrier.arrive.expect_tx %bar, %tx_count : !nvgpu.mbarrier, i32
   ↓
nvvm.mbarrier.arrive.expect_tx.shared %bar, %tx_count : !llvm.ptr<3>, i32

%t = nvgpu.mbarrier.try_wait.parity %bar, %phase, %ticks
   ↓
%t = nvvm.mbarrier.try_wait.parity.shared %bar, %phase, %ticks : !llvm.ptr<3>, i1, i32 -> i1

nvgpu.mbarrier.inval %bar : !nvgpu.mbarrier
   ↓
nvvm.mbarrier.inval.shared %bar : !llvm.ptr<3>
```

If the source operand does not resolve to a shared-memory pointer, the rewriter emits `"mbarrier requires shared-memory operand"` and fails. The pattern rejects rather than inserts an implicit cast because the cast would change the semantic memory space and downstream tcgen05 lowering depends on shared-memory residence.

### TMA Async Load

`nvgpu.tma.async.load` rewrites to `nvvm.cp.async.bulk.tensor.shared.cluster.global` with the descriptor pointer, coordinate operands, and barrier. Optional attributes — `multicastMask` and `l2CacheHint` — wire into the intrinsic's optional argument slots when present.

```mlir
nvgpu.tma.async.load %desc, %smem, %coords[%c0, %c1], %barrier
   { multicastMask = 0x000F : i16, l2CacheHint = 0xCAFE : i64 }
   ↓
nvvm.cp.async.bulk.tensor.shared.cluster.global.5d
   %smem, %desc, %barrier, %c0, %c1, %c2, %c3, %c4,
   multicast_mask = %mask, l2_cache_hint = %hint
   : !llvm.ptr<3>, !llvm.ptr<1>, !llvm.ptr<3>, i32 x 5, i16, i64
```

<a id="tma-async-load-operand-mapping"></a>

#### Operand mapping (rank N)

The intrinsic signature for `nvvm.cp.async.bulk.tensor.{N}d.shared.cluster.global.tile` is rank-parameterised; the `multicastMask` and `l2CacheHint` operands are optional. The rewriter maps the flat `nvgpu` operand list onto positional intrinsic slots and sets two `Unit`-typed enable attributes that gate the optional slots.

| `nvgpu.tma.async.load` operand | NVVM intrinsic slot |
|---|---|
| `dst` (SMEM memref, addr-space 3) | slot 0 — `dstAddr : ptr addrspace(3)` |
| `tensorMapDescriptor` | slot 1 — `tensorMap : ptr` to the 128-byte `CUtensorMap` |
| `coordinates[0..N-1]` | slots 2..N+1 — `coords : i32`, one per rank |
| `barrier` | slot N+2 — `barrier : ptr addrspace(3)` |
| `multicastMask` (optional) | slot N+3 — `multicastMask : i16` |
| `l2CacheHint` (optional) | slot N+4 — `cacheHint : i64` |

The two `Unit` attributes (`multicastEnable`, `cacheHintEnable`) are not nvgpu attributes — they are produced by the rewriter from operand presence. When `multicastMask` is supplied, the rewriter sets `multicastEnable = unit` on the new `nvvm.*` op; otherwise it leaves both operand and enable absent. The same rule applies to `l2CacheHint` / `cacheHintEnable`.

Worked example, 3-D TMA load with both optional operands:

```text
%smem        : memref<128x128xf16, 3>
%bar         : !nvgpu.mbarrier.group
%tmap        : !nvgpu.tensormap.descriptor
%c0,%c1,%c2  : i32
%mask        : i16
%hint        : i64

input :
  nvgpu.tma.async.load %smem[%c0,%c1,%c2], %bar, %tmap,
                       multicastMask = %mask,
                       l2CacheHint   = %hint

output :
  %smem_ptr = unrealized_conversion_cast %smem : memref<128x128xf16, 3> to !llvm.ptr<3>
  %bar_ptr  = ...                              : !llvm.ptr<3>
  %tmap_ptr = ...                              : !llvm.ptr
  nvvm.cp.async.bulk.tensor.3d.shared.cluster.global.tile
      %smem_ptr,                  // slot 0
      %tmap_ptr,                  // slot 1
      %c0, %c1, %c2,              // slots 2..4
      %bar_ptr,                   // slot 5
      %mask,                      // slot 6 (multicast)
      %hint                       // slot 7 (cache hint)
      { multicastEnable, cacheHintEnable, mode = #nvvm.load_mode<tile> }
```

If `%mask` is absent, slot 6 is dropped and `multicastEnable` is not set; slot 7 (if `%hint` is present) shifts left into slot 6 of the actually-emitted call. The intrinsic ID stays the same; only the operand bag changes width. Absent operands leave slots unset rather than emitting zero constants — a zero `cacheHint` would force a non-default code path in the backend.

### TMA Async Store

`nvgpu.tma.async.store` is the symmetric reverse direction. The descriptor and coordinates appear in the same operand positions; the source becomes shared memory and the destination becomes global memory. There is no barrier — the producer issues the store and continues.

```mlir
nvgpu.tma.async.store %smem, %desc, %coords[%c0, %c1]
   ↓
nvvm.cp.async.bulk.tensor.global.shared.cta.5d
   %desc, %smem, %c0, %c1, %c2, %c3, %c4
   : !llvm.ptr<1>, !llvm.ptr<3>, i32 x 5
```

Operand mapping (rank N):

| `nvgpu.tma.async.store` operand | NVVM intrinsic slot |
|---|---|
| `tensorMapDescriptor` | slot 0 — `tensorMap : ptr` |
| `coordinates[0..N-1]` | slots 1..N — `coords : i32` |
| `src` (SMEM memref, addr-space 3) | slot N+1 — `srcAddr : ptr addrspace(3)` |
| `l2CacheHint` (optional) | slot N+2 — `cacheHint : i64`, gated by `cacheHintEnable` |

For the reduce variant (`nvgpu.tma.async.reduce`), the `redop` attribute selects the intrinsic ID at registration time — eight distinct intrinsics exist per rank, one per reduction kind. The operand mapping mirrors the store form; the rewriter copies `redop` into the `red_op` slot of the new `nvvm.cp.async.bulk.tensor.reduce` op so verifier and PTX emission see the same enum.

The fence pattern `nvgpu.tma.fence.descriptor` rewrites to `nvvm.fence.proxy.acquire.sync` so descriptor updates from the CUDA host become visible to the device proxy before the next async load.

### WGMMA Pipeline

`nvgpu.warpgroup.mma` expands into the four-op WGMMA protocol the hardware expects: fence, async issue, commit, wait. The accumulator is an aggregate the pattern emits as register-file values; the matching `nvgpu.warpgroup.generate.descriptor` pattern pre-packs the GMMA descriptors.

```mlir
%acc' = nvgpu.warpgroup.mma %desc_a, %desc_b, %acc
   ↓
nvvm.wgmma.fence.aligned
%acc' = nvvm.wgmma.mma_async %desc_a, %desc_b, %acc : i64, i64, !llvm.struct<(f32, f32, ...)>
nvvm.wgmma.commit.group.sync.aligned
nvvm.wgmma.wait.group.sync.aligned 0
```

The four ops must appear in order: the fence ensures prior shared-memory stores are visible to the WGMMA pipeline; `mma_async` issues the operation; `commit.group` packages it into a group the warpgroup tracks; `wait.group 0` blocks until the in-flight group count reaches zero. Reordering any pair changes the semantics — a missing fence loses input-dependence guarantees, and a missing wait races the accumulator into downstream reads.

### Ldmatrix and Repack

`nvgpu.ldmatrix` rewrites to `nvvm.ldmatrix.sync` and repacks the returned register fragments into the LLVM-typed vector the consumer expects. The shape and transpose attributes pass through verbatim onto the intrinsic.

```mlir
%v = nvgpu.ldmatrix %smem, num=4, transpose=false : memref<*xi32, 3>, vector<4xi32>
   ↓
%p = nvvm.ldmatrix.sync %smem, num=4, trans=false
   : !llvm.ptr<3> -> !llvm.struct<(i32, i32, i32, i32)>
%v0 = llvm.extractvalue %p[0] : !llvm.struct<(i32, i32, i32, i32)>
%v1 = llvm.extractvalue %p[1] : !llvm.struct<(i32, i32, i32, i32)>
%v2 = llvm.extractvalue %p[2] : !llvm.struct<(i32, i32, i32, i32)>
%v3 = llvm.extractvalue %p[3] : !llvm.struct<(i32, i32, i32, i32)>
%v  = vector.from_elements %v0, %v1, %v2, %v3 : vector<4xi32>
```

The fragment count (1, 2, or 4) selects the struct shape: `num=1` returns a single `i32`, `num=2` returns `!llvm.struct<(i32, i32)>`, `num=4` returns `!llvm.struct<(i32, i32, i32, i32)>`. The repack always uses `extractvalue` + `vector.from_elements` so the consumer sees a uniform vector regardless of fragment count.

### Device Async Copy (SM80)

`nvgpu.device_async_copy` rewrites to SM80-era `cp.async`. The associated group and wait operations rewrite one-to-one.

```mlir
%tok = nvgpu.device_async_copy %gmem, %smem, %size : memref<*xf32, 1>, memref<*xf32, 3>
   ↓
nvvm.cp.async.shared.global %smem, %gmem, %size : !llvm.ptr<3>, !llvm.ptr<1>, i32

nvgpu.device_async_create_group [%tok0, %tok1, ...] : !nvgpu.token
   ↓
nvvm.cp.async.commit.group

nvgpu.device_async_wait %group { numGroups = 0 : i32 }
   ↓
nvvm.cp.async.wait.group 0
```

Async tokens lower to `i32` integer values; the create-group operation discards its token operands because `cp.async.commit.group` operates on the implicit in-flight group rather than on explicit token list.

### Sparse MMA Inline Assembly

`nvgpu.mma.sp.sync` has no first-class NVVM op in the current dialect snapshot, so the rewriter emits the PTX sparse-MMA instruction through `llvm.inline_asm`. This is the only operation in the bank that uses inline assembly; prefer NVVM intrinsics for everything else.

## Descriptor and Barrier Rules

Mbarrier lowering is address-space-sensitive. Shared-memory barriers use the `.shared` NVVM variants; non-shared barrier values are rejected with a diagnostic rather than silently cast, because the cast would change the semantic memory space and downstream tcgen05 lowering depends on shared-memory residence. Token parity stays as a small integer value so wait operations can consume it directly without unpacking.

TMA lowering separates descriptor construction from descriptor use. `nvgpu.tma.create.descriptor` materialises a 128-byte tensor-map object on the function's stack and populates it with the static shape, stride, element-type, swizzle, rank, and interleave fields the CUDA-side encoder reads. Load, store, prefetch, and fence operations consume that descriptor pointer — they never reconstruct the descriptor from its fields, so descriptor canonicalisation can hoist construction freely.

For device-side descriptor rebind, this pass emits the inline-asm `tensormap.replace.tile.*` calls — `global_address` once, `global_dim` once per rank, `global_stride` once per non-leading rank — wrapped in the `fence.proxy.tensormap::generic` acquire/release pair so the generic-proxy write becomes visible to the tensormap proxy that `cp.async.bulk.tensor.*` reads from. The mutator templates, descriptor field layout, and fence-scope selection (`.cta` vs `.gpu` vs `.sys`) are documented in [TMA Descriptor Mutators](../codegen/tma-tensormap-and-cp-async-bulk.md#tma-descriptor-mutators). The rewrite contract here is that the rewriter emits exactly that fixed sequence — any deviation (writing strides before dims, omitting the acquire fence, scoping to `.cta` across a cluster) leaves the descriptor partially coherent and the consumer reads stale lanes.

WGMMA descriptor packing is a pure integer operation over five inputs: the shared-memory base pointer, leading-byte offset, matrix stride, swizzle base, and swizzle mode. The 64-bit layout is fixed by the Hopper GMMA ISA — bit positions and field widths are documented in [MMA Atoms sm70-120 — SM90 WGMMA](../dialects/cute_nvgpu/mma-atoms-sm70-120.md#sm90-wgmma). The packer is deterministic and side-effect-free, so schedulers and common-subexpression elimination can hoist redundant descriptor construction across loop iterations.

```mlir
%desc = nvgpu.warpgroup.generate.descriptor %smem_base
   { leading_byte_offset = 16 : i64, matrix_stride = 64 : i64,
     swizzle_base = 128 : i64, swizzle_mode = #nvgpu<swizzle 128B> }
   ↓
%bits   = arith.constant 0x... : i64           // pre-folded bit pattern from attribute fields
%base_i = llvm.ptrtoint %smem_base : !llvm.ptr<3> to i64
%desc   = llvm.or %bits, %base_i : i64
```

The runtime base pointer is the only operand that varies per instance; everything else folds at compile time from the GMMA-descriptor attribute, so the generated LLVM is typically two instructions (`ptrtoint` plus `or`) per descriptor.

## Conversion Invariants

- The pass must leave no executable `gpu.*` or `nvgpu.*` operation behind.
- `gpu.module` may survive only as the module container consumed by GPU-to-binary serialization.
- Vector math is scalarized before libdevice calls are introduced.
- CUDA assertion lowering must preserve the original predicate and source metadata.
- Mbarrier variants must agree with the operand address space.
- TMA descriptor construction must be kept separate from TMA copy and prefetch operations.
- Sparse MMA uses inline assembly only for the missing dialect intrinsic; other operations should prefer first-class NVVM ops.
- WGMMA lowering must emit the fence, MMA, commit, and wait sequence in the order expected by the hardware pipeline.

## Cross-References

[Conversion / Lowering Overview](overview.md#lowering-stages) places this pass at the companion-lowering stage that runs alongside CuTe lowering. [CuTe and CuTe-NVGPU to LLVM — Architecture-Specialized Atoms](cute-and-cute_nvgpu-to-llvm.md#architecture-specialized-atoms) covers the CuTe atom rewrites whose outputs this pass consumes through `cute_nvgpu.atom`. [Shared LLVM Type Converter](pattern-set-and-typeconverter.md#shared-llvm-type-converter) describes the shared LLVM type converter every pattern in this bank threads through. [MMA Atoms sm70-120 — SM90 WGMMA](../dialects/cute_nvgpu/mma-atoms-sm70-120.md#sm90-wgmma) is the canonical reference for the WGMMA descriptor bit layout the packer above emits.

