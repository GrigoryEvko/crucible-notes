# Lowering: Target and Debug Info

## Abstract

Two module-level adapters prepare the lowered MLIR module for NVVM serialization. `AttachNVVMTarget` turns Tileiras target metadata into the standard `#nvvm.target` attribute that the GPU-to-binary serializer reads off `gpu.module`. `TranslateDebugInfo` rewrites Tileiras debug-value operations into LLVM debug intrinsics, inserting an NVIDIA-specific `llvm.nvvm.move` value pin so the PTX backend can keep the debugged value visible across optimisation passes that would otherwise fold it away.

Both passes translate between internal TileIR metadata and the public LLVM/NVVM surface. A reimplementation does not need their original pass layout, but it must preserve the target-attribute fields, the libNVVM option dictionary, the debug intrinsic arguments, and the value-pin step.

## Target Attribute Conversion

The target pass walks `gpu.module` operations. For each one it reads the compute capability from the module's attribute dictionary, normalises it to an `sm_XX` chip name, builds the libNVVM flag dictionary, and writes the resulting `#nvvm.target` attribute as a single-element array onto the module.

### Attribute Sources

Three module-level attributes feed the target adapter, read in the order below.

| Attribute name | Type | Role |
|---|---|---|
| `nv_tileaa.compute_capability` | `IntegerAttr` (`major*10+minor`) | Primary source; emitted by `ConvertCudaTileToTileAA` from the `--compute-capability` option. |
| `nv_tileaa.target_spec` | `StringAttr` (`"sm_XX"` form) | Fallback when compute_capability is absent. |
| `nv_tileaa.libnvvm_use_nvgpucomp` | `BoolAttr` | Optional; selects the NVGpuComp/libNVVM serialisation path. |

When neither compute_capability nor target_spec resolves, the pass surfaces the verbatim `"failed to get compute capability."` diagnostic (with the trailing period) and fails the module; the closely related `"invalid or missing --compute-capability option"` is emitted by the option parser earlier in the pipeline when the CLI argument itself is absent.

### Generated Target Fields

The `#nvvm.target` attribute is a small record consumed by the upstream GPU-to-binary serializer. Field semantics:

| Field | Value | Source |
|---|---|---|
| target triple | `nvptx64-nvidia-cuda` | fixed |
| chip | normalised `sm_XX` chip name | `nv_tileaa.compute_capability` or `nv_tileaa.target_spec` |
| optimization level | `0..3` | pass option, defaulting to the optimised path |
| feature string | empty | reserved for later target hooks |
| link mode | `false` | non-linking module target |
| flag dictionary | libNVVM options below | composed per-module |

The flag dictionary is small but consequential. Each entry communicates one decision to the libNVVM backend.

| Flag | When emitted | Purpose |
|---|---|---|
| `-g` | only when debug info is enabled for the module | asks the backend to preserve debug emission |
| `-Xopt` | always | opens the libNVVM option channel |
| `-pragma-unroll-threshold=9900000` | always | discourages backend re-rolling after Tileiras scheduling |
| `-fma=0` | always | prevents backend FMA contraction from changing explicit numeric choices |
| `libNVVMUseNVGpuComp=true` | only when the option is enabled | selects the NVGpuComp/libNVVM path downstream |

### Consumer Passes

Once the target attribute attaches, three downstream consumers read it:

- The GPU-to-binary serializer reads triple, chip, optimisation level, and feature string to build the libNVVM/NVPTX command line.
- The PTX assembler stage reads chip to pick the SASS target.
- The `cluster_dim`/`reqntid` validators read chip to gate cluster-launch metadata on SM90 and above.

A module with `#nvvm.target` missing reaches the serializer with no target chip and fails serialisation with a "no target attribute" diagnostic before any binary is emitted.

### Conversion Algorithm

The pass body is small: walk gpu.modules, resolve compute capability, build flags, attach the attribute.

