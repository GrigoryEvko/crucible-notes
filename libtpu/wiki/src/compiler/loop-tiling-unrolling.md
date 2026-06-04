# Loop Tiling & Unrolling

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`). Other versions will differ. `.text` addresses are virtual; for this binary `.text` VMA == file offset `0xe63c000`.*

## Abstract

There is no single "loop optimizer" in the TPU compiler. What a reader coming from LLVM expects — one pass that tiles a loop nest and unrolls the inner loop by a cost-driven factor — is split across three unrelated mechanisms operating on three different IRs and gated by three independent flags. This page owns the parts of that machinery that are *decisions about loop shape*: the **TileKind rule** that stamps each shape's layout with a compact-or-SparseCore tile tag (`TpuTilingAssignment::GetCopyTileKind`, `0x13dd0ca0`), the **`LoopConfig` / `LoopUnrollConfig` protos** that carry the per-loop-dimension unroll directives a SparseCore kernel was given (decoded field-by-field), and the **HLO while-loop pipeline unroller** (`WhileLoopPipelineUnroller`, `0x12ee2200`) that turns a software-pipelineable `while` into a depth-stage chained-call body.

The thing called "tiling" here is *not* a GPU/Triton tile size. On the TPU TensorCore path it is a 16-bit `TileKind` tag written into every `Shape::Layout` — for a copy, the *pair* `(input_kind, output_kind)` with each kind in `{Default=0, SparseCore=3}` — that the rest of the backend reads to know whether a buffer keeps a real 2-D tile or is SparseCore-laid-out. The genuinely cost-ranked MXU operand-window tiling lives on the [dot/conv → MXU lowering page](dot-conv-mxu-lowering.md); the SparseCore tile-index address expansion (`ExpandTiledMemRefs`) lives on [Tile-Index Expansion](../dma/tile-index-expansion.md). The LLVM-side modulo scheduler that finds the initiation interval for the loops these passes leave behind is on [Bundle Modulo Scheduling](../sched/bundle-modulo-scheduling.md). This page does not re-derive any of those; it owns the TileKind decision, the LoopConfig schema, and the unroll/pipeline transforms.

For reimplementation, the contract is:

- **TileKind is a layout tag, not a tile size.** `TpuTilingAssignment` walks the module after layout assignment and stamps each shape's `Layout` with a `TileKind`. For a `kCopy` it computes a packed `(input,output)` pair from four `TransferSizeUtil` layout predicates; every other opcode inherits the layout-assignment tile via `HardwareLayout::GetDefaultLayout`. There is no rich per-opcode tile-shape solver on this path.
- **The unroll factor a SparseCore loop carries lives in the `LoopConfig` proto.** Field 3 (`unrolled_loops`, repeated `LoopUnrollConfig`) keys each entry on a `loop_dim` (field 1) and carries either an explicit `unroll_factor` or an "auto" sentinel (a oneof). The normalizer resolves "auto" by dividing the loop bound by the `vectorizing_shape` (field 4) element count, with an exact-divisibility CHECK.
- **The per-arch SparseCore copy unroll factor is a two-template switch.** `CustomLoopUnrollPolicy<5>` (template constant `tpu::TpuVersion 5` = 6acc60406, marketing "Ironwood") vs `<3>` (template constant `tpu::TpuVersion 3` = viperfish, the fallback for every version that is not 6acc60406), dispatched on `Target::tpu_version`. The `<3>` template emits 16 (elementwise) / 8 (structured); `<5>` emits transpose `{16,8}`, general 32/16, or MD-vectorizing `16/pack`.
- **While-loop unrolling and software-pipelining are independent, additive transforms.** `WhileLoopUnroller` (full / double-buffer / auto) and `WhileLoopPipelineUnroller` (loop-carry-depth pipelining) are gated by two separate env knobs and can both run on the same module. The pipeliner clones the body into `depth` chained call stages and decrements the trip count by `depth-1`.

| | |
|---|---|
| **TileKind stamp (HLO)** | `xla::jellyfish::TpuTilingAssignment` : `HloPassInterface`; `name()` returns `"tiling-assignment"` |
| **TileKind decision** | `TpuTilingAssignment::GetCopyTileKind` @ `0x13dd0ca0` (decompiled, byte-anchored) |
| **TileKind driver** | `VerifyOrAssignTiling` @ `0x10922a20`; `RunImpl` @ `0x13dd10a0`; `Verify` @ `0x13dd2900` |
| **Post-fusion special tiling** | `TpuPostFusionTilingAssignment` @ `RunImpl 0x13dd85a0`; `name()` returns `"post-fusion-tiling-assignment"` |
| **LoopConfig proto** | `xla::jellyfish::LoopConfig` (serializer `0x1d6eade0`); `LoopUnrollConfig` (serializer `0x1d6f2680`) |
| **Unroll arithmetic** | `LoopConfigWrapper::GetNormalizedUnrollFactor` @ `0x13d6c1c0` (decompiled) |
| **Per-arch SC copy factor** | `GetCustomLoopUnrollPolicy` @ `0x13916ec0`; `CustomLoopUnrollPolicy<(tpu::TpuVersion)5>::GetConfig<(HloOpcode)44>` @ `0x13916fe0`; `<(tpu::TpuVersion)3>` @ `0x139173a0` |
| **While-loop unroller** | `xla::WhileLoopUnroller`; `IsLoopUnrollable` @ `0x12ee8620`; `name()` returns `"while_loop_unroller"` |
| **Pipeline unroller** | `xla::WhileLoopPipelineUnroller::RunImpl` @ `0x12ee2200`; `ComputeWhileLoopPipelineDepth` @ `0x12ee0fc0`; `name()` returns `"while_loop_pipeline_unroller"` |
| **SC window selector** | `WindowUnrollFactorSelector::Select` @ `0x1385c360`; `name()` returns `"window-unroll-factor-selector"` |
| **Pipeline host** | `PostOptimizationPipeline` @ `0x1093fd40` (HLO loop passes); `RunBackendWithBufferAssignment` @ `0x13070bc0` (SC selector) |
| **Confidence** | CONFIRMED (byte-anchored) unless a row or callout says otherwise |

---

## The three "loop" mechanisms, kept apart

The word "tiling" and the word "unrolling" each name three different things in this binary. A reimplementer must keep them separate or build the wrong solver.

| Concern | Mechanism (this page) | IR it acts on | Decision style |
|---|---|---|---|
| **TileKind layout tag** | `TpuTilingAssignment` stamps `(Default\|SparseCore)` per shape | HLO `Shape::Layout` | correctness/layout predicate, *not* a cost model |
| **SparseCore loop unroll factor** | `LoopConfig.unrolled_loops` (written by `WindowUnrollFactorSelector` + `CustomLoopUnrollPolicy`); applied by MLIR `LoopUnrollPass` | jellyfish HLO → SC MLIR `scf.for` | per-arch table + scratchpad-bounded greedy fit |
| **HLO while-loop unroll / pipeline** | `WhileLoopUnroller`, `WhileLoopPipelineUnroller` | HLO `kWhile` | trip-count/body-size thresholds; loop-carry depth |

Three things this page deliberately does **not** own, with their homes:

- The cost-ranked **MXU operand-window tiling** (`IterateThroughWindowConfigs`, priced in MXU cycles, bounded by VMEM) — that is the only genuinely cost-driven tiling search, and it is the convolution path. See [Dot / Conv → MXU Lowering](dot-conv-mxu-lowering.md).
- The **SparseCore tile-index address expansion** (`ExpandTiledMemRefs` / `expandTiledIndices`) — the per-tile memref index algebra. See [Tile-Index Expansion](../dma/tile-index-expansion.md).
- The **bundle modulo scheduler** — the LLVM-side initiation-interval (II) search that software-pipelines the *LLO bundle stream* the SC loop unroller leaves behind. This is a different "pipelining" from the HLO while-loop pipelining on this page. See [Bundle Modulo Scheduling](../sched/bundle-modulo-scheduling.md).

> **NOTE — "tiling" the TensorCore assigns is a tag, not a size.** The HLO `TpuTilingAssignment` pass does not solve for a tile shape. The actual tile geometry (Default 128×16 / X64 / X128) is *produced* by layout assignment and stored in the `Layout`; this pass only writes the `TileKind` enum that says which family of tile a shape uses, so the lowering legalizer can read it back. A reimplementer who builds a tile-size search here is building the wrong pass.

---

## TileKind: the layout stamp

### Driver and mode (`VerifyOrAssignTiling`, `0x10922a20`)

`xla::jellyfish::DeepseaCompilerBase::VerifyOrAssignTiling(const Target&, HloModule*)` is the single entry that decides whether — and how — `TileKind` tags are committed. It reads a tri-state int at `TpuCompilationEnvironment + 0xDFC` (3580 decimal; the flag `xla_tpu_verify_or_assign_tiling_before_lowering`, which has two additional suffixed variants `...lowering3` / `...lowering8` present as separate flag strings):

```cpp
absl::Status DeepseaCompilerBase::VerifyOrAssignTiling(
    const Target& target, HloModule* module) {
  int mode = *(int*)(GetTpuCompEnv(target) + 3580);            // env + 0xDFC

  if (mode == 1) {                                  // VERIFY
    TpuTilingAssignment pass(target, /*ctor_bool=*/false);     // ctor arg = 0
    return pass.Verify(module, /*exec_threads=*/{});           // deepsea_compiler_base.cc:3053
  }
  if (mode == 2) {                                  // ASSIGN
    TpuTilingAssignment pass(target, /*ctor_bool=*/true);      // ctor arg = 1
    return pass.Run(module, /*exec_threads=*/{}).status();     // deepsea_compiler_base.cc:3056
  }
  return absl::OkStatus();                          // mode == 0: leave tiling as-is
}
```

The `TpuTilingAssignment(const Target&, bool)` ctor (`0x13dd1080`) selects verify-vs-assign internally; the decompile shows `mode == 1` constructs with `bool = 0` and calls `Verify()`, while `mode == 2` constructs with `bool = 1` and calls `Run()`. So the ctor `bool` is *not* a "verify_only" flag — `1` is the assign/Run path. The manual override is one tri-state int: `0` skip, `1` verify, `2` assign.

### The per-copy TileKind algorithm (`GetCopyTileKind`, `0x13dd0ca0`)

This is the one TileKind rule recovered byte-for-byte. For a `kCopy`, it returns a `StatusOr` packing a 16-bit `TileKind` = `(input_kind | (output_kind << 8))`, each kind in `{Default=0, SparseCore=3}`. The *input* side reads `operand(0)`'s shape; the *output* side reads `copy.shape()`; both run the identical four-predicate chain.

```cpp
StatusOr<uint16_t> GetCopyTileKind(const HloInstruction& copy, const Target& target) {
  TransferSizeUtil* tu = target.transfer_size_util();   // Target + 0x3B8 (qword 119)
  uint16_t in_kind, out_kind;

  // --- INPUT side: operand(0) shape ---
  const Shape& in = copy.operand(0)->shape();
  int ms = in.layout().memory_space();                  // Shape + 0x138 (byte 312)
  const Shape& real_in = (ms == kVmem /*3*/ || ms == kSmem /*5*/)
                       ? in.tuple_element_or_self() : in;
  if (real_in.layout().has_minor_to_major()             // Shape + 0x130 (byte 304) == 1
      && in.layout().minor_to_major().size() >= 2       // layout()[9] >= 2
      && !tu->HasLinearLayout(in)
      && !tu->HasSparseCoreLayout(topo, in)
      && !tu->HasPadless2ndMinorLayout(topo, in)
      && !tu->HasLarge2ndMinorLayout(topo, in)) {
    in_kind = 0;                                        // Default (compact 2-D tile)
  } else if (tu->HasSparseCoreLayout(topo, in)) {
    in_kind = 3;                                        // SparseCore tile
  } else {
    return InvalidArgument(
        "Input shape does not have compact or sparse core layout.");  // :66
  }

  // --- OUTPUT side: copy.shape() (same chain) ---
  const Shape& out = copy.shape();
  if (/*…same four-predicate chain on out…*/) {
    out_kind = 0;
  } else if (tu->HasSparseCoreLayout(topo, out)) {
    out_kind = 0x300;                                   // = 3 << 8
  } else {
    return InvalidArgument(
        "Output shape does not have compact or sparse core layout.");  // :77
  }

  return in_kind | out_kind;   // stored at *((uint16_t*)result + 4); status word = OK(1)
}
```

The structure is verbatim from the decompile (`0x13dd0ca0`): input kind `v18 ∈ {0,3}`, output kind `v23 ∈ {0,0x300}`, result `v18 | v23` written at `this+8`. VMEM(3)/SMEM(5) operands dereference the tuple-element shape (`v9 = *(v7+8)`) before the has-bit check. The four `TransferSizeUtil` layout predicates are the gate — they decide whether a side keeps a real 2-D tile (kind 0) or is SparseCore (kind 3):

| Predicate | Address | Meaning |
|---|---|---|
| `HasLinearLayout(Shape)` | `0x1d6af220` | untiled/linear layout (host-transfer boundary) |
| `HasSparseCoreLayout(TpuTopology, Shape)` | `0x110b7440` | SparseCore tiling |
| `HasPadless2ndMinorLayout(TpuTopology, Shape)` | `0x1d6af3e0` | no-pad 2nd-minor |
| `HasLarge2ndMinorLayout(TpuTopology, Shape)` | `0x1d6af2e0` | "special" / large 2nd-minor |

> **GOTCHA — the copy TileKind is a *pair*, and a mixed pair is legal.** A `kCopy` whose operand is compact and whose result is SparseCore-laid-out returns `0 | 0x300 = 0x0300`; the all-SparseCore case is `0x0303`; the all-compact case is `0x0000`. A reimplementer who collapses TileKind to a single per-shape value will fail to round-trip the copies that re-tile across the compact↔SparseCore boundary.

### Every other opcode inherits the layout-assignment tile

`RunImpl` (`0x13dd10a0`) does not compute an `(input,output)` pair for non-`kCopy` ops. It walks the result shape with `ShapeUtil::ForEachMutableSubshape` (the `$_4`/`$_5` visitors at `0x13dd26a0` / `0x13dd27e0`) and stamps `HardwareLayout::GetDefaultLayout(result_shape)` into each leaf's `mutable_layout()`. The only opcodes with special handling are:

- **`kCopy`** — the `(input,output)` pair above.
- **`kOutfeed`** (opcode 80) — uses `mutable_outfeed_shape()`.
- **tuple-result ops** (e.g. `kReduce`-class) — recurse the tuple via the subshape visitor, stamping each leaf's default tile.
- **async-SparseCore** (`async_execution_thread == "sparsecore"`) — reads the `SparseCoreConfig` from the async-chain start's backend config and stamps `HardwareLayout::FromProto(config)` — the T8 / SC-tiling opt-in path (`IsT8CustomKernelInstruction`, `0x13dd0e80`).

A dtype gate guards the general stamp: `element_type` must be in the supported-tiled mask `0x2FFF91FFE` (or a small extra set), else the instruction must be fused (`CHECK instruction->IsFused()`, `tpu_tiling_assignment.cc:233`) and is skipped.

> **NOTE — there is no rich per-HLO-opcode TileKind divergence.** The only "rules" are the four above; everything else preserves the layout-assignment-chosen tile via `GetDefaultLayout`. This is why TileKind is documented as a *stamp*, not a solver.

The post-fusion pass `TpuPostFusionTilingAssignment` (`RunImpl 0x13dd85a0`) runs much later, after copy insertion, and forward-propagates "special tiling" (the `HasLarge2ndMinorLayout` family, gated by `xla_tpu_enable_large_2nd_minor_layout[_for_x{4,8,16}]`) from producers that `CanProduceSpecialTiling` (`0x13dd7760`) to consumers that `AcceptsSpecialTiling` (`0x13dd6580`), subject to the entry in/out set and alias constraints. The per-opcode predicate set is named but its producer→consumer rule is not decompiled line-by-line (see Confidence table).

---

## The `LoopConfig` / `LoopUnrollConfig` protos

A SparseCore kernel carries its loop-tiling and unroll directives in a `LoopConfig` proto attached per fusion. The `WindowUnrollFactorSelector` writes it; `LoopConfigWrapper` reads it back; the MLIR `LoopUnrollPass` applies it. Both schemas were decoded field-by-field from their `_InternalSerialize` wire emitters (field number = tag byte >> 3; offsets are struct byte offsets in the generated C++ message; has-bits in `_has_bits_`).

### `xla::jellyfish::LoopConfig` (serializer `0x1d6eade0`)

| Fld | Wire tag | Type | Struct off | Name (inferred) | Semantics |
|---|---|---|---|---|---|
| 1 | `0x0A` | repeated int64 (packed) | RepeatedField `+0x18`, cnt int `+0x1C`, data ptr `+0x20`; has-bit `&1` | `loop_bounds` | per-dim trip / index space |
| 2 | `0x10` | int64 | `+0x58`; has-bit `&8` | (scalar) | aux scalar (trip/total) |
| 3 | `0x1A` | repeated message | RepeatedPtrField `+0x30`, cnt int `+0x38`; has-bit `&2` | `unrolled_loops` | repeated `LoopUnrollConfig` |
| 4 | `0x22` | repeated int64 (packed) | RepeatedField `+0x40`, cnt int `+0x50`, data ptr `+0x48`; has-bit `&4` | `vectorizing_shape` | per-dim native vector shape |

### `xla::jellyfish::LoopUnrollConfig` (serializer `0x1d6f2680`)

| Fld | Wire tag | Type | Struct off | Name (inferred) | Semantics |
|---|---|---|---|---|---|
| 1 | `0x08` | int64 | `+0x18` | `loop_dim` | the dim this entry keys on (join key); has-bit `_has_[0]&1` |
| 2 | `0x10` | int64 (1 byte stored) | `+0x30` | `auto_kind` | "auto/full" sentinel — **oneof case 2** (shared union slot) |
| 3 | `0x18` | int64 | `+0x30` | `unroll_factor` | explicit factor — **oneof case 3** (shared union slot) |
| 4 | `0x20` | bool | `+0x28` | `pipeline_remainder` | remainder-loop pipelining flag; has-bit `_has_[0]&4` |
| 5 | `0x28` | int64 | `+0x20` | (aux) | auxiliary value; has-bit `_has_[0]&2` |

Fields 2 and 3 form a **oneof**: their payloads share the same union slot at `+0x30`, and the discriminator `oneof_case` is the dword at `+0x38` (`2`=auto, `3`=explicit). This is confirmed twice: the serializer at `0x1d6f2680` reads case `*((uint32*)this+14)` (byte +0x38) and writes either field-2 byte `+0x30` or field-3 qword `+0x30`; and `GetLoopUnrollFactor` (`0x13d6c100`) reads the copied-out `oneof_case` from +0x38 and the payload from +0x30. `loop_dim` (field 1) is the join key: `GetLoopUnrollConfig(dim)` (`0x13d6c080`) linearly scans `unrolled_loops` for the entry whose field-1 == `dim`.

> **NOTE — the proto field names are inferred from wire-format reverse engineering, not symbol strings.** The tags, types, and offsets are byte-anchored from the serializers; the human-readable names (`loop_bounds`, `unrolled_loops`, `vectorizing_shape`, `loop_dim`, `unroll_factor`) are the most consistent reading of the surrounding code and CHECK strings. MEDIUM confidence on the *names*; CONFIRMED on the *layout*.

---

## The unroll-factor arithmetic

### `GetNormalizedUnrollFactor` (`0x13d6c1c0`)

This is the algorithm that turns a `LoopUnrollConfig` entry into a concrete factor for a given loop bound. Decompiled (`0x13d6c1c0`):

```cpp
StatusOr<int64_t> LoopConfigWrapper::GetNormalizedUnrollFactor(
    absl::Span<const long> bounds, long dim) const {
  const LoopUnrollConfig* cfg = GetLoopUnrollConfig(dim);   // scan unrolled_loops by loop_dim
  if (!cfg) return 1;                                       // no entry → factor 1

  int64_t raw = (cfg->oneof_case() == 2) ? (-(uint8_t)cfg->auto_kind() | 1)  // auto sentinel
              : (cfg->oneof_case() == 3) ?  cfg->unroll_factor()             // explicit
              :                              1;
  if (raw != -1) return raw;                  // explicit / non-auto factor is final

  // --- AUTO path (raw == -1): divide loop bound by the VECTORIZING SHAPE ---
  int vs_size = loop_config_.vectorizing_shape_size();      // field 4 count, env-of-this + 0x44
  if (vs_size == 0)
    return InvalidArgument("Vectorizing shape missing");                       // 25-char
  if (vs_size >= 2)
    return InvalidArgument(
        "Vectorizing shape has too many dimensions: %d", vs_size);            // 45-char fmt

  int64_t loop_bound       = bounds[dim];
  int     vectorizing_shape = loop_config_.vectorizing_shape(0)[8];  // the dim count
  CHECK(loop_bound % vectorizing_shape == 0)                  // loop_config_wrapper.cc:358
      << loop_bound << " % " << vectorizing_shape;
  return loop_bound / vectorizing_shape;
}
```

The decompile confirms this exactly: the linear scan over `unrolled_loops` (at `a2+48`, count `a2+56`) matching `loop_dim` (`*v7+3`), the oneof read (`v36 == 2` → `-(uint8)v35 | 1`; `== 3` → `v35`; else `1`), the `!= -1` short-circuit, and the auto path reading `vectorizing_shape_size` (`*(a2+68)`), the two error strings, `bounds[dim]`, `vectorizing_shape(0)[8]`, and the `loop_bound % vectorizing_shape == 0` CHECK at line 358 with `LogMessageFatal`.

> **GOTCHA — the "auto" divisor is `vectorizing_shape`, not the unroll-config count.** The normalized factor for an `auto`-tagged loop is `loop_bound / vectorizing_shape[0].dim_count`, requiring exact divisibility. A reimplementer who divides by `unrolled_loops_size` (a plausible misreading) will produce wrong factors and miss the `"loop_bound % vectorizing_shape == 0"` invariant. The supporting helpers: `GetLoopUnrollFactor` (`0x13d6c100`) returns the raw factor (oneof 3 → `unroll_factor`; oneof 2 → `-(uint8)auto_kind`); `GetLoopPipelineRemainder` (`0x13d6c4e0`) returns `(remainder_byte | 0x100)` when set, default `0x101`.

### The per-arch SparseCore copy policy

`GetCustomLoopUnrollPolicy(SmallVector<long,6> bounds, HloInstruction, Target)` (`0x13916ec0`) dispatches on `Target::tpu_version` (`Target + 0x398`):

```cpp
LoopUnrollPolicy GetCustomLoopUnrollPolicy(const SmallVector<long,6>& bounds,
                                           const HloInstruction& hlo,
                                           const Target& target) {
  int version = target.tpu_version();                       // Target + 0x398 (920)
  const HloInstruction* copy = lowering_util::GetCopyInstruction(hlo);
  if (!copy) return {};                                     // empty policy
  LoopUnrollPolicy p =
      (version == 5)                                        // tpu::TpuVersion 5 = 6acc60406 ("Ironwood")
        ? CustomLoopUnrollPolicy<5>::GetConfig<kCopy>(bounds, *copy, target)
        : CustomLoopUnrollPolicy<3>::GetConfig<kCopy>(bounds, *copy, target);  // every other version
  // CHECK each returned unroll_dimension ∈ [0, bounds.size())  (perf_utils.cc:151/152)
  return p;
}
```

The C++ `tpu::TpuVersion` enum is the proto enum minus one — `kJellyfish=0, kDragonfish=1, kPufferfish=2, kViperfish=3, kGhostlite=4, k6acc60406=5` — so the dispatch literal `5` is 6acc60406 (the `TPU_VERSION_*` proto descriptor numbers these 1..6, confirming the −1 offset). There are exactly two arch templates: `<(tpu::TpuVersion)5>` (6acc60406, marketing "Ironwood") taken only when `version == 5`, and `<(tpu::TpuVersion)3>` (template constant viperfish) used as the fallback for every other version (jellyfish through ghostlite).

**`CustomLoopUnrollPolicy<(tpu::TpuVersion)3>::GetConfig<kCopy>` (`0x139173a0`, the fallback template)** — decompiled, byte-anchored:

```cpp
LoopUnrollPolicy CustomLoopUnrollPolicy</*tpu::TpuVersion*/3>::GetConfig<kCopy>(
    const SmallVector<long,6>& bounds, const HloInstruction& hlo, const Target&) {
  CHECK(hlo.opcode() == HloOpcode::kCopy);                  // perf_utils.cc:43
  int inner = bounds.back();
  bool elementwise = lowering_util::IsElementwiseCopy(hlo);
  // ONE entry: dim = inner - 1, factor = 8 * elementwise + 8
  return { { /*dim=*/inner - 1, /*factor=*/ (elementwise ? 16 : 8) } };
}
```

The decompile shows `*(v5+8) = 8 * IsElementwiseCopy + 8` and `*(v5) = v3 - 1` — i.e. factor **16** for an elementwise copy, **8** for a structured copy, on the innermost dim.

**`CustomLoopUnrollPolicy<(tpu::TpuVersion)5>::GetConfig<kCopy>` (`0x13916fe0`, 6acc60406 / "Ironwood")** — decompiled byte-for-byte:

```cpp
LoopUnrollPolicy CustomLoopUnrollPolicy</*tpu::TpuVersion*/5>::GetConfig<kCopy>(
    const SmallVector<long,6>& bounds, const HloInstruction& hlo, const Target& target) {
  CHECK(hlo.opcode() == HloOpcode::kCopy);                  // perf_utils.cc:76
  int rank = bounds.size(); int inner = bounds.back();
  PrimitiveType dtype = hlo.shape().element_type();
  // dtype acceptance mask 0x2FFF91FFE ∪ {0x20,0x21,15,18}; else FATAL (primitive_util.h:757)

  // TRANSPOSE-COPY special case: sub-word packed dtype (mask 0x910) &&
  //   IsMinorTransposeCopy(hlo) && rank >= 2 (CHECK "rank > 1", perf_utils.cc:81) && inner < 32
  if (sub_word_packed(dtype) && lowering_util::IsMinorTransposeCopy(hlo)
      && rank >= 2 && inner < 32) {
    return { {rank-1, 16}, {rank-2, 8} };                   // TWO entries
  }

  // GENERAL case
  bool pred1 = TransferSizeUtil::ShouldPackPREDAsSingleBit(topo, hlo.shape());
  int  pack  = TransferSizeUtil::ElementPackingFactor(dtype, pred1);
  CHECK(target.SupportsSparseCore());                       // target.h:1709
  int scs_tc = target.topology()->sc_tile_count();          // topology + 148
  int subl   = target.SublaneCount();
  LoopConfigWrapper w = LoopConfigWrapper::Create(hlo, rank, scs_tc, pack, subl);
  int factor = lowering_util::IsMDVectorizingShape(pack, target, /*…*/)
             ? 16 / pack
             : ((inner % (32 * scs_tc)) == 0 ? 32 : 16);
  return { {rank-1, factor} };                              // ONE entry
}
```

| Template | Case | Unroll factor |
|---|---|---|
| **`<3>`** (fallback: every version != 6acc60406) | elementwise copy | **16** |
| **`<3>`** | structured copy | **8** |
| **`<5>`** (6acc60406 / "Ironwood") | narrow transpose copy (inner < 32) | `{16, 8}` (two dims) |
| **`<5>`** | MD-vectorizing shape | `16 / pack` |
| **`<5>`** | general, `inner % (32·scs_tc) == 0` | **32** |
| **`<5>`** | general, otherwise | **16** |

> **NOTE — the template constants are not marketing chip names.** `CustomLoopUnrollPolicy<5>` is the C++ template parameter `tpu::TpuVersion 5` = 6acc60406 (the proto descriptor numbers it `TPU_VERSION_6acc60406 = 6`, so the C++ enum is proto−1); `<3>` is the viperfish constant used as the catch-all for every version the dispatch does not route to `<5>`. Both `GetConfig<kCopy>` bodies are decompiled byte-for-byte: `<3>` at `0x139173a0` (`8 * IsElementwiseCopy + 8` → 16/8), `<5>` at `0x13916fe0` (transpose `{16,8}`; general `16 * (inner % (32·scs_tc) == 0) + 16` → 32/16; MD-vectorizing `16 / pack`). CONFIRMED.

---

## SparseCore window unroll: scratchpad-bounded greedy

`WindowUnrollFactorSelector` is the HLO pass that picks the gather/scatter window unroll factor and *writes* the `CustomLoopUnrollPolicy` result into the `LoopConfig.unrolled_loops` proto. It is added in the SparseCore backend's own HLO sub-pipeline by `RunBackendWithBufferAssignment` (`0x13070bc0`) as `AddPass<WindowUnrollFactorSelector, Target const*, long>` where the `long` is `FLAGS_xla_sc_tiles`; it is one of the "two late-running annotation passes immediately before lowering" (`CHECK pipeline.PassesSize() == 2`, `sparse_core_compiler.cc:599`).

`Select(instr, bool)` (`0x1385c360`):

1. Recognize gather-offload / scatter-offload custom fusions; for an offloaded op, log `"But this is an offloaded op. So, we will not find an unroll factor."` and bail (no factor).
2. Extract the inner `kGather` / `kScatter` and classify the access pattern (`IsSublane/IsElement/IsLane/IsChunk` Gather/Scatter).
3. Read the SC scratchpad budget by mode: tile → `MaxTileScratchpadSizeInBytes`; SCS → `MaxScsScratchpadSizeInBytes`; loop-fusion → `FusionEmitter::GetReservedScratchpadBytes` (or `GetReservedSmemBytes`).
4. Pick the **largest** candidate factor that fits — a greedy resource fit, not a roofline cost.
5. For each returned `CustomLoopUnrollPolicy` entry, build a `LoopUnrollConfig` (field-1 = `loop_dim`, oneof-3 = `unroll_factor`) and `Add` it to the parent `LoopConfig.unrolled_loops`.

The per-candidate fit test, `ChunkGatherWindowSizeFitsInScratchpad(target, instr, factor)` (`0x1385c240`):

```text
window_elems = Product(GetSliceSizesTiledPadding(instr))
bytes        = window_elems * ByteSizeOfPrimitiveType(dtype)
sized        = 8 * ((bytes >> 2) + 1) * factor   // round up to 4-byte words × 8 sublanes × factor
return lowering_util::FitsInScratchPad(target, sized)
```

After the proto is written, the MLIR `LoopUnrollPass::runOnOperation` (`0x1352ca20`) walks every `scf.for` (`walk<scf::ForOp>`, pre-order) and applies the factor; the `VectorUnrollPass` then splits wide vector ops to native lane width. These MLIR-side passes are summarized here only to close the data path; their bodies belong to the SparseCore lowering pages.

---

## HLO while-loop unrolling

Two open-source passes run in `PostOptimizationPipeline` (`0x1093fd40`), the last HLO pipeline before the latency-hiding scheduler. They are gated **independently** and can both run on the same module.

### `WhileLoopUnroller` — full / double-buffer / auto

Gated by `*(TpuCompEnv + 4904) != 0` (a *pointer* to the `xla_while_loop_unroll_count`). When added (`AddPass<WhileLoopUnroller, long, bool>` @ `0x1096ee60`), it builds a 0x30-byte object:

| Offset | Value | Meaning |
|---|---|---|
| `+8` | `*(env + 4904)` | `unroll_count` |
| `+16` | `0` | `wrap_in_trip_count_remainder` (hard-false from this site) |
| `+24` | `64` | `kUnrollTripCountThreshold` (full-unroll trip cap) |
| `+32` | `800` | `kUnrollInstructionCountThreshold` (body-size cap) |
| `+40` | `10000` | `kUnrollExpandFactorThreshold` (trip × instrs expand cap) |

The four modes (DebugOptions strings): `WHILE_LOOP_UNROLLING_NO_UNROLL` (disabled), `_DOUBLE_BUFFER` (factor 2), `_FULL_UNROLL` (needs static trip ≤ 64), `_AUTO_UNROLL` (factor 2 only if the body contains a collective). Failure strings: `"Cannot unroll while loop. The trip count is greater than the threshold:  …  Threshold: "` and `"Cannot unroll while loop. Too many instructions in the body: "`.

**`IsLoopUnrollable(HloInstruction*)` (`0x12ee8620`)** — the 9-step legality gate (src `while_loop_unroller.cc`, decompiler-confirmed at the named address):

1. opcode == `kWhile` (0x82) — line 1222.
2. single loop-carried tuple (`operands().size() == 1`) — line 1225.
3. no control predecessors — line 1238 (`"…due to control dependency: "`).
4. `while_body` and `while_condition` contain none of the Send/Recv family `{kSend,kRecv,kSendDone,kRecvDone}` — line 1252 (`"…because it contains a send/recv node: "`).
5. `operand(0)` opcode == `kTuple` (0x81) — line 1259.
6. `while_condition->HasSideEffect() == false` — line 1269.
7. `GetLoopInductionVarTupleIdx` succeeds — line 1277.
8. `HloEvaluator::Evaluate(IV init)` succeeds — line 1287.
9. `MatchTrivialLoopTripCount` succeeds — lines 1295/1299.

On all-pass it stores `{while, init_value, trip_count, iv_tuple_idx, is_unrollable=true}`.

> **NOTE — the exact 4-opcode `flat_hash_set` backing arrays are not byte-decoded.** IDA mislabels the rodata adjacency (`unk_AE07CA8` / `unk_AE07CAC`) as ASCII; the family is fixed as Send/Recv by the diagnostic string, not by decoding the initializer list. HIGH confidence on the family, LOW on the byte-exact opcode list.

### `WhileLoopPipelineUnroller` — software-pipelining

Gated by `EnablePipelinedLoopUnrolling(env)` (`0x1d6b71a0`), which reads an `AutoProto` at `TpuCompEnv + 752` (`xla_tpu_enable_pipelined_loop_unrolling`): "set" iff `(~AutoOr<bool>::FromProtoOrDie(proto) & 0x101) == 0`. In `PostOptimizationPipeline` it is wrapped by `TpuAnnotateTraceableLoops(true)` before and `(false)` after.

**`ComputeWhileLoopPipelineDepth(const HloInstruction&)` (`0x12ee0fc0`)** — the loop-carry depth = number of pipeline stages = number of iterations a value lives before consumption. It CHECKs `kWhile` (line 44, `"while_instruction.opcode() == HloOpcode::kWhile"`) and that the while-body root's *shape* is a tuple (line 52, `CHECK(while_root->shape().IsTuple())`, `"While Instruction has not been canonicalized to have a tuple shape"`), then walks the root tuple's operands: a `kGetTupleElement` (0x40) reading `parameter(0)` at a tuple index `≠ i` is a *carry edge* (slot rotation), recorded in a `flat_hash_map<int64,int64>` (the swiss-table SIMD probe is visible as the `_mm_crc32_u64`/`vpcmpeqb` inner loops). A deque-BFS over the carry-edge graph plus a binary-GCD reduction over chain lengths yields the depth. Depth `< 2` ⇒ the caller skips the loop.

**`RunImpl` (`0x12ee2200`)** — the transform. For each loop with `depth >= 2`, it clones the body into `depth` chained call stages and decrements the trip count by `depth-1`:

```cpp
StatusOr<bool> WhileLoopPipelineUnroller::RunImpl(HloModule* module, threads) {
  for (HloInstruction* loop : while_loops_with_depth_ge_2) {
    int64_t depth = ComputeWhileLoopPipelineDepth(*loop);
    VLOG(1) << "Unrolling: " << loop->name() << " unroll_factor: " << depth;  // :129

    // New outer body "%s.unrolled_%dx": chain `depth` clones of the body as calls.
    HloComputation::Builder b(Format("%s.unrolled_%dx", body->name(), depth));
    HloInstruction* cur = b.AddInstruction(Parameter(0, loop->shape(), "input_tuple"));
    HloComputation* outer = module->AddEmbeddedComputation(b.Build());
    for (int64_t i = 0; i < depth; ++i) {
      HloComputation* stage = module->AddEmbeddedComputation(
          body->Clone(Format("unrolled_%dx_step_%d", depth, i)));
      cur = outer->AddInstruction(Call(loop->shape(), {cur}, stage));
    }
    outer->set_root_instruction(cur);

    HloComputation* new_cond = module->AddEmbeddedComputation(
        cond->Clone(Format("unrolled_%dx", depth)));
    HloInstruction* nw = loop->parent()->AddInstruction(
        While(loop->shape(), new_cond, outer, loop->mutable_operand(0)));

    // Lift depth-1 iterations into the implicit prologue/epilogue.
    Status s = WhileUtil::IncrementWhileLoopTripCount(*nw, /*increment=*/ 1 - depth);  // :176
    nw->set_while_body(outer);
    if (s.ok()) RETURN_IF_ERROR(loop->ReplaceOperandWith(0, nw));
    else        VLOG(1) << "Failed to unroll: " << loop->name();                       // :178
  }
  RETURN_IF_ERROR(FlattenCallGraph().Run(module, threads));   // :188 — inline the stages
  RETURN_IF_ERROR(/*follow-on pass*/.Run(module, threads));   // :190
  return changed;
}
```

The pipelining semantics: the body `B` becomes `depth` chained call stages `B0→B1→…→B_{depth-1}` inside one outer body. Because each stage consumes the previous stage's output tuple, the rotated loop-carry values are produced in stage *k* and consumed in stage *k+1* within one outer iteration — `depth` original iterations are in flight simultaneously. The trip count drops by `depth-1` (`IncrementWhileLoopTripCount(1 - depth)`, helper `0x1e3ae7c0`): the first `depth-1` fills are the implicit prologue and the last `depth-1` are the implicit drain, folded into the chained-call structure. `FlattenCallGraph` then inlines the stages, so the residual counted loop body of `depth × original` size is what the LLVM modulo scheduler ([Bundle Modulo Scheduling](../sched/bundle-modulo-scheduling.md)) sees and overlaps across iterations.

> **GOTCHA — unroll and pipeline are not mutually exclusive.** There is no single "unroll-or-pipeline" switch. Small static-trip loops get full/double/auto unrolled (knob A: `env+4904`); loops with a genuine loop-carry depth ≥ 2 *additionally* get software-pipelined (knob B: `EnablePipelinedLoopUnrolling`). A loop can be candidate-checked by both passes; once unrolled into straight-line code it no longer matches the pipeliner's `kWhile` check, so the order (unroll → pipeline) is what makes them compose rather than conflict.

---

## Cost-model interaction

On the TPU path, the loop transforms on this page are **constrained, not cost-ranked**:

- **SparseCore window unroll** — greedy: the *largest* factor that fits the scratchpad (`FitsInScratchPad`). A resource fit, not a roofline.
- **HLO TileKind** — a correctness/layout decision driven by `TransferSizeUtil` predicates, not a cost model.
- **While-loop unrolling** — gated by trip-count (≤ 64) / body-size (≤ 800) / expand (≤ 10000) thresholds.
- **Pipeline depth** — a structural property (loop-carry distance), computed exactly.

The one genuinely cost-driven tiling search is the convolution MXU window tiling (MXU cycles + VMEM fit) — and it is not on this page; see [Dot / Conv → MXU Lowering](dot-conv-mxu-lowering.md).

---

## Worked example: a SparseCore gather loop on 6acc60406 ("Ironwood")

Given a SparseCore custom-fusion that gathers a window into VMEM inside `while (i < 512)`, on a 6acc60406 target (`tpu::TpuVersion 5`, the only version that takes the `<5>` copy policy), window slice sizes `[1, 8, 128]` (BF16, 2 B), loop-carry rotation depth 3:

1. **TileKind** (`TpuTilingAssignment`, post-fusion): each VMEM buffer's layout already carries its compact tile; the fusion's `kCopy` outputs get `GetCopyTileKind` → `0x0000` (compact in, compact out), or `0x0303` if SparseCore-laid-out.
2. **Window unroll** (`WindowUnrollFactorSelector`): `window_elems = 1·8·128 = 1024`; `bytes = 2048`; per-factor size `= 8·((2048>>2)+1)·f = 4104·f`. Pick the largest `f` with `4104·f ≤ S` (e.g. `S = 64 KiB` → `f ≤ 15` → `f = 8`). The `CustomLoopUnrollPolicy<5>` copy factor for BF16 (`pack=1`): MD-vectorizing → `16/1 = 16`; else `inner=128`, `32·scs_tc` (say `scs_tc=4` → 128) → `128 % 128 == 0` → 32. The selector clamps the copy unroll to what the scratchpad allows.
3. **`LoopConfig` written**: `unrolled_loops += { loop_dim = inner, unroll_factor = f }`; `vectorizing_shape = [16]`. `GetNormalizedUnrollFactor`: explicit → `f`; auto (`-1`) → `bounds[inner] / vectorizing_shape = 128 / 16 = 8`.
4. **MLIR `LoopUnrollPass`** unrolls the `scf.for` over the window by the factor; remainder = `GetLoopPipelineRemainder`.
5. **HLO pipelining** (if `xla_tpu_enable_pipelined_loop_unrolling`): `ComputeWhileLoopPipelineDepth = 3` ⇒ pipeline. Body cloned 3× as `unrolled_3x_step_{0,1,2}`, chained as calls; new trip `= 512 − 2 = 510`; `FlattenCallGraph` inlines the stages. The 510-iteration counted loop feeds the hardware loop counter; the [modulo scheduler](../sched/bundle-modulo-scheduling.md) overlaps the 3 stages.

**Result:** window tile `1×8×128` (BF16), copy unroll 32 (or scratchpad-clamped 8), `scf.for` unrolled ×8, while-loop software-pipelined depth 3 (trip 510), inner loop modulo-scheduled.

---

## Manual override flags

| Flag | Effect |
|---|---|
| `xla_tpu_verify_or_assign_tiling_before_lowering` | tri-state: 0=skip, 1=verify, 2=assign (env `+0xDFC`); `...3`/`...8` per-arch variants |
| `xla_tpu_enable_untiled_layout` / `xla_tpu_untiled_layout_for_1` | allow linear (untiled) layout (`TpuTilingRewriter`) |
| `xla_tpu_experimental_enable_small_minor_tiling` | enable small-minor special tiling |
| `xla_tpu_enable_large_2nd_minor_layout[_for_x{4,8,16}]` | special / large-2nd-minor tiling (post-fusion) |
| `xla_sc_tiles` | SparseCore tile count (the `WindowUnrollFactorSelector` `long` arg) |
| `xla_while_loop_unroll_count` | `WhileLoopUnroller` unroll factor / trip bound (env `+4904` pointer; gate A) |
| `xla_tpu_enable_pipelined_loop_unrolling` | enable `WhileLoopPipelineUnroller` (env `+752` AutoProto; gate B) |
| `xla_sc_disable_remainder_loop_pipelining` / `xla_sc_max_pipelining_stages` | SC remainder-loop pipelining disable / stage cap |
| `xla_tpu_scatter_partial_unroll_factor` / `xla_tpu_unroll_strided_remote_dma` | scatter / strided-DMA loop unroll |

> **NOTE — the `xla_gpu_*` / `xla_cpu_*` tiling/unroll flags in this binary are not on the TPU path.** `SymbolicTileAnalysis`, `TiledHloSchedule`, `xla_cpu_matmul_tiling_*`, `xla_gpu_max_kernel_unroll_factor`, etc. ship in `libtpu.so` but are driven only by the `xla::cpu::`/`xla::gpu::` emitters. A reimplementer should ignore them for TPU codegen.

---

## Confidence Summary

| Claim | Evidence |
|---|---|
| TileKind is a `(input,output)` 16-bit pair packed by `GetCopyTileKind` | decompiled `0x13dd0ca0`: `v18\|v23`, error lines 66/77, mem-space byte 312, has-bit byte 304 |
| Four `TransferSizeUtil` predicates gate compact-vs-SparseCore | called in `GetCopyTileKind`; addrs `0x1d6af220/110b7440/1d6af3e0/1d6af2e0` |
| Non-`kCopy` ops inherit layout via `GetDefaultLayout`; only kOutfeed/tuple/async-SC are special | `RunImpl 0x13dd10a0`, subshape visitors `0x13dd26a0/27e0`, dtype CHECK `:233` |
| `VerifyOrAssignTiling` tri-state at env `+0xDFC` (3580); mode 1→ctor(0)+Verify, mode 2→ctor(1)+Run | decompiled `0x10922a20`; `deepsea_compiler_base.cc:3053/3056` |
| `LoopConfig`/`LoopUnrollConfig` field layout (tags, offsets, oneof) | wire serializers `0x1d6eade0`/`0x1d6f2680` |
| Auto unroll factor = `loop_bound / vectorizing_shape` with divisibility CHECK | decompiled `0x13d6c1c0`; CHECK `loop_config_wrapper.cc:358` |
| `<3>` (viperfish, fallback) SC copy factor 16 (elementwise) / 8 (structured) | decompiled `0x139173a0`: `8*elementwise + 8` |
| `<5>` (6acc60406) SC copy factors (transpose {16,8}; general 32/16; MD 16/pack) | decompiled `0x13916fe0` byte-for-byte |
| `tpu::TpuVersion` C++ enum = proto−1; dispatch `version==5` ⇒ 6acc60406 | `GetCustomLoopUnrollPolicy 0x13916ec0`; `TPU_VERSION_*` proto descriptor numbers 1..6 |
| Window selector picks largest scratchpad-fitting factor; writes `LoopConfig` | `Select 0x1385c360`, fit test `0x1385c240` (`8·((bytes>>2)+1)·f`) |
| While-loop unroll thresholds 64/800/10000; gate at env `+4904` | `AddPass 0x1096ee60` object fields; `IsLoopUnrollable 0x12ee8620` |
| `IsLoopUnrollable` 9-step gate; Send/Recv family forbidden | `0x12ee8620`, src lines 1222–1299 |
| Pipeline depth = loop-carry rotation distance; depth ≥ 2 to pipeline | `ComputeWhileLoopPipelineDepth 0x12ee0fc0` |
| Pipeliner clones body into `depth` chained calls; trip −= depth−1 | `RunImpl 0x12ee2200`; `IncrementWhileLoopTripCount(1-depth)` `0x1e3ae7c0` |
| Unroll and pipeline gated independently, can both run | two gates in `PostOptimizationPipeline 0x1093fd40` (env+4904, `EnablePipelinedLoopUnrolling 0x1d6b71a0`) |
| `TpuPostFusionTilingAssignment` propagates special tiling | `RunImpl 0x13dd85a0`, `AcceptsSpecialTiling 0x13dd6580`, `CanProduceSpecialTiling 0x13dd7760` |

---

## Cross-References

- [The TPU Compiler](overview.md) — Part V orientation; where these loop passes sit in `PostOptimizationPipeline` (Phase 1) relative to layout assignment, fusion, and the scheduler.
- [Fusion Patterns](fusion-patterns.md) — the fusion that runs before `TpuPostFusionTilingAssignment` re-tiles its outputs; the copies the post-fusion stamp re-tiles are introduced here and by copy insertion.
- [Dot / Conv → MXU Lowering](dot-conv-mxu-lowering.md) — the *cost-ranked* MXU operand-window tiling (`IterateThroughWindowConfigs`), the second meaning of "tiling" this page is careful not to claim.
- [tpu → LLO Lowering](tpu-to-llo-ods.md) — the legalizer that reads the `TileKind` tag this page stamps and the loop the unroller leaves behind.
- [Tile-Index Expansion](../dma/tile-index-expansion.md) — the SparseCore per-tile memref index algebra (`ExpandTiledMemRefs`), the address side of SC tiling.
- [Bundle Modulo Scheduling](../sched/bundle-modulo-scheduling.md) — the LLVM-side initiation-interval search that software-pipelines the LLO bundle stream of the loops left after HLO unroll/pipeline; the third meaning of "pipelining".
- **Binary:** `extracted/libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64/libtpu/libtpu.so` (build-id `89edbbe81c5b328a958fe628a9f2207d`)
- **Index entry:** Part V — Compiler: Lowering & Optimization Passes / Fusion, dot/conv, tiling — [back to index](../index.md)
