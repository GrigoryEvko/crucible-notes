# MHLO → XTile → tpu Lowering

> *All addresses, symbols, and op-name strings on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`, build `libtpu_lts_20260413_b_RC00`). Other versions will differ.*

## Abstract

This page documents the `MHLO/StableHLO → XTile → tpu` lowering as it actually exists in the binary — and the most important fact about it is structural and corrective. The phrase "MHLO → XTile → tpu" describes **two different lowering trees that share `libtpu.so` but never connect**:

- **`MHLO/StableHLO → XTile` is real.** XTile (`xla::xtile::XTileDialect`) is a tiled-tensor MLIR dialect with seven registered ops, a four-pass front-end lowering, and a verbatim StableHLO→`arith` pattern table. It lives under `third_party/.../xla/codegen/xtile/` and lowers to the **CPU/LLVM** stack via a `xtile-cpu-*` pipeline.
- **`XTile → tpu` is not real.** No pass produces the `tpu` dialect from XTile. The binary contains **zero** `*ToTpuPass` / `MhloToTpu` / `StablehloToTpu` conversion-pass functions. XTile's dependent-dialect set is the CPU/LLVM set (`xla::cpu::XlaCpuDialect`, `memref`, `vector`, `LLVM`) — `tpu`, `llo`, and `sparse_core` appear nowhere in any XTile pass.

The `tpu` dialect that the [compiler overview](overview.md) names as Level 2 is therefore **not** produced by lowering general MHLO. On the TPU device path, general HLO is emitted straight to LLO by ~3225 `xla::jellyfish::*Emitter` classes; the `tpu` dialect is only ever *imported* — authored by the Pallas/Mosaic frontend, embedded in an HLO `kCustomCall("tpu_custom_call")`, and extracted by `xla::jellyfish::mlir_utils::GetMlirModuleOpFromCustomCall`. This page documents Tree B (XTile, in full) and pins down where Tree A's `tpu` dialect actually originates.

> **CORRECTION — this page supersedes the "XTile sits between MHLO and `tpu`" claim.** The [overview](overview.md) IR-stack diagram and the dialect inventory describe XTile as an MHLO→`tpu` mid-level. The symbol, string, and dependent-dialect evidence in *this* binary contradicts the XTile→`tpu` link (see [The Two-Tree Picture](#the-two-tree-picture)). Read this page's tree split as the authoritative refinement of the overview's Level 2.

For reimplementation, the contract this page fixes is:

- **The two trees and their boundary.** Tree A (TPU device: HLO → jellyfish `*Emitter` → LLO; `tpu` only via Mosaic custom-call import) versus Tree B (XLA CPU/GPU codegen: StableHLO/MHLO → XTile → memref/vector → LLVM). Why "XTile → `tpu`" has no pass.
- **The four XTile front-end passes** — `stablehlo-lower-to-xtile`, `stablehlo-lower-to-arith`, `convert-elementwise-0d-tensor-to-scalar`, `verify-legal-xtile-ops` — their CLI names, create-functions, dependent dialects, and ordering inside the `xtile-cpu-*` pipeline.
- **The seven registered XTile ops** with recovered `build()` signatures, plus the two attributes (`xtile.layout`, `xtile.tiling_info`) and `TiledBufferInterface`.
- **The per-op MHLO/StableHLO → XTile mapping** — the verbatim StableHLO→`arith` template table, the `Emit*` fusion-emitter helpers, and worked lowerings for elementwise, dot, block-scaled dot, transpose, reshape, and constant.
- **Where the `tpu` dialect actually comes from** — the Mosaic import seam (`GetMlirModuleOpFromCustomCall` / `RunMLIRPasses`), so a reimplementer does not waste effort building an MHLO→`tpu` legalizer that does not exist.

| | |
|---|---|
| **XTile dialect** | `xla::xtile::XTileDialect` (`initialize` @ `0x1507ec20`); source `third_party/.../xla/codegen/xtile/ir/xtile_ops.cc` |
| **Front-end passes** | `StablehloLowerToXtilePass` `0x150602c0` · `StablehloLowerToArithPass` `0x1505a880` · `ConvertElementwise0DTensorToScalarPass` `0x15059440` · `VerifyLegalXTileOpsPass` `0x15062300` |
| **Terminal CPU pass** | `xla::cpu::LowerXTileEntryPass` (`xtile-cpu-lower-xtile-entry`; create @ `0x14d905c0`) |
| **Top-level emitter** | `xla::xtile::EmitXTileModule` @ `0x14c1c9e0` (HLO fusion → `xtile.entry_func` module) |
| **Registered ops (7)** | `entry_func`, `return`, `extract`, `insert`, `mask`, `dot`, `dot_scaled` |
| **Attributes (2)** | `xtile.layout` (`LayoutAttr`) · `xtile.tiling_info` (`TilingInfoAttr`) |
| **XTile lowering target** | CPU/LLVM (`memref`/`vector`/`scf`/`LLVM`) — **not** `tpu` or `llo` |
| **`tpu`-dialect origin** | imported only: `GetMlirModuleOpFromCustomCall` @ `0x13e327a0`; pipeline `RunMLIRPasses` @ `0x111fefa0` |
| **MHLO→`tpu` passes in binary** | **none** (0 `*ToTpuPass`/`MhloToTpu`/`StablehloToTpu` symbols) |
| **Confidence** | HIGH (symbol/string-anchored) unless a row or callout says otherwise |

---

## The Two-Tree Picture

The single structural fact that governs this page: the string `"MHLO → XTile → tpu"` names a chain that does not exist as one chain. Two independent lowering trees are linked into `libtpu.so`, and the `tpu`-dialect link in the middle is absent.

```text
TREE A — TPU device (the real product path)
┌────────────────────────────────────────────────────────────────────────┐
│  HLO  ──[97-row pre-passes]──►  HLO                                       │
│       ──[~3225 xla::jellyfish::*Emitter + LloRegionBuilder]──►  LLO       │
│       ──[bundle packer]──►  ISA bundles                                   │
│                                                                          │
│  PLUS, for JAX/Pallas kernels ONLY:                                      │
│  HLO kCustomCall("tpu_custom_call")  carrying a serialized `tpu` module   │
│       ──[GetMlirModuleOpFromCustomCall  0x13e327a0]──►  tpu-dialect IR    │
│       ──[RunMLIRPasses 16-stage pipeline]──►  LLO                         │
└────────────────────────────────────────────────────────────────────────┘
        the `tpu` dialect is AUTHORED upstream and IMPORTED — never
        produced by lowering MHLO