```c
LogicalResult attach_nvvm_target(ModuleOp module, TargetOptions options) {
    for (GpuModuleOp gpu_module : module.gpu_modules()) {
        ComputeCapability cc = read_compute_capability(gpu_module);
        if (!cc.valid()) {
            cc = read_target_spec_compute_capability(gpu_module);
        }
        if (!cc.valid()) {
            return gpu_module.emit_error("failed to get compute capability.");
        }

        DictionaryAttr flags = build_libnvvm_flags(gpu_module, options);
        NVVMTargetAttr target = NVVMTargetAttr::get(
            module.context(),
            options.opt_level,
            "nvptx64-nvidia-cuda",
            cc.to_sm_name(),
            /*features=*/"",
            flags,
            /*link=*/false);

        gpu_module.set_attr("nvvm.target", ArrayAttr::get({target}));
    }
    return success();
}
```

Idempotency matters: re-running the pass on a module that already carries `#nvvm.target` overwrites the attribute rather than appending a second target. Two targets on the same gpu.module produce undefined behaviour in the serializer.

## Debug-Info Conversion

Tileiras carries source-variable metadata in an internal `debuginfo.*` dialect. Before LLVM translation, those operations must become LLVM-dialect debug intrinsic calls (`llvm.intr.dbg.value`, `llvm.intr.dbg.declare`, `llvm.intr.dbg.addr`) whose operands the NVPTX backend can serialise into DWARF.

### MLIR Loc to LLVM !dbg

Every operation in Tileiras carries an MLIR `Location`. When debug info is enabled, the LLVM translation phase reads those locations and emits LLVM `!dbg` metadata that attaches to each lowered LLVM instruction. The mapping is direct:

| MLIR location | LLVM `!dbg` form |
|---|---|
| `FileLineColLoc(file, line, col)` | `DILocation(line, col, scope)` referencing the file's `DIFile` |
| `FusedLoc(child_locs, metadata)` | The metadata's `DILocation`, with `child_locs` becoming an inlined-at chain |
| `CallSiteLoc(callee_loc, caller_loc)` | `DILocation` for callee with `inlinedAt` pointing at caller's `DILocation` |
| `NameLoc(name, child)` | Passes through to `child`'s location; `name` becomes a `DILocalVariable` only at debug-value sites |
| `UnknownLoc` | No `!dbg` emitted; the LLVM instruction is untracked |

### Debug Scope Nesting for gpu.func

Each `gpu.func` participates in a `DISubprogram` scope. The translation builds the scope hierarchy bottom-up:

```
DICompileUnit (per module, attached to llvm.module)
  └── DIFile (per source file referenced)
       └── DISubprogram (per gpu.func, attached to the llvm.func)
            └── DILexicalBlock (per scf.if / scf.for / nested region)
                 └── DILocalVariable (per debuginfo.value)
```

Nested SCF regions get a fresh `DILexicalBlock` so debuggers can step into them without losing local-variable visibility from the parent. The lexical-block scope is parented to the surrounding subprogram, not to other lexical blocks — debuggers walk the inlining chain via `inlinedAt` rather than nested scopes.

### Lineinfo vs Device-Debug

The level of debug information depends on which compile option is active.

| Option | `!dbg` on instructions | `DILocalVariable` | `DISubprogram` | `dbg.value` intrinsics |
|---|---|---|---|---|
| `--lineinfo` off, `--device-debug` off | dropped | dropped | dropped | dropped |
| `--lineinfo` on | emitted | dropped | minimal (name + line only) | dropped |
| `--device-debug` on | emitted | emitted | full (with variables) | emitted with `llvm.nvvm.move` pins |

`--lineinfo` produces enough metadata for profilers to map SASS instructions back to source lines without paying the optimisation cost of tracking local variables. `--device-debug` adds local-variable tracking and is the only mode that keeps `dbg.value` intrinsics alive through the optimisation pipeline.

### debuginfo.value Rewrite Shape

