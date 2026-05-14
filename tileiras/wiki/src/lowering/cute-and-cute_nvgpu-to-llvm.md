# Lowering: cute / cute_nvgpu to LLVM

## Abstract

The `cute` and `cute_nvgpu` dialects carry layout algebra, tuple manipulation, descriptor iterators, and architecture-specific MMA or copy atoms. They sit beside the TileAA and TileAS pipeline rather than forming a single linear rung. Their lowering desugars high-level CuTe constructs into a primitive vocabulary, lowers layout and descriptor operations into LLVM-compatible values, then rewrites Hopper and Blackwell atom builders into the NVGPU/NVVM path.

The public contract: layout algebra stays inspectable until enough target information exists, and no CuTe-only executable operation may reach final NVPTX serialization.

## Lowering Stages

| Stage | Responsibility |
|---|---|
| `CuteDesugar` | expands syntactic sugar into primitive `cute`, `scf`, `arith`, and `memref` operations |
| `cute -> LLVM` pattern set | lowers layout tuples, descriptor iterators, pointer casts, and primitive helpers |
| `cute_nvgpu` atom lowering | rewrites SM90 and SM100 atom builders into target-specific IR |
| NVGPU/NVVM companion lowering | consumes the emitted atom, descriptor, and architectural operations |

Stage order simplifies high-level CuTe layout manipulation before architectural operations are selected.

```c
LogicalResult lower_cute_stack(ModuleOp module, LoweringOptions options) {
    if (failed(run_cute_desugar(module))) {
        return failure();
    }

    if (failed(apply_cute_to_llvm_patterns(module, options))) {
        return failure();
    }

    if (failed(lower_cute_nvgpu_atoms(module, options))) {
        return failure();
    }

    return success();
}
```

## Desugaring Contract

`CuteDesugar` rewrites high-level layout construction and inspection into primitive operations later conversion patterns can lower mechanically.

| Sugar operation | Desugared shape |
|---|---|
| `cute.make_layout` | structured loop over grouped shape and stride modes |
| `cute.make_shape` | loop-driven construction from iterator leaves |
| `cute.make_stride` | loop-driven static stride construction |
| `cute.make_tile` | primitive tile construction and dice operations |
| `cute.make_coord` | flat-coordinate extraction |
| view equality and projection | shape and stride reads followed by boolean conjunction |
| `cute.print` | element loop with coordinate materialization and scalar print |
| `cute.make_atom` | atom-interface-specific primitive atom construction |

The pass is target-neutral. It must not branch on compute capability — target selection belongs to the atom-lowering bodies and NVGPU conversion.

## Input and Output Dialects

| Direction | Surface |
|---|---|
| input ops | `cute.*` (layout, tuple, descriptor, copy, partition), `cute_nvgpu.*` (atoms, SM100 tcgen05 helpers) |
| input types | `cute::LayoutType`, `cute::ShapeType`, `cute::StrideType`, `cute::AtomType`, descriptor iterator types |
| output ops | `llvm.*` (alloca, insertvalue, extractvalue, load, store, struct construction), `nvvm.*` (tcgen05, wgmma, cp.async.bulk), `arith` and `scf` for residual control structure, `cutlass.*` for atoms forwarded into companion lowering |
| output types | layout and shape tuples become integers or `!llvm.struct`; descriptor iterators become a 3-field struct (`ptr, i32, i32`); atoms become opaque struct payloads consumed by the next stage |

## Bulk `cute -> LLVM` Conversion

A contiguous vtable bank of forty-four `{anonymous}::*OpLowering` instantiations covers the primitive CuTe surface. The bank lives at `0x59E7590..0x59E8700`, populated by `sub_16CF350` (13 393 B). Each pattern is a 0x68-B `OpConversionPattern` matching Shape B from the pattern-vtables-and-shapes document: the trailing slot at `+0x60` carries the LLVM `TypeConverter*` pointer, since almost every cute lowering needs type conversion when descending into LLVM struct, integer, and pointer representations.

