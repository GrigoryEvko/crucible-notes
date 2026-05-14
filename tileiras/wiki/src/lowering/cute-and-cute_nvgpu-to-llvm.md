# Lowering: cute / cute_nvgpu to LLVM

## Abstract

The `cute` and `cute_nvgpu` dialects carry layout algebra, tuple manipulation, descriptor iterators, and architecture-specific MMA or copy atoms. They sit beside the TileAA and TileAS pipeline rather than forming a single linear rung. Their lowering desugars high-level CuTe constructs into a primitive vocabulary, lowers layout and descriptor operations into LLVM-compatible values, then rewrites Hopper and Blackwell atom builders into the NVGPU/NVVM path.

The public contract: layout algebra stays inspectable until enough target information exists, and no CuTe-only executable operation may reach final NVPTX serialization.

## Lowering Stages

The CuTe lowering pipeline is three passes that run in order. Each pass owns a different layer of abstraction, and the next pass relies on the prior pass having normalised its input.

| Stage | Responsibility |
|---|---|
| `CuteDesugar` | Expands sugar into primitive `cute`, `scf`, `arith`, and `memref` operations. Target-neutral. |
| `cute -> LLVM` pattern set | Lowers layout tuples, descriptor iterators, pointer casts, and primitive helpers into LLVM-dialect values. |
| `cute_nvgpu` atom lowering | Rewrites SM90 and SM100 atom builders into target-specific NVVM and tcgen05 IR. |

Stage order simplifies high-level CuTe layout manipulation before architectural operations are selected. Desugaring must run first because the primitive CuTe lowering bank can only see what the desugarer has reduced; atom lowering runs last because its target gates depend on having LLVM-typed operands available.

## Layout Descriptors to LLVM

The translation from CuTe layout algebra to LLVM follows a single rule: each `cute` tuple becomes an `llvm.struct`, and each `Layout` becomes a sequence of `llvm.insertvalue` operations on a fresh undef of that struct type. Modes within a tuple — shape, stride, swizzle — translate independently and compose by struct nesting. A rank-2 layout, for example, packs as `!llvm.struct<(struct<(i32, i32)>, struct<(i32, i32)>)>` where the outer struct holds shape and stride and each inner struct holds the per-mode entries.

The descriptor-iterator primitive sits at the heart of this lowering. CuTe represents iteration over a layout via `cute.make_descriptor_iterator`, which the bank rewrites into a four-op LLVM sequence: a `ceildivsi` for the total iteration count, an `alloca` for the iterator state slot, an `undef` to initialise it, and three `insertvalue` operations that populate the base pointer, current index, and stride fields.

```mlir
%iter = cute.make_descriptor_iterator %base, %extent, %tile_shape, %stride
   ↓
%count   = arith.ceildivsi %extent, %tile_shape : i32
%storage = llvm.alloca %c1 x !llvm.struct<(ptr, i32, i32)> : (i32) -> !llvm.ptr
%init    = llvm.mlir.undef : !llvm.struct<(ptr, i32, i32)>
%s0      = llvm.insertvalue %base,   %init[0] : !llvm.struct<(ptr, i32, i32)>
%s1      = llvm.insertvalue %c0,     %s0[1]   : !llvm.struct<(ptr, i32, i32)>
%iter    = llvm.insertvalue %stride, %s1[2]   : !llvm.struct<(ptr, i32, i32)>
```

`descriptor.advance` and `descriptor.rewind` work directly on the resulting three-field struct — `extractvalue` to read the index, `add` or `sub` to update it, `insertvalue` to write it back. The iterator is small (24 bytes) and the LLVM optimiser usually eliminates the stack slot through SROA after inlining; emitting the alloca up front gives the optimiser a stable target to fold.

Per-shape escape hatches sit at the edges of the bank. `cute.print` desugars to an element-wise loop with coordinate materialisation and a scalar print call. `cute.make_atom` dispatches to atom-interface-specific construction. `cute.filter_zeros`, `cute.group_modes`, `cute.coalesce`, and `cute.complement` carry layout-algebra semantics: they rewrite to layout reconstruction sequences that compute new shape and stride tuples from the input layout.

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

