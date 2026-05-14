# Compilation Pipeline Overview

## Abstract

Tileiras consumes a `builtin.module` carrying one or more `gpu.module` payloads expressed in the `cuda_tile` dialect and produces a relocatable object containing assembled cubin. The work splits cleanly across a host-side outer pipeline that operates on the enclosing module and a device-side inner pipeline that runs once per `gpu.module`. The outer pipeline registers dialects, resolves a single `#nvvm.target` per device module, and walks each `gpu.module` through dialect lowering. The inner pipeline pushes TileIR through TileAA, TileAS, CuTe/CUTLASS, NVGPU, and finally the MLIR `llvm`+`nvvm` dialect pair, then hands the result to an embedded LLVM 21 NVPTX backend that emits PTX. The driver invokes `ptxas` on that PTX and stitches the cubin into the output object. Each cascade page underneath this one documents one stage of that chain; this page is the contract between them.

## Full cascade

```
   MLIR bytecode (input)
     ↓
   cuda_tile dialect (public surface)
     ↓
   nv_tileaa dialect (analysis)
     ↓
   nv_tileas + cute + cute_nvgpu + cutlass dialects
     ↓
   mlir::nvgpu intermediate
     ↓
   llvm + nvvm dialects
     ↓
   libNVVM linkage
     ↓
   NVPTX backend (LLVM 21 fork)
     ↓
   PTX assembly
     ↓
   ptxas (downstream)
     ↓
   cubin
```

The descent is driven by three driver responsibilities:

1. Register the dialect universe needed by the pipeline.
2. Build a pass manager from resolved pipeline options.
3. Run the MLIR pipeline, translate the resulting GPU module to LLVM/NVVM, and serialize it through the NVPTX backend.

Instrumentation exposes two major scopes: `CompileNVVM` for the MLIR lowering work and `SerializeGPUModule` for the LLVM/NVPTX serialization work. Those two scopes are a useful mental boundary: above them the program is still MLIR; below them it is LLVM IR, PTX, and finally cubin/object data.

## Dialect handoff points

Each row is one boundary in the cascade. The "entry-pass" column names
the pass that introduces the lower-dialect ops; the "key invariant"
column names what must hold at the moment the pass is added.

| From | To | Boundary operation | Key invariant on entry |
|---|---|---|---|
| `cuda_tile` | `nv_tileaa` | Convert public TileIR to alias-aware TileAA. | Module is fresh from bytecode loading; one `gpu.module` is present. |
| `nv_tileaa` | `nv_tileas` | Lower typed, alias-aware operations into assembler-near TileAS operations. | Per-function TileAA cleanup has settled canonical forms. |
| `nv_tileas` plus `cute*`/`cutlass` | `nvgpu` | Materialize schedules, layouts, TMA descriptors, and hardware-facing operations. | TileAS scheduling and layout passes have made execution structure explicit. |
| `nvgpu` | `llvm` plus `nvvm` | Convert NVIDIA GPU dialect operations to NVVM intrinsics and LLVM dialect operations. | Memref, vector, and math lowering have removed higher-level abstractions. |
| Untargeted `gpu.module` | Targeted `gpu.module` with `#nvvm.target` | Attach the resolved NVPTX target attribute. | Kernel metadata and target options are still available. |
| MLIR `llvm` dialect | `llvm::Module` | Translate MLIR LLVM dialect to an LLVM module. | Exactly one GPU target has been resolved. |
| `llvm::Module` | linked `llvm::Module` | Link external bitcode/blob libraries. | Any libdevice surrogate payloads have been attached. |
| linked `llvm::Module` | optimized `llvm::Module` | Run the LLVM optimization pipeline. | Target machine and optimization level are known. |
| optimized `llvm::Module` | PTX text | Run the NVPTX backend. | NVPTX subtarget information is populated. |
| PTX text | cubin/object payload | Invoke `ptxas` and package the result. | PTX has been emitted for the resolved target. |

The first six rows are "tier-1" boundaries (MLIR-on-MLIR passes inside
the visible PassManager). The remaining four rows are "tier-2"
boundaries (libNVVM linkage + NVPTX codegen). The split between the two
tiers is described below.

## Pass Pipeline Shape