TREE B — XLA CPU/GPU codegen (bundled, off the TPU device path)
┌────────────────────────────────────────────────────────────────────────┐
│  StableHLO/MHLO  ──[stablehlo-lower-to-xtile]──►  XTile (+arith/math)     │
│       ──[stablehlo-lower-to-arith]──►  XTile + arith/math                 │
│       ──[convert-elementwise-0d-tensor-to-scalar]──►  scalarized          │
│       ──[verify-legal-xtile-ops]──►  (gate)                               │
│       ──[xtile-cpu-bufferization → memref]──►                            │
│       ──[xtile-cpu-*-to-vector / -to-loops]──►  vector/scf/memref         │
│       ──[xtile-cpu-lower-xtile-entry]──►  func/LLVM  ──►  CPU kernel       │
└────────────────────────────────────────────────────────────────────────┘
        XTile lowers to the LLVM/CPU stack, NEVER to `tpu`
```

So "MHLO → XTile" is Tree B and is fully real; "XTile → `tpu`" is the broken link. The evidence:

| Observation | Evidence in this binary |
|---|---|
| Every XTile transform pass is `xtile-cpu-*` | CLI strings recovered: `xtile-cpu-bufferization`, `-fuse-elementwise`, `-linalg-elementwise-to-vector`, `-memref-copy-to-loops`, `-shlo-to-vector`, `-tensor-ops-to-bufferizable`, `-unpack-sub-byte-vector-write`, `-vector-to-scalar`, `-lower-xtile-entry` |
| XTile lives in the XLA *codegen* tree, not jellyfish | source paths `codegen/xtile/ir/xtile_ops.cc`, `codegen/xtile/ir/transforms/lower_stablehlo_to_xtile.cc`, `codegen/xtile/codegen/fusion_emitter.cc`, `…/dot_algorithms.cc`, `…/emitter_helpers.{cc,h}`, `…/tiled_emitter_constraints.cc` |
| XTile's dependent-dialect set is CPU/LLVM, with no `tpu` | registry `insert<cf, func, math, xla::cpu::XlaCpuDialect, mhlo, scf, LLVM, tensor, vector, xla::Xla, xla::xtile, stablehlo, linalg, memref, ub>` — `XlaCpuDialect` confirmed present; no `tpu`/`llo`/`sparse_core` in any XTile pass |
| XTile ops bufferize through MLIR std One-Shot Bufferize | `ExtractTileOp`/`InsertTileOp` implement `mlir::bufferization::BufferizableOpInterface`; the `tpu`/`llo` ops never do (they go through jellyfish allocation) |
| The terminal pass is `xla::cpu::` | `LowerXTileEntryPass` rewrites `xtile.entry_func` → `func.func` for the CPU/LLVM emitter |
| No MHLO→`tpu` pass exists | grep of the functions table: **0** hits for `*ToTpuPass`, `*ToTPUDialect`, `MhloToTpu`, `HloToTpu`, `StablehloToTpu`, `LegalizeToTpu`, `LowerMhloToTpu` |

> **NOTE — "TLP" is not a dialect, and it is not XTile.** "TLP" (TPU-Level Program) in the [overview](overview.md) is the conceptual name for the HLO→MLIR import vehicle of Phase 2a. There is no `tlp` MLIR dialect and no `xla::tlp::`/`mlir::tlp::` symbol. On the TPU path, the `tpu` (Mosaic) dialect is the physical realization of the "TPU-Level Program" container — but **only** for imported Pallas/Mosaic kernels. General MHLO is lowered straight to LLO by jellyfish `*Emitter` classes; it never becomes `tpu` ops. XTile is an unrelated CPU/GPU dialect that happens to ship in the same `.so`.

---

## The Four XTile Front-End Passes

The MHLO/StableHLO → XTile front-end is four passes in `xla::xtile::(anonymous namespace)`, each generated from the `impl::*PassBase` TableGen template. The first three rewrite the tiled fusion body; the fourth is a legality gate. All addresses are `.text` create-function entry points.

| Pass class | create() | `runOnOperation` | CLI argument | role |
|---|---|---|---|---|
| `StablehloLowerToXtilePass` | `0x150602c0` | `0x15060560` | `stablehlo-lower-to-xtile` | structural/tensor-shaped StableHLO → `xtile.extract`/`insert`/`dot`/`mask`/`transpose`/`reshape` |
| `StablehloLowerToArithPass` | `0x1505a880` | `0x1505aa60` | `stablehlo-lower-to-arith` | scalar/elementwise StableHLO → `arith`/`math` |
| `ConvertElementwise0DTensorToScalarPass` | `0x15059440` | `0x15059620` | `convert-elementwise-0d-tensor-to-scalar` | `tensor<f32>` (rank-0) → scalar `f32` |
| `VerifyLegalXTileOpsPass` | `0x15062300` | `0x150624c0` | `verify-legal-xtile-ops` | gate: only XTile/`arith`/`math`/`tensor`/`func` legal |

The `StablehloLowerToXtilePass` description string is recovered verbatim: `"Lowers stablehlo ops to Xtile."`

**Dependent dialects** (from each `*PassBase::getDependentDialects` body):

- `StablehloLowerToXtilePass` — registers `arith`, `stablehlo`, plus the shared XTile/`tensor`/`math` registry insert. **Input** = StableHLO (with a small allow-list of MHLO-only ops tolerated); **output** = XTile + `arith` + `math` + `tensor`.
- `StablehloLowerToArithPass` — registers `arith`. **Input** = StableHLO scalar/elementwise ops; **output** = `arith`/`math`.
- `ConvertElementwise0DTensorToScalarPass` — a `mlir::TypeConverter`-driven *full conversion* mapping rank-0 ranked tensors to scalars, with source+target materialization callbacks and `addDynamicallyLegalOp<arith::ConstantOp>` to keep 0-D constants legal until the materializers fire.
- `VerifyLegalXTileOpsPass` — a verifier that walks the module (`walk<Operation*>`) and fails with `"Could not legalize op: "` if any op outside the legal XTile/`arith`/`math`/`tensor`/`func` set survives. This is the "must be gone before XTile codegen" gate.

> **NOTE — `lower-to-xtile` and `lower-to-arith` run together over the tiled fusion body.** `StablehloLowerToXtilePass` handles the *shape-level* ops (the ones that become tile reads/writes/dot/transpose/reshape), while `StablehloLowerToArithPass` handles the *scalar/elementwise* ops over the already-extracted tiles. They are complementary halves of one legalization, not sequential phases that fully legalize independently.

### Position inside the `xtile-cpu-*` pipeline

The four front-end passes are stages 1–4 of the full CPU codegen pipeline, in lowering order (all CLI names recovered verbatim from the string pool):

```text
 1  stablehlo-lower-to-xtile                 shape ops → xtile.extract/insert/dot/mask/transpose/reshape
 2  stablehlo-lower-to-arith                 scalar/elementwise → arith/math
 3  convert-elementwise-0d-tensor-to-scalar  tensor<f32> → f32
 4  verify-legal-xtile-ops                   gate (only XTile/arith/math/tensor/func legal)
 5  xtile-cpu-fuse-elementwise               fuse adjacent elementwise tile ops
 6  xtile-cpu-shlo-to-vector / -linalg-elementwise-to-vector   → vector dialect
 7  xtile-cpu-tensor-ops-to-bufferizable     normalize stray tensor.* ops
 8  xtile-cpu-bufferization                  tensor → memref (One-Shot Bufferize)
 9  xtile-cpu-memref-copy-to-loops           memref copies → scf.for
