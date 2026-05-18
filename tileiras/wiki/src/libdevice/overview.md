# libdevice Overview

## Abstract

`libdevice` is the NVIDIA device math library: a precompiled LLVM bitcode module shipped with CUDA that supplies device-side bodies for hundreds of math functions — `__nv_sin`, `__nv_cos`, `__nv_exp`, `__nv_log`, `__nv_pow`, `__nv_sqrt`, `__nv_div_*`, the full transcendental and special-function set, and their `f`/`d` variants. Each body is written in LLVM IR with NVPTX-aware patterns, parameterized on per-module configuration (flush-to-zero mode, IEEE divide/sqrt precision, fast integer division) through `__nvvm_reflect("KEY")` queries. TileIR lowering emits direct calls to these declarations whenever a GPU math operation is better represented as a library call than as a single intrinsic. Before NVPTX code generation, every `__nv_*` declaration must resolve to a concrete bitcode body — unresolved external declarations are a backend error.

The integration is a four-pass correctness sequence: link the library bitcode into the user module so the `__nv_*` declarations gain definitions, fold every `__nvvm_reflect("KEY")` call site into a configuration-derived integer constant, run an always-inliner pass that fires on every libdevice function, and simplify the now-resolved configuration branches plus garbage-collect the unused library helpers. The sequence runs at every optimization level — even `-O0` — because resolution is required for correctness rather than for speed.

## Pipeline

```text
LLVM module with calls to __nv_* declarations
    |
    | link embedded or supplied libdevice bitcode
    v
LLVM module with __nv_* definitions
    |
    | fold __nvvm_reflect("KEY") queries
    v
configuration-specialized libdevice bodies
    |
    | always-inline libdevice calls into kernels
    v
kernel bodies containing selected math implementations
    |
    | simplify branches, fold constants, remove unused library functions
    v
LLVM module ready for NVPTX code generation
```

The effective order matters. Libdevice bodies contain reflection queries, so reflection folding must see the linked bodies. Inlining should run after reflection so dead configuration arms are already easy to remove. Constant folding and global dead-code elimination then remove unused paths and unused library definitions.

## Reflection

`__nvvm_reflect` is a compile-time query mechanism. Libdevice bodies call it with string keys — `"__CUDA_FTZ"`, `"__CUDA_PREC_DIV"`, `"__CUDA_PREC_SQRT"`, `"__CUDA_FAST_INT_DIV"`, `"__CUDA_ARCH"`, and their variants — and the reflect pass replaces those calls with integer constants drawn from a three-source resolver: module-level metadata (`!nvvm-reflect` and module flags), command-line overrides (`-mllvm -nvvm-reflect-add=KEY=VAL`), and target defaults. The result of folding is dead-branch material: each query lives inside an `if (__nvvm_reflect("KEY")) { … }` guard inside the library body, and once the call is replaced by `i32 0` or `i32 1`, normal IR simplification eliminates the unreachable arm.

```c
PreservedAnalyses NVVMReflectPass::run(Module &m, ModuleAnalysisManager &) {
    DenseMap<StringRef, int> resolved;
    seed_from_module_metadata(m, resolved);     /* !nvvm-reflect / module flags */
    seed_from_command_line(resolved);           /* -nvvm-reflect-add=KEY=VAL    */
    seed_from_target_defaults(m, resolved);     /* SM-derived defaults          */

    for (StringRef name : {"__nvvm_reflect", "__nvvm_reflect_ocl",
                           "_Z20__nvvm_reflectPKc",   /* …5 mangled variants… */}) {
        Function *f = m.getFunction(name);
        if (!f) continue;
        for (User *u : llvm::make_early_inc_range(f->users())) {
            auto *call = cast<CallInst>(u);
            StringRef key = require_constant_cstring(call->getArgOperand(0));
            int v = resolved.lookup_or_zero(key);    /* unknown → 0, recorded once */
            call->replaceAllUsesWith(ConstantInt::get(call->getType(), v, /*Signed=*/false));
            call->eraseFromParent();
        }
        if (f->use_empty()) f->eraseFromParent();
    }
    return PreservedAnalyses::none();
}
```

