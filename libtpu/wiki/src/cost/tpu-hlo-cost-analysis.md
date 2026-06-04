# TpuHloCostAnalysis

> *Addresses apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (BuildID md5 `89edbbe81c5b328a958fe628a9f2207d`). The binary is **not** stripped — every symbol below is a demangled C++ name. `.text`/`.rodata` VMA == file offset; `.data.rel.ro` VMA − 0x200000 == file offset. Other versions differ.*

## Abstract

`xla::jellyfish::TpuHloCostAnalysis` is the HLO-level flop/byte/transcendental cost model. It is a thin **subclass** of the generic XLA `xla::HloCostAnalysis` (vtable @ `0x218fb618`, ctor @ `0x130a1620`): it inherits every per-opcode emitter unchanged and overrides **exactly three** — `HandleGather`, `HandleScatter`, `HandleCustomCall`. Each `Handle*` walks one HLO instruction and writes three `float` cost properties into a per-instruction `Properties` flat-hash-map: flop count (`+0x50`), transcendental-op count (`+0x54`), and bytes accessed (`+0x58`). The familiar reference frame is upstream XLA's `HloCostAnalysis` exactly — same `kFlopsKey` / `kTranscendentalsKey` / `kBytesAccessedKey` properties, same `2·M·N·K` dot model — with a TPU-specific patch on the three ops whose silicon cost the generic model gets wrong: gather and scatter (priced as chunk-granule DMA, not flops) and custom-call (priced by a target-keyed registry).

These flop properties are one of *two* cost surfaces the TPU compiler builds over the same HLO, and the page must not conflate them. The `Properties` flops here are the **fusion-priority** input: `flop_count(inst)` (@ `0x1e4841e0`) reads `Properties[inst].float[+0x50]` and feeds the convolution-cycle estimate that [NormalizedComputationCost](normalized-computation-cost.md) caches per `unique_id`. The *other* surface is the **bundle-occupancy** cost — `CostModel::RecordHloCycles` (@ `0x130bbfe0`) deposits per-op throughput cycles into the 23-slot [`ResourceVector`](resource-enum.md) routed by an opcode jump table — reached through `GetHloResourcesImpl` (@ `0x130aa580`, see [GetHloResources Routing](gethloresources-routing.md)). The two share the same HLO and agree on which ops are expensive, but they are computed by different functions, store into different structures, and never share a number. This page documents the flop/byte `Properties` model and the `RecordHloCycles` opcode→slot routing that runs alongside it.

The third strand is the routing decision that selects *which* cost sub-emitter prices an op. `GetHloResourcesImpl` is a five-way dispatch: collective → network model; conv-lowerable / reduce-window → MXU output-fusion model; collective-compute fusion → its own model; else → the loop/elementwise path that ultimately calls `RecordHloCycles`. The page covers the override table, the `RecordHloCycles` opcode jump table, and the base flop formulas the overrides leave untouched.

For reimplementation, the contract is:

- The subclass shape: which three `Handle*` are overridden, the `+0x50/+0x54/+0x58` `Properties` layout, and the `model_tpu_specific_overheads` gate (this+456) that turns the overrides on.
- The base `HloCostAnalysis` flop formulas the TPU model inherits verbatim: dot, convolution (divides by **both** feature- and batch-group counts), reduce, reduce-window, and the elementwise flop-vs-transcendental classification.
- The three TPU overrides: gather/scatter chunk-ratio bytes model, scatter's added combiner flops, and the custom-call `FunctionRegistry` keyed on `custom_call_target`.
- The `RecordHloCycles` opcode→`ResourceVector`-slot jump table: the per-op `CT::Instruction` issued, the named slot it lands in, the cycle quantity, and the float-type gate that scatters add/subtract across the dedicated vector-ALU lanes.

| | |
|---|---|
| **Class** | `xla::jellyfish::TpuHloCostAnalysis` : `xla::HloCostAnalysis` |
| **Vtable / ctor** | `0x218fb618` / `0x130a1620` |
| **Overridden methods** | `HandleGather` `0x130a2de0`, `HandleScatter` `0x130a3160`, `HandleCustomCall` `0x130a35c0` |
| **Properties fields** | `+0x50` flops, `+0x54` transcendentals, `+0x58` bytes-accessed (all `float`) |
| **Flop reader** | `HloCostAnalysis::flop_count` `0x1e4841e0` → `Properties[inst].float[+0x50]` |
| **Overrides gate** | `*(byte*)(this+456)` = `model_tpu_specific_overheads`; off → tail-call base |
| **Routing dispatch** | `CostModel::GetHloResourcesImpl` `0x130aa580` (5-way) |
| **Bundle deposit** | `CostModel::RecordHloCycles` `0x130bbfe0` + jump table @ `.rodata 0xae0ebbc` |
| **Source file** | `platforms/xla/service/jellyfish/tpu_hlo_cost_analysis.cc` |