The first sixteen entries cover layout construction, tuple manipulation, and the descriptor-iterator primitives that anchor the rest of the bank. The remaining twenty-eight entries continue with copy and partition helpers, fast division specializations, pointer cast bridges, and `cute_nvgpu` helper ops, ending at `0x59E8700`. A single linear sweep registers the whole bank with no conditional branches on target, so a faithful reimplementation can mirror it as a flat pattern list.

| Vtable offset | Class name |
|---|---|
| `0x59E7590` | `MakeDescriptorIteratorOpLowering` |
| `0x59E75E0` | `DescriptorAdvanceOpLowering` |
| `0x59E7630` | `DescriptorRewindOpLowering` |
| `0x59E7680` | `MakeLayoutOpLowering` |
| `0x59E76D0` | `MakeCoordOpLowering` |
| `0x59E7720` | `CrdToIdxOpLowering` |
| `0x59E7770` | `TiledDivOpLowering` |
| `0x59E77C0` | `TiledModOpLowering` |
| `0x59E7810` | `ShapeDivOpLowering` |
| `0x59E7860` | `CeilDivOpLowering` |
| `0x59E78B0` | `FilterZerosOpLowering` |
| `0x59E7900` | `GroupModesOpLowering` |
| `0x59E7950` | `CoalesceOpLowering` |
| `0x59E79A0` | `ComplementOpLowering` |
| `0x59E79F0` | `PartitionOpLowering` |
| `0x59E7A40` | `TilePartitionOpLowering` |

Descriptor iterator creation is the canonical complex pattern in the bank. The body at `sub_16F47D0` (6 296 B) emits a four-step LLVM sequence: an `arith.ceildivsi` for the total iteration count from the descriptor's shape, an `llvm.alloca` reserving a 24-B descriptor-iterator state slot on the stack, an `llvm.mlir.undef` to initialise the slot, and three `llvm.insertvalue` operations that populate the descriptor pointer, current-index, and stride fields. The resulting iterator is a three-field LLVM struct that downstream `descriptor.advance` and `descriptor.rewind` operations read and write through `llvm.extractvalue` and `llvm.insertvalue`.

```mlir
%count   = arith.ceildivsi %extent, %tile_shape : i32
%storage = llvm.alloca %c1 x !llvm.struct<(ptr, i32, i32)> : (i32) -> !llvm.ptr
%init    = llvm.mlir.undef : !llvm.struct<(ptr, i32, i32)>
%s0      = llvm.insertvalue %base,    %init[0] : !llvm.struct<(ptr, i32, i32)>
%s1      = llvm.insertvalue %c0,      %s0[1]   : !llvm.struct<(ptr, i32, i32)>
%s2      = llvm.insertvalue %stride,  %s1[2]   : !llvm.struct<(ptr, i32, i32)>
```

A secondary entry point at `sub_16D27B0` extends the bank with two `DerefineOpLowering` patterns — one for layout-projection-to-coord, one for layout-flatten — and registers the `ConvertGPUFuncSignature` rewrite that downgrades MLIR `gpu.func` signatures to LLVM `func.func`. This second registrar runs after the main bank so `cute_nvgpu` helper rewrites can rely on the primitive CuTe operations already being convertible.

## Dialect Registration Semantics

The `cute` dialect publishes a broad operation set that falls into a small number of semantic classes:

- pure layout algebra and tuple operations;
- memory-effecting load, store, and print operations;
- type-inference operations such as pointer casts and atom construction;
- verifier-heavy layout operations that reject non-positive or malformed tuple leaves;
- no-interface helper operations used as desugaring intermediates.

Model these classes explicitly in any reimplementation. The verifier is not optional: malformed CuTe tuple leaves can otherwise survive until descriptor packing, where the error becomes much harder to explain.

## Architecture-Specialized Atoms

Three large atom rewriters carry the architectural split.

