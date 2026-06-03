# Dot / Conv → MXU Lowering

> *All addresses, symbols, and offsets on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`, 781,691,048 bytes, not stripped, `.text` VA == file offset). Other versions will differ; treat every VA as version-pinned.*

## Abstract

Every TPU matrix product reaches the systolic array through one descent: HLO `kConvolution` → a tiled loop nest of LLO `matprep` / `matmul` / `matres` ops. There is **no separate dot path** — an upstream HLO pass rewrites `kDot` into `kConvolution` so a single lowering serves both (see [RaggedDot and Convolution Geometry Lowering](raggeddot-convolution.md) for that rewrite and the dimension-number mapping). This page documents the *back half* of that descent: the per-window **tile-cost comparator** that picks the systolic tiling, the **`EmitFunctorEnum` dispatch** that picks one of 19 MXU emission strategies, and the **`MxuLatchPacker`** post-pass that halves latch count by packing two operand rows into one MXU latch. The three are the parts a reimplementer cannot guess from the op surface alone.

The reference frame is a textbook im2col/Winograd convolution lowering — and the divergence is total. The TPU MXU is a 128×128 systolic array fed by a *stationary* operand latched into its gain registers and a *moving* operand pushed through a staging register; there is no im2col, no Winograd, no FFT in the binary. Lowering is therefore a tiling problem (how to chop the activation/kernel/output cubes into ≤128×128 windows that fit VMEM and the array), a strategy problem (which dim lands in lanes vs. sublanes vs. the K-reduction), and a packing problem (how to amortise the latch cost across small tiles). Those three problems are solved by `SpatialMajorConvolution::IterateThroughWindowConfigs` (cost search), `MatrixMultiplyAccumulateFunctor::GetEmitFunctorEnumAndLoweringDecisions` (the 19-way selector), and the post-emission `MxuLatchPacker` / `MxuDiagonalPacker` passes respectively. The MXU op surface itself — the ODS shapes of `VectorMatmulMubrOp`, `VectorLatchOp`, the four register enums (`MatmulMode`/`GainLatchMode`/`GainMatrixRegister`/`MatrixStagingRegister`) — is owned by [tpu → LLO ODS Lowering](tpu-to-llo-ods.md); this page consumes those build factories rather than re-deriving them.

For reimplementation, the contract is:

- **The tile-cost comparator** — the per-window MXU-cycle formula (precision-pass ×1/×2 multiplier), the VMEM-footprint budget, the `WindowConfig_CostModelType` enum, and the cycles → VMEM-fit → granule tie-break ladder that selects the winning `WindowConfig`.
- **The `EmitFunctorEnum` dispatch** — the 19-value `convolution_util::EmitFunctor` enum (ordinals byte-exact from the jump table), the `GetEmitFunctorEnumAndLoweringDecisions` decision tree (depthwise / batch-group / reduce-window / dense branches), and the enum → member-function pointer dispatch.
- **The `MatrixMultiplyAccumulateFunctor` loop body** — the per-accumulator-window MXU sequence (`AccumFirstZero` → `Accumulate`), and how K>128 becomes a multi-pass accumulation.
- **The `MxuLatchPacker`** — the greedy same-mode adjacent-pair packing (`PackLatches`), the operand pack/unpack helpers, the `num_latches == num_latchpreps` invariant, and the sibling `MxuDiagonalPacker` quadrant co-residence.

| | |
|---|---|
| **Lowering entry** | `ConvolutionEmitter::Create` — `0x130d86c0` (234 B; megacore dispatch → `SpatialMajorConvolution`) |
| **Lowering driver** | `SpatialMajorConvolution::Emit` — `0x13178340` (3550 B) |
| **Tile-cost search** | `SpatialMajorConvolution::IterateThroughWindowConfigs` — `0x13167f20` (23877 B, 939 BB) |
| **Cost freeze** | `SpatialMajorConvolution::SetupBestConfig` — `0x13172580` (1770 B) |
| **MXU codegen** | `MatrixMultiplyAccumulateFunctor::operator()` — `0x1310cd80` (5398 B) |
| **Strategy selector** | `GetEmitFunctorEnumAndLoweringDecisions` — `0x1310c720` (1632 B) → 16-bit `(decision<<8)\|ord` |
| **Strategy dispatch** | `GetEmitFunctorFromEmitFunctorEnum` — `0x130e8de0` (`switch`, 19 cases) |
| **Mode comparator** | `ConvMatmulModes::operator<` — `0x130e12a0` (39 B; weight table `0xae0f480`) |
| **Latch packer** | `xla::jellyfish::PackLatches` — `0x10f726c0` (8576 B; `mxu_latch_packer.cc`) |
| **LLO ops emitted** | `vmatprep.subr` / `vmatprep.mubr` (+ `.msk`), per-gen `vmatmul`, `vmatres`, fused `kVectorMatmulLmr` |
| **MXU geometry** | 128×128 systolic array; `LaneCount`=128 (`0x1d60f400`), `SublaneCount`=8 (`0x1d60f300`); 2 MXUs/TC on v5+ |

---

## Lowering Entry — ConvolutionEmitter::Create

### Purpose

`ConvolutionEmitter` is a one-method factory: it resolves the megacore core mask and constructs the single lowering class that handles every conv shape. There is no strategy switch here — the strategy choice is deferred to the cost search and the per-window selector downstream.

### Entry Point

```text
ConvolutionEmitter::Create (0x130d86c0, 234 B)        ── megacore dispatch
  ├─ megacore_util::GetMegacoreCoreMask                ── core_mask ∈ {1=secondary, 3=both}
  ├─ lowering_util::SecondaryCoreRegion (if mask==1)   ── swap LLO region to secondary core
  └─ make_unique<SpatialMajorConvolution>(...)         ── the only lowering class
        └─ FusedSpatialMajorConvolution                ── subclass when conv is the root of an output fusion
