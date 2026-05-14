# GlobalValue Flag Bits

## Abstract

The `tileiras` binary inherits LLVM's 16-bit `GlobalValue` flag word. The word stores linkage, visibility, DLL storage class, thread-local mode, unnamed-address policy, and subclass data. NVIDIA reuses bit 14 as a fast marker for functions whose `nvvm.annotations` metadata has been transplanted into function attributes. This page covers the bit-level contract and the annotation kinds mirrored by the attribute infrastructure.

## GlobalValue 16-bit flag word — LLVM standard low 14 bits

The word lives in LLVM's `GlobalValue` flag field. Bits 0..13 follow upstream LLVM; bit 14 is NVIDIA-repurposed; bit 15 is reserved.

| Bits | Field | Notes |
|---|---|---|
| `0..3` | `LinkageTypes` | values 0..15; `InternalLinkage = 7`. Cleared on kernels, set to internal on non-kernels. |
| `4..5` | `VisibilityTypes` | `Default=0`, `Hidden=1`, `Protected=2`. Tested via `0x30` to gate the marker write. |
| `6..7` | `DLLStorageClass` | `Default / Import / Export`. Preserved through `0xFCC0`. |
| `8..9` | `ThreadLocalMode` | one of four TLS models. Preserved. |
| `10..11` | `UnnamedAddr` | `None / Local / Global`. Preserved. |
| `12` | `HasLLVMReservedName` | LLVM sentinel. Preserved. |
| `13` | subclass-data | `GlobalValue` subclass slot. Preserved. |
| `14` | **NVIDIA-repurposed** | See next section. |
| `15` | reserved | Always zero in observed paths. |

## Bit 14 — NVIDIA-repurposed `"nvvm.annotations_transplanted"` marker

Bit 14 is the NVIDIA-private `"nvvm.annotations_transplanted"` marker. It is set on defined kernel functions with default visibility. The same fact is also stored as a string-keyed function attribute, giving the backend a dual encoding — a fast bit check plus the structured attribute. `isKernelFunction` can short-circuit when the marker is set, skipping the fallback to legacy `!nvvm.annotations !"kernel"` metadata.

Consumers read the marker through a single bit test before falling back to attribute or metadata lookup:

```c
static bool is_kernel_fast(const Function *fn) {
    /* Bit 14 of the 16-bit GlobalValue flag word doubles as the
     * "nvvm.annotations_transplanted" cache. If it is set, the
     * attribute is guaranteed present and the !nvvm.annotations
     * fallback can be skipped. */
    return (fn->global_value_flags & (1u << 14)) != 0
        || function_has_attribute(fn, "nvvm.kernel");
}
```

The bit is only a cache; the source of truth remains the string attribute, which is what IR-text consumers see. Dropping the bit but keeping the attribute is correct (just slower); setting the bit but omitting the attribute breaks anything that round-trips IR through textual MLIR.

## Bit-mask decoded for IPMSP clone-stamping

The linkage pass rewrites the flag word with four hard-coded masks. Each one names exactly one logical operation on the field.

| Mask | Width | Preserved bits | Cleared bits | Operation | Resulting state |
|---|---|---|---|---|---|
| `0xFCC0` | 16 | `6, 7, 10..15` | `0..5, 8, 9` | `flags = (flags & 0xFCC0) \| 7` on non-kernels | `LinkageTypes = InternalLinkage`; DLLStorage, unnamed-addr, marker survive. |
| `0xF0` | 8 lo | `4..7` | `0..3` | `flags_lo &= 0xF0` on kernels | Clears the 4-bit linkage nibble; visibility + DLLStorage low half preserved. |
| `0x30` | 8 lo | — | — (test) | `(flags_lo & 0x30) != 0` on kernels | Tests visibility != `Default`. If non-zero, marker write is skipped. |
| `0x40` | 8 hi | — | — (OR) | `flags_hi \|= 0x40` on kernels with `Default` visibility | Sets bit 14, the `"nvvm.annotations_transplanted"` marker. |

The asymmetry between non-kernels (`0xFCC0` mask plus `InternalLinkage`) and kernels (`0xF0` low-byte mask plus visibility test plus marker bit) is deliberate. Non-kernels exit after one rewrite; kernels preserve externally visible linkage state but add the NVIDIA marker when visibility allows it.

## nvvm.annotations 10-kind catalog

Ten distinct kinds are encoded by the legacy `!nvvm.annotations` named metadata node and the parallel `"nvvm.<kind>"` function-attribute form. Bit 14 gates short-circuiting between the two via `isKernelFunction`.

| # | Legacy MDString | Attribute form | Format |
|---|---|---|---|
| 1 | `kernel` | `nvvm.kernel` | `i32 1` becomes an empty attribute. |
| 2 | `maxntid{x,y,z}` | `nvvm.maxntid` | Dim3 tuple serialized as `"X,Y,Z"`. |
| 3 | `reqntid{x,y,z}` | `nvvm.reqntid` | Dim3 tuple serialized as `"X,Y,Z"`. |
| 4 | `cluster_dim_{x,y,z}` | `nvvm.cluster_dim` | Dim3 tuple serialized as `"X,Y,Z"`. |
| 5 | `minctasm` | `nvvm.minctasm` | `i32` serialized as a decimal string. |
| 6 | `maxnreg` | `nvvm.maxnreg` | `i32` serialized as a decimal string. |
| 7 | `cluster_max_blocks` / `maxclusterrank` | `nvvm.maxclusterrank` | `i32` stored as an integer-valued attribute. |
| 8 | `nvvm.blocksareclusters` | `nvvm.blocksareclusters` | `i32 1` becomes an empty attribute. |
| 9 | `grid_constant` | `nvvm.grid_constant` | Variadic 1-based indices. |
| 10 | — | `nvvm.annotations_transplanted` | Empty attribute plus bit 14 marker. |

Kinds 2..4 are dim3 triples serialized as comma-joined strings. Kinds 5..6 are string-valued scalars; kind 7 is the lone integer-valued attribute, matching the CUDA cluster-attribute shape. Kind 8's legacy MDString already carries the `"nvvm."` prefix and passes through unchanged. Kind 9 (`grid_constant`) is the only kind the transplanter does **not** rewrite; it remains in the legacy node. Kind 10 has no legacy MDString and exists only as the dual-encoded marker.

## Reimplementation Notes

```text
for function in module.functions:
    if is_kernel(function):
        function.flags.linkage = external_kernel_linkage(function)
        if function.visibility == default:
            function.flags.nvvm_annotations_transplanted = true
            function.attrs["nvvm.annotations_transplanted"] = unit
    else:
        function.flags.linkage = internal
```

Keep the bit and the string attribute synchronized. Consumers may use the bit as a fast path, but tools that inspect IR text should still see the explicit `nvvm.annotations_transplanted` attribute.
