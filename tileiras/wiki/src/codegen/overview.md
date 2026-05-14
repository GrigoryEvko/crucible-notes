# Codegen Overview

## Abstract

The backend half of `tileiras` starts where the MLIR pipeline ends: an
NVVM-ready `gpu.module` with a resolved `#nvvm.target`. The program is no
longer TileIR. It is an LLVM/NVVM module that must be linked against device
libraries, optimized, lowered through NVPTX target rules, selected into
machine instructions, and printed as PTX text for `ptxas`. This page states
the contracts and invariants each stage must preserve. Child pages document
the dispatchers, opcode tables, and modifier vocabularies that implement
those contracts.

The useful model is:

```
MLIR llvm/nvvm dialect
    -> llvm::Module
    -> linked device-library module
    -> optimized LLVM module
    -> SelectionDAG and machine functions
    -> MCInst stream
    -> PTX assembly
```

Child pages document the detailed reverse-engineered subsystems. This
overview lays out the backend contracts that matter for users and
reimplementers.

## Backend Contract

| Stage | Responsibility | Public invariant |
|---|---|---|
| LLVM module handoff | Translate MLIR LLVM dialect to an `llvm::Module` and attach target triple, chip, features, and data layout. | The module is already ABI-ready; no high-level TileIR operations remain. |
| Device library linkage | Link embedded or external device bitcode used by math and NVVM helper calls. | Undefined device helper calls must be resolved before final codegen. |
| LLVM optimization | Run the LLVM optimization pipeline selected by the requested optimization level. | Optimizations preserve NVVM address spaces, kernel attributes, and libdevice semantics. |
| NVPTX target lowering | Lower calls, formal arguments, returns, intrinsics, address spaces, and custom target nodes. | Param-space values and kernel arguments are handled through NVPTX ABI rules, not generic pointer rules. |
| Instruction selection | Select custom NVPTX nodes first, then fall back to generated SelectionDAG matcher tables. | Feature-gated intrinsics are rejected or expanded before an illegal PTX instruction can be emitted. |
| Machine-function passes | Run target passes for argument lowering, image handles, scheduling, register allocation, and MIR cleanup. | Machine IR still carries enough target information for correct PTX emission. |
| PTX emission | Print PTX mnemonics, operands, modifiers, sections, directives, and target attributes. | Emitted PTX matches the resolved target feature set and is suitable for `ptxas`. |

## Target Initialization

The backend registers both 32-bit and 64-bit NVPTX targets, constructs
subtarget information from the target triple, CPU string, and feature string,
then builds or reuses a target machine for the compilation. The normal CUDA
device path is 64-bit and uses the `nvptx64-nvidia-cuda` triple.

Target initialization provides:

- target registry entries for `nvptx` and `nvptx64`;
- MC layer objects for registers, instruction descriptions, subtarget features,
  and asm output;
- an NVPTX target machine keyed by triple, chip, and feature set;
- a feature bitset used by target lowering and instruction selection.

The target feature set is the guardrail for newer instructions. Tensor memory,
TMA, WGMMA, tcgen05, block-scaled MMA, cluster operations, and related PTX
modifiers reach selection only when the subtarget says they are legal.

## LLVM Optimization

After translation and device-library linkage, the module goes through the LLVM
optimization pipeline selected by `O0`, `O1`, `O2`, `O3`, `Os`, or `Oz`. The
pipeline is the standard `PassBuilder` shape, but the analysis manager bank and
the per-level pipeline construction must be reused across functions so analysis
caches survive between passes:

```c
void optimize_llvm_module(LLVMModule module, TargetMachine tm,
                          OptLevel opt_level) {
    PassBuilder pb(tm);

    LoopAnalysisManager     lam;
    FunctionAnalysisManager fam;
    CGSCCAnalysisManager    cgam;
    ModuleAnalysisManager   mam;

    pb.register_module_analyses(mam);
    pb.register_cgscc_analyses(cgam);
    pb.register_function_analyses(fam);
    pb.register_loop_analyses(lam);
    pb.cross_register_proxies(lam, fam, cgam, mam);

    ModulePassManager mpm =
        (opt_level == OPT_NONE)
            ? pb.build_o0_default_pipeline(opt_level)
            : pb.build_per_module_default_pipeline(opt_level);

    mpm.run(module, mam);
}
```

NVVM-specific properties must survive ordinary LLVM optimization. Kernel
functions stay identifiable, NVVM intrinsics do not get rewritten into
target-illegal forms, address spaces stay distinct, and libdevice calls
retain the ABI the NVPTX backend expects.

## NVPTX ABI Lowering

NVPTX has a stricter ABI than ordinary LLVM IR suggests. Kernel parameters,
device function parameters, return values, `byval` aggregates, grid constants,
and parameter-space pointers each need explicit handling.

```c
void lower_nvptx_function(Function fn, TargetInfo target) {
    for (Argument arg : fn.arguments()) {
        if (fn.is_kernel()) {
            lower_kernel_argument_to_param_space(fn, arg, target);
        } else {
            lower_device_function_argument(fn, arg, target);
        }
    }

    for (CallInst call : fn.calls()) {
        lower_call_arguments(call, target);
        lower_call_return(call, target);
    }

    rewrite_address_space_casts(fn, target);
    lower_nvvm_intrinsics(fn, target);
}
```