```

### Algorithm

```c
// ConvolutionEmitter::Create — 0x130d86c0
unique_ptr<ConvolutionEmitter> Create(conv, getter, region, ...):
    core_mask = megacore_util::GetMegacoreCoreMask(conv, target)
    if core_mask == kUseSecondaryCore:                 // == 1
        region = lowering_util::SecondaryCoreRegion(backend_cfg, target)
    CHECK(core_mask == 1 || core_mask == 3)            // else FATAL convolution_emitter.cc:781
    return make_unique<SpatialMajorConvolution>(
        conv, getter, region, /*core_mask=*/core_mask, ...)
```

> **NOTE —** the `core_mask` is a 2-bit core enable: `1` = secondary core only (region swapped), `3` = both cores (megacore split). Any other value FATALs. Megacore splitting picks one iteration dim and halves its bound across the two TC cores (`ChooseMegacoreDimAndIterationBounds`); if no dim is large enough the conv runs single-core. The split dim feeds back into the cost search as the `megacore_active_dims` span the selector tests for "all dims static" (see the selector tree).

### Considerations

The absence of `Winograd` / `Im2Col` / `FFT` strings in the binary is positive evidence of the single-strategy design — the only `im2col` strings present belong to the unrelated NVPTX intrinsic surface. A reimplementer who builds a multi-algorithm conv dispatcher is over-engineering relative to this target: the MXU's natural fit is direct spatial convolution, and *all* shape specialisation happens inside the one `SpatialMajorConvolution` class via the cost search and the 19-way emit selector.

---

## The Tile-Cost Comparator

### Purpose

`IterateThroughWindowConfigs` enumerates every legal `(kernel_window, output_window)` tile tuple, scores each by MXU cycles and VMEM footprint, and streams the survivors to a visitor callback; `SetupBestConfig` freezes the winner into a `WindowConfig` proto. This is the systolic-tile selection — the choice of how a logical matmul is chopped into ≤128×128 array-sized windows. Getting the cost model wrong does not produce wrong answers, it produces a tiling that either overflows VMEM (compile failure) or wastes systolic throughput.

### Entry Point

```text
SpatialMajorConvolution::Emit (0x13178340)
  ├─ EmitZeroElementCases / EmitZeroByteCase           ── size-0 / all-padding short-circuits
  ├─ IterateThroughWindowConfigs (0x13167f20)          ── enumerate + score candidates
  │     ├─ CalculateWindowMxuCycles (0x1315fe60)        ── PRIMARY key: MXU cycles
  │     │     ├─ CalculateClassicWindowCost (0x131626c0)
  │     │     └─ CalculateDepthwiseWindowCycles (0x13161d80)
  │     ├─ GetConvPrecision (0x131916e0)               ── precision index → ×2.0 / ×1.0 multiplier
  │     └─ VmemToUseForPotentialWindows                ── SECONDARY key: VMEM footprint
  └─ SetupBestConfig (0x13172580)                      ── freeze winning WindowConfig
```

### Algorithm — the cost formula

The primary key is MXU cycles for the window. The core formula is recovered from `CalculateWindowMxuCycles` (the `vmulsd`/`vdivsd`/`vaddsd` chain at `0x131605bd`):

```c
// CalculateWindowMxuCycles — 0x1315fe60 (classic at 0x131626c0, depthwise at 0x13161d80)
long window_cycles(window, hlo, operand, target):
    conv_count = (int) matmul_step_count(window)        // # systolic steps this window issues
    precision  = GetConvPrecision(hlo, operand, target) // 0 or 1   @ 0x131916e0
    mult       = precision_mult_table[precision]        // @0xa2c6050 = { 2.0, 1.0 }
    cycles_d   = conv_count * mult / divisor + base_cycles   // base from MxuLatencyTable
    return (long) cycles_d
```

- `precision_mult_table` at `0xa2c6050` is the two-element double array `{2.0, 1.0}`. Index `0` (×2.0) is the low-precision / split-accumulate pass (fp32-emulated and other two-pass dtypes); index `1` (×1.0) is the native single-pass (bf16, fp8).
- The *absolute* per-format base cycles (bf16 ≈ 211, fp8 ≈ 204) come from the per-generation `MxuLatencyTable` — see [MXU Latency Overview](../cost/mxu-latency-overview.md). This pass layers the precision multiplier on top; the `divisor`/`base_cycles` operands are window-shape-dependent and were not fully unwound (LOW confidence on the exact divisor term).

> **GOTCHA —** `kF32` carries the *same* per-format base (211) as `kBf16` in the latency table, yet fp32 is roughly half the throughput. The throughput penalty lives in the **precision multiplier**, not the base: fp32 takes `GetConvPrecision == 0 → ×2.0`. A reimplementer who reads only the per-format latency table and not the precision multiplier will model fp32 at bf16 speed and mis-tile every fp32 conv.

### Algorithm — the budget filter

VMEM footprint is the secondary key *and* a hard filter. Per candidate it sums the three resident tiles:

```c
// VmemToUseForPotentialWindows — inline @0x131684c6, uses xla::Product (0x20cf5200)
long vmem_bytes(window):
    bytes = Product(kernel_tile) + Product(activations_tile) + Product(output_accumulator)
    vmem_unit = Target::MemUnitFromBytes(bytes)         // 0x1d61bfe0; quantized to chunk granules
    return vmem_unit

