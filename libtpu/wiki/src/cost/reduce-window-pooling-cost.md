# Reduce-Window / Pooling Cost

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build `libtpu_lts_20260413_b_RC00`, BuildID md5 `89edbbe81c5b328a958fe628a9f2207d`, 781,691,048 bytes, not stripped). `.text`/`.rodata` VMA == file offset; `.data.rel.ro` VMA − 0x200000 == file offset. The binary retains full demangled C++ symbols. Other libtpu builds will differ.*

## Abstract

`RecordReduceWindowCycles` is the cost-model emitter that prices an HLO `kReduceWindow` — max-pool, average-pool, or any windowed reduction. It is the **pooling sibling of the convolution emitter** [`RecordConvolutionCycles`](convolution-cost-state.md): both reuse the same per-call `ConvState` dim-product struct, the same `Target` geometry divisors (`SublaneCount`, `ChunksPerTile`), and the same four-`ResourceVector` output protocol. The difference is the *destination*. A convolution lands its compute cost on the MXU pipes (`Matmul`/`Matpush`, priced through [`MxuLatencyTable`](mxu-latency-overview.md)); a reduce-window is a vector reduction, not a matrix multiply, so every compute cycle lands on the **vector / cross-lane pipes** — `VectorLoad`, `VectorAluAny`, and `Xlu`. There is no MXU traffic.

The familiar reference frame is an LLVM `TargetTransformInfo` cost hook for a reduction intrinsic, except that this model is *throughput-shaped*, not latency-shaped: the cost is "how many cycles does the vector unit stay busy reading and combining the window," computed as a per-output-element count multiplied by a per-op throughput drawn from the per-generation `CycleTable`. The window area enters the cost through the `ConvState` dim products, and the *combiner* — the `to_apply` subcomputation that XLA attaches to every reduce-window (the `maximum` of a max-pool, the `add` of an avg-pool) — is priced separately by walking its instructions and charging each modeled op once per window-element per output.

The shape of the cost is set by which **axis** the window reduces over. `GetReduceWindowType` classifies the reduction into Lane (most-minor axis), Sublane (the sublane axis), or Major (a slow/outer axis), and each gets a distinct leaf emitter with a distinct deposit pattern: Lane adds a cross-lane `Xlu` drain, Sublane adds a sublane-shuffle term, and Major is purely read-plus-combine. This page documents the dispatch, the `ConvState`-driven window-area formula each leaf uses, the per-op throughput it multiplies by, and the combiner-cost helper they share.

For reimplementation, the contract is:

- The four-`ResourceVector` entry protocol and the trivial-zero short-circuit when the cost model is not live.
- The `ConvState` build (shared with conv) and the three named `ConvState` dim products each leaf reads: output-volume, window-extent, and kernel-spatial-iteration count.
- The `GetReduceWindowType` 3-way axis dispatch and the slot deposits each leaf makes (`VectorLoad`, `VectorAluAny`, `Xlu`), including the `Target+0x4b0` Xlu-rate divisor.
- The combiner cost: walk `to_apply`, classify each modeled combiner opcode → a `CycleTable::Instruction`, and charge `throughput(CT) × count` where `count` is the per-axis window-element count (`vol·(window−1)` for Lane, `base·(window−1)` plus a `4·base` term for Sublane, the whole `vol` for Major).

| | |
|---|---|
| **Class** | `xla::jellyfish::CostModel` (`cost_model.cc`) |
| **Entry (4-RV)** | `RecordReduceWindowCycles` `@0x130b5ec0` |
| **Axis dispatch** | `fusion_util::GetReduceWindowType` `@0x1454d4a0` → −1 / 0 / 1 / 2 |
| **Lane leaf** | `RecordLaneReduceWindowCycles` `@0x130c97e0` (cost_model.cc:4931) |
| **Sublane leaf** | `RecordSublaneReduceWindowCycles` `@0x130c9c60` (cost_model.cc:4978) |
| **Major leaf** | `RecordMajorReduceWindowCycles` `@0x130c9f00` (cost_model.cc:5033) |
| **Combiner cost** | `UpdateCostBasedOnReductionFunction` `@0x130c9a20` (fatal @ cost_model.cc:6870) |
| **Deposit** | `ResourceVector::Acc(Resource, double)` `@0x1c89adc0` |
| **Throughput source** | per-gen `CycleTable::GetCyclesForThroughput(Instruction)` (via `[CostModel+0x8]`=`CycleTable`, then vtable slot `[+0x10]`) |
| **Compute pipes** | `R[7] VectorLoad`, `R[5] VectorAluAny`, `R[2] Xlu` — **no MXU** |
| **Input DMA** | `RecordOperandCycles` `@0x130ca140` → `R[9..12] MemXfer` |

