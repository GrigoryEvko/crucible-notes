# LLVM PassBuilder Registry

## Abstract

Tileiras embeds an LLVM `PassBuilder` registry that resolves textual pass names to factory callbacks. It is the mechanism by which strings inside `--pass-pipeline="..."` arguments turn into pass instances, and it is the same mechanism the driver uses when the inner serialization stage builds its LLVM optimization pipeline from a named `OptimizationLevel`. The registry holds 551 entries: 478 reached through templated `getTypeName<T>()` keys derived from C++ pass class names, 66 reached through bare string keys for passes that opt out of the templated path, 5 pipeline aliases that expand to multi-pass sequences, and 2 specials for analysis printing and verification. The registrar populates the global `StringMap` at static-init time before any compilation begins, so the table is read-only during compile and the textual resolver runs as a single hash lookup.

The registry is a menu, not a schedule. An entry being present means a user could ask for the pass by name; it does not mean the default Tileiras pipeline runs that pass.

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
| `check-kernel-functions` | Module | Normalize kernel and non-kernel function linkage (see [Kernel/CDP/Inline/Pretreat — Kernel Identity](../nvptx-passes/kernel-cdp-inline-pretreat.md#kernel-identity)). |
| `cnp-launch-check` | Module | Validate CUDA dynamic-parallelism launch calls (see [Kernel/CDP/Inline/Pretreat — CDP Launch Expansion](../nvptx-passes/kernel-cdp-inline-pretreat.md#cdp-launch-expansion)). |
| `ipmsp` | Module | Specialize generic-pointer callees by memory space. |
| `nv-early-inliner` | Module | Run an NVIDIA-tuned early inliner (see [Kernel/CDP/Inline/Pretreat](../nvptx-passes/kernel-cdp-inline-pretreat.md)). |
| `nv-inline-must` | Module | Force-inline functions whose ABI cannot survive as calls (see [Kernel/CDP/Inline/Pretreat](../nvptx-passes/kernel-cdp-inline-pretreat.md)). |
| `nvvm-pretreat` | Module | Canonicalize raw NVVM IR before verification and optimization (see [Kernel/CDP/Inline/Pretreat](../nvptx-passes/kernel-cdp-inline-pretreat.md)). |
| `nvvm-verify` | Module | Check NVVM kernel launches and parameter-space usage (see [NVVM IR Verifier](../nvptx-passes/nvvm-ir-verifier.md)). |
| `printf-lowering` | Module | Lower device `printf` to the `vprintf` ABI (see [printf Lowering and vprintf](../nvptx-passes/printf-lowering-and-vprintf.md)). |
| `select-kernels` | Module | Restrict processing to selected kernels for diagnostics/testing. |
| `nvvm-aa` | Function analysis | Provide address-space-aware alias information. |
| `kernel-info` | Function | Emit per-kernel diagnostic metrics. |
| `nvvm-peephole-optimizer` | Function | Simplify NVVM IR and address arithmetic before selection (see [Peephole MIR and Image Handles](../nvptx-passes/peephole-mir-and-image-handles.md)). |
| `propagate-alignment` | Function | Propagate alignment facts through memory operations. |
| `reuse-local-memory` | Function | Reuse non-overlapping local-memory slots. |
| `memory-space-opt` | Function | Infer and rewrite concrete address spaces (see [Memory-Space-Opt and Process-Restrict](../nvptx-passes/memory-space-opt-and-process-restrict.md)). |
| `lower-aggr-copies` | Function | Expand unsupported aggregate memory intrinsics (see [lower-args, lower-aggr-copies, lower-struct-args](../nvptx-passes/lower-args-and-aggr-and-struct.md)). |
| `lower-struct-args` | Function | Lower by-value struct kernel parameters (see [lower-args, lower-aggr-copies, lower-struct-args](../nvptx-passes/lower-args-and-aggr-and-struct.md)). |
| `process-restrict` | Function | Materialize `__restrict__` alias metadata (see [Memory-Space-Opt and Process-Restrict](../nvptx-passes/memory-space-opt-and-process-restrict.md)). |

The same registry also exposes stock LLVM names such as `default`, `thinlto`, `lto`, `verify`,
`inline`, `function-simplification`, and machine-pipeline passes like `greedy`, `regallocfast`,
`machine-scheduler`, and `virt-reg-rewriter`. Those names are useful for textual LLVM pipeline
experiments, but Tileiras' normal MLIR pipeline reaches the NVPTX backend through its own target
handoff rather than through an arbitrary user-supplied LLVM text pipeline.

## Registry Depth

The registry breaks down into five disjoint groups. The 478 templated entries dominate; they exist because LLVM's pass framework derives a string from the C++ class name through `getTypeName<T>()` and registers the factory under that string. The 66 naked-class entries are passes whose registered name is hand-written (usually because the templated name would be unwieldy or would collide with a stock LLVM pass). The 5 aliases are short names that expand to multi-pass sequences. The 2 specials wire up the analysis-print and verify infrastructure that LLVM's pass parser depends on.

| Group | Count | Examples | Why this group |
|---|---|---|---|
| `getTypeName<T>()` keys | 478 | `InstCombinePass`, `LICMPass`, `MachineCSEPass` | Default registration; key derived from C++ type name. |
| Naked-class string keys | 66 | `printf-lowering`, `nvvm-pretreat`, `select-kernels` | Manually named NVIDIA passes plus a handful of upstream passes that opt out of the templated key. |
| Pipeline aliases | 5 | `default<O2>`, `thinlto<O3>`, `lto<O2>` | Expand to multi-pass strings inside the parser. |
| Specials | 2 | `print<analysis>`, `verify` | Wire up the analysis-print and verify infrastructure. |
| **Total** | **551** | | |

The 478 + 66 + 5 + 2 = 551 sum is the registry's full depth. There is no overflow path; an unknown name is a parser error, not a fallback to a generic factory.

## Static-Init Registrar

The registrar is a single function that calls `PassBuilder::registerPipelineParsingCallback` once per entry. It runs at static initialization time because each `RegisterPass<T>` global constructor inserts into the same `StringMap`. The map's load factor and bucket count are sized at first use; the registrar uses `StringMap::insert` which is O(1) amortized. After static-init the map is never mutated.

```c
void register_passbuilder_entries(PassBuilder &pb) {
    register_templated_module_passes(pb);     // contributes to the 478
    register_templated_function_passes(pb);   // contributes to the 478
    register_templated_loop_passes(pb);       // contributes to the 478
    register_templated_machine_passes(pb);    // contributes to the 478

    register_named_nvvm_passes(pb);           // contributes to the 66
    register_named_nvptx_passes(pb);          // contributes to the 66

    register_pipeline_aliases(pb);            // the 5
    register_print_and_verify(pb);            // the 2
}
```

The split between templated and named registration is what allows NVIDIA-private passes to be interleaved with upstream LLVM passes in the same registry: upstream passes register under their templated keys; NVIDIA passes register under hand-chosen names from the `nv-*`, `nvvm-*`, `cnp-*`, `check-*`, and `lower-*` families.

## Textual Resolution

The parser resolves pass names in the context of the current pipeline level. The same string can appear at module, CGSCC, function, loop, and machine-function levels without collision because the parser only consults the registry slice for the manager it is currently constructing.

```c
Expected<PassPlugin> parse_pass(PipelineLevel level, StringRef text) {
    auto [name, options] = split_name_and_options(text);  // "pass{key=value,...}"

    auto *info = lookup_registry_for_level(level, name);
    if (!info) {
        return make_error<StringError>(
            "unknown pass '" + name + "' at " + describe_level(level));
    }

    PassOptions parsed_opts;
    if (failed(parse_option_block(options, info->schema, parsed_opts))) {
        return make_error<StringError>(
            "invalid option block for pass '" + name + "'");
    }
    return info->construct(parsed_opts);
}
```

Name matching is case-sensitive and exact on the name portion. The optional `{key=value,...}` block is parsed against the per-pass schema after the lookup succeeds; an unrecognized option key is rejected rather than silently ignored.

## Relationship to TileIR Passes

TileIR MLIR passes are scheduled by the Tileiras pipeline builder against MLIR's own registry, which is independent of this LLVM `PassBuilder` registry. The LLVM registry is consulted only after the inner pipeline reaches LLVM/NVVM IR or when a user supplies a textual LLVM pipeline through `--passes=` to the embedded NVPTX backend. A pass appearing here is not scheduled by Tileiras's default flow unless `populate_pipeline` or `run_llvm_passbuilder_pipeline` references it explicitly.

## Cross-References

[Compilation Pipeline Overview — Serialization Boundary](overview.md#serialization-boundary) describes where in the outer/inner split this registry is consulted. [Pipeline Options Mapping](options-mapping.md) names the options that the inner LLVM pipeline reads through the registry's factories. [Pass List by Optimization Level — Handoff to LLVM/NVPTX](full-pass-list-by-opt-level.md#handoff-to-llvmnvptx) is the MLIR-tier pass list that runs before this registry comes into play. [NVPTX Backend Passes overview](../nvptx-passes/overview.md) is where the NVIDIA-specific entries above are described pass by pass.