## Bulk `cute -> LLVM` Pattern Bank

Forty-four `OpConversionPattern` classes cover the primitive CuTe surface. The first sixteen anchor the bank by lowering layout construction, tuple manipulation, and the descriptor-iterator primitives every later pattern reaches into; the remaining twenty-eight extend it with copy and partition helpers, fast-division specialisations, pointer-cast bridges, and `cute_nvgpu` helper operations. Registration is a flat linear sweep with no conditional branches on target — a faithful reimplementation mirrors the bank as a single pattern list.

The sixteen anchor classes:

| Class | Source op | Rewrite target |
|---|---|---|
| `MakeDescriptorIteratorOpLowering` | `cute.make_descriptor_iterator` | `alloca` + `undef` + three `insertvalue` (the four-op sequence above) |
| `DescriptorAdvanceOpLowering` | `cute.descriptor.advance` | `extractvalue` + `add` + `insertvalue` |
| `DescriptorRewindOpLowering` | `cute.descriptor.rewind` | `extractvalue` + `sub` + `insertvalue` |
| `MakeLayoutOpLowering` | `cute.make_layout` | `undef` + recursive `insertvalue` over shape and stride |
| `MakeCoordOpLowering` | `cute.make_coord` | Flat coordinate-tuple construction |
| `CrdToIdxOpLowering` | `cute.crd_to_idx` | Dot product of coordinate and stride tuples |
| `TiledDivOpLowering` | `cute.tiled_div` | Per-mode `divsi` |
| `TiledModOpLowering` | `cute.tiled_mod` | Per-mode `remsi` |
| `ShapeDivOpLowering` | `cute.shape_div` | Layout reconstruction after shape divide |
| `CeilDivOpLowering` | `cute.ceildiv` | `ceildivsi` lifted to LLVM scalars |
| `FilterZerosOpLowering` | `cute.filter_zeros` | Layout reconstruction skipping zero-extent modes |
| `GroupModesOpLowering` | `cute.group_modes` | Layout reconstruction with grouped modes |
| `CoalesceOpLowering` | `cute.coalesce` | Layout reconstruction with adjacent compatible modes merged |
| `ComplementOpLowering` | `cute.complement` | Layout-complement construction |
| `PartitionOpLowering` | `cute.partition` | Pointer-offset GEP plus layout adjustment |
| `TilePartitionOpLowering` | `cute.tile_partition` | Tiled-partition iteration emission |

A secondary registrar runs after the main bank and adds two `DerefineOpLowering` patterns (layout-projection-to-coord and layout-flatten) plus the `ConvertGPUFuncSignature` rewrite that downgrades `gpu.func` signatures to LLVM-compatible `func.func`. Running this registrar second matters: it lets the `cute_nvgpu` helper rewrites assume the primitive CuTe operations are already convertible, so they can compose with the bank's outputs rather than racing them.

## Dialect Registration Semantics

The `cute` dialect publishes a broad operation set that falls into a small number of semantic classes:

- pure layout algebra and tuple operations;
- memory-effecting load, store, and print operations;
- type-inference operations such as pointer casts and atom construction;
- verifier-heavy layout operations that reject non-positive or malformed tuple leaves;
- no-interface helper operations used as desugaring intermediates.

Model these classes explicitly in any reimplementation. The verifier is not optional: malformed CuTe tuple leaves can otherwise survive until descriptor packing, where the error becomes much harder to explain.

## Architecture-Specialized Atoms

Three atom rewriters carry the architectural split. They register as independent `OpConversionPattern` subclasses rather than one switch, so the dialect-conversion engine selects among them by op kind rather than by runtime dispatch inside a shared rewriter.

