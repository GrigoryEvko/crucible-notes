# RaggedDot and Convolution Geometry Lowering

> *All addresses on this page apply to `libtpu.so` v0.0.40 (`libtpu-0.0.40-cp314`, build-id `89edbbe81c5b328a958fe628a9f2207d`, 781,691,048 B, not stripped, ELF x86-64, `libtpu_lts_20260413_b_RC00`). Other versions will differ. All findings derive from static analysis of the binary.*

## Abstract

XLA's `kRaggedDot` is a grouped matmul: a single contraction whose contracting (or non-contracting) axis is partitioned into a ragged set of variable-length groups described by a `group_sizes` operand. There is no MXU instruction for a ragged contraction, so the TPU compiler lowers it to something the hardware already knows how to emit: a **windowed convolution** whose spatial window walks the ragged axis, plus a boolean **iteration mask** that zeroes the cross-group products the dense convolution unavoidably computes. The pass that does this is `xla::jellyfish::RaggedDotExpander`, an HLO-level pass living in `platforms/xla/service/jellyfish/ragged_dot_expander.cc` (the source path is embedded verbatim in every error site).

The expander's central data structure is the `RaggedConvSpec` (`xla::jellyfish::(anonymous namespace)::RaggedConvSpec`), produced by `FromRaggedDot`. It is the convolution geometry — a `ConvolutionDimensionNumbers`, a `Window`, the conv output `Shape`, the feature/batch group counts, a `PrecisionConfig`, and a one-byte contraction-mode selector — reconstructed from the `RaggedDotDimensionNumbers` of the source instruction. The expansion then has two orthogonal choices. The first is the **contraction mode** (`RaggedConvContractionMode`, two arms: `reduce` and `dynamic_slice`), which picks how the masked windows are folded into the dense output. The second is whether to attach an **iteration mask** at all (the `xla_tpu_impure_use_iteration_mask` knob, AUTO=ON on Tc-version ≥ 3), and on megacore parts whether to split the mask across the two cores. After the expander runs, the emitted `kConvolution` flows through the normal conv → LLO path, where `SpatialMajorConvolution` chooses a `ConvolutionLoweringStrategy` from the window geometry.

This page documents three things a reimplementer must reproduce: the `RaggedConvSpec` geometry and the `PipelineWindowSpec` window-bounds parse; the two contraction-mode arms (the `reduce` arm with its `MaskAggregatorConfig` + `CreateReduce`/`CreateAddComputation`, and the `dynamic_slice` arm with `DynamicSliceMaskedConv` + an `update_into` accumulator); and the `Convolve` → LLO `SpatialMajorConvolution` emission. The companion knob/gate machinery (the three `Should…IterationMask` predicates and the impure-flag plumbing) is summarized here and owned in full by the impure-flags page.

For reimplementation, the contract is:

- **The `RaggedConvSpec` layout** — which conv geometry fields it carries, at which byte offsets, and how `FromRaggedDot` derives them from `RaggedDotDimensionNumbers`.
- **The window model** — the `g/m/k/n` `PipelineWindowSpec`, parsed from a 4-element string vector, and how it parameterizes the convolution window vs. the LLO lowering strategy.
- **The two contraction-mode arms** — `reduce` (mask-aggregator + reduce-add) and `dynamic_slice` (`DynamicSliceMaskedConv` + scatter-accumulate), selected by the `RaggedConvSpec`'s mode byte.
- **The iteration mask** — the `Iota + Broadcast + Compare(≥)·Compare(<) + And` boolean mask, consumed by a `kSelect(mask, conv, 0)` ternary, and when it is and is not attached.
- **The LLO hand-off** — the `kConvolution` the expander emits, and how `SpatialMajorConvolution` turns the window into a `ConvolutionLoweringStrategy`.

| | |
|---|---|
| **Pass** | `xla::jellyfish::RaggedDotExpander` (HLO pass) |
| **Pass object** | `0x30` bytes; `+0x08` `use_iteration_mask` bool, `+0x09` `RaggedConvContractionMode`, `+0x10`/`+0x20` `window_bounds` `vector<string>` |
| **Pass entry** | `RunImpl` `0x10fae060` |
| **Pass wiring** | `PostMainFusionHloOptimize` `0x10966560` (AddPass call site at `0x109673b3`) → `AddPass<…>` `0x1096d2e0` (`make_unique` `0x1096e360`) |
| **Geometry builder** | `FromRaggedDot` (error lambda `$_0` `0x10fb2160`); `ExpandShape` `0x10fb2360` |
| **Expander body** | `ExpandRaggedDot` `0x10fafa20` |
| **Mask builder** | `CreateOutputMask` `0x10fb2900` |
| **Conv+select fusion** | `CreateConvolutionSelectFusion` `0x10fb31e0`; `DynamicSliceMaskedConv` `0x10fb6a00` (dynamic_slice arm) |
| **LLO emitter** | `xla::jellyfish::SpatialMajorConvolution` (`SetLoweringStrategy` `0x13167e40`) |
| **Source file** | `platforms/xla/service/jellyfish/ragged_dot_expander.cc` |
| **IR level** | HLO (expander); MLO/LLO (`SpatialMajorConvolution`) |

