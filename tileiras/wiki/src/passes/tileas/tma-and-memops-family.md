# TileAS TMA and Memops Family

## Abstract

The TMA and memops family owns Tensor Memory Accelerator lowering, token-ordered tiled memory ops, TMA descriptor ABI construction, host-side descriptor separation, and Blackwell tensor-memory copy legalization. The passes share descriptor indices, host/device TMA counts, kernel argument updates, and the host-code module that prepares CUDA tensor maps at launch time.

The core contract splits along the host/device line: device IR uses TileAS memory operations and TMA descriptor handles; the host side may pre-encode tensor maps and pass descriptor pointers as hidden grid-constant kernel arguments. Later NVVM lowering consumes those descriptors through `cp.async.bulk.tensor.*`, tcgen05, and related tensor-map operations.

## Pass Roster

| Pass or family | Purpose |
|---|---|
| memops verifiers | validate `tiled_load`, `tiled_store`, and `tiled_atomic_rmw` shape and attributes |
| `LowerTMALoadStoreToAsync` | rewrites eligible tiled memory ops into async TMA operations |
| `SeparateHostTMA` | hoists descriptor creation into host code and attaches object bytes to the module |
| `AttachTMADescriptorArgs` | extends kernel ABI with descriptor arguments and descriptor-count attributes |
| `TileASLegalizeTmemCopy` | rewrites TMEM-crossing copies into layouts legal for tcgen05 lowering |
| TMA descriptor builders/verifiers | build and validate `make_tiled_tma_desc` before lowering |
| tensormap mutators | update device-side tensor-map fields when descriptors are device-born |

The intended order is:

```text
AssignLoadStoreLayouts
LowerTMALoadStoreToAsync
SeparateHostTMA
AttachTMADescriptorArgs
TileASLegalizeTmemCopy
```

## TileAS TMA Operations

The TMA operation family covers async tiled load/store, async tiled reduction and atomic-like variants, gather/scatter TMA ops, the descriptor producer, and an opaque metadata type binding the TileAS descriptor to its CuTe layout and host/device index.

| Operation concept | Role |
|---|---|
| async tiled TMA load | copies tensor tiles from global tensor memory into shared or tensor memory |
| async tiled TMA store | copies tensor tiles back to global tensor memory |
| async tiled atomic/reduction | emits TMA reduction-style traffic when the atom supports it |
| gather/scatter TMA | handles non-contiguous tensor access patterns |
| make tiled TMA descriptor | captures tensor shape, strides, layout, and descriptor storage |
| tiled TMA metadata | links descriptor uses to host/device descriptor accounting |

`LowerTMALoadStoreToAsync` rewrites earlier tiled memory ops whenever the copy atom and per-op attributes permit TMA. A preference environment switch can bias eligible ops toward TMA, but verifier checks remain authoritative.

```c
LogicalResult lower_tma_load_store(FuncOp func, TmaOptions options) {
    KernelSpec spec = read_kernel_spec(func);
    if (!spec.valid()) {
        return func.emit_error("missing kernel spec for TMA lowering");
    }

    uint32_t next_tma_index = 0;
    for (MemoryOp op : func.memory_ops()) {
        if (!op.allow_tma() && !options.prefer_tma) {
            continue;
        }
        if (!op.copy_atom().supports_tma()) {
            continue;
        }

        AsyncTmaOp async = rewrite_memory_op_to_async_tma(op, next_tma_index++);
        replace_op(op, async);
    }

    return success();
}
```

## Token-Ordered Memops

`tiled_load`, `tiled_store`, and `tiled_atomic_rmw` are token-ordered memory ops. They preserve ordering and memory semantics until the async/TMA path or terminal NVVM lowering consumes them.

Verifier responsibilities:

- the operation has no unexpected regions or successors;
- `operandSegmentSizes` matches `{view, coords, offsets, token}`;
- the token segment has zero or one value;
- coordinate count and coordinate type match the view and index type;
- tile sizes are positive constants, powers of two, and within implementation limits;
- load/store/atomic memory semantics are allowed for the operation kind;
- atomic mode is compatible with the element type;
- padding values and in-bounds flags agree for stores.

```c
LogicalResult verify_tiled_memop(TiledMemOp op) {
    verify_operand_segments(op, {"view", "coords", "offsets", "token"});
    verify_optional_token_segment(op);
    verify_coordinate_types(op.view(), op.coords());
    verify_tile_dimensions(op);
    verify_memory_semantics(op);

    if (isa<TiledAtomicRmwOp>(op)) {
        verify_atomic_mode_and_element_type(op);
    }

    return success();
}
```