10  xtile-cpu-vector-to-scalar               residual vector → scalar
11  xtile-cpu-unpack-sub-byte-vector-write   sub-byte stores
12  xtile-cpu-lower-xtile-entry              xtile.entry_func → func.func (xla::cpu::LowerXTileEntryPass)
13  → LLVM dialect → CPU object code
```

> **GOTCHA — this pipeline is NOT a row in the 97-row HLO pre-pass table.** The HLO pre-passes ([Compile Phases](compile-phases.md)) belong to the TPU device compiler (`DeepseaCompilerBase::RunHloPasses`). The `xtile-cpu-*` pipeline is the XLA *CPU* backend's per-fusion codegen, invoked from `EmitXTileModule` after the CPU backend's own StableHLO/MHLO normalization. There is no row in the device pipeline that adds any `xtile-*` pass. A reimplementer searching the TPU phase list for an XTile stage will not find one; that is the concrete consequence of the two-tree split.

---

## The Seven Registered XTile Ops

The XTile dialect registers **seven** ops (each has `build`/`verify`/`print`/`parse` and an `xtile.<name>` op-name string). Earlier inventories that counted "12 ops" conflated registered ops with verifier helpers and attributes — the helpers (`CanonicalizeDotOp`, `CheckConcatenateOp`, `VerifyBufferOp`) and the two attributes are *not* ops.

| op-name | C++ class | recovered `build()` signature | role |
|---|---|---|---|
| `xtile.entry_func` | `xla::xtile::EntryFuncOp` | `(StringRef name, ArrayRef<Type> argTypes, ArrayRef<NamedAttribute>, ArrayRef<DictionaryAttr>)` | tiled-fusion entry function (`FunctionOpInterface`) |
| `xtile.return` | `xla::xtile::EntryFuncReturnOp` | terminator (no operands in std build) | `entry_func` terminator |
| `xtile.extract` | `xla::xtile::ExtractTileOp` | `(Type result, Value source, ValueRange offsets, ArrayRef<long> staticOffsets, ArrayRef<long> staticSizes)` | read a tile out of a tiled buffer/tensor (`BufferizableOpInterface`, `TiledBufferInterface`) |
| `xtile.insert` | `xla::xtile::InsertTileOp` | `(Value tile, Value dest, ValueRange offsets, ArrayRef<long> staticOffsets, ArrayRef<long> staticSizes)` | write a tile back into a tiled buffer/tensor (`BufferizableOpInterface`, `TiledBufferInterface`) |
| `xtile.mask` | `xla::xtile::MaskOp` | `(Value source, ArrayRef<long> maskedDims, Value padValue)` | predicate/pad partial boundary tiles; has `fold()` + `inferReturnTypes` |
| `xtile.dot_scaled` | `xla::xtile::DotScaledOp` (a.k.a. `ScaledDotOp`) | `(Type, Value lhs, Value rhs, Value lhsScale, Value rhsScale, bool, bool, bool)` | block-scaled (MXFP) tile dot; custom print/parse |
| `xtile.dot` | `xla::xtile::DotOp` | (no printed op-name; emitted via `EmitSingleTileDot`) | plain tile dot; folded to `linalg`/`vector` by the dot algorithms |

> **NOTE — `xtile.extract`/`xtile.insert` use the canonical mixed static/dynamic slice form.** Their operands are static-offset + dynamic-offset (`ValueRange`) + static-size — exactly the shape of `tensor.extract_slice`/`tensor.insert_slice`. That is precisely why they bufferize cleanly to `memref.subview`-style reads and in-place tile stores under One-Shot Bufferize.

> **QUIRK — `xtile.dot` has no op-name string.** `DotOp` carries no `xtile.dot` string and no print/parse symbols, so it is either generic-printed or immediately folded by the dot-algorithm emitter (`EmitSingleTileDot`, `dot_algorithms.cc`). The block-scaled `DotScaledOp` *does* have a custom printed form (`xtile.dot_scaled`). Per-algorithm dot instruction sequences (f32×f32, bf16×bf16→f32, tf32, x3/x6 high-precision) live in `dot_algorithms.cc` and are not enumerated here — LOW confidence on the exact per-algorithm bodies.

### XTile attributes and interfaces

| symbol | encodes |
|---|---|
| `xla::xtile::LayoutAttr` (`xtile.layout`) | minor-to-major layout: `minor_to_major : DenseI64ArrayAttr` (parse-error string `"failed to parse XTile_LayoutAttr parameter 'minor_to_major' …"`) |
| `xla::xtile::TilingInfoAttr` (`xtile.tiling_info`) | `tile_count : int32_t`, `tiles_per_workgroup : int32_t` |
| `xla::xtile::TileInfo` | per-op tile descriptor consumed by `EmitParameterExtract` |
| `xla::xtile::TiledBufferInterface` | op interface implemented by `ExtractTileOp` + `InsertTileOp` |
| `xla::xtile::DotOperands` / `ScaledDotOperands` / `DotOperandSide` | helper structs for dot operand routing |

The `tiles_per_workgroup` field, together with the `xla.cpu.KernelThunkProto.NumWorkGroups` anchor, confirms the **CPU/GPU workgroup-parallel** model: a dimension is split into `tile_count` tiles, and `tiles_per_workgroup` are assigned to each parallel workgroup. This is a CPU/GPU codegen concept; the TPU side uses sublane/lane vreg tiling instead ([Mosaic Layout Inference](mosaic-layout-inference.md)).

---

## Per-Op MHLO/StableHLO → XTile Mapping

XTile lowering splits cleanly: scalar/elementwise ops go through a verbatim StableHLO→`arith` table (`StablehloLowerToArithPass`), while shape-level ops go through the `Emit*` fusion-emitter helpers (`StablehloLowerToXtilePass`, `fusion_emitter.cc`/`emitter_helpers.cc`).

### The StableHLO → arith table (verbatim)

Each row is a template instantiation recovered from the demangled name `LowerStableHloOpToArith<Src, FloatDst, SignedIntDst[, UnsignedIntDst]>`. Float vs int dispatch is selected at match time from the operand element type.

| StableHLO src | float dst | signed-int dst | unsigned-int dst |
|---|---|---|---|
| `stablehlo.add` | `arith.addf` | `arith.addi` | `arith.addi` |
| `stablehlo.subtract` | `arith.subf` | `arith.subi` | `arith.subi` |
| `stablehlo.mul` | `arith.mulf` | `arith.muli` | `arith.muli` |
| `stablehlo.div` | `arith.divf` | `arith.divsi` | `arith.divui` |
| `stablehlo.rem` | `arith.remf` | `arith.remsi` | `arith.remui` |
| `stablehlo.max` | `arith.maximumf` | `arith.maxsi` | `arith.maxui` |
| `stablehlo.min` | `arith.minimumf` | `arith.minsi` | `arith.minui` |
| `stablehlo.and` | `arith.andi` | `arith.andi` | `arith.andi` |
| `stablehlo.or` | `arith.ori` | `arith.ori` | `arith.ori` |
| `stablehlo.xor` | `arith.xori` | `arith.xori` | `arith.xori` |

Unary ops route through `LowerStableHloUnaryOpToMath<Src, MathDst>` (recovered instantiation: `stablehlo.round_nearest_even → math.roundeven`). Additional dedicated pattern classes in the same anonymous namespace:

- `LowerCompareOp` — `stablehlo.compare → arith.cmpf`/`arith.cmpi` (predicate by comparison direction + type).
- `LowerConvertOp` — `stablehlo.convert → arith.ext*`/`trunc*`/`sitofp`/`uitofp`/`fptosi`/`fptoui` (type-pair table; class confirmed, the full per-pair mapping is inferred — MEDIUM).
- `ConstantConversionPattern` — `stablehlo.constant → arith.constant` (`DenseElementsAttr` passthrough).
- `ElementwiseConverter` — the generic elementwise dispatcher.

### The shape-level emit helpers

These (in `xla::xtile::` / its anonymous namespace) build the tile-op structure that the `arith` ops fill in:

| helper | text addr | builds |
|---|---|---|
| `EmitXTileModule` | `0x14c1c9e0` | **top level**: the whole `xtile.entry_func` module for a fusion |
| `EmitScope` | `0x15066ec0` | walks an HLO instruction span, emits XTile/`arith` into a value map |
| `EmitParameterExtract` | `0x15066e00` | `xtile.extract` of a parameter tile given a `TileInfo` |
| `EmitConstant` | `0x15064da0` | `arith.constant`/splat for HLO constants |
| `EmitElementwise` | `0x150630a0` | elementwise HLO → `arith`/`math` over extracted tiles |
| `EmitSingleTileDot` | `0x14c277a0` | one tile of a `dot` → the dot algorithm (`dot_algorithms.cc`) |
| `EmitSingleTileScaledDot` | `0x14c289c0` | one tile of a block-scaled dot → `xtile.dot_scaled` |
| `EmitTiledReshape` | `0x150694c0` | reshape over tiled tensors |
| `EmitTiledTranspose` | `0x15069860` | transpose over tiled tensors (`SmallVector<long,6>` permutation) |
| `EmitTiledComputation` / `EmitTiledInstructionList` / `EmitTiledBitcast` / `EmitReduceComputation` / `EmitNestedFusion` | (multiple syms) | whole computation / per-instruction / bitcast / reduce body / nested-fusion inlining |

`EmitXTileModule` signature (recovered):

```cpp
EmitXTileModule(std::string_view name,
                HloFusionInstruction const*,
                SymbolicTileAnalysis const&,
                Tiling const&,
                MLIRContext&,
                absl::Span<Type>,
                std::optional<stream_executor::GpuComputeCapability> const&);