---

## Pass Wiring and Member Layout

### Purpose

`RaggedDotExpander` is constructed and added in `PostMainFusionHloOptimize` (function entry `0x10966560`, AddPass call site at `0x109673b3`), after the main fusion pass, via `HloPassPipeline::AddPass<RaggedDotExpander, bool, RaggedConvContractionMode, vector<string>, Target&>` (`0x1096d2e0`). Three impure compile knobs feed the constructor; they are read once at pipeline-build time, not per instruction.

### Entry Point

```text
PostMainFusionHloOptimize          0x10966560  (AddPass block at 0x109673b3)
  ├─ RaggedDotExpanderShouldUseIterationMask  0x1d6b5d60  ── use_iteration_mask arg
  ├─ FLAGS_xla_tpu_impure_contract_ragged_conv_with  0x223a7cf8  ── RaggedConvContractionMode arg
  ├─ FLAGS_xla_tpu_impure_ragged_dot_window_bounds   0x223a7d58  ── vector<string> g/m/k/n arg
  └─ AddPass<RaggedDotExpander,…>    0x1096d2e0
       └─ make_unique<RaggedDotExpander>  0x1096e360  (0x30 bytes)
            ├─ +0x08  use_iteration_mask  (bool)
            ├─ +0x09  contraction mode    (RaggedConvContractionMode)
            └─ +0x10 / +0x20  window_bounds (vector<string>)
```

### Member Layout

| Field | Offset | Type | Meaning |
|---|---|---|---|
| vtable | `+0x00` | ptr | `HloModulePass` vtable |
| `use_iteration_mask` | `+0x08` | bool | gate the masked lowering; written `mov %al,0x8` in the ctor |
| contraction mode | `+0x09` | enum (1 byte) | `reduce`=0 / `dynamic_slice`=1; written `mov %cl,0x9` |
| `window_bounds` | `+0x10`/`+0x20` | `vector<string>` | 4 strings `{g,m,k,n}` |

> **NOTE —** the three knobs are *impure* (read at compile time, not part of the compilation environment proto). `use_iteration_mask` and `enable_masked_fusion_iteration_skipper` are `AutoOr<bool>` read directly off their `FlagImpl+0x58` cache; `contract_ragged_conv_with` is a `RaggedConvContractionMode` `FixedOptionSetFlag`; `ragged_dot_window_bounds` is a `vector<string>`. The full impure-flag mechanics (AUTO polarity, the `FlagImpl` read path) are owned by [Registry-Mediated Flags](../config/registry-mediated-flags.md).

### The Three Iteration-Mask Gates

Two distinct gate functions exist because the mask is consulted at two pipeline levels — the HLO expander and the LLO conv emitter — and they read different predicates. All three gate on TPU Tc-version ≥ 3 first (`mov 0x8(view),%rax; cmpl $0x3,(%rax); jl ret-false`).

| Gate | Address | Predicate | Consumer |
|---|---|---|---|
| `RaggedDotExpanderShouldUseIterationMask` | `0x1d6b5d60` | v≥3 AND `use_iteration_mask` (AUTO=ON) | `PostMainFusionHloOptimize` `0x10966560` (the `+0x08` ctor arg) |
| `ShouldUseIterationMask` | `0x1d6b5dc0` | v≥3 AND (`use_iteration_mask` OR `enable_masked_fusion_iteration_skipper`) | `SpatialMajorConvolution` ctor / Emit (LLO level) |
| `ShouldEnableMaskedFusionIterationSkipper` | `0x1d6b5d20` | v≥3 AND plain-bool skipper | (the skipper disjunct above) |

The expander gate (`0x1d6b5d60`) consults only `use_iteration_mask`; the LLO gate (`0x1d6b5dc0`) also honors the masked-fusion-iteration-skipper. The AUTO=ON polarity is the `AutoOr` idiom: the cached word is read, and the result is `(word & 0x101) != 0x100` — true unless the flag is explicitly present-and-false.

