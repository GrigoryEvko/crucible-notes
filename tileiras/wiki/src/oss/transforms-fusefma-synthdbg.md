# Transforms / FuseFMA / SynthDebugInfo

## Abstract

None of the three OSS `cuda-tile` transform previews ships in tileiras in its original
cuda-tile-dialect form. `FuseFMA.cpp` is superseded by lower compiler layers. `LoopSplit.cpp` is
absent without a TileIR-equivalent replacement. `SynthesizeDebugInfoScopes.cpp` is replaced by
upstream MLIR's LLVM debug-scope pass, with only shared location helper behavior surviving.

That means a reimplementation should not blindly copy the public `Transforms/` directory into the
tileiras pipeline. The public files are useful for understanding the historical `cuda_tile` tool,
but the released compiler routes FMA, loop shaping, and debug-scope synthesis elsewhere.

## FuseFMA

The OSS pass rewrites `mulf`/`addf` and `mulf`/`subf` patterns into `cuda_tile.fma` when rounding
modes and modifiers agree. Tileiras keeps the `cuda_tile.fma` operation, but not the pass that
searches for these patterns at the `cuda_tile` layer.

FMA formation is delegated to lower layers:

- `tileas-legalize-fma-dot` handles TileAS-level MMA accumulator contraction.
- `-nvptx-fma-level` controls scalar FMA formation after lowering to LLVM/NVPTX IR.
- `-enable-fma-to-ffma2` covers the backend's F2 fused variant.

This is a semantic decision. Fusing `(a * b) + c` changes double-rounding into a single-rounded FMA,
so tileiras places the scalar decision under the same backend policy that `nvcc --fmad` controls.

## LoopSplit

The OSS `LoopSplit.cpp` pass walks `cuda_tile.for` loops and splits a loop when an inner
`cuda_tile.if` predicate flips at a loop-invariant boundary. Tileiras does not ship that pass and
does not provide an equivalent TileIR or TileAS pass.

The nearest named relative is loop unrolling, not loop splitting. Schedule materialization can
decompose some guarded loop structure earlier in the pipeline, but it is not the same
predicate-based loop-split transform. A compatible clone should not add OSS `LoopSplit` unless it
is intentionally adding functionality beyond tileiras.

## Debug Scope Synthesis

The OSS `SynthesizeDebugInfoScopes.cpp` pass is replaced by upstream MLIR's LLVM function-scope
debug pass. The replacement pass is anchored on `builtin.module`, requires the LLVM dialect, emits
compile units with producer `"MLIR"`, and supports the standard `emission-kind` enum:

| Value | Emission kind |
| ---:| --- |
| `0` | `None` |
| `1` | `Full` |
| `2` | `LineTablesOnly` |
| `3` | `DebugDirectivesOnly` |

The important behavioral difference is where locations are attached. The OSS pass rewrites per-op
locations to `DILocAttr`. Tileiras leaves that work for the later `ConvertDebugInfoToLLVM` path,
which consumes `debuginfo.value` operations after LLVM-dialect lowering. The scope pass itself
walks LLVM functions and attaches function-level `DISubprogramAttr` information.

## Delta Summary

| OSS transform | Tileiras behavior | Compatibility decision |
| --- | --- | --- |
| `FuseFMA.cpp` | Not present as a cuda-tile pass; superseded by TileAS and NVPTX backend policy. | Do not register OSS `fuse-fma` in the tileiras-compatible pipeline. |
| `LoopSplit.cpp` | Not present; no equivalent TileIR/TileAS split pass. | Do not add a loop-split substitute for compatibility. |
| `SynthesizeDebugInfoScopes.cpp` | Replaced by upstream LLVM function debug-scope pass. | Use `DIScopeForLLVMFuncOp` and leave per-op location lowering downstream. |

## Reimplementation Notes

```c
void configure_tileiras_transform_pipeline(Pipeline *pipeline, OptLevel opt_level) {
    add_tileas_legalize_fma_dot(pipeline);
    set_nvptx_fma_level(pipeline, 2);

    if (opt_level == OPT_O3) {
        add_di_scope_for_llvm_func_op(pipeline, DEBUG_DIRECTIVES_ONLY);
    } else {
        add_di_scope_for_llvm_func_op(pipeline, LINE_TABLES_ONLY);
    }

    add_convert_debug_info_to_llvm(pipeline);
}
```

The key omission is intentional: do not add cuda-tile `FuseFMA`, cuda-tile `LoopSplit`, or the
cuda-tile-specific debug-scope pass when targeting tileiras behavior.