---

## GetHloResourcesImpl — the Five-Way Routing Dispatch

### Purpose

`GetHloResourcesImpl` is the front door that decides which cost sub-model prices an HLO op before it is deposited into the bundle's `ResourceVector`. It is **not** the flop path (that is the `Properties`/`Handle*` side); it is the resource-routing path that picks between the collective-network model, the MXU conv/output-fusion model, the collective-compute-fusion model, and the loop/elementwise default that reaches `RecordHloCycles`.

### Entry Point

```text
GetHloResourcesImpl (0x130aa580)            ── StatusOr<ResourceVector>
  ├─ IsSupportedCollectiveHlo (0x130aeda0)  ── collective?  → GetCollectiveCycles (0x130abfc0)
  ├─ IsFusionSupportedHlo     (0x130abee0)  ── numeric-type + fusion gate
  ├─ IsConvLowerable          (0x14553620)  ┐
  ├─ ExtractConvLikeHlo       (0x1d6aa140)  ├ conv / reduce-window selection
  ├─ GetReduceWindowType      (0x1454d4a0)  ┘  (type −1/2 max-pool → not conv)
  │     → GetOutputFusionOrConvolutionCycles (0x130aede0)
  ├─ IsCollectiveComputeFusion(0x13e028c0)  → GetCollectiveComputeFusionCycles (0x130b13a0)
  └─ (default)                              → GetLoopFusionOrUnfusedHloCycles  (0x130b2bc0)
```

### Algorithm

```c
StatusOr<ResourceVector> GetHloResourcesImpl(inst, opts, fs, isFused):  // sub_130AA580
    op = inst.opcode;                              // byte [inst+0xc]

    // (1) COLLECTIVE — all-reduce / all-gather / … priced by the network path.
    if IsSupportedCollectiveHlo(inst):             // sub_130AEDA0
        // (a fusion of opcode 0x3d that wraps a collective is also peeled here)
        return GetCollectiveCycles(inst, &rv);     // sub_130ABFC0 — rv[+8] = scalar cycles

    // (2) NUMERIC-TYPE + FUSION-SUPPORT GATE
    et = inst.shape.element_type;                  // [inst+0x58]->[+0]
    if et <= 0x21 and (0x2FFF91FFE >> et)&1 ...    // numeric/packed-type mask (bittest64)
       and IsFusionSupportedHlo(inst, opts.target): // sub_130ABEE0
        // (3) CONV / REDUCE-WINDOW MAIN-OP SELECTION
        convlowerable = IsConvLowerable(inst);     // sub_14553620
        conv = ExtractConvLikeHlo(inst);           // sub_1D6AA140
        if conv and conv.opcode == 0x5e:           // reduce-window (0x5e == 94)
            t = GetReduceWindowType(conv);         // sub_1454D4A0
            if t == -1 or t == 2:                  // max-pool / unknown — NOT conv-priced
                goto default_path;
        if convlowerable:
            return GetOutputFusionOrConvolutionCycles(inst, opts, fs);  // sub_130AEDE0

    // (4) COLLECTIVE-COMPUTE FUSION (collective fused with compute)
    if IsCollectiveComputeFusion(inst):            // sub_13E028C0
        return GetCollectiveComputeFusionCycles(inst, opts, fs);        // sub_130B13A0

default_path:
    // (5) DEFAULT: loop / elementwise / unfused op — the per-op deposit path
    return GetLoopFusionOrUnfusedHloCycles(inst, opts, fs);             // sub_130B2BC0
```

The numeric gate at `0x130aa9dc` is `cmp et, 0x21` followed by `bt 0x2FFF91FFE` against the element-type ordinal — a bitmask of the numeric/packed element types that the bundle model can price (non-numeric tuple/token/opaque types fall straight through). The reduce-window sentinel is the one non-obvious arm: a `reduce-window` whose `GetReduceWindowType` is `-1` (unknown) or `2` (max-pool) is *not* a lowerable convolution, so it is priced as a plain loop op rather than through the MXU output-fusion estimator.