```c
function ShouldUseIterationMask(env, topology):     // 0x1d6b5dc0
    if topology.tc_version < 3: return false         // mov 0x8(a2); cmpl $3; jl
    // disjunct 1: use_iteration_mask (AutoOr<bool>, AUTO=ON)
    if FLAGS_use_iteration_mask present:
        word = ~ReadOneWord(FLAGS_xla_tpu_impure_use_iteration_mask)
        if (word & 0x101) == 0: return true
    // disjunct 2: the masked-fusion-iteration-skipper (plain bool)
    if topology.tc_version >= 3:
        if ReadOneBool(FLAGS_xla_tpu_impure_enable_masked_fusion_iteration_skipper):
            return true
    // final: AUTO resolves to ON
    word = ReadOneWord(FLAGS_xla_tpu_impure_use_iteration_mask)
    return (word & 0x101) != 0x100
```

---

## The RaggedConvSpec Geometry

### Purpose

`RaggedConvSpec` is the convolution that a `RaggedDot` becomes. It is an anonymous-namespace struct in `ragged_dot_expander.cc`, built by `FromRaggedDot(const HloInstruction*, RaggedConvContractionMode, const Target&)`. It carries everything `HloInstruction::CreateConvolve` needs plus the mode selector. Its field offsets are recovered directly from the `CreateConvolve` call in `CreateConvolutionSelectFusion` (`0x10fb31e0`), where each conv argument is loaded from a fixed displacement off the spec pointer (`a7`/`v108`).

### Layout

The offsets below are read from the disassembly of the single `CreateConvolve` call site (`0x10fb31e0`, line ~1288) and the surrounding loads. The spec pointer is held as a `int64*` (`v108`), so the decompiler renders the conv sub-object pointers as **qword indices** — `v108 + 6`, `v108 + 24`, `v108 + 29`, `v108 + 72` — which are byte offsets `8 ×` the index. The contraction-mode selector is the lone exception: it is read with a byte access `*(int8*)(a7 + 24)`, i.e. genuine **byte offset 24**, not qword index 24. The call is `CreateConvolve(builder, lhs, rhs, feature_group_count, batch_group_count, &window, &dim_numbers, &precision_config, &sparsity, 0)` with `v108+24`, `v108+6`, `v108+72` as the window/dim-numbers/precision args (line 1288) and `Shape::Shape(&v417, v108+29)` as the output shape (line 1272, also `a7+232` at line 453):

| Field | Byte offset | Qword index in decompile | Type | Role in `CreateConvolve` |
|---|---|---|---|---|
| contraction-mode byte | `+0x18` (24) | — (byte access `*(int8*)(a7+24)`) | u8 | `0`=reduce arm, `1`=dynamic_slice arm; tested to pick the fold |
| `dim_numbers` | `+0x30` (48) | `v108 + 6` | `ConvolutionDimensionNumbers` | conv dim numbers arg |
| `window` | `+0xC0` (192) | `v108 + 24` | `Window` | window arg |
| conv `Shape` | `+0xE8` (232) | `v108 + 29` | `Shape` | output/conv shape (`Shape::Shape(&v417, a7+232)`) |
| `feature_group_count` | `+0x230` (560) | `v108[70]` | i64 | conv feature-group arg |
| `batch_group_count` | `+0x238` (568) | `v108[71]` | i64 | conv batch-group arg |
| `precision_config` | `+0x240` (576) | `v108 + 72` | `PrecisionConfig` | precision arg |

> **GOTCHA —** the contraction-mode byte sits at byte 24, but the `Window` proto is at qword index 24 — *byte 192*, not byte 24. The decompiler renders both as a literal `24`, but the mode is a one-byte read (`*(int8*)(a7+24)`) and the window is a qword-indexed sub-object pointer (`v108 + 24` → `a7 + 192`); they are distinct fields ~168 bytes apart. Multiply every qword index by 8 to recover the byte offset. The byte-offset column is HIGH/MEDIUM where the load is unambiguous.

`RunImpl` confirms the spec's component set by its destructors: after each expansion attempt it tears down a `PrecisionConfig`, a `Shape`, a `Window`, and a `ConvolutionDimensionNumbers` (`0x10fae060`, lines ~1408-1419) — exactly the four conv sub-objects above.

### FromRaggedDot

`FromRaggedDot` validates the `RaggedDotDimensionNumbers` and assembles the spec; the only part separately symbolized is its error lambda `$_0` (`0x10fb2160`), which formats `"Failed to create RaggedConvSpec from ragged_dot instruction %s: %s, got %lld"` (`MakeErrorImpl<3>`, line 145 of the source). The validation that drives the lambda lives inline in `RunImpl`:

