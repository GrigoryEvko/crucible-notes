# Compilation Pipeline Overview

`tileiras` is, end-to-end, an MLIR-on-MLIR optimizing assembler whose job is
to take a CUDA Tile IR bytecode module and ride it down a deep dialect
cascade until what remains is a single `gpu.module` carrying the `nvvm`
dialect plus a fully-populated `#nvvm.target` attribute. That artefact is
then translated to LLVM IR, linked against bitcode blobs (libdevice
surrogate), pushed through an LLVM `PassBuilder` pipeline, and emitted as
PTX assembly by an embedded fork of the LLVM 21 NVPTX backend. PTX is
finally handed off to `ptxas` for SASS/cubin generation. This page is the
cascade anchor for the `pipeline/` section: each pass family, each dialect
boundary, and each handoff has a dedicated page underneath; this page
describes the shape of the whole.

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
| `cuda_tile` | `nv_tileaa` | Convert public Tile IR to alias-aware TileAA. | Module is fresh from bytecode loading; one `gpu.module` is present. |
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

## Kernel-attribute lift

A `cute.kernel` attribute marks a function as a GPU entry point while the
module is still in the Tile/CuTe half of the pipeline. Lowering must carry that
entry-point fact all the way to LLVM. The public contract is:

1. A Tile/CuTe kernel function is converted to a function marked with the NVVM
   kernel attribute.
2. The parent `gpu.module` receives exactly one `#nvvm.target` attribute.
3. That target attribute carries the resolved SM architecture, PTX feature set,
   and launch-shape metadata needed by the backend.
4. LLVM translation turns each NVVM kernel into an LLVM function with the
   backend-visible `ptx_kernel` calling convention/attribute set.

The lift is the point where target selection stops being implicit. Above it,
architecture information can be represented as Tile-level attributes and
pipeline options. Below it, the backend sees only the LLVM triple, CPU string,
feature string, and function attributes derived from `#nvvm.target`.

```c
struct NvvmTarget {
    string triple;       // "nvptx64-nvidia-cuda" for normal 64-bit CUDA device code
    string chip;         // for example "sm_90", "sm_100", or "sm_120"
    string features;     // comma-separated PTX/subtarget feature list
    int num_warps;
    int num_ctas;
};

NvvmTarget lift_gpu_target(GpuModule module, CompileOptions options) {
    TargetInfo target = resolve_target(options.gpu_name, options.ptx_version);

    require(module.targets.empty(),
            "GPU module must not already carry a conflicting target");
    require(module.kernel_functions.size() > 0,
            "GPU module must contain at least one kernel entry point");

    for (Function fn : module.kernel_functions) {
        if (fn.has_attr("cute.kernel")) {
            fn.remove_attr("cute.kernel");
            fn.set_attr("nvvm.kernel", true);
        }
    }

    NvvmTarget nvvm = {
        .triple = target.pointer_bits == 32
            ? "nvptx-nvidia-cuda"
            : "nvptx64-nvidia-cuda",
        .chip = target.sm_name,
        .features = target.feature_string,
        .num_warps = options.num_warps,
        .num_ctas = options.num_ctas,
    };

    module.set_attr("nvvm.target", nvvm);
    return nvvm;
}
```

## Two-tier compiler shape

`tileiras` is easiest to reimplement as two compilers stacked behind one
driver contract.

The outer tier is the MLIR compiler. It owns bytecode loading, dialect
registration, pass-manager construction, Tile/CuTe/CUTLASS lowering, scheduling,
layout decisions, and conversion to the MLIR `llvm`/`nvvm` dialects. User-facing
pipeline controls such as optimization level and pipeline strategy primarily
select this tier's pass composition.

The inner tier begins when the module is ready to serialize as a GPU target. It
translates the MLIR LLVM dialect to an `llvm::Module`, links embedded bitcode
libraries, runs the LLVM optimization pipeline selected for the requested
optimization level, registers/selects the NVPTX target, emits PTX, and returns
that PTX to the driver for `ptxas` assembly.

```c
ByteBuffer compile_tileir(ModuleBytecode input, CompileOptions options) {
    MlirContext ctx = make_tileiras_context();
    ModuleOp module = parse_tileir_bytecode(ctx, input);

    PassManager pm = build_tileiras_pipeline(options);
    run(pm, module);

    GpuModule gpu = require_single_gpu_module(module);
    NvvmTarget target = require_single_nvvm_target(gpu);
    string ptx = serialize_gpu_module(gpu, target, options);

    return assemble_with_ptxas(ptx, target, options);
}

string serialize_gpu_module(GpuModule gpu, NvvmTarget target,
                            CompileOptions options) {
    LLVMModule llvm = translate_mlir_llvm_to_llvm_ir(gpu);
    link_external_bitcode_libraries(llvm, options);
    run_llvm_optimization_pipeline(llvm, target, options.opt_level);
    return emit_ptx_with_nvptx_backend(llvm, target);
}
```

This split is more than an implementation detail. It tells reimplementers where
the format boundary is: everything before serialization is MLIR dialect
semantics; everything after serialization is ordinary LLVM/NVVM module
semantics plus NVPTX code generation. It also explains why pass debugging and
backend debugging need different tools and different invariants.

## Δ vs cicc

`cicc` (the legacy CUDA front-end compiler binary) and `tileiras`
meet at the LLVM/NVVM layer, not at the source-language layer.

`cicc` enters from CUDA C++ front-end output: textual LLVM IR or LLVM bitcode
already expressed with NVVM intrinsics and CUDA device ABI conventions.
`tileiras` enters from CUDA Tile IR bytecode and therefore owns a much larger
upper half: Tile dialect parsing, TileAA analysis, TileAS scheduling,
CuTe/CUTLASS materialization, GPU layout decisions, and MLIR-to-LLVM lowering.

Once both compilers hold an LLVM module with NVVM intrinsics, their remaining
responsibilities converge:

| Area | `tileiras` | `cicc` | Shared after convergence |
|---|---|---|---|
| Input language | CUDA Tile IR bytecode | CUDA front-end LLVM IR/bitcode | no |
| Tile/CuTe/CUTLASS dialect cascade | yes | no | no |
| Tile scheduling and layout materialization | yes | no | no |
| LLVM/NVVM module optimization | yes | yes | yes |
| NVPTX target and asm printer | yes | yes | yes |
| PTX-to-cubin handoff through `ptxas` | yes | yes | yes |

The practical rule is simple: documentation under `dialects/`, `passes/`, `scheduler/`, `lowering/`, and most of `mlir-infra/` describes `tileiras` behavior. Documentation under `nvptx-passes/`, `codegen/`, and `libdevice/` describes the shared LLVM/NVPTX backend path unless a page explicitly says otherwise. Exact pass counts are intentionally not repeated here because they vary with optimization level, warp-specialization mode, and pipeline strategy; use `pipeline/full-pass-list-by-opt-level.md` when exact pass ordering matters.
