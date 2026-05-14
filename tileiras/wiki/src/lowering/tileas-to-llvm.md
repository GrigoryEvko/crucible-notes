# Lowering: nv_tileas to LLVM

## Abstract

`ConvertTileASToLLVM` is the terminal Tileiras lowering stage. It consumes a module already scheduled, layout-assigned, pipelined, and materialized in TileAS, then rewrites the remaining TileAA, TileAS, CuTe, CUTLASS, arithmetic, math, vector, and utility operations into LLVM and NVVM dialect operations. The output is a GPU module ready to translate to LLVM IR and then to NVPTX.

Function-boundary conversion runs before body conversion. The order matters: kernel function signatures and attributes must be LLVM-compatible before body patterns lower pointers, descriptors, async tokens, barriers, and target-specific operations.

## Input and Output Dialects

| Direction | Surface |
|---|---|
| input ops | residual `nv_tileaa.*`, all `nv_tileas.*`, `cute.*`, `cute_nvgpu.*`, `cutlass.*`, plus remaining `arith.*` and `math.*` that did not lower in earlier stages |
| input types | TileAA / TileAS memref, view, token, layout, and atom types |
| output ops (legal) | `llvm.*`, `nvvm.*`, `gpu.module` (container only), surviving `cute.*` / `cute_nvgpu.*` / `cutlass.*` consumed by companion lowering |
| output types | LLVM descriptor structs, LLVM pointers (address-space-qualified), `i32` async tokens, `i1` mbarrier results |

The canonical rewrite shapes for the major TileAS families are:

```text
nv_tileas.alloc_tensor     -> llvm.mlir.addressof @global_smem + llvm.getelementptr
nv_tileas.convert_layout   -> sequence of llvm.extractvalue / llvm.insertvalue, possibly via stmatrix / ldmatrix
nv_tileas.tiled_load       -> nvvm.cp.async / cp.async.bulk / tma.tile (selected from CopyAtom witness)
nv_tileas.tiled_store      -> nvvm.cp.async.bulk.s2g / stmatrix (selected from CopyAtom witness)
nv_tileas.async.pipeline.* -> i32 token phase + nvvm.mbarrier.* arrive / wait
nv_tileas.dot              -> nvvm.wgmma.* (SM90) or nvvm.tcgen05.mma (SM100)
```

## Pass Ordering

The LLVM lowering stage is two passes that the driver runs in sequence:

1. `ConvertTileFuncToLLVM` rewrites `nv_tileaa.func` and `nv_tileaa.return` into `func.func` and `func.return`, applies the bare-pointer kernel ABI, and translates the kernel-spec attribute set into `nvvm.*` discardable attributes.
2. `ConvertTileASToLLVM` rewrites bodies in nine phases (described below), starting with shared-memory global synthesis and ending with cast reconciliation.

Function-boundary conversion runs first because body conversion needs LLVM-typed function arguments: every body pattern that reads or writes a kernel argument depends on the bare-pointer ABI having been applied. Reversing the order would produce body lowerings against argument types that are still `nv_tileaa`-typed, and the cast-reconciliation phase would have nothing to reconcile against.

## Function Boundary Conversion

`ConvertTileFuncToLLVM` (CLI: `convert-nv-tile-func-to-llvm`) is the function-boundary lifter that runs before `ConvertTileASToLLVM`. It rewrites `nv_tileaa.func` and `nv_tileaa.return` into `func.func` and `func.return`, performing full kernel-attribute translation against the active target. The body at `sub_1159990` is 6 172 B across 239 basic blocks and calls 51 distinct helpers. `sub_1156310` registers the dependent dialects `llvm`, `func`, and `cutlass` — `cutlass` is pulled in so residual `cutlass.*` markers carried alongside kernel metadata stay legal during the rewrite.

The pass runs as a four-phase state machine. Three module/function attributes gate the kernel-translation path:

| Attribute | Reader | Reader size |
|---|---|---|
| `nv_tileaa.kernel_spec` | `sub_13FE910` | 0x574 B |
| `nv_tileaa.compute_capability` | `sub_13FB490` | 0x179 B |
| `nv_tileaa.target_spec` | `sub_13FB490` | 0x179 B |