budget = min( DefaultScopedVmemBytes(target,module) * LoweringVmemLimitScalingFactor(cfg,env),
              GetHloScopedVmemBytes() )
         - already_used_module_vmem
```

Candidates over budget are rejected with `"Giving up on potential kernel window ... because they exceed VMEM limits"` (or `"... they'd run out of VMEM"`). In **scavenging mode** a retry counter (`"compilation retry count: "`) progressively relaxes the scoped-VMEM cap on recompilation.

### Algorithm — the tie-break ladder

The visitor callback is handed `(window6, cycles, MemUnit, granules, WindowConfig_CostModelType)`; the consumer keeps the best by this ordering:

| Rank | Key | Rule | Confidence |
|---|---|---|---|
| 1 | MXU cycles | lowest wins | HIGH |
| 2 | VMEM fit (`MemUnit`) | among equal cycles, the candidate wasting the least scratchpad | HIGH |
| 3 | `best_granules` | among equal cycles+VMEM, the tile granule best matching `Target::ChunkGranules()` (preserves largest-contiguous DMA) | MEDIUM |
| 4 | enumeration order | last resort: output-window outer loop, kernel-window inner loop → first-enumerated equal candidate wins | MEDIUM |

`SetupBestConfig` logs the frozen choice: `"Chosen kernel window: <…> output window: <…> cycles <N> hlo <name> best_granules <G> max vmem: <M>"`.

> **NOTE —** the cost *components* (cycles, MemUnit, granules, CostModelType) are recovered from the callback signature and the producer; the consumer's own min-selection lambda (`ComputeWindowConfigs` / `InitEmitterGenerateWindowConfigAndReturnCost`) was not separately disassembled, so the rank-2..4 ordering is inferred from which fields are passed and the `best_granules` log field (MEDIUM). Rank 1 (cycles) is HIGH.

### Data Tables

| Knob / table | Address | Value / role | Confidence |
|---|---|---|---|
| `precision_mult_table` | `0xa2c6050` | `{2.0, 1.0}` doubles; index = `GetConvPrecision` | HIGH |
| `WindowConfig_CostModelType` | proto enum | `INVALID`(0), `CLASSIC`(1), `ML_PGN_V`(2) | HIGH |
| `Target::LaneCount` | `0x1d60f400` | 128 | HIGH |
| `Target::SublaneCount` | `0x1d60f300` | 8 | HIGH |
| `Target::MemUnitFromBytes` | `0x1d61bfe0` | bytes → quantized `MemUnit` | HIGH |
| `xla::Product(Span<long>)` | `0x20cf5200` | tile-byte product | CERTAIN |

> **QUIRK —** the callback carries a `WindowConfig_CostModelType` so the consumer knows whether to rank on the classic cycle/VMEM model or the `ML_PGN_V` learned-model field. In this build the learned path is a data-table fallback: no `LearnedCostModelClient` is shipped (see [Learned Cost Model Client](../cost/learned-cost-model-client.md)), so `ML_PGN_V` resolves to the same classic numbers. A reimplementation can implement only `CLASSIC` and be behaviourally exact for this binary.

---

## The EmitFunctorEnum Dispatch

### Purpose

Once the tiling is frozen, `MatrixMultiplyAccumulateFunctor::operator()` must pick *how* to map the conv dims onto the MXU's lanes / sublanes / K-reduction. That choice is one of 19 strategies in the `convolution_util::EmitFunctor` enum. `GetEmitFunctorEnumAndLoweringDecisions` is the selector (which strategy); `GetEmitFunctorFromEmitFunctorEnum` is the dispatch (enum → member-function pointer). This is the dot/conv analogue of an instruction-selection table: a fixed enum, a switch, and a decision tree that reads shape/dtype booleans off the parent.

### The 19-value enum

`GetEmitFunctorFromEmitFunctorEnum` (`0x130e8de0`) is a `switch (ord)` with `case 0..18`; the out-of-range default FATALs at `matrix_multiply_accumulate_functor.cc` line `0x24a`. The decompile confirms `case 18` is the highest ordinal (19 strategies). The companion `EmitFunctorToString` (`0x130e88a0`) uses a parallel jump table whose ordinal-3/4 string fragments (`"...wSublane"`, `"...Lane"`) match the table below byte-for-byte. Both jump tables are byte-exact in `.rodata` (member-fn `0xae0f50c`, string `0xae0f4c0`).

| Ord | `EmitFunctor` value | member fn | MXU strategy | Confidence |
|----:|---|---|---|---|
| 0 | `kBatchGroupDepthwiseInputBatchInLanesOutputBatchInSublanes` | `0x130e8f40` | grouped depthwise: input batch lanes, out sublanes | HIGH |
| 1 | `kBatchGroupDepthwiseInputBatchInSublanesOutputBatchInSublanes` | `0x130e9b80` | grouped depthwise: both batches in sublanes | HIGH |
| 2 | `kDepthwiseAllBatchInLanes` | `0x130ea960` | depthwise: all batch in lanes (1 latch/channel) | HIGH |
| 3 | `kReduceWindowSublane` | `0x130eb860` | `kReduceWindow`-as-conv, sublane-major reduce | HIGH |
| 4 | `kReduceWindowLane` | `0x130ebd80` | `kReduceWindow`-as-conv, lane-major reduce | HIGH |
| 5 | `kDepthwiseInputBatchInLanes` | `0x130ec2a0` | depthwise: input batch in lanes | HIGH |
| 6 | `kDepthwiseAllBatchInSublanesPacked` | `0x130ed3c0` | depthwise: all batch sublanes, packed | HIGH |
| 7 | `kDepthwiseInputBatchInSublanes` | `0x130ef2e0` | depthwise: input batch in sublanes | HIGH |
| 8 | `kInputFeaturePackedInputBatchInLanes` | `0x130f01a0` | feature-packed K reuse, input batch lanes | HIGH |
| 9 | `kInputBatchInLanes` | `0x130f0740` | classic: input batch rolled into lanes | HIGH |
| 10 | `kAllInputFeaturePackedInSublanesOutputBatchInSublanes` | `0x130f48a0` | full K in sublanes, packed; out batch sublanes | HIGH |
| 11 | `kAllInputFeatureInSublanesOutputBatchInSublanes` | `0x130f5d00` | full K (≤128) in sublanes — single matmul covers K | HIGH |
| 12 | `kAllInputFeatureInSublanesOutputBatchInSublanesXposeReuse` | `0x130f7e20` | as 11, reusing transposed activations | HIGH |
| 13 | `kOutputBatchInLanesKernelOutputFeatureInLanes` | `0x130fb360` | dual-lane: out batch + kernel out-feature in lanes | HIGH |
| 14 | `kOutputBatchInLanesInputBatchInSublanes` | `0x130fee80` | transposed MAC: out batch lanes, in batch sublanes | HIGH |
| 15 | `kOutputBatchInLanesKernelOutputFeatureInSublanes` | `0x131021a0` | hybrid: out batch lanes + out feature sublanes | HIGH |
| 16 | `kAllBatchInSublanes` | `0x131055c0` | batch packed into sublanes (common default) | HIGH |
| 17 | `kInputBatchInSublanesOutputBatchInSublanesPacked` | `0x131064c0` | in+out batch both in sublanes, packed | HIGH |
| 18 | `kOutputBatchInSublanes` | `0x13108b60` | output batch in sublanes (the broad default) | HIGH |

> **QUIRK —** the enum's *ordinal* order and its *grouping* are unrelated. Ordinals 0–7 are the depthwise / grouped / reduce-window family, 8–18 are the dense dot/conv core — but the selector reaches them out of ordinal order (it can pick ord 18 before ever testing ord 8). Drive a reimplementation off the **decision tree**, not the ordinal sequence; the ordinals exist only to index the two jump tables.

### Algorithm — the selector

`GetEmitFunctorEnumAndLoweringDecisions` (`0x1310c720`) returns a 16-bit value: low byte = the ordinal above, high byte = a second "lowering decision" bool consumed by `UpdateLoweringStrategyWithWindowInfo`. It reads shape/dtype booleans off the parent decision-state struct and the frozen window. The branch helpers are binary-confirmed (`GetReduceWindowType`, `Target::LaneCount`/`SublaneCount` ×2, `ShouldPackInputFeature` ×2, `WindowCoversEntireEffectiveInputFeature`):

```c
// GetEmitFunctorEnumAndLoweringDecisions — 0x1310c720
// returns (decision_byte << 8) | emit_ordinal
pair<EmitFunctor,bool> select(state, window):
    if state.is_batch_group_depthwise:                  // routes to grouped depthwise
        require !reduce_window && megacore_span.empty() && !output_batch_in_lanes
        if input_batch_in_lanes:  return (k0, decision=1) // BatchGroupDepthwise InLanes/InSub
        else:                     return (k1, decision=1) // BatchGroupDepthwise InSub/InSub
    else if state.is_depthwise:
        if input_batch_in_lanes:
            ord = kDepthwiseInputBatchInLanes (5)
            if output_batch_in_lanes:
                if reduce_window:
                    rw  = fusion_util::GetReduceWindowType(hlo)   // 0/1/2  @0x1454d4a0
                    ord = 4 - rw                          // 4=ReduceWindowLane, 3=…Sublane
                else:
                    ord = kDepthwiseAllBatchInLanes (2)
            return (ord, ...)
        else:                                             // input batch in sublanes
            if feature_packed && !out_lanes && kernel_of_lanes:
                return kDepthwiseAllBatchInSublanesPacked (6)
            else:
                return kDepthwiseInputBatchInSublanes (7)
    else:                                                 // ---- DENSE dot/conv core ----
        if input_batch_in_lanes:
            if output_batch_in_lanes:                     // both in lanes
                pack = ShouldPackInputFeature(out_feature, target)  // @0x13194220
                return pack ? kInputFeaturePackedInputBatchInLanes (8)
                            : kInputBatchInLanes (9)       // decision=0
            else:                                         // input lanes, output sublanes
                return xpose_reuse ? k13 : <14/15 family>
        else:                                             // input batch in sublanes
            if output_batch_in_lanes:                     // out lanes, in sublanes
                ord = kAllBatchInSublanes (16)            // decision=1
                if out_feature >= SublaneCount(8):
                    if WindowCoversEntireEffectiveInputFeature(window):  // @0x1315b2c0; K fits one window
                        ord = (spatial_extent < 2 || all_dims_static)
                                ? kAllInputFeatureInSublanesOutputBatchInSublanes (11)
                                : kAllInputFeatureInSublanesOutputBatchInSublanesXposeReuse (12)
                    else:                                 // K-tiled (K>128)
                        ord = ShouldPackInputFeature(...) ? k10 : k16   // decision=0
                return (ord, ...)
            else:                                         // both in sublanes
                ord = kOutputBatchInSublanes (18)         // decision=1, the broad default
                if out_batch >= LaneCount(128) && (out_batch % 128) && pack_eligible:
                    ord = kInputBatchInSublanesOutputBatchInSublanesPacked (17)
                return (ord, ...)