---

## The Entry Point and Output Protocol

### Purpose

`RecordReduceWindowCycles` is invoked from the output-fusion cost path as one of several priced sub-emitters (the conv/dot/reduce-window family). It takes the reduce-window HLO, three `Window` descriptors (activations / a second window / output), and **four** `ResourceVector` out-parameters, and deposits the pooling cost across them. The four-vector form mirrors `RecordConvolutionCycles`: the caller later folds the four sub-vectors with `MaxResourceCycles`' overlap rules (see [`resource-enum`](resource-enum.md)).

### Entry Point

```text
RecordReduceWindowCycles (4-RV)        @0x130b5ec0  ── prices a kReduceWindow
  ├─ convolution_util::GetConvLikeProperties        ── derive ConvolutionDimensionNumbers + shapes
  ├─ window_util::HasBaseDilation                   ── dilation gate (blocks Lane/Sublane)
  ├─ (inline) build ConvState at rbp-0x1d0          ── Product(dim)/SublaneCount/ChunksPerTile
  ├─ fusion_util::GetReduceWindowType   @0x1454d4a0 ── 0=Lane 1=Sublane 2=Major (−1=not lowerable)
  ├─ RecordLaneReduceWindowCycles       @0x130c97e0 ── axis 0
  ├─ RecordSublaneReduceWindowCycles    @0x130c9c60 ── axis 1
  ├─ RecordMajorReduceWindowCycles      @0x130c9f00 ── axis 2
  ├─ RecordOperandCycles                @0x130ca140 ── input DMA → R[9..12]
  └─ RecordTopLevelConvolutionOutputCycles @0x130bcb80 ── output DMA (when top-level)
```

### Algorithm