If any of the three is absent, the kernel path is skipped and only the plain signature rewrite runs; the function is a host-side helper as far as this pass is concerned.

### Phase 1 — TypeConverter build

`sub_15685F0` reads argument types, `sub_4419090` rewrites each one, and the rewritten types collect into a `SmallVector<Type, 6>` with inline header `0x600000000` (inline cap=6, size=0). `sub_43FE7A0` then pins the vector back into the new function-type slot. The inline-6 choice is sized for typical Tileiras kernels — argument-buffer pointers plus a handful of scalar launch parameters fit without spilling to the heap.

### Phase 2 — Three conversion patterns

Pattern construction goes through `sub_1158660` (4 052 B, 213 BB). It installs three `OpConversionPattern` subclasses, each a 0x68-B object — 8 B wider than upstream `OpConversionPattern` to accommodate an RTTI string parked at slots `+64`/`+72`. The trio:

| Pattern | Vtable | Role |
|---|---|---|
| `FuncOpConversion` | `off_59D57E8` | Rewrites `nv_tileaa.func` into `func.func` with the converted signature and transfers kernel attributes when the kernel-spec triple is present. |
| `ReturnOpConversion` | `off_59D5838` | Rewrites `nv_tileaa.return` into `func.return` and enforces the kernel-return policy described in Phase 4. |
| `CastOpElimination` | `off_59D5888` | Eliminates the unrealized casts that the signature rewrite introduces between the old and new argument SSA values. |

The PDL fallback `sub_36F9730` runs unconditionally so pattern authors can express auxiliary rewrites in PDL; `sub_36CB0C0` then drives `applyPartialConversion`. A failure raises `"region types conversion failed"` and sets bit 2 of `pass+40`, matching the sticky failure-reported scheme the sister pass uses.

### Phase 3 — Kernel-attribute transfer

This phase runs only when all three attribute reads return non-empty. It performs BarePtr-style ABI translation: argument-buffer slots become pointer-sized LLVM args, and every kernel-spec field becomes an `nvvm.*` discardable attribute. The full eight-row translation table:

| Source (`nv_tileaa.*`) | Destination | Type | Emission predicate |
|---|---|---|---|
| `kernel_spec` (presence) | `cute.kernel` | `UnitAttr` | always when kernel_spec valid |
| `kernel_spec.numWarps` | `nvvm.reqntid` | `IntegerAttr<i32>` | always; value = `32 * numWarps` |
| `kernel_spec.clusterDim{X,Y,Z}` | `nvvm.cluster_dim` | `IntegerAttr<i32>` | `targetSM > 89 && clusterProduct > 1` |
| (cluster gating) | `nvvm.blocksareclusters` | `UnitAttr` | same predicate as `nvvm.cluster_dim` |
| (constant 1) | `nvvm.minctasm` | `IntegerAttr<i32>` | always |
| `nv_tileaa.occupancy` | `nvvm.maxnreg` | `IntegerAttr<i32>` | iff occupancy set; value from `sub_13FDB70` (per-SM occupancy → maxnreg table) |
| `nv_tileaa.compute_capability` | (consumed) | `IntegerAttr` | gates SM-dependent emission only |
| `nv_tileaa.target_spec` | (consumed) | `StringAttr` | gates SM-dependent emission only |

The translation uses dual-path DictionaryAttr lookup. The interned StringAttr `"mlir::FunctionOpInterface]"` — cached at `qword_5B37670` behind the double-checked lock `byte_5B37668` — drives the fast-path pointer comparison; the slow path goes through `sub_43F70F0` to rebuild the StringAttr key and `sub_446DC50` / `sub_446DC70` to search the dictionary and install the new entry. The split exists because the interned key is cheaper than a string compare, but the cache may be cold on the first kernel in the module.

The `cute.kernel` placeholder is deliberately **not** renamed to `nvvm.kernel` here. That rewrite lives in the downstream `CuteKernelToNvvmRewrite` pass at `sub_1698C20`. The split exists because the downstream pass also lifts `cute_nvgpu.grid_constant` argument attributes to `nvvm.grid_constant`, and that lift needs the LLVM-legal function arguments this pass has just produced. Doing both rewrites in one pass would force grid-constant migration against not-yet-lowered argument types.