```

Key helpers:

- `fusion_util::GetReduceWindowType(hlo)` (`0x1454d4a0`) → 0/1/2; `ord = 4 - type` maps to ReduceWindowLane(4) / ReduceWindowSublane(3).
- `convolution_util::ShouldPackInputFeature(out_feature, target)` (`0x13194220`) → true iff the input-feature count is below `Target::SublaneCount()` (==8) — sub-8 K reductions get feature-packed. The `xor $0x9,al` in the dense both-in-lanes branch maps `{pack?1:0} → {8:9}`.
- `WindowCoversEntireEffectiveInputFeature(window)` (`0x1315b2c0`) → true iff one MXU window absorbs the whole K dim (no K-tiling).

> **GOTCHA —** the high decision byte is *not* the strategy. It is a second bool (1 in most paths, 0 in the packed-K and dual-lane branches) fed to `UpdateLoweringStrategyWithWindowInfo(.., decision)`, which patches the `ConvolutionLoweringStrategy` for the chosen window — most plausibly a transpose-reuse / sublane-tile toggle. The bit is recovered; the human-readable field name is not (LOW). A reimplementer must thread it to the strategy-patch step even though its precise meaning is unresolved here.

> **NOTE —** practical decode: a plain bf16 batched dot with K≤128, both batches in sublanes, K folded into one window → ord **11** (or **12** with transpose-reuse). The same dot with K>128 (K-tiled) → ord **18** (`kOutputBatchInSublanes`), the canonical default, with K accumulated across passes (next section).

---

## The MatrixMultiplyAccumulateFunctor Loop

### Purpose

This is the inner code generator: given the frozen window and the chosen `EmitFunctor`, it issues the actual LLO `matprep` / `matmul` / `matres` stream per accumulator tile, handles K>128 as a multi-pass accumulation, and runs the output-fusion epilogue. The functor is constructed once per chunk; `operator()` drives it.

### Algorithm

```c
// MatrixMultiplyAccumulateFunctor::operator() — 0x1310cd80
void operator()(builder):
    ret      = GetEmitFunctorEnumAndLoweringDecisions()   // 0x1310c720
    ord      = ret & 0xff;  decision = (ret >> 8) & 0xff   // split at 0x1310cf91
    functor  = GetEmitFunctorFromEmitFunctorEnum(ord)      // 0x130e8de0 → member fn ptr
    name     = EmitFunctorToString(ord)                    // 0x130e88a0 (debug)
    UpdateLoweringStrategyWithWindowInfo(.., decision)     // 0x13167e80 (patch strategy)

    ResetRegistries(builder)                               // clear latch-group + rotation maps
    for acc_window in 0 .. AccWindowCount():
        ctx = InterLatchGroupContext(acc_window)
        (this->*functor)(acc_window, builder)              // issues loads + matprep/matmul/matres
        PossiblyDoOutputFusion(acc_window, builder)        // bias/activation epilogue
    MaterializeLatchGroups(tile_emitter_map_)              // flush deferred latch-group emitters