```

The XTile module is emitted directly from an HLO **fusion** using the symbolic tile analysis + the chosen tiling, gated by GPU compute capability (CPU passes `nullopt`). Log anchor at entry: `"Emitting XTile IR for fusion"`. Dot-emit canonicalization uses `CanonicalizeDotOperand` (`0x150692a0`), `MaskDotOperand`, `GetDotLoopIterationCount`, `GetDotAccumulatorType`, `GetPaddedTileSizes`.

### Worked lowerings

```mlir
// (a) elementwise binary — stablehlo.add of two tiled f32 tensors
//     %c = stablehlo.add %a, %b : tensor<256x256xf32>
//     after tiling + StablehloLowerToArith over one tile:
%ta = xtile.extract %A[%i, %j] [/*static offsets*/] [/*static sizes*/] : tensor<TMxTNxf32>
%tb = xtile.extract %B[%i, %j] [...] [...]                              : tensor<TMxTNxf32>
%tc = arith.addf %ta, %tb : tensor<TMxTNxf32>      // float vs int per the arith table
      xtile.insert %tc into %C[%i, %j] [...] [...]
```

```mlir
// (b) dot (matmul), one tile — EmitSingleTileDot (dot_algorithms.cc)
//     stablehlo.dot_general %lhs, %rhs {contracting = ...}
//     per output tile: extract lhs/rhs tiles; loop GetDotLoopIterationCount over the
//     contraction tiling; accumulate in GetDotAccumulatorType (f32 acc for bf16/f8 inputs);
//     operands canonicalized by CanonicalizeDotOperand, boundary-masked by MaskDotOperand;
//     result xtile.insert into the output buffer.
```

```mlir
// (c) block-scaled dot (MXFP) — EmitSingleTileScaledDot
//     a block_scaled_dot custom-call (scale type f8E8M0FNU):
%d = xtile.dot_scaled %lhsTile, %rhsTile, %lhsScale, %rhsScale
       {transpose_lhs, transpose_rhs, acc}    // three bool flags