| Atom | Architecture | Accumulator location | Critical state |
|---|---|---|---|
| `Sm90WgmmaAtom` | SM90 Hopper | warpgroup register file | GMMA shared-memory descriptors, WGMMA fence |
| `Sm100ImmaAtom` | SM100 Blackwell | tensor memory | TMEM pointer plus mbarrier ownership |
| `Sm100S2tCopyAtom` | SM100 Blackwell | tensor memory | Cluster CTA rank for multi-CTA copy partition |

Accumulator location is the structural distinction. Hopper WGMMA accumulates in the warpgroup register file, so the rewriter materialises a register-allocated accumulator and packages it as the atom's result. Blackwell IMMA and S2T copy accumulate in tensor memory, so their rewriters materialise tensor-memory references and any required mbarrier ownership before emitting the atom.

## Hopper WGMMA Contract

The SM90 WGMMA atom rewriter builds operand descriptors for shared-memory matrices, creates a register accumulator, emits the WGMMA fence, and packages the atom for later NVGPU/NVVM lowering. Descriptor packing is deterministic integer arithmetic over the shared-memory base pointer, leading-byte offset, matrix stride, swizzle mode, and base offset — see the canonical bit layout in [MMA Atoms sm70-120 — SM90 WGMMA](../dialects/cute_nvgpu/mma-atoms-sm70-120.md#sm90-wgmma). The packer is side-effect-free so common-subexpression elimination can hoist redundant descriptor construction across loop iterations.

```mlir
%atom = cute_nvgpu.atom.sm90.wgmma %a_smem, %b_smem, %shape, %elt
   ↓
%desc_a = cute_nvgpu.gmma.descriptor %a_smem : i64       // packed bitfield
%desc_b = cute_nvgpu.gmma.descriptor %b_smem : i64
%acc    = cute_nvgpu.register.accumulator %shape, %elt   // warpgroup registers
nvvm.wgmma.fence.aligned
%atom   = cute_nvgpu.atom %desc_a, %desc_b, %acc
```

The atom is a CuTe payload, not an executable WGMMA. The fence sits between descriptor materialisation and the atom packaging because schedulers can move descriptor construction freely but cannot reorder it past the fence; emitting the fence here pins the boundary the consumer pass relies on.

## Blackwell IMMA and S2T Contract

Blackwell IMMA lowers through tensor memory rather than the register file. The rewrite validates operand element types, retrieves a tensor-memory destination via the `retrieve_tmem_ptr` lowering above, initialises any required mbarriers, and emits a CuTe atom payload that the tcgen05 path later consumes.

```mlir
%atom = cute_nvgpu.atom.sm100.imma %a_smem, %b_smem, %shape, %elt
   ↓
%tmem    = cute_nvgpu.arch.sm100.retrieve_tmem_ptr %handle, %cols
%mbar    = cute_nvgpu.mbarrier.init %ticks
%atom    = cute_nvgpu.atom %a_smem, %b_smem, %tmem, %mbar
```

S2T copy follows the same shape and additionally owns cluster-rank arithmetic. For multi-CTA shapes, the rewriter reads the cluster CTA rank, computes the rank modulo the participating CTA group, and emits the conditional copy structure for the selected partition. The partition computation reduces to two integer operations: `rank % cta_group` for the local index and a bitwise mask `(rank % cta_group) & (cta_group - 1)` that the rewriter folds into the destination address arithmetic when `cta_group` is a power of two.

## SM100 `retrieve_tmem_ptr` Lowering

`cute_nvgpu.arch.sm100.retrieve_tmem_ptr` converts a TMEM handle — a 32-bit token returned by `tcgen05.alloc.shared` — into a typed `i32*` pointing into the per-CTA tensor-memory file. Multiple consumers in the same kernel call `retrieve_tmem_ptr` against the same handle, and emitting `tcgen05.alloc` more than once for one handle is illegal hardware behaviour. A per-function cache keyed by the handle SSA value is therefore the primary correctness mechanism: the first retrieval emits the alloc, subsequent retrievals reuse the cached pointer.

On a cache hit the rewrite is a no-op replacement with the cached pointer. On a miss the rewriter emits a four-op LLVM sequence and inserts the resulting pointer into the cache under the handle key:

```mlir
%handle  = cute_nvgpu.arch.sm100.retrieve_tmem_ptr %tmem_handle, %num_columns
   ↓
%handle    = nvvm.tcgen05.alloc.shared {num_columns = N : i32} : i32
llvm.store %handle, %tmem_alloc_handle_slot : !llvm.ptr     // for later relinquish
nvvm.tcgen05.relinquish_alloc_permit                         // permit other CTAs to alloc
%tmem_ptr  = llvm.load %tmem_handle_addr : !llvm.ptr -> !llvm.ptr<3>
```

The kernel-entry prologue emits `tmem_alloc_handle_slot` and `tmem_handle_addr` earlier, both living in the function's stack frame, so the retrieval lowering reads them as already-allocated stack slots rather than constructing them on demand.

```c
Value lowerRetrieveTmemPtr(RetrieveTmemPtrOp op, Value handle,
                           ConversionPatternRewriter &rw,
                           DenseMap<Value, Value> &cache) {
    if (auto cached = cache.lookup(handle))                       return cached;
    Value h    = rw.create<nvvm::Tcgen05AllocSharedOp>(loc, op.getNumColumns());
    rw.create<llvm::StoreOp>(loc, h, getTmemHandleSlot(op));
    rw.create<nvvm::Tcgen05RelinquishAllocPermitOp>(loc);
    Value ptr  = rw.create<llvm::LoadOp>(loc, llvmPtr(/*as=*/3), getTmemHandleAddr(op));
    cache.insert({handle, ptr});
    return ptr;
}
```

The SM100 populator installs fifteen patterns in one call. The roster covers `retrieve_tmem_ptr`, `tmem_load`, `tmem_store`, `tmem_alloc`, `tmem_dealloc`, and ten further tcgen05 operations including `load_b8x256` and `store_b8x256`. The populator gates on the `tmem` subtarget feature (see [NVPTX Subtarget and Feature Matrix — Cached Tensor-Memory Predicate](../codegen/nvptx-subtarget-and-feature-matrix.md#cached-tensor-memory-predicate)); on non-Blackwell or consumer-Blackwell builds the populator is invoked with a no-op flag and registers nothing, so the conversion target never accepts `cute_nvgpu.arch.sm100.*` operations and any surviving op fails legalisation with a clean diagnostic.

## Conversion Invariants

- Desugaring must run before primitive CuTe conversion.
- Desugaring is target-neutral.
- Descriptor iterators must lower to a stable LLVM aggregate layout.
- CuTe tuple and layout verifiers must reject malformed non-positive leaves before descriptor construction.
- SM90 WGMMA uses register accumulators; SM100 IMMA and S2T copy use tensor-memory-backed structures.
- Atom lowerings should emit explicit diagnostics for unsupported architecture or operand type combinations.
- No CuTe-only executable operation may reach final NVPTX serialization.

## Cross-References

[Conversion / Lowering Overview](overview.md#lowering-stages) places CuTe lowering in the companion-dialect stage that runs after TileAS bodies have lowered to LLVM. [nvgpu / gpu to NVVM — NVGPU Dialect Lowering](nvgpu-and-gpu-to-nvvm.md#nvgpu-dialect-lowering) is the sister pass that consumes the architectural atoms this pass emits — WGMMA atoms go to `nvvm.wgmma.*`, IMMA and S2T copy atoms go to `nvvm.tcgen05.*`. [TileAS to LLVM — Body Conversion Phases](tileas-to-llvm.md#body-conversion-phases) emits the residual `cute_nvgpu.arch.sm100.retrieve_tmem_ptr` operations this pass resolves through the per-function TMEM cache. [MMA Atoms sm70-120 — SM90 WGMMA](../dialects/cute_nvgpu/mma-atoms-sm70-120.md#sm90-wgmma) carries the canonical bit layout for GMMA descriptors.

