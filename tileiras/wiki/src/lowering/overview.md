# Conversion / Lowering Overview

A verified TileIR module reaches NVVM-ready MLIR through a staged dialect-conversion pipeline:

```
cuda_tile -> nv_tileaa -> nv_tileas -> llvm/nvvm -> targeted gpu.module
```

Every stage shares the same shape: declare which dialects and operations are legal, populate a rewrite pattern set, convert types through the stage's type converter, run conversion, and verify that the previous abstraction level has not leaked through. The public contract is the sequence of legality boundaries — not the identity of any recovered helper in the binary.

`cute`, `cute_nvgpu`, and `cutlass` are companion dialects rather than a single linear rung. They may survive one lowering stage when a later sister pass owns their conversion. The arrangement is intentional: TileAS handles scheduling and layout, while the CuTe/CUTLASS families carry atom, descriptor, and pipeline structure until NVVM conversion can emit the right target intrinsics.

## Cascade

```
   TileIR bytecode
                       |
                       v
                    cuda_tile
                       |
                       v   public tile IR -> alias-aware tile IR
                    nv_tileaa
                       |
                       v   scheduling/layout/materialization
                    nv_tileas + cute/cute_nvgpu/cutlass
                       |
                       v   ABI and intrinsic lowering
                    llvm + nvvm
                       |
                       v   kernel and target finalization
                    gpu.module with #nvvm.target
                       |
                       v
                    LLVM/NVPTX serialization
```

## Dialect Roles

| Dialect | Role in lowering | Exit condition |
|---|---|---|
| `cuda_tile` | Public input dialect: tile math, views, tokens, entry ops, and structured control flow. | No `cuda_tile` operations remain after the first conversion. |
| `nv_tileaa` | Alias-aware tile algebra that keeps tile semantics explicit while introducing internal memory and token forms. | TileAA compute and memory ops are either lowered to TileAS or explicitly kept as legal bridge ops. |
| `nv_tileas` | Scheduling-aware tile IR: async pipelines, TMA descriptors, CTA/cluster behavior, layouts, buffers, and staged execution. | Hardware-facing TileAS ops become LLVM/NVVM, inline asm, or companion-dialect constructs. |
| `cute`, `cute_nvgpu`, `cutlass` | Companion dialects for layout algebra, MMA/copy atoms, and pipeline abstractions. | Lowered by their dedicated passes when enough target information and LLVM-compatible types exist. |
| `gpu` | Standard MLIR GPU container and builtin GPU queries. | Thread/block/cluster queries, barriers, launches, and GPU functions become NVVM/LLVM operations. |
| `llvm`, `nvvm` | Terminal MLIR form before translation to `llvm::Module`. | The module has kernel attributes, target metadata, ABI-ready arguments, and no high-level tile operations. |

## Stage Contracts

### `cuda_tile -> nv_tileaa`

The producer-facing legality boundary. Elementwise math may become standard `arith` or `math`; tile, view, token, memory, reduction, scan, MMA, and entry operations become TileAA operations. This stage also establishes the type converter that maps public tile/view/token types to internal equivalents.

Key invariant: `cuda_tile` is illegal after this pass. A producer bug should surface here, while the IR is still close to the public dialect.

### `nv_tileaa -> nv_tileas`

TileAA describes what the program means; TileAS begins describing how the program will execute. This stage introduces layout-aware constants, schedulable memory operations, async pipeline structure, and TileAS function forms, while preserving the ordinary `arith`, `math`, and bridge operations that later passes still own.

Key invariant: tile-level memory and compute are now in the dialect the scheduler and layout passes understand.

### `nv_tileas -> llvm/nvvm`

Scheduled tile execution becomes ABI-ready LLVM and NVVM. Loads, stores, allocas, layout conversions, async pipeline ops, cluster barriers, TMA operations, and target-specific helpers turn into LLVM dialect operations, NVVM intrinsics, or tightly scoped inline assembly.

Key invariant: once this stage completes, TileAS no longer owns executable semantics. Any surviving companion-dialect operations must be explicitly legal because a sister pass will lower them.

### Companion and GPU Lowering

The CuTe/CUTLASS/NVGPU path lowers layout atoms, TMA copies, WGMMA/tcgen05 operations, grid-constant argument attributes, and kernel markers. The standard GPU lowering path handles thread/block/cluster IDs, barriers, dynamic shared memory, `printf`, subgroup operations, GPU functions, returns, and launch packing.

Key invariant: before serialization, the surviving `gpu.module` contains only LLVM/NVVM-compatible operations and exactly one resolved target.

## Kernel Entry ABI

Kernel tagging is staged because the function is not ABI-ready until after function-type conversion. Early lowering marks the intended entry point with a dialect-level kernel marker and carries launch metadata — requested threads, cluster dimensions, CTA count, occupancy, register limits. Final NVVM lowering rewrites that marker to `nvvm.kernel` and migrates argument attributes such as grid-constant semantics onto LLVM-compatible function arguments.