At maximum optimization the visible MLIR cascade is a long nested pass manager, but the important shape is easier to understand as phase groups:

| Phase | Purpose | Typical scope |
|---|---|---|
| Frontend conversion | Convert input `cuda_tile` operations into `nv_tileaa`. | `gpu.module` |
| Early debug and cleanup | Attach debug scopes, canonicalize, and remove obvious redundancy. | top-level and `gpu.module` |
| TileAA to TileAS | Lower alias-aware operations into assembler-near TileAS functions. | nested `nv_tileaa.func` |
| Host/callback materialization | Emit host wrapper and callback plumbing. | `gpu.module` |
| TileAS scheduling and layout | Materialize async pipeline, convert layouts, assign buffers, plan CTA/cluster behavior, and generate schedules. | `gpu.module` |
| LLVM/NVGPU lowering | Convert TileAS/CuTe/CUTLASS operations toward `nvgpu`, `llvm`, and `nvvm`. | `gpu.module` |
| Kernel legalization/finalization | Normalize kernel attributes, target metadata, and debug scopes. | top-level and `gpu.module` |
| Post-lowering cleanup | Canonicalize and run CSE/DCE after the largest rewrites. | `gpu.module` |
| LLVM translation | Translate MLIR LLVM dialect to `llvm::Module`. | whole module |
| LLVM optimization | Run the LLVM PassBuilder pipeline for the selected optimization level. | `llvm::Module` |
| NVPTX emission | Emit PTX and assemble it downstream. | target module |

The detailed pass-count page remains the right place for exact pass ordering and opt-level deltas. This overview is the semantic contract: each phase must leave the module in the form expected by the next phase.

## Outer and Inner Pipelines

The driver runs two pass managers in sequence on a single MLIR context. The outer pass manager is anchored on `builtin.module`. It registers every dialect that any later stage might need, parses the bytecode, and runs only a small amount of work directly on the top-level module: dialect normalization, host-wrapper attribute resolution, and the `gpu.module` walk that distributes per-device work. The inner pass manager is anchored on `gpu.module`. It is constructed once and reused for each device module the walk discovers. The two managers share an `OperationName` cache and an analysis manager, but they keep separate verifier-each settings because the outer pipeline runs cheap structural checks and the inner pipeline runs expensive type-and-region checks that fire on every TileIR mutation.

```c
LogicalResult run_tileiras_pipeline(ModuleOp top, PipelineOptions opts) {
    PassManager outer  = make_pass_manager(top->getName(), &top->getContext());
    populate_outer_pipeline(&outer, opts);

    OpPassManager *inner = &outer.nest<GpuModuleOp>();
    populate_inner_pipeline(inner, opts);

    return outer.run(top);
}
```

The outer pipeline guarantees three invariants before the inner pipeline starts. First, every `gpu.module` carries exactly one resolved `#nvvm.target` attribute giving SM name, PTX feature string, and launch-shape constants. Second, each kernel symbol has a normalized linkage attribute and a populated parent symbol table. Third, target-machine options that the inner passes read by name (`num-warps`, `num-ctas`, `index-bitwidth`) have been attached to the device module so that nested passes pick them up through MLIR's standard attribute lookup rather than through driver globals.

State hand-off between the two pipelines is therefore purely attribute-based: there are no thread-local or driver-side dictionaries that the inner pipeline reads at run time. This rule is what makes the inner pipeline reentrant when the outer walk crosses multiple `gpu.module` ops with different targets in the same compile.

## Kernel-Attribute Lift

A `cute.kernel` attribute marks a function as a GPU entry point while the module is still in the Tile/CuTe half of the inner pipeline. The lift converts that marker into a chain of three downstream attributes: the function gains `nvvm.kernel`, the parent `gpu.module` gains `#nvvm.target`, and after MLIR-to-LLVM translation the corresponding `llvm.func` gains the `ptx_kernel` calling convention plus the launch-shape function attributes that the NVPTX backend reads.