### Phase 4 — Kernel-return policy

`ReturnOpConversion::matchAndRewrite` at `sub_11565D0` enforces the kernel-return policy. If the parent op is a kernel (`*(parent+46) < 0`) and the return carries any operands, the pattern emits `"Kernel functions do not support return with operands"` at severity 259 and fails. Empty returns become `func.return` unconditionally. Non-kernel `nv_tileaa.return` is rewritten with whatever operands it carried, since regular `func.return` accepts arbitrary value lists.

### Dynamic legality

`nv_tileaa.func` dynamic legality at `sub_1156400` (0x1E4 B) is a four-way unrolled operand walk that returns "illegal — must rewrite" whenever any argument or result type carries a non-null operand-value pointer — i.e. is still `nv_tileaa`-typed. Purely LLVM signatures are already legal and skip the pattern entirely, so a function that has already been lifted (for instance because the producer emitted LLVM types directly) doesn't pay for a redundant rewrite.

```c
LogicalResult lower_kernel_return(ReturnOp op, Rewriter *rw) {
    if (is_kernel(op.parent_function()) && !op.operands().empty()) {
        return op.emit_error("Kernel functions do not support return with operands");
    }

    rw->replace_op_with_new_op(op, "func.return", {});
    return success();
}
```

## Body Conversion Phases

`ConvertTileASToLLVM::runOnOperation` lives at `sub_11547D0`, a 20 KB, 180-basic-block body whose only state argument is the pass instance itself. The MLIRContext is recovered from `pass[5] & ~7`; the three low bits of that word encode `skip-pipeline`, an `index-bitwidth != 0` marker, and a sticky failure-reported flag the pass body sets in place of calling `signalPassFailure()` directly. The pass-manager wrapper inspects the bit after return. Compute capability and target spec are fetched via `sub_13FB490`, which keys off the `nv_tileaa.compute_capability` and `nv_tileaa.target_spec` module attributes; absence produces `"Failed to get ComputeCapability"` (severity 259/0x103) routed through `sub_446CE00` before the failure bit is set and the pass returns.

Before any rewrite pattern installs, the pass body emits the shared-memory scratch global via `sub_1144DA0` (749 bytes, 14 basic blocks). The helper looks up an existing `global_smem` symbol with `sub_1144CC0`; if none exists, it synthesises `llvm.mlir.global @global_smem ... addr_space(3) align 16 : !llvm.array<N x i8>` with `N = pass[6] >> 2`. Kernels with no extended shared memory request (`pass[6] <= 0`) short-circuit the helper and emit no global. The body also exercises the standard `"Building op \`…\` but it isn't known in this MLIRContext"` probe to distinguish "op registered" from "op missing" without aborting — a deliberate use of MLIR's diagnostic infrastructure as a registration test, not an error path.