```c
void finalize_kernel_entry(Function fn, KernelSpec spec, TargetInfo target) {
    require(fn.has_attr("cute.kernel") || fn.has_attr("tile.kernel"));

    fn.remove_attr("cute.kernel");
    fn.set_attr("nvvm.kernel", true);

    fn.set_attr("nvvm.reqntid", dim3(32 * spec.num_warps, 1, 1));
    fn.set_attr("nvvm.minctasm", spec.num_ctas);

    if (target.supports_cluster_launch() && spec.cluster_product > 1) {
        fn.set_attr("nvvm.cluster_dim", spec.cluster_dim);
        fn.set_attr("nvvm.blocksareclusters", true);
    }

    if (spec.max_registers) {
        fn.set_attr("nvvm.maxnreg", *spec.max_registers);
    }

    for (Argument arg : fn.arguments()) {
        if (arg.has_attr("cute_nvgpu.grid_constant")) {
            arg.remove_attr("cute_nvgpu.grid_constant");
            arg.set_attr("nvvm.grid_constant", true);
        }
    }
}
```

The separation is practical: entry-point intent exists before LLVM argument types exist, but final NVVM attributes must attach to the exact arguments the backend will see.

## Type ABI

One LLVM type converter spans the TileAS, Tile function, and companion NVGPU/CuTe lowering paths, so every pass agrees on ABI shape. The important rules:

| Source concept | LLVM/NVVM representation |
|---|---|
| Ranked memref or internal tile memory reference | Descriptor `{allocatedPtr, alignedPtr, offset, sizes[N], strides[N]}` unless bare-pointer kernel ABI applies. |
| Kernel memref argument under bare-pointer ABI | The aligned pointer becomes the formal argument; sizes, strides, and launch metadata are carried separately. |
| `index` | Target index integer width, normally `i64` unless configured otherwise. |
| Vectors | LLVM vector type with converted element type. |
| Async/pipeline token | `i32`; the low bit carries producer/consumer phase for parity-sensitive waits. |
| Tiled view descriptor | Small LLVM struct containing the base pointer and packed layout/rank metadata. |
| Memory spaces | Address-space-qualified pointers; global, shared, constant, local, and tensor memory remain distinct. |

These conversions are ABI commitments. A reimplementation may rearrange the internal pass structure, but it must not silently change descriptor field order, token width, address-space classification, or kernel argument lowering.

## Lowering Stages

Lowering runs as four named conversion passes plus a small cluster of companion passes that prepare the module for NVPTX serialization. Each stage hands a specific kind of state to the next: TileAA hands aliasing-aware tile algebra to TileAS, TileAS hands scheduled-and-laid-out tile execution to the LLVM stage, the LLVM stage hands ABI-ready LLVM IR plus a populated `gpu.module` to the target-attribute and debug-info passes, and those leave a module the GPU-to-binary serializer can consume directly.

### Stage 1 — `ConvertCudaTileToTileAA`

Rewrites the public input dialect. Three populators run in fixed order: Part A covers arithmetic and structured control flow, Part B covers memory, pointer, token, and view operations, Part C lowers the four specialists (`mmaf`, `mmai`, `reduce`, `scan`) whose shapes depend on decisions made by A and B. The pass installs three type-converter functor pairs that bridge the public `TileType`, `PointerType`, and `TokenType` to their `nv_tileaa` equivalents.

Hand-off: every `cuda_tile.*` op has been rewritten. `nv_tileaa.*` carries the alias-aware tile algebra. Tokens are still SSA values with explicit memory dependences.

### Stage 2 — `ConvertTileAAToTileAS`

Lowers TileAA's "what the program means" view into TileAS's "how the program will execute" view. CopyAtom and ReduceAtom witnesses attach to memory operations during this stage and ride verbatim onto their TileAS replacements; the downstream LLVM stage reads them to pick the concrete hardware primitive. The kernel-spec attribute mirrors onto the function so SM-gated rewrites (notably the SM100 block-scaled MMA path) have a target spec to consult.

Hand-off: TileAS operations carry async-pipeline, layout, and TMA-descriptor structure. The [TileAS scheduling and layout-assignment passes](../scheduler/overview.md) (D07 through D22) now own the module.

### Stage 3 — `ConvertTileFuncToLLVM` then `ConvertTileASToLLVM`

Function-boundary conversion runs first. `ConvertTileFuncToLLVM` rewrites `nv_tileaa.func` and `nv_tileaa.return` into `func.func` and `func.return`, applies the bare-pointer ABI, and translates kernel-spec fields into `nvvm.*` attributes (`nvvm.reqntid`, `nvvm.cluster_dim`, `nvvm.minctasm`, `nvvm.maxnreg`). Kernel-returning operands fail the pass with an explicit diagnostic; non-kernel functions may return arbitrary value lists.

`ConvertTileASToLLVM` then rewrites bodies in nine phases (decompose-print, bufferization analysis, main TileAA/TileAS rewrites, bulk supplementary, cute/cute_nvgpu, async.pipeline, arith/llvm cleanup, reconcile-unrealized-casts, late materializer). The shared-memory scratch global `@global_smem` is emitted before any pattern runs when the kernel requested extended shared memory. The PDL-to-PDLInterp fallback compiles embedded PDL bytecode immediately before the conversion engine runs.