```c
function RunImpl(module, …):                         // 0x10fae060
    for comp in module.MakeComputationPostOrder():    // 0x10fae060+250
      for inst in comp where inst.opcode == kRaggedDot:
        // a ragged BATCH dim must already be gone
        CHECK(ragged_dot_mode != RaggedDotMode::kBatch)        // "Ragged dot with a ragged batch dim
                                                               //  should not reach the expander."
        // exactly-one-of dim validation (each failure -> FromRaggedDot $_0 error)
        CHECK(num_contracting_dims == 1)              // "number of contracting dimensions should be 1"
        CHECK(num_lhs_noncontracting == 1)            // "number of lhs non-contracting dimensions should be 1"
        CHECK(num_rhs_noncontracting == 1)            // "number of rhs non-contracting dimensions should be 1"
        spec = FromRaggedDot(inst, mode, target)      // build RaggedConvSpec
        if use_iteration_mask:                         // member +0x8 == 1
            CHECK(window_bounds.size() == 4)          // "window_bounds.size() == 4"
            pws.g = SimpleAtoi(window_bounds[0])       // safe_strto64_base(…,10)
            pws.m = SimpleAtoi(window_bounds[1])
            pws.k = SimpleAtoi(window_bounds[2])
            pws.n = SimpleAtoi(window_bounds[3])
        ExpandRaggedDot(inst, spec, use_iteration_mask,
                        optional<PipelineWindowSpec>(pws), nullopt, target)   // 0x10fafa20
```

> **QUIRK —** the `kBatch` ragged mode is a hard `CHECK`, not a graceful fallback. A ragged *batch* dimension is expected to be eliminated by an earlier pass; if one survives to the expander it aborts. Only ragged *contracting* and ragged *non-contracting* modes reach `FromRaggedDot`, and each demands exactly one of its respective dim class.

### The PipelineWindowSpec window-bounds

When `use_iteration_mask` is set, the expander parses the four `window_bounds` strings into a `PipelineWindowSpec {g, m, k, n}` by `absl::SimpleAtoi` / `safe_strto64_base(…, 10)`. The mapping is positional and fixed (each parse target is named in its `CHECK` string at `0x10fae060+1295…1359`):

| Index | Field | Source CHECK string |
|---|---|---|
| `window_bounds[0]` | `pipeline_window_spec.g` | `"absl::SimpleAtoi(window_bounds[0], &pipeline_window_spec.g)"` |
| `window_bounds[1]` | `pipeline_window_spec.m` | `"…window_bounds[1], &pipeline_window_spec.m)"` |
| `window_bounds[2]` | `pipeline_window_spec.k` | `"…window_bounds[2], &pipeline_window_spec.k)"` |
| `window_bounds[3]` | `pipeline_window_spec.n` | `"…window_bounds[3], &pipeline_window_spec.n)"` |

`g/m/k/n` are the grouped-matmul axes (groups, lhs-non-contracting M, contracting K, rhs-non-contracting N). The `PipelineWindowSpec` is the *pipeline* window — distinct from the conv `Window` proto inside the `RaggedConvSpec` — and is threaded as an `optional` argument into `ExpandRaggedDot` and on to `CreateConvolutionSelectFusion`. It is only populated on the masked path; the unmasked path passes `nullopt`.

---

## The Contraction-Mode Arms

`RaggedConvContractionMode` is a `FixedOptionSetFlag` with exactly two options, registered in `GetRaggedConvContractionModeParser` (`0x1db15340`):

```text
"reduce"        -> 0      // mask-aggregator + reduce-add fold
"dynamic_slice" -> 1      // DynamicSliceMaskedConv + scatter-accumulate
```

The mode byte rides in the `RaggedConvSpec` (`+0x18`) and is tested as `*(a7+24)` throughout `CreateConvolutionSelectFusion` to switch between the two folds. Both arms share the same front half: the dense `kConvolution` over the windowed operands, then a per-window mask. They differ in how masked windows are combined into the dense output.

### Reduce Arm (`reduce`, mode 0)

The reduce arm builds a fused computation that convolves, selects by the iteration mask, broadcasts a zero, and reduce-adds across the window. Recovered from `CreateConvolutionSelectFusion` (`0x10fb31e0`) and the `MaskAggregatorConfig` setup in `ExpandRaggedDot`:

