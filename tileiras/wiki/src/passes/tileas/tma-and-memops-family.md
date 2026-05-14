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

## LowerTMALoadStoreToAsync

`LowerTMALoadStoreToAsync` converts synchronous tiled load/store ops carrying a TMA copy-atom into the four-op async sequence the downstream NVVM lowering expects: descriptor bind, async bulk-tensor op, mbarrier wait, fence. The CLI mnemonic is `lower-tma-load-store-to-async` and the description string registered with the pass infrastructure reads "lowering TiledLoad or TiledStore which with tma atom to async tiled load or tiled store". The pass runs once over each function, walking eight phases in fixed order.

The eight phases are:

1. **KernelSpec gate.** Read the function-scoped `KernelSpecAttr` (the same attribute that anchors kernel identity through the rest of TileAS). Without it the function cannot host TMA descriptors at all; the pass exits with `"LowerTMALoadStoreToAsync: missing or invalid KernelSpecAttr on function"`.
2. **TMA-eligibility scan.** Iterate every `nv_tileas.tiled_load`, `nv_tileas.tiled_store`, and `nv_tileas.tiled_atomic_rmw` op in the function. Each must carry either `allow_tma = true` (the per-op hint inherited from the public dialect's `cuda_tile.allow_tma`) or the environment switch `TILEIR_PREFER_TMA_FOR_LOAD_STORE` must be set. The atom referenced by the op must implement `TmaAtomTypeInterface` — atoms that don't (plain `ldg`, `stg`, `ldgsts`) are skipped without rewrite.
3. **tmaIdx assignment.** Assign a monotonically-increasing `tmaIdx` IntegerAttr to each surviving op. The counter is per-function, starting at zero, and the assignment walk is a single pre-order recursion so descriptor uses receive indices in the order the function would emit them.
4. **Descriptor bind.** For each op, emit (or look up) an `nv_tileas.make_tiled_tma_desc` whose result feeds the async op. The bind captures tensor shape, stride, padding mode, descriptor mode (tiled / im2col / im2col_at / tiled_at / gather4 for loads, store / reduce / scatter4 for stores), the element type, and the `tma_internal_type` attribute when the descriptor's internal element type differs from the tensor's element type.
5. **Async op materialization.** Replace the synchronous tiled op with its `nv_tileas.async.tiled_tma_load` / `tiled_tma_store` / `tiled_atomic_rmw` (TMAREDG atom) / `gather_tma_load` / `scatter_tma_store` counterpart. The new op carries the same coordinates and tile, plus the descriptor handle, plus a fresh `mbarrier` SSA value the load variants will wait on, plus a `tx_count` IntegerAttr giving the per-atom byte transfer count. Load variants additionally enforce zero padding — non-zero padding fires `"TmaLoad only support zero padding now"`, and the gather equivalent fires `"GatherTmaLoad only support zero padding now"`.
6. **mbarrier emission.** Each async load reserves an `mbarrier` in the function's SMEM arena and emits the arrive/wait skeleton. The arrival side is `cutlass.pipeline.get_producer_mask` plus the bind from phase 4; the wait side is `cutlass.pipeline.create` and an `async.wait` token. Store variants don't reserve their own mbarrier — the PipelineWaitGroupEmitter aggregates TMA stores with co-located GMMA stages, with the gate documented under [Async and Pipeline Family — Pipeline to NVVM](async-pipeline-family.md#pipeline-to-nvvm). The mbarrier state machine itself is documented in [mbarrier State Machine](../../topics/mbarrier-state-machine.md).
7. **Wait sinking.** When `TILEIR_DELAY_TMA_STORE_WAIT` is set, the matching `async.wait` for store variants may sink past the next barrier, letting the next stage's compute overlap the store's final commit. The pass records the option on the produced op so the wait-group emitter respects it.
8. **Diagnostic finalization.** Any op left unresolved by phases 4-6 — typically because its atom couldn't be located in the function's atom table — fires `"failed to find smem buffer address for mbarrier"`, `"failed to get expected tx-count"`, `"failed to init mbarrier"`, or `"failed to get MBarrier object"` depending on which sub-step lost track of its operand.

```c
LogicalResult lower_tma_load_store(FuncOp func, TmaOptions options) {
    KernelSpec spec = read_kernel_spec(func);
    if (!spec.valid()) {
        return func.emitOpError() << "LowerTMALoadStoreToAsync: missing or invalid KernelSpecAttr on function";
    }
    uint32_t next_tma_index = 0;
    for (MemoryOp op : func.tiled_memops()) {
        if (!op.allow_tma() && !options.prefer_tma) continue;
        if (!op.copy_atom().implements<TmaAtomTypeInterface>()) continue;

        TmaDescriptor desc = bind_descriptor(op, next_tma_index++);
        MbarrierOp mb = (op.kind() == LOAD) ? reserve_mbarrier(func, op) : nullptr;
        AsyncTmaOp async = rewrite_to_async_tma(op, desc, mb);
        emit_wait_skeleton(async, options.delay_store_wait);
        replace_op(op, async);
    }
    return success();
}
```

The input IR shape is a `nv_tileas.tiled_load` / `tiled_store` carrying a TMA copy-atom; the output is the four-op sequence — `make_tiled_tma_desc` (or a reuse of an existing one), the async `tiled_tma_*`, an mbarrier wait for load variants, and the matching fence inserted by the wait-group emitter. The `tmaIdx` attribute stamped on each async op is read later by `SeparateHostTMA` and `AttachTMADescriptorArgs` to wire host descriptor preparation back to device descriptor consumption.

## Token-Ordered Memops

`tiled_load`, `tiled_store`, and `tiled_atomic_rmw` are the three token-ordered memory ops the TileAS layer exposes. They consume and produce `nv_tileaa.mem_token` SSA values so that ordering between overlapping asynchronous transfers is expressed at the IR level rather than through fences or barriers, and they carry a memory-consistency enum (`weak`, `relaxed`, `acquire`, `release`, `acq_rel`) plus an optional `mem_scope` (`cta`, `cluster`, `gpu`, `sys`) for the full ordering contract. The three op verifiers share an almost line-for-line skeleton with three small specialisations: load produces a tile plus an out-token, store produces an out-token only, and atomic_rmw produces both a pre-image tile and an out-token. The diagnostics each emits are grouped below by op family; every string is part of the verifier's user-visible contract and reproduced verbatim.

### Structural diagnostics — all three ops

These fire before any semantic check. They guard the op's structural shape — regions, successors, the `operandSegmentSizes` attribute that partitions the variadic operand list, and the per-segment type constraints.

- `"requires zero regions"` — any token-ordered memop has zero regions; the dispatcher rejects regions before reading any operand.
- `"requires 0 successors but found "` — same shape, no successors permitted.
- `"operand group starting at #"` and `" requires 0 or 1 element, but found "` — paired diagnostic when the optional token operand segment carries more than one value.
- `"result group starting at #"` — counterpart on the result side when the optional token result holds more than one value.

The `operandSegmentSizes` attribute is parsed against the dialect-interned key string `"operandSegmentSizes"` (19 characters). The four operand segments are `view`, `coords`, `offsets`, and `token` in that order; the token segment accepts zero or one element, and segment shape mismatches fall back to the standard MLIR `operand group` diagnostic above.

### Coordinate and shape diagnostics — all three ops

Coordinate-count, coordinate-type, and tile-vs-tensor consistency are checked by an identical 1176-byte verifier instantiated once per op. The numeric segment index differs (load reaches into segment 2 for the memref operand, store into segment 1, atomic_rmw into segment 2) but the message set is the same.

- `"expects <N> coordinates, but got <M>"` — the literal partial string in the binary is `" coordinates, but got "`; the count before it is the expected coordinate count, derived from the view's rank plus an optional `+1` when the view carries a TMA descriptor that requires a leading offset.
- `"expects CoordType is same as memref index type, but got "` — every coordinate must match the memref's index type after masking off the low-3-bit type-uniquer tag.
- `"requires the same size for tileSize and tensor"` — emitted by the coord-type check when the product of the tile-size dims disagrees with the view's tensor shape.
- `"requires the same shape for tileSize and tensor value"` — the parallel diagnostic from the dedicated shape-equality helper.
- `"view elementType not equal with tensor element type: "` — the tile's element type must match the view's element type; the diagnostic is followed by two `printType` outputs separated by `" != "`.

### Tile-dimension invariants — all three ops

A dialect-shared helper enforces three invariants on every tile dim. These do not belong to the TMA family per se — they apply across the dialect — but they fire on this op family more than any other.

- `"all dimensions must be positive constants, got "` — every tile-size dim must be a positive integer constant.
- `"all dimensions must be powers of two, got "` — every dim must additionally be a power of two.
- `"tile would exceed the maximum of "` — the product of tile dims must stay below `0x1000000` (16 M elements), the dialect-wide cap.

### Memory semantics — all three ops, op-specific tables

Each op runs its own `mem_semantic` / `mem_scope` / `in_bounds` validator. The shared rules are the scope-vs-semantic compatibility:

- `"mem_scope not supported when mem_semantic is weak"` (snake_case, emitted by load and store)
- `"mem_scope required when mem_semantic is not weak"` (load and store)
- `"memScope not supported when memSemantic is weak"` (camelCase, emitted by atomic_rmw)
- `"memScope required when memSemantic is not weak"` (atomic_rmw)

Each op rejects the consistency modes that don't make sense for its access pattern. Load forbids `acquire` and `acq_rel`:

- `"unsupported mem_semantic: acquire"` (load)
- `"unsupported mem_semantic: acq_rel"` (load)

Store forbids `release` and `acq_rel`:

- `"unsupported mem_semantic: release"` (store)
- `"unsupported mem_semantic: acq_rel"` (store)

Across all three ops, the `in_bounds` `DenseI1ArrayAttr` length must equal the tile rank:

- `"incorrect number of in_bounds elements: expected "` followed by `", but found "`.

### Store-only diagnostics

Store cross-validates the optional `padding_value` operand against `in_bounds`:

- `"inbounds must be true when paddingValue is not set"`
- `"inbounds must be false when paddingValue is set"`

A separate float-typing helper guards the special padding values:

- `"special padding values (nan, pos_inf, neg_inf, neg_zero) only for float-like element types"`

### atomic_rmw-only diagnostics

The atomic op is checked first for the presence of its `rmw_mode` attribute, then for its bit-width ban list:

- `"requires attribute 'rmw_mode'"` — fires before any other attribute check when the `rmw_mode` IntegerAttr is missing entirely.
- `"tiled_atomic_rmw not supported for 8-bit types"` — no SM target supports tile-granular 8-bit atomics.
- `"tiled_atomic_rmw not supported for 16-bit integer"` — same hardware reality for 16-bit integer atomics.
- `"tiled_atomic_rmw for 16-bit float only supports add, max, min operations"` — when the element is bf16 or f16 and the `rmw_mode` is outside the three-element set the hardware natively supports.
- `"tiled_atomic_rmw op cannot use fadd operation, please use add instead for both int and float types"` — the dialect normalises floating-point adds to the same `add` opcode the integer path uses; the dispatcher decides int-vs-fp at lowering time.
- `"tiled_atomic_rmw op cannot use xchg operation"` — `xchg` has no meaningful tile-granular implementation because the pre-image would only be valid for one lane.

### Skeleton

```c
LogicalResult verify_tiled_memop(TiledMemOp op) {
    verify_zero_regions(op);
    verify_zero_successors(op);
    verify_result_count(op);                  // 2 for load and atomic_rmw, 1 for store
    verify_operand_segments(op, "operandSegmentSizes", /*width=*/19);
    verify_segment_types_and_attributes(op);  // also enforces rmw_mode presence for atomic_rmw

    verify_tile_size_matches_tensor_shape(op);
    verify_coord_count_and_type(op);
    verify_tile_element_type_matches_view(op);
    verify_tile_dimensions_positive_pow2_bounded(op);
    verify_mem_semantics_in_bounds_and_extras(op);  // padding for store, bitwidth ban for atomic_rmw

    return success();
}
```

TMA-backed views add one extra expected coordinate to the count check above — typically the im2col leading offset on SM100 — by reading the descriptor's leading-dim count from the view type and adding it to the rank-derived baseline.

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

The host-code path uses a small callback ABI that lets the runtime prepare TMA descriptors before each launch without changing the device-facing kernel signature. The host module emitted by `SeparateHostTMA` registers two callbacks with the `__CUDA_TILEIR_CALLBACKS` instrumentation hook: a one-shot SM-count / scratch-size query and a per-descriptor 64-byte payload emitter. Both are `printf`-style emitters that the runtime parses; their format strings are part of the binary-compatible contract and reproduced verbatim below.

| Callback | Format string | Calls per launch | Purpose |
|---|---|---:|---|
| SM count / scratch size | `"[TileIR Callback] SmNum: %ld deviceTMAMemorySize: 0x%lx"` | 1 | tells the runtime how many SMs the kernel targets and how many bytes of per-SM descriptor scratch to allocate |
| Descriptor payload | `"[TileIR Callback] DESC_TMA512: 0x%016lx %016lx %016lx %016lx"` | N (= `num-device-tmas`) | dumps each descriptor's 64-byte payload as four i64 words, in the order matching the kernel's `tmaIdx` numbering |

The 64-byte payload (`DESC_TMA512` — 512 bits) matches NVIDIA's published `cp.async.bulk.tensor.Nd` descriptor layout: global address, dim sizes, dim strides, format, swizzle, fill mode, element type, and rank, packed into four i64 words. The descriptors are emitted in tmaIdx order so the runtime can index them directly when patching descriptor pointers into the launch frame.

The host module attaches three pieces of metadata to the parent `builtin.module`. The compiled host object is stored under the `nv_tileas.host-code` attribute (an `UnitAttr` on the function plus the raw object bytes on the module). The descriptor counts are stored under the inherent attributes `nv_tileas.num-device-tmas` and `nv_tileas.num-host-tmas` on the kernel function. Each descriptor pointer argument the kernel ABI grew through `AttachTMADescriptorArgs` carries a `cute_nvgpu.grid_constant` argument attribute that the later cute-to-llvm lowering lifts to `nvvm.grid_constant`, so `ptxas` places the descriptor in `.param` memory rather than `.global`. The combination keeps the device-facing kernel signature stable across host-code revisions: the host module's compiled object lives entirely in the `nv_tileas.host-code` blob, and any change to descriptor preparation logic is contained in that blob without disturbing the device side.

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

The pass gates on the Blackwell `tmem` subtarget feature — feature index 80 in the NVPTX subtarget table. On any target that doesn't advertise that bit, the walk still runs but the dispatch table finds no work, because no `nv_tileas.copy` op references a TMEM-tagged operand. See [NVPTX Subtarget and Feature Matrix — The 81 Feature Indices](../../codegen/nvptx-subtarget-and-feature-matrix.md#the-81-feature-indices) for the feature table layout. The split between layout inference and tile emission lines up with the rest of the Blackwell lowering path: [Pipe / Mutex Value Layout](../../scheduler/pipe-mutex-value-layout.md) describes the per-stage value layout the inferred register layout must match, [tcgen05, WGMMA, mbarrier, and Cluster Sync — tcgen05 Machine Validation](../../codegen/tcgen05-wgmma-mbarrier-cluster.md#tcgen05-machine-validation) covers the `tcgen05.ld` / `tcgen05.st` instruction family this pass emits, and [ldmatrix/stmatrix and Register-Class Vtables — Matrix-Copy Templates](../../codegen/ldmatrix-stmatrix-and-register-class-vtables.md#matrix-copy-templates) documents the `ldmatrix` / `stmatrix` companion path for the SMEM-paired cases.

## Descriptor Builders and Verifiers

`make_tiled_tma_desc` records element bit-width, tensor rank, shape, strides, padding, descriptor mode (tiled / im2col / im2col_at / tiled_at), and operand segments. Its pre-lowering verifier and the closely related `AttachTMADescriptorArgs` and `MakeTiledTMADescOpCaptureVerifier` diagnostics share a common error surface; the rules below are organised by which structural property they guard.

### tmaIdx and descriptor-count rules

`AttachTMADescriptorArgs` validates the descriptor-index attribute against the host and device descriptor counts it records on the function.

- `"tmaIdx exceed tmaHostNum."` — the op's `tmaIdx` is at or beyond the count recorded in `nv_tileas.num-host-tmas`.
- `"tmaIdx exceed tmaDeviceNum."` — same against `nv_tileas.num-device-tmas`.
- `"not find tmaIdx."` — the op has no `tmaIdx` attribute at all.
- `"funcOp lack tmaDeviceNum and tmaHostNum attr"` — the function is missing both descriptor-count attributes the pass needs to validate any `tmaIdx`.

### Pointer-alignment and structural rules

- `"expected tma descriptor pointer to have alignment at least "` — TMA descriptor pointers must be at least 128-byte aligned; the diagnostic ends with the alignment value expected.
- `"tma boxDims[0] * elemTypeBitWidth is not a multiple of 16 bytes"` — the leading box-dim's bit-width must be a 16-byte multiple.
- `"tma leading box-dim bit-width is not 16 bytes aligned"` — the equivalent invariant from the descriptor-pointer side.
- `"tmaBoxDim and atomBoxDim length mismatch"` — descriptor box-dim count must match atom box-dim count.
- `"tmaBoxDim and atomBoxDim mismatch"` — same but for any per-dim disagreement.
- `"tma box-dim and copy atom box-dim mismatch"` — equivalent diagnostic from the copy-atom-side check.
- `"smem layout is not TMA compatible"` — the shared-memory layout's swizzle and rank must fall in the TMA-accepted set documented under [Mode Pattern Verifiers — TMA Rank and Mode Gates](../../dialects/cute_nvgpu/mode-pattern-verifiers.md#tma-rank-and-mode-gates).
- `"only support element_stride = 1 tma desc"` — element stride above 1 is not implemented for any TMA mode.

### Mode and multicast rules

- `"unsupported tma load mode '"` — the descriptor's mode value, when serialised, falls outside the accepted enum range (`tiled`, `im2col`, `im2col_at`, `tiled_at`, `gather4`).
- `"mcast is not supported for TMA load with less than 128bytes per atom"` — multicast requires at least 128-byte atoms.
- `" but the return TMA load type does not support multicast"` — the atom's return type cannot carry multicast metadata.
- `"missing or invalid num_multicast for a multicast TMA load"` — the `num_multicast` attribute must be present and well-typed when multicast is requested.

### Padding rules

- `"TmaLoad only support zero padding now"` — non-zero padding is not implemented for any TMA load path.
- `"GatherTmaLoad only support zero padding now"` — same for gather variants.
- `"padding value is not supported for TMA load with non-zero padding value"` — the explicit-padding-value form is rejected end-to-end.

### Atom-type rules

The verifier checks that every TMA-bearing op's atom operand has the right family (load vs store vs reduce) and falls inside the per-mode allow-list.

- `"expect a tma_store atom type"`
- `"expect a tma_load atom type"`
- `"expect a tma_redg atom type"`
- `"expect a stg, tma_store or unknown_copy atom type"` — the broader allow-list for store-side ops that may also be plain `stg`.
- `"expect a ldgsts, tma_load, ldg or unknown_copy atom type"` — load-side equivalent.
- `"TmaReduceOp do not support SCATTER4 mode"` — the reduce path cannot run in scatter4 because the scatter mode has no reduction operator.
- `"invalid TMA atom type"` — fallthrough when none of the allow-lists matches.

### Capture-walker rules

`MakeTiledTMADescOpCaptureVerifier` walks back from each operand through `RegionBranchOpInterface` to check that the dependency closure only uses ops the host-side lowering can replay.

- `"values depended by MakeTiledTMADescOp are not supportedbecause "` followed by `" matches more than 1 captured values."` — the operand SSA graph reaches a value with multiple capture sources, which the host module cannot reproduce.
- `"expected MakeTiledTMADescOp not depends on scf"` — the descriptor builder depends on an `scf` op that the host pass cannot lower. `SeparateHostTMA` refuses to run when this dependency exists.
- `"expect lower MakeTiledTMADescOp"` — the residual device-side `make_tiled_tma_desc` op that the host-conversion pass expected to be gone is still present after host lowering.
- `"math dialect not suppourt in separateHostTMA pass in the moment."` (verbatim typo preserved) — the descriptor's capture closure reaches a `math` dialect op the host module cannot emit.

### Composed-layout, descriptor-construction, and partitioning rules

These diagnostics fire from the TMA descriptor's lowering patterns and the partition verifier.

- `"unable to partition input tensors for TMA"` — the TMA partition step couldn't find a partition that satisfies the atom's box-dim constraints.
- `"failed to compute the TMA G-basis, got "` — the descriptor's G-basis (the global-tensor stride pattern) could not be computed from the supplied shape/stride pair.
- `"Computed TMA layout is invalid, got "` — the synthesised layout failed the layout verifier downstream.
- `"Failed to construct the TMA tensor type"` — the descriptor's `!cute.tensor` result type couldn't be built from the supplied operand types.
- `"doesn't support composed layout for "` — the composed-layout path is restricted to the set of swizzle modes the descriptor packer can express.

### Skeleton

```c
LogicalResult verify_tma_descriptor(MakeTiledTmaDescOp op) {
    require_global_memref(op.tensor());
    reject_unsupported_composed_layout(op.layout());
    require_rank_at_most(op.tensor(), MAX_TMA_RANK);
    require_descriptor_alignment(op.descriptor_pointer(), /*bytes=*/128);
    verify_tma_stride_contract(op);
    verify_cache_mode(op);
    verify_atom_type_in_allow_list(op);
    return verify_descriptor_capture(op);
}
```

## Tensormap Mutators

The CUDA driver encodes host-born descriptors once. Device-born descriptors use a fixed three-mutator subset — `tensormap.replace.global_address`, `tensormap.replace.dim_size`, and `tensormap.replace.stride_size` — driven in the order `address → dim[0..rank-1] → stride[1..rank-1]`. The mutable fields are precisely the three the runtime needs to vary across launches without re-encoding a descriptor: the tensor's base pointer, its per-dim sizes, and its per-dim strides. Everything else is immutable construction state:

| Field | Mutable on device | Notes |
|---|---|---|
| global base address | yes | `tensormap.replace.global_address` |
| global dim sizes (per dim) | yes | `tensormap.replace.dim_size`, one per dim |
| global strides (per dim) | yes | `tensormap.replace.stride_size`, one per dim |
| element type | no | fixed at construction |
| rank | no | fixed at construction |
| format (tiled / im2col / im2col_at / tiled_at) | no | fixed at construction |
| box shape | no | fixed at construction |
| swizzle mode | no | fixed at construction; the descriptor packer chooses from a closed set |
| fill mode (zero / constant) | no | fixed at construction |
| oob fill value | no | fixed at construction |
| interleave layout | no | fixed at construction |
| im2col offsets | no | fixed at construction |

The proxy fence rule pairs every device rebind with its consumer's read. Mutators write through generic memory; `cp.async.bulk.tensor.*` reads through the TMA proxy. A device-side rebind sequence therefore terminates in `fence.proxy.tensormap::generic.release.{cta,gpu}` before any TMA op consumes the mutated descriptor; the consumer's side emits the paired `fence.proxy.tensormap::generic.acquire.*` before its first read. The 64-byte descriptor payload, the `.b1024` mutator write width that forces 128-byte allocation alignment, and the exact inline-asm templates the three mutators emit are documented end-to-end in [TMA, Tensormap, and cp.async.bulk Emission — TMA Descriptor Mutators](../../codegen/tma-tensormap-and-cp-async-bulk.md#tma-descriptor-mutators).

This pass's specific contract is narrower: it materialises a `make_tiled_tma_desc` op carrying the rank, box, stride, padding, and cache attributes the partition verifier later re-checks, and tags every TMA-descriptor-typed kernel argument with `cute_nvgpu.grid_constant` so the kernel ABI carries the descriptor as a `.param` constant. Downstream NVVM lowering reads those attributes and emits the rebind sequence the codegen page documents.

