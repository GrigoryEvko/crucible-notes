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

## GPU Dialect Lowering

The standard GPU pass builds a conversion target that legalizes LLVM and NVVM, keeps only the GPU container operations needed while kernel bodies are rewritten, and marks the executable GPU dialect illegal. That makes failed conversions easy to diagnose — a surviving `gpu.*` op means either no pattern was registered or the pattern rejected the operation.

Two pattern families do the work. Index and control patterns rewrite GPU structural operations directly to NVVM or LLVM operations. Math patterns first scalarize vector-typed arithmetic, then emit calls to libdevice symbols appropriate for the scalar element type.

```c
void configure_gpu_to_nvvm_target(ConversionTarget *target) {
    target->add_legal_dialect("llvm");
    target->add_legal_dialect("nvvm");
    target->add_illegal_dialect("gpu");

    target->add_legal_op("gpu.module");
    target->add_legal_op("gpu.yield");

    target->add_illegal_op("func.func");
    target->add_illegal_op("cf.assert");
    target->add_illegal_op("llvm.frem");

    for (Name op : libdevice_backed_math_intrinsics()) {
        target->add_illegal_op(op);
    }
}
```

Index queries become one NVVM special-register read plus an `i32`-to-`index` extension. The shape is uniform across `thread_id`, `block_id`, `block_dim`, `grid_dim`, and the cluster-equivalent queries.

```text
input  : %i = gpu.thread_id x : index
output : %r = nvvm.read.ptx.sreg.tid.x : i32
         %i = arith.index_cast %r : i32 to index
```

The barrier rewrite is direct and must not introduce control flow.

```c
LogicalResult lower_gpu_barrier(GpuBarrierOp op, Rewriter *rewriter) {
    rewriter->replace_op_with_new_op(op, "nvvm.barrier0");
    return success();
}
```

The assertion rewrite preserves CUDA's runtime contract. Message, source file, and function name become global strings. The original predicate controls a branch — the failing edge calls `__assertfail`, the passing edge falls through.

```c
LogicalResult lower_assert(AssertOp op, Rewriter *rewriter) {
    Value ok = op.condition();

    Block *fail = rewriter->split_block_before(op);
    Block *cont = rewriter->create_block_after(fail);

    rewriter->set_insertion_point_before(op);
    rewriter->create_cond_br(ok, cont, fail);

    rewriter->set_insertion_point_to_start(fail);
    Value msg = materialize_global_string(op.message());
    Value file = materialize_global_string(op.file_name());
    Value func = materialize_global_string(op.function_name());
    Value line = rewriter->constant_i32(op.line());
    rewriter->call("__assertfail", {msg, file, line, func, rewriter->constant_i64(0)});
    rewriter->create_br(cont);

    rewriter->erase_op(op);
    return success();
}
```

For math, vector lanes get normalized before libdevice dispatch. Keep that ordering in a reimplementation: libdevice functions are scalar, and later cleanup folds scalar LLVM values far more reliably than dialect-vector calls.

```c
Value lower_libdevice_math(MathOp op, Type element_type, Rewriter *rewriter) {
    if (op.result_type().is_vector()) {
        return scalarize_lanes(op, rewriter, lower_libdevice_math);
    }

    StringRef callee = libdevice_symbol_for(op.kind(), element_type);
    SmallVector<Value> args = convert_operands(op.operands(), rewriter);
    return rewriter->call(callee, args).result(0);
}
```

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

A plain root-op switch is enough, as long as each rewrite preserves operand order and result-type conversion.

```c
LogicalResult lower_nvgpu_op(Operation *op, Rewriter *rewriter, TypeConverter *types) {
    switch (op->kind()) {
    case NVGPU_MBarrierCreate:
        return lower_mbarrier_create(op, rewriter, types);
    case NVGPU_TmaAsyncLoad:
        return lower_tma_async_load(op, rewriter, types);
    case NVGPU_WarpgroupMma:
        return lower_wgmma_pipeline(op, rewriter, types);
    case NVGPU_MmaSpSync:
        return lower_sparse_mma_inline_asm(op, rewriter, types);
    case NVGPU_DeviceAsyncCopy:
        return lower_cp_async_copy(op, rewriter, types);
    case NVGPU_PackedFma:
        return lower_packed_fma(op, rewriter, types);
    default:
        return failure();
    }
}
```