```c
function RecordReduceWindowCycles(rw, act_win, win2, out_win, rv0, rv1, rv2, rv3, fusion, top):  // @0x130b5ec0
    CHECK(rw->opcode() == kReduceWindow)             // opcode byte +12 == 0x5e; cost_model.cc:5068

    if (this->cost_model_live == 0):                 // CostModel byte +0x14 (the [a1+20] gate)
        // TRIVIAL path: not modellable — deposit zero into the four vector/Xlu slots and return.
        rv2.Acc(VectorLoad=7,  0.0)                  // 0x130b5fc3..6007 (vpxor → 0.0)
        rv2.Acc(VectorAlu0=3,  0.0)
        rv2.Acc(VectorAlu1=4,  0.0)
        rv2.Acc(Xlu=2,         0.0)
        return Ok

    // REAL path
    props   = GetConvLikeProperties(rw)              // ConvolutionDimensionNumbers + operand/result shapes
    dilated = HasBaseDilation(act_win, dim=0)        // window_util::HasBaseDilation → blocks Lane/Sublane
    CHECK(rw_state.input_batch_is_most_minor)        // cost_model.cc:5093
    CHECK(!rw_state.output_feature_is_most_minor)    // cost_model.cc:5094

    state = BuildConvState(props, out_win, ...)      // dim products / SublaneCount / ChunksPerTile (rbp-0x1d0)
                                                     // incl. the 64-bit window-volume multiply loop @0x130b6290

    switch (GetReduceWindowType(rw)):                // @0x1454d4a0
        case 0:  CHECK(!dilated)                     // cost_model.cc:5142
                 RecordLaneReduceWindowCycles(rw, axis_minor, state, rv2)
        case 1:  CHECK(!dilated)                     // cost_model.cc:5149
                 RecordSublaneReduceWindowCycles(rw, axis_minor, state, rv2)
        case 2:  RecordMajorReduceWindowCycles(rw, axis_minor, state, rv2)
        // case −1 (not conv-lowerable) is filtered upstream by the fusion sentinel; not reached here.

    if (top):                                        // top-level (non-fused) reduce-window
        RecordOperandCycles(rw->operand(0), op_state, ...)            // input DMA  → rv (R[9..12])
        RecordTopLevelConvolutionOutputCycles(rw, out_win, ...)       // output DMA → R[11..12]
    return Ok
```

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `CostModel::RecordReduceWindowCycles` (4-RV) | `0x130b5ec0` | top emitter — CHECK, ConvState build, axis dispatch | CERTAIN |
| `fusion_util::GetReduceWindowType` | `0x1454d4a0` | axis classifier — −1 / 0 / 1 / 2 | CERTAIN |
| `CostModel::RecordLaneReduceWindowCycles` | `0x130c97e0` | lane-axis leaf — VectorLoad + combine + Xlu drain | CERTAIN |
| `CostModel::RecordSublaneReduceWindowCycles` | `0x130c9c60` | sublane-axis leaf — adds sublane shuffle | CERTAIN |
| `CostModel::RecordMajorReduceWindowCycles` | `0x130c9f00` | major-axis leaf — read-heavy, no Xlu | CERTAIN |
| `CostModel::UpdateCostBasedOnReductionFunction` | `0x130c9a20` | combiner-op cost (shared by all three leaves) | CERTAIN |
| `CostModel::RecordOperandCycles` | `0x130ca140` | input-DMA deposit → `R[9..12]` MemXfer | HIGH |
| `CostModel::RecordTopLevelConvolutionOutputCycles` | `0x130bcb80` | output-DMA deposit (top-level only) | HIGH |
| `ResourceVector::Acc(Resource, double)` | `0x1c89adc0` | the slot deposit primitive (bound `< 23`) | CERTAIN |
| `convolution_util::GetConvLikeProperties` | `0x13190bc0` | shared conv/RW dim-number + shape extraction | HIGH |
| `window_util::HasBaseDilation` | (call @0x130b62..) | dilation gate blocking Lane/Sublane | HIGH |

> **NOTE —** the trivial-zero short-circuit (`CostModel+0x14 == 0`) is not a no-op: it still deposits four explicit `0.0` cycles into `VectorLoad`/`VectorAlu0`/`VectorAlu1`/`Xlu`. The deposit count must match a real pooling op even when the cost is zero, because downstream the four `ResourceVector`s are folded positionally; a reimplementation that simply `return`s without the four zero-`Acc` calls leaves those slots uninitialized for the fold. The deposit goes into the **third** out-vector (`a9`/`rv2`), which is also where every real-path leaf deposits.

---

## ConvState — the Shared Dim-Product Struct

### Purpose

A reduce-window does not build a `ConvCostState`. It builds the smaller per-call `ConvState` *inline*, from the `ConvolutionDimensionNumbers` and the output `Window`, by exactly the convolution recipe: each dim field is `Product(dim_chunk_sizes)` divided by a `Target` geometry constant (`SublaneCount()` = 8, `ChunksPerTile()` = 16), paired with a divisibility remainder. The same struct, the same divisions — see [`convolution-cost-state`](convolution-cost-state.md) for the full field map. The reduce-window leaves read only three of its fields, and the binary *names* them through CHECK strings and VLOG arguments.

### The three fields the pooling cost reads

The three leaf emitters index `ConvState` as a `_QWORD` array (`a4[i]` = `ConvState + 8*i`). Only three offsets carry the pooling cost, and the binary identifies each:

| `ConvState` offset | `a4[i]` | Named role | Evidence | Confidence |
|---|---|---|---|---|
| `+0x18` | `a4[3]` | output-spatial dim product | VLOG arg 3 ("`, %ld`" in Lane/Sublane @cost_model.cc:4931/4978) | HIGH |
| `+0x20` | `a4[4]` | **kernel-spatial-dims iteration count** | CHECK string `rw_state.kernel_spatial_dims_iteration_count == 1` (Major @cost_model.cc:5033) | CERTAIN |
| `+0x68` | `a4[13]` | output dim product / window extent (per-axis) | VLOG arg 2 (Lane/Sublane) + combiner `(a4[13]−1)` | HIGH |
| `+0x70` | `a4[14]` | window-extent / iteration count (the reduced axis) | VLOG arg 1 (Lane/Sublane) + combiner `(a4[14]−1)` | HIGH |