The reimplementation rule is direct: do not treat param-space values as generic
pointers. Formal arguments, calls, returns, by-value aggregates, and grid
constants must pass through the NVPTX calling convention logic.

## Instruction Selection

Instruction selection is a two-layer process. Custom selectors handle NVPTX
intrinsics and target-specific operations that require validation or expansion.
The generated matcher table handles ordinary SelectionDAG nodes.

```c
void select_nvptx_dag(SelectionDAG dag, SubtargetFeatures features) {
    for (Node node : dag.nodes_in_selection_order()) {
        if (is_nvptx_custom_node(node)) {
            require(features.supports(node.required_feature()),
                    "target does not support requested NVPTX operation");
            select_custom_nvptx_node(node, features);
            continue;
        }

        select_with_generated_matcher_table(node, features);
    }
}
```

This division matters for correctness. TMA, tensor-memory, WGMMA, tcgen05,
special registers, vector memory operations, fences, barriers, and address-space
conversions need custom legality checks before the generated matcher can
safely produce an opcode.

## PTX Emission

PTX emission is TableGen-style instruction printing plus NVIDIA-specific
modifier helpers. The printer receives machine instructions and emits:

- opcode mnemonics and PTX type suffixes;
- rounding, saturation, cache, scope, fence, and memory-order modifiers;
- MMA/WGMMA/tcgen05 shape and layout modifiers;
- TMA coordinates, descriptor suffixes, multicast controls, and cache policy;
- section and scope comments used by the NVPTX asm printer;
- kernel directives such as register limits, cluster dimensions, and required
  thread dimensions.

The dense opcode printer and its modifier helpers are documented in
[asm-printer-monster-and-windows.md](asm-printer-monster-and-windows.md) and
[per-sm-emission-templates.md](per-sm-emission-templates.md). The overview only
needs the contract: PTX printing must be driven by the resolved target feature
set and by the opcode selected for that feature set.

```c
void print_ptx(Module *llvm, TargetInfo target, OutputStream *out) {
    emit_target_header(out, target.triple, target.chip, target.ptx_version);
    emit_address_size_directive(out, target.address_bits);

    for (GlobalVariable *gv : llvm->globals()) {
        emit_global_decl(out, gv, target);
    }

    for (Function *fn : llvm->functions()) {
        emit_function_directives(out, fn, target);   /* .entry / .func, regs, cluster, reqntid */
        for (BasicBlock *bb : fn->blocks()) {
            emit_label(out, bb);
            for (MachineInst *mi : bb->machine_insts()) {
                print_inst(out, mi, target.features);
            }
        }
        emit_function_end(out, fn);
    }
}
```

`print_inst` looks up the opcode in the per-SM opcode/mnemonic table, prints
the type suffix and modifier tokens in the order required by `ptxas`, and then
prints operands in the register-class vocabulary selected by the machine-IR
operand kinds.

## End-To-End Algorithm

```c
string emit_ptx_from_nvvm(ModuleOp mlir_module, CompileOptions options) {
    LLVMModule llvm = translate_mlir_llvm_to_llvm_ir(mlir_module);
    link_device_libraries(llvm, options);

    TargetInfo target = resolve_nvptx_target(options);
    TargetMachine tm = get_or_create_target_machine(target);

    optimize_llvm_module(llvm, tm, options.opt_level);

    for (Function fn : llvm.functions()) {
        lower_nvptx_function(fn, target);

        SelectionDAG dag = build_selection_dag(fn);
        select_nvptx_dag(dag, target.features);

        MachineFunction mf = build_machine_function(dag, fn);
        run_nvptx_machine_passes(mf, target);
    }

    return print_ptx(llvm, target);
}
```

## Codegen Invariants

- The module has exactly one resolved NVPTX target before backend emission.
- Kernel functions retain `nvvm.kernel` and launch metadata through LLVM
  optimization.
- Address spaces remain semantic: global, shared, constant, local, parameter,
  and tensor memory are not interchangeable.
- Param-space values are lowered through NVPTX ABI code, not generic pointer
  lowering.
- Custom intrinsic selection validates subtarget support before emission.
- Generated matcher-table selection remains the default path for ordinary DAG
  nodes.
- Vector memory selection preserves lane grouping and address-space
  classification.
- TMA, WGMMA, tcgen05, tensor memory, cluster, and block-scaled MMA operations
  are subtarget-gated.
- PTX emission prints the instruction selected for the target, not a generic
  approximation.

## Cross-Links

- [nvptx-bring-up-and-target-init.md](nvptx-bring-up-and-target-init.md) covers target registration and target-machine construction.
- [nvptx-target-lowering-call-and-args.md](nvptx-target-lowering-call-and-args.md) covers parameter, call, and custom-node lowering.
- [iseldag-and-matchertable.md](iseldag-and-matchertable.md) covers instruction selection.
- [per-sm-emission-templates.md](per-sm-emission-templates.md) covers SM-specific opcode families.
- [tma-tensormap-and-cp-async-bulk.md](tma-tensormap-and-cp-async-bulk.md) covers TMA and tensor-map emission.
- [tcgen05-wgmma-mbarrier-cluster.md](tcgen05-wgmma-mbarrier-cluster.md) covers tensor memory, WGMMA, barriers, and cluster features.
- [../libdevice/overview.md](../libdevice/overview.md) covers device-library linkage and libdevice behavior.