## Pattern Shapes

Every NVGPU pattern in this stage shares one outer shape: match on a root NVGPU op, convert its operands through the shared LLVM type converter, emit one or more NVVM ops plus any packing arithmetic, and replace the root. The four shapes below cover the families that need more than a single emission step; the remaining one-to-one patterns reduce to `generic_remap` from [pattern-set-and-typeconverter.md](pattern-set-and-typeconverter.md).

### Mbarrier

The mbarrier family rewrites `nvgpu.mbarrier.*` into `nvvm.mbarrier.*`. Shared-memory variants take a `!llvm.ptr<3>` barrier address; non-shared variants take a generic pointer the pattern must address-space-cast or reject.

```text
input  : %t = nvgpu.mbarrier.try_wait.parity %bar, %phase, %ticks
output : %t = nvvm.mbarrier.try_wait.parity.shared %bar, %phase, %ticks
```

```c
LogicalResult lower_mbarrier_try_wait_parity(MbarrierTryWaitParityOp op,
                                              Rewriter *rw,
                                              TypeConverter *types) {
    Value bar = convert_to_shared_ptr(op.barrier(), op.loc(), rw);
    if (!bar) return op.emit_error("mbarrier requires shared-memory operand");

    Value out = rw->create("nvvm.mbarrier.try_wait.parity.shared",
                            {bar, op.phase(), op.ticks()},
                            rw->i1_type()).result(0);
    rw->replace_op(op, out);
    return success();
}
```

### TMA Async Load and Store

`nvgpu.tma.async.load` and `nvgpu.tma.async.store` rewrite to `nvvm.cp.async.bulk.tensor.{shared.cluster.global,global.shared.cta}`, with the descriptor materialized by `nvgpu.tma.create.descriptor`. Coordinates pass as separate operands; the pattern emits one NVVM op plus the proxy fence the descriptor consumer needs.

```text
input  : nvgpu.tma.async.load %desc, %smem, %coords, %barrier
output : nvvm.cp.async.bulk.tensor.shared.cluster.global %smem, %desc, %coords, %barrier
```

```c
LogicalResult lower_tma_async_load(TmaAsyncLoadOp op, Rewriter *rw,
                                    TypeConverter *types) {
    Value smem = convert_to_shared_ptr(op.dst(), op.loc(), rw);
    Value desc = convert_to_descriptor(op.descriptor(), op.loc(), rw);
    Value bar  = convert_to_shared_ptr(op.barrier(), op.loc(), rw);
    if (!smem || !desc || !bar) return failure();

    rw->create("nvvm.cp.async.bulk.tensor.shared.cluster.global",
                concat({smem, desc, bar}, op.coords()));
    rw->erase_op(op);
    return success();
}
```

### WGMMA Pipeline

`nvgpu.warpgroup.mma` expands into a four-op sequence: pre-fence, async MMA, commit, wait. The accumulator is an aggregate the pattern emits as register-file values; the matching `nvgpu.warpgroup.generate.descriptor` pattern pre-packs the GMMA descriptors.

```text
input  : %acc' = nvgpu.warpgroup.mma %desc_a, %desc_b, %acc
output : nvvm.wgmma.fence.aligned
         %acc' = nvvm.wgmma.mma_async %desc_a, %desc_b, %acc
         nvvm.wgmma.commit.group.sync.aligned
         nvvm.wgmma.wait.group.sync.aligned 0
```