| Atom | Architecture | Core behavior |
|---|---|---|
| IMMA atom | SM100 Blackwell | lowers integer MMA into tensor-memory-backed atom structure |
| WGMMA atom | SM90 Hopper | lowers warpgroup MMA into register-file accumulator and GMMA descriptors |
| S2T copy atom | SM100 Blackwell | lowers shared-memory-to-tensor-memory copy with cluster-rank handling |

Accumulator location is the critical distinction. Hopper WGMMA accumulates in the warpgroup register file; Blackwell IMMA and S2T copy use tensor memory, so their lowerings must materialize tensor-memory references and any required mbarrier ownership.

```c
LogicalResult lower_arch_atom(CuteNvgpuAtomOp op, Rewriter *rw, TargetInfo target) {
    switch (op.atom_kind()) {
    case AtomKind::Sm100Imma:
        require(target.supports_tensor_memory());
        return lower_sm100_imma_atom(op, rw);
    case AtomKind::Sm90Wgmma:
        require(target.supports_wgmma());
        return lower_sm90_wgmma_atom(op, rw);
    case AtomKind::Sm100SharedToTensorCopy:
        require(target.supports_tensor_memory());
        return lower_sm100_s2t_copy_atom(op, rw);
    default:
        return failure();
    }
}
```

## Hopper WGMMA Contract

WGMMA atom lowering builds operand descriptors for shared-memory matrices, creates a register accumulator, emits the required WGMMA fence, and packages the atom for later NVGPU/NVVM lowering. Descriptor packing is deterministic integer arithmetic and must not depend on mutable pass state.

```c
LogicalResult lower_sm90_wgmma_atom(CuteNvgpuAtomOp op, Rewriter *rw) {
    Value acc = make_register_accumulator(op.shape(), op.element_type(), rw);
    Value desc_a = make_gmma_shared_descriptor(op.operand_a(), rw);
    Value desc_b = make_gmma_shared_descriptor(op.operand_b(), rw);

    rw->create("nvvm.wgmma.fence.aligned");
    Value atom = make_cute_atom(op, {desc_a, desc_b, acc}, rw);
    rw->replace_op(op, atom);
    return success();
}
```

## Blackwell IMMA and S2T Contract

Blackwell IMMA lowers through tensor memory. The rewrite validates operand element types, builds tensor-memory destinations, initializes required mbarriers, and emits a CuTe atom payload that later tcgen05 lowering can consume.

S2T copy follows the same shape but owns cluster-rank arithmetic. For multi-CTA shapes, it reads the cluster CTA rank, computes the rank modulo the participating CTA group, and emits conditional copy structure for the selected partition.

```c
Value compute_cluster_partition(Value cta_rank, uint32_t cta_group, Rewriter *rw) {
    Value group = rw->constant_i32(cta_group);
    Value local = rw->rem_signed(cta_rank, group);
    return rw->and_i(local, rw->constant_i32(cta_group - 1));
}
```

## SM100 `retrieve_tmem_ptr` Lowering

`cute_nvgpu.arch.sm100.retrieve_tmem_ptr` converts a TMEM handle — a 32-bit token returned by `tcgen05.alloc.shared` — into a typed `i32*` pointing into the per-CTA tensor-memory file. The lowering at `sub_1146AA0` (2 341 B) emits a four-op LLVM sequence guarded by a per-function TMEM-cache hash table. Every consumer of the TMEM region calls `retrieve_tmem_ptr` independently against the same handle, and emitting `tcgen05.alloc` more than once per handle is illegal — the cache is the primary correctness mechanism. The alloc must happen exactly once per handle, and the cache turns subsequent retrieval ops into no-op rewrites that reuse the cached pointer.

The TMEM cache is a per-function open-addressing DenseMap keyed by the TMEM handle SSA value. Each slot is a 16-byte `{handle: u64, cached_ptr: void*}` pair. Sentinel handles `-4096` mark empty slots and `-8192` mark tombstones, the standard LLVM DenseMap convention. On a hit, the cached pointer is returned directly; on a miss, the lowering emits the four-op sequence below, then inserts the resulting pointer into the cache under the handle key.