```

### K>128 multi-pass accumulation

The 128×128 array reduces at most 128 of the K (input-feature) dim per pass. For K>128 the lowering tiles K and accumulates partial `matres` results into a VMEM accumulator:

```c
// per output tile, K tiled into n passes:
AccumFirstZero(off0, ..., builder)    // 0x13124e80 — first K-tile: matmul, matres → acc (no add)
for k in 1 .. n:
    Accumulate(..., builder)          // 0x13123f20 — matres → tmp; VaddF32/VaddS32(acc, tmp)
PossiblyDoOutputFusion(acc, builder)  // 0x... — bias add / activation after K fully reduced
```

`Accumulate` uses `LloRegionBuilder::VaddF32` for fp accumulation and `VaddS32` for int. The `matres`+`Vadd` chain is later coalesced by the `MxuLmrTransform` / `MxuResultAddJoin` post-passes where the schedule allows.

```text
LLO shape per output tile (K = 256 → 2 passes, bf16):
  ; --- K-tile 0 (AccumFirstZero) ---
  vlatch        rhs_K0   (GainLatchMode _PACKED_BF16)   ; stationary weights → MXU latch
  vmatprep.mubr lhs_K0   (MatpushTarget MSRA)           ; moving activations → staging
  vmatmul                (DoneWithGains NORMAL, fmt kBf16)
  vmatres       acc                                     ; first matres → accumulator (no add)
  ; --- K-tile 1 (Accumulate) ---
  vlatch        rhs_K1   (_PACKED_BF16)
  vmatprep.mubr lhs_K1   (MSRB)                          ; alternate staging register
  vmatmul                (NORMAL, kBf16)
  vmatres       tmp
  VaddF32       acc, tmp                                ; K-accumulate
  ; --- epilogue ---
  DoOutputFusion(acc)                                   ; bias / activation if fused
