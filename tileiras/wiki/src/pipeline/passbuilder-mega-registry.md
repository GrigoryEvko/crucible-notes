# LLVM PassBuilder Registry

## Abstract

Tileiras embeds an LLVM PassBuilder registry so textual LLVM pipelines can resolve analyses and
passes by name. Most entries are stock LLVM module, CGSCC, function, loop, and machine-function
passes. A smaller NVIDIA-specific cluster registers NVVM and NVPTX preparation passes such as
kernel-linkage normalization, launch checking, memory-space propagation, printf lowering, and
aggregate-copy lowering.

This registry is a menu. It does not mean every pass runs in the default Tileiras pipeline.

## Registry Families

| Family | Examples | Role |
| --- | --- | --- |
| Module analyses | call graph, profile summary, verifier analysis | Query module-wide facts. |
| Module transforms | inlining, internalization, global optimization | Rewrite whole modules. |
| CGSCC passes | inliner and call-graph transforms | Optimize call-graph components. |
| Function analyses | alias analysis, dominators, loops, scalar evolution | Query function-local facts. |
| Function transforms | instcombine, GVN, vectorization, NVVM cleanup | Rewrite LLVM IR functions. |
| Loop passes | LICM, rotate, unswitch, unroll | Rewrite loops. |
| Machine passes | register allocation, scheduling, MIR cleanup | Rewrite MachineIR. |

## NVIDIA-Specific Entries

| Pass name | Stage | Purpose |
| --- | --- | --- |
| `check-gep-index` | Module | Validate constant GEP indices after frontend cleanup. |
| `check-kernel-functions` | Module | Normalize kernel and non-kernel function linkage. |
| `cnp-launch-check` | Module | Validate CUDA dynamic-parallelism launch calls. |
| `ipmsp` | Module | Specialize generic-pointer callees by memory space. |
| `nv-early-inliner` | Module | Run an NVIDIA-tuned early inliner. |
| `nv-inline-must` | Module | Force-inline functions whose ABI cannot survive as calls. |
| `nvvm-pretreat` | Module | Canonicalize raw NVVM IR before verification and optimization. |
| `nvvm-verify` | Module | Check NVVM kernel launches and parameter-space usage. |
| `printf-lowering` | Module | Lower device `printf` to the `vprintf` ABI. |
| `select-kernels` | Module | Restrict processing to selected kernels for diagnostics/testing. |
| `nvvm-aa` | Function analysis | Provide address-space-aware alias information. |
| `kernel-info` | Function | Emit per-kernel diagnostic metrics. |
| `nvvm-peephole-optimizer` | Function | Simplify NVVM IR and address arithmetic before selection. |
| `propagate-alignment` | Function | Propagate alignment facts through memory operations. |
| `reuse-local-memory` | Function | Reuse non-overlapping local-memory slots. |
| `memory-space-opt` | Function | Infer and rewrite concrete address spaces. |
| `lower-aggr-copies` | Function | Expand unsupported aggregate memory intrinsics. |
| `lower-struct-args` | Function | Lower by-value struct kernel parameters. |
| `process-restrict` | Function | Materialize `__restrict__` alias metadata. |

The same registry also exposes stock LLVM names such as `default`, `thinlto`, `lto`, `verify`,
`inline`, `function-simplification`, and machine-pipeline passes like `greedy`, `regallocfast`,
`machine-scheduler`, and `virt-reg-rewriter`. Those names are useful for textual LLVM pipeline
experiments, but Tileiras' normal MLIR pipeline reaches the NVPTX backend through its own target
handoff rather than through an arbitrary user-supplied LLVM text pipeline.

## Textual Pipeline Resolution

Pass names are resolved in the context of the current pipeline level. A name such as `verify` can
exist at module, function, loop, and machine-function levels without colliding because the parser knows
which pass manager it is building.

```c
Pass *parse_pass(PipelineLevel level, StringRef name, PipelineOptions options) {
    PassInfo *info = pass_registry_lookup(level, name);

    if (info == NULL) {
        error("unknown pass in pipeline");
    }

    return info->construct(options);
}
```

This is why a registry entry should be documented with its IR unit: module, CGSCC, function, loop, or
machine function.

## Relationship to TileIR Passes

TileIR MLIR passes are scheduled by the Tileiras pipeline builder. LLVM PassBuilder entries are used
after lowering reaches LLVM/NVVM IR or when a textual LLVM pipeline is parsed. Do not expect a pass
listed here to run merely because it is registered.

## Reimplementation Checklist

1. Register passes by IR unit, not in one flat namespace.
2. Keep textual names stable for user-supplied pipelines.
3. Distinguish registry availability from default scheduling.
4. Document NVIDIA-specific LLVM passes separately from MLIR TileIR passes.
5. Reuse upstream LLVM pass names where behavior is unchanged.
6. Give target-specific passes explicit NVVM/NVPTX names.
7. Report unknown textual pass names with the pipeline level.
8. Keep registry setup independent of per-invocation pipeline construction.