```c
LogicalResult run_convert_tileas_to_llvm(Pass *pass) {
    MLIRContext *ctx = (MLIRContext *)(pass[5] & ~7uLL);
    TargetSpec *spec = sub_13FB490(pass);                          // compute_capability / target_spec
    if (!spec) {
        emit_error(ctx, "Failed to get ComputeCapability");        // diag 0x103
        pass[5] |= 4uLL;                                            // failure bit, no signalPassFailure()
        return failure();
    }

    if (failed(sub_1151450(ctx, sub_1151520, &patterns))) {        // (1) decompose-print
        emit_error(ctx, "fails to decompose print ops");
        pass[5] |= 4uLL; return failure();
    }
    if (failed(sub_11523A0(ctx, sub_1152460, &patterns))) {        // (2) bufferization analysis
        emit_error(ctx, "fails to do bufferization analysis");
        pass[5] |= 4uLL; return failure();
    }

    sub_1144DA0(pass);                                              // global_smem emission (if pass[6] > 0)

    sub_114F970(ctx, sub_114D1B0, &patterns);                       // (3) main nv_tileaa/nv_tileas
    sub_114F880(ctx, sub_1150300, &patterns);                       // (4a) bulk supplementary patterns

    for (Slot *slot = barrier_map.slots; slot < barrier_map.end; ++slot) {
        if (slot->key == -4096 || slot->key == -8192) continue;     // empty / tombstone sentinels
        sub_114BB00(slot, ctx, &patterns);                          // cluster/barrier replay
    }

    sub_114FA40(ctx, sub_114DC50, &patterns);                       // (5) cute / cute_nvgpu
    if (!(pass[5] & 1uLL)) {                                        // skip-pipeline gate
        sub_114FB10(ctx, sub_114EA50, &patterns);                   // (6) async.pipeline
    }
    sub_1153E30(ctx, sub_11540E0, &patterns);                       // (7) arith / llvm / math cleanup
    sub_1154530(ctx, sub_1155A80, &patterns);                       // (8) reconcileUnrealizedCasts
    sub_11508F0(ctx, sub_1150580, &patterns);                       // (9) late materializer

    sub_115E240(&target, &type_converter);                          // configureConversionTarget
    if (failed(sub_36F9730(&patterns))) {                           // PDL → PDLInterp fallback
        emit_error(ctx, "failed to lower PDL pattern module to the PDL Interpreter");
        pass[5] |= 4uLL; return failure();
    }
    if (failed(sub_36CB0C0(module, &target, &patterns))) {          // applyPartialConversion engine
        pass[5] |= 4uLL; return failure();
    }

    teardown_dense_maps(pass);                                      // 8 slot-array free loops + ~Op() vtable+8
    return success();
}
```

Pattern installation is six identical trampoline/body pairs plus three additional driver/populator pairs in the same shape. Every trampoline is a 199-230 byte, 14-basic-block skeleton that captures the conversion target and the shared `TypeConverter`, tail-calls its inner populator, and bubbles the resulting `bool`. Every populator body is a 54-basic-block `emplace_back` chain over `std::vector<std::unique_ptr<RewritePattern>>`; each `emplace_back` resolves to one `sub_44A8C20(0x68)` arena allocation paired with one indirect pattern-vtable construction call, so the 54 blocks correspond to 54 distinct pattern classes per phase.

| # | Phase | Trampoline | Body | Role | Diagnostic | Driver |
|---|-------|-----------|------|------|------------|--------|
| 1 | decompose-print | `sub_1151450` | `sub_1151520` | Decomposes nv_tileaa print operations under a `FunctionOpInterface` classof guard | `"fails to decompose print ops"` | `applyPartialConversion` |
| 2 | bufferize | `sub_11523A0` | `sub_1152460` | Bufferization-analysis driver; assigns buffer forms required by later memory rewrites | `"fails to do bufferization analysis"` | `applyPartialConversion` |
| 3 | main TileAA/TileAS | `sub_114F970` | `sub_114D1B0` | Main nv_tileaa/nv_tileas → llvm/nvvm rewrites: tid-arith, ctaid/gridDim, warp shuffle, mbarrier init, TMA load/store, atomic RMW | — | `applyPartialConversion` |
| 4 | bulk supplementary | `sub_114F880` | `sub_1150300` | Additional lowerings populated in a nested scope between the main roster and the cluster/barrier replay | — | `applyPartialConversion` |
| 5 | cute / cute_nvgpu | `sub_114FA40` | `sub_114DC50` | `cute.*` and `cute_nvgpu.*` layout, copy, and SM100 arch helpers including `cute_nvgpu.arch.sm100.retrieve_tmem_ptr` | — | `applyPartialConversion` |
| 6 | async.pipeline | `sub_114FB10` | `sub_114EA50` | `nv_tileas.async.pipeline.{create_pipeline, produce_one, consume_one, yield}`; skipped when `skip-pipeline` is set | — | `applyPartialOneToNConversion` |
| 7 | arith / llvm cleanup | `sub_1153E30` | `sub_11540E0` | Final upstream `arith` / `math` → `llvm` cleanup | — | `applyPartialConversion` |
| 8 | reconcile-unrealized-casts | `sub_1154530` | `sub_1155A80` | `reconcileUnrealizedCasts`: strips leftover `builtin.unrealized_conversion_cast` ops | — | `applyFullConversion` |
| 9 | late materializer | `sub_11508F0` | `sub_1150580` | Fill-remainder helpers; emits PDL-fallback-friendly type materialisers | — | `applyPartialConversion` |