The CHECK at cost_model.cc:5033 is the one hard name: in the Major path, `ConvState+0x20` (`a4[4]`) **must equal 1** and is explicitly labeled the *kernel-spatial-dims iteration count*. That is the conv "how many kernel-window positions iterate" field; a major-axis reduce-window has its window folded into a single major iteration, so it is pinned to 1. The other three are named from the order in which the Lane/Sublane VLOG prints them (`win_extent`, `output`, `output_spatial`) and from which one the combiner decrements by 1 (the *window extent* of the reduced axis).

> **QUIRK —** the pair `(+0x68, +0x70)` is the reduced-axis pair. In the Lane leaf the VectorLoad volume is built from `+0x70 · +0x18 · +0x20` and the combiner window is `+0x68`; in the Sublane leaf the VectorLoad volume includes *both* `+0x68` and `+0x70` and the combiner window is `+0x70`. The two leaves read the same struct but treat opposite members of the pair as "the window being reduced," because Lane and Sublane reduce over physically different axes. A reimplementation must not assume a single canonical "window-size" field — the window dimension is axis-dependent.

---

## Axis Dispatch — `GetReduceWindowType`

### Purpose

The cost shape is set by which physical axis the window sweeps. `GetReduceWindowType` (`@0x1454d4a0`, namespace `fusion_util`) returns the axis class. It first asks `IsConvLowerableReduceWindow`; if the reduce-window cannot be lowered to the conv path it returns `−1` (`0xFFFFFFFF`). Otherwise it maps the layout to physical order (`LayoutUtil::MakeLogicalToPhysical`) and inspects which window dimensions are non-trivial (`window_util::IsTrivialWindowDimension`).

### Algorithm

```c
function GetReduceWindowType(rw):                    // @0x1454d4a0
    if !IsConvLowerableReduceWindow(rw): return -1   // not modellable via conv path

    phys = MakeLogicalToPhysical(operand_layout)
    for each non-most-minor physical dim d:          // the "outer/major" dims (the loop)
        if !IsTrivialWindowDimension(window[d]):
            return 2                                  // Major: window on a slow axis

    // window is confined to the two most-minor physical dims (minor0 = most-minor / lane axis)
    if !IsTrivialWindowDimension(window[minor0]):     return 0   // Lane:    lane axis has a window
    if !IsTrivialWindowDimension(window[minor1]):     return 1   // Sublane: lane trivial, sublane non-trivial
    return 2                                          // Major:   both most-minor dims trivial
```

The master emitter dispatches on the result with `if (type) { if (type != 1) { if (type == 2) Major; } else Sublane; } else Lane;` — so the mapping is **0 → Lane, 1 → Sublane, 2 → Major**, confirmed at the three call sites in `@0x130b5ec0`. (Note the decompile reaches the `return 0`/`return 1`/`return 2` for the most-minor pair via a shared tail: when `window[minor0]` is non-trivial it falls straight through to return `IsTrivial(window[minor0]) == 0` = Lane; when `minor0` is trivial it tests `minor1`, returning `IsTrivial(window[minor0]) == 1` = Sublane if `minor1` is non-trivial, else holding `result = 2` = Major.)

> **GOTCHA —** there are two distinct reduce-window "type −1/2" filters in the cost model and they are not the same gate. The output-fusion *dispatch* (in `GetOutputFusionOrConvolutionCycles`) uses a sentinel to *skip* reduce-windows of type 2 or −1 before any state is built. The emitter here is reached only as a *priced* sub-emitter, and at that point **all three axis types deposit** (Lane, Sublane, Major). Do not carry the upstream skip-logic into the leaf dispatch. A type −1 never reaches this function (it is filtered upstream); the `case −1` branch is therefore not exercised here.