The per-op rewrite turns each `debuginfo.value` into a debug intrinsic call. The NVIDIA-specific step is `llvm.nvvm.move`: an SSA pass-through value that constant-folding and dead-code elimination treat as opaque, so the debugged value stays visible to the backend even when the surrounding code is folded away.

```mlir
debuginfo.value %v, #var, #expr : !debuginfo.value<f32>
   ↓
%pinned = llvm.nvvm.move %v : f32
llvm.intr.dbg.value %pinned, !DILocalVariable(#var), !DIExpression(#expr)
```

For aggregate values, the rewriter walks vector and struct fields, extracts each leaf, pins it through `llvm.nvvm.move`, and emits a separate debug intrinsic per leaf. Aggregate fragments are described via `DIExpression(DW_OP_LLVM_fragment, offset, size)` so the debugger can reconstruct the original aggregate at display time.

```c
LogicalResult lower_debug_value(DebugValueOp op, Rewriter *rewriter) {
    Value source = materialize_debug_source(op.value(), op.fragment(), rewriter);
    Value pinned = rewriter->create("llvm.nvvm.move", source).result(0);

    DebugIntrinsic intrinsic = select_debug_intrinsic(op.kind());
    rewriter->create("llvm.intr." + intrinsic.name(), {
        pinned,
        op.local_variable_attr(),
        op.expression_attr()
    });
    rewriter->erase_op(op);
    return success();
}
```

If a referenced symbol or metadata node cannot be resolved yet, the rewriter emits a placeholder operand that the LLVM-translation phase diagnoses with the surrounding operation context. Failing here rather than at translation time gives a useful Tile-level location for the diagnostic.

### Type Conversion for Debug Operands

The debug pass uses its own small type converter rather than the full TileAS LLVM converter. Its job is to make debug operands legal without touching the executable ABI.

| Source debug type | LLVM debug operand form |
|---|---|
| integer scalar | same-width LLVM integer, restricted to backend-supported widths |
| half, bfloat16, tf32-like numeric extensions | LLVM numeric surrogate used by the value-lowering path |
| vector | per-lane extraction followed by scalar debug emission |
| struct or tuple | recursive field extraction and debug emission |
| unresolved aggregate member | placeholder plus diagnostic context |

The debug converter never invents executable computation. The only SSA values it introduces are `llvm.nvvm.move` pins and the `extractvalue`/`extractelement` operations needed to reach a debug leaf; everything else is metadata.

## Error Handling

Both passes fail the module with diagnostics that name the missing semantic input rather than the internal mechanism:

- missing compute capability or target specification for `#nvvm.target`;
- unknown or unloaded LLVM/NVVM operation while building debug IR;
- unsupported debug value type;
- unresolved debug metadata that cannot be represented as an LLVM debug operand.

## Conversion Invariants

- Every serializable `gpu.module` must have a resolved `#nvvm.target` attribute before serialisation.
- The target triple is the 64-bit CUDA NVPTX triple.
- The compute capability is normalised to the chip name consumed by NVVM.
- Debug emission is gated by the same module-level debug option used to add `-g`.
- `llvm.nvvm.move` must sit between the debugged SSA value and the LLVM debug intrinsic.
- Debug conversion must not alter executable dataflow except for the value pin used by debug metadata.

## Cross-References

[Conversion / Lowering Overview](overview.md#lowering-stages) places the target-attachment and debug-translation passes in their position at the tail of the pipeline. [TileAS to LLVM — Function Boundary Conversion](tileas-to-llvm.md#function-boundary-conversion) emits the `gpu.module` and `nvvm.kernel` attributes those passes consume. [NVPTX Subtarget and Feature Matrix — The 40 CPU Rows](../codegen/nvptx-subtarget-and-feature-matrix.md#the-40-cpu-rows) lists the chip names the compute-capability normaliser produces. [Debugging and Introspection](../topics/debugging-and-introspection.md) is the user-facing guide that frames `--lineinfo` and `--device-debug` against the other four debugging surfaces and documents when to pick each one.