TMA-backed views may need one extra coordinate for descriptor-dependent offsets — im2col leading offsets on newer targets, for example.

## Descriptor ABI

`AttachTMADescriptorArgs` flips the kernel ABI from "the device builds every descriptor" to "the host or runtime passes descriptor pointers to the kernel." It counts host-side and device-side descriptors, appends descriptor pointer arguments, marks them grid constants, hides existing arguments from the public ABI view, and writes descriptor-count attributes.

```c
LogicalResult attach_tma_descriptor_args(FuncOp kernel) {
    TmaCounts counts = count_tma_descriptors(kernel);
    FunctionType old_type = kernel.get_function_type();

    SmallVector<Type> args = old_type.inputs();
    for (uint32_t i = 0; i < counts.device; ++i) {
        args.push_back(device_tma_descriptor_pointer_type(kernel.context()));
    }
    for (uint32_t i = 0; i < counts.host; ++i) {
        args.push_back(host_tma_descriptor_pointer_type(kernel.context()));
    }

    kernel.set_function_type(FunctionType::get(args, old_type.results()));
    mark_appended_descriptor_args_grid_constant(kernel, old_type.inputs().size());
    mark_existing_args_hidden(kernel, old_type.inputs().size());
    kernel.set_attr("nv_tileas.num-device-tmas", i32_attr(counts.device));
    kernel.set_attr("nv_tileas.num-host-tmas", i32_attr(counts.host));
    return success();
}
```

Descriptor-index verification confirms that every descriptor use holds a valid `tmaIdx` within the recorded host or device descriptor count.

## Separate Host TMA

`SeparateHostTMA` hoists descriptor construction into a paired host module. The host module builds CUDA tensor maps, compiles to an in-memory object, and attaches that object as module data. Device code receives pointers or runtime callback hooks instead of constructing every descriptor inline.

The pass phases are:

1. Find the enclosing kernel function.
2. Read host and device TMA counts.
3. Enforce the device-descriptor count limit.
4. Read compute capability.
5. Convert the device function signature for callback use.
6. Reject unsupported math dialect operations in host descriptor code.
7. Emit callback functions and descriptor globals.
8. Lower host-side descriptor creation to LLVM.
9. Emit pre-load callback plumbing.
10. Compile the host module to object code.
11. Attach the object bytes as host-code metadata.

```c
LogicalResult separate_host_tma(ModuleOp module, FuncOp kernel) {
    TmaCounts counts = read_tma_counts(kernel);
    if (counts.empty()) {
        return success();
    }
    if (counts.device > MAX_DEVICE_TMA_DESCRIPTORS) {
        return kernel.emit_error("too many device TMA descriptors");
    }

    ModuleOp host = create_host_descriptor_module(kernel);
    emit_tileir_callback_globals(host, kernel, counts);
    lower_tma_descriptor_builders_to_host_calls(host);
    emit_on_preload_callback(host, kernel, counts);

    ObjectBytes object = compile_host_module_to_object(host);
    module.set_attr("nv_tileas.host-code", bytes_attr(object));
    return success();
}
```

Host separation rejects descriptor builders that depend on structured control flow. Any descriptor builder moved to the host must depend only on values the callback ABI can represent.

## D15: AttachTMADescriptorArgs + SeparateHostTMA

D15 splits a tile kernel into a host module that builds and ships TMA descriptors and a device module that consumes them. The pass triple sits at `sub_7BDF00`, `sub_7BDF10`, and `sub_7BDF20`; the identity strings match the description "Attach TMA descriptor arguments and separate host TMA bookkeeping". The run body at `sub_7BE450` spans roughly 2 487 bytes of machine code.

The body walks the function once looking for `nv_tileas.make_tma_descriptor` ops. For each match, it asks the counter callback at `sub_7BE1D0` whether the descriptor is built outside the kernel boundary (host-side) or inside it (device-side), then bumps the matching tally. Once the walk finishes, two integer attributes stamp the function with the split, and each TMA-descriptor-typed kernel argument gets marked so NVPTX codegen places it in `.param` space rather than `.global`.

| Attribute | Type | Where | Meaning |
|---|---|---|---|
| `nv_tileas.host-code` | `UnitAttr` | inherent on function op | function is the host-emitter twin (vs device) |
| `nv_tileas.num-device-tmas` | `i32` | inherent on function op | count of descriptors the device side consumes |
| `nv_tileas.num-host-tmas` | `i32` | inherent on function op | count of descriptors the host side builds |
| `cute_nvgpu.grid_constant` | `UnitAttr` | argument attribute | TMA-descriptor-typed argument lives in `.param` |