Between phases 4 and 5 the pass body walks an 80-byte DenseMap slot array using the standard LLVM ADT sentinels (`-4096` empty, `-8192` tombstone) and invokes `sub_114BB00` (5.3 KB) on every non-sentinel slot. This is the cluster/barrier pattern replay that carries the multi-variant barrier lowerings — `nvvm.barrier`, `nvvm.cluster.arrive.relaxed`, `nvvm.cluster.wait` — each registered as a distinct pattern class so the slot-array walk can install them all without reflowing the main populator.

After every populator phase is installed, `sub_115E240` (2.2 KB, 13 basic blocks, 34 string literals) builds the conversion target. Three sub-helpers split the op-by-op work. `sub_115CDA0` adds `nv_tileas.{alloc_tensor, convert_layout, load, store}` and `nv_tileaa.plugin` as dynamic-legal "holes punched in the illegal dialect" — accepted only once their operands have already been converted to LLVM types. `sub_115DDB0` adds `llvm.{getelementptr, load, inline_asm, mlir.global, extractelement}` as statically legal. `sub_115D280` marks every `nv_tileas.async.pipeline.*` op together with `nv_tileaa.mark_for_reuse` and the sister-pass-owned `nv_tileaa.func` / `nv_tileaa.return` as legal, so the surface `ConvertTileFuncToLLVM` owns survives this pass untouched. `arith` adds with dynamic legality through `sub_36B52E0(target, "arith", 5, {sub_115B300, sub_115D940})`; the predicate pair asks the `TypeConverter` whether the operation's result and operand types are already LLVM-typed.

Once populators are installed and the target is configured, the PDL-to-PDLInterp fallback runs through `sub_36F9730` (carrying the diagnostic `"failed to lower PDL pattern module to the PDL Interpreter"`), letting pattern authors express catch-all rewrites declaratively in PDL rather than C++. The conversion engine `sub_36CB0C0` then drives `applyPartialConversion`. Teardown is eight DenseMap slot-array free loops that hand each storage buffer to `sub_4560420` with the right stride, followed by per-pattern `~Op()` destructor calls dispatched through the vtable slot at offset `+8`. The pipeline phase is skipped only when the caller has deliberately picked a path that does not require TileAS async pipeline lowering — typically for IR introspection, since the upstream `OneToNTypeConverter` folds intermediate casts during type-split and that elision obscures pipeline structure in debug dumps. In normal compilation, every one of the nine phases runs.

## Dynamic Shared Memory

Before body patterns run, the pass may create a shared-memory byte array symbol. The symbol is emitted only when the kernel requested extended shared memory, and uses the shared address space with conservative alignment so later GEPs can carve typed views out of it.

```c
void ensure_global_smem(ModuleOp module, uint32_t bytes, Rewriter *rw) {
    if (bytes == 0 || module.lookup_symbol("global_smem")) {
        return;
    }

    Type element = rw->i8_type();
    Type array = rw->llvm_array_type(element, bytes);
    rw->create("llvm.mlir.global", {
        .name = "global_smem",
        .type = array,
        .address_space = AddressSpace::Shared,
        .alignment = 16,
    });
}
```

### `global_smem` Synthesis

Before any conversion pattern fires, `ConvertTileASToLLVM` emits a single shared-memory scratch global named `global_smem`. Emission happens in `sub_1144DA0` (749 bytes, 14 basic blocks), once per pass invocation. The array length is `N = pass[6] >> 2`, where `pass[6]` is the upstream-computed extended-SMEM byte budget produced by the TileAS scheduler; the right shift by two converts that budget into the i8-array length the LLVM dialect expects on the synthesised global. Kernels that did not request extended shared memory (`pass[6] <= 0`) short-circuit at the entry test and emit nothing.