> **NOTE —** `GetLoopFusionOrUnfusedHloCycles` is the only arm that reaches `RecordHloCycles`. For a fused region it goes through `RecordCyclesIfFused` (@ `0x130cc720`), which first peels convolution (opcode `0x2b` → `RecordConvolutionCycles` @ `0x130ca6c0`), reduce-window (`0x5e` → `RecordReduceWindowCycles` @ `0x130c94e0`), loop-fusion and output-fusion, and only then falls through to `RecordHloCycles` per leaf op. `IsProducerUse` (@ `0x130ab0c0`) drops a producer→consumer edge's input DMA before deposit. The window-iteration bodies of the conv / output-fusion emitters are out of scope here (see [Bundle-Aware Cost](bundle-aware-cost.md)); this page covers the leaf opcode→slot routing.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `CostModel::GetHloResourcesImpl` | `0x130aa580` | 5-way routing dispatch | CERTAIN |
| `IsSupportedCollectiveHlo` | `0x130aeda0` | Collective predicate (arm 1) | CERTAIN |
| `IsFusionSupportedHlo` | `0x130abee0` | Numeric + fusion-support gate | CERTAIN |
| `IsConvLowerable` | `0x14553620` | Conv-lowerable predicate | CERTAIN |
| `ExtractConvLikeHlo` | `0x1d6aa140` | Pull the conv/reduce-window root | CERTAIN |
| `GetReduceWindowType` | `0x1454d4a0` | `−1`/`2` max-pool sentinel | HIGH |
| `GetCollectiveComputeFusionCycles` | `0x130b13a0` | Arm 4 | HIGH |
| `GetLoopFusionOrUnfusedHloCycles` | `0x130b2bc0` | Default → `RecordHloCycles` | CERTAIN |

---

## RecordHloCycles — the Opcode→Slot Jump Table

### Purpose

`RecordHloCycles` is the leaf per-op deposit: given one HLO instruction, an output element count, and the bundle's `ResourceVector`, it deposits that op's throughput cycles into the named `Resource` slot(s) the op occupies. The op→slot decision is a `switch` over the opcode compiled to a `0x7f`-entry self-relative jump table at `.rodata 0xae0ebbc` (index = `opcode − 3`, `ja > 0x7e` → default).

