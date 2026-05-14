# Lowering: Target and Debug Info

## Abstract

Two module-level adapters prepare the lowered MLIR module for NVVM serialization. The target adapter turns Tileiras target metadata into the standard `#nvvm.target` attribute carried by `gpu.module`. The debug-info adapter turns Tileiras debug-value operations into LLVM debug intrinsics, inserting an NVIDIA-specific `llvm.nvvm.move` value pin so the PTX debug path can keep the value visible after optimization.

Both passes translate between internal TileIR metadata and the public LLVM/NVVM surface. A reimplementation doesn't need their original pass layout, but it must preserve the target-attribute fields, the libNVVM option dictionary, the debug intrinsic arguments, and the value-pinning step.

## Target Attribute Conversion

The target pass walks the module hunting for `gpu.module` operations. For each one, it reads the TileAA compute-capability attribute (falling back to the target-spec attribute when needed) and writes an NVVM target array attribute onto the GPU module.

The generated target uses:

| Field | Value |
|---|---|
| target triple | `nvptx64-nvidia-cuda` |
| chip | normalized `sm_XX` name derived from compute capability |
| optimization level | pass option, defaulting to the normal optimized path |
| feature string | empty unless a later target hook supplies features |
| link mode | non-linking module target |
| flag dictionary | libNVVM options and optional NVGpuComp selector |

The flag dictionary is small but consequential.

| Flag | When emitted | Purpose |
|---|---|---|
| `-g` | only when debug info is enabled for the module | asks the backend to preserve debug emission |
| `-Xopt` | always | opens the libNVVM option channel |
| `-pragma-unroll-threshold=9900000` | always | discourages backend re-rolling after Tileiras scheduling |
| `-fma=0` | always | prevents backend FMA contraction from changing explicit numeric choices |
| `libNVVMUseNVGpuComp=true` | only when the option is enabled | selects the NVGpuComp/libNVVM path downstream |

The target conversion algorithm is straightforward:

```c
LogicalResult attach_nvvm_target(ModuleOp module, TargetOptions options) {
    for (GpuModuleOp gpu_module : module.gpu_modules()) {
        ComputeCapability cc = read_compute_capability(gpu_module);
        if (!cc.valid()) {
            cc = read_target_spec_compute_capability(gpu_module);
        }
        if (!cc.valid()) {
            return gpu_module.emit_error("missing compute capability for NVVM target");
        }

        DictionaryAttr flags = build_libnvvm_flags(gpu_module, options);
        NVVMTargetAttr target = NVVMTargetAttr::get(
            module.context(),
            options.opt_level,
            "nvptx64-nvidia-cuda",
            cc.to_sm_name(),
            "",
            flags,
            /*link=*/false);

        gpu_module.set_attr("nvvm.target", ArrayAttr::get({target}));
    }

    return success();
}
```

## Debug-Info Conversion

Tileiras debug-info values carry source-variable metadata in an internal dialect. Before LLVM translation, each internal value must become LLVM dialect debug infrastructure. Each `debuginfo.value` becomes a short chain:

1. Materialize the element or lane selector as an LLVM constant.
2. Extract the debugged scalar from the original value when the source is aggregate-like.
3. Pass the scalar through `llvm.nvvm.move`.
4. Emit an LLVM debug intrinsic call with the local-variable and expression metadata.
5. Erase the original debug operation.

`llvm.nvvm.move` is the NVIDIA-specific part of the contract. It creates an ordinary SSA value that optimization is less likely to fold away before the backend emits DWARF location information.

```c
LogicalResult lower_debug_value(DebugValueOp op, Rewriter *rewriter) {
    Value source = materialize_debug_source(op.value(), op.fragment(), rewriter);
    Value pinned = rewriter->create("llvm.nvvm.move", source).result(0);

    DebugIntrinsic intrinsic = select_debug_intrinsic(op.kind());
    rewriter->create("llvm.call_intrinsic", {
        intrinsic.symbol_ref(),
        pinned,
        op.local_variable_attr(),
        op.expression_attr(),
        op.metadata_operands()
    });

    rewriter->erase_op(op);
    return success();
}
```

Aggregate values need recursive materialization. The converter walks vector and struct fields, converts each sub-value to an LLVM-compatible type, and emits extraction operations before the pin. If a referenced symbol or metadata node cannot be resolved yet, the lowering leaves a placeholder downstream LLVM translation can diagnose with the surrounding operation context.

## Type Conversion for Debug Values

The debug pass uses its own small type converter rather than the full TileAS lowering converter. Its job is to make debug operands legal without touching the executable ABI.

| Source debug type | LLVM debug operand form |
|---|---|
| integer scalar | same-width LLVM integer, restricted to backend-supported widths |
| half, bfloat16, tf32-like numeric extensions | LLVM numeric surrogate used by the value-lowering path |
| vector | per-lane extraction followed by scalar debug emission |
| struct or tuple | recursive field extraction and debug emission |
| unresolved aggregate member | placeholder plus diagnostic context |

Keep the debug converter conservative. Debug lowering must never invent executable computation that changes program behavior — it only exposes already-computed values to metadata.

## Error Handling

Both passes fail the module when required metadata is missing or when a target operation cannot be built because a dependent dialect was not loaded. The useful diagnostic names the missing semantic input:

- missing compute capability or target specification for `#nvvm.target`;
- unknown or unloaded LLVM/NVVM operation while building debug IR;
- unsupported debug value type;
- unresolved debug metadata that cannot be represented as an LLVM debug operand.

## Conversion Invariants

- Every serializable `gpu.module` must have a resolved NVVM target attribute.
- The target triple is the 64-bit CUDA NVPTX triple.
- The compute capability is normalized to the chip name consumed by NVVM.
- Debug emission must be gated by the same module-level debug option used to add `-g`.
- `llvm.nvvm.move` must sit between the debugged SSA value and the LLVM debug intrinsic.
- Debug conversion must not alter executable dataflow except for the value pin used by debug metadata.

## Reimplementation Checklist

1. Read TileAA compute capability and target-spec metadata from each GPU module.
2. Build the NVVM target attribute with triple, chip, optimization level, flags, and link mode.
3. Emit libNVVM flags deterministically, including the debug flag only when requested.
4. Convert internal debug-value operations into LLVM constants, extracts, `llvm.nvvm.move`, and debug intrinsics.
5. Preserve local-variable and expression metadata exactly.
6. Treat missing target metadata or unsupported debug operand types as pass failures.