// verifier strings: "expect scale operands dimension 2 to equal C/block_size",
// "block sizes for cast_from_block_scaled and cast_to_block_scaled must match"
```

```mlir
// (d) transpose — EmitTiledTranspose(b, permutation, tiledValue)
//     stablehlo.transpose {permutation} → tile-level transpose over the tiled
//     RankedTensorType using the SmallVector<long,6> permutation.

// (e) reshape — EmitTiledReshape(b, newShape, tiledValue)
//     stablehlo.reshape → tile-level reshape; collapse/expand of unit dims via CollapseUnitDims.

// (f) constant — EmitConstant → arith.constant (DenseElementsAttr); 0-D constants are
//     later scalarized by ConvertElementwise0DTensorToScalarPass.
```

> **NOTE — broadcast / iota / concatenate.** `BroadcastInDims`, `Splat`, and `ConstLike` helpers handle broadcast; iota and concatenate go through `EmitGeneric` / `CheckConcatenateOperands`. The upstream StableHLO→Linalg `IotaConverter`/`ConcatenateConverter` classes also ship in the binary, but they belong to the GPU/CPU `stablehlo-to-linalg` path, **not** the XTile pass — do not conflate them.

---

## Tiling and Type Conversion

### XTile consumes a tiling; it does not choose one

XTile does **not** decide tiling. The decision is made earlier by `xla::SymbolicTileAnalysis`:

- `AnalyzeFusion` (`0x14c35a40`) / `AnalyzeComputation` (`0x14c359a0`) propagate an `IndexingMap` through the fusion.
- `GetValidTilings` (`0x14c40280`) enumerates valid `Tiling`s; `ParametersSatisfyConstraints` (`0x14c3d040`) + `tiled_emitter_constraints.cc` filter them.
- `ComputeOutputTilingInfo` (`0x14c46960`) / `ComputeTiledInstructions` (`0x14c47f80`) produce the per-instruction `TileInfo`.

The chosen `Tiling` + `GpuComputeCapability` are passed into `EmitXTileModule`, which bakes `TilingInfoAttr{tile_count, tiles_per_workgroup}` and `LayoutAttr{minor_to_major}` onto the entry function and tiles. Failure anchor: `"cannot compute parametric tile sizes for dynamically shaped payload op"`. This is the GPU/CPU symbolic-tiling search; the TPU analogue (window-config search, Mosaic sublane/lane tiling) is entirely separate code.

### Two type conversions

1. **`tensor<f32>` → scalar `f32`** (`ConvertElementwise0DTensorToScalarPass`) — a `mlir::TypeConverter` full conversion carrying `addConversion(Type)` (identity), `addConversion(RankedTensorType)` (rank-0 → element type), `addSourceMaterialization` (scalar → tensor re-wrap), `addTargetMaterialization` (tensor → scalar unwrap), plus `addDynamicallyLegalOp<arith::ConstantOp>` so 0-D constants stay legal until the materializers fire.

2. **`tensor` → `memref`** (`xtile-cpu-bufferization`) — MLIR One-Shot Bufferize via the `BufferizableOpInterface` that `ExtractTileOp`/`InsertTileOp` implement: `ExtractTileOp::bufferizesToMemoryRead = true` on the source and emits a `memref.subview`-style read; `InsertTileOp::bufferizesToMemoryWrite = true` on the dest and emits an in-place tile store; `bufferizesToAllocation` governs scratch allocation. `xtile-cpu-tensor-ops-to-bufferizable` first normalizes stray `tensor.*` ops; `xtile-cpu-memref-copy-to-loops` then lowers copies to `scf` loops. Entry-function memrefs must use a tiled layout — anchor: `"All memref arguments should use the TiledLayoutAttr for layout"`; alignment is checked with `"Cannot confirm that the memref is memory tile-aligned …"`.

---

## Failing / Unsupported Ops at the XTile Boundary

`verify-legal-xtile-ops` and the two lower-to-* full conversions reject anything outside the legal set. Recovered failure anchors:

| anchor string | trigger |
|---|---|
| `"Could not legalize op: "` | an op with no lower-to-xtile/arith pattern survived |
| `"cannot compute parametric tile sizes for dynamically shaped payload op"` | dynamic-shape payload with no static tiling |
| `"Boundaries of the clamp are not legal: "` | `xtile.mask`/clamp bound out of range |
| `"All tiles must have the same rank."` | mixed-rank tiles into a tile op |
| `"At least one tile shape must be specified."` | tiling info absent |
| `"All tiled squeezed dimensions must be of size 1."` | bad squeeze on a tiled dim |
| `"All memref arguments should use the TiledLayoutAttr for layout"` | entry-func memref lacks tiled layout |
| `"Cannot confirm that the memref is memory tile-aligned …"` | alignment check could not prove producer is tile-aligned |
| `"expect scale operands dimension 2 to equal C/block_size "` | `xtile.dot_scaled` scale operand shape mismatch |
| `"Allow XLA's MHLO ops not in StableHLO to remain present after legalization (copy, add_dependency, fusion, etc.)"` | flag: MHLO-only ops tolerated; if not lowered they hit `"Could not legalize op:"` later |

The "MHLO not in StableHLO" flag confirms the input is StableHLO produced from MHLO, with a small allow-list of MHLO-only ops (`copy`, `add_dependency`, `fusion`) permitted to persist into the XTile lowering.

---

## Where the `tpu` Dialect Actually Comes From

Because there is no MHLO→`tpu` pass, a reimplementer needs to know the *real* origin of the `tpu` dialect on the TPU path — it is imported, not lowered. The seam:

1. A Pallas `@pl.kernel` (or hand-written Mosaic kernel) is compiled **by the JAX frontend, outside `libtpu`** into a serialized `tpu`-dialect MLIR module.
2. That module is embedded in `HloOpcode::kCustomCall` with `custom_call_target = "tpu_custom_call"`, inside the `mlir_module` field of the backend config.
3. At HLO→LLO emit time, `xla::jellyfish::mlir_utils::GetMlirModuleOpFromCustomCall` (`0x13e327a0`) extracts and caches the `mlir::ModuleOp` (CityHash128-keyed, one parse per unique kernel).
4. The imported module runs through the `tpu` pass pipeline `RunMLIRPasses` (`0x111fefa0`) — `MosaicSerdePass` (version upgrade) → memref/vector-layout passes → `createLowerToLLOPass` ([tpu → LLO](tpu-to-llo-ods.md)) → LLO.

The `tpu` dialect is therefore **authored upstream and imported**, never produced by lowering general MHLO. See [Mosaic Overview](mosaic-overview.md) for the import/serde/pipeline detail and [The tpu MLIR Dialect](tpu-dialect-and-ops.md) for the dialect itself.

> **GOTCHA — do not build an MHLO→`tpu` legalizer; it does not exist.** The convergence point the [overview](overview.md) draws at Level 2 (`tpu` dialect) is fed by exactly two producers: the Mosaic custom-call import (Tree A2) and — on the general path — *nothing*, because general HLO bypasses `tpu` entirely and is emitted to LLO directly by jellyfish `*Emitter` classes (Tree A1). XTile (Tree B) feeds neither; it targets CPU/LLVM. A reimplementation that allocates effort to an MHLO/StableHLO→`tpu` conversion pattern set is building a stage that the production compiler does not contain.

---

## Confidence Summary

| Claim | Evidence | Confidence |
|---|---|---|
| Four XTile front-end passes with the listed CLI names/create-functions | `StablehloLowerToXtilePass` `0x150602c0`, `StablehloLowerToArithPass` `0x1505a880`, `ConvertElementwise0DTensorToScalarPass` `0x15059440`, `VerifyLegalXTileOpsPass` `0x15062300`; CLI strings recovered | HIGH |
| Seven registered XTile ops with the listed classes | `EntryFuncOp`, `EntryFuncReturnOp`, `ExtractTileOp`, `InsertTileOp`, `MaskOp`, `DotScaledOp`, `DotOp` symbols; op-name strings `xtile.entry_func/return/extract/insert/mask/dot_scaled` | HIGH |
| StableHLO→`arith` table (10 binary-op rows) | `LowerStableHloOpToArith<…>` template instantiations recovered from demangled names | HIGH |
| `Emit*` fusion-emitter helper map + `EmitXTileModule` signature | `EmitXTileModule` `0x14c1c9e0`, `EmitSingleTileDot` `0x14c277a0`, `EmitTiledTranspose` `0x15069860`, … ; log anchor "Emitting XTile IR for fusion" | HIGH |
| XTile attributes `xtile.layout`/`xtile.tiling_info` and the workgroup model | `minor_to_major`, `tile_count`, `tiles_per_workgroup`, `XTile_LayoutAttr`, `TilingInfoAttr` strings; `KernelThunkProto.NumWorkGroups` | HIGH |
| `tensor`→`memref` via One-Shot Bufferize on `BufferizableOpInterface` | `ExtractTileOp`/`InsertTileOp` implement `BufferizableOpInterface` + `TiledBufferInterface`; `xtile-cpu-bufferization` CLI string | HIGH |
| Failure anchors at the XTile boundary | all 10 anchor strings recovered in the decompiled output | HIGH |
| **XTile is NOT on the TPU MXU path** (CORRECTION) | XTile dep-set = `XlaCpuDialect`/`memref`/`vector`/`LLVM` (no `tpu`/`llo`); `xtile-cpu-*` pipeline; `codegen/xtile/` source paths | HIGH |
| **No MHLO/HLO→`tpu` conversion pass exists** | grep of functions table: 0 hits for `*ToTpuPass`/`MhloToTpu`/`StablehloToTpu`/`LegalizeToTpu` | HIGH |
| `tpu` dialect is imported only, via `tpu_custom_call` | `GetMlirModuleOpFromCustomCall` `0x13e327a0`, `RunMLIRPasses` `0x111fefa0`, `MosaicSerdePass`, `tpu_custom_call` all present | HIGH |
| Plain `xtile.dot` printed form + per-algorithm dot bodies | `DotOp` has no op-name string; `dot_algorithms.cc` bodies not decompiled per-algorithm | LOW |
| Full `LowerConvertOp` ext/trunc/itofp/fptoi type-pair table | `LowerConvertOp` class present (119 refs); per-pair mapping inferred, not enumerated | LOW |

---

## Cross-References

- [The TPU Compiler (overview)](overview.md) — the five-phase spine and IR-layer stack; this page refines its Level 2 ("XTile between MHLO and `tpu`") into the two-tree split.
- [Compile Phases 0–3](compile-phases.md) — the device compiler's phase pipeline, into which the `xtile-cpu-*` pipeline is *not* wired.
- [The tpu MLIR Dialect](tpu-dialect-and-ops.md) — the `tpu` target dialect that XTile does **not** produce; the convergence point of the optimizer and Mosaic paths on the TPU side.
- [tpu → LLO Lowering](tpu-to-llo-ods.md) — the `createLowerToLLOPass` descent that the *imported* `tpu` module runs through (the real path, unlike XTile's CPU/LLVM target).
- [DialectConversion Legalizer](dialect-conversion-legalizer.md) — the MLIR dialect-conversion machinery underlying both the XTile full conversions and the `tpu` legalizers.
- [Mosaic Overview](mosaic-overview.md) — the Pallas/Mosaic import seam (`GetMlirModuleOpFromCustomCall` / `RunMLIRPasses`) that is the actual origin of the `tpu` dialect.
- [Mosaic Layout Inference](mosaic-layout-inference.md) — the TPU sublane/lane vreg tiling that is the analogue of XTile's CPU/GPU workgroup tiling.
- **Binary:** `extracted/libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64/libtpu/libtpu.so` (build-id `89edbbe81c5b328a958fe628a9f2207d`)
- **Index entry:** Part V — Compiler: Lowering & Optimization Passes / MLIR lowering chain — [back to index](../index.md)