---

## The Three Leaf Emitters

All three leaves share the signature `Record<Axis>ReduceWindowCycles(rw, axis_minor:int, ConvState&, ResourceVector*)` and the deposit grammar: throughput cycles into named `ResourceVector` slots via `Acc`. They differ in the volume formula and which extra pipes they touch. The per-op throughput is `CycleTable::GetCyclesForThroughput(CT)` — read through the `CostModel`'s `CycleTable` vtable slot `[+0x10]` — where `CT` is a `CycleTable::Instruction` ordinal. The integers behind each `CT` are per-generation and live in [`vf-cycletable`](vf-cycletable.md) / [`jf-cycletable`](jf-cycletable.md).

### Lane — `RecordLaneReduceWindowCycles` `@0x130c97e0`

The lane-axis reduction reduces over the most-minor (lane) axis, so after the per-element combine it must drain the partial results *across lanes* on the cross-lane unit (`Xlu`). This is the only leaf with an Xlu term.

```c
function RecordLaneReduceWindowCycles(rw, axis_minor, st, rv):   // @0x130c97e0
    vol = st[+0x20] * st[+0x18] * st[+0x70]          // a4[4]·a4[3]·a4[14] — output volume read into VMEM
    window = st[+0x68]                                // a4[13]

    rv.Acc(VectorLoad=7, (double)vol)                // R[7]: input read into VMEM

    // F16 pre-packed-feed special case (operand is f16 AND not a kBitcast-class feed, opcode != 0x3d):
    if element_type(operand0) == F16(16) && operand0.opcode != 0x3d:
        cyc = GetCyclesForThroughput(CT 0x16)        // vtable [CycleTable+0x10]
        rv.Acc(VectorAluAny=5, cyc * vol)            // R[5]: extra per-element unpack-combine

    UpdateCostBasedOnReductionFunction(to_apply(rw), vol * (window - 1), rv)   // combiner cost

    drain = GetCyclesForThroughput(CT 0x1b)          // CT 0x1b = Xlu-class throughput
    rv.Acc(Xlu=2, drain / Target[+0x4b0])            // R[2]: cross-lane reduction drain

    // F16 residual unpack (only when axis_minor == 0 and the *result* is f16):
    if axis_minor == 0 && element_type(result) == F16(16):
        rv.Acc(VectorAluAny=5, ...)                  // R[5]
```

- `R[7] VectorLoad` = `vol` = `ConvState[+0x20]·[+0x18]·[+0x70]` — the input volume streamed into the vector unit.
- `R[2] Xlu` = `throughput(CT 0x1b) / Target[+0x4b0]` — the bare CT-0x1b throughput divided by the divisor; there is no additional spatial multiplier in the decompiled deposit (`@0x130c97e0`: `GetCyclesForThroughput(27)` → numerator, `[Target+0x4b0]` → denominator, single `vdivsd`). `Target+0x4b0` is the **vector-ISA Xlu/cross-lane rate divisor**, populated in `Target::Init` from `TpuSequencerParts::vector_isa()` — *not* a `ConvCostState` field (see correction below). It is the same divisor the conv emitter uses for its `Xlu` mxres deposit.
- The combiner count is `vol · (window − 1)` — the reduction combines `window − 1` times per output element (a tree reduce over the window touches each of the `window` inputs but performs `window − 1` combines).

### Sublane — `RecordSublaneReduceWindowCycles` `@0x130c9c60`

The sublane reduction reduces across the 8 sublanes. Instead of an `Xlu` drain it charges a **sublane-shuffle** term on `VectorAluAny` (the shuffle/broadcast family lands on `R[5]`, per [`resource-enum`](resource-enum.md)), and it invokes the combiner **twice** — once for the window reduction and once with a fixed `4×` factor for the cross-sublane combine tree.