```c
// inside CreateConvolutionSelectFusion, *(spec+24) == 0  (reduce)
function ReduceArmFusionBody(spec, mask, …):           // 0x10fb31e0
    kernel       = Parameter(0, "kernel")
    activations  = Parameter(1, "activations")
    output_mask  = Parameter(2, "output_mask")
    conv  = CreateConvolve(builder, kernel, activations,
                           spec.feature_group_count, spec.batch_group_count,
                           &spec.window, &spec.dim_numbers, &spec.precision, &sparsity, 0)
    zero  = CreateBroadcast(CreateConstant(Zero(elem_type)))   // 0
    sel   = CreateTernary(108 /*kSelect*/, output_mask, conv, zero)  // select(mask, conv, 0)
    addc  = CreateAddComputation(elem_shape)                   // scalar add reducer
    out   = CreateReduce(sel, init=zero, dims={window}, addc)  // sum masked windows
    return Build(out)                                          // wrapped in a kFusion
```

The `MaskAggregatorConfig` (a `xla::jellyfish` proto, arena `DefaultConstruct` in `ExpandRaggedDot` at `0x10fafa20`) parameterizes the aggregation. Two of its fields are written directly: a flag byte at `+44` set to `0`, and a packed pair at `+48` set to `0x200000000` (i.e. `{0, 2}` as two 32-bit halves), with presence bits `(+16) |= 0xE`. The same setup block also configures a `WindowConfig` (`+184 = 1`, presence `(+17) |= 0x20`). On megacore parts the spec is further split (see below).

> **NOTE —** the `MaskAggregatorConfig` is the SparseCore/TensorCore-level realization of the iteration mask: it tells the emitted kernel how the per-group masks are aggregated across conv windows. Only its two written fields (`+44`, `+48 = 0x200000000`) are recovered; the full proto schema is not decoded (LOW confidence on field semantics beyond the observed writes).

### Dynamic-Slice Arm (`dynamic_slice`, mode 1)

When `*(spec+24) == 1`, the fold is delegated to `xla::jellyfish::DynamicSliceMaskedConv` (`0x10fb6a00`, called from `CreateConvolutionSelectFusion` line ~2003). This arm adds two extra fusion parameters — `group_starts` (`Parameter(3)`) and `update_into` (`Parameter(4)`) — and scatters each group's masked conv result into the running accumulator at the group's start offset rather than reduce-summing windows:

```c
// inside CreateConvolutionSelectFusion, *(spec+24) == 1  (dynamic_slice)
function DynamicSliceArm(spec, mask, group_starts, update_into, …):    // 0x10fb31e0 -> DynamicSliceMaskedConv 0x10fb6a00
    CHECK(group_starts_param != nullptr)   // "group_starts_param != nullptr"
    CHECK(update_into_param  != nullptr)   // "update_into_param != nullptr"
    // build the conv + select(mask, conv, 0) as in the reduce arm, then:
    DynamicSliceMaskedConv(builder, spec, group_starts, update_into, …)
    // scatters the masked conv into update_into at the per-group start offset
```

The two arms produce numerically equivalent results for a correct ragged contraction; `dynamic_slice` avoids the full window reduce by writing each group's contribution into place, which is preferable when groups are large and sparse. The choice is purely the `xla_tpu_impure_contract_ragged_conv_with` knob.

### ConvDimNumbers

The `ConvolutionDimensionNumbers` is the spec's `+0x06` field and is what makes the ragged contracting axis a convolution *spatial* dim. It is reconstructed from the source `RaggedDotDimensionNumbers` in `ExpandRaggedDot` (`RaggedDotDimensionNumbers::RaggedDotDimensionNumbers((…)v313, 0, ragged_dot_dimension_numbers(inst))`, `0x10fafa20+1063`) and copy-constructed several times in `CreateConvolutionSelectFusion` (`0x10fb31e0+1631/1917/1942`). The window's ragged dim is sanity-checked against the kernel/output window bounds: `CHECK(conv_window_config.output_window_bounds_size() == 3)`, `CHECK(conv_window_config.kernel_window_bounds_size() == 3)`, and `CHECK(ragged_dim_window_bound <= max_ragged_dim_window_bound)`.

---

## ExpandRaggedDot — the Subgraph

### Purpose

`ExpandRaggedDot` (`0x10fafa20`) is the routine that replaces the `kRaggedDot` instruction with the convolution subgraph. It builds, in order: the group-boundary machinery (from `group_sizes`), the windowed-and-padded operands, the iteration mask(s), and the conv+select fusion. The whole thing ends in `HloComputation::ReplaceInstruction(inst, fused_conv)`.

### Algorithm

