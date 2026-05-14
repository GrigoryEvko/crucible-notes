# Lowering: nv_tileaa to nv_tileas

## Abstract

`ConvertTileAAToTileAS` lowers the alias-aware typed-pointer dialect `nv_tileaa` into the assembler-near dialect `nv_tileas`. It runs after [ConvertCudaTileToTileAA](cuda-tile-to-tileaa.md) and before the [TileAS family of passes](../scheduler/overview.md) (D07 through D22). Above this boundary tile algebra is target-independent and described in terms of typed pointers and abstract memory; below it, operations carry CopyAtom and ReduceAtom witnesses, the function's kernel-spec is mirrored as an attribute on the module, and SM100-only forms such as block-scaled MMA become legal.

Structurally this is a textbook MLIR partial conversion. A single driver assembles a `RewritePatternSet` from three fixed-order populators, attaches kernel-spec metadata onto the function, builds the conversion target, and runs `applyPartialConversion`. There is no second pipeline stage — canonicalization of slice scaffolding is left to the following passes.

## Pass Driver

`runOnOperation` populates three pattern groups in fixed order, attaches the kernel-spec attribute onto the function, constructs the conversion target, and applies it.

```c
LogicalResult convertTileAAToTileAS(ModuleOp mod) {
    RewritePatternSet patterns;
    populateArithPatterns(patterns);                    // 43-instantiation GenericOpPattern bank
    populateMathPatterns(patterns);                     // math.* → nv_tileas.* with arith fallback
    populateTileAACorePatterns(patterns);               // queue, execute, alias_token, memory ops

    attachKernelSpecAttributes(mod);                    // mirrors cute.kernel onto nv_tileaa.kernel_spec
    ConversionTarget target = buildConversionTarget(mod);

    if (failed(applyPartialConversion(mod, target, std::move(patterns)))) {
        return emit("failed to convert nv_tileaa to nv_tileas");
    }
    return success();
}

ConversionTarget buildConversionTarget(ModuleOp mod) {
    ConversionTarget target(*mod.getContext());

    target.addLegalDialect<nv_tileas::TileASDialect,
                           arith::ArithDialect,
                           math::MathDialect,
                           func::FuncDialect,
                           gpu::GPUDialect,
                           scf::SCFDialect>();
    target.addIllegalDialect<nv_tileaa::TileAADialect>();

    // nv_tileaa.func, nv_tileaa.return, and nv_tileaa.mark_for_reuse stay legal —
    // they are owned by ConvertTileFuncToLLVM, which has not yet run.
    target.addLegalOp<nv_tileaa::FuncOp,
                      nv_tileaa::ReturnOp,
                      nv_tileaa::MarkForReuseOp>();

    return target;
}
```

The arith populator runs first because the math populator falls back to arith for any non-NVPTX-specific operation. Both run before the nv_tileaa core populator so the core sees already-lowered subexpressions when it walks operand types during rewrite. The kernel-spec attachment runs before the partial-conversion driver because the SM100 block-scale guard reads compute capability through the attached attribute.

## Input and Output Dialects

| Direction | Surface |
|---|---|
| input ops | `nv_tileaa.*` (illegal after pass), `arith.*`, `math.*` |
| output ops | `nv_tileas.*` plus `arith.*` and `math.*` lowered to TileAS-form when the generic bank applies |
| attribute carriers | `CopyAtomAttrInterface` on memory ops, `ReduceAtomAttrInterface` on reduce / scan ops, `nv_tileaa.kernel_spec` on the function |

The shared rewrite shape for a memory op is:

```text
input  : %t = nv_tileaa.tiled_load %src, layout = #layout {copy_atom = #cute_nvgpu.copy_atom<...>}
output : %t = nv_tileas.tiled_load %src, layout = #layout {copy_atom = #cute_nvgpu.copy_atom<...>}
```