```c
function RecordSublaneReduceWindowCycles(rw, axis_minor, st, rv):   // @0x130c9c60
    base   = st[+0x20] * st[+0x18] * st[+0x68]       // a4[4]·a4[3]·a4[13]
    window = st[+0x70]                                // a4[14]
    vol    = base * window                            // full output volume

    rv.Acc(VectorLoad=7, (double)vol)                // R[7]: input read

    if element_type(operand0) == F16(16) && operand0.opcode != 0x3d:   // same F16 gate as Lane
        cyc = GetCyclesForThroughput(CT 0x16)
        rv.Acc(VectorAluAny=5, cyc * vol)

    UpdateCostBasedOnReductionFunction(to_apply(rw), base * (window - 1), rv)   // window combine

    SublaneCount()                                    // = 8 (consulted, result folded into the shuffle)
    r = CycleTable::GetResource(CT 0x15)              // CT 0x15 → R[5] VectorAluAny (shuffle family)
    rv.Acc(r, <shuffle cycles>)                       // R[5]: sublane shuffle

    UpdateCostBasedOnReductionFunction(to_apply(rw), 4 * base, rv)   // cross-sublane combine tree (×4)

    if axis_minor == 0 && element_type(result) == F16(16):
        rv.Acc(VectorAluAny=5, ...)                   // R[5] F16 residual
```

> **QUIRK —** the second combiner call uses a literal `4 × base` factor (`@0x130c9c60`, `4 * v9`), not `base × (window−1)`. This `4` is the cost of the cross-sublane reduction tree, charged independently of the window size — sublane reductions fan in over a fixed shuffle network whose depth the model prices as a constant `4` combiner passes. A reimplementation that scales this term by the sublane count (8) or by the window will mis-price every sublane-axis pool.

### Major — `RecordMajorReduceWindowCycles` `@0x130c9f00`

A major-axis reduce-window reduces over a slow/outer dimension, so it is dominated by the cost of *reading* the (large) window volume; there is no cross-lane or cross-sublane drain.

```c
function RecordMajorReduceWindowCycles(rw, axis_minor, st, rv):   // @0x130c9f00
    CHECK(st[+0x20] == 1)   // "rw_state.kernel_spatial_dims_iteration_count == 1"  cost_model.cc:5033

    props      = GetConvLikeProperties(rw)
    window_vol = Product(window_dim.size for each window dimension)   // 64-bit multiply over WindowDimension list
    vol        = st[+0x68] * st[+0x70] * st[+0x18] * window_vol       // a4[13]·a4[14]·a4[3]·window_vol

    rv.Acc(VectorLoad=7, (double)vol)                                // R[7]: read the whole window volume
    UpdateCostBasedOnReductionFunction(to_apply(rw), vol, rv)        // combiner: once per element of vol
```

- `R[7] VectorLoad` carries the full `output_volume · window_volume` read. The combiner count is the *entire* `vol` (no `−1` decrement), because a major reduction's combine count is folded directly into the volume product rather than separated into a per-output window-minus-one term.
- No `Xlu`, no shuffle, no F16 residual: a major reduce-window is read-plus-combine only.

---

## The Combiner Cost — `UpdateCostBasedOnReductionFunction`

### Purpose

Every reduce-window carries a `to_apply` subcomputation — `maximum` for max-pool, `add` for sum/avg-pool, or an arbitrary user reduction. `UpdateCostBasedOnReductionFunction` (`@0x130c9a20`, shared by all three leaves) prices the *combiner* itself: it walks the subcomputation's instruction list, and for **each** modeled combiner op (skipping `parameter`/`get-tuple-element`) it maps the opcode to a `CycleTable::Instruction` and charges `throughput(CT) × count` into `VectorAluAny`. The loop does not stop after the first op — every modeled instruction in `to_apply` contributes a deposit, so a single-op reduction (the common `maximum`/`add`) charges once while a multi-op user reduction charges once per modeled op. The `count` is supplied by the caller (the window-element-per-output count computed above).

### Algorithm