```c
function ExpandRaggedDot(inst, spec, use_iteration_mask, pws, dyn, target):   // 0x10fafa20
    // 0. optional control dependency on operand 2 (group_sizes) via AfterAll+AddDependency
    gs = inst.mutable_operand(2)                       // group_sizes
    if has_control_dep: gs = AddDependency(gs, AfterAll())

    // 1. group_ends  = reduce-window prefix over group_sizes
    CHECK(gs.shape.rank == 1)                           // "group_sizes should be rank 1"
    gs2  = Bitcast(gs, AppendMajorDimension(gs.shape))  // conv layout
    zero = CreateConstant(LiteralUtil::Zero(elem))
    addc = CreateAddComputation(...)
    group_ends = CreateReduceWindow(gs2, zero, window, addc)   // running group offsets
    group_ends = AddInstruction(... UniquifyName "group_ends")

    // 2. group boundaries fusion (group_starts from group_ends)
    CHECK(group_ends.shape.rank == 2)                   // "group_ends should be rank 2"
    bc   = Bitcast(StripShape(group_ends))
    off  = CreateConstant(LiteralUtil::CreateR1<int>({...}))   // group offsets literal
    // pad_add_fusion: Pad(group_ends) AND Pad(slice) -> kAnd -> kFusion "pad_add_fusion"
    pad1 = CreatePad(group_ends, zero, cfg)             // "group boundaries fusion"
    pad2 = CreatePad(slice,      zero, cfg)
    bnd  = CreateBinary(kAnd, pad1, pad2)               // "group_starts_one_longer should be rank 1"
    bnd_fusion = CreateFusion(Build(bnd))               // -> group starts/ends

    // 3. iteration mask(s)
    group_starts = Slice(bnd_fusion, ...)               // group_starts from boundaries
    out_shape    = AppendMinorDimension(operand0.shape) // conv output layout
    mask  = CreateOutputMask(group_ends_inst, group_starts, out_shape, target)   // 0x10fb2900
    if use_iteration_mask AND Megacore AND num_cores >= 2:
        mask2 = CreateOutputMask(..., AppendMinorDimension x2, ...)              // per-core mask
        // operand0 also wrapped in AfterAll + AddDependency for the second core

    // 4. the conv + select fusion (picks reduce vs dynamic_slice from spec.mode)
    fused = CreateConvolutionSelectFusion(operand0, operand1, mask,
                                          out_shape, spec, pws, target, use_iteration_mask)  // 0x10fb31e0

    // 5. megacore split: tag the MaskAggregator + fusion with the split dim
    if use_iteration_mask AND Megacore AND num_cores >= 2:
        wd = windowing_util::MakeWindowDescription(target, fused.shape)
        if even_split:  AddMatchingMegacoreSplitDimensionsToMaskAggregatorAndFusion(fused, …)
        else:           AddMegacoreSplitDimension(fused, …)  // + MegacoreConfig

    return ReplaceInstruction(inst, fused)
```

> **QUIRK —** on a megacore part with ≥ 2 cores, the expander builds **two** masks (`CreateOutputMask` is called twice with progressively appended minor dimensions) and either `AddMatchingMegacoreSplitDimensionsToMaskAggregatorAndFusion` (when the ragged dim splits evenly across cores) or `AddMegacoreSplitDimension` + a `MegacoreConfig` (otherwise). A single-core or non-megacore reimplementation needs only the first mask and neither split helper. The split decision is a divisibility test of the ragged-dim window bound by the per-core split.

### The Iteration Mask — CreateOutputMask

`CreateOutputMask` (`0x10fb2900`) builds the boolean mask that zeroes cross-group products. It is the structural heart of the lowering: a dense convolution over the windowed ragged axis computes the full block-Cartesian product, and the mask keeps only the `[group_start, group_end)` band per output position.

```c
function CreateOutputMask(group_ends, group_starts, out_shape, target):   // 0x10fb2900
    body builds a kFusion computation:
      stripped_ends = Parameter(0, "stripped_group_ends")    // upper bounds
      group_starts  = Parameter(1, "group_starts")           // lower bounds
      lo   = CreateBroadcast(group_starts, out_shape)
      iota = CreateIota(out_shape, iota_dim)                 // output-position index
      ge   = CreateCompare(lo, iota, 4 /*kGe*/)              // iota >= group_start
      hi   = CreateBroadcast(stripped_ends, out_shape)
      lt   = CreateCompare(hi, iota, 5 /*kLt*/)              // iota <  group_end
      mask = CreateBinary(13 /*kAnd*/, ge, lt)               // in-band <=> in this group
    return CreateFusion(Build(mask))                          // boolean mask, one kFusion
```