`sub_1144CC0` probes for existing symbols. If a `global_smem` already exists on the module — for instance, emitted by an earlier pass running on the same module — synthesis is skipped and the existing symbol is reused. The synthesised IR has the canonical shape:

```mlir
llvm.mlir.global @global_smem () { addr_space = 3 : i32, alignment = 16 : i64,
                                   linkage = #llvm.linkage<internal> } : !llvm.array<N x i8>
```

Address space `3` is the CUDA shared address space (`__shared__`). Alignment `16` matches the maximum natural alignment for any vectorisable load or store hitting the global, so later GEPs carving typed views out of the i8 backing array need not widen the alignment in place. Internal linkage keeps the symbol private to the module. The classic `"Building op `…` but it isn't known in this MLIRContext"` diagnostic is wired in as a sanity probe to distinguish "op registered" from "op missing" without aborting — if the `llvm.mlir.global` op is not registered in the MLIRContext, the helper emits the diagnostic and returns failure rather than crashing.

```c
LogicalResult emitGlobalSmem(PassContext *ctx, ModuleOp module) {
    if (ctx->pass[6] <= 0)                                                           return success(); // skip
    if (Operation *existing = sub_1144CC0(module, "global_smem"))                    return success(); // reuse
    uint64_t n_bytes = (uint64_t)ctx->pass[6] >> 2;
    OperationName globalName("llvm.mlir.global", ctx->mlirCtx);
    if (!globalName.isRegistered()) {
        emit("Building op `llvm.mlir.global` but it isn't known in this MLIRContext");
        return failure();
    }
    OpBuilder b(module.getBodyRegion());
    b.create<llvm::GlobalOp>(/*loc=*/loc, /*type=*/llvmArrayI8(n_bytes), /*sym_name=*/"global_smem",
                             /*linkage=*/Internal, /*addr_space=*/3, /*alignment=*/16);
    return success();
}
```

`ConvertTileASToLLVM` runs once per kernel, and each invocation gets its own `pass[6]`, so the per-pass emission shape is natural — the same module may host multiple kernels, each with its own extended-SMEM budget, and each kernel's pass instance carries its own byte count. The reuse path through `sub_1144CC0` fires only when two kernels happen to share the same scratch, which is rare in practice.

## Conversion Target

The terminal conversion target legalizes LLVM and NVVM while treating TileAA and TileAS as illegal except for explicitly dynamic bridge operations. Some `arith` and `math` operations are dynamically legal once their operands and results are already LLVM-compatible; otherwise cleanup patterns lower them.

| Legal or dynamic surface | Reason |
|---|---|
| `llvm` and `nvvm` | terminal executable dialects |
| `gpu.module` containers | consumed by GPU-to-binary serialization |
| selected `arith` operations | legal only after type conversion |
| selected `math` operations | legal only after cleanup-compatible type conversion |
| selected TileAS bridge ops | legal only when operands are already LLVM-typed |
| async pipeline bridge ops | legal only for the pipeline lowering phase |
| unrealized casts | temporary, removed by reconciliation |

The target must reject every executable TileAS operation after the body phase. Accepting one would shift a compiler bug into backend translation.

## Conversion-Target Legality

`sub_115E240` (2.2 KB, 13 basic blocks, 34 string literals) assembles the `ConversionTarget` `applyPartialConversion` consults during the main `ConvertTileASToLLVM` pass. It runs once, after every populator phase is installed and before the PDL fallback. The job is purely declarative: tell the partial-conversion driver which dialects are fully legal, which dialect is uniformly illegal, and which individual operations are legal only when a type-converter predicate accepts them. No rewrite work happens here.

Two static-legality vectors carry the bulk of the configuration, passed through `sub_36B4F90(target, vec, count, kind)`. A seven-entry vector with `kind = 0` ("fully legal — accept any op of these dialects without further checks") carries the terminal executable dialects together with the CuTe and CUTLASS surface that survives lowering verbatim. A single-entry vector with `kind = 2` ("illegal") names `nv_tileaa`, so every `nv_tileaa` op is a rewrite target by default; the dynamic predicates that follow are the "holes punched in the illegal dialect" that let specific bridge ops slip through when their operands have already been converted.