```c
LogicalResult lower_wgmma_pipeline(WarpgroupMmaOp op, Rewriter *rw,
                                    TypeConverter *types) {
    rw->create("nvvm.wgmma.fence.aligned");
    Value next = rw->create("nvvm.wgmma.mma_async",
                              {op.descA(), op.descB(), op.accumulator()},
                              op.accumulator().getType()).result(0);
    rw->create("nvvm.wgmma.commit.group.sync.aligned");
    rw->create("nvvm.wgmma.wait.group.sync.aligned",
                {rw->constant_i32(0)});
    rw->replace_op(op, next);
    return success();
}
```

### Ldmatrix and Repack

`nvgpu.ldmatrix` lowers to `nvvm.ldmatrix` and repacks the returned register
fragments into the LLVM-typed vector that the consumer expects.

```text
input  : %v = nvgpu.ldmatrix %smem, num=4, transpose=false : vector<4xi32>
output : %p = nvvm.ldmatrix %smem, num=4, transpose=false : !llvm.struct<(i32,i32,i32,i32)>
         %v = repack(%p)
```

```c
LogicalResult lower_ldmatrix(LdmatrixOp op, Rewriter *rw,
                              TypeConverter *types) {
    Value smem = convert_to_shared_ptr(op.src(), op.loc(), rw);
    Value tuple = rw->create("nvvm.ldmatrix",
                              {smem},
                              ldmatrix_result_struct(op.num(), op.element())).result(0);
    Value packed = pack_struct_into_vector(tuple, op.result().getType(), rw);
    rw->replace_op(op, packed);
    return success();
}
```

## Descriptor and Barrier Rules

Mbarrier lowering is address-space-sensitive. Shared-memory barriers use the shared NVVM variants; non-shared barrier values must be rejected or explicitly cast into a representation the target operation accepts. Token parity stays as a small integer value so wait operations can consume it directly.

TMA lowering separates descriptor construction from descriptor use. The descriptor builder materializes a tensor-map object with enough static shape, stride, element type, swizzle, rank, and interleave metadata for the CUDA-side encoder. Load, store, prefetch, and fence operations consume that descriptor later.

WGMMA descriptor packing is a pure integer operation over shared-memory base, leading-byte offset, matrix stride, swizzle mode, and base offset. Keep the packer deterministic and side-effect-free — schedulers and common-subexpression cleanup may move it across ordinary arithmetic.

```c
uint64_t pack_gmma_descriptor(GmmaDescriptorInput in) {
    uint64_t desc = 0;
    desc |= place_bits(in.matrix_base, MATRIX_BASE_FIELD);
    desc |= place_bits(in.leading_byte_offset, LBO_FIELD);
    desc |= place_bits(in.matrix_stride, MATRIX_STRIDE_FIELD);
    desc |= place_bits(in.swizzle_base, SWIZZLE_BASE_FIELD);
    desc |= place_bits(in.swizzle_mode, SWIZZLE_MODE_FIELD);
    return desc;
}
```

## Conversion Invariants

- The pass must leave no executable `gpu.*` or `nvgpu.*` operation behind.
- `gpu.module` may survive only as the module container consumed by GPU-to-binary serialization.
- Vector math is scalarized before libdevice calls are introduced.
- CUDA assertion lowering must preserve the original predicate and source metadata.
- Mbarrier variants must agree with the operand address space.
- TMA descriptor construction must be kept separate from TMA copy and prefetch operations.
- Sparse MMA uses inline assembly only for the missing dialect intrinsic; other operations should prefer first-class NVVM ops.
- WGMMA lowering must emit the fence, MMA, commit, and wait sequence in the order expected by the hardware pipeline.

## Reimplementation Checklist

1. Build a conversion target that makes GPU and NVGPU executable operations illegal.
2. Register GPU index, barrier, function, return, dynamic shared-memory, shuffle, reduce, and `printf` patterns.
3. Register scalarize-then-libdevice patterns for floating math.
4. Register the NVGPU architectural pattern table with one root operation per pattern.
5. Implement descriptor packers and TMA tensor-map construction as pure helpers.
6. Emit inline assembly only for sparse MMA, and keep its constraints local to that rewrite.
7. Run a final legality check before NVPTX serialization.