> **GOTCHA —** the compare directions are `kGe` (opcode 4) for the lower bound and `kLt` (opcode 5) for the upper bound, against a single `kIota` of output positions. The band is half-open `[group_start, group_end)`. The mask is then `kAnd`'d (opcode 13) and wrapped in its own `kFusion` so the boolean computation fuses independently of the conv. Getting the half-open convention or the compare polarity wrong silently double-counts or drops a boundary element of each group.

The mask feeds `CreateConvolutionSelectFusion`, where `CreateTernary` with opcode `108` (`kSelect`) computes `select(mask, conv, broadcast_zero)` — the masked conv result. The reduce arm then reduce-adds it; the dynamic_slice arm scatters it.

---

## Convolve → LLO: SpatialMajorConvolution

### Purpose

The expander leaves behind an ordinary `kConvolution` (inside a `kFusion`). It flows through the standard conv → MLO/LLO path. At the LLO level, `xla::jellyfish::SpatialMajorConvolution` is the emitter that turns that convolution into a tiled, spatially-major matmul-accumulate loop. It re-consults the iteration-mask predicate via the LLO gate `ShouldUseIterationMask` (`0x1d6b5dc0`), which is why that gate also honors the masked-fusion-iteration-skipper.

### Lowering-Strategy Selection

`SpatialMajorConvolution` holds a `ConvolutionLoweringStrategy` at object offset `+0x882` (2178). It is set two ways:

```c
function SpatialMajorConvolution::SetLoweringStrategy(strategy):     // 0x13167e40
    this[+0x882] = strategy                          // store the 24-byte strategy struct

function SpatialMajorConvolution::UpdateLoweringStrategyWithWindowInfo(   // 0x13167e80
        ragged_dim_bound, lhs_bound, rhs_bound, is_ragged):
    s = convolution_util::GetConvolutionLoweringStrategy(    // window geometry -> strategy
            this.window_field, this.spatial_field,
            ragged_dim_bound, /*flag=*/1, is_ragged + 256, …)
    this[+0x882] = s
```

`UpdateLoweringStrategyWithWindowInfo` derives the strategy from the window bounds via `convolution_util::GetConvolutionLoweringStrategy` — this is where the `g/m/k/n` window geometry becomes a concrete LLO tiling decision. The companion `GetLoweringStrategyString` (`0x13167e60`) renders it for diagnostics (`convolution_util::GetLoweringStrategyString(this, this+2178)`).

### Function Map

| Function | Address | Role |
|---|---|---|
| `SpatialMajorConvolution::SetLoweringStrategy` | `0x13167e40` | store strategy at `+0x882` |
| `SpatialMajorConvolution::GetLoweringStrategyString` | `0x13167e60` | render strategy for diagnostics |
| `SpatialMajorConvolution::UpdateLoweringStrategyWithWindowInfo` | `0x13167e80` | window geometry → `GetConvolutionLoweringStrategy` → store |
| `SpatialMajorConvolution::RoundUpWindowBoundToFactorAndCompact` | `0x1315b2a0` | round ragged window bound up to a hardware factor |
| `SpatialMajorConvolution::InitFromFusion` | `0x13155fc0` | bind emitter to the expander's `kFusion` |
| `SpatialMajorConvolution::InitFusionEmitters` | `0x13173b60` | per-operand window emitters |
| `SpatialMajorConvolution::MatrixMultiplyAccumulate` | `0x131792e0` | the MXU MMA inner emit |
| `SpatialMajorConvolution::EmitZeroByteCase` / `EmitZeroElementCases` | `0x13178100` / `0x13178020` | degenerate-window short circuits |
| `SpatialMajorConvolution::PopulateNestedOutputFusions` | `0x13155d40` | wire the select/reduce sub-fusions |

> **NOTE —** `MatrixMultiplyAccumulate` (`0x131792e0`) is the bridge to the MXU emitter shared with dense dot/conv. This page stops at the strategy decision; the tile-cost comparator and `EmitFunctorEnum` it dispatches into are the subject of [Dot / Conv → MXU Lowering](dot-conv-mxu-lowering.md).

---

## Function Map