Each deposit issues one or more `CT::Instruction` (the ~33-bucket LLO collapse), resolves the slot via `CycleTable::GetResource(k)` (@ `0x1c89ce20`, gen-invariant table @ `0xb438aec`; see [Resource Enum](resource-enum.md#opslot-mapping--cycletablegetresource)), reads the per-gen throughput via the cycle-table vtable+0x10 (`GetCyclesForThroughput(k)`, per-gen, see [Per-Opcode Cycle Constants](per-opcode-cycle-constants.md)), and accumulates `Acc(slot, element_count × throughput × W)` (`ResourceVector::Acc` @ `0x1c89adc0`) where `W` is a fixed FP multiplier read from `.rodata`.

### Algorithm

```c
RecordHloCycles(inst, window, rv, fs, nesting):       // sub_130BBFE0
    elems     = Product(output_dims);                 // xla::Product → [rbp-0x30]
    isFloat   = ShapeUtil::ElementIsFloating(inst.shape);  // sub @0x130bc... line 139
    if RecordHloCyclesIfTopLevel(...) != 1: return;   // sub_130CDD80 (fusion-root guard)

    switch (inst.opcode):                             // jump table @ .rodata 0xae0ebbc
      case 0x03 add:        slot = isFloat ? GetResource(CT 0x12) : R5;  // R4 / R5
                            Acc(slot, elems × thru(CT 0x12));
      case 0x4b multiply:   Acc(GetResource(CT 0x14), elems × thru(CT 0x14));   // R3
      case 0x7b subtract:   slot = isFloat ? GetResource(CT 0x13) : R5;  // R4 / R5
                            Acc(slot, elems × thru(CT 0x13));
      case 0x32 divide:     /* 4-deposit reciprocal+mul micro-sequence */
            Acc(R6, elems × thru(CT 0x18));                       // VectorEup
            Acc(GetResource(CT 0x14)=R3, elems × thru(CT 0x14) × 3.0);
            Acc(GetResource(CT 0x12)=R4, (elems×2) × thru(CT 0x12));
            Acc(R5, elems × 9.0);                                 // VectorAluAny
      case 0x47 logistic:   /* sigmoid micro-sequence */
            Acc(GetResource(CT 0x12)=R4, elems × thru(CT 0x12));
            Acc(GetResource(CT 0x14)=R3, (elems×2) × thru(CT 0x14));  // VectorAlu0, same slot as multiply
            Acc(R6, elems × thru(CT 0x1a));                       // VectorEup, lane-cmp
      case 0x38 erf:        if isExtPrecPath(inst):               // vtable+312(11)
                                Acc(R6, elems × thru(CT 0x11));   // single VectorEup deposit
                            else:                                 // 4-deposit polynomial
                                Acc(R6, elems × thru(CT 0x18));
                                Acc(GetResource(CT 0x14)=R3, elems × thru(CT 0x14) × 16.0);
                                Acc(GetResource(CT 0x12)=R4, (elems×2) × thru(CT 0x12));
                                Acc(R5, elems × 4.0);
      case 0x2a convert:    if ElementHasBitWidth(inst.shape, 1): // 1-bit PRED
                                Acc(R5, elems × 2.0);             // packed-bool repack
                            else: /* no deposit — free */
      case 0x6c select:     Acc(R5, elems × 2.0);                 // read both branches + pred
      case 0x52 parameter:  if IsFused(inst): /* RecordFusionInputCycles → MemXfer */
                            else: /* no deposit — non-fused param is free */
      case 0x5b reduce:     if IsFused(inst): Acc(R5, elems);
                            else: w = GetInputWindow(inst); Acc(R5, Product(w));  // operand window
      case 0x18,0x1a,0x27,0x29,0x43,0x61,0x81: /* ZERO — no deposit */
      default:              Acc(R5, elems × 1.0);                 // VectorAluAny, 1 cy/elem
```

> **GOTCHA —** Two slot-routing traps in this table. (1) `add` and `subtract` are **float-type-gated**: `slot = ElementIsFloating(shape) ? GetResource(CT) : R5`. Integer add/subtract deposit into `VectorAluAny` (`R5`) like the default; only floating-point add/subtract reach the dedicated lanes (`R4` via `CT 0x12`/`0x13`). Ignoring the gate over-occupies the dedicated lanes for integer fusions. (2) `convert` deposits **only** for 1-bit PRED (`R5`, `elems × 2.0`, a packed-bool repack); every wider numeric width-conversion falls through to the zero path and is free — the opposite of the intuitive "narrow free, wide costed" assumption.

### The Decoded Jump Table

`element_count` is `Product(output dims)`; `thru(k)` is the per-gen `GetCyclesForThroughput(CT::Instruction k)`; `W` is the fixed FP multiplier. CT-bucket→slot via [`CycleTable::GetResource`](resource-enum.md#opslot-mapping--cycletablegetresource).

| Opcode (name) | Block @ | CT issued → slot | Cycle quantity | Confidence |
|---|---|---|---|---|
| `0x03` add | `0x130bc0f7` | float: `CT 0x12`→`R4`; int: `R5` | `elems × thru(0x12)` | CERTAIN |
| `0x4b` multiply | `0x130bc4dd` | `CT 0x14`→`R3` | `elems × thru(0x14)` | CERTAIN |
| `0x7b` subtract | `0x130bc4af` | float: `CT 0x13`→`R4`; int: `R5` | `elems × thru(0x13)` | CERTAIN |
| `0x32` divide | `0x130bc3cf` | `R6` + `CT 0x14`→`R3` + `CT 0x12`→`R4` + `R5` | 4-deposit; `×3.0`(R3), `×2`(R4 elems), `×9.0`(R5) | CERTAIN |
| `0x47` logistic | `0x130bc17b` | `CT 0x12`→`R4`, `CT 0x14`→`R3`, `CT 0x1a`→`R6` | sigmoid seq; `×2`(R3 elems) | HIGH |
| `0x38` erf | `0x130bc245` | fast: `CT 0x11`→`R6`; slow: `R6`+`R3`+`R4`+`R5` | gated; slow `×16.0`(R3), `×2`(R4), `×4.0`(R5) | HIGH |
| `0x2a` convert | `0x130bc293` | 1-bit: `R5`; else none | 1-bit: `elems × 2.0`; wider: 0 | CERTAIN |
| `0x6c` select | `0x130bc2a9` | `R5` (`LABEL_26` ×2 path) | `elems × 2.0` | CERTAIN |
| `0x52` parameter | `0x130bc2b8` | fused→`RecordFusionInputCycles`; else none | fused: input-DMA into MemXfer; non-fused: `0` | CERTAIN |
| `0x5b` reduce | `0x130bc2f9` | `R5` over operand window | priced over reduced-OVER input window | HIGH |
| DEFAULT (most ops) | `0x130bc3c0` | `R5` `VectorAluAny` | `elems × 1.0` | CERTAIN |
| ZERO-cost | `0x130bc8c2` | (no deposit) | `0` | CERTAIN |

The DEFAULT arm covers every numeric elementwise / structural op not named above (the switch's `cases 4-23, 25, 27-38, 40, 43-49, 51-55, 57-66, 68-70, 72-74, 76-81, 83-90, 92-96, 98-107, 109-122, 124-128` per the decompiler's jumptable annotation). The ZERO arm covers the data-layout ops: `bitcast` (`0x18`), `broadcast` (`0x1a`), `concatenate` (`0x27`), `constant` (`0x29`), `iota` (`0x43`), `reshape` (`0x61`), `tuple` (`0x81`).

### Fixed FP Multipliers (`.rodata`, byte-verified)

| Address | Value | Used by | Confidence |
|---|---|---|---|
| `0xa2df230` | `1.0` | default per-op multiplier | CERTAIN |
| `0xa2df930` | `3.0` | divide `VectorAlu0` (R3) scale | CERTAIN |
| `0xa2deb40` | `9.0` | divide `VectorAluAny` (R5) scale | CERTAIN |
| `0xa2df040` | `16.0` | erf slow-path `VectorAlu0` (R3) scale | CERTAIN |
| `0xa2de830` | `4.0` | erf slow-path `VectorAluAny` (R5) scale | CERTAIN |
| `0xa2df5c8` | `0.5` | `MaxResourceCycles` `VectorAlu` port-balance blend | CERTAIN |

### Considerations

The routing across the three `VectorAlu` slots is the port-balancing input to the `MaxResourceCycles` 0.5-blend group `{R3, R4, R5}` (see [Resource Enum](resource-enum.md#the-maxresourcecycles-reduction)). Multiply pins `VectorAlu0` (`R3`), floating add/subtract pin `VectorAlu1` (`R4`), and the catch-all DEFAULT plus the `×2` ops (`select`, 1-bit `convert`) and integer add/subtract land on `VectorAluAny` (`R5`), the lane the blend redistributes. A fusion mixing multiplies, floating adds, and "any" ops therefore fills both dedicated lanes and the bundle cost is the balanced max, not the serial sum.

> **QUIRK —** the bundle model prices a `broadcast` (`0x1a`) at **zero** here, even though [NormalizedComputationCost](normalized-computation-cost.md) may charge a cross-lane-movement weight for the same op on the fusion-priority side. The two models disagree on layout ops by design: the bundle path treats a broadcast as a free register splat (no functional-unit occupancy), while the priority model accounts for the data movement. Do not unify them.

`parameter` and `reduce` route *out* of the ALU lanes. A fused `parameter` is an input-DMA priced by `RecordFusionInputCycles` (@ `0x130ce940`) into the `MemXfer` slots `R9..12`; whether VMEM-resident params instead route to `R7` `VectorLoad` per memory space is not byte-confirmed (MEDIUM). A **non-fused** `parameter` deposits nothing — `case 0x52` falls straight to the finish path (`if (!IsFused) goto LABEL_77`), so a top-level argument carries zero functional-unit cost. A non-fused `reduce` is priced over its *operand* window via `GetInputWindow` (so cost scales with the large reduced-over tensor, not the small output) — the bundle-side mirror of `HandleReduce`'s `ExtentProduct(operand)` flop formula below.

---

## Base Flop Formulas (inherited from `HloCostAnalysis`)

`TpuHloCostAnalysis` overrides only gather/scatter/custom-call; every other op uses the base `xla::HloCostAnalysis` emitter unchanged. These are the flop properties cached in `Properties[inst].float[+0x50]` (flops) / `[+0x54]` (transcendentals), read back by `flop_count` (@ `0x1e4841e0`).

### Dot — `2·M·N·K`

`HandleDot` (@ `0x1e47c9c0`) → `GetDotFlops` (@ `0x1e47c7a0`):

```c
GetDotFlops(out_shape, lhs_shape, dnums):           // sub_1E47C7A0
    contract_batch = 1;
    for d in dnums.contracting_and_batch_dim_indices:
        contract_batch *= lhs_shape.dims[d];         // imul over the index list
    return 2 * contract_batch * ExtentProduct(out_shape);  // ×2 for multiply-add
```

The decompile is an unrolled `imul` chain over the `DotDimensionNumbers` index lists, then `imul ExtentProduct(out)`, then `add rax, rax` (the `×2`). This is the classic `2·M·N·K`.

> **GOTCHA —** raw `dot` is lowered to `convolution` before the TPU cost path runs ([dot/conv MXU lowering](../compiler/dot-conv-mxu-lowering.md)); reaching `GetDotFlops` on an un-lowered dot hits a `CHECK`-fatal (`buffer != nullptr`, `shape.h:843`). In practice the convolution formula below is what feeds the fusion priority.

### Convolution — divides by **both** group counts

`HandleConvolution` (@ `0x1e480be0`) calls `GetConvolutionFlops` (vtable+1128) and stores the result to `+0x50`. `GetConvolutionFlops` (@ `0x1e480060`):

```c
GetConvolutionFlops(inst, out, lhs, rhs):           // sub_1E480060
    out_spatial   = ExtentProduct(window-iterated output spatial+batch);  // v114
    window_vol    = Product(kernel spatial window sizes);                  // v108
    in_feature    = GetDimension(lhs, input_feature_dim);                  // Dimension
    out_feature   = GetDimension(out, output_feature_dim);                 // v115
    fgc = inst.feature_group_count(); bgc = inst.batch_group_count();
    return 2 * (out_feature / bgc) * (in_feature / fgc) * window_vol * out_spatial;
```

> **GOTCHA —** The convolution flop divides by **both** `feature_group_count` *and* `batch_group_count`, not feature-group alone — two separate `idiv`/`div` sequences (`v109 = Dimension/fgc`, `v110 = v115/bgc`). Grouped convolutions (depthwise, batch-grouped) are mis-costed by any model that drops the batch-group divide.

This is the flop the [NormalizedComputationCost](normalized-computation-cost.md) conv path caches per `unique_id` and divides by the per-LHS-format peak to get MXU cycles.

### Reduce / Reduce-Window — combiner cost × extent

`HandleReduce` (@ `0x1e47d6a0`): `flops = to_apply().per_element_cost × ExtentProduct(operand_being_reduced)`. The combiner subcomputation's per-element cost (flops, transcendentals, bytes — each scaled by `vmulss`) times the number of **input** elements reduced over. Cost scales with the large reduced-over tensor, not the small output.

`HandleReduceWindow` (@ `0x1e47e1c0`): `flops = to_apply().per_element_cost × Product(window dim sizes) × ExtentProduct(output_shape)` — the combiner runs once per window element per output element.

Both feed the combiner cost through the base `ProcessSubcomputation` recursion (not re-walked here).

### Elementwise — the flop/transcendental classifier

`HandleElementwiseOp` (@ `0x1e47b320`) computes `cost = ExtentProduct(output_shape)` (1 op/element) and routes it by a `switch` on the opcode that selects the store offset:

```c
HandleElementwiseOp(inst):                          // sub_1E47B320
    cost = ExtentProduct(inst.shape);
    off  = is_transcendental(opcode) ? 0x54 : 0x50; // ecx = 84 : 80
    Properties[inst].float[off] = (float)cost;
```

The 22-opcode transcendental set (stored to `+0x54`), byte-exact from the `switch` cases:

| Opcode | Name | Opcode | Name | Opcode | Name |
|---|---|---|---|---|---|
| `0x01` | acos | `0x2f` | cosine | `0x55` | power |
| `0x02` | acosh | `0x30` | cosh | `0x68` | rsqrt |
| `0x0e` | asin | `0x38` | erf | `0x75` | sine |
| `0x0f` | asinh | `0x39` | exponential | `0x76` | sinh |
| `0x13` | atan2 | `0x3a` | exponential-minus-one | `0x79` | sqrt |
| `0x14` | atanh | `0x45` | log | `0x7c` | tan |
| `0x1c` | cbrt | `0x46` | log-plus-one | `0x7d` | tanh |
| | | `0x47` | logistic | | |

Every other elementwise op (add, multiply, subtract, and/or/xor, compare, clamp, negate, abs, floor, ceil, sign, round, shift, min, max, remainder, …) stores to `+0x50` (flops), 1/element. The transcendental set the flop model routes to `+0x54` is the same set the bundle model prices as multi-µop sequences (`divide`, `logistic`, `erf` above), so the two surfaces agree on which ops are expensive.

---

## TPU-Specific Overrides

All three overrides are gated on `*(byte*)(this + 456)` — the `model_tpu_specific_overheads` option. When it is clear, each method tail-calls its base `HloCostAnalysis` counterpart and the TPU model contributes nothing. When set but the `Target` pointer (`this + 56`) is null, gather/scatter raise `FailedPrecondition("target_ not specified but model_tpu_specific_overheads ... enabled")` (`tpu_hlo_cost_analysis.cc`).

### HandleGather — chunk-ratio bytes, no flops

`HandleGather` (@ `0x130a2de0`):

```c
HandleGather(inst):                                 // sub_130A2DE0
    if !this[456]: return base::HandleGather(inst);  // overrides off
    if !this.target: FailedPrecondition(...);
    out_sz   = GetShapeSize(inst.output_shape);                 // bytes
    ratio    = GetGatherSizeInChunkRatio(target, inst);         // sub_14A8E420
    op1_sz   = GetShapeSize(inst.operand(1).shape);             // index operand
    // four float fields stored at this+0x58/+0x6c/+0x70/+0x74 (bytes-accessed family)
    store {out_sz, ratio-amplified bytes, op1_sz, ...} to Properties bytes fields;
```

Gather is modelled as a **pure memory/DMA op** — no flops are added. The cost is the granule "chunk ratio" (`GetGatherSizeInChunkRatio` @ `0x14a8e420`): a chunk-aligned read-amplification factor over the output and index sizes. The decompile stores four `float` results (`+0x58`, `+0x6c`, `+0x70`, `+0x74`) via `vcvtsi2ss`/`vmovss`, i.e. into the bytes-accessed property family rather than a single field. The internal granule math of `GetGatherSizeInChunkRatio` is not enumerated (the amplification factor is confirmed; the formula is LOW).

### HandleScatter — chunk-ratio bytes **plus** combiner flops

`HandleScatter` (@ `0x130a3160`):

```c
HandleScatter(inst):                                // sub_130A3160
    if !this[456]: return base::HandleScatter(inst);
    if !this.target: FailedPrecondition(...);
    op2_sz = GetShapeSize(inst.operand(2).shape);               // updates
    ratio  = GetScatterSizeInChunkRatio(target, inst);          // sub_14A90CE0
    op1_sz = GetShapeSize(inst.operand(1).shape);               // indices
    store ratio-amplified bytes into Properties bytes fields (this+0x58/0x5c/0x60/...);
    // PLUS the scatter-combiner compute:
    upd_extent = ExtentProduct(inst.operand(2).shape);          // updates element count
    per_elem   = ProcessSubcomputation(inst.to_apply());        // vtable slot 150
    for each non-zero combiner property p:
        Properties[inst].float[p_offset] += per_elem[p] * upd_extent;  // incl. +0x54 transcend.
```

Scatter = the gather-style chunk-ratio bytes term **plus** the update-combine compute. The decompile multiplies each non-zero combiner per-element property (flops `+0x50`, transcendentals `+0x54`, and the bytes fields) by `ExtentProduct(updates)` and folds it into `Properties`, iterating the combiner's property map via `Properties::operator[]` keyed by the standard cost-analysis property names. So a scatter with a transcendental combiner correctly bumps the transcendental count, not just flops. `GetScatterSizeInChunkRatio` is @ `0x14a90ce0` (granule formula LOW).

### HandleCustomCall — target-keyed registry, else base

`HandleCustomCall` (@ `0x130a35c0`):

```c
HandleCustomCall(inst):                             // sub_130A35C0
    reg = CustomCallRegistration::GetGlobalRegistry();  // lazy __cxa_guard; FunctionRegistry
    cb  = reg.Get(inst.custom_call_target());           // string-keyed lookup
    if cb found:
        return cb(inst, &this.shape_size_fn /*this+240*/, &this.Properties /*this+80*/);
    VLOG(1) "Custom hlo cost analysis not found: " << target;
    return base::HandleCustomCall(inst);                // sub_1E482A20
```

Custom-call is the open extensibility hook: a `util_registration::FunctionRegistry<string, Status(HloInstruction*, function<long(Shape const&)> const&, Properties&)>` keyed on the custom-call target string. A registered callback computes a bespoke flop/byte model (handed the instruction, the `GetShapeSize` functor at `this+240`, and the `Properties` map at `this+80`); an unregistered target logs a `VLOG(1)` miss and falls back to the base emitter. This mirrors the custom-fusion registry on the fusion side ([fusion cost model](../compiler/fusion-cost-model.md)).

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `TpuHloCostAnalysis::HandleGather` | `0x130a2de0` | chunk-ratio bytes; gated on `this+456` | CERTAIN |
| `TpuHloCostAnalysis::HandleScatter` | `0x130a3160` | chunk-ratio bytes + combiner flops/transcend. | CERTAIN |
| `TpuHloCostAnalysis::HandleCustomCall` | `0x130a35c0` | `FunctionRegistry` on target; else base | CERTAIN |
| `GetGatherSizeInChunkRatio` | `0x14a8e420` | gather read-amplification factor | HIGH |
| `GetScatterSizeInChunkRatio` | `0x14a90ce0` | scatter write-amplification factor | HIGH |
| `HloCostAnalysis::GetShapeSize` | `0x1e47a6e0` | byte size of a shape | CERTAIN |
| base `HloCostAnalysis::HandleCustomCall` | `0x1e482a20` | fallback emitter | CERTAIN |

---

## Worked Example — elementwise fusion bundle deposit

A loop-fusion body `{ multiply [256,128] (f32), add [256,128] (f32), tanh [256,128] }`. `element_count = 256·128 = 32768`. Each op routes through the `RecordHloCycles` jump table:

```text
multiply (0x4b)  → CT 0x14 Shuffle → GetResource → R3 VectorAlu0:   Acc(R3, 32768 × thru(0x14))
add (0x03), f32  → CT 0x12 RotIn   → GetResource → R4 VectorAlu1:   Acc(R4, 32768 × thru(0x12))
tanh (0x7d)      → DEFAULT block   →               R5 VectorAluAny: Acc(R5, 32768 × 1.0)
```

The flop side classifies `tanh` as transcendental (`Properties+0x54`) and `multiply`/`add` as flops (`Properties+0x50`); the bundle side prices all three via the table above. `MaxResourceCycles`' 0.5-blend on `{R3, R4, R5}` runs the multiply (`VectorAlu0`) and add (`VectorAlu1`) lanes in parallel, load-balances the `tanh` "any" work into the less-busy lane, and overlaps the residual at 50%, so the bundle cost is ≈ `max(R3, R4) + half the tanh residual`, not the serial sum. Had `add` been integer-typed, it would have joined `tanh` on `R5` (per HCA-1) rather than pinning `R4`.

---

## Related Components

| Component | Relationship |
|---|---|
| [GetHloResources Routing](gethloresources-routing.md) | The full `GetHloResourcesImpl` → sub-emitter routing this page summarizes |
| [NormalizedComputationCost](normalized-computation-cost.md) | The fusion-priority consumer of `flop_count` / the conv flop |
| [Resource Enum (23-slot)](resource-enum.md) | The `ResourceVector` slots and `MaxResourceCycles` reduction the deposits feed |
| [Bundle-Aware Cost](bundle-aware-cost.md) | The loop composition over the per-op deposits |
| [Per-Opcode Cycle Constants](per-opcode-cycle-constants.md) | The per-gen `GetCyclesForThroughput` integers multiplied into each deposit |

## Cross-References

- [Cost Model Overview](overview.md) — the three class families that build the per-gen `CycleTable` / `Performance` the deposits query
- [GetHloResources Routing](gethloresources-routing.md) — the five-way dispatch and the fusion-peel router into `RecordHloCycles`
- [NormalizedComputationCost](normalized-computation-cost.md) — the opcode→weight fusion-priority model that consumes `flop_count` and the conv flop
- [Resource Enum (23-slot)](resource-enum.md) — the `Resource` slot names, `Acc`, and the `MaxResourceCycles` overlap model
- [Per-Opcode Cycle Constants](per-opcode-cycle-constants.md) — the per-gen `GetCyclesForThroughput(CT::Instruction)` integers
- [CycleTable Family](cycletable-family.md) — `GetResource` (op→slot) and the `Instruction` bucket enum
- [Bundle-Aware Cost](bundle-aware-cost.md) — the software-pipelined loop cost over the per-op deposits
- [dot/conv MXU Lowering](../compiler/dot-conv-mxu-lowering.md) — why raw `dot` is lowered to `convolution` before the cost path
- [Fusion Cost Model](../compiler/fusion-cost-model.md) — the custom-fusion registry that mirrors the custom-call hook
- [Compiler Overview](../compiler/overview.md) — where HLO cost analysis sits in the compile pipeline