The witness attribute carries verbatim across the rewrite; the next stage ([TileAS to LLVM](tileas-to-llvm.md#tile-memory-and-descriptor-lowering)) picks the concrete hardware primitive (`cp.async`, `cp.async.bulk`, `tcgen05.cp`, `ldmatrix`, `stmatrix`) from it.

## Three Populators

| Populator | Size | Dialect family | Patterns |
|---|---:|---|---:|
| `sub_733EF0` | 12.6 KB | arith                  | ~30 (the `GenericOpPattern` bank documented in [the 43-instantiation arith bank](pattern-set-and-typeconverter.md#the-43-instantiation-arith-bank)) |
| `sub_730C50` | 13.1 KB | math                   | ~25 (`math.*` to `nv_tileas` equivalents) |
| `sub_72D810` | 13.0 KB | nv_tileaa core         | ~35 (queue, execute, alias_token, memory ops) |

Each populator is a flat sequence: allocate a 0x68-byte pattern object, fill its vtable and `OperationName`, push into the pattern vector. The pattern bodies themselves live in the named pattern bank described below; the populators only materialize them.

## Named Pattern Bank

Sixteen-plus `TileAAToTileAS*OpPattern` classes spanning `sub_72A1C0` through `sub_73C710` make up the dedicated patterns. Each is a 0x68-byte `OpConversionPattern` of the shape described in [Pattern Categories](pattern-set-and-typeconverter.md#pattern-categories): vtable pointer, interned `OperationName`, `PatternBenefit`, captured `TypeConverter*`, typeinfo-name string, and a small per-pattern tail. The vtables sit at consecutive offsets in `0x59B9000..0x59B9700`, one slot per pattern, with the standard eight-entry RewritePattern dispatch order (destructor, deleting destructor, `getRootKind`, root-kind init, `match`, `rewrite`, clone, move helper).

Pattern bodies known by their op names are the global / memref family (`nv_tileaa.global`, `get_global`, `make_memref`,
`block_tile`, `tiled_load`) at `sub_72A1C0`, the copy-atom load/store/atomic family (`load`, `store`, `tiled_load`,
`tiled_store`, `tiled_atomic_rmw`, `gather_load`, `scatter_store`) at `sub_7263C0..sub_728F50`, the
`extract_slice`/`convert_layout` rewriter at `sub_7297B0`, the `cat` rewriter at `sub_729D30`, the `plugin` rewriter at
`sub_7254B0`, the `generate` rewriter at `sub_738E70`, the `reduce` and `scan` rewriters at `sub_739A50` and `sub_739FE0`,
the `mark_for_reuse` verifier-style pattern at `sub_73C190`, and the SM100-gated `dot` lowering at `sub_72C180`. The copy
patterns each look up the `mlir::nv_tile_ir::as::CopyAtomAttrInterface` TypeID once via a double-checked init guarded by
`byte_5B38C18` and binary-search the op's attribute dictionary for the resolved CopyAtom witness; the reduce and scan
patterns do the same against `ReduceAtomAttrInterface` cached in `qword_5B38C00`. Selection of a concrete hardware
primitive (`cp.async`, `cp.async.bulk`, LDGSTS, TMA tile or im2col, `tcgen05.cp`, `ldmatrix`, `stmatrix`) happens later in
the TileAS materialization pipeline; the attachment point is here.

A handful of diagnostics from this layer outline the bank: `"TODO: only reg and smem layouts are supported at the moment"` from `sub_7297B0`, `"missing source layout"` and `"failed to infer source layout"` from `sub_729D30`, `"plugin has unsupported feature"` and `"fails to assign layout"` from `sub_7254B0`, `"failed to convert block signature"` from `sub_738E70`, and `"expect operands with queue types"` from `sub_73C190`.

## 137 realloc_insert Trampolines

137 byte-identical 343-byte trampolines fill `0x7000E0..0x70FC80`, one per push into the pattern vector. Each is a distinct instantiation of `std::vector<std::unique_ptr<RewritePattern>>::_M_realloc_insert`, byte-identical apart from the move-constructor vtable offset the inlined relocation loop calls for the unique_ptr's `Pattern::T` destructor. The count is 137 because the three populators add inserts at multiple `PatternBenefit` levels: only about 90 distinct pattern classes exist, but several get registered through more than one trampoline. The trampolines defer capacity growth to `sub_6E6530`, whose sole string is `"vector::_M_realloc_insert"`.

## SM100 MMA Block-Scale Guard

`sub_72C180` (2 970 B) wraps the `nv_tileaa.mma_block_scale` to `nv_tileas.block_scaled_mma` lowering with a target-spec check. The pattern reads the kernel-spec and target-spec from the module, asserts both are present (otherwise emits `"failed to get the target spec"`), runs the MMA shape validator at `sub_14B71C0`, then guards the block-scaled variant on compute capability:

```c
v82 = validate_mma_shape(...);                  // sub_14B71C0
v84 = get_compute_capability(target_spec);      // sub_152FDA0
if (is_block_scale_variant(v82) && cc_int(v84) <= 99)
    return emit("mma block scale is not supported by compute capability < sm100");
```

The integer encoding is `major * 10 + minor`, so the inclusive `<= 99` gate rejects every capability up to and including sm_89 and admits sm_90, sm_100, sm_103, sm_110, sm_120, and sm_121. The default compute capability baked into the pass constructor (`sub_738810`) is `"sm_80"`, which means the gate is closed on the default invocation — the pipeline driver must bump the capability through the `--compute-capability` option before the block-scale path becomes reachable. The same function then validates the MMA partition (`"failed to find available mma partition"`) and infers the 2D layout (`"failed to infer 2d layout"`) before building `nv_tileas.dot`. The atom-K and vector-size triple table the validator consults is documented in [MMA Atoms sm70-120 — Operand Contract by Tier](../dialects/cute_nvgpu/mma-atoms-sm70-120.md#operand-contract-by-tier).

## Kernel-Spec Attachment

`sub_72B8E0` walks the function looking for `cute.kernel` attributes emitted by `ConvertTileFuncToLLVM` and attaches mirroring `nv_tileaa.kernel_spec` attributes. The mirror lets downstream TileAS passes read kernel parameters such as `numWarps`, `clusterDim`, and occupancy directly from the operation's attribute dictionary, without traversing back to the LLVM-level function attributes. The reader interns the attribute name `"nv_tileaa.kernel_spec"` (length 21) once through the StringAttr getter and walks the op's attribute dictionary at offset +56. A close variant `sub_72BCD0` does the same work while also touching the SymbolTable trait. Both are read-only; writes to the kernel-spec attribute happen through the verifier in Strand C.

## Conversion Invariants

Executable `nv_tileaa` operations must not survive the pass — `applyPartialConversion` reports failure if any illegal-dialect operation remains. CopyAtom and ReduceAtom witnesses on `nv_tileaa` memory operations must be preserved exactly onto their `nv_tileas` replacements, because later passes use them to pick the concrete hardware primitive. The kernel-spec attribute must attach before the first pattern that reads compute capability runs, so the sm100 guard in `sub_72C180` has a non-null target spec to consult. Populator order has to stay arith, math, nv_tileaa core — both for the math-to-arith fallback and so the core populator's operand-type walks see already-lowered subexpressions.

## Cross-References

[Pattern Categories](pattern-set-and-typeconverter.md#pattern-categories) documents the dedicated `OpConversionPattern` layout and
[the 43-instantiation arith bank](pattern-set-and-typeconverter.md#the-43-instantiation-arith-bank) is shared with the arith populator. [Convert cuda_tile to TileAA](cuda-tile-to-tileaa.md) covers
the previous boundary that produces the `nv_tileaa` input this pass consumes. [TileAS to LLVM — Tile Memory and Descriptor Lowering](tileas-to-llvm.md#tile-memory-and-descriptor-lowering) is the
downstream materialization that resolves the CopyAtom and ReduceAtom witnesses attached here into concrete instructions.
[MMA Atoms sm70-120 — Operand Contract by Tier](../dialects/cute_nvgpu/mma-atoms-sm70-120.md#operand-contract-by-tier) lists the atom-K and vector-size triples consulted by
the SM100 block-scale validator.