| Function | Address | Role |
|---|---|---|
| `RaggedDotExpander::RunImpl` | `0x10fae060` | per-instruction validate + window parse + dispatch |
| `ExpandRaggedDot` | `0x10fafa20` | build the conv subgraph, replace the `kRaggedDot` |
| `FromRaggedDot` (error lambda `$_0`) | `0x10fb2160` | `RaggedConvSpec` build-failure formatter |
| `ExpandShape` | `0x10fb2360` | append the conv spatial dim to operand shapes |
| `CreateOutputMask` | `0x10fb2900` | `Iota + Broadcast + Compare(≥)·Compare(<) + And` mask |
| `CreateConvolutionSelectFusion` | `0x10fb31e0` | `Convolve + Select(mask,·,0)` + reduce/dynamic_slice fold |
| `DynamicSliceMaskedConv` | `0x10fb6a00` | the `dynamic_slice` arm scatter-accumulate |
| `GetRaggedConvContractionModeParser` | `0x1db15340` | `FixedOptionSetFlag` (`reduce`/`dynamic_slice`) |
| `RaggedDotExpanderShouldUseIterationMask` | `0x1d6b5d60` | expander-level mask gate |
| `ShouldUseIterationMask` | `0x1d6b5dc0` | LLO-level mask gate |
| `ShouldEnableMaskedFusionIterationSkipper` | `0x1d6b5d20` | skipper disjunct |
| `make_unique<RaggedDotExpander>` | `0x1096e360` | ctor + member layout |
| `AddPass<RaggedDotExpander,…>` | `0x1096d2e0` | pipeline wiring |

---

## Diagnostic Strings

All emitted from `platforms/xla/service/jellyfish/ragged_dot_expander.cc`. These are the central assertions a reimplementation must honor (and a debugging engineer will grep for).

| String | When | Severity |
|---|---|---|
| `Ragged dot with a ragged batch dim should not reach the expander.` | `ragged_dot_mode == kBatch` reaches `RunImpl` | FATAL CHECK |
| `number of contracting dimensions should be 1` | dim validation in `FromRaggedDot` | error Status |
| `number of lhs non-contracting dimensions should be 1` | dim validation | error Status |
| `number of rhs non-contracting dimensions should be 1` | dim validation | error Status |
| `Failed to create RaggedConvSpec from ragged_dot instruction %s: %s, got %lld` | `FromRaggedDot` `$_0` | error Status |
| `window_bounds.size() == 4` | masked path, malformed `ragged_dot_window_bounds` | FATAL CHECK |
| `absl::SimpleAtoi(window_bounds[{0..3}], &pipeline_window_spec.{g,m,k,n})` | non-numeric window bound | error Status |
| `group_sizes should be rank 1` | `group_sizes` operand shape | error Status |
| `group_ends should be rank 2` | reduce-window output shape | error Status |
| `group_starts_one_longer should be rank 1` | boundary fusion shape | error Status |
| `conv_window_config.output_window_bounds_size() == 3` | conv window assembly | FATAL CHECK |
| `conv_window_config.kernel_window_bounds_size() == 3` | conv window assembly | FATAL CHECK |
| `ragged_dim_window_bound <= max_ragged_dim_window_bound` | window-bound clamp | FATAL CHECK |
| `group_starts_param != nullptr` / `update_into_param != nullptr` | `dynamic_slice` arm preconditions | FATAL CHECK |

---

## Related Components

| Name | Relationship |
|---|---|
| `RaggedDotExpander` | the HLO pass documented here |
| `SpatialMajorConvolution` | the LLO emitter that lowers the `kConvolution` the expander produces |
| `MaskAggregatorConfig` / `WindowConfig` / `MegacoreConfig` | `jellyfish` backend-config protos written by `ExpandRaggedDot` |
| `RaggedConvContractionMode` | the `reduce` / `dynamic_slice` `FixedOptionSetFlag` |
| MXU dot/conv emitter | `MatrixMultiplyAccumulate` hands the windowed conv to the shared MXU path |

---

## Cross-References

- [Compiler Overview](overview.md) — where `RaggedDotExpander` sits in the post-fusion HLO pipeline
- [Dot / Conv → MXU Lowering](dot-conv-mxu-lowering.md) — the tile-cost comparator / `EmitFunctorEnum` that `MatrixMultiplyAccumulate` dispatches into
- [TPU → LLO ODS](tpu-to-llo-ods.md) — the LLO op surface `SpatialMajorConvolution` emits against
- [Fusion Patterns](fusion-patterns.md) — the `kFusion` wrapping conventions the mask and conv+select fusions follow
- [Custom-Call Lowering](custom-call-lowering.md) — sibling Part V lowering path for ops with no native MXU form
- [Registry-Mediated Flags](../config/registry-mediated-flags.md) — the impure/`AutoOr` flag read paths behind `use_iteration_mask` and `contract_ragged_conv_with`
- [back to index](../index.md) — Part V — Compiler: Lowering & Optimization Passes