| Vector | `kind` | Entries |
|---|---|---|
| static legal dialects | 0 | `arith`, `gpu`, `nvvm`, `scf`, `cute`, `cute_nvgpu`, `cutlass` |
| static illegal dialects | 2 | `nv_tileaa` |

Three sub-helpers refine the per-op surface. They are independently callable but share a fixed call order inside `sub_115E240`: the static "fully legal" bucket fills first, the illegal dialect is declared next, and only then are the dynamic and per-op exceptions layered on top.

| Sub-helper | Size | Operations | Legality |
|---|---|---|---|
| `sub_115CDA0` | 1.2 KB | `nv_tileas.{alloc_tensor, convert_layout, load, store}`, `nv_tileaa.plugin` | dynamic — accepted only when operands already carry LLVM-legal types |
| `sub_115DDB0` | 1.1 KB | `llvm.{getelementptr, load, inline_asm, mlir.global, extractelement}` | static |
| `sub_115D280` | 737 B | `nv_tileas.async.pipeline.*`, `nv_tileaa.{mark_for_reuse, func, return}` | static |

`sub_115D280` carries the sister-pass contract. `nv_tileaa.func` and `nv_tileaa.return` are owned by `ConvertTileFuncToLLVM`, which has already run; marking them legal here keeps the surface the function-boundary pass produced untouched. `nv_tileaa.mark_for_reuse` is a scheduling annotation that must survive into NVVM translation. The `nv_tileas.async.pipeline.*` family is legal because the pipeline phase (phase 6, `sub_114FB10` → `sub_114EA50`) uses `applyPartialOneToNConversion` rather than `applyPartialConversion`, and the bookkeeping ops it leaves behind must not be re-attacked by the main driver.

Dynamic legality for the remaining `arith`, `math`, and `ub.poison` operations installs through `sub_36B52E0(target, name, count, predicates)` and `sub_36B50E0(target, op)`. Each predicate pair is a `(legality, materialization)` callback: the first asks the `TypeConverter` whether the operation's result and operand types are already LLVM-typed, the second is the cast-materialization hook invoked when the answer is "almost — convert these operands first".

| Op or family | Registration | Predicate pair |
|---|---|---|
| `arith` (5 ops) | `sub_36B52E0(target, "arith", 5, …)` | `{sub_115B300, sub_115D940}` |
| `math.absi`, `math.ctlz`, `math.ctpop`, `math.cttz`, `math.trunc` | `sub_36B50E0(target, op)` per op | inherited from the dialect's default dynamic predicate |
| `ub.poison` | `sub_36B52E0(target, "ub.poison", 1, …)` | `{sub_115B360, sub_115B250}` |

Those five `math.*` operations are exactly the ones with direct NVVM equivalents (`absi` → `nvvm.abs`, `ctlz`/`cttz` → `nvvm.bfind`, `ctpop` → `nvvm.popc`, `trunc` → integer-truncating cast); the upstream `math` patterns lower them in the cleanup phase only when their operands are already LLVM-typed.

```c
void buildConversionTarget(ConversionTarget *target) {
    sub_36B4F90(target,
                /*legalDialects=*/{arith, gpu, nvvm, scf, cute, cute_nvgpu, cutlass},
                /*count=*/7,
                /*kind=*/0);
    sub_36B4F90(target,
                /*illegalDialects=*/{nv_tileaa},
                /*count=*/1,
                /*kind=*/2);

    sub_115CDA0(target);     // dynamic-legal nv_tileas.{alloc_tensor, convert_layout, load, store} + nv_tileaa.plugin
    sub_115DDB0(target);     // statically-legal llvm.{getelementptr, load, inline_asm, mlir.global, extractelement}
    sub_115D280(target);     // legal nv_tileas.async.pipeline.* + nv_tileaa.{mark_for_reuse, func, return}

    sub_36B52E0(target, "arith", 5, /*predicates=*/{sub_115B300, sub_115D940});
    sub_36B50E0(target, "math.absi");
    sub_36B50E0(target, "math.ctlz");
    sub_36B50E0(target, "math.ctpop");
    sub_36B50E0(target, "math.cttz");
    sub_36B50E0(target, "math.trunc");
    sub_36B52E0(target, "ub.poison", 1, /*predicates=*/{sub_115B360, sub_115B250});
}
```