```c
void lift_kernel_attributes(GpuModuleOp gpu, NvvmTargetAttr target) {
    require(!gpu->hasAttr("nvvm.target"),
            "gpu.module already carries a conflicting target");

    for (FuncOp fn : gpu.getOps<FuncOp>()) {
        if (!fn->hasAttr("cute.kernel")) {
            continue;
        }
        fn->removeAttr("cute.kernel");
        fn->setAttr("nvvm.kernel", UnitAttr::get(gpu.getContext()));
        propagate_launch_shape(fn, target);
    }

    gpu->setAttr("nvvm.target", target);
}
```

The lift is the line at which target selection stops being implicit. Above it, architecture information lives in Tile-level attributes and pipeline options. Below it, only the triple, CPU string, feature string, and per-function attributes derived from `#nvvm.target` remain.

## Serialization Boundary

When the inner pipeline finishes, the `gpu.module` contains only `llvm` and `nvvm` dialect operations plus the resolved target attribute. The driver then runs serialization, which is not a pass — it is a context-level translation that walks the `gpu.module`, builds an `llvm::Module` through MLIR's `translateModuleToLLVMIR`, [links the embedded libdevice surrogate](../libdevice/overview.md#link-inline-simplify), runs an [LLVM `PassBuilder` pipeline](passbuilder-mega-registry.md) at the driver's chosen `OptimizationLevel`, emits PTX through the [NVPTX backend](../codegen/overview.md), and invokes `ptxas` to produce cubin.

```c
ByteBuffer serialize_gpu_module(GpuModuleOp gpu, PipelineOptions opts) {
    NvvmTargetAttr target = cast<NvvmTargetAttr>(gpu->getAttr("nvvm.target"));
    LLVMModulePtr llvm   = translate_to_llvm_ir(gpu);

    link_libdevice_surrogate(llvm, target);
    run_llvm_passbuilder_pipeline(llvm, target, opts.opt_level);

    StringRef ptx = emit_ptx_with_nvptx_backend(llvm, target);
    return invoke_ptxas(ptx, target, opts);
}
```

Two consequences of this boundary matter when debugging. The MLIR pass timing report and the [action handler trace](instrumentation-and-action-handler.md#mlir-actions) cover only the work above the boundary. Below it, all timing comes from LLVM's `--time-passes` and from `ptxas` profiling output. The verifier layers reset across the boundary: between-pass verification stops, and what replaces it is the LLVM module verifier plus the [NVVM kernel-launch verifier](../nvptx-passes/nvvm-ir-verifier.md) that runs at module-finalize time.

## Δ vs cicc

`cicc` and Tileiras meet at the LLVM/NVVM layer, not at the source-language layer. `cicc` enters from CUDA C++ front-end output: textual LLVM IR or bitcode already expressed with NVVM intrinsics and CUDA device ABI conventions. Tileiras enters from CUDA TileIR bytecode and owns a much larger upper half — Tile dialect parsing, TileAA analysis, TileAS scheduling, CuTe/CUTLASS materialization, GPU layout decisions, and MLIR-to-LLVM lowering. Once both compilers hold an LLVM module with NVVM intrinsics, their remaining responsibilities converge.

| Area | `tileiras` | `cicc` | Shared after convergence |
|---|---|---|---|
| Input language | CUDA TileIR bytecode | CUDA front-end LLVM IR/bitcode | no |
| Tile/CuTe/CUTLASS dialect cascade | yes | no | no |
| Tile scheduling and layout materialization | yes | no | no |
| LLVM/NVVM module optimization | yes | yes | yes |
| NVPTX target and asm printer | yes | yes | yes |
| PTX-to-cubin handoff through `ptxas` | yes | yes | yes |

## Cross-References

[Driver Entry and Optimization Levels](driver-and-opt-levels.md) covers how `--opt-level` resolves to a concrete pipeline. [Pass Manager Internals](pass-manager-internals.md) documents the nesting and dispatch rules these two pipelines rely on. [Pipeline Invariants and Verifiers](invariants-and-verifiers.md) names the three verifier layers that guard the cascade. [Pass List by Optimization Level](full-pass-list-by-opt-level.md) is the right place to look for exact pass ordering. Backend-side documentation lives under the [NVPTX Backend Passes overview](../nvptx-passes/overview.md), the [Codegen overview](../codegen/overview.md), and the [libdevice overview](../libdevice/overview.md).