The host-code options helper `sub_7BF4B0` (1 472 bytes) reads the always-on `--enable-extended-smem=true` flag from the pass-option block and threads it onto the host module's CLI tail, so host-side compilation sees the same shared-memory configuration the device side was tuned for.

The two twin modules share a parent `builtin.module`. Layout offsets `+56` and `+16` then `+56` on the parent op carry the host-twin and device-twin module references; both modules ship in the same bytecode artifact but compile separately downstream. The `cute_nvgpu.grid_constant` argument attribute is consumed later in the cute-to-llvm lowering at `sub_1698C20`, which lifts it to `nvvm.grid_constant` on the lowered function so `ptxas` places the descriptor in `.param` space.

```c
LogicalResult attachTmaArgs(FunctionOpInterface fn) {
    int host = 0, device = 0;
    fn.walk([&](Operation *op) {
        if (op->getName() != "nv_tileas.make_tma_descriptor") return;
        bool isHost = isOutsideKernel(op);
        if (isHost) ++host;
        else ++device;
    });
    fn->setAttr("nv_tileas.num-host-tmas", IntegerAttr::get(i32, host));
    fn->setAttr("nv_tileas.num-device-tmas", IntegerAttr::get(i32, device));
    for (BlockArgument arg : fn.getArguments()) {
        if (isTmaDescriptorType(arg.getType())) {
            fn.setArgAttr(arg.getArgNumber(), "cute_nvgpu.grid_constant", UnitAttr::get(ctx));
        }
    }
    return success();
}
```

The walk-once-then-stamp shape matters for reimplementation. Counting and ABI rewriting can't split into separate passes without re-walking the function — the descriptor-count attributes must land on the same op the argument attributes do, and downstream consumers expect both sides of the split (the host-code module under `nv_tileas.host-code` and the device-side argument decorations) visible in a single IR view.

## Callback ABI

The host-code path uses a small callback ABI. The module carries a callback table, per-kernel function callback slots, and an OnPreLoad hook the runtime can fill before launch.

| Symbol concept | Purpose |
|---|---|
| callback table | identifies the ABI revision and callback function pointers |
| per-kernel callback table | stores per-kernel argument-change and descriptor hooks |
| OnPreLoad hook | lets runtime patch or prepare descriptors before launch |
| host-code attribute | carries compiled host object bytes for descriptor preparation |

The ABI is deliberately table-driven so the device-facing kernel signature stays stable while host descriptor logic evolves.

## Tensor-Memory Copy Legalization

`TileASLegalizeTmemCopy` (pass D18, CLI mnemonic `"tileas-legalize-tmem-copy"` at rodata `0x46018DF`) is the Blackwell-specific rewriter that turns `nv_tileas.copy` ops crossing the TMEM boundary into pairs of legal `tcgen05.ld` / `tcgen05.st` plus `ldmatrix` / `stmatrix` sequences. It runs after D08 (`MaterializeConvertLayout`) has chosen the staging path — which memory space the values travel through — and before `ConvertTileASToLLVM` emits the corresponding NVVM intrinsics. By that point each copy carries stable source and destination memory-space tags, so the pass dispatches on a concrete TMEM-paired memory-space relation rather than rerunning layout inference.

The pass body sits at `sub_7C8920` (`0x270` bytes, 624 B). `runOnOperation` performs a function walk using `sub_7C8B90` as the filter callback; the callback gates on classID `&unk_5B44FD8` (the `nv_tileas.copy` op type) and any other op falls through untouched. The legalization core `sub_7C78A0` (`0xF8A` bytes, 3 978 B) runs once per matched copy. It first reads the source and destination memory-space tags through `sub_13C5C50`, which returns a 4-bit enum: `0` generic, `1` local, `2` shared, `3` global, `4` tmem, `5` constant. It then infers a register-side layout from the TMEM layout and a source-side layout from the TMEM layout. The two failure paths emit verbatim diagnostics `"failed to infer register layout from tmem layout"` (rodata `0x4601948`) and `"failed to infer source layout from tmem layout"` (rodata `0x4601980`); both abort the rewrite for the current copy without touching neighbouring ops.

With both layouts inferred, the rewriter dispatches on the `(srcMS, dstMS)` pair. The table below is exhaustive for the TMEM-crossing cases; every other pair was already legal after D08 and the callback leaves it alone.

| `srcMS` → `dstMS` | Legalised sequence |
|---|---|
| `4` (tmem) → `0` (rmem) | one `tcgen05.ld` per register tile |
| `0` (rmem) → `4` (tmem) | one `tcgen05.st` per register tile |
| `4` (tmem) → `2` (smem) | `tcgen05.ld` into registers, then `stmatrix.sync.aligned` to smem |
| `2` (smem) → `4` (tmem) | `ldmatrix.sync.aligned` into registers, then `tcgen05.st` to tmem |
| any other pair | pass through; D08 has already lowered or rejected it |