Unknown keys resolve to zero, and the resolver records the zero so the same unknown key folds consistently at every call site. A reimplementer wanting bug-for-bug compatibility must seed the resolver from the same three sources in the same priority order — module metadata wins over target defaults, command-line overrides win over both. Diverging from the zero-default for unknown keys is observable: libdevice bodies pick different approximation paths based on whether a flag resolves to `0` or `1`.

## Link, inline, simplify

Libdevice linking is a module-construction step rather than an optimization pass. The driver parses the embedded bitcode blob into an `llvm::Module`, then runs the LLVM linker in `OnlyNeeded` mode so only functions reachable from the user module are pulled in. Once linked, the user module gains concrete bodies for every previously external `__nv_*` declaration. Each libdevice body carries the `alwaysinline` attribute, so a dedicated always-inliner pass — separate from the optimization-level inliner — fires on every call site regardless of `-O0`/`-O1`. After inlining, the configuration constants left behind by `NVVMReflectPass` propagate into the inlined bodies; the subsequent simplify-cfg + SCCP + global-DCE pair collapses the now-dead approximation arms and removes the library functions that no longer have callers.

```c
bool prepare_libdevice(Module &user, MemoryBufferRef libdevice_bc, ReflectConfig cfg) {
    /* 1. parse and link — OnlyNeeded keeps the user module small */
    std::unique_ptr<Module> lib = parseBitcodeFile(libdevice_bc, user.getContext());
    if (Linker::linkModules(user, std::move(lib), Linker::Flags::OnlyNeeded))
        return false;

    /* 2. resolve every __nvvm_reflect call into a configuration-derived constant */
    ModulePassManager mpm;
    mpm.addPass(NVVMReflectPass(cfg));

    /* 3. always-inline libdevice bodies into their call sites */
    mpm.addPass(AlwaysInlinerPass(/*InsertLifetime=*/false));

    /* 4. simplify the configuration-folded branches and garbage-collect leftovers */
    FunctionPassManager fpm;
    fpm.addPass(SimplifyCFGPass());
    fpm.addPass(SCCPPass());
    fpm.addPass(InstCombinePass());
    mpm.addPass(createModuleToFunctionPassAdaptor(std::move(fpm)));
    mpm.addPass(GlobalDCEPass());

    ModuleAnalysisManager mam;
    mpm.run(user, mam);

    return verify_no_unresolved_libdevice_declarations(user);
}
```

At higher optimization levels the standard inliner, instruction combiner, SCCP, GVN, and global DCE refine the result further. At `-O0` the four-pass sequence still runs because resolution is a correctness requirement: an unresolved `__nv_sin` declaration reaches the NVPTX backend as an external symbol the backend cannot lower into PTX.

## Constant folding

After linking and reflection, many libdevice call paths reduce to compile-time constants or short arithmetic chains. Constant folding evaluates calls with constant operands (`__nv_sin(0.0)` collapses to `0.0`), prunes the dead `if (FTZ) { … } else { … }` arms that reflection just selected, and global-DCE removes the library helpers whose only callers have been inlined away. This matters most for math functions with multiple approximation paths behind target-mode checks: without folding, the IR retains the unselected approximation as dead code the backend then has to schedule around.

The goal is not to prove every math call at compile time. The goal is to specialize the library to the selected target and remove impossible branches before the backend sees them.

## Cross-references

The reflection key set, the three-source resolver, and the post-reflect constant-conditional cleanup pass are documented in [NVVMReflect Mechanism — Three var-map sources](nvvm-reflect-mechanism.md#three-var-map-sources) and [NVVMReflect Mechanism — Post-reflect cleanup](nvvm-reflect-mechanism.md#post-reflect-cleanup-nvvm-reflect-pp). The mapping from libdevice function names to LLVM intrinsic IDs — used by the constant folder to recognize math calls without reading their bodies — is documented in [Intrinsic ID Switch and Name Table — libdevice suffix name table](intrinsic-id-switch-and-name-table.md#libdevice-suffix-name-table). The surrounding LLVM math-optimization flow, including the crosswalk between libdevice calls and target-specific intrinsics, is covered in [Math Pass Pipeline and Crosswalk — Full math-op crosswalk](math-pass-pipeline-and-crosswalk.md#full-math-op-crosswalk). The NVPTX bring-up that links the embedded libdevice bitcode at module construction time is documented in [NVPTX Bring-up and Target Init](../codegen/nvptx-bring-up-and-target-init.md).