```c
function UpdateCostBasedOnReductionFunction(comp, count, rv):   // @0x130c9a20
    for inst in comp->instructions():               // walks ALL instructions; deposits per modeled op
        switch (inst->opcode()):                     // opcode byte +12
            case 0x29 ')' , 0x52 'R':  continue      // parameter / get-tuple — no cost, skip
            case 0x49 'I', 0x4a 'J':                 // (min/max-class combiner)
                Resource = VectorAluAny(5); CT = 0x20
            case 0x4b 'K':                            // multiply-class
                Resource = VectorAluAny(5); CT = 0x14                    // CT 0x14 for both int and float
                if ElementIsFloating(operand): Resource = GetResource(CT)
            default:
                if (opcode != 0x03 add): FATAL "Reduction Function not modeled: Please file an XLA bug."  // :6870
                Resource = VectorAluAny(5); CT = 0x12                    // CT 0x12 (add)
                if ElementIsFloating(operand): Resource = GetResource(CT)  // remap to the float-op slot

        cyc = GetCyclesForThroughput(CT)             // vtable [CycleTable+0x10]
        rv.Acc(Resource, cyc * count)                // the combiner deposit, charged for this op
        // loop continues to the next instruction — every modeled combiner op in `comp` is charged
```

The opcode→CT classification (`@0x130c9a20`):

| Combiner opcode | Class | CT (float) | CT (int) | Resource | Confidence |
|---|---|---|---|---|---|
| `0x49`/`0x4a` (min/max — the max-pool case) | compare-select | `0x20` | `0x20` | `R[5] VectorAluAny` (no float remap) | CERTAIN |
| `0x4b` (multiply) | mul | `0x14` | `0x14` | `R[5]`; float remaps slot via `GetResource(0x14)` | CERTAIN |
| `0x03` add (the avg/sum-pool case) | add | `0x12` | `0x12` | `R[5]`; float remaps slot via `GetResource(0x12)` | CERTAIN |
| `0x29` (parameter) / `0x52` (get-tuple-element) | structural | — | — | (no deposit) | CERTAIN |
| anything else | — | FATAL (`cost_model.cc:6870`) | — | — | CERTAIN |

> **NOTE —** the count argument is the only thing that ties the combiner to the window area. Lane passes `vol·(window−1)`, Sublane passes `base·(window−1)` plus a separate `4·base`, Major passes the whole `vol`. So `to_apply × window_volume × output` — the flop the HLO-level `HandleReduceWindow` estimator charges — appears here as `throughput(CT) × count`: the combiner runs once per window-element per output, priced at the per-generation throughput of the specific combiner op (max vs add vs multiply), not as a fixed flop.

> **CORRECTION (cost-VII) —** an earlier synthesis listed the Xlu-rate divisor `Target+0x4b0` (and the matmul-rate divisor `Target+0x4ac`) as `ConvCostState` fields. They are **`Target`** fields, populated in `Target::Init` from `TpuSequencerParts::vector_isa()` (`vector_isa[+0xc]` → `Target+0x4ac` and `+0x4b0`). The Lane leaf reads `Target+0x4b0` through the `CostModel`'s `CycleTable` (`[CostModel+0x8]` = `CycleTable`, then its `Target` pointer, then `[Target+0x4b0]`) at `@0x130c98f6` — the same vector-ISA Xlu rate the conv emitter divides its mxres deposit by. It is not a per-reduce-window derate.

---

## How a Pool Is Priced — End to End

Putting the pieces together, a reduce-window's compute cost lands entirely on the vector/cross-lane pipes:

```text
max-pool / avg-pool cost  (per the three leaves)
  R[7] VectorLoad   = output_volume                              (input read into VMEM)
  R[5] VectorAluAny = throughput(combiner CT) · combiner_count   (the to_apply op, once per window-elem)
                    + throughput(CT 0x16) · vol                  (F16 unpack, only when operand is f16)
                    + sublane shuffle                            (Sublane axis only, via GetResource(CT 0x15))
  R[2] Xlu          = throughput(CT 0x1b) / Target[+0x4b0]            (Lane axis only — cross-lane drain)
  R[9..12] MemXfer  = RecordOperandCycles(input)  (+ output DMA when top-level)
```

The bundle cost then comes from `MaxResourceCycles` ([`resource-enum`](resource-enum.md)): the three `VectorAlu*` slots blend at 50% port-balance, the four `MemXfer` slots serial-sum, and `VectorLoad`/`Xlu` join the plain-MAX group. So a pooling op is bound by whichever dominates — the input read (`VectorLoad`), the combiner throughput (`VectorAluAny`), the cross-lane drain (`Xlu`), or the input DMA (`MemXfer`).