The fixed declaration order matters. Dynamic predicates registered later take precedence over the dialect-wide `kind = 0` decision — a reimplementation that swaps `sub_115CDA0` with the static-legal-dialect call would accidentally make every `arith` op statically legal and skip the cleanup phase's type-conversion gate. The driver also relies on `nv_tileaa` being declared illegal before the per-op holes are punched: punching holes first and declaring dialect-wide illegality second would override the dynamic predicates and force every `nv_tileaa.plugin` invocation to rewrite unconditionally, which the plugin contract does not support.

## Async Pipeline Lowering

TileAS async pipeline operations carry producer/consumer phase, stage index, and queue structure. Terminal lowering distills that structure into integer tokens, barriers, waits, and memory operations.

```c
LogicalResult lower_pipeline_consume(ConsumeOp op, Rewriter *rw, PipelineState *state) {
    Value token = state->token_for(op.pipeline(), op.stage());
    Value phase = rw->and_i(token, rw->constant_i32(1));

    Value ready = rw->create("nvvm.mbarrier.try_wait.parity.shared", {
        op.barrier(),
        phase
    }).result(0);

    rw->replace_op(op, ready);
    return success();
}
```

The exact intrinsic varies by operation; the invariant is stable: pipeline tokens are integer phase carriers, not heap objects.

## Tile Memory and Descriptor Lowering

The main TileAA/TileAS body patterns lower:

- thread, warp, CTA, grid, and cluster index arithmetic;
- tile loads, stores, gathers, scatters, and atomics;
- TMA descriptor creation and tiled TMA load/store operations;
- mbarrier initialization, arrive, wait, and transaction-count operations;
- layout conversion and view operations;
- tensor-memory helper operations for Blackwell paths;
- inline assembly only where no first-class NVVM operation exists.

Prefer first-class NVVM operations over inline assembly. Inline assembly is appropriate only for target instructions absent from the NVVM dialect snapshot.

## Arith Template Cleanup

The cleanup path includes generic arithmetic patterns plus a higher-priority constant conversion. Generic arithmetic conversion maps compare, add, multiply, division, shifts, select, casts, and min/max into the target dialect under the shared type converter. Constants get special handling so tensor constants become TileAA or LLVM aggregate materializations rather than scalar-only constants.

```c
LogicalResult lower_arith_constant(ConstantOp op, Rewriter *rw, TypeConverter *types) {
    Attribute value = op.value();

    if (isa<DenseElementsAttr>(value) || isa<SplatElementsAttr>(value)) {
        return lower_tensor_constant(op, rw, types);
    }

    return lower_scalar_constant(op, rw, types);
}
```

## Conversion Invariants

- Function signatures must be LLVM-compatible before body lowering starts.
- Kernel functions may not return operands.
- Kernel metadata must be transferred to NVVM-compatible attributes without losing launch semantics.
- Shared-memory globals are emitted only when requested and must use shared address space.
- Async pipeline tokens lower to integer phase carriers.
- TileAA and TileAS executable operations must not survive terminal conversion.
- Temporary unrealized casts must be reconciled before serialization.
- Inline assembly must remain narrowly scoped to missing NVVM dialect coverage.

## Cross-References

[Overview](overview.md) places this pass at the LLVM-lowering stage between TileAS scheduling and companion-dialect lowering. [TileAA to TileAS](tileaa-to-tileas.md) is the upstream producer whose CopyAtom and ReduceAtom witnesses this pass resolves into concrete hardware primitives. [CuTe and CuTe-NVGPU to LLVM](cute-and-cute_nvgpu-to-llvm.md) and [nvgpu / gpu to NVVM](nvgpu-and-gpu-to-nvvm.md) are the companion passes that lower the surviving `cute.*`, `cute_nvgpu.*`, `gpu.*`, and `nvgpu.*` operations after this pass. [Pattern Set and Type Converter](pattern-set-and-typeconverter.md) describes the shared LLVM type converter every pattern in this pass threads through.