```

> **NOTE —** the adjacent matpreps alternate `MSRA → MSRB` (the double-buffered moving-operand staging register) so the MXU can stage the next K-tile while the current one drains; the *numeric* GMR index and the A/B alternation are this allocator's per-tile assignment, not the upstream `GetGainLatchModeAndScalingFactor` selector. The latch/matmul/accum triples are ordered by `ReorderLatchesMatmulsAndAccums(lhs_mode, rhs_mode, lhs_ty, rhs_ty, bool)` (`0x1311f880`), which calls `GetPackedGainLatchMode` per latch and `GroupLatchGroupsForX4` (`0x1311c8a0`) to batch 4 latches into one x4-packed issue. See [Matprep, IAR, and Latch Sub-Slots](../isa/slot-matprep-iar-latch.md) for the slot encoding.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `operator()` | `0x1310cd80` | per-chunk MXU sequence driver | HIGH |
| `GetEmitFunctorEnumAndLoweringDecisions` | `0x1310c720` | 19-way strategy selector | HIGH |
| `GetEmitFunctorFromEmitFunctorEnum` | `0x130e8de0` | enum → member fn ptr | HIGH |
| `EmitFunctorToString` | `0x130e88a0` | debug name (parallel jump table) | HIGH |
| `AccumFirstZero` | `0x13124e80` | first K-tile, accumulator seeded zero | HIGH |
| `Accumulate` | `0x13123f20` | subsequent K-tile, `VaddF32`/`VaddS32` | HIGH |
| `ReorderLatchesMatmulsAndAccums` | `0x1311f880` | (latch, matmul, accum) ordering + gain staging | HIGH |
| `CreateMatprepOrLatch` | `0x1311bf00` | emit latch / matprep (carries `GainLatchMode`, `PrimitiveType`) | HIGH |
| `CreateMatprepOrMatmul` | `0x1311bb40` | emit matprep / matmul (carries `MatmulMode`, `MatmulDataFormat`) | HIGH |
| `GroupLatchGroupsForX4` | `0x1311c8a0` | group 4 latches into one x4 packed issue | HIGH |

---

## ConvMatmulModes — the Mode-Pair Comparator

### Purpose

Before emission, `GetMatmulModes` builds a candidate list of `{lhs_mode, rhs_mode}` pairs (the stationary/moving feed roles for the two operands), then sorts it so the cheapest feasible pair is consumed first. The comparator is a single-key weighted preference. This is the small-but-easy-to-miss piece: the *mode* choice (which operand is stationary, transposed, packed) is orthogonal to the *strategy* choice above and to the *dtype format* choice below.

### Algorithm

`ConvMatmulModes` is a 2-byte struct `{uint8 lhs_mode; uint8 rhs_mode;}`, each byte an `xla::jellyfish::MatmulMode` ordinal (0..15). The comparator is byte-exact (decompiled at `0x130e12a0`):

```c
// ConvMatmulModes::operator< — 0x130e12a0 (39 B)
// weight table W[16] @ 0xae0f480
bool operator<(const ConvMatmulModes& a, const ConvMatmulModes& b) {
    return (unsigned) (W[a.lhs_mode] + W[a.rhs_mode])
         < (unsigned) (W[b.lhs_mode] + W[b.rhs_mode]);   // setb — unsigned-less on the int sums
}
```

The decompile reads literally `dword_AE0F480[*a1] + dword_AE0F480[a1[1]] < dword_AE0F480[*a2] + dword_AE0F480[a2[1]]`. The 16-entry weight table (`0xae0f480`, lower = cheaper / preferred):

| MatmulMode ordinal | 0 | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 | 11 | 12 | 13 | 14 | 15 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| **weight** | 5 | 4 | 3 | 2 | **1** | 40 | 40 | 30 | 30 | 20 | 10 | 10 | 40 | 40 | 40 | 40 |

So the preference tiers are: `{0..4}` (weights 5..1, the cheap stationary-feed group, ord 4 cheapest), `{10,11}` (=10, mid), `{7,8,9}` (30/30/20), and `{5,6,12,13,14,15}` (=40, the most expensive transposed / multi-quadrant feeds).

`GetMatmulModes()` (`0x130df600`) cross-products the per-operand LHS mode list with the RHS mode list (each from `GetMatmulModes(long operand)` at `0x130dfbe0`, which calls `GetConvPrecision`), skips the degenerate `{2,2}` pair, then `std::stable_sort`s by `operator<` (sorter `0x130dfd80`). The decompile confirms the `stable_sort` + `operator<` call. Because the sort is *stable*, the secondary tie-break when two pairs sum equal is "first-generated wins", and generation order is LHS-mode-outer × RHS-mode-inner.

> **QUIRK —** the comparator is a *single* additive key, not a lexicographic `(lhs, rhs)` order. Two very different pairs `{4,10}` and `{1,2}` both sum to 11 and 3 respectively — the model genuinely treats the mode pair as one cost number, so `{stationary=cheap, moving=mid}` can lose to a pair with a more expensive stationary but a cheaper moving operand if the sum is lower. The MatmulMode ordinal *names* (stationary/moving/transposed/packed roles) were not cleanly extractable from `symbolizeMatmulMode` (`0x13e4e660`); the weights are byte-exact, the names are unresolved (LOW). See [Matmul Mode Modifiers](../cost/matmul-mode-modifiers.md) for the cost-side modifier view.

---

## MxuLatchPacker — PackLatches

### Purpose

The conv emitter produces one latch per operand row, but bf16/int8 operands pack two element-rows into a single MXU latch — halving the latch instruction count. `PackLatches` is the post-emission pass that performs this packing greedily over adjacent same-mode latches. It runs after `MxuSequenceCollector` and `MxuAssigner` (which bind sequences to physical MXU banks) and before the LMR fusion.

### Entry Point

```text
LLO post-emission MXU stack (after MatrixMultiplyAccumulateFunctor):
  MxuSequenceCollector  ── gather (matprep,matmul,matres) runs into MxuSequence objects
  MxuAssigner           ── bind sequences to physical MXU bank; SetLatchIndices; MSR bounce
  MxuSequenceTrimmer    ── drop unused matres/matprep
  PackLatches (0x10f726c0) ──────── greedy same-mode adjacent latch packing   [THIS UNIT]
  CombineSequencesToFitMxuSize / PackSingleSequence (mxu_diagonal_packer.cc) ── quadrant co-residence
  MxuLmrTransform       ── fuse (latch,matmul,matres) → LMR; partial-quadrant rewrite
  MxuAccumulation / MxuResultAddJoin / MxuLatencyBalancing
