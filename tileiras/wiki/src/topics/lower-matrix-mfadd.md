# LowerMatrix + mfadd

## Abstract

Tileiras includes LLVM's target-independent `LowerMatrixIntrinsics` pass, which handles `@llvm.matrix.*` intrinsics: it verifies matrix shapes when requested, performs the transpose peephole `(transpose A) + (transpose B) -> transpose(A + B)`, gathers pass statistics, and lowers remaining matrix intrinsics into ordinary scalar and vector IR.

The correction worth flagging: `mfadd` and `mfadd_t` are not NVIDIA instructions, NVPTX opcodes, or private intrinsics. They are SSA value-name prefixes created by the upstream LLVM matrix pass while rewriting transposed additions. CUDA tensor-core paths such as WMMA, WGMMA, and `tcgen05.mma` use the NVVM intrinsic family and NVPTX instruction selection — they never go through this generic matrix-lowering pass.

This page exists because the `mfadd` string surfaces in the binary's rodata between strings that *do* belong to NVIDIA-private tensor-core paths, and an unwary cross-reference would wire it to the wrong subsystem. The appearance of `mfadd` and `mfadd_t` in the binary is proof that the upstream `LowerMatrixIntrinsics` pass is linked in, not proof of a custom WMMA path. For the actual NVIDIA tensor-core lowering see the WMMA / WGMMA / `tcgen05.mma` sections under codegen — nothing on this page applies.

## Attribution Correction

An earlier working note treated `"mfadd"`, `"mfadd_t"`, and the diagnostic `"Matrix shape verification failed, compilation aborted!"` as NVIDIA-internal additions. That attribution is wrong. Those names and diagnostics belong to upstream LLVM's `LowerMatrixIntrinsics.cpp`.

The mistake matters for documentation and reimplementation. A tileiras-compatible frontend needs no NVIDIA-specific "mfadd" operation — only the normal upstream matrix pass behavior when `@llvm.matrix.*` intrinsics are present, with CUDA tensor-core lowering routed through the NVVM/NVPTX intrinsic path.

## Provenance

The pass body in `tileiras` is pinned to three public `llvm/llvm-project` commits authored by Florian Hahn (`fhahn@apple.com`, Apple). Each commit landed before the upstream snapshot that `cicc` statically links. The table records the verbatim hashes, dates, and the strings each commit introduced.

| Commit | Date | Subject | String(s) introduced |
| --- | --- | --- | --- |
| `da09b35334ab` | 2022-11-28 | [Matrix] Optimize matrix transposes around additions | `"mfadd"`, `"mfadd_t"` (Aᵀ+Bᵀ → (A+B)ᵀ rewrite) |
| `f10153fe9150` | 2023-04-21 | [Matrix] Handle integer types when distributing transposes across adds | extends the same rewrite to integer FAdd/Add |
| `0e8717f71198` | 2023-05-13 | [Matrix] Add shape verification | `verify-matrix-shapes` `cl::opt`, `"Conflicting shapes ("`, `") for "`, `"Matrix shape verification failed, compilation aborted!"` |

The first commit added the peephole that splits a pair of transposed operands feeding an FAdd into one FAdd named `"mfadd"` and one outer transpose named `"mfadd_t"`. The second commit broadened the same rewrite to handle integer Add as well as FAdd, so the same `"mfadd"` / `"mfadd_t"` value names are reused on the integer path. The third commit added the `VerifyShapeInfo` debug pass and its `report_fatal_error` diagnostic, gated by the hidden `cl::opt` named `verify-matrix-shapes` that defaults off in upstream. The cicc snapshot post-dates all three commits, so the strings reach the binary by direct inclusion of the upstream pass, not by patch.

## mfadd identity

`"mfadd"` is not an NVPTX instruction mnemonic, not an LLVM intrinsic ID, and not a target opcode of any kind. It is a literal `llvm::Twine` `Name` argument passed to `IRBuilder::CreateFAdd` (and on the integer path to `IRBuilder::CreateAdd`) inside the pass's `OptimizeTransposes` sweep. LLVM uses the `Twine` to build the SSA value name of the new `Instruction`, so the rewritten IR carries the prefix `%mfadd` on the fused FAdd and `%mfadd_t` on the outer `@llvm.matrix.transpose` call. Both are ordinary IR values; the pass then re-runs its own lowering on the freshly minted transpose, converting it into the column-by-column scalar/vector form (`col.load` / `vec.start` / `vec.gep`). The transform is a pure target-independent IR peephole: `(transpose A) + (transpose B)` becomes `transpose(A + B)`, halving the number of materialized transposes when both operands of an FAdd are transposed views of equally shaped matrices.

## Pass Pipeline

Read the implementation as five semantic phases, not as a collection of binary entry points:

| Phase | Role | User-visible artifacts |
| --- | --- | --- |
| Shape verification | Walk the shape map and reject incompatible matrix dimensions when enabled. | `verify-matrix-shapes`, `Conflicting shapes`, fatal shape-verification diagnostic |
| Transpose optimization | Rewrite `(A^T + B^T)` into `(A + B)^T` for compatible shapes. | `mfadd`, `mfadd_t`, `NumExposedTransposes` |
| Pass setup and accounting | Register and update matrix-lowering statistics. | `matrix-lowered`, `NumStores`, `NumLoads`, `NumComputeOps`, `NumFPOps` |
| Top-level driver | Sequence verification, transpose optimization, optional dumps, and final lowering. | `matrix-print-after-transpose-opt` |
| Column-major lowering | Replace matrix intrinsics with loads, GEPs, shuffles, and arithmetic. | `col.load`, `vec.start`, `vec.gep`, `result.vec.` |

No matrix-shaped intrinsic remains by the time control returns to the pass manager. NVPTX instruction selection sees ordinary IR: loads, address arithmetic, `fmul`/`fadd` or integer arithmetic, vector shuffles, and stores.

## Reimplementation Notes

A compatible reimplementation can follow the upstream algorithm directly:

- Treat `mfadd` and `mfadd_t` only as optional IR names.
- Run shape verification only when the verification option is enabled.
- Preserve the transpose-add peephole before final lowering.
- Lower all remaining matrix intrinsics into target-independent scalar/vector IR.
- Keep CUDA tensor-core lowering on the NVVM intrinsic path, not on `@llvm.matrix.*`.

```c
bool run_lower_matrix_intrinsics(Function *function, MatrixOptions options) {
    ShapeMap shapes = collect_matrix_shapes(function);

    if (options.verify_shapes) {
        verify_matrix_shapes_or_die(function, &shapes);
    }

    bool changed = optimize_transposed_adds(function, &shapes);

    if (options.print_after_transpose_opt) {
        dump_function(function);
    }

    changed |= lower_matrix_intrinsics_to_vectors(function, &shapes, options);
    return changed;
}
```