Hand-off: `nv_tileaa` and `nv_tileas` no longer appear in executable positions. `llvm.*` and `nvvm.*` carry the kernel; `cute.*`, `cute_nvgpu.*`, and `cutlass.*` survive only where a companion pass is responsible for them.

### Stage 4 — Companion lowering and target attachment

`ConvertCuteAndCuteNvgpuToLLVM` desugars layout sugar, lowers primitive CuTe descriptor and tuple operations, then dispatches architectural atoms (SM90 WGMMA, SM100 IMMA, SM100 shared-to-tensor copy) to their dedicated rewriters. `ConvertNVGPUAndGPUToNVVM` rewrites the standard `gpu` dialect and the `nvgpu` architectural surface into `nvvm.*` intrinsics. `AttachNVVMTarget` reads the module's compute-capability and target-spec attributes and writes a populated `#nvvm.target` attribute onto the `gpu.module`. `TranslateDebugInfo` rewrites `debuginfo.value` chains into LLVM debug intrinsics with the NVIDIA-specific `llvm.nvvm.move` value pin.

Hand-off: every executable op is `llvm.*` or `nvvm.*`, the `gpu.module` carries exactly one resolved target, and debug metadata is in LLVM form. The module is ready for GPU-to-binary serialization.

## Stage Sequence

```c
ModuleOp lower_to_nvvm(ModuleBytecode input, CompileOptions options) {
    ModuleOp module = parse_tileir(input);

    run_pass<ConvertCudaTileToTileAA>(module, options);
    run_pass<ConvertTileAAToTileAS>(module, options);

    run_pass<ConvertTileFuncToLLVM>(module, options);
    run_pass<ConvertTileASToLLVM>(module, options);

    run_pass<ConvertCuteAndCuteNvgpuToLLVM>(module, options);
    run_pass<ConvertNVGPUAndGPUToNVVM>(module, options);

    run_pass<AttachNVVMTarget>(module, options);
    run_pass<TranslateDebugInfo>(module, options);

    return module;
}
```

Each pass owns one boundary. The driver does not interleave them — Tile-function conversion must complete before TileAS bodies lower, body lowering must complete before companion CuTe/NVGPU passes run, and target attachment is last because it depends on a fully-lowered `gpu.module`.

Pattern population, type conversion, and pattern-bank structure are described in [Pattern Sets and Type Conversion](pattern-set-and-typeconverter.md). This overview leans on the invariant that each stage has a complete legality target and a type converter that agrees with the next stage.

## Options and Placement

The conversion cascade runs at every optimization level because later backend stages cannot consume high-level TileIR. Optimization level and pipeline strategy mainly choose auxiliary cleanup, scheduling, async pipeline, debug-info, and snapshot behavior around the mandatory conversions.

| Option family | Effect on lowering |
|---|---|
| Optimization level | Selects cleanup intensity and LLVM/NVVM optimization level, but does not remove the dialect cascade. |
| Pipeline strategy | Changes async pipeline materialization and scheduling choices before TileAS-to-NVVM lowering. |
| Debug and line info | Enables debug-info conversion and preserves source scopes into LLVM metadata. |
| Target GPU / PTX version | Feeds `#nvvm.target`, feature strings, cluster attributes, and target-gated intrinsic selection. |
| `use-nvgpucomp-libnvvm` style switches | Select whether serialization uses the bundled open NVPTX path or a libNVVM/NVGPUComp path when available. |

## Lowering Invariants

- No `cuda_tile` operations survive after `cuda_tile -> nv_tileaa`.
- No executable TileAA compute or memory operations survive after TileAA-to-TileAS, except explicitly legal bridge operations owned by later passes.
- `cute`, `cute_nvgpu`, and `cutlass` operations may remain only when a later companion pass declares them legal.
- Memrefs lower to LLVM descriptors unless a kernel bare-pointer ABI rule applies.
- Async and pipeline tokens lower to `i32`; parity-sensitive tokens use the low bit as phase state.
- Kernel metadata is staged: tile-level entry metadata first, final `nvvm.kernel` only after LLVM-compatible arguments exist.
- Target metadata must exist before serialization: triple, chip, feature string, optimization level, and libNVVM/NVPTX flags.
- The final `gpu.module` must be serializable without consulting any `cuda_tile`, TileAA, or TileAS verifier.

## Cross-Links

- [cuda_tile to nv_tileaa](cuda-tile-to-tileaa.md) covers the public input-dialect conversion.
- [nv_tileaa to nv_tileas](tileaa-to-tileas.md) covers the analysis-to-scheduled-tile transition.
- [nv_tileas to LLVM](tileas-to-llvm.md) covers async, memory, layout, and TileAS lowering.
- [cute / cute_nvgpu to LLVM](cute-and-cute_nvgpu-to-llvm.md) covers companion dialect lowering.
- [nvgpu / gpu to NVVM](nvgpu-and-gpu-to-nvvm.md) covers standard GPU/NVGPU lowering.
- [Target and Debug Info](target-and-debuginfo.md) covers `#nvvm.target` and debug metadata.