```

### Algorithm

```c
// xla::jellyfish::PackLatches(Span<unique_ptr<MxuSequence>>, Target, CycleTable,
//                             PackLatchesOptions) — 0x10f726c0 (8576 B), mxu_latch_packer.cc
void PackLatches(sequences, target, cycle_table, opts):
  for each MxuSequence run, walk latch instructions in pairs (cur, nxt):
    m_cur = cur.latch_mode()                 // LloInstruction::latch_mode  @0x1d4e7500
    m_nxt = nxt.latch_mode()
    if m_cur != m_nxt:                        // "matching pair failed because latch modes don't match"
        emit cur UNPACKED; advance by 1
        continue
    // packable pair — also check MSR target compat ("merging matpushes")
    check cur.matrix_staging_register() vs nxt.matrix_staging_register()  // @0x1d4e7b80
    // pack two operand vregs into one packed vreg:
    if bf16: packed = VpackCBf16(lo, hi)      // @0x1d5669a0
    else    : packed = VmpackCLow(lo, hi)     // @0x1d55d520  (int8/byte)
    CreatePackedVlatchOrVlatchprep(builder, opcode, packed,
                                   packed_GainLatchMode, mask)   // @0x10f74c20
    // at the consuming matmul, split back:
    hi = VunpackUpperCB8ToBf16(packed)        // @0x1d5696e0
    lo = VunpackLowerCB8ToBf16(packed)        // @0x1d569600
    align via VslaneRotateAZ(v, SublaneCount) // @0x1d54ee00
    advance by 2
  // odd trailing latch → "skipping last latch" (left unpacked)
  CHECK(num_latches == num_latchpreps)        // matrix_register.h pairing invariant
  LloMutator::SubstituteWithRegionAndDestroy(latch, packed_region)
```

Guard helpers: `MaskAndVregForLatchOrLatchprep` (`0x10f74aa0`) builds the predicate mask + source vreg for a partially-valid latch; `VectorS8FeedingF32Latch` (`0x10f74840`) detects the int8-operand-into-fp32-latch case that must not be double-packed.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `PackLatches` | `0x10f726c0` | greedy same-mode adjacent latch packer | HIGH |
| `CreatePackedVlatchOrVlatchprep` | `0x10f74c20` | emit the single packed latch | HIGH |
| `MaskAndVregForLatchOrLatchprep` | `0x10f74aa0` | predicate mask + source vreg | HIGH |
| `VectorS8FeedingF32Latch` | `0x10f74840` | int8→fp32-latch guard | HIGH |
| `LloRegionBuilder::VpackCBf16` | `0x1d5669a0` | pack bf16 pair → one vreg | HIGH |
| `LloRegionBuilder::VmpackCLow` | `0x1d55d520` | pack int8/byte → one vreg | HIGH |
| `VunpackUpper/LowerCB8ToBf16` | `0x1d5696e0` / `0x1d569600` | result split at consumer | HIGH |
| `LloInstruction::latch_mode` | `0x1d4e7500` | the packing equivalence key | HIGH |
| `LloInstruction::matrix_staging_register` | `0x1d4e7b80` | MSR compat check | HIGH |

> **GOTCHA —** `PackLatches` is *adjacency-greedy*, not optimal: it packs `(cur, nxt)` only when their `latch_mode` matches exactly, and it never reorders across a mode boundary. A run `[bf16, bf16, int8, bf16]` packs the first pair, emits the int8 unpacked, and leaves the trailing bf16 unpacked ("skipping last latch") — even though reordering would pair the two bf16 ends. A reimplementation that performs a global min-cost pairing will diverge from this binary's output. The `num_latches == num_latchpreps` CHECK is the hard invariant: every packed latch must keep the latch/latchprep count balanced.

### MxuDiagonalPacker — the sibling quadrant packer

`CombineSequencesToFitMxuSize` (`0x10f6eb80`) and `PackSingleSequence` (`0x10f6fd20`) are *not* part of the latch packer — they live in `mxu_diagonal_packer.cc` and pack short MXU sequences into the unused quadrants of the 128×128 array:

- `CombineSequencesToFitMxuSize` — **block-diagonal** packing (strategies `"PackToDiagonal"` / `"ShardToDiagonal"`): latches operand A into the upper-left and operand B into the lower-right quadrant so one set of matmul steps computes both independent products. Compatibility CHECKs: `"Incompatible matmul format"`, `"Incompatible number of matmuls"`, `"Incompatible number of latches"`, `"Incompatible size for matres sequences"`.
- `PackSingleSequence` — **single-quadrant** packing (strategy `"PackToSingleQuadrant"`): packs one sequence whose K and N fit a 64×64 (or 64×128) quadrant into a corner. Requires matmul count divisible by 2 (`"Rejecting, number of matmuls must be divisible by 2"`) and uniform format.

Together they double MXU throughput for narrow K/N tiles that would otherwise under-fill the array.

---

## Per-dtype Format — GetMatmulDataFormat

### Purpose

Orthogonal to strategy and mode, the per-matmul `MatmulDataFormat` fixes the actual MXU op variant per dtype. `GetMatmulDataFormat` (`0x1307be40`) is a two-stage decision: strategy-packing-bit precedence, then a `PrimitiveType` jump table.

### Algorithm

```c
// convolution_util::GetMatmulDataFormat(PrimitiveType prim,
//   const ConvolutionLoweringStrategy& s, const HloInstruction&, const Target&) — 0x1307be40
int GetMatmulDataFormat(prim, s, hlo, target):
    // STAGE 1 — packing-bit precedence (int8/bf16 packed paths short-circuit)
    if any_bf16_pack_flag(s):  return 1 or 2          // bf16 packed-format codes
    if any_x8_pack_flag(s):    /* continue, format 2 */
    // STAGE 2 — dense dtype dispatch via jt[prim-2] @ 0xae0d6f4 (22 entries, cmp 0x15)
    switch (prim - 2):
        int8     -> 6
        int4     -> 6 (signed) / 8 (unsigned)
        F8E5M2   -> 5 / 7
        F8E4M3Fn -> 3 (native) / 9 (converted)
        fp8 fnuz -> 10
        F32      -> 4
        default  -> FATAL (matmul_data_format.cc line 0x129)