```c
LogicalResult legalizeTmemCopy(FunctionOpInterface fn) {
    fn.walk([&](Operation *op) {
        if (op->getName().getTypeID() != /*&unk_5B44FD8*/ COPY_TID) return;
        uint32_t srcMS = sub_13C5C50(op->getOperand(0).getType());
        uint32_t dstMS = sub_13C5C50(op->getOperand(1).getType());
        Layout regLayout, srcLayout;
        if (failed(inferRegLayoutFromTmem(op, &regLayout)))     return emit("failed to infer register layout from tmem layout");
        if (failed(inferSrcLayoutFromTmem(op, &srcLayout)))     return emit("failed to infer source layout from tmem layout");
        if      (srcMS == 4 && dstMS == 0/*RMEM*/) emitTcgen05Ld(op);
        else if (srcMS == 0 && dstMS == 4)         emitTcgen05St(op);
        else if (srcMS == 4 && dstMS == 2/*SMEM*/) { emitTcgen05Ld(op); emitStMatrix(op); }
        else if (srcMS == 2 && dstMS == 4)         { emitLdMatrix(op); emitTcgen05St(op); }
        else /* pass through */;
    });
    return success();
}
```

The pass gates on the Blackwell `tmem` subtarget feature — feature index 80 in the NVPTX subtarget table. On any target that doesn't advertise that bit, the walk still runs but the dispatch table finds no work, because no `nv_tileas.copy` op references a TMEM-tagged operand. See `codegen/nvptx-subtarget-and-feature-matrix.md` for the feature table layout. The split between layout inference and tile emission lines up with the rest of the Blackwell lowering path: `pipe-mutex-value-layout.md` describes the per-stage value layout the inferred register layout must match, `codegen/tcgen05-wgmma-mbarrier-cluster.md` covers the `tcgen05.ld` / `tcgen05.st` instruction family this pass emits, and `codegen/ldmatrix-stmatrix-and-register-class-vtables.md` documents the `ldmatrix` / `stmatrix` companion path for the SMEM-paired cases.

## Descriptor Builders and Verifiers

`make_tiled_tma_desc` construction records element bit width, tensor rank, shape, strides, padding, descriptor mode, and operand segments. Verifiers enforce:

- the descriptor points at global memory;
- composed layouts are rejected when unsupported;
- tensor rank stays within descriptor limits;
- descriptor pointer alignment is sufficient for the record;
- TMA load atom stride structure matches the input layout;
- cache-mode attributes are present and well-typed;
- descriptor-dependent values do not capture multiple incompatible sources.

```c
LogicalResult verify_tma_descriptor(MakeTiledTmaDescOp op) {
    require_global_memref(op.tensor());
    reject_unsupported_composed_layout(op.layout());
    require_rank_at_most(op.tensor(), MAX_TMA_RANK);
    require_descriptor_alignment(op.descriptor_pointer());
    verify_tma_stride_contract(op);
    verify_cache_mode(op);
    return verify_descriptor_capture(op);
}
```

## Tensormap Mutators

The CUDA driver encodes host-born descriptors. Device-born descriptors use tensor-map mutator instructions to update a small set of fields — global base address, global dimensions, global strides. The remaining descriptor fields stay fixed by the host encoder or descriptor builder.

Allocation alignment is stricter than the live record size because bulk tensor-map writes operate on a larger transaction width. A reimplementation must distinguish record alignment from allocation alignment.

## Reimplementation Invariants

- Run TMA lowering after load/store layout assignment.
- Keep token-ordered memops valid until async/TMA lowering consumes them.
- Count host and device descriptors before rewriting the kernel ABI.
- Mark appended descriptor arguments as grid constants.
- Keep descriptor indices within the recorded host/device counts.
- Reject host-separated descriptors that depend on structured control flow.
- Legalize TMEM-crossing copies only after layouts and aliases are stable.
- Distinguish host-born descriptors from device-born descriptors.

## Reimplementation Checklist

1. Verify all tiled memops before rewriting them.
2. Rewrite eligible tiled operations to async TMA operations and assign descriptor indices.
3. Hoist host descriptors into a paired host module when required.
4. Extend the kernel ABI with descriptor pointer arguments.
5. Emit descriptor-count attributes on both device and host sides.
6. Compile and attach host-code object bytes for descriptor callbacks.
7. Legalize tensor-memory boundary copies for Blackwell.
8. Keep tensormap mutators scoped to device-born descriptors.