When the cache misses, the rewriter emits this sequence into the LLVM IR:

```mlir
%handle    = nvvm.tcgen05.alloc.shared {num_columns = N : i32} : i32
                                                              // store handle into the function's
                                                              // tmem-alloc-handle slot for later relinquish
llvm.store %handle, %tmem_alloc_handle_slot : !llvm.ptr
%relinquish = nvvm.tcgen05.relinquish_alloc_permit            // permit other CTAs to alloc
%tmem_ptr  = llvm.load %tmem_handle_addr : !llvm.ptr -> !llvm.ptr<3>
                                                              // load the per-CTA tmem ptr from the
                                                              // shared-memory mirror at addr_space(3)
```

The kernel-entry prologue emits `tmem_alloc_handle_slot` and `tmem_handle_addr` earlier, both living in the function's stack frame, so the retrieval lowering reads them as already-allocated stack slots rather than constructing them on demand.

```c
Value lowerRetrieveTmemPtr(Op op, Value handle, ConversionPatternRewriter &rw) {
    if (auto cached = cache.lookup(handle))                             return cached;       // hit
    Value h = rw.create<nvvm::Tcgen05AllocSharedOp>(loc, /*numColumns=*/op.getN());
    rw.create<llvm::StoreOp>(loc, h, getTmemHandleSlot(op));
    rw.create<nvvm::Tcgen05RelinquishAllocPermitOp>(loc);
    Value ptr = rw.create<llvm::LoadOp>(loc, llvmPtr(/*as=*/3), getTmemHandleAddr(op));
    cache.insert(handle, ptr);
    return ptr;
}
```

The 15-pattern SM100 populator at `sub_16730B0` (6.8 KB) registers the retrieve pattern and installs every `cute_nvgpu.arch.sm100.*` pattern in one call. Its roster: `retrieve_tmem_ptr`, `tmem_load`, `tmem_store`, `tmem_alloc`, `tmem_dealloc`, and ten further tcgen05 ops including `load_b8x256` and `store_b8x256`. Each pattern is a 0x68-byte `OpConversionPattern` of the shared Shape B layout; their vtables sit consecutively in the bank `0x59EE??0..0x59EF??0`, which makes the roster easy to enumerate from the binary.

The populator gates on the `tmem` subtarget feature (index 80 in the `SubtargetFeatureKV` table; see [NVPTX Subtarget and Feature Matrix](../codegen/nvptx-subtarget-and-feature-matrix.md)). On non-Blackwell or consumer-Blackwell builds, `sub_16730B0` is invoked with a no-op flag and registers nothing, so the conversion target never accepts `cute_nvgpu.arch.sm100.*` operations and any surviving op fails legalization with a clean diagnostic.

## Conversion Invariants

- Desugaring must run before primitive CuTe conversion.
- Desugaring is target-neutral.
- Descriptor iterators must lower to a stable LLVM aggregate layout.
- CuTe tuple and layout verifiers must reject malformed non-positive leaves before descriptor construction.
- SM90 WGMMA uses register accumulators; SM100 IMMA and S2T copy use tensor-memory-backed structures.
- Atom lowerings should emit explicit diagnostics for unsupported architecture or operand type combinations.
- No CuTe-only executable operation may reach final NVPTX serialization.

## Reimplementation Checklist

1. Implement `CuteDesugar` as a target-neutral expansion pass.
2. Register primitive CuTe-to-LLVM patterns for tuple, layout, descriptor, pointer, and helper operations.
3. Model descriptor iterator layout as an explicit LLVM aggregate.
4. Keep CuTe verifiers active throughout lowering.
5. Implement separate atom rewriters for SM90 WGMMA, SM100 IMMA, and SM100 S2T copy.
6. Defer final architectural intrinsic emission to NVGPU/NVVM lowering when possible.
7. Verify that no illegal `cute` or `cute_nvgpu` operation remains before serialization.