```

| dtype | `MatmulDataFormat` | packed-VLATCH path | latch GainLatchMode group | Confidence |
|---|---|---|---|---|
| bf16 | 1 (`kBf16`) | `VpackCBf16` pairs | `_PACKED_BF16` | HIGH |
| fp32 | 4 (`kF32`) | none (½ throughput, ×2.0 precision) | `_NO_XPOSE_F32`/`_HI`/`_LOW` | HIGH |
| int8 (x8) | 6 | `VmpackCLow` ×8 | `_NO_XPOSE_S8`/`_U8` | MEDIUM |
| int4 (x4) | 6/8 | x4 packed VLATCH | `_NO_XPOSE_S4`/`_U4`/`_NIBBLE` | MEDIUM |
| fp8 E4M3Fn | 3/9 | `_PACKED_E4M3FN` | `_F8E4M3*_TO_BF16` | MEDIUM |
| fp8 E5M2 | 5/7 | `_PACKED_E5M2` | `_F8E5M2_TO_BF16` | MEDIUM |

> **NOTE —** the jump-table *targets* (`0x1307bec0`…`0x1307bf1f`) are byte-exact, but this build's `PrimitiveType` ordinals are the in-libtpu enum (`prim - 2` index), not the upstream XLA values. The dtype labels above are inferred from the resulting format code matching the `MatmulDataFormat` enum (1=bf16, 4=F32, 5=F8E5M2, 6=int8, etc.), so the int4/fp8 rows are MEDIUM. `GetConvPrecision` (`0x131916e0`) compares `primitive_util::SignificandWidth` against the `PrecisionConfig` and returns the 0/1 index feeding the ×2.0/×1.0 cycle multiplier.

> **QUIRK —** int8 and int4 do **not** get distinct `MatmulDataFormat` *enum members* — they reuse format codes 6/8 and select the x8/x4 *opcode variant* via the `ConvolutionLoweringStrategy` packing booleans instead. The dtype paths are thus split across two mechanisms: fp variants ride the `MatmulDataFormat` enum, integer variants ride the strategy bool-vector. A reimplementer who models all dtypes as enum members will have no place to put the int4-unsigned-vs-signed distinction, which lives in the strategy flags.

---

## Related Components

| Name | Relationship |
|---|---|
| `DotCanonicalizer` (HLO pass) | rewrites `kDot` → `kConvolution` upstream; covered by [RaggedDot and Convolution Geometry Lowering](raggeddot-convolution.md) |
| `FusedSpatialMajorConvolution` | subclass used when the conv is an output-fusion root; emits the bias/activation epilogue per chunk |
| `MxuAssigner` | binds `MxuSequence`s to physical MXU banks and latch indices before `PackLatches` |
| `MxuLmrTransform` | fuses `(latch, matmul, matres)` triples into `kVectorMatmulLmr` after packing |
| `RotatedPincerEmitter` family | flag-gated alternative outer wrapper for collective matmul; calls a standard MMA functor for the per-shard MXU emission (not a separate MXU path) |

---

## Cross-References

- [The TPU Compiler](overview.md) — the five-phase dialect descent this lowering sits inside
- [tpu → LLO ODS Lowering](tpu-to-llo-ods.md) — the MXU build factories and the four register enums (`MatmulMode`/`GainLatchMode`/GMR/MSR) these ops carry; the gain/staging table
- [RaggedDot and Convolution Geometry Lowering](raggeddot-convolution.md) — the `kDot` → `kConvolution` rewrite and dimension-number mapping upstream of this page
- [Fusion Patterns](fusion-patterns.md) — the output-fusion forming the bias/activation chain above the conv
- [Mosaic VectorLayout](mosaic-vectorlayout.md) — the alternative Mosaic/Pallas matmul layout path
- [MXU Slot](../isa/slot-mxu.md) — bundle-slot bit encoding of the matmul ops this lowering emits
- [Matprep, IAR, and Latch Sub-Slots](../isa/slot-matprep-iar-latch.md) — slot encoding of the latch/matprep ops and the MSR/GMR fields
- [MXU Latency Overview](../cost/mxu-latency-overview.md) — the per-format base-cycle table the tile-cost formula layers the ×1/×2 precision multiplier onto
- [Matmul Mode Modifiers](../cost/matmul-mode-modifiers.md) — the cost-side view of the `MatmulMode` axis the `ConvMatmulModes` comparator sorts on
- [Learned Cost Model Client](../cost/learned-cost-model-client.md) — why the `ML_PGN_V` cost-model type falls back to the classic data tables in this build