The contrast with the conv emitter is exact and instructive: the conv leaf [`RecordConvKernelCycles`](convolution-cost-state.md) deposits into `R[0] Matpush`, `R[1] Matmul`, and `R[2] Xlu` (priced through [`MxuLatencyTable`](mxu-latency-overview.md) at the matmul/matpush rate), because a convolution is a systolic matrix multiply. The reduce-window emitter reuses the *same* `ConvState`, the *same* `Target+0x4b0` Xlu divisor, and the *same* four-`ResourceVector` protocol — but deposits onto `VectorLoad`/`VectorAluAny`/`Xlu`, never the MXU pipes, because a windowed reduction is a vector operation. Same scaffolding, different functional unit.

---

## Considerations

- **Dilation blocks Lane/Sublane.** If `HasBaseDilation` is true, the Lane and Sublane leaves `CHECK`-fail (`has_base_dilation == false`, cost_model.cc:5142/5149). A base-dilated window is therefore only modellable as a Major reduce-window; a reimplementation must route dilated pools to the major path or it will trip the assertion.
- **Layout pins the axis.** `GetReduceWindowType` depends on the *physical* layout (`MakeLogicalToPhysical`), so the same logical reduce-window can classify Lane / Sublane / Major depending on which dimension the layout assignment made most-minor. The cost is layout-sensitive by construction.
- **The two CHECKs on minor-ness** (`input_batch_is_most_minor`, `!output_feature_is_most_minor`) gate the whole real path. They encode the assumption that a conv-lowerable reduce-window keeps batch most-minor and feature not-most-minor; an HLO violating either is not priced by this emitter.
- **Per-gen throughput.** Every `CT 0x12/0x14/0x16/0x1b/0x15/0x20` resolves to a per-generation integer via `GetCyclesForThroughput`. The combiner-op CTs (`0x12` add, `0x14` mul, `0x20` min/max) are generation-invariant in *which* slot they hit (`VectorAluAny`) but not in *how many cycles* — those live on the per-gen cycle-table pages.

---

## Related Components

| Name | Relationship |
|---|---|
| `convolution-cost-state` | the `ConvState`/`ConvCostState` structs this emitter reuses; the conv sibling `RecordConvKernelCycles` |
| `resource-enum` | the 23-slot `ResourceVector`, the `Acc` deposit, and the `MaxResourceCycles` fold |
| `mxu-latency-overview` | the MXU occupancy model the conv path uses and the pooling path does **not** |
| `vf-cycletable` / `jf-cycletable` | the per-`CT` throughput integers `GetCyclesForThroughput` returns |
| `window-description-cost` | the conv/DMA byte+throughput primitive behind `RecordOperandCycles` |
| `slot-vpu` | the LLO VALU slot whose `vadd`/`vmax`/`.xlane` ops the combiner CTs price |

---

## Cross-References

- [ConvolutionCostState](convolution-cost-state.md) — the `ConvState`/`ConvCostState` field map and the conv emitter this is the pooling sibling of
- [WindowDescription Byte-Cost](window-description-cost.md) — the conv/DMA byte+throughput primitive `RecordOperandCycles` builds on
- [VfCycleTable](vf-cycletable.md) — the per-generation `CycleTable::Instruction` → throughput integers the combiner and drain CTs resolve through
- [JF CycleTable](jf-cycletable.md) — the Jellyfish/Dragonfish throughput table
- [Resource Enum (23-slot)](resource-enum.md) — `VectorLoad` / `VectorAluAny` / `Xlu` slots, `Acc`, and the `MaxResourceCycles` overlap reduction
- [MXU Latency Overview](mxu-latency-overview.md) — the MXU reservation model the conv path prices through and the pooling path bypasses
- [Performance Overview](performance-overview.md) — the base per-opcode latency grid behind `GetCyclesForThroughput`
- [VPU (Vector-ALU) Slot](../isa/slot-vpu.md) — the LLO VALU/`.xlane` ops the combiner and reduction CTs charge against
